#!/usr/bin/env python3
"""virtualcam.py - auto-follow virtual camera for stitched field panoramas.

Takes a stitched pano video (ideally the NATIVE-resolution render, --look-width 0)
and produces a standard 16:9 video that pans across the field following the play.

How it decides where to look: the physical camera is fixed, so anything that moves
IS the game. Pass 1 measures per-frame motion energy (frame differencing at low
resolution), takes its horizontal centroid, and smooths it into a camera path that
moves like a human operator: a dead zone so it doesn't twitch, a velocity limit so
it pans - never snaps - and zero-lag Gaussian smoothing (offline luxury: we know
the future). The window NEVER zooms: it always uses the full pano height with a
16:9 width - zooming in would only trade sharpness for magnification, and zooming
out is impossible (there are no more rows to include).

The path is saved as a CSV sidecar (frame,center_x). To hand-correct a segment,
edit the CSV and re-run with --path <csv>: analysis is skipped and the render uses
your numbers (they pass through the same smoothing unless --no-smooth).

Pass 2 crops the window per frame, does the ONE deliberate upscale to the output
size (default 2560x1440 - YouTube's good VP9/AV1 tier starts at 1440p, and this
keeps the stretch factor sane), sharpens lightly (cas), and encodes via ffmpeg
(h264_videotoolbox on macOS, else libx264). Audio is copied over from the source
when present.

Typical use:
  python3 virtualcam.py game_pano.mp4 --out game_follow.mp4
  python3 virtualcam.py game_pano.mp4 --preview follow_preview.mp4   # fast path check
  python3 virtualcam.py game_pano.mp4 --path game_pano.campath.csv --out fixed.mp4
"""
import argparse
import csv
import os
import platform
import shutil
import subprocess
import sys

import cv2
import numpy as np


def analyze(src, args):
    """Pass 1: per-frame horizontal motion centroid, in pano pixels. Returns
    (raw_centers, pano_w, pano_h, fps, nframes); center is NaN on no-motion frames."""
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        sys.exit(f"cannot open {src}")
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    small_w = 640
    scale = small_w / W
    small_h = max(1, int(H * scale))
    prev = None
    centers = []
    n = 0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        g = cv2.cvtColor(cv2.resize(frame, (small_w, small_h), interpolation=cv2.INTER_AREA),
                         cv2.COLOR_BGR2GRAY)
        g = cv2.GaussianBlur(g, (5, 5), 0)
        if prev is None:
            centers.append(np.nan)
        else:
            d = cv2.absdiff(g, prev)
            _, mask = cv2.threshold(d, args.motion_thresh, 255, cv2.THRESH_BINARY)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            colmass = mask.sum(axis=0).astype(np.float64)
            m = colmass.sum()
            # Too little motion (dead ball, timeout): no opinion this frame - the
            # smoother holds the last known position through the gap.
            if m < args.min_motion * 255 * small_h:
                centers.append(np.nan)
            else:
                cx = (colmass * np.arange(small_w)).sum() / m
                centers.append(cx / scale)
        prev = g
        n += 1
        if n % 900 == 0:
            print(f"  analyze: {n}/{total or '?'} frames")
    cap.release()
    return np.array(centers, dtype=np.float64), W, H, fps, n


