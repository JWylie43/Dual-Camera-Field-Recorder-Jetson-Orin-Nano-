#!/usr/bin/env python3
"""
placeholder_extrinsics.py - write a stereo_extrinsics.json from the rig's
DESIGN numbers, so the stitch pipeline can run before the housing exists.

    python3 placeholder_extrinsics.py --yaw 74 --baseline-mm 60 --out rock-rig

This is a stand-in, not a measurement. It assumes the cameras are perfectly
mounted: pure yaw, no pitch, no roll, and a baseline straight along X. A real
rig is off by a degree or two in every axis, and those errors are exactly what
the stitch seam is sensitive to - so re-run the real calibration
(snap_pair.sh + calibrate.py --use-intrinsics) once the cameras are fixed in
the printed housing, and overwrite this file.

What it is good for: getting the whole pipeline running end to end - pairing,
warping, seam, encode - so the only thing left to change later is the numbers.

Sign convention: yaw is the rotation from the LEFT camera to the RIGHT one
about the vertical axis. If the stitched pano comes out with the two views
swapped or diverging, negate --yaw.
"""

import argparse
import json
import math
import os

ap = argparse.ArgumentParser()
ap.add_argument("--yaw", type=float, default=74.0,
                help="design angle between the cameras, degrees (default 74)")
ap.add_argument("--baseline-mm", type=float, default=60.0,
                help="camera-to-camera spacing in mm (default 60)")
ap.add_argument("--out", default="rock-rig", help="output directory")
args = ap.parse_args()

# Sign matched to the working Orin calibration, whose solved yaw_toe_y was
# NEGATIVE for a right camera toed out to the right - so the stitcher's printed
# "right yaw" comes out with the same sign as a real calibration of this rig.
t = math.radians(-args.yaw)
c, s = math.cos(t), math.sin(t)
# rotation about the vertical (Y) axis
R = [[c, 0.0, s],
     [0.0, 1.0, 0.0],
     [-s, 0.0, c]]
T = [-args.baseline_mm, 0.0, 0.0]

os.makedirs(args.out, exist_ok=True)
path = os.path.join(args.out, "stereo_extrinsics.json")
with open(path, "w") as f:
    json.dump({
        "PLACEHOLDER": True,
        "note": ("design values, NOT a calibration - assumes perfect mounting. "
                 "Replace by running calibrate.py on the housed rig."),
        "rotation_matrix": R,
        "translation_mm": T,
        "baseline_mm": args.baseline_mm,
        "toe_in_angle_deg": args.yaw,
        "rotation_euler_deg": {"pitch_x": 0.0, "yaw_toe_y": -args.yaw, "roll_z": 0.0},
    }, f, indent=2)

print(f"wrote {path}  (yaw {args.yaw} deg, baseline {args.baseline_mm} mm)")
print("PLACEHOLDER - re-run the real calibration once the rig is in its housing.")
