#!/usr/bin/env python3
"""
calibrate.py - ChArUco intrinsic calibration for the stereo rig (run on your Mac)

This is a PROCESSING tool, not something the Orin runs. Workflow:
  1. On the web panel, tap Snapshot ~20-30x with the ChArUco board at varied
     angles/distances, covering the whole frame (see notes in the repo README).
  2. Pull the shots to your Mac:  scp -r joe@joe-desktop:/mnt/video/calib ./calib
  3. Run this:  python3 calibrate.py --images ./calib

Each snapshot is the COMBINED 3840x1200 frame (both cameras). By default this
splits every image into left (0:1920) and right (1920:3840) halves and calibrates
each camera SEPARATELY -> per-camera intrinsics + distortion + FOV. Intrinsics are
mount-independent, so this stays valid however you later mount the cameras; the FOV
it prints is what you use to plan baseline/toe angle for stitching overlap.

Requires OpenCV with the aruco module (>= 4.7):
    pip install -U opencv-contrib-python numpy

Board defaults match the recommended board (7x10, DICT_5X5_1000). IMPORTANT: after
printing, MEASURE a real square with calipers and pass --square-mm / --marker-mm so
the scale is correct.

Outputs (in --out dir): <side>_intrinsics.json  and  <side>_undistort_sample.jpg
"""

import argparse
import glob
import json
import math
import os
import sys

import numpy as np

try:
    import cv2
    import cv2.aruco as aruco
except ImportError:
    sys.exit("OpenCV not found. Install with:  pip install -U opencv-contrib-python numpy")

if not hasattr(aruco, "CharucoDetector"):
    sys.exit("Your OpenCV is too old for this script (needs the >=4.7 aruco API).\n"
             "Upgrade:  pip install -U opencv-contrib-python")


def build_board(cfg):
    dict_const = getattr(aruco, cfg.dict, None)
    if dict_const is None:
        sys.exit(f"Unknown ArUco dictionary '{cfg.dict}'. Examples: DICT_5X5_1000, "
                 f"DICT_4X4_50, DICT_6X6_250.")
    dictionary = aruco.getPredefinedDictionary(dict_const)
    # Units in mm: intrinsics (pixels) are unaffected by the choice; only the
    # extrinsic scale (translation) inherits these units.
    board = aruco.CharucoBoard(
        (cfg.squares_x, cfg.squares_y), cfg.square_mm, cfg.marker_mm, dictionary)
    detector = aruco.CharucoDetector(board)
    return board, detector


def grid_coverage(points, w, h, gx=8, gy=5):
    """Fraction of a gx*gy grid that has at least one detected corner."""
    filled = set()
    for p in points:
        x, y = float(p[0]), float(p[1])
        cx = min(gx - 1, max(0, int(x / w * gx)))
        cy = min(gy - 1, max(0, int(y / h * gy)))
        filled.add((cx, cy))
    return len(filled) / float(gx * gy)


