#!/usr/bin/env python3

from tqdm import trange
import numpy as np
from collections import defaultdict
import os
import os.path
import pandas as pd
import toml
from numpy import array as arr
from glob import glob
from scipy import optimize
import cv2

from .common import make_process_fun, find_calibration_folder, \
    get_video_name, get_cam_name, natural_keys

from camibrate.cameras import CameraGroup

def proj(u, v):
    """Project u onto v"""
    return u * np.dot(v,u) / np.dot(u,u)

def ortho(u, v):
    """Orthagonalize u with respect to v"""
    return u - proj(v, u)

def get_median(all_points_3d, ix):
    pts = all_points_3d[:, ix]
    pts = pts[~np.isnan(pts[:, 0])]
    return np.median(pts, axis=0)


def correct_coordinate_frame(config, all_points_3d, bodyparts):
    """Given a config and a set of points and bodypart names, this function will rotate the coordinate frame to match the one in config"""
    bp_index = dict(zip(bodyparts, range(len(bodyparts))))
    axes_mapping = dict(zip('xyz', range(3)))

    ref_point = config['triangulation']['reference_point']
    axes_spec = config['triangulation']['axes']
    a_dirx, a_l, a_r = axes_spec[0]
    b_dirx, b_l, b_r = axes_spec[1]

    a_dir = axes_mapping[a_dirx]
    b_dir = axes_mapping[b_dirx]

    ## find the missing direction
    done = np.zeros(3, dtype='bool')
    done[a_dir] = True
    done[b_dir] = True
    c_dir = np.where(~done)[0][0]

    a_lv = get_median(all_points_3d, bp_index[a_l])
    a_rv = get_median(all_points_3d, bp_index[a_r])
    b_lv = get_median(all_points_3d, bp_index[b_l])
    b_rv = get_median(all_points_3d, bp_index[b_r])

    a_diff = a_rv - a_lv
    b_diff = ortho(b_rv - b_lv, a_diff)

    M = np.zeros((3,3))
    M[a_dir] = a_diff
    M[b_dir] = b_diff
    M[c_dir] = np.cross(a_diff, b_diff)

    M /= np.linalg.norm(M, axis=1)[:,None]

    center = get_median(all_points_3d, bp_index[ref_point])

    all_points_3d_adj = (all_points_3d - center).dot(M.T)
    center_new = get_median(all_points_3d_adj, bp_index[ref_point])
    all_points_3d_adj = all_points_3d_adj - center_new

    return all_points_3d_adj

def load_pose2d_fnames(fname_dict, offsets_dict):
    cam_names, pose_names = list(zip(*sorted(fname_dict.items())))

    maxlen = 0
    for pose_name in pose_names:
        dd = pd.read_hdf(pose_name)
        length = max(dd.index)+1
        maxlen = max(maxlen, length)

    length = maxlen
    dd = pd.read_hdf(pose_names[0])
    scorer = dd.columns.levels[0][0]
    dd = dd[scorer]

    bodyparts = arr(dd.columns.levels[0])

    # frame, camera, bodypart, xy
    all_points_raw = np.zeros((length, len(cam_names), len(bodyparts), 2))
    all_scores = np.zeros((length, len(cam_names), len(bodyparts)))

    for ix_cam, (cam_name, pose_name) in \
            enumerate(zip(cam_names, pose_names)):
        dd = pd.read_hdf(pose_name)
        scorer = dd.columns.levels[0][0]
        dd = dd[scorer]
        offset = offsets_dict[cam_name]
        index = arr(dd.index)
        for ix_bp, bp in enumerate(bodyparts):
            X = arr(dd[bp])
            all_points_raw[index, ix_cam, ix_bp, :] = X[:, :2] + [offset[0], offset[1]]
            all_scores[index, ix_cam, ix_bp] = X[:, 2]

    return {
        'cam_names': cam_names,
        'points': all_points_raw,
        'scores': all_scores,
        'bodyparts': bodyparts
    }

def load_offsets_dict(config, cam_names, video_folder):
    ## TODO: make the recorder.toml file configurable
    # record_fname = os.path.join(video_folder, 'recorder.toml')

    # if os.path.exists(record_fname):
    #     record_dict = toml.load(record_fname)
    # else:
    #     record_dict = None
    #     # if 'cameras' not in config:
    #     # ## TODO: more detailed error?
    #     #     print("-- no crop windows found")
    #     #     return

    offsets_dict = dict()
    for cname in cam_names:
        # if record_dict is None:
        if 'cameras' not in config or cname not in config['cameras']:
            # print("W: no crop window found for camera {}, assuming no crop".format(cname))
            offsets_dict[cname] = [0, 0]
        else:
            offsets_dict[cname] = config['cameras'][cname]['offset']
        # else:
        #     offsets_dict[cname] = record_dict['cameras'][cname]['video']['ROIPosition']

    return offsets_dict


