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
    seek index only on a clean EOS, which capture.py sends on stop (SIGINT). A
    file killed with SIGTERM shows Duration: N/A and won't scrub. MJPEG-in-MKV
    needs VLC to play.

--------------------------------------------------------------------------------
Audio (on by default, best-effort, fault-isolated from video)
--------------------------------------------------------------------------------
The Orin has NO analog audio input, so audio needs a USB Audio Class (UAC) mic -
any class-compliant USB mic / interface shows up as an ALSA card with no driver
work. Audio is recorded as a SEPARATE process writing its own WAV file, NOT muxed
into the video MKV. Why separate:

  * VIDEO MUST SURVIVE AUDIO FAILURE. If the mic is unplugged mid-record, its
    capture process errors and dies - but the video capture is a totally
    separate OS process that shares nothing with it, so video keeps recording,
    guaranteed. (You cannot get that guarantee inside one gst pipeline: a fatal
    source error there tears down the whole pipeline, video included.)
  * HOT-PLUG / RESUME. A supervisor thread watches for the mic. Whenever a mic
    is present and audio isn't currently recording, it starts a NEW audio
    segment (a fresh WAV). Unplug -> that segment ends; replug -> a new segment
    starts. No mic at start -> no audio, but the sidecar is still written.

Sync between the two files is by timestamp, done in post. WAV carries no
timestamps, and MKV stores only relative per-frame offsets, so we record an
absolute CAPTURE-TIME ANCHOR for each stream on the shared CLOCK_MONOTONIC clock
into a SIDECAR json ("<stem>.sync.json"):

    align: shift each audio segment by (segment.anchor_ns - video.anchor_ns)/1e9 s

capture.py reads each stream's first-buffer clock time and reports it to this
supervisor over stdout; THIS process is the sole writer of the sidecar (behind a
lock, written atomically), so the capture processes can never race on it. Merge
the files later on the desktop with merge_av.py (which also re-attaches audio to
a stitched panorama, since the stitcher drops audio).

Pipeline (video, default, no preview):
  nvv4l2camerasrc -> NVMM,UYVY -> nvvidconv -> I420 -> queue(leaky)
    -> nvjpegenc -> jpegparse -> matroskamux -> filesink
Pipeline (audio segment):
  alsasrc -> queue(leaky) -> audioconvert -> audioresample -> S16LE -> wavenc -> filesink

Usage:
    python3 record.py                 # record video + audio (if a mic is present)
    python3 record.py --no-audio      # video only
    python3 record.py --seconds 30    # fixed 30 seconds
    python3 record.py --audio-device plughw:2,0   # force a specific mic
    python3 record.py --seconds 600 --log-thermals   # soak test + thermal log
    python3 record.py --measure --seconds 20         # self-test: sustained fps (no audio)
    python3 record.py --preview-port 8090            # also serve preview (server.py uses this)
    python3 record.py --dry-run       # print the pipelines, don't run
