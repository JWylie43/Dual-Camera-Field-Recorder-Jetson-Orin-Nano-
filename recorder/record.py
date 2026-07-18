#!/usr/bin/env python3
"""
record.py - Field camera rig recorder (Jetson Orin Nano + Arducam B0577)

Records the combined stereo stream (one 3840x1200 frame = both cameras side by
side) to a hardware-MJPEG MKV on the NVMe, LIVE at 30fps, keeping the CPU nearly
idle for maximum thermal headroom (this rig runs outdoors in the heat).

Why MJPEG / why this pipeline (the hard-won lessons):
  * Orin Nano has NO hardware H.264/H.265 encoder (no NVENC). The two real
    options are software x264 (CPU-heavy) or hardware MJPEG (nvjpegenc, the
    NVJPG engine). We chose MJPEG: the JPEG engine does all the work, so the
    six CPU cores stay ~idle -> the most thermal margin for hot outdoor use.
    Trade-off: MJPEG is intra-only so files are ~6x bigger than x264
    (~40 GB/hr @ quality 85). At ~40 GB/hr a 500 GB drive holds ~12 hours.
  * Capture MUST use `nvv4l2camerasrc`, not `v4l2src`. v4l2src copies every 9 MB
    frame on a single CPU thread and capped the pipeline at ~28 fps no matter
    the encoder. nvv4l2camerasrc captures zero-copy into NVMM (GPU memory); with
    it, MJPEG holds a clean 30 fps and the NVJPG engine sits at only ~15%.
  * Every queue is leaky=downstream so the VIC's small buffer pool can never be
    starved into a deadlock - under pressure it drops a frame instead of stalling.
  * Container is MKV (matroskamux): crash-resilient. The MKV gets its duration +
    seek index only on a clean EOS, which this script sends on stop (SIGINT). A
    file killed with SIGTERM shows Duration: N/A and won't scrub. MJPEG-in-MKV
    needs VLC to play.

Optional live preview (--preview-port N):
  Adds a `tee` that splits the captured frames into two branches:
    record  branch: full-res JPEG -> file        (NVJPG engine)
    preview branch: downscaled JPEG -> tcpserversink on port N   (NVJPG1 engine)
  This lets server.py show a live in-browser preview WHILE recording. The second
  JPEG encode runs on the Orin's second hardware JPEG engine, so it barely costs
  anything and the leaky queues keep the preview branch from ever back-pressuring
  the recording branch. Without the flag, the recorder runs preview-free as
  before.

Pipeline (default, no preview):
  nvv4l2camerasrc -> NVMM,UYVY -> nvvidconv -> I420 -> queue(leaky)
    -> nvjpegenc -> jpegparse -> matroskamux -> filesink

Usage:
    python3 record.py                 # record until Enter / Ctrl+C
    python3 record.py --seconds 30    # fixed 30 seconds
    python3 record.py --seconds 600 --log-thermals   # soak test + thermal log
    python3 record.py --measure --seconds 20         # self-test: sustained fps
    python3 record.py --preview-port 8090            # also serve preview (server.py uses this)
    python3 record.py --dry-run       # print the pipeline, don't run
"""

import argparse
import datetime
import os
import shutil
import signal
import subprocess
import sys

# Multipart boundary for the preview MJPEG stream. MUST match server.py.
PREVIEW_BOUNDARY = "spinframe"


class Config:
    device = "/dev/video0"          # the camera (confirmed via v4l2-ctl --list-devices)
    width = 3840                    # combined frame width  (2 x 1920)
    height = 1200                   # combined frame height (1 x 1200)
    fps = 30                        # frames per second
    pixel_format = "UYVY"           # camera output (UYVY 4:2:2, from the onboard ISP)

    jpeg_quality = 85               # MJPEG quality 0-100 (85 ~ visually lossless)

    thermal_interval_ms = 5000      # tegrastats sampling interval when --log-thermals

    # Optional preview branch (set via --preview-port). Downscaled + lower quality
    # so it's light on the second JPEG engine and on Wi-Fi bandwidth.
    preview_port = None
    preview_width = 1280
    preview_height = 400
    preview_quality = 50

    output_dir = "/mnt/video"       # the NVMe mount point (records land here)
    filename_prefix = "game"        # output files: game_YYYY-MM-DD_HH-MM-SS.mkv

    # Camera controls applied before recording, using names from
    # `v4l2-ctl --list-ctrls`. Edit / extend for your conditions.
    controls = {
        "frame_rate": fps,          # make sure the sensor is at our target rate
        "trigger_mode": 0,          # 0 = free-run (kit handles stereo sync internally)
    }


