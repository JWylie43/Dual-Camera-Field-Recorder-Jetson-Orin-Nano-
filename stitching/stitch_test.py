#!/usr/bin/env python3
"""
stitch_test.py - single-frame stereo panorama test using the rig's CALIBRATION.

Mac/Orin-runnable sanity check for the calibration-based stitch. It:
  1. Splits one combined 3840x1200 snapshot into left (0:1920) / right (1920:3840)
  2. Undistorts each half with its own intrinsics (left/right_intrinsics.json)
  3. Warps both onto a common CYLINDER using the stereo extrinsic rotation R
     (from stereo_extrinsics.json) - i.e. it aligns the cameras purely from your
     measured geometry, no feature matching
  4. Blends the overlap into a panorama, and writes a 50/50 overlay so you can
     eyeball how well the calibration aligns the two views.

NOTE: this uses ROTATION only (the usual assumption for panoramic stitching of a
close-baseline rig). The translation/baseline causes parallax that rotation alone
can't remove; for distant scenes it's negligible. The production video stitcher
(main.cpp) instead aligns with feature-homography - this script is the calibration
counterpart for quick iteration.

Usage:
    python3 stitch_test.py --image ../calibration/images/calib_114.jpg --calib-dir ../calibration --out stitch_out
"""

import argparse
import json
import os
import sys

import numpy as np

try:
    import cv2
except ImportError:
    sys.exit("OpenCV not found. Use the repo venv:  ../.venv/bin/python stitch_test.py ...")


def load_intrinsics(path):
    with open(path) as f:
        j = json.load(f)
    K = np.array(j["camera_matrix"], dtype=float)
    D = np.array(j["distortion_coefficients"], dtype=float).reshape(1, -1)
    return K, D


def project(dirs, K, w, h):
    """Project (H,W,3) ray directions through pinhole K -> (mapx, mapy, valid)."""
    p = dirs @ K.T
    z = p[..., 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        u = p[..., 0] / z
        v = p[..., 1] / z
    valid = (z > 1e-6) & (u >= 0) & (u < w) & (v >= 0) & (v < h)
    u = np.where(valid, u, -1).astype(np.float32)
    v = np.where(valid, v, -1).astype(np.float32)
    return u, v, valid


def main():
    ap = argparse.ArgumentParser(description="Extrinsics-based single-frame stereo stitch test.")
    ap.add_argument("--image", required=True, help="combined LEFT|RIGHT snapshot (e.g. a calib frame)")
    ap.add_argument("--calib-dir", default="../calibration",
                    help="dir holding left/right_intrinsics.json + stereo_extrinsics.json")
    ap.add_argument("--out", default="stitch_out", help="output folder")
    ap.add_argument("--fcyl", type=float, default=None,
                    help="cylinder focal length px (default: left fx)")
    ap.add_argument("--no-blend", action="store_true",
                    help="hard seam instead of averaging the overlap")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    img = cv2.imread(args.image)
    if img is None:
        sys.exit(f"Cannot read image: {args.image}")
    mid = img.shape[1] // 2
    left, right = img[:, :mid], img[:, mid:]
    h, w = left.shape[:2]

    KL, DL = load_intrinsics(os.path.join(args.calib_dir, "left_intrinsics.json"))
    KR, DR = load_intrinsics(os.path.join(args.calib_dir, "right_intrinsics.json"))
    with open(os.path.join(args.calib_dir, "stereo_extrinsics.json")) as f:
        ext = json.load(f)
    R = np.array(ext["rotation_matrix"], dtype=float)   # maps LEFT rays -> RIGHT rays

    # 1) undistort each half to a clean pinhole image (intrinsics preserved)
    undL = cv2.undistort(left, KL, DL)
    undR = cv2.undistort(right, KR, DR)

    # 2) angular span of the panorama, from the two cameras' FOV + divergence
    fcyl = args.fcyl or float(KL[0, 0])
    half_l = np.arctan(w / (2 * KL[0, 0]))              # left half-HFOV (rad)
    half_r = np.arctan(w / (2 * KR[0, 0]))              # right half-HFOV
    axis_r_in_left = R.T @ np.array([0.0, 0.0, 1.0])    # right optical axis, in left frame
    yaw_r = np.arctan2(axis_r_in_left[0], axis_r_in_left[2])
    pad = np.radians(3)
    theta_min = min(-half_l, yaw_r - half_r) - pad
    theta_max = max(half_l, yaw_r + half_r) + pad
    vhalf = np.arctan(h / (2 * KL[1, 1]))               # vertical half-FOV

    out_w = min(int((theta_max - theta_min) * fcyl), 12000)
    out_h = min(int(2 * np.tan(vhalf) * fcyl), 4000)
    print(f"left half {left.shape}, right half {right.shape}")
    print(f"right axis yaw in left frame: {np.degrees(yaw_r):.1f} deg")
    print(f"panorama span: {np.degrees(theta_max - theta_min):.1f} deg  "
          f"overlap ~{np.degrees(min(half_l, yaw_r + half_r) - max(-half_l, yaw_r - half_r)):.1f} deg")
    print(f"output canvas: {out_w} x {out_h}")

    # 3) cylinder ray grid, expressed in the LEFT camera frame
    xs = np.arange(out_w)
    ys = np.arange(out_h)
    theta = theta_min + xs / fcyl
    hval = (ys - out_h / 2.0) / fcyl
    TH, HV = np.meshgrid(theta, hval)
    dirs = np.stack([np.sin(TH), HV, np.cos(TH)], axis=-1)   # (out_h, out_w, 3)

    # 4) project the cylinder rays into each camera and remap
    uL, vL, okL = project(dirs, KL, w, h)
    uR, vR, okR = project(dirs @ R.T, KR, w, h)            # rotate rays into right frame
    warpL = cv2.remap(undL, uL, vL, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    warpR = cv2.remap(undR, uR, vR, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)

    okL3, okR3 = okL[..., None], okR[..., None]

    # 5) composite
    if args.no_blend:
        pano = np.where(okL3, warpL, warpR).astype(np.uint8)
    else:
        wsum = okL3.astype(float) + okR3.astype(float)
        wsum[wsum == 0] = 1
        pano = ((warpL.astype(float) * okL3 + warpR.astype(float) * okR3) / wsum).astype(np.uint8)

    overlap = cv2.addWeighted(warpL, 0.5, warpR, 0.5, 0.0)   # alignment check

    pano_path = os.path.join(args.out, "pano.jpg")
    overlap_path = os.path.join(args.out, "overlap_5050.jpg")
    cv2.imwrite(pano_path, pano)
    cv2.imwrite(overlap_path, overlap)
    print(f"saved -> {pano_path}")
    print(f"saved -> {overlap_path}  (in the overlap band, a well-aligned rig shows"
          f" features as a single sharp image, not a double/ghost)")


if __name__ == "__main__":
    main()