def triangulate(config,
                calib_folder, video_folder, pose_folder,
                fname_dict, output_fname):

    cam_names = sorted(fname_dict.keys())

    calib_fname = os.path.join(calib_folder, 'calibration.toml')
    cgroup = CameraGroup.load(calib_fname)

    offsets_dict = load_offsets_dict(config, cam_names, video_folder)

    out = load_pose2d_fnames(fname_dict, offsets_dict)
    all_points_raw = out['points']
    all_scores = out['scores']
    bodyparts = out['bodyparts']

    length = all_points_raw.shape[0]
    n_frames, n_cams, n_joints, _ = all_points_raw.shape

    # TODO: configure this threshold
    all_points_raw[all_scores < 0.7] = np.nan

    # TODO: make ransac a configurable option
    points_2d = all_points_raw.swapaxes(0, 1).reshape(n_cams, n_frames*n_joints, 2)

    if config['triangulation']['ransac']:
        points_3d, picked, p2ds, errors = cgroup.triangulate_ransac(
            points_2d, min_cams=3, progress=True)

        all_points_picked = p2ds.reshape(n_cams, n_frames, n_joints, 2) \
                                .swapaxes(0, 1)

        good_points = ~np.isnan(all_points_picked[:, :, :, 0])

        num_cams = np.sum(np.sum(picked, axis=0), axis=1)\
                     .reshape(n_frames, n_joints)\
                     .astype('float')
    else:
        points_3d = cgroup.triangulate(points_2d, progress=True)
        errors = cgroup.reprojection_error(points_3d, points_2d, mean=True)
        good_points = ~np.isnan(all_points_raw[:, :, :, 0])
        num_cams = np.sum(good_points, axis=1).astype('float')

    all_points_3d = points_3d.reshape(n_frames, n_joints, 3)
    all_errors = errors.reshape(n_frames, n_joints)

    all_scores[~good_points] = 2
    scores_3d = np.min(all_scores, axis=1)

    scores_3d[num_cams < 2] = np.nan
    all_errors[num_cams < 2] = np.nan
    num_cams[num_cams < 2] = np.nan

    if 'reference_point' in config['triangulation'] and 'axes' in config['triangulation']:
        all_points_3d_adj = correct_coordinate_frame(config, all_points_3d, bodyparts)
    else:
        all_points_3d_adj = all_points_3d

    dout = pd.DataFrame()
    for bp_num, bp in enumerate(bodyparts):
        for ax_num, axis in enumerate(['x','y','z']):
            dout[bp + '_' + axis] = all_points_3d_adj[:, bp_num, ax_num]
        dout[bp + '_error'] = all_errors[:, bp_num]
        dout[bp + '_ncams'] = num_cams[:, bp_num]
        dout[bp + '_score'] = scores_3d[:, bp_num]

    dout['fnum'] = np.arange(length)

    dout.to_csv(output_fname, index=False)


def process_session(config, session_path):
    pipeline_videos_raw = config['pipeline']['videos_raw']
    pipeline_calibration_results = config['pipeline']['calibration_results']
    pipeline_pose = config['pipeline']['pose_2d']
    pipeline_pose_filter = config['pipeline']['pose_2d_filter']
    pipeline_3d = config['pipeline']['pose_3d']

    calibration_path = find_calibration_folder(config, session_path)
    if calibration_path is None:
        return

    if config['filter']['enabled']:
        pose_folder = os.path.join(session_path, pipeline_pose_filter)
    else:
        pose_folder = os.path.join(session_path, pipeline_pose)

    calib_folder = os.path.join(calibration_path, pipeline_calibration_results)
    video_folder = os.path.join(session_path, pipeline_videos_raw)
    output_folder = os.path.join(session_path, pipeline_3d)

    pose_files = glob(os.path.join(pose_folder, '*.h5'))

    cam_videos = defaultdict(list)

    for pf in pose_files:
        name = get_video_name(config, pf)
        cam_videos[name].append(pf)

    vid_names = cam_videos.keys()
    vid_names = sorted(vid_names, key=natural_keys)

    if len(vid_names) > 0:
        os.makedirs(output_folder, exist_ok=True)

    for name in vid_names:
        print(name)
        fnames = cam_videos[name]
        cam_names = [get_cam_name(config, f) for f in fnames]
        fname_dict = dict(zip(cam_names, fnames))

        output_fname = os.path.join(output_folder, name + '.csv')

        if os.path.exists(output_fname):
            continue

        triangulate(config,
                    calib_folder, video_folder, pose_folder,
                    fname_dict, output_fname)


triangulate_all = make_process_fun(process_session)
