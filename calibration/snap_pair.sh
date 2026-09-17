#!/usr/bin/env bash
# snap_pair.sh - capture ONE calibration pair (cam0 + cam1 full-res stills).
#
# Run on the ROCK, once per ChArUco board pose:
#     ./snap_pair.sh
# Saves ~/calib/cam0_NNN.png + cam1_NNN.png (auto-numbered). Hold the board
# STILL for the ~4 seconds this takes - captures are sequential, and a static
# board needs no genlock.
#
# Afterwards pull to the Mac and solve (from the repo's calibration/ dir):
#     scp -r rock:~/calib images-rock
#     python3 calibrate.py --left-glob 'images-rock/cam0_*.png' \
#                          --right-glob 'images-rock/cam1_*.png'
set -euo pipefail

OUT=${OUT:-$HOME/calib}
mkdir -p "$OUT"
n=$(ls "$OUT"/cam0_*.png 2>/dev/null | wc -l)
idx=$(printf '%03d' "$n")

for c in 0 1; do
  if [ "$c" = 0 ]; then dev=/dev/video22; else dev=/dev/video31; fi
  v4l2-ctl -d "$dev" --set-fmt-video=width=3840,height=2160,pixelformat=NV12 \
    --stream-mmap --stream-count=45 --stream-to=/tmp/cal.nv12
  ffmpeg -loglevel error -f rawvideo -pix_fmt nv12 -s 3840x2160 -i /tmp/cal.nv12 \
    -update 1 -y "$OUT/cam${c}_${idx}.png"
done

echo "pair $idx saved -> $OUT  ($(ls "$OUT" | wc -l) files total)"
