#!/usr/bin/env bash
# snap.sh - capture ONE full-4K still from an IMX477 through the Argus ISP,
# with every runtime dial settable via env vars. Runs ON THE ORIN. Writes
# /tmp/snap.jpg and prints the settings used. Pair with snap_pull.sh on the
# Mac for the capture -> pull -> eyeball tuning loop.
#
# The camera must be free: sudo systemctl stop camera-rig
#
# Dials (env vars), their meaning, and the tradeoff each one buys:
#   CAM=0        sensor-id (0 or 1)
#   EXP_MS       shutter time in milliseconds (decimals ok: EXP_MS=1.5).
#                Unset = auto exposure. More time = more light = less noise,
#                but anything moving smears. Sports: 1-2. Ceiling: 33 (30fps).
#   GAIN         analog gain, 1-22. Unset = auto within 1-GAIN_MAX. Adds no
#                light - just amplifies, revealing the noise of a short
#                exposure. Keep at 1 when light allows; grain soup past ~8.
#   GAIN_MAX=8   auto-mode gain ceiling (only matters when GAIN is unset)
#   EV=0         auto-exposure bias, -2..2 (only matters in auto: EXP_MS unset)
#   AEREGION     auto-exposure metering rectangle "l t r b 1" in 3840x2160
#                sensor coords (pre-flip). E.g. center: "960 540 2880 1620 1"
#   EE=0.3       edge-enhance (sharpen) strength -1..1. Crisper detail; too
#                high = halos on high-contrast edges + crunchy noise.
#   EE_MODE=2    0 off, 1 fast, 2 high quality
#   TNR=0.15     temporal noise reduction -1..1. Cleaner flat areas; cost is
#                smeared motion + eaten fine texture. Try 0 to see the truth.
#   TNR_MODE=2   0 off, 1 fast, 2 high quality
#   SAT=1.0      saturation 0..2. Taste. 1 = calibrated, 1.3 = punchy.
#   WB=6         white balance: 1 auto, 0 off, presets 2-8 (5 = daylight,
#                6 = cloudy-daylight). A preset keeps color constant across
#                captures; 6 is the rig standard - the post color matrix in
#                grade.sh was measured against it.
#   FLIP=0       nvvidconv flip-method: 2 = 180 (sensor mounted upside down)
#   Q=95         JPEG quality of the saved still
#   SETTLE=45    frames to run before keeping one (AE/AWB/TNR convergence;
#                45 = 1.5 s. Manual exposure still wants ~15 for TNR.)
#   DUR          seconds of VIDEO to record instead of a still. Writes
#                /tmp/snap.mkv (full 4K30 MJPEG) - the motion test: step
#                through frames to judge blur, TNR smear, rolling shutter.
#                Size grows fast (~0.5-1 GB per 5 s at Q=95); keep it short.
#
# Examples:
#   EXP_MS=2 GAIN=1 ./snap.sh                  # manual, sports shutter
#   EE=0 TNR=0 ./snap.sh                       # unprocessed truth (auto exp)
#   EXP_MS=8 GAIN=1 EE=0.6 SAT=1.3 FLIP=2 ./snap.sh
#   DUR=4 EXP_MS=2 GAIN=4 FLIP=2 ./snap.sh     # 4s motion clip, frozen action
set -euo pipefail

CAM=${CAM:-0}
GAIN_MAX=${GAIN_MAX:-8}
EE=${EE:-0.3}; EE_MODE=${EE_MODE:-2}
TNR=${TNR:-0.15}; TNR_MODE=${TNR_MODE:-2}
SAT=${SAT:-1.0}; WB=${WB:-6}
FLIP=${FLIP:-0}; Q=${Q:-95}; SETTLE=${SETTLE:-45}
EV=${EV:-0}

PROPS=(sensor-id="$CAM" num-buffers="$SETTLE"
       ee-mode="$EE_MODE" ee-strength="$EE"
       tnr-mode="$TNR_MODE" tnr-strength="$TNR"
       saturation="$SAT" wbmode="$WB"
       ispdigitalgainrange="1 1" aeantibanding=3)

if [[ -n "${EXP_MS:-}" ]]; then
    ns=$(awk "BEGIN{printf \"%d\", ${EXP_MS}*1000000}")
    PROPS+=(exposuretimerange="$ns $ns")
    desc_exp="${EXP_MS}ms (manual)"
else
    desc_exp="auto"
    [[ "$EV" != 0 ]] && PROPS+=(exposurecompensation="$EV")
    [[ -n "${AEREGION:-}" ]] && PROPS+=(aeregion="$AEREGION")
fi
if [[ -n "${GAIN:-}" ]]; then
    PROPS+=(gainrange="$GAIN $GAIN")
    desc_gain="${GAIN}x (manual)"
else
    PROPS+=(gainrange="1 $GAIN_MAX")
    desc_gain="auto (1-${GAIN_MAX}x)"
fi

echo ">> cam$CAM  exposure: $desc_exp  gain: $desc_gain"
echo ">> ee=$EE (mode $EE_MODE)  tnr=$TNR (mode $TNR_MODE)  sat=$SAT  wb=$WB  flip=$FLIP  q=$Q"

CAPS='video/x-raw(memory:NVMM),width=3840,height=2160,framerate=30/1'

if [[ -n "${DUR:-}" ]]; then
    # Motion clip: settle first (so AE/TNR are converged when it starts),
    # then DUR seconds of full 4K30 MJPEG into a scrub-friendly MKV.
    frames=$(( SETTLE + DUR * 30 ))
    # num-buffers counts from the start, so re-set it to cover settle+clip.
    PROPS=("${PROPS[@]/num-buffers=$SETTLE/num-buffers=$frames}")
    gst-launch-1.0 -q nvarguscamerasrc "${PROPS[@]}" \
        ! "$CAPS" \
        ! nvvidconv flip-method="$FLIP" ! video/x-raw,format=I420 \
        ! nvjpegenc quality="$Q" ! jpegparse ! matroskamux \
        ! filesink location=/tmp/snap.mkv
    echo ">> /tmp/snap.mkv ($(du -h /tmp/snap.mkv | cut -f1))"
else
    TMPD=$(mktemp -d /tmp/snap.XXXXXX)
    trap 'rm -rf "$TMPD"' EXIT
    gst-launch-1.0 -q nvarguscamerasrc "${PROPS[@]}" \
        ! "$CAPS" \
        ! nvvidconv flip-method="$FLIP" ! video/x-raw,format=I420 \
        ! nvjpegenc quality="$Q" \
        ! multifilesink location="$TMPD/f_%05d.jpg"
    last=$(ls "$TMPD" | tail -1)
    mv "$TMPD/$last" /tmp/snap.jpg
    echo ">> /tmp/snap.jpg ($(du -h /tmp/snap.jpg | cut -f1))"
fi