def _run(cmd, check=True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def check_prerequisites(cfg, measure=False):
    """Fail early with a clear message if something basic is wrong."""
    problems = []

    if shutil.which("gst-launch-1.0") is None:
        problems.append("gst-launch-1.0 not found (GStreamer not installed?).")
    if shutil.which("v4l2-ctl") is None:
        problems.append("v4l2-ctl not found (install with: sudo apt install v4l-utils).")
    if shutil.which("gst-inspect-1.0") and \
            _run(["gst-inspect-1.0", "nvv4l2camerasrc"], check=False).returncode != 0:
        problems.append("nvv4l2camerasrc not found - this is the zero-copy capture "
                        "element the recorder needs to hit 30fps. Check the L4T install.")

    if not os.path.exists(cfg.device):
        problems.append(f"Camera device {cfg.device} does not exist. "
                        f"Check `ls /dev/video*` and the ribbon connection.")

    if not measure:  # in --measure we never touch the disk
        if not os.path.isdir(cfg.output_dir):
            problems.append(f"Output directory {cfg.output_dir} does not exist. "
                            f"Is the NVMe mounted there? See `lsblk`.")
        elif not os.access(cfg.output_dir, os.W_OK):
            problems.append(f"No write permission to {cfg.output_dir}. "
                            f"Try: sudo chown $USER:$USER {cfg.output_dir}")

    return problems


def apply_controls(cfg, dry_run=False):
    """Set camera controls via v4l2-ctl (the reliable way for this driver)."""
    for name, value in cfg.controls.items():
        ctrl = f"{name}={value}"
        cmd = ["v4l2-ctl", f"--device={cfg.device}", f"--set-ctrl={ctrl}"]
        if dry_run:
            print("  would run:", " ".join(cmd))
            continue
        result = _run(cmd, check=False)
        if result.returncode != 0:
            print(f"  warning: could not set {ctrl}: "
                  f"{result.stderr.strip() or 'unknown error'}")
        else:
            print(f"  set {ctrl}")


# --------------------------------------------------------------------------
# Thermal logging - runs tegrastats alongside the recording, same per-line data
# as interactive `sudo tegrastats`. Needs root, so we prepend sudo when not root.
# --------------------------------------------------------------------------
def _tegrastats_cmd(extra):
    cmd = ["tegrastats"] + extra
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        cmd = ["sudo"] + cmd
    return cmd


def start_thermal_log(log_path, interval_ms):
    if shutil.which("tegrastats") is None:
        print("  warning: tegrastats not found; thermal logging disabled.")
        return None
    cmd = _tegrastats_cmd(["--interval", str(interval_ms), "--logfile", log_path])
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:                       # noqa: BLE001
        print(f"  warning: could not start thermal logging: {e}")
        return None


def stop_thermal_log(proc):
    if proc is None:
        return
    _run(_tegrastats_cmd(["--stop"]), check=False)
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:                            # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# Pipeline construction
# --------------------------------------------------------------------------
def _record_branch(cfg, output_path):
    """Full-res JPEG -> MKV file (runs on the primary NVJPG engine)."""
    return [
        "nvvidconv", "!", "video/x-raw,format=I420",
        "!", "queue", "max-size-buffers=8", "leaky=downstream",
        "!", "nvjpegenc", f"quality={cfg.jpeg_quality}",
        "!", "jpegparse", "!", "matroskamux", "!", "filesink", f"location={output_path}",
    ]


def _preview_branch(cfg):
    """Downscaled JPEG -> tcpserversink (server.py relays it to the browser)."""
    return [
        "nvvidconv",
        "!", f"video/x-raw,format=I420,width={cfg.preview_width},height={cfg.preview_height}",
        "!", "nvjpegenc", f"quality={cfg.preview_quality}",
        "!", "multipartmux", f"boundary={PREVIEW_BOUNDARY}",
        "!", "tcpserversink", "host=127.0.0.1", f"port={cfg.preview_port}", "sync=false",
    ]


def build_pipeline(cfg, output_path, measure=False):
    """
    Build the gst-launch argument list.

    measure=True swaps the muxer+filesink for fpsdisplaysink+fakesink (no disk).
    cfg.preview_port set -> tee the stream into record + preview branches.
    """
    nvmm_caps = (f"video/x-raw(memory:NVMM),format={cfg.pixel_format},"
                 f"width={cfg.width},height={cfg.height},framerate={cfg.fps}/1")
    flags = ["-e", "-v"] if measure else ["-e"]
    src = ["nvv4l2camerasrc", f"device={cfg.device}", "!", nvmm_caps]

    if measure:
        chain = src + [
            "!", "nvvidconv", "!", "video/x-raw,format=I420",
            "!", "queue", "max-size-buffers=8", "leaky=downstream",
            "!", "nvjpegenc", f"quality={cfg.jpeg_quality}", "!", "jpegparse",
            "!", "fpsdisplaysink", "video-sink=fakesink", "sync=false", "text-overlay=false",
        ]
    elif cfg.preview_port:
        # tee -> [record branch] + [preview branch]; each branch starts with its
        # own leaky queue so a slow branch can never back-pressure the other.
        chain = src + [
            "!", "tee", "name=t",
            "t.", "!", "queue", "max-size-buffers=8", "leaky=downstream", "!",
        ] + _record_branch(cfg, output_path) + [
            "t.", "!", "queue", "max-size-buffers=4", "leaky=downstream", "!",
        ] + _preview_branch(cfg)
    else:
        chain = src + [
            "!", "nvvidconv", "!", "video/x-raw,format=I420",
            "!", "queue", "max-size-buffers=8", "leaky=downstream",
            "!", "nvjpegenc", f"quality={cfg.jpeg_quality}", "!", "jpegparse",
            "!", "matroskamux", "!", "filesink", f"location={output_path}",
        ]

    return ["gst-launch-1.0"] + flags + chain


def make_output_path(cfg):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(cfg.output_dir, f"{cfg.filename_prefix}_{stamp}.mkv")


def record(cfg, seconds=None, dry_run=False, measure=False, log_thermals=False):
    output_path = None if measure else make_output_path(cfg)

    print(f"Device      : {cfg.device}")
    print(f"Resolution  : {cfg.width}x{cfg.height} @ {cfg.fps}fps ({cfg.pixel_format})")
    print(f"Encoder     : MJPEG (hardware NVJPG), quality={cfg.jpeg_quality}")
    print(f"Storage est : ~40 GB/hr @ q85 (varies; ~12 hr on 500 GB)")
    if cfg.preview_port and not measure:
        print(f"Preview     : tcpserversink :{cfg.preview_port} "
              f"({cfg.preview_width}x{cfg.preview_height} MJPEG, 2nd JPEG engine)")
    if measure:
        print("Mode        : MEASURE (no disk write; benchmarking sustained fps)")
    else:
        print(f"Output      : {output_path}")

    thermal_log_path = None
    if log_thermals:
        if output_path:
            thermal_log_path = os.path.splitext(output_path)[0] + ".tegrastats.log"
        else:
            stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            thermal_log_path = os.path.join(cfg.output_dir, f"thermal_{stamp}.log")
        print(f"Thermal log : {thermal_log_path} (tegrastats every "
              f"{cfg.thermal_interval_ms/1000:.0f}s)")
    print()

    print("Applying camera controls:")
    apply_controls(cfg, dry_run=dry_run)
    print()

    pipeline = build_pipeline(cfg, output_path, measure=measure)

    if dry_run:
        print("Pipeline that would run:")
        print(" ", " ".join(pipeline))
        return

    print("Starting...")
    if measure:
        print("  Watch 'current' (should hold ~30.0) and 'dropped' (should stay 0).")
        print("  'average' starts high then settles to ~30 - startup burst, not drops.")
    if seconds:
        print(f"  (will stop automatically after {seconds} seconds)")
    else:
        print("  Press Enter to stop (or Ctrl+C).")
    print()

    thermal_proc = start_thermal_log(thermal_log_path, cfg.thermal_interval_ms) \
        if log_thermals else None

    # start_new_session: own process group, so SIGINT reaches the whole pipeline
    # for a clean EOS (finalized, playable MKV).
    proc = subprocess.Popen(pipeline, start_new_session=True)

    def stop():
        if proc.poll() is None:
            print("\nStopping (finalizing MKV)...")
            try:
                proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                print("  did not finalize in time; terminating.")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    try:
        if seconds:
            try:
                proc.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                stop()
        else:
            try:
                input()
            except EOFError:
                proc.wait()
            else:
                stop()
    except KeyboardInterrupt:
        stop()
    finally:
        stop_thermal_log(thermal_proc)

    rc = proc.poll()
    print()
    if measure:
        print(f"Measure run finished (GStreamer exit code: {rc}).")
        if thermal_log_path:
            print(f"Thermal log: {thermal_log_path}")
        return None

    if os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / 1_000_000
        print(f"Done. Wrote {output_path} ({size_mb:.1f} MB)")
        if seconds:
            print(f"  Effective rate: ~{size_mb/seconds*3600/1000:.0f} GB/hour")
            print(f"  Verify frame count (expect ~{cfg.fps*seconds}):")
            print(f"    ffprobe -v error -count_frames -select_streams v:0 \\")
            print(f"      -show_entries stream=nb_read_frames -of csv=p=0 '{output_path}'")
        if size_mb < 0.1:
            print("  NOTE: file is suspiciously small - check the camera signal.")
        print("  Play with VLC (default players may not open MJPEG-in-MKV).")
        if thermal_log_path and os.path.exists(thermal_log_path):
            print(f"  Thermal log: {thermal_log_path}")
    else:
        print(f"WARNING: expected output {output_path} was not created. "
              f"GStreamer exit code: {rc}")
    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Record the combined stereo stream to hardware-MJPEG MKV on the NVMe.")
    parser.add_argument("--seconds", type=int, default=None,
                        help="Record a fixed number of seconds, then stop.")
    parser.add_argument("--measure", action="store_true",
                        help="Benchmark sustained fps with NO disk write (self-test).")
    parser.add_argument("--log-thermals", action="store_true",
                        help="Log tegrastats to a file next to the video. Needs root.")
    parser.add_argument("--thermal-interval", type=int, default=Config.thermal_interval_ms,
                        help=f"Thermal sampling interval in ms (default: "
                             f"{Config.thermal_interval_ms} = 5s).")
    parser.add_argument("--preview-port", type=int, default=None,
                        help="Also serve a downscaled MJPEG preview via tcpserversink "
                             "on this port (used by server.py for in-browser preview).")
    parser.add_argument("--device", default=Config.device,
                        help=f"V4L2 device (default: {Config.device})")
    parser.add_argument("--output-dir", default=Config.output_dir,
                        help=f"Where to write files (default: {Config.output_dir})")
    parser.add_argument("--quality", type=int, default=Config.jpeg_quality,
                        help=f"MJPEG quality 0-100 (default: {Config.jpeg_quality})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the controls and pipeline without running.")
    args = parser.parse_args()

    cfg = Config()
    cfg.device = args.device
    cfg.output_dir = args.output_dir
    cfg.jpeg_quality = args.quality
    cfg.thermal_interval_ms = args.thermal_interval
    cfg.preview_port = args.preview_port

    problems = check_prerequisites(cfg, measure=args.measure)
    if problems:
        fatal = problems if not args.dry_run else [p for p in problems if "not found" in p]
        if fatal:
            print("Cannot start:")
            for p in fatal:
                print("  -", p)
            sys.exit(1)
        else:
            print("Warnings (continuing because --dry-run):")
            for p in problems:
                print("  -", p)
            print()

    record(cfg, seconds=args.seconds, dry_run=args.dry_run, measure=args.measure,
           log_thermals=args.log_thermals)


if __name__ == "__main__":
    main()