def calibrate_side(name, images, detector, board, cfg, out_dir):
    all_obj, all_img, all_pts = [], [], []
    size = None
    used = 0
    print(f"\n=== {name.upper()} camera ===")
    for path in images:
        img = cv2.imread(path)
        if img is None:
            continue
        # split the combined frame unless --single
        if cfg.single:
            half = img
        else:
            mid = img.shape[1] // 2
            half = img[:, :mid] if name == "left" else img[:, mid:]
        gray = cv2.cvtColor(half, cv2.COLOR_BGR2GRAY)
        if size is None:
            size = (gray.shape[1], gray.shape[0])   # (w, h)

        ch_corners, ch_ids, _, _ = detector.detectBoard(gray)
        n = 0 if ch_ids is None else len(ch_ids)
        if ch_ids is not None and n >= 6:
            obj, imgp = board.matchImagePoints(ch_corners, ch_ids)
            if obj is not None and len(obj) >= 6:
                all_obj.append(obj)
                all_img.append(imgp)
                all_pts.extend(imgp.reshape(-1, 2))
                used += 1
        print(f"  {os.path.basename(path):28} corners: {n}")

    if used < 8:
        print(f"  ! Only {used} usable views for {name} (want >= ~15). "
              f"Take more shots covering the frame edges/corners and re-run.")
        if used < 4:
            return None

    rms, K, dist, _, _ = cv2.calibrateCamera(all_obj, all_img, size, None, None)
    w, h = size
    fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]
    hfov = math.degrees(2 * math.atan(w / (2 * fx)))
    vfov = math.degrees(2 * math.atan(h / (2 * fy)))
    cov = grid_coverage(all_pts, w, h)

    print(f"  images used     : {used}")
    print(f"  image size      : {w} x {h}")
    print(f"  RMS reproj error: {rms:.3f} px   ({'good' if rms < 1.0 else 'high - see notes'})")
    print(f"  focal (fx, fy)  : {fx:.1f}, {fy:.1f} px")
    print(f"  principal (cx,cy): {cx:.1f}, {cy:.1f}")
    print(f"  distortion      : {np.round(dist.ravel(), 4).tolist()}")
    print(f"  >> FOV          : {hfov:.1f} deg horizontal, {vfov:.1f} deg vertical")
    print(f"  frame coverage  : {cov*100:.0f}% of an 8x5 grid "
          f"({'good' if cov > 0.8 else 'thin - add edge/corner shots'})")

    # save results (JSON = git-diffable)
    result = {
        "camera": name, "image_width": w, "image_height": h,
        "rms_reproj_error_px": round(float(rms), 4),
        "camera_matrix": K.tolist(),
        "distortion_coefficients": dist.ravel().tolist(),
        "fov_horizontal_deg": round(hfov, 2),
        "fov_vertical_deg": round(vfov, 2),
        "images_used": used,
        "board": {"squares_x": cfg.squares_x, "squares_y": cfg.squares_y,
                  "square_mm": cfg.square_mm, "marker_mm": cfg.marker_mm,
                  "dictionary": cfg.dict},
    }
    out_json = os.path.join(out_dir, f"{name}_intrinsics.json")
    with open(out_json, "w") as f:
        json.dump(result, f, indent=2)
    print(f"  saved -> {out_json}")

    # undistort the first usable image as a visual sanity check (straight lines)
    for path in images:
        img = cv2.imread(path)
        if img is None:
            continue
        half = img if cfg.single else (
            img[:, :img.shape[1] // 2] if name == "left" else img[:, img.shape[1] // 2:])
        und = cv2.undistort(half, K, dist)
        out_img = os.path.join(out_dir, f"{name}_undistort_sample.jpg")
        cv2.imwrite(out_img, und)
        print(f"  saved -> {out_img}  (eyeball: straight lines should be straight)")
        break

    return result


def main():
    ap = argparse.ArgumentParser(description="ChArUco intrinsic calibration (per camera).")
    ap.add_argument("--images", default="calib", help="folder of calibration JP/PNG snapshots")
    ap.add_argument("--out", default=".", help="where to write results")
    ap.add_argument("--squares-x", type=int, default=7)
    ap.add_argument("--squares-y", type=int, default=10)
    ap.add_argument("--square-mm", type=float, default=35.0,
                    help="MEASURED printed square size in mm")
    ap.add_argument("--marker-mm", type=float, default=26.0,
                    help="MEASURED printed marker size in mm")
    ap.add_argument("--dict", default="DICT_5X5_1000", help="ArUco dictionary name")
    ap.add_argument("--single", action="store_true",
                    help="treat each image as ONE camera (don't split into left/right)")
    args = ap.parse_args()

    files = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.PNG"):
        files += glob.glob(os.path.join(args.images, ext))
    files = sorted(set(files))
    if not files:
        sys.exit(f"No images found in '{args.images}'. "
                 f"Pull them first:  scp -r joe@joe-desktop:/mnt/video/calib ./calib")
    os.makedirs(args.out, exist_ok=True)

    print(f"Found {len(files)} images in {args.images}")
    print(f"Board: {args.squares_x}x{args.squares_y}, square={args.square_mm}mm, "
          f"marker={args.marker_mm}mm, dict={args.dict}")
    print("(If detection is poor, confirm these match your PRINTED board.)")

    board, detector = build_board(args)
    sides = ["single"] if args.single else ["left", "right"]
    results = {}
    for side in sides:
        r = calibrate_side(side, files, detector, board, args, args.out)
        if r:
            results[side] = r

    if len(results) == 2:
        print(f"\nBoth cameras done. Horizontal FOV: "
              f"left {results['left']['fov_horizontal_deg']:.1f} deg, "
              f"right {results['right']['fov_horizontal_deg']:.1f} deg.")
        print("Use that FOV to pick your toe angle:  overlap = FOV - toe_angle.")


if __name__ == "__main__":
    main()
