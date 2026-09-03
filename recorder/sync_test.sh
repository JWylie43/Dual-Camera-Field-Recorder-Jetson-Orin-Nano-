#!/usr/bin/env bash
# sync_test.sh - measure frame-timing alignment between the two IMX477s.
# Runs ON THE ORIN. The XVS genlock instrument: streams raw frames from both
# cameras at once (no ISP, no encoder - v4l2 dequeues buffers and drops them),
# logs each frame's hardware start-of-frame timestamp (stamped by the Jetson
# VI block from the one shared monotonic clock), then prints the cam0<->cam1
# offset over time.
#
# Reading the result:
#   offset DRIFTS steadily  -> cameras are free-running (two separate
#                              crystals; typically drifts ~10-100 us/s)
#   offset FROZEN (~0 slope)-> XVS sync is locked
#
# The cameras must be free: sudo systemctl stop camera-rig
#
# Dials (env vars):
#   DUR=30      seconds to stream
#   W=1920 H=1080  sensor mode (1080p binned; W=3840 H=2160 for full res)
#   FPS=60      the 1080p mode's native rate - only used to size the run
#   POKE=0      1 = ONE-SHOT SYNC TEST: after DUR/3 of baseline, write the
#               IMX477 XVS sync registers over i2c mid-stream (cam1 -> sink
#               first, then cam0 -> source). If the pads are wired and work,
#               the printed offset drifts for the first third and freezes
#               after the poke - the whole experiment in one run.
#               (Register recipe: Raspberry Pi's imx477.c trigger_mode.
#               Datasheet wants these set in standby, so mid-stream pokes are
#               out-of-spec - fine for validating the wiring; the real fix is
#               porting the writes into nv_imx477.c.)
#   I2C0=9 I2C1=10  i2c bus of cam0/cam1 (only used with POKE=1)
#
# Examples:
#   ./sync_test.sh                # 30s drift measurement (baseline)
#   DUR=60 POKE=1 ./sync_test.sh # baseline 20s, then live-enable XVS sync
set -euo pipefail

DUR=${DUR:-30}
W=${W:-1920}; H=${H:-1080}; FPS=${FPS:-60}
POKE=${POKE:-0}
I2C0=${I2C0:-9}; I2C1=${I2C1:-10}
ADDR=0x1a
FRAMES=$(( DUR * FPS ))

TMPD=$(mktemp -d /tmp/synctest.XXXXXX)
trap 'rm -rf "$TMPD"' EXIT

stream() {
  local dev=$1 out=$2
  v4l2-ctl -d "/dev/video$dev" \
    --set-fmt-video=width=$W,height=$H \
    --stream-mmap --stream-count=$FRAMES --verbose 2>&1 \
    | grep -o 'ts: [0-9.]*' | cut -d' ' -f2 > "$out"
}

# XVS sync registers (from raspberrypi/linux imx477.c):
#   0x3F0B MC_MODE  0x3041 MS_SEL  0x3040 XVS_IO_CTRL  0x4B81 EXTOUT_EN
# source = 1/1/1/1, sink = 1/0/0/0
poke_regs() {
  local bus=$1 v=$2   # v=1 source, v=0 sink
  sudo i2ctransfer -f -y "$bus" w3@$ADDR 0x3f 0x0b 0x01
  sudo i2ctransfer -f -y "$bus" w3@$ADDR 0x30 0x41 "0x0$v"
  sudo i2ctransfer -f -y "$bus" w3@$ADDR 0x30 0x40 "0x0$v"
  sudo i2ctransfer -f -y "$bus" w3@$ADDR 0x4b 0x81 "0x0$v"
}

echo ">> streaming ${W}x${H} from both cameras for ${DUR}s ($FRAMES frames)..."
stream 0 "$TMPD/cam0.ts" & P0=$!
stream 1 "$TMPD/cam1.ts" & P1=$!

if [[ "$POKE" == 1 ]]; then
  sleep $(( DUR / 3 ))
  echo ">> poking XVS sync registers: cam1(bus $I2C1) -> SINK, cam0(bus $I2C0) -> SOURCE"
  poke_regs "$I2C1" 0
  poke_regs "$I2C0" 1
fi

wait $P0 $P1
echo ">> cam0: $(wc -l < "$TMPD/cam0.ts") frames, cam1: $(wc -l < "$TMPD/cam1.ts") frames"

python3 - "$TMPD/cam0.ts" "$TMPD/cam1.ts" <<'EOF'
import sys, bisect

def load(p):
    with open(p) as f:
        ts = [float(l) for l in f if l.strip()]
    return ts

a, b = load(sys.argv[1]), load(sys.argv[2])
if len(a) < 10 or len(b) < 10:
    sys.exit("!! too few frames captured to analyze")

t0 = min(a[0], b[0])

# For each cam0 frame, signed offset to the NEAREST cam1 frame (ms). With
# free-running cameras this sawtooths through +-half a frame period as they
# drift past each other; when locked it sits flat.
pts = []
for t in a:
    i = bisect.bisect_left(b, t)
    cands = [b[j] for j in (i - 1, i) if 0 <= j < len(b)]
    near = min(cands, key=lambda x: abs(x - t))
    pts.append((t - t0, (t - near) * 1e3))

# one line per second: mean offset in that second
print("\n   t(s)   cam0-cam1 offset (ms)")
buckets = {}
for rel, off in pts:
    buckets.setdefault(int(rel), []).append(off)
for s in sorted(buckets):
    v = buckets[s]
    print(f"  {s:5d}   {sum(v)/len(v):+9.3f}")

# drift rate: compare mean offset of first and last 3 seconds
secs = sorted(buckets)
head = [o for s in secs[:3] for o in buckets[s]]
tail = [o for s in secs[-3:] for o in buckets[s]]
span = (secs[-1] - secs[0]) or 1
drift = (sum(tail)/len(tail) - sum(head)/len(head)) / span * 1e3  # us/s
last = buckets[secs[-1]]
jitter = (max(last) - min(last)) * 1e3  # us peak-to-peak, final second

print(f"\n>> drift: {drift:+.1f} us/s   jitter (last second): {jitter:.0f} us p-p")
if abs(drift) < 5 and jitter < 500:
    print(">> verdict: LOCKED - offset is frozen; XVS sync is working")
else:
    print(">> verdict: FREE-RUNNING - offset drifts; no sync (expected before")
    print(">>          wiring + register setup, or the XVS line isn't working)")
EOF
