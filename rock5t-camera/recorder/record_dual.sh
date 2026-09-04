#!/usr/bin/env bash
# record_dual.sh - dual genlocked IMX477 recording on the ROCK 5T. DRAFT:
# written before first hardware bring-up; device discovery and encoder
# element names verified against docs, not yet against the running board.
#
# Pipeline per camera (the Rock replacement for the Orin's nvargus MJPEG
# path - the RK3588 has a real H.265 encoder, the Orin Nano does not):
#   rkisp mainpath (NV12, ISP output w/ imx477_RPI-HQ IQ) -> mpph265enc
#   -> matroskamux -> one MKV per camera
#
# Two INDEPENDENT files by design: fault isolation, full per-camera
# timestamps/metadata, no live cross-coupling. Left/right pairing happens at
# stitch time on the Mac via the genlocked hardware timestamps. Nothing here
# checks one camera against the other - by design (see ROCK5T_CAMERA.md).
#
# Dials (env vars):
#   W=3840 H=2160 FPS=30   capture mode (must match a driver mode:
#                          4056x3040@10 | 3840x2160@30 | 2028x1520@40)
#   DUR=15                 seconds
#   BR=28000000            H.265 bitrate per camera (28 Mbit; the rig's
#                          quality target band is 25-30M at 4K30)
#   OUT=.                  output directory
#   CODEC=h265             h265 | h264 (mpph265enc / mpph264enc)
#
# Genlock note: start order does not matter for lock (the sink re-locks on
# every master pulse), but the source camera (CAM0) must stay streaming for
# as long as the sink records.
set -euo pipefail

W=${W:-3840}; H=${H:-2160}; FPS=${FPS:-30}
DUR=${DUR:-15}
BR=${BR:-28000000}
OUT=${OUT:-.}
CODEC=${CODEC:-h265}
FRAMES=$(( DUR * FPS ))
STAMP=$(date +%F_%H-%M-%S)

case "$CODEC" in
  h265) ENC="mpph265enc"; PARSE="h265parse" ;;
  h264) ENC="mpph264enc"; PARSE="h264parse" ;;
  *) echo "CODEC must be h265 or h264"; exit 1 ;;
esac

# Discover the two rkisp mainpath video nodes (one per ISP, one per camera).
# Sorted by device path so index 0 = isp0 = CAM0 (genlock source).
mapfile -t DEVS < <(
  for v in /sys/class/video4linux/video*; do
    if grep -q "rkisp_mainpath" "$v/name" 2>/dev/null; then
      echo "/dev/$(basename "$v")"
    fi
  done | sort)

if [[ ${#DEVS[@]} -ne 2 ]]; then
  echo "!! expected 2 rkisp_mainpath nodes, found ${#DEVS[@]}: ${DEVS[*]:-none}"
  echo "   (driver loaded? overlay active? check: v4l2-ctl --list-devices)"
  exit 1
fi

echo ">> cam0(source)=${DEVS[0]}  cam1(sink)=${DEVS[1]}"
echo ">> ${W}x${H}@$FPS, $DUR s, $CODEC @ $(( BR / 1000000 )) Mbit/cam"

FILES=()
PIDS=()
for i in 0 1; do
  f="$OUT/rock_cam${i}_$STAMP.mkv"
  FILES+=("$f")
  gst-launch-1.0 -q -e \
    v4l2src device="${DEVS[$i]}" num-buffers=$FRAMES ! \
    "video/x-raw,format=NV12,width=$W,height=$H,framerate=$FPS/1" ! \
    queue max-size-buffers=8 ! \
    $ENC bps=$BR bps-max=$(( BR * 3 / 2 )) ! \
    $PARSE ! matroskamux ! filesink location="$f" &
  PIDS+=($!)
done

wait "${PIDS[@]}" || true

# Honest completion check: frames actually in each file vs requested.
echo
for f in "${FILES[@]}"; do
  if command -v ffprobe >/dev/null; then
    n=$(ffprobe -v error -count_packets -select_streams v:0 \
        -show_entries stream=nb_read_packets -of csv=p=0 "$f" 2>/dev/null || echo "?")
    echo ">> $f : $(du -h "$f" | cut -f1), $n/$FRAMES frames"
  else
    echo ">> $f : $(du -h "$f" | cut -f1) (install ffprobe for a frame count)"
  fi
done
