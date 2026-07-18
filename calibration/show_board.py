#!/usr/bin/env python3
"""
show_board.py - generate & display the ChArUco calibration board (run on your Mac)

Lets you show the board on your screen and point the cameras at it, instead of
printing. Board params match calibrate.py's defaults (7x10, DICT_5X5_1000).

*** CRITICAL: measure the on-screen square, don't trust a nominal size ***
Screen pixel density and OS scaling vary, so after the board opens, put a ruler on
the screen, measure ONE black square edge-to-edge in mm, and pass that number as
--square-mm to calibrate.py (and marker-mm = square-mm * 26/35). The on-screen size
is the source of truth; the --square-mm here only sets the marker-vs-square ratio in
the drawn image, not the physical size.

Screen-display caveats (why printing is still preferred):
  - Glare/reflections off the glossy screen cause missed detections -> dim the room,
    kill reflections, maybe tilt the screen away from light sources.
  - Moire between the screen pixel grid and the sensor can appear -> vary distance.
  - Keep the board's squares SQUARE: this saves a correct-aspect PNG and opens it in
    your OS image viewer (which preserves aspect). Do NOT stretch it to fill a
    differently-shaped window, or the squares distort and calibration is wrong.
  - Move the screen (a tablet/laptop is easiest) to get varied angles/distances, and
    cover the camera frame's edges/corners across your ~20-30 snapshots.

Usage:
    pip install -U opencv-contrib-python numpy
    python3 show_board.py               # generates + opens charuco_board.png
    # then fullscreen the viewer, measure a square, and snapshot from the web panel
"""

import argparse
import os
import subprocess
import sys

try:
    import cv2
    import cv2.aruco as aruco
except ImportError:
    sys.exit("OpenCV not found. Install with:  pip install -U opencv-contrib-python numpy")

if not hasattr(aruco, "CharucoBoard"):
    sys.exit("Your OpenCV lacks the aruco module. Install:  pip install -U opencv-contrib-python")


def main():
    ap = argparse.ArgumentParser(description="Generate/display the ChArUco board.")
    ap.add_argument("--squares-x", type=int, default=7)
    ap.add_argument("--squares-y", type=int, default=10)
    ap.add_argument("--square-mm", type=float, default=35.0,
                    help="sets marker/square RATIO in the image only (measure the screen for real mm)")
    ap.add_argument("--marker-mm", type=float, default=26.0)
    ap.add_argument("--dict", default="DICT_5X5_1000")
    ap.add_argument("--px-per-square", type=int, default=220, help="render resolution per square")
    ap.add_argument("--out", default="charuco_board.png")
    ap.add_argument("--no-open", action="store_true", help="just save the PNG, don't open it")
    args = ap.parse_args()

    dict_const = getattr(aruco, args.dict, None)
    if dict_const is None:
        sys.exit(f"Unknown dictionary '{args.dict}' (e.g. DICT_5X5_1000, DICT_4X4_50).")
    dictionary = aruco.getPredefinedDictionary(dict_const)
    board = aruco.CharucoBoard(
        (args.squares_x, args.squares_y), args.square_mm, args.marker_mm, dictionary)

    # outSize matches squaresX:squaresY exactly so squares stay square (no distortion).
    w = args.squares_x * args.px_per_square
    h = args.squares_y * args.px_per_square
    margin = args.px_per_square // 4
    img = board.generateImage((w, h), marginSize=margin, borderBits=1)

    out = os.path.abspath(args.out)
    cv2.imwrite(out, img)
    print(f"Saved board: {out}")
    print(f"  {args.squares_x}x{args.squares_y} squares, dict={args.dict}, {w}x{h}px")

    if not args.no_open:
        opener = {"darwin": ["open"], "linux": ["xdg-open"]}.get(sys.platform)
        if opener:
            try:
                subprocess.run(opener + [out], check=False)
            except Exception:                        # noqa: BLE001
                print("  (couldn't auto-open; open the PNG yourself)")
        else:
            print("  (open the PNG yourself and fullscreen it)")

    print("\nNext:")
    print("  1. Fullscreen the image viewer (keep the aspect - don't stretch it).")
    print("  2. MEASURE one black square on the screen with a ruler (mm).")
    print("  3. Point the cameras at the screen; snapshot ~20-30x from the web panel,")
    print("     varying angle/distance and covering the frame edges/corners.")
    print("  4. Calibrate with the MEASURED size, e.g.:")
    print("       python3 calibrate.py --images /mnt/video/calib \\")
    print("         --square-mm <measured> --marker-mm <measured*26/35>")


if __name__ == "__main__":
    main()
