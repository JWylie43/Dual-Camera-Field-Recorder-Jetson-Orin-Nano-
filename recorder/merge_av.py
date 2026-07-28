#!/usr/bin/env python3
"""
merge_av.py - RUN ON THE DESKTOP. Mux the recorder's separate video + audio into
one synced file, using the capture-time anchors in the sidecar.

The recorder writes three things per take:
    game_TS.mkv          video (MJPEG)
    game_TS_aN.wav       one or more audio segments (mic present -> a segment)
    game_TS.sync.json    the sidecar: each stream's CLOCK_MONOTONIC capture anchor

Alignment is pure arithmetic on those anchors - no waveform guessing:
    delay(segment) = (segment.anchor_ns - video.anchor_ns) / 1e9   seconds
Each audio segment is delayed onto the video's timeline by its own offset, so a
late mic start, and the silent gaps between hot-plug segments, land exactly right.

Because a mic yanked mid-record can leave a WAV with a stale length header, we
read every WAV with the demuxer's `-ignore_length 1` so ffmpeg decodes to EOF
regardless. Video is stream-copied (MJPEG untouched); mixed audio is written as
PCM (lossless, MKV-friendly).

You can also point --video at a STITCHED panorama instead of the raw capture: the
stitcher keeps the same frame timeline, so the same anchors still align the audio.

Usage:
    python3 merge_av.py game_TS.sync.json
    python3 merge_av.py game_TS.sync.json --video stitched.mp4 --out final.mkv
    python3 merge_av.py game_TS.sync.json --dry-run
"""

import argparse
import json
import os
import shutil
import subprocess
import sys


def _q(p):
    return "'" + str(p).replace("'", "'\\''") + "'"


def main():
    ap = argparse.ArgumentParser(description="Mux recorder video + WAV audio segments into one synced file.")
    ap.add_argument("sidecar", help="the .sync.json written next to the recording")
    ap.add_argument("--video", default=None,
                    help="video file to mux onto (default: the one named in the sidecar; "
                         "point this at a stitched panorama to add sound to it).")
    ap.add_argument("--out", default=None,
                    help="output file (default: <video-stem>.withaudio.mkv).")
    ap.add_argument("--dry-run", action="store_true", help="print the ffmpeg command, don't run.")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None and not args.dry_run:
        sys.exit("ffmpeg not found on PATH (install ffmpeg).")

    with open(args.sidecar) as f:
        sc = json.load(f)

    base_dir = os.path.dirname(os.path.abspath(args.sidecar))
    video_file = args.video or os.path.join(base_dir, sc["video"]["file"])
    if not os.path.exists(video_file):
        sys.exit(f"video file not found: {video_file}")

    v_anchor = sc["video"].get("anchor_ns")
    segments = sc.get("audio_segments", [])
    if not segments:
        sys.exit("No audio segments in the sidecar - nothing to merge (video already has no audio).")
    if v_anchor is None:
        sys.exit("Sidecar has no video anchor (first frame never captured?). Cannot align.")

    out_file = args.out or (os.path.splitext(video_file)[0] + ".withaudio.mkv")

    # Build the ffmpeg command: video first, then each WAV as an input.
    cmd = ["ffmpeg", "-y", "-i", video_file]
    labels = []
    for i, seg in enumerate(segments, start=1):
        wav = os.path.join(base_dir, seg["file"])
        if not os.path.exists(wav):
            print(f"  warning: segment file missing, skipping: {wav}")
            continue
        cmd += ["-f", "wav", "-ignore_length", "1", "-i", wav]
        delay_s = (seg["anchor_ns"] - v_anchor) / 1e9
        label = f"a{i}"
        if delay_s >= 0:
            ms = int(round(delay_s * 1000))
            cmd_filter = f"[{i}:a]adelay={ms}:all=1[{label}]"
        else:
            # Audio started before video: trim its head instead of delaying.
            cmd_filter = (f"[{i}:a]atrim=start={-delay_s:.6f},"
                          f"asetpts=PTS-STARTPTS[{label}]")
        labels.append((label, cmd_filter))

    if not labels:
        sys.exit("No usable audio segment files were found next to the sidecar.")

    filt = ";".join(f for _, f in labels)
    mixed = "[" + "][".join(lbl for lbl, _ in labels) + "]"
    if len(labels) == 1:
        # single segment: adelay/atrim output is already the audio track
        aout = labels[0][0]
        filter_complex = filt
    else:
        aout = "aout"
        filter_complex = f"{filt};{mixed}amix=inputs={len(labels)}:normalize=0[{aout}]"

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "0:v", "-map", f"[{aout}]",
        "-c:v", "copy", "-c:a", "pcm_s16le",
        out_file,
    ]

    printable = " ".join(_q(c) if (" " in str(c) or "[" in str(c)) else str(c) for c in cmd)
    print("ffmpeg command:")
    print(" ", printable)
    if args.dry_run:
        return

    print(f"\nMerging -> {out_file}")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        sys.exit(f"ffmpeg failed (exit {rc}).")
    print(f"Done: {out_file}")


if __name__ == "__main__":
    main()