def smooth_path(raw, win_w, pano_w, fps, args):
    """Raw motion centers -> camera path (window CENTER x per frame)."""
    x = raw.copy()
    # Fill no-motion gaps by holding the last value (and back-fill the head).
    last = np.nan
    for i in range(len(x)):
        if np.isnan(x[i]):
            x[i] = last
        else:
            last = x[i]
    first = x[np.isfinite(x)]
    x[np.isnan(x)] = first[0] if len(first) else pano_w / 2.0

    # Median filter (spike rejection: a bird, a sideline jogger).
    k = max(1, int(args.median_s * fps) | 1)
    pad = k // 2
    xm = np.copy(x)
    if k > 1:
        xp = np.pad(x, pad, mode="edge")
        xm = np.array([np.median(xp[i:i + k]) for i in range(len(x))])

    # Dead zone + velocity-limited follower: the "camera operator". The window
    # only moves when the action leaves a comfort zone around its center, and
    # never faster than max_vel - this is what makes it watchable.
    dead = args.dead_zone
    vmax = args.max_vel_pps / fps
    out = np.empty_like(xm)
    pos = xm[0]
    for i, target in enumerate(xm):
        err = target - pos
        if abs(err) > dead:
            step = np.clip(err - np.sign(err) * dead, -vmax, vmax)
            pos += step
        out[i] = pos

    # Zero-lag Gaussian polish (offline: uses future frames, so no trailing).
    sigma = max(1.0, args.smooth_s * fps)
    r = int(3 * sigma)
    kern = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    kern /= kern.sum()
    out = np.convolve(np.pad(out, r, mode="edge"), kern, mode="valid")

    return np.clip(out, win_w / 2.0, pano_w - win_w / 2.0)


def encoder_default():
    return "h264_videotoolbox" if platform.system() == "Darwin" else "libx264"


