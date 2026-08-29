#!/usr/bin/env bash
# Short raw-quality capture: full 4K30 from the sensors straight to
# uncompressed YUV on the NVMe, bypassing every encoder (no JPEG, no H.264).
# For judging what the cameras/ISP actually deliver without the encode
# bottleneck. Output is .y4m (raw I420 frames with a small text header, so
# ffplay/ffmpeg know the geometry - the pixels themselves are untouched).
#
# SIZE WARNING: raw 4K30 is ~373 MB/s per camera -> ~5.6 GB per camera for
# the default 15 seconds (~11 GB for both). Recording both at once also
# needs ~750 MB/s sustained NVMe writes; if the drive can't keep up, frames
# stall - the printed frame count at the end is the honest check.
#
# Usage (stop the recorder first - it owns the sensors):
#   sudo systemctl stop camera-rig
#   ./raw_capture.sh              # both cameras, 15 s
#   ./raw_capture.sh 1            # just cam1, 15 s
#   ./raw_capture.sh both 5       # both cameras, 5 s
#
# View on the Mac after copying:  ffplay game_raw_cam1_*.y4m
set -euo pipefail

CAM="${1:-both}"          # 0 | 1 | both
DUR="${2:-15}"            # seconds
W=3840; H=2160; FPS=30    # nv_imx477 mode 0 - full pixel readout
FRAMES=$(( DUR * FPS ))
OUT=/mnt/video
STAMP=$(date +%F_%H-%M-%S)

# Same ISP tuning the recorder bakes in (see server.py), so this shows the
# exact image quality recordings are built from. Quotes are re-parsed by the
# eval below.
TUNING='tnr-mode=2 tnr-strength=0.5 ee-mode=2 ee-strength=0.3 gainrange="1 8" ispdigitalgainrange="1 2" aeantibanding=3'

branch() {
  local id=$1 file=$2
  # nvvidconv pulls the frame out of NVMM into system memory as I420; the
  # 512MB queue absorbs NVMe write hiccups without eating all 8GB of RAM.
  echo "nvarguscamerasrc sensor-id=$id num-buffers=$FRAMES $TUNING \
        ! 'video/x-raw(memory:NVMM),width=$W,height=$H,framerate=$FPS/1' \
        ! nvvidconv ! 'video/x-raw,format=I420' \
        ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=536870912 \
        ! y4menc ! filesink location=$file"
}

DESC=""
FILES=()
if [[ "$CAM" == "both" || "$CAM" == "0" ]]; then
  f="$OUT/game_raw_cam0_$STAMP.y4m"; FILES+=("$f"); DESC+=" $(branch 0 "$f")"
fi
if [[ "$CAM" == "both" || "$CAM" == "1" ]]; then
  f="$OUT/game_raw_cam1_$STAMP.y4m"; FILES+=("$f"); DESC+=" $(branch 1 "$f")"
fi
[[ -n "$DESC" ]] || { echo "usage: $0 [0|1|both] [seconds]"; exit 1; }

echo ">> capturing $FRAMES frames ($DUR s) of raw ${W}x${H}@$FPS I420"
echo ">> expect ~$(( FRAMES * W * H * 3 / 2 / 1000000 )) MB per camera"
eval gst-launch-1.0 -e "$DESC"

# Honest completion check: frames actually on disk vs requested, computed
# from the file size (y4m = one header line, then 'FRAME\n' + W*H*1.5 bytes
# per frame).
echo
PER_FRAME=$(( 6 + W * H * 3 / 2 ))
for f in "${FILES[@]}"; do
  size=$(stat -c%s "$f")
  hdr=$(head -c 256 "$f" | head -n 1 | wc -c)
  echo ">> $f : $(du -h "$f" | cut -f1), $(( (size - hdr) / PER_FRAME ))/$FRAMES frames"
done
echo ">> copy to the Mac:  scp joe@$(hostname -I | awk '{print $1}'):\"$OUT/game_raw_*_$STAMP.y4m\" ."
