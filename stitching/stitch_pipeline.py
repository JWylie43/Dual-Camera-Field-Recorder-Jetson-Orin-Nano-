#!/usr/bin/env python3
"""
stitch_pipeline.py - calibration-driven cylindrical stitch (logical port of main.cpp,
                     with NO feature detection / NO findHomography).

This mirrors the C++ StitchingApplication's actual processing, but replaces its
feature-matched homography with your measured stereo extrinsics. There is no BRISK,
no matcher, no findHomography anywhere - the rotation R aligns the cameras.

Per-camera it folds three things into ONE remap (as the C++ combined-map does):
    undistortion (distortion model)  +  cylindrical projection  +  rotation R
Then it composites the two warped halves with a HARD SEAM (C++ processAndStitch
style: left pixels left of the seam, right pixels right of it).

Correspondence to main.cpp:
    build_cyl_map()      ~ initUndistortCylindrialWarpMap + buildWarpMap + combineUndistortionAndWarp
    R (extrinsics)       ~ replaces the feature `homographyMatrix`
    hard-seam composite  ~ processAndStitchVideos / testWarpPipeline seam logic
    --degrees/--seam/--box ~ the same config knobs (auto-derived if omitted)

Usage:
    python3 stitch_pipeline.py --image ../calibration/images/calib_114.jpg \
        --calib-dir ../calibration --out pipeline_out
"""

import argparse
import json
import os
import sys

import numpy as np

try:
    import cv2
except ImportError:
    sys.exit("OpenCV not found. Use the repo venv:  ../.venv/bin/python stitch_pipeline.py ...")


def load_intrinsics(path):
    with open(path) as f:
        j = json.load(f)
    K = np.array(j["camera_matrix"], dtype=float)
    D = np.array(j["distortion_coefficients"], dtype=float).ravel()
    return K, D


def apply_distortion(x, y, D):
    """Forward Brown-Conrady model: undistorted normalized (x,y) -> distorted (xd,yd).
    Matches OpenCV's model (k1,k2,p1,p2,k3[,k4,k5,k6]); mirrors the C++ distortion step."""
    k1 = D[0] if len(D) > 0 else 0.0
    k2 = D[1] if len(D) > 1 else 0.0
    p1 = D[2] if len(D) > 2 else 0.0
    p2 = D[3] if len(D) > 3 else 0.0
    k3 = D[4] if len(D) > 4 else 0.0
    k4 = D[5] if len(D) > 5 else 0.0
    k5 = D[6] if len(D) > 6 else 0.0
    k6 = D[7] if len(D) > 7 else 0.0
    r2 = x * x + y * y
    radial = (1 + k1 * r2 + k2 * r2**2 + k3 * r2**3) / (1 + k4 * r2 + k5 * r2**2 + k6 * r2**3)
    xd = x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x)
    yd = y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y
    return xd, yd


