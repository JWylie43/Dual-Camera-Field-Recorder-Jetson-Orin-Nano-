#!/usr/bin/env python3
"""
pair_check.py - verify a Veery take's two camera files before stitching, and
work out the frame offset between them.

    python3 pair_check.py take_20260918_181903_cam0.mkv
    python3 pair_check.py left.mkv right.mkv --seconds 120

What it checks, and why each matters for the stitch:

  frame count / duration   The two files must describe the same span. The record
                           pipeline stamps a rigid 30fps grid (videorate), so a
                           capture drop becomes a DUPLICATED frame rather than a
                           shortened file - counts should match exactly.
  PTS regularity           Gaps > 1.5 frame intervals mean the encoder or disk
                           fell behind: real missing time, and everything after
                           the gap is shifted against the other camera.
  duplicate frames         Consecutive frames with identical mean luma = the
                           sensor missed a frame and videorate repeated one.
                           A few is cosmetic; many means the capture is sick.
  offset estimate          Cross-correlates per-frame brightness between the two
                           cameras and reports the integer frame shift with the
                           best match, plus how sharply that peak stands out.

Genlocked vs free-running:
  With XVS wired the sensors expose together, so the offset is a small CONSTANT
  (the two gst pipelines start a few ms apart) and the correlation peak is sharp
  - trust it and pass it to the stitcher. Free-running, the cameras drift
  against each other during the take: a single offset cannot be right for the
  whole file, and a weak/broad peak here is the expected symptom, not a bug.
"""

import argparse
import os
import re
import subprocess
import sys


def run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def probe_pts(path):
    """Presentation timestamps of every video frame, in seconds."""
    r = run(f'ffprobe -v error -select_streams v -show_entries packet=pts_time '
            f'-of csv=p=0 "{path}"')
    ts = sorted(float(x) for x in r.stdout.split() if x.strip())
    return ts


def luma_series(path, seconds):
    """Per-frame mean luma (downscaled, so it is cheap) for the first N seconds."""
    r = run(f'ffmpeg -v error -t {seconds} -i "{path}" '
            f'-vf "scale=160:90,signalstats,metadata=print:'
            f'key=lavfi.signalstats.YAVG:file=-" -f null - 2>&1')
    return [float(m) for m in re.findall(r'YAVG=([\d.]+)', r.stdout)]


def correlate(a, b, max_shift):
    """Best integer shift of b against a, plus how much it beats the runner-up."""
    n = min(len(a), len(b))
    if n < 30:
        return None, 0.0, 0.0
    a, b = a[:n], b[:n]

    def score(shift):
        if shift >= 0:
            x, y = a[shift:], b[:n - shift]
        else:
            x, y = a[:n + shift], b[-shift:]
        m = len(x)
        if m < 20:
            return -2.0
        mx, my = sum(x) / m, sum(y) / m
        dx = [v - mx for v in x]
        dy = [v - my for v in y]
        num = sum(p * q for p, q in zip(dx, dy))
        den = (sum(p * p for p in dx) * sum(q * q for q in dy)) ** 0.5
        return num / den if den else -2.0

    scored = sorted(((score(s), s) for s in range(-max_shift, max_shift + 1)),
                    reverse=True)
    best, shift = scored[0]
    # margin over the best shift that is not adjacent to the winner
    runner = next((v for v, s in scored[1:] if abs(s - shift) > 1), 0.0)
    return shift, best, best - runner


def report_file(tag, path, fps_nominal=30.0):
    ts = probe_pts(path)
    size = os.path.getsize(path) / (1 << 20)
    if len(ts) < 2:
        print(f"  {tag}: UNREADABLE ({path})")
        return None
    span = ts[-1] - ts[0]
    fps = (len(ts) - 1) / span if span else 0
    gaps = [(ts[i], (ts[i + 1] - ts[i]) * 1000) for i in range(len(ts) - 1)]
    interval = 1000.0 / fps_nominal
    big = [(t, g) for t, g in gaps if g > interval * 1.5]
    print(f"  {tag}: {len(ts)} frames, {span:.2f}s, {fps:.3f} fps, {size:.0f} MB")
    if big:
        print(f"       ! {len(big)} timing gap(s) > {interval * 1.5:.0f}ms "
              f"(first at {big[0][0]:.2f}s = {big[0][1]:.0f}ms)")
    return {"frames": len(ts), "span": span, "fps": fps, "gaps": len(big)}


def main():
    ap = argparse.ArgumentParser(description="Verify and pair a Veery take's two files.")
    ap.add_argument("left", help="cam0 file (cam1 is found automatically if named _cam0)")
    ap.add_argument("right", nargs="?", help="cam1 file (optional if auto-detectable)")
    ap.add_argument("--seconds", type=int, default=60,
                    help="how much of the take to analyse for the offset (default 60)")
    ap.add_argument("--max-shift", type=int, default=15,
                    help="largest frame offset to consider (default 15)")
    args = ap.parse_args()

    left = args.left
    right = args.right
    if not right:
        if "_cam0." in left:
            right = left.replace("_cam0.", "_cam1.")
        else:
            sys.exit("give both files, or name them *_cam0.* / *_cam1.*")
    for f in (left, right):
        if not os.path.isfile(f):
            sys.exit(f"missing: {f}")

    print(f"\n== files ==")
    a = report_file("cam0", left)
    b = report_file("cam1", right)
    if not a or not b:
        sys.exit(1)

    print("\n== pairing ==")
    dframes = a["frames"] - b["frames"]
    if dframes:
        print(f"  ! frame counts differ by {dframes} "
              f"- the shorter file ends the stitch; check the longer one for a late start")
    else:
        print("  frame counts match")

    la = luma_series(left, args.seconds)
    lb = luma_series(right, args.seconds)
    dups_a = sum(1 for i in range(1, len(la)) if la[i] == la[i - 1])
    dups_b = sum(1 for i in range(1, len(lb)) if lb[i] == lb[i - 1])
    if dups_a or dups_b:
        print(f"  duplicate frames in first {args.seconds}s: cam0 {dups_a}, cam1 {dups_b} "
              f"(a sensor drop repeated by videorate)")

    shift, corr, margin = correlate(la, lb, args.max_shift)
    if shift is None:
        print("  ! too few frames analysed to estimate an offset")
        sys.exit(1)

    print(f"  best offset: {shift} frames   (correlation {corr:.3f}, "
          f"margin over next candidate {margin:.3f})")
    if corr < 0.5:
        print("    weak match - expected while the cameras FREE-RUN (they drift, so no")
        print("    single offset fits the whole take). With XVS genlock this should be")
        print("    a sharp peak; if it is not, the scene may simply lack brightness")
        print("    variation - re-check on footage with movement or changing light.")
    elif margin < 0.05:
        print("    ambiguous peak - neighbouring offsets score almost as well.")
    else:
        print("    confident.")

    print(f"\n== stitch with ==")
    flag = f" --pair-offset {shift}" if shift else ""
    print(f"  ./build/StitchPipeline --source \"{left}\"{flag} --calib-dir ../calibration\n")


if __name__ == "__main__":
    main()