def render(src, path, win_w, win_h, args, fps, pano_w, pano_h):
    ow, oh = args.out_size
    has_audio = False
    if shutil.which("ffprobe"):
        p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                            "-show_entries", "stream=codec_type", "-of", "csv=p=0", src],
                           capture_output=True, text=True)
        has_audio = "audio" in p.stdout
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pixel_format", "bgr24",
           "-video_size", f"{ow}x{oh}", "-framerate", f"{fps}", "-i", "-"]
    if has_audio:
        cmd += ["-ss", f"{args.start / fps}", "-i", src, "-map", "0:v", "-map", "1:a?",
                "-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd += ["-vf", f"cas={args.cas}", "-c:v", args.venc, "-b:v", args.bitrate,
            "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", "bt709", "-movflags", "+faststart", args.out]
    pipe = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    cap = cv2.VideoCapture(src)
    if args.start:
        cap.set(cv2.CAP_PROP_POS_FRAMES, args.start)
    n = args.start
    written = 0
    while n < len(path) and (args.end < 0 or n <= args.end):
        ok, frame = cap.read()
        if not ok:
            break
        cx = path[n]
        x0 = int(round(cx - win_w / 2.0))
        x0 = max(0, min(pano_w - win_w, x0))
        y0 = max(0, (pano_h - win_h) // 2)
        win = frame[y0:y0 + win_h, x0:x0 + win_w]
        out = cv2.resize(win, (ow, oh), interpolation=cv2.INTER_LANCZOS4)
        pipe.stdin.write(out.tobytes())
        n += 1
        written += 1
        if written % 900 == 0:
            print(f"  render: {written} frames")
    cap.release()
    pipe.stdin.close()
    rc = pipe.wait()
    if rc != 0:
        sys.exit(f"ffmpeg exited {rc}")
    print(f"render -> {args.out}  ({written} frames @ {ow}x{oh})")


def render_preview(src, path, win_w, win_h, args, fps, pano_w, pano_h):
    """Fast path-check video: downscaled pano with the window drawn on it."""
    pw = 1280
    s = pw / pano_w
    ph = int(pano_h * s) // 2 * 2
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pixel_format", "bgr24", "-video_size", f"{pw}x{ph}",
           "-framerate", f"{fps}", "-i", "-", "-c:v", encoder_default(),
           "-b:v", "6M", "-pix_fmt", "yuv420p", args.preview]
    pipe = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    cap = cv2.VideoCapture(src)
    if args.start:
        cap.set(cv2.CAP_PROP_POS_FRAMES, args.start)
    n = args.start
    while n < len(path) and (args.end < 0 or n <= args.end):
        ok, frame = cap.read()
        if not ok:
            break
        small = cv2.resize(frame, (pw, ph), interpolation=cv2.INTER_AREA)
        cx = path[n] * s
        x0 = int(cx - win_w * s / 2.0)
        y0 = int(max(0, (pano_h - win_h) // 2) * s)
        cv2.rectangle(small, (x0, y0), (x0 + int(win_w * s), y0 + int(win_h * s)),
                      (0, 0, 255), 2)
        pipe.stdin.write(small.tobytes())
        n += 1
    cap.release()
    pipe.stdin.close()
    pipe.wait()
    print(f"preview -> {args.preview}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="stitched pano video (native res preferred: --look-width 0)")
    ap.add_argument("--out", default="", help="output video (omit to only write the path CSV)")
    ap.add_argument("--preview", default="", help="also write a fast overlay video showing the window on the pano")
    ap.add_argument("--path", default="", help="reuse/edit an existing .campath.csv instead of analyzing")
    ap.add_argument("--no-smooth", action="store_true", help="with --path: use the CSV values verbatim")
    ap.add_argument("--out-size", default="2560x1440", help="output WxH (default 2560x1440 = YouTube 1440p tier)")
    ap.add_argument("--start", type=int, default=0, help="first frame to render (analysis always runs full-length)")
    ap.add_argument("--end", type=int, default=-1, help="last frame to render (-1 = end)")
    ap.add_argument("--venc", default=encoder_default())
    ap.add_argument("--bitrate", default="28M")
    ap.add_argument("--cas", default="0.3", help="output sharpening strength (0 disables)")
    ap.add_argument("--dead-zone", type=float, default=60, help="pano px the action may drift from center before the camera moves")
    ap.add_argument("--max-vel-pps", type=float, default=420, help="max pan speed, pano px/second")
    ap.add_argument("--smooth-s", type=float, default=1.0, help="Gaussian smoothing sigma, seconds")
    ap.add_argument("--median-s", type=float, default=0.5, help="median spike-rejection window, seconds")
    ap.add_argument("--motion-thresh", type=int, default=12, help="frame-diff threshold (0-255)")
    ap.add_argument("--min-motion", type=float, default=0.0004, help="min motion fraction to trust a frame's centroid")
    args = ap.parse_args()
    args.out_size = tuple(int(v) for v in args.out_size.lower().split("x"))

    if args.path:
        with open(args.path) as f:
            rows = [r for r in csv.reader(f) if r and not r[0].startswith("#")]
        raw = np.array([float(r[1]) for r in rows])
        cap = cv2.VideoCapture(args.source)
        W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        cap.release()
    else:
        print("pass 1: analyzing motion...")
        raw, W, H, fps, _ = analyze(args.source, args)

    # Fixed window: full pano height, 16:9. No zoom, ever - see the module docstring.
    win_h = H
    win_w = min(W, int(round(H * 16 / 9)) // 2 * 2)
    print(f"pano {W}x{H} @ {fps:g} fps; window {win_w}x{win_h} -> {args.out_size[0]}x{args.out_size[1]} "
          f"(stretch {args.out_size[0] / win_w:.2f}x)")

    if args.path and args.no_smooth:
        path = np.clip(raw, win_w / 2.0, W - win_w / 2.0)
    else:
        path = smooth_path(raw, win_w, W, fps, args)

    csv_out = os.path.splitext(args.source)[0] + ".campath.csv"
    if not args.path or os.path.abspath(csv_out) != os.path.abspath(args.path):
        with open(csv_out, "w") as f:
            f.write("# frame,center_x (pano px) - edit and re-run with --path to override\n")
            for i, v in enumerate(path):
                f.write(f"{i},{v:.1f}\n")
        print(f"camera path -> {csv_out}")

    if args.preview:
        render_preview(args.source, path, win_w, win_h, args, fps, W, H)
    if args.out:
        render(args.source, path, win_w, win_h, args, fps, W, H)
    elif not args.preview:
        print("(no --out/--preview given: path CSV only)")


if __name__ == "__main__":
    main()