"""

import argparse
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading

# Multipart boundary for the preview MJPEG stream. MUST match server.py.
PREVIEW_BOUNDARY = "spinframe"

CAPTURE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture.py")


class Config:
    device = "/dev/video0"          # the camera (confirmed via v4l2-ctl --list-devices)
    width = 3840                    # combined frame width  (2 x 1920)
    height = 1200                   # combined frame height (1 x 1200)
    fps = 30                        # frames per second
    pixel_format = "UYVY"           # camera output (UYVY 4:2:2, from the onboard ISP)

    jpeg_quality = 95               # MJPEG quality 0-100 (95 = transparent; 100 ~2x the
                                    # bytes for no visible gain, 85 shows blocking in grass)

    thermal_interval_ms = 5000      # tegrastats sampling interval when --log-thermals

    # Optional preview branch (set via --preview-port). Downscaled + lower quality
    # so it's light on the second JPEG engine and on Wi-Fi bandwidth.
    preview_port = None
    preview_width = 1280
    preview_height = 400
    preview_quality = 50

    # Audio (on by default; --no-audio disables). Mic must be a USB Audio Class
    # device (the Orin has no analog input). audio_device None -> auto-detect the
    # first USB capture card from `arecord -l`; override with --audio-device.
    # Recorded as WAV/PCM: no codec (zero encode CPU), and no trailer to finalize,
    # so an abrupt unplug at worst leaves a stale length field the merge repairs.
    audio_enabled = True
    audio_device = None             # e.g. "plughw:2,0"; None = auto-detect each segment
    audio_rate = 48000              # 48 kHz
    audio_channels = 1              # mono is plenty for a field mic
    audio_sample_format = "S16LE"   # 16-bit PCM (~5.5 MB/min mono; tiny vs 40 GB/hr video)
    audio_poll_seconds = 2.0        # how often the supervisor re-checks for the mic
    audio_settle_seconds = 1.2      # wait after a mic appears before opening it (USB/ALSA
                                    # lists the card a moment before the PCM is openable)

    output_dir = "/mnt/video"       # the NVMe mount point (records land here)
    filename_prefix = "game"        # output files: game_YYYY-MM-DD_HH-MM-SS.mkv

    # Camera controls applied before recording, using names from
    # `v4l2-ctl --list-ctrls`. Edit / extend for your conditions.
    controls = {
        "frame_rate": fps,          # make sure the sensor is at our target rate
        "trigger_mode": 0,          # 0 = free-run (kit handles stereo sync internally)
        # Exposure recipe (2026-09 outdoor bracket tests): auto-exposure ON, but
        # with a LOW backlight-compensation bias - the driver default of 6 makes
        # AE expose for the shadows and clip sunlit grass to pure white, which
        # no post pass can recover. blc=2 biases darker; a too-dark frame is
        # recoverable in the grade, a clipped one is gone. Gain pinned to 0
        # (cleanest); AE lengthens the shutter for clouds/evening on its own.
        # NOTE this sensor's oddball mapping: `exposure` is the 0/1 AE toggle,
        # and `brightness` is the shutter time (~us, 0-33000) used when AE is
        # off. UI --set-ctrl overrides still win over all of these.
        "exposure": 1,              # auto-exposure ON
        "backlight_compensation": 2,
        "gain": 0,
    }


def _run(cmd, check=True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _gi_gst_ok():
    """Can we import the GStreamer Python bindings? capture.py needs them."""
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Prerequisite checks
# --------------------------------------------------------------------------
def check_prerequisites(cfg, measure=False):
    """Fail early with a clear message if something basic is wrong.

    Audio problems are NEVER fatal here - audio is best-effort. Missing camera /
    GStreamer / disk is fatal; a missing mic just means video-only.
    """
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

    if not measure:
        # The real recorder drives the pipeline from capture.py (python-gi), so
        # both are hard requirements (--measure still uses plain gst-launch).
        if not os.path.exists(CAPTURE_SCRIPT):
            problems.append(f"capture.py not found next to record.py ({CAPTURE_SCRIPT}).")
        if not _gi_gst_ok():
            problems.append("python3-gi (GStreamer Python bindings) not found - the "
                            "recorder needs it. Install: sudo apt install python3-gi "
                            "gir1.2-gstreamer-1.0.")

    if not measure:  # in --measure we never touch the disk
        if not os.path.isdir(cfg.output_dir):
            problems.append(f"Output directory {cfg.output_dir} does not exist. "
                            f"Is the NVMe mounted there? See `lsblk`.")
        elif not os.access(cfg.output_dir, os.W_OK):
            problems.append(f"No write permission to {cfg.output_dir}. "
                            f"Try: sudo chown $USER:$USER {cfg.output_dir}")

    return problems


def audio_warnings(cfg):
    """Non-fatal audio-only checks (gi + capture.py are already fatal-checked)."""
    warns = []
    if shutil.which("gst-inspect-1.0") and \
            _run(["gst-inspect-1.0", "wavenc"], check=False).returncode != 0:
        warns.append("wavenc not found; audio disabled. "
                     "Install with: sudo apt install gstreamer1.0-plugins-good.")
    return warns


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
# Audio device detection - find the first USB capture card via `arecord -l`.
# Output lines look like:  card 2: Device [USB Audio Device], device 0: ...
# We return an ALSA "plughw:<card>,<device>" string. plughw (vs raw hw) lets
# ALSA convert the card's native rate/format to what the pipeline asks for, so
# any class-compliant mic just works regardless of its native sample rate. We
# re-detect for every segment because a mic often re-enumerates to a new card
# number when it's replugged (hw:2 -> hw:3).
# --------------------------------------------------------------------------
def detect_audio_device():
    """Return plughw:<card>,<device> for the first USB capture card, else None.

    USB-only ON PURPOSE. The Orin's only real mic input is a USB Audio Class
    device. It ALSO exposes the Tegra 'APE' XBAR/ADMAIF routing fabric as capture
    cards - those are NOT microphones: opening one "succeeds" but yields no audio
    and never errors, which would wedge the hot-plug supervisor. So we match only
    lines that mention USB and otherwise report "no mic" (use --audio-device to
    force a non-USB device if you ever need to).
    """
    if shutil.which("arecord") is None:
        return None
    result = _run(["arecord", "-l"], check=False)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if "usb" not in line.lower():
            continue
        m = re.match(r"\s*card (\d+):.*?device (\d+):", line)
        if m:
            return f"plughw:{m.group(1)},{m.group(2)}"
    return None


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
#   - _video_chain / video_pipeline_desc: the real recording (run via capture.py)
#   - build_pipeline: the --measure self-test only (plain gst-launch, no disk)
#   - audio_pipeline_desc: one audio segment (run via capture.py)
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


def _video_chain(cfg, output_path):
    """The real-recording video element chain (no preview, or tee'd preview).

    The source is named 'vsrc' so capture.py can attach the first-frame anchor
    probe. Everything downstream is byte-for-byte the proven 30fps pipeline.
    """
    nvmm_caps = (f"video/x-raw(memory:NVMM),format={cfg.pixel_format},"
                 f"width={cfg.width},height={cfg.height},framerate={cfg.fps}/1")
    src = ["nvv4l2camerasrc", f"device={cfg.device}", "name=vsrc", "!", nvmm_caps]
    if cfg.preview_port:
        return src + [
            "!", "tee", "name=t",
            "t.", "!", "queue", "max-size-buffers=8", "leaky=downstream", "!",
        ] + _record_branch(cfg, output_path) + [
            "t.", "!", "queue", "max-size-buffers=4", "leaky=downstream", "!",
        ] + _preview_branch(cfg)
    return src + [
        "!", "nvvidconv", "!", "video/x-raw,format=I420",
        "!", "queue", "max-size-buffers=8", "leaky=downstream",
        "!", "nvjpegenc", f"quality={cfg.jpeg_quality}", "!", "jpegparse",
        "!", "matroskamux", "!", "filesink", f"location={output_path}",
    ]


def video_pipeline_desc(cfg, output_path):
    """gst-launch-style description string for capture.py."""
    return " ".join(_video_chain(cfg, output_path))


def audio_pipeline_desc(cfg, device, wav_path):
    """One audio segment: USB mic -> S16LE PCM -> WAV file (run via capture.py).

    Source named 'asrc' for capture.py's anchor probe. A leaky queue keeps a mic
    hiccup from backing up; audioconvert+audioresample turn any UAC mic's native
    format into the 48 kHz mono S16LE we store.
    """
    caps = (f"audio/x-raw,rate={cfg.audio_rate},channels={cfg.audio_channels},"
            f"format={cfg.audio_sample_format}")
    return " ".join([
        "alsasrc", f"device={device}", "name=asrc", "do-timestamp=true",
        "!", "queue", "max-size-buffers=200", "leaky=downstream",
        "!", "audioconvert", "!", "audioresample",
        "!", caps,
        "!", "wavenc",
        "!", "filesink", f"location={wav_path}",
    ])


def build_pipeline(cfg, output_path, measure=False):
    """gst-launch argument list. Used only for --measure (and --dry-run display)."""
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
    else:
        chain = _video_chain(cfg, output_path)
    return ["gst-launch-1.0"] + flags + chain


def make_output_path(cfg):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(cfg.output_dir, f"{cfg.filename_prefix}_{stamp}.mkv")


# --------------------------------------------------------------------------
# Sidecar - THE single source of truth for A/V alignment, written ONLY here.
# Every mutation takes the lock and writes atomically (temp file + os.replace),
# so a reader can never see a half-written file and the capture processes (which
# never open it) can't race on it.
# --------------------------------------------------------------------------
class Sidecar:
    def __init__(self, path, video_file, cfg):
        self.path = path
        self._lock = threading.Lock()
        self.data = {
            "version": 1,
            "clock": "CLOCK_MONOTONIC (gst SystemClock), nanoseconds",
            "created_utc": _utc_now_iso(),
            "align": "shift each audio segment by (segment.anchor_ns - video.anchor_ns)/1e9 seconds",
            "video": {
                "file": os.path.basename(video_file),
                "anchor_ns": None,        # filled in when the first frame is captured
                "anchor_utc": None,
            },
            "audio": {
                "rate": cfg.audio_rate,
                "channels": cfg.audio_channels,
                "sample_format": cfg.audio_sample_format,
            },
            "audio_segments": [],         # one entry per time the mic was recording
        }
        self._flush()

    def _flush(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)        # atomic on POSIX

    def set_video_anchor(self, anchor_ns, anchor_utc):
        with self._lock:
            self.data["video"]["anchor_ns"] = anchor_ns
            self.data["video"]["anchor_utc"] = anchor_utc
            self._flush()

    def add_audio_segment(self, wav_file, anchor_ns, anchor_utc):
        with self._lock:
            self.data["audio_segments"].append({
                "file": os.path.basename(wav_file),
                "anchor_ns": anchor_ns,
                "anchor_utc": anchor_utc,
            })
            self._flush()

    def finalize(self):
        with self._lock:
            self.data["ended_utc"] = _utc_now_iso()
            self._flush()


# --------------------------------------------------------------------------
# Capture process management
# --------------------------------------------------------------------------
def spawn_capture(desc, probe_name, on_anchor):
    """Launch capture.py for one stream; call on_anchor(ns, utc) on its first buffer.

    start_new_session so a SIGINT to record.py doesn't hit the capture directly -
    the supervisor decides when each capture stops (and how, cleanly).
    """
    proc = subprocess.Popen(
        ["python3", CAPTURE_SCRIPT, "--pipeline", desc, "--probe", probe_name],
        stdout=subprocess.PIPE, stderr=None, text=True, start_new_session=True)

    def reader():
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("ANCHOR"):
                parts = line.split(maxsplit=2)
                if len(parts) >= 2:
                    try:
                        ns = int(parts[1])
                    except ValueError:
                        continue
                    utc = parts[2] if len(parts) > 2 else ""
                    on_anchor(ns, utc)

    threading.Thread(target=reader, daemon=True).start()
    return proc


def _remove_if_empty(path):
    """Delete a segment WAV that never captured audio (a failed open attempt).

    'Never anchored' means no first buffer ever flowed, so the file is just an
    empty/header-only WAV - clutter we don't want next to the real segments.
    """
    try:
        if path and os.path.exists(path) and os.path.getsize(path) < 1024:
            os.remove(path)
    except OSError:
        pass


def _sigint_and_wait(proc, timeout):
    """SIGINT a capture process (-> clean EOS in capture.py) and reap it."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def audio_supervisor(cfg, sidecar, base_stem, stop_event):
    """Hot-plug loop: keep an audio segment recording whenever a mic is present.

    A new WAV per segment (never reopen an old one - resume-into-the-same-WAV
    would corrupt the implicit sample-index timing). Each segment reports its own
    capture anchor, so each lines up to the video independently with real silent
    gaps where the mic was gone. This thread owns the audio process lifecycle,
    including the final clean stop.
    """
    seg_index = 0
    audio_proc = None
    seg = None                                        # {"wav":..., "anchored":bool} for the live segment

    def cleanup_failed(s):
        # A segment that never anchored captured no audio (failed/empty open) -
        # drop its empty WAV so only real segments sit next to the video.
        if s is not None and not s["anchored"]:
            _remove_if_empty(s["wav"])

    try:
        while not stop_event.is_set():
            if audio_proc is None or audio_proc.poll() is not None:
                cleanup_failed(seg)                   # the attempt that just ended
                audio_proc = None
                seg = None
                # Forced device stays fixed; otherwise re-detect (a replugged mic
                # can land on a new card number).
                device = cfg.audio_device if _device_was_forced else detect_audio_device()
                if device:
                    # A just-plugged USB mic is listed before its PCM is openable;
                    # let it settle so we don't burn a failed open attempt.
                    stop_event.wait(cfg.audio_settle_seconds)
                    if stop_event.is_set():
                        break
                    device = cfg.audio_device if _device_was_forced else detect_audio_device()
                if device:
                    seg_index += 1
                    wav_path = f"{base_stem}_a{seg_index}.wav"
                    seg = {"wav": wav_path, "anchored": False}
                    print(f"  audio: mic {device} -> segment {seg_index} "
                          f"({os.path.basename(wav_path)})")

                    def on_anchor(ns, utc, wp=wav_path, s=seg):
                        s["anchored"] = True
                        sidecar.add_audio_segment(wp, ns, utc)

                    audio_proc = spawn_capture(
                        audio_pipeline_desc(cfg, device, wav_path), "asrc", on_anchor)
            stop_event.wait(cfg.audio_poll_seconds)
    finally:
        if audio_proc is not None:
            _sigint_and_wait(audio_proc, 15)
        cleanup_failed(seg)


# Set once in main(): True if the user passed --audio-device (so we keep it fixed
# instead of re-detecting each segment).
_device_was_forced = False


def _print_header(cfg, output_path, sidecar_path, audio_on, measure):
    print(f"Device      : {cfg.device}")
    print(f"Resolution  : {cfg.width}x{cfg.height} @ {cfg.fps}fps ({cfg.pixel_format})")
    print(f"Encoder     : MJPEG (hardware NVJPG), quality={cfg.jpeg_quality}")
    print(f"Storage est : ~40 GB/hr @ q85 (varies; ~12 hr on 500 GB)")
    if measure:
        print("Mode        : MEASURE (no disk write; benchmarking sustained fps)")
        return
    if audio_on:
        forced = cfg.audio_device if _device_was_forced else "auto-detect USB mic"
        print(f"Audio       : {cfg.audio_channels}ch {cfg.audio_sample_format} @ "
              f"{cfg.audio_rate} Hz WAV, separate process ({forced}); hot-plug on")
    else:
        print("Audio       : disabled (video only)")
    if cfg.preview_port:
        print(f"Preview     : tcpserversink :{cfg.preview_port} "
              f"({cfg.preview_width}x{cfg.preview_height} MJPEG, 2nd JPEG engine)")
    print(f"Output      : {output_path}")
    print(f"Sidecar     : {sidecar_path}")


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------
def _record_measure(cfg, seconds, log_thermals):
    """The --measure self-test: unchanged plain gst-launch path, no disk, no audio."""
    _print_header(cfg, None, None, audio_on=False, measure=True)
    thermal_log_path = None
    if log_thermals:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        thermal_log_path = os.path.join(cfg.output_dir, f"thermal_{stamp}.log")
        print(f"Thermal log : {thermal_log_path}")
    print("\nApplying camera controls:")
    apply_controls(cfg)
    print("\nStarting...")
    print("  Watch 'current' (should hold ~30.0) and 'dropped' (should stay 0).")
    if seconds:
        print(f"  (will stop automatically after {seconds} seconds)")
    else:
        print("  Press Enter to stop (or Ctrl+C).")
    print()

    thermal_proc = start_thermal_log(thermal_log_path, cfg.thermal_interval_ms) \
        if log_thermals else None
    proc = subprocess.Popen(build_pipeline(cfg, None, measure=True), start_new_session=True)

    def stop():
        _sigint_and_wait(proc, 15)

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

    print(f"\nMeasure run finished (GStreamer exit code: {proc.poll()}).")
    if thermal_log_path:
        print(f"Thermal log: {thermal_log_path}")


def record(cfg, seconds=None, dry_run=False, measure=False, log_thermals=False):
    if measure:
        if dry_run:
            print("Pipeline that would run (measure):")
            print(" ", " ".join(build_pipeline(cfg, None, measure=True)))
            return None
        return _record_measure(cfg, seconds, log_thermals)

    output_path = make_output_path(cfg)
    base_stem = os.path.splitext(output_path)[0]
    sidecar_path = base_stem + ".sync.json"

    # Decide audio once, up front (best-effort: warn and disable, never fail).
    audio_on = cfg.audio_enabled
    if audio_on:
        for w in audio_warnings(cfg):
            print(f"  audio warning: {w}")
            audio_on = False
    if audio_on and not (_device_was_forced or detect_audio_device()):
        print("  audio: no USB mic detected at start; will start recording audio "
              "automatically if one is plugged in.")

    _print_header(cfg, output_path, sidecar_path, audio_on, measure=False)

    thermal_log_path = None
    if log_thermals:
        thermal_log_path = base_stem + ".tegrastats.log"
        print(f"Thermal log : {thermal_log_path} (tegrastats every "
              f"{cfg.thermal_interval_ms / 1000:.0f}s)")
    print()

    print("Applying camera controls:")
    apply_controls(cfg, dry_run=dry_run)
    print()

    if dry_run:
        print("Video pipeline that would run (via capture.py):")
        print(" ", video_pipeline_desc(cfg, output_path))
        if audio_on:
            print("\nAudio segment pipeline that would run (via capture.py):")
            demo_dev = cfg.audio_device or "plughw:<auto-detected USB mic>"
            print(" ", audio_pipeline_desc(cfg, demo_dev, base_stem + "_a1.wav"))
        print(f"\nSidecar that would be written: {sidecar_path}")
        return None

    print("Starting...")
    if seconds:
        print(f"  (will stop automatically after {seconds} seconds)")
    else:
        print("  Press Enter to stop (or Ctrl+C).")
    print()

    thermal_proc = start_thermal_log(thermal_log_path, cfg.thermal_interval_ms) \
        if log_thermals else None

    sidecar = Sidecar(sidecar_path, output_path, cfg)

    video_proc = spawn_capture(
        video_pipeline_desc(cfg, output_path), "vsrc", sidecar.set_video_anchor)

    stop_event = threading.Event()
    audio_thread = None
    if audio_on:
        audio_thread = threading.Thread(
            target=audio_supervisor, args=(cfg, sidecar, base_stem, stop_event), daemon=True)
        audio_thread.start()

    stopped = {"done": False}

    def stop():
        if stopped["done"]:
            return
        stopped["done"] = True
        print("\nStopping (finalizing files)...")
        stop_event.set()                          # tell the audio supervisor to wind down
        _sigint_and_wait(video_proc, 15)          # clean EOS -> finalized MKV
        if audio_thread is not None:
            audio_thread.join(timeout=25)         # supervisor SIGINTs its own audio proc
        sidecar.finalize()

    try:
        if seconds:
            try:
                video_proc.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                pass
            stop()
        else:
            try:
                input()
            except EOFError:
                # No TTY (e.g. launched by server.py): block until SIGINT arrives.
                try:
                    video_proc.wait()
                except KeyboardInterrupt:
                    pass
            stop()
    except KeyboardInterrupt:
        stop()
    finally:
        stop_thermal_log(thermal_proc)

    _report(cfg, output_path, sidecar_path, seconds, thermal_log_path)
    return output_path


def _report(cfg, output_path, sidecar_path, seconds, thermal_log_path):
    print()
    if os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / 1_000_000
        print(f"Done. Wrote {output_path} ({size_mb:.1f} MB)")
        if seconds:
            print(f"  Effective rate: ~{size_mb / seconds * 3600 / 1000:.0f} GB/hour")
        if size_mb < 0.1:
            print("  NOTE: file is suspiciously small - check the camera signal.")
        print("  Play with VLC (default players may not open MJPEG-in-MKV).")
    else:
        print(f"WARNING: expected output {output_path} was not created.")

    # Report the audio segments recorded (read back from the sidecar we wrote).
    try:
        with open(sidecar_path) as f:
            data = json.load(f)
        segs = data.get("audio_segments", [])
        print(f"  Sidecar: {sidecar_path}")
        if segs:
            print(f"  Audio  : {len(segs)} segment(s):")
            for s in segs:
                p = os.path.join(cfg.output_dir, s["file"])
                mb = os.path.getsize(p) / 1_000_000 if os.path.exists(p) else 0.0
                print(f"           {s['file']} ({mb:.1f} MB)")
            print("  Merge audio + video later on the desktop:")
            print(f"    python3 merge_av.py '{sidecar_path}'")
        else:
            print("  Audio  : none recorded (no mic seen during this take).")
    except Exception:                             # noqa: BLE001
        pass

    if thermal_log_path and os.path.exists(thermal_log_path):
        print(f"  Thermal log: {thermal_log_path}")


def main():
    global _device_was_forced
    parser = argparse.ArgumentParser(
        description="Record the combined stereo stream to hardware-MJPEG MKV on the NVMe, "
                    "with optional fault-isolated USB-mic audio + a sync sidecar.")
    parser.add_argument("--seconds", type=int, default=None,
                        help="Record a fixed number of seconds, then stop.")
    parser.add_argument("--measure", action="store_true",
                        help="Benchmark sustained fps with NO disk write (self-test, no audio).")
    parser.add_argument("--log-thermals", action="store_true",
                        help="Log tegrastats to a file next to the video. Needs root.")
    parser.add_argument("--thermal-interval", type=int, default=Config.thermal_interval_ms,
                        help=f"Thermal sampling interval in ms (default: "
                             f"{Config.thermal_interval_ms} = 5s).")
    parser.add_argument("--preview-port", type=int, default=None,
                        help="Also serve a downscaled MJPEG preview via tcpserversink "
                             "on this port (used by server.py for in-browser preview).")
    parser.add_argument("--no-audio", action="store_true",
                        help="Disable audio (record video only). Audio is ON by default.")
    parser.add_argument("--audio-device", default=None,
                        help="Force a specific ALSA capture device, e.g. plughw:2,0 "
                             "(default: auto-detect the first USB mic, re-detected per segment).")
    parser.add_argument("--device", default=Config.device,
                        help=f"V4L2 device (default: {Config.device})")
    parser.add_argument("--output-dir", default=Config.output_dir,
                        help=f"Where to write files (default: {Config.output_dir})")
    parser.add_argument("--quality", type=int, default=Config.jpeg_quality,
                        help=f"MJPEG quality 0-100 (default: {Config.jpeg_quality})")
    parser.add_argument("--set-ctrl", action="append", default=None, metavar="NAME=VALUE",
                        help="Extra V4L2 control to apply before recording (repeatable), "
                             "e.g. --set-ctrl exposure=800 --set-ctrl gain=20. Used by "
                             "server.py to carry a live exposure/gain tune into the take.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the controls and pipelines without running.")
    args = parser.parse_args()

    cfg = Config()
    cfg.device = args.device
    cfg.output_dir = args.output_dir
    cfg.jpeg_quality = args.quality
    cfg.thermal_interval_ms = args.thermal_interval
    cfg.preview_port = args.preview_port

    # Merge any --set-ctrl overrides on top of the built-in controls. Use an
    # instance-level copy so we never mutate the shared Config class dict.
    cfg.controls = dict(Config.controls)
    for item in (args.set_ctrl or []):
        if "=" not in item:
            print(f"  warning: ignoring malformed --set-ctrl '{item}' (need NAME=VALUE)")
            continue
        key, val = item.split("=", 1)
        key, val = key.strip(), val.strip()
        try:
            cfg.controls[key] = int(val)
        except ValueError:
            cfg.controls[key] = val

    # Audio: on by default; never in --measure (no file is written).
    cfg.audio_enabled = (not args.no_audio) and (not args.measure)
    cfg.audio_device = args.audio_device
    _device_was_forced = args.audio_device is not None

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
