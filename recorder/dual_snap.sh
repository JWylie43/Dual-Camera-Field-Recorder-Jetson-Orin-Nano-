#!/usr/bin/env bash
# dual_snap.sh - bring up BOTH IMX477s and grab a few frames from each, keeping
# one JPEG per camera to eyeball. Runs ON THE ORIN (Pi HQ dual overlay active).
# The smoke test for the XVS sync work: proves both sensors stream together at
# low res before any sync wiring/registers enter the picture.
#
# Default is both cameras in ONE pipeline (truly simultaneous start - the
# configuration the sync test needs). SEQ=1 runs them one after another
# instead, which isolates a misbehaving camera from a dual-pipeline problem.
#
# The cameras must be free: sudo systemctl stop camera-rig
#
# Dials (env vars):
#   SEQ=0       1 = capture cam0 then cam1 sequentially instead of together
#   W=1920 H=1080  capture size (1080p = the binned mode, light on the ISP;
#               W=3840 H=2160 for the full-res mode)
#   FPS=30      requested framerate
#   FRAMES=60   frames to run per camera (~2s: lets AE/AWB converge; the
#               LAST frame is the one kept)
#   FLIP=0      nvvidconv flip-method (2 = 180 for upside-down mounting)
#   Q=90        JPEG quality
#
# Output: /tmp/dual_cam0.jpg and /tmp/dual_cam1.jpg
#
# Examples:
#   ./dual_snap.sh                 # both at once, 1080p
#   SEQ=1 ./dual_snap.sh           # one after the other (debugging)
#   W=3840 H=2160 ./dual_snap.sh   # dual full-res through the ISP
set -euo pipefail

SEQ=${SEQ:-0}
W=${W:-1920}; H=${H:-1080}; FPS=${FPS:-30}
FRAMES=${FRAMES:-60}
FLIP=${FLIP:-0}; Q=${Q:-90}

TMPD=$(mktemp -d /tmp/dualsnap.XXXXXX)
trap 'rm -rf "$TMPD"' EXIT

CAPS="video/x-raw(memory:NVMM),width=$W,height=$H,framerate=$FPS/1"

# One camera's pipeline branch: sensor -> ISP -> JPEG per frame. Every frame
# lands in TMPD; only the last (converged) one is kept.
branch() {
  local id=$1
  echo "nvarguscamerasrc sensor-id=$id num-buffers=$FRAMES aeantibanding=3 \
        ! '$CAPS' \
        ! nvvidconv flip-method=$FLIP ! 'video/x-raw,format=I420' \
        ! nvjpegenc quality=$Q \
        ! multifilesink location=$TMPD/cam${id}_%03d.jpg"
}

keep_last() {
  local id=$1
  local last
  last=$(ls "$TMPD"/cam${id}_*.jpg 2>/dev/null | tail -1) || true
  if [[ -z "$last" ]]; then
    echo "!! cam$id produced NO frames"
    return 1
  fi
  local count
  count=$(ls "$TMPD"/cam${id}_*.jpg | wc -l)
  mv "$last" "/tmp/dual_cam${id}.jpg"
  echo ">> cam$id: $count/$FRAMES frames, kept /tmp/dual_cam${id}.jpg ($(du -h /tmp/dual_cam${id}.jpg | cut -f1))"
}

echo ">> ${W}x${H}@$FPS, $FRAMES frames per camera, $([[ "$SEQ" == 1 ]] && echo sequential || echo simultaneous)"

if [[ "$SEQ" == 1 ]]; then
  for id in 0 1; do
    echo ">> starting cam$id..."
    eval gst-launch-1.0 -q -e "$(branch $id)"
  done
else
  eval gst-launch-1.0 -q -e "$(branch 0)" "$(branch 1)"
fi

ok=0
keep_last 0 || ok=1
keep_last 1 || ok=1

echo ">> pull to the Mac:  scp joe@$(hostname -I | awk '{print $1}'):/tmp/dual_cam\\*.jpg ."
exit $ok