def build_cyl_map(K, D, R_cam_from_left, theta, hval, w, h):
    """ONE remap: common-cylinder pixel -> source pixel in this camera's DISTORTED image.
    cylinder ray (left frame) -> rotate into camera -> normalize -> distort -> K.
    R_cam_from_left = identity for the left camera, R for the right."""
    TH, HV = np.meshgrid(theta, hval)
    dirs = np.stack([np.sin(TH), HV, np.cos(TH)], axis=-1)     # rays in LEFT frame
    cam = dirs @ R_cam_from_left.T                             # rays in THIS camera's frame
    z = cam[..., 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        xn = cam[..., 0] / z
        yn = cam[..., 1] / z
    xd, yd = apply_distortion(xn, yn, D)
    u = K[0, 0] * xd + K[0, 2]
    v = K[1, 1] * yd + K[1, 2]
    valid = (z > 1e-6) & (u >= 0) & (u < w) & (v >= 0) & (v < h)
    mapx = np.where(valid, u, -1).astype(np.float32)
    mapy = np.where(valid, v, -1).astype(np.float32)
    return mapx, mapy, valid


def main():
    ap = argparse.ArgumentParser(description="Calibration-driven cylindrical stitch (C++ port, no features).")
    ap.add_argument("--image", required=True, help="combined LEFT|RIGHT frame")
    ap.add_argument("--calib-dir", default="../calibration",
                    help="dir with left/right_intrinsics.json + stereo_extrinsics.json")
    ap.add_argument("--out", default="pipeline_out")
    ap.add_argument("--fcyl", type=float, default=None, help="cylinder focal px (default: left fx)")
    ap.add_argument("--degrees", type=float, default=0.0, help="horizon roll correction (C++ 'degrees')")
    ap.add_argument("--seam", type=int, default=None, help="hard-seam column (default: overlap center)")
    ap.add_argument("--blend", action="store_true", help="feather instead of the C++ hard seam")
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
        R = np.array(json.load(f)["rotation_matrix"], dtype=float)   # LEFT-frame ray -> RIGHT-frame ray

    # ---- cylinder canvas from FOV + divergence (no hardcoded box needed) ----
    fcyl = args.fcyl or float(KL[0, 0])
    half_l = np.arctan(w / (2 * KL[0, 0]))
    half_r = np.arctan(w / (2 * KR[0, 0]))
    yaw_r = np.arctan2((R.T @ [0, 0, 1.0])[0], (R.T @ [0, 0, 1.0])[2])
    pad = np.radians(3)
    theta_min = min(-half_l, yaw_r - half_r) - pad
    theta_max = max(half_l, yaw_r + half_r) + pad
    vhalf = np.arctan(h / (2 * KL[1, 1]))
    out_w = min(int((theta_max - theta_min) * fcyl), 12000)
    out_h = min(int(2 * np.tan(vhalf) * fcyl), 4000)
    theta = theta_min + np.arange(out_w) / fcyl
    hval = (np.arange(out_h) - out_h / 2.0) / fcyl

    # ---- one combined remap per camera (undistort + cylinder + rotation) ----
    mapLx, mapLy, okL = build_cyl_map(KL, DL, np.eye(3), theta, hval, w, h)
    mapRx, mapRy, okR = build_cyl_map(KR, DR, R, theta, hval, w, h)
    warpL = cv2.remap(left, mapLx, mapLy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    warpR = cv2.remap(right, mapRx, mapRy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)

    # ---- composite (C++ hard seam by default) ----
    cols_both = np.where(okL.any(0) & okR.any(0))[0]
    seam = args.seam if args.seam is not None else (
        int(cols_both.mean()) if len(cols_both) else out_w // 2)
    print(f"panorama {out_w}x{out_h}, right yaw {np.degrees(yaw_r):.1f} deg, "
          f"overlap cols [{cols_both.min() if len(cols_both) else 0}..{cols_both.max() if len(cols_both) else 0}], seam @ {seam}")

    if args.blend:
        okL3, okR3 = okL[..., None].astype(float), okR[..., None].astype(float)
        wsum = okL3 + okR3
        wsum[wsum == 0] = 1
        pano = ((warpL * okL3 + warpR * okR3) / wsum).astype(np.uint8)
    else:
        pano = np.zeros_like(warpL)
        pano[:, :seam] = warpL[:, :seam]
        pano[:, seam:] = warpR[:, seam:]

    # ---- optional horizon roll (C++ 'degrees') ----
    if args.degrees:
        M = cv2.getRotationMatrix2D((out_w / 2, out_h / 2), args.degrees, 1.0)
        pano = cv2.warpAffine(pano, M, (out_w, out_h))

    overlap = cv2.addWeighted(warpL, 0.5, warpR, 0.5, 0.0)
    cv2.imwrite(os.path.join(args.out, "pano.jpg"), pano)
    cv2.imwrite(os.path.join(args.out, "overlap_5050.jpg"), overlap)
    print(f"saved -> {os.path.join(args.out, 'pano.jpg')}  (hard seam @ col {seam})")
    print(f"saved -> {os.path.join(args.out, 'overlap_5050.jpg')}")


if __name__ == "__main__":
    main()
