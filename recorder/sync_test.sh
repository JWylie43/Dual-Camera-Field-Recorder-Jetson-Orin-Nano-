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
#   FPS=30      assumed delivery rate, only used to size the run. The 1080p
#               mode advertises 60fps but delivers 30 via this v4l2 path
#               (measured); if the run finishes early/late the analysis is
#               unaffected - everything is computed from real timestamps.
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
W=${W:-1920}; H=${H:-1080}; FPS=${FPS:-30}
POKE=${POKE:-0}
I2C0=${I2C0:-9}; I2C1=${I2C1:-10}
ADDR=0x1a
FRAMES=$(( DUR * FPS ))

TMPD=$(mktemp -d /tmp/synctest.XXXXXX)
trap 'rm -rf "$TMPD"' EXIT

# bypass_mode=0 is required for direct v4l2 capture on Jetson (default routes
# the VI to Argus and the stream hangs with no frames). The timeout is a
# backstop so a wedged stream can't hang the test forever.
stream() {
  local dev=$1 out=$2
  timeout $(( DUR * 3 + 15 )) v4l2-ctl -d "/dev/video$dev" \
    --set-fmt-video=width=$W,height=$H \
    --set-ctrl bypass_mode=0 \
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

wait $P0 || true
wait $P1 || true
echo ">> cam0: $(wc -l < "$TMPD/cam0.ts") frames, cam1: $(wc -l < "$TMPD/cam1.ts") frames"

python3 - "$TMPD/cam0.ts" "$TMPD/cam1.ts" <<'EOF'
import sys, bisect

def load(p):
    # keep only strictly-increasing timestamps: a glitched stream (e.g. the
    # mid-run register poke) can emit duplicate/backwards ts lines
    ts, last = [], -1.0
    with open(p) as f:
        for l in f:
            if not l.strip():
                continue
            v = float(l)
            if v > last:
                ts.append(v)
                last = v
    return ts

a, b = load(sys.argv[1]), load(sys.argv[2])
if len(a) < 10 or len(b) < 10:
    sys.exit("!! too few frames captured to analyze")

# only compare the window where BOTH cameras were streaming - one stream
# ending early otherwise produces huge junk offsets at the tail
lo, hi = max(a[0], b[0]), min(a[-1], b[-1])
t0 = lo

# For each cam0 frame, signed offset to the NEAREST cam1 frame (ms). With
# free-running cameras this sawtooths through +-half a frame period as they
# drift past each other; when locked it sits flat.
pts = []
for t in a:
    if t < lo or t > hi:
        continue
    i = bisect.bisect_left(b, t)
    cands = [b[j] for j in (i - 1, i) if 0 <= j < len(b)]
    near = min(cands, key=lambda x: abs(x - t))
    pts.append((t - t0, (t - near) * 1e3))
if len(pts) < 10:
    sys.exit("!! streams barely overlap - too little common data to analyze")

# one line per second: mean offset in that second
print("\n   t(s)   cam0-cam1 offset (ms)")
buckets = {}
for rel, off in pts:
    buckets.setdefault(int(rel), []).append(off)
for s in sorted(buckets):
    v = buckets[s]
    print(f"  {s:5d}   {sum(v)/len(v):+9.3f}")

# drift rate over the FINAL 10 seconds (least-squares slope of the
# per-second means) - so a POKE run is judged on its post-poke state, not
# on the deliberately-drifting baseline at the start
secs = sorted(buckets)
tail_secs = secs[-10:]
xs = tail_secs
ys = [sum(buckets[s]) / len(buckets[s]) for s in tail_secs]
n = len(xs)
mx, my = sum(xs) / n, sum(ys) / n
den = sum((x - mx) ** 2 for x in xs) or 1
drift = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den * 1e3  # us/s
last = buckets[secs[-1]]
jitter = (max(last) - min(last)) * 1e3  # us peak-to-peak, final second

print(f"\n>> drift (last {len(tail_secs)}s): {drift:+.2f} us/s   jitter (last second): {jitter:.0f} us p-p")
# A locked pair is FLAT - drift indistinguishable from zero. Free-running
# crystals show a steady linear creep (even well-matched ones drift ~3 us/s).
if abs(drift) < 0.3 and jitter < 500:
    print(">> verdict: LOCKED - offset is frozen; XVS sync is working")
else:
    print(">> verdict: FREE-RUNNING - offset drifts; no sync (expected before")
    print(">>          wiring + register setup, or the XVS line isn't working)")
EOF
