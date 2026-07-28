#!/usr/bin/env python3
"""
server.py - Web control panel for record.py (Jetson Orin Nano camera rig)

Mobile-friendly LAN page: live camera preview, Start/Stop, status, an audio
toggle, a live thermal readout, and a "Manage Files" page. Drives record.py.
Thermals are always logged to a file alongside each recording (run under sudo).

  START -> stop idle preview, launch `record.py --preview-port <P> --log-thermals ...`
  STOP  -> SIGINT the recorder (clean EOS), then restart idle preview

Manage Files (/files) - only when idle (greyed out while recording): list the
recordings on /mnt/video, mount/unmount the external SSD, transfer selected
files to it (background rsync with progress + byte-size verification), and
delete originals. The /snapshot route is retained (no button) for calibration.

PREVIEW (on-demand when idle; always-on while recording):
  The camera can only be opened by one process, and the preview pipeline draws
  power (camera + downscale + JPEG engine) even with nobody watching. So when
  idle we run it ON DEMAND: the browser's /preview.mjpg connection is the signal.
  We reference-count those connections (_viewers) and only run the lightweight
  idle pipeline (camera -> downscale -> JPEG -> tcpserversink on PREVIEW_TCP)
  while at least one viewer is connected. Close the tab or untick "Show preview"
  and the last connection drops -> the camera is released and idles.
  When recording, record.py's own `tee` serves the preview on the SAME port, so
  we leave the idle pipeline off and never touch the camera. Either way the
  browser reads /preview.mjpg, which relays the TCP MJPEG stream as
  multipart/x-mixed-replace into an <img> tag. The source behind the port swaps
  transparently; the page just reconnects across the brief gap.

Live thermals are read from /sys/class/thermal (no root). The "log thermals"
toggle passes --log-thermals to record.py (tegrastats, needs root) -> run this
server under sudo for that toggle.

Run:
    sudo apt install -y python3-flask
    sudo python3 server.py        # sudo enables the log-thermals toggle
    # browse from your phone to http://<orin-ip>:8080   (hostname -I)
"""

import glob
import os
import re
import signal
import socket
import subprocess
import threading
import time

from flask import Flask, Response, jsonify, request

RECORD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "record.py")
DEVICE = "/dev/video0"
OUTPUT_DIR = "/mnt/video"
PORT = 8080

PREVIEW_TCP = 8090            # local port the gst preview pipeline serves on
PREVIEW_BOUNDARY = "spinframe"  # MUST match record.py
PREVIEW_W, PREVIEW_H = 1280, 400
SETTLE = 0.6                  # seconds to let the camera/port free during a swap
CALIB_DIR = os.path.join(OUTPUT_DIR, "calib")  # calibration snapshots land here

# External SSD for field offload (keep UUID in sync with field-offload/).
SSD_UUID = "5E64-018F"        # this drive's exFAT UUID (lsblk -f)
USB_MNT = "/mnt/usb"          # where we mount it
USB_SUBDIR = "orin-video"     # folder recordings are copied into on the SSD

app = Flask(__name__)

_lock = threading.Lock()
_state = {"proc": None, "started": None, "preview": None, "suspended": False,
          "rec_preview": True}   # whether the active recording built a preview tee

# Background file-transfer job (rsync /mnt/video -> SSD). One at a time.
_xfer = {"active": False, "percent": 0, "line": "", "done": False,
         "ok": None, "error": None, "names": []}
_xfer_lock = threading.Lock()


def _is_running():
    p = _state["proc"]
    return p is not None and p.poll() is None


def _newest_recording():
    files = glob.glob(os.path.join(OUTPUT_DIR, "*.mkv"))
    return max(files, key=os.path.getmtime) if files else None


# ---- idle preview pipeline (camera -> downscale -> JPEG -> tcp) -----------
def _idle_preview_cmd():
    return [
        "gst-launch-1.0",
        "nvv4l2camerasrc", f"device={DEVICE}",
        "!", "video/x-raw(memory:NVMM),format=UYVY,width=3840,height=1200,framerate=30/1",
        "!", "nvvidconv",
        "!", f"video/x-raw,format=I420,width={PREVIEW_W},height={PREVIEW_H}",
        "!", "nvjpegenc", "quality=50",
        "!", "multipartmux", f"boundary={PREVIEW_BOUNDARY}",
        "!", "tcpserversink", "host=127.0.0.1", f"port={PREVIEW_TCP}", "sync=false",
    ]


def _start_idle_preview():
    p = _state.get("preview")
    if p is not None and p.poll() is None:
        return
    try:
        _state["preview"] = subprocess.Popen(
            _idle_preview_cmd(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:                            # noqa: BLE001
        _state["preview"] = None


def _stop_idle_preview():
    p = _state.get("preview")
    if p is not None and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    _state["preview"] = None


# ---- on-demand preview lifecycle -----------------------------------------
# _viewers counts live /preview.mjpg connections. The idle preview should run
# iff someone is watching AND we're neither recording nor mid-transition
# (record.py owns the camera then). _sync_preview reconciles the pipeline to
# that desired state; _pv_lock serializes it so a start and stop can't race.
_viewers = 0
_pv_lock = threading.Lock()


def _preview_wanted():
    """True iff the idle pipeline should be running right now. Call under _lock."""
    return _viewers > 0 and not _is_running() and not _state.get("suspended")


def _sync_preview():
    """Bring the idle preview pipeline in line with current demand."""
    with _pv_lock:
        with _lock:
            want = _preview_wanted()
        if want:
            _start_idle_preview()
        else:
            _stop_idle_preview()


def _viewer_delta(d):
    """Adjust the live-viewer count, then reconcile the pipeline. Never hold
    _lock across this (it takes _pv_lock -> _lock internally)."""
    global _viewers
    with _lock:
        _viewers = max(0, _viewers + d)
    _sync_preview()


# ---- thermals via sysfs (no root needed) ---------------------------------
_prev_cpu = {"total": 0, "idle": 0}


def _cpu_percent():
    try:
        with open("/proc/stat") as f:
            nums = [int(x) for x in f.readline().split()[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        dt, di = total - _prev_cpu["total"], idle - _prev_cpu["idle"]
        _prev_cpu["total"], _prev_cpu["idle"] = total, idle
        return round(100.0 * (dt - di) / dt, 1) if dt > 0 else None
    except Exception:                            # noqa: BLE001
        return None


def _read_sysfs(path):
    """Binary read avoids a text-decoder quirk on some L4T thermal nodes."""
    with open(path, "rb") as f:
        return f.read().decode("ascii", "replace").strip()


def _read_thermals():
    zones = []
    for tz in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        try:
            name = _read_sysfs(os.path.join(tz, "type"))
            milli = int(_read_sysfs(os.path.join(tz, "temp")))
            zones.append({"name": name, "temp_c": round(milli / 1000.0, 1)})
        except Exception:                        # noqa: BLE001 - skip bad zone
            continue
    return zones


# ---- routes --------------------------------------------------------------
@app.route("/")
def index():
    return PAGE


@app.route("/status")
def status():
    running = _is_running()
    elapsed = int(time.time() - _state["started"]) if running and _state["started"] else 0
    # A preview is available when idle (on demand) or when the active recording
    # was started with its preview tee. False -> recording without live preview.
    preview = _state.get("rec_preview", True) if running else True
    return jsonify(running=running,
                   file=_newest_recording() if running else None,
                   elapsed=elapsed,
                   preview=preview,
                   server_root=(getattr(os, "geteuid", lambda: 1)() == 0))


@app.route("/thermals")
def thermals():
    try:
        zones = _read_thermals()
        max_c = max((z["temp_c"] for z in zones), default=None)
        return jsonify(zones=zones, max_c=max_c, cpu=_cpu_percent())
    except Exception as e:                       # noqa: BLE001 - never 500 the poller
        return jsonify(zones=[], max_c=None, cpu=None, error=repr(e))


@app.route("/preview.mjpg")
def preview():
    """Relay the gst tcpserversink MJPEG stream to the browser as multipart.

    Opening this connection is what turns the idle preview ON (via _viewer_delta);
    closing it (tab closed, 'Show preview' unticked) turns it back OFF once no
    other viewer remains. On the first viewer the pipeline is still spinning up,
    so we retry the local connect for a few seconds before giving up."""
    def gen():
        _viewer_delta(+1)                        # demand up -> maybe start pipeline
        s = None
        try:
            deadline = time.monotonic() + 5.0    # producer may be starting up
            while s is None:
                try:
                    s = socket.create_connection(("127.0.0.1", PREVIEW_TCP), timeout=3)
                except OSError:
                    if time.monotonic() >= deadline:
                        return                   # gave up; browser will retry
                    time.sleep(0.3)
            while True:
                chunk = s.recv(8192)
                if not chunk:
                    break
                yield chunk
        finally:
            if s is not None:
                s.close()
            _viewer_delta(-1)                    # demand down -> maybe stop pipeline
    return Response(gen(),
                    mimetype=f"multipart/x-mixed-replace; boundary={PREVIEW_BOUNDARY}")


@app.route("/start", methods=["POST"])
def start():
    with _lock:
        if _is_running():
            return jsonify(ok=False, error="already recording"), 409
        if _state.get("suspended"):
            return jsonify(ok=False, error="busy"), 409
        _state["suspended"] = True               # keep preview off during the swap
    data = request.get_json(silent=True) or {}
    want_preview = bool(data.get("preview", True))   # live preview tee during recording
    _sync_preview()                              # free the camera + preview port
    time.sleep(SETTLE)
    # Thermals are always logged to a file next to the video (server runs under
    # sudo); quality uses record.py's default (no flag).
    cmd = ["python3", RECORD_SCRIPT, "--output-dir", OUTPUT_DIR, "--log-thermals"]
    if want_preview:                             # omit -> record with no preview tee
        cmd += ["--preview-port", str(PREVIEW_TCP)]
    # Audio is ON by default in record.py (auto-detected USB mic, hot-plug,
    # written as a separate WAV + sync sidecar). The checkbox only DISABLES it.
    if data.get("audio") is False:
        cmd.append("--no-audio")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with _lock:
        _state.update(proc=proc, started=time.time(), suspended=False,
                      rec_preview=want_preview)
    return jsonify(ok=True)


@app.route("/stop", methods=["POST"])
def stop():
    with _lock:
        if not _is_running():
            return jsonify(ok=False, error="not recording"), 409
        proc = _state["proc"]
    proc.send_signal(signal.SIGINT)              # clean EOS -> finalized MKV
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.terminate()
    f = _newest_recording()
    with _lock:
        _state.update(proc=None, started=None)
    time.sleep(SETTLE)
    _sync_preview()                              # resume idle preview iff someone's watching
    return jsonify(ok=True, file=f)


def _snapshot_cmd(pattern):
    # Full-res (3840x1200), high quality for sharp ChArUco corners. num-buffers=15
    # lets the ISP auto-exposure settle; we keep the last frame.
    return [
        "gst-launch-1.0",
        "nvv4l2camerasrc", f"device={DEVICE}", "num-buffers=15",
        "!", "video/x-raw(memory:NVMM),format=UYVY,width=3840,height=1200,framerate=30/1",
        "!", "nvvidconv", "!", "video/x-raw,format=I420",
        "!", "nvjpegenc", "quality=95",
        "!", "multifilesink", f"location={pattern}",
    ]


@app.route("/snapshot", methods=["POST"])
def snapshot():
    """Grab one full-res still into CALIB_DIR (for camera calibration). The frame
    holds BOTH cameras (3840x1200); crop halves per-camera in your calib script."""
    with _lock:
        if _is_running():
            return jsonify(ok=False, error="stop recording first"), 409
        if _state.get("suspended"):
            return jsonify(ok=False, error="busy"), 409
        _state["suspended"] = True               # hold the camera for the still
    try:
        os.makedirs(CALIB_DIR, exist_ok=True)
        _sync_preview()                          # release the camera
        time.sleep(SETTLE)
        tmp = os.path.join(CALIB_DIR, "_tmp_%03d.jpg")
        try:
            subprocess.run(_snapshot_cmd(tmp), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        except Exception:                            # noqa: BLE001
            pass
        temps = sorted(glob.glob(os.path.join(CALIB_DIR, "_tmp_*.jpg")))
        saved = None
        if temps:
            n = 1
            while os.path.exists(os.path.join(CALIB_DIR, f"calib_{n:03d}.jpg")):
                n += 1
            saved = os.path.join(CALIB_DIR, f"calib_{n:03d}.jpg")
            os.replace(temps[-1], saved)             # keep the last (AE-settled) frame
            for t in temps[:-1]:
                try:
                    os.remove(t)
                except OSError:
                    pass
    finally:
        with _lock:
            _state["suspended"] = False
        _sync_preview()                          # restore preview iff someone's watching
    if not saved:
        return jsonify(ok=False, error="capture failed (no frame)"), 500
    count = len(glob.glob(os.path.join(CALIB_DIR, "calib_*.jpg")))
    return jsonify(ok=True, file=os.path.basename(saved), count=count)


# --------------------------------------------------------------------------
# File management: list /mnt/video, mount/unmount the SSD, transfer + verify,
# delete. All run as root (the server runs under sudo). Transfer runs in a
# background thread with progress; everything else is quick and synchronous.
# --------------------------------------------------------------------------
def _ssd_dev():
    return f"/dev/disk/by-uuid/{SSD_UUID}"


def _ssd_present():
    return os.path.exists(_ssd_dev())


def _ssd_mounted():
    return os.path.ismount(USB_MNT)


def _human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _ssd_info():
    info = {"uuid": SSD_UUID, "present": _ssd_present(), "mounted": _ssd_mounted(),
            "mountpoint": USB_MNT, "subdir": USB_SUBDIR, "free": None, "total": None}
    if info["mounted"]:
        try:
            st = os.statvfs(USB_MNT)
            info["total"] = _human(st.f_blocks * st.f_frsize)
            info["free"] = _human(st.f_bavail * st.f_frsize)
        except OSError:
            pass
    return info


def _ssd_size(name):
    p = os.path.join(USB_MNT, USB_SUBDIR, name)
    try:
        return os.path.getsize(p)
    except OSError:
        return None


def _list_files():
    """Every file in /mnt/video, with size and (if mounted) its SSD copy status."""
    mounted = _ssd_mounted()
    out = []
    for path in sorted(glob.glob(os.path.join(OUTPUT_DIR, "*"))):
        if not os.path.isfile(path):
            continue
        name = os.path.basename(path)
        size = os.path.getsize(path)
        m = re.match(r"(game_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})", name)
        entry = {"name": name, "size": size, "size_h": _human(size),
                 "stem": m.group(1) if m else name}
        if mounted:
            ssz = _ssd_size(name)
            entry["on_ssd"] = ssz is not None
            entry["match"] = (ssz == size)
        else:
            entry["on_ssd"] = None
            entry["match"] = None
        out.append(entry)
    return out


def _safe_local(name):
    """Absolute path in OUTPUT_DIR for a basename, or None if it escapes the dir."""
    if not name or "/" in name or name in (".", ".."):
        return None
    p = os.path.realpath(os.path.join(OUTPUT_DIR, name))
    return p if os.path.dirname(p) == os.path.realpath(OUTPUT_DIR) else None


def _run_ok(cmd, timeout=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:                           # noqa: BLE001
        return False, repr(e)


@app.route("/files")
def files_page():
    return FILES_PAGE


@app.route("/api/files")
def api_files():
    return jsonify(recording=_is_running(), output_dir=OUTPUT_DIR,
                   ssd=_ssd_info(), files=_list_files())


@app.route("/api/mount", methods=["POST"])
def api_mount():
    if not _ssd_present():
        return jsonify(ok=False, error="No SSD detected (its UUID isn't present). Plug it in.")
    if _ssd_mounted():
        return jsonify(ok=True, already=True)
    os.makedirs(USB_MNT, exist_ok=True)
    out = ""
    for cmd in (["mount", _ssd_dev(), USB_MNT],
                ["mount", "-t", "exfat", _ssd_dev(), USB_MNT],
                ["mount.exfat-fuse", _ssd_dev(), USB_MNT]):  # FUSE fallback (this Orin)
        _, out = _run_ok(cmd, timeout=30)
        if _ssd_mounted():
            return jsonify(ok=True)
    return jsonify(ok=False, error=f"mount failed: {out or 'unknown'}")


@app.route("/api/unmount", methods=["POST"])
def api_unmount():
    if not _ssd_mounted():
        return jsonify(ok=True, already=True)
    _run_ok(["sync"], timeout=30)
    ok, out = _run_ok(["umount", USB_MNT], timeout=30)
    if ok and not _ssd_mounted():
        return jsonify(ok=True)
    return jsonify(ok=False, error=f"unmount failed (drive in use?): {out or 'unknown'}")


def _transfer_worker(srcs, dest):
    cmd = ["rsync", "-a", "--info=progress2", "--"] + srcs + [dest + "/"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            m = re.search(r"(\d+)%", line)
            with _xfer_lock:
                _xfer["line"] = line
                if m:
                    _xfer["percent"] = int(m.group(1))
        proc.wait()
        rc = proc.returncode
        bad = [os.path.basename(p) for p in srcs
               if not (os.path.exists(os.path.join(dest, os.path.basename(p)))
                       and os.path.getsize(os.path.join(dest, os.path.basename(p)))
                       == os.path.getsize(p))]
        with _xfer_lock:
            _xfer["done"] = True
            _xfer["active"] = False
            _xfer["ok"] = (rc == 0 and not bad)
            if _xfer["ok"]:
                _xfer["percent"] = 100
            else:
                _xfer["error"] = f"rsync exit {rc}" + (f"; verify failed: {bad}" if bad else "")
    except Exception as e:                           # noqa: BLE001
        with _xfer_lock:
            _xfer.update(done=True, active=False, ok=False, error=repr(e))


@app.route("/api/transfer", methods=["POST"])
def api_transfer():
    if _is_running():
        return jsonify(ok=False, error="Stop recording before transferring."), 409
    if not _ssd_mounted():
        return jsonify(ok=False, error="SSD is not mounted."), 409
    with _xfer_lock:
        if _xfer["active"]:
            return jsonify(ok=False, error="A transfer is already running."), 409
    names = (request.get_json(silent=True) or {}).get("names") or []
    srcs = [p for p in (_safe_local(n) for n in names) if p and os.path.isfile(p)]
    if not srcs:
        return jsonify(ok=False, error="No valid files selected."), 400
    dest = os.path.join(USB_MNT, USB_SUBDIR)
    os.makedirs(dest, exist_ok=True)
    with _xfer_lock:
        _xfer.update(active=True, percent=0, line="", done=False, ok=None, error=None,
                     names=[os.path.basename(p) for p in srcs])
    threading.Thread(target=_transfer_worker, args=(srcs, dest), daemon=True).start()
    return jsonify(ok=True, count=len(srcs))


@app.route("/api/transfer_status")
def api_transfer_status():
    with _xfer_lock:
        return jsonify(dict(_xfer))


@app.route("/api/delete", methods=["POST"])
def api_delete():
    if _is_running():
        return jsonify(ok=False, error="Stop recording before deleting."), 409
    names = (request.get_json(silent=True) or {}).get("names") or []
    deleted, errors = [], []
    for n in names:
        p = _safe_local(n)
        if p and os.path.isfile(p):
            try:
                os.remove(p)
                deleted.append(n)
            except OSError as e:
                errors.append({"name": n, "error": repr(e)})
        else:
            errors.append({"name": n, "error": "not found"})
    return jsonify(ok=not errors, deleted=deleted, errors=errors)


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Camera Rig</title>
<style>
 :root { color-scheme: dark; }
 body { font-family: system-ui, sans-serif; margin:0; background:#111; color:#eee; }
 .wrap { max-width:560px; margin:0 auto; padding:20px; }
 h1 { font-size:1.2rem; }
 .card { padding:14px; border-radius:10px; background:#1b1b1b; margin-bottom:16px; }
 #preview { width:100%; border-radius:10px; background:#000; display:block; aspect-ratio:16/5; object-fit:contain; }
 .dot { display:inline-block; width:12px; height:12px; border-radius:50%; background:#555;
        margin-right:8px; vertical-align:middle; }
 .dot.rec { background:#e33; animation:pulse 1.2s infinite; }
 @keyframes pulse { 50% { opacity:.3; } }
 button { width:100%; padding:22px; font-size:1.3rem; font-weight:700; border:0;
          border-radius:12px; margin:8px 0; color:#fff; }
 #start { background:#1f8a3b; } #stop { background:#b3271e; }
 button:disabled { opacity:.35; }
 .btnlink { display:block; text-align:center; text-decoration:none; padding:18px;
            font-size:1.15rem; font-weight:700; border-radius:12px; margin:8px 0;
            color:#fff; background:#2a6f9e; }
 .btnlink.disabled { opacity:.35; pointer-events:none; }
 label { display:block; margin:8px 0; }
 input[type=number] { width:70px; background:#222; color:#eee; border:1px solid #444;
                      border-radius:6px; padding:6px; }
 table { width:100%; border-collapse:collapse; font-size:.95rem; }
 td { padding:4px 6px; border-bottom:1px solid #222; }
 td.t { text-align:right; font-variant-numeric:tabular-nums; }
 .big { font-size:2rem; font-weight:700; }
 .ok{color:#4caf50}.warn{color:#ffb300}.hot{color:#e33}
 .muted{color:#888; font-size:.85rem; word-break:break-all;}
</style></head><body><div class="wrap">
 <h1>&#127909; Camera Rig</h1>
 <div class="card" style="padding:6px">
   <img id="preview" src="/preview.mjpg" alt="preview">
 </div>
 <div class="card">
   <span class="dot" id="dot"></span><span id="statetext">&hellip;</span>
   <div id="detail" class="muted"></div>
 </div>
 <div class="card">
   <label><input type="checkbox" id="pvrec" checked> Live preview while recording</label>
   <div class="muted">Off &rarr; camera is dedicated to recording (no preview until you stop).</div>
   <label><input type="checkbox" id="audio" checked> Record audio (USB mic)</label>
   <div class="muted">On by default. Separate WAV + sync sidecar; auto-starts if a mic is present (or plugged in mid-record). Untick for video only. Thermals always logged.</div>
   <div id="rootnote" class="muted"></div>
 </div>
 <button id="start">&#9679; Start Recording</button>
 <button id="stop" disabled>&#9632; Stop Recording</button>
 <a id="manage" href="/files" class="btnlink">&#128193; Manage Files</a>
 <div class="card">
   <label><input type="checkbox" id="showth"> Show live thermals</label>
   <div id="thermals" style="display:none">
     <div>Max: <span id="maxt" class="big">&ndash;</span> &nbsp; CPU: <span id="cpu">&ndash;</span></div>
     <table id="ztable"></table>
   </div>
 </div>
</div>
<script>
const $ = id => document.getElementById(id);
const post = (u,b) => fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},
                              body:JSON.stringify(b||{})}).then(r=>r.json());
const fmt = s => Math.floor(s/60)+':'+String(s%60).padStart(2,'0');
const tclass = c => c>=80?'hot':c>=65?'warn':'ok';

// Preview is always shown when a stream exists. previewAvailable is false only
// while a recording started with "Live preview while recording" unticked -- then
// there's no stream to fetch, so we hide the <img> and don't retry.
let previewAvailable = true;
function reconnectPreview(){ if(previewAvailable) $('preview').src = '/preview.mjpg?' + Date.now(); }
function applyPreviewState(){
  if(previewAvailable){ $('preview').style.display='block'; }
  else { $('preview').style.display='none'; $('preview').src=''; }
}
$('preview').onerror = () => { if(previewAvailable) setTimeout(reconnectPreview, 1200); };

let wasRunning = null;
async function refreshStatus(){
  try{
    const s = await (await fetch('/status')).json();
    $('dot').className = 'dot' + (s.running?' rec':'');
    $('statetext').textContent = s.running ? 'RECORDING — '+fmt(s.elapsed) : 'Idle';
    $('detail').textContent = s.running && s.file ? s.file : '';
    $('start').disabled = s.running;
    $('stop').disabled  = !s.running;
    $('manage').classList.toggle('disabled', s.running);  // no file mgmt while recording
    $('rootnote').textContent = !s.server_root
      ? 'Note: thermal logging needs the server run under sudo.' : '';
    const prevAvail = previewAvailable;
    previewAvailable = (s.preview !== false);
    if(prevAvail !== previewAvailable) applyPreviewState();
    if((wasRunning !== null && wasRunning !== s.running) || (prevAvail !== previewAvailable))
      setTimeout(reconnectPreview, 1500);
    wasRunning = s.running;
  }catch(e){ $('statetext').textContent = 'server unreachable'; }
}
$('start').onclick = async () => { $('start').disabled=true;
  await post('/start',{preview:$('pvrec').checked, audio:$('audio').checked});
  refreshStatus(); };
$('stop').onclick  = async () => { $('stop').disabled=true; await post('/stop'); refreshStatus(); };
$('showth').onchange = () => { $('thermals').style.display = $('showth').checked?'block':'none'; };

async function refreshThermals(){
  if(!$('showth').checked) return;
  try{
    const t = await (await fetch('/thermals')).json();
    $('maxt').textContent = t.max_c!=null ? t.max_c.toFixed(1)+'°C' : '–';
    $('maxt').className = 'big ' + (t.max_c!=null?tclass(t.max_c):'');
    $('cpu').textContent = t.cpu!=null ? t.cpu+'%' : '–';
    $('ztable').innerHTML = t.zones.map(z =>
      '<tr><td>'+z.name+'</td><td class="t '+tclass(z.temp_c)+'">'+
      z.temp_c.toFixed(1)+'°C</td></tr>').join('');
  }catch(e){}
}
setInterval(refreshStatus, 1500);
setInterval(refreshThermals, 2000);
refreshStatus();
</script></body></html>"""


FILES_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Manage Files</title>
<style>
 :root { color-scheme: dark; }
 body { font-family: system-ui, sans-serif; margin:0; background:#111; color:#eee; }
 .wrap { max-width:720px; margin:0 auto; padding:20px; }
 h1 { font-size:1.2rem; }
 a.back { color:#7fb2d9; text-decoration:none; }
 .card { padding:14px; border-radius:10px; background:#1b1b1b; margin-bottom:16px; }
 .banner { background:#5a1f1f; color:#ffdede; padding:12px; border-radius:10px; margin-bottom:16px; }
 button { padding:12px 16px; font-size:1rem; font-weight:700; border:0; border-radius:10px;
          color:#fff; background:#2a6f9e; margin:4px 4px 4px 0; }
 button.danger { background:#b3271e; } button.go { background:#1f8a3b; }
 button:disabled { opacity:.35; }
 table { width:100%; border-collapse:collapse; font-size:.92rem; }
 th,td { padding:6px 8px; border-bottom:1px solid #222; text-align:left; }
 td.sz { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
 .ok{color:#4caf50}.warn{color:#ffb300}.muted{color:#888;}
 .bar { height:14px; background:#333; border-radius:7px; overflow:hidden; }
 .bar > div { height:100%; width:0; background:#1f8a3b; transition:width .3s; }
 .name { word-break:break-all; }
</style></head><body><div class="wrap">
 <h1>&#128193; Manage Files <a class="back" href="/">&larr; back to recorder</a></h1>
 <div id="recbanner" class="banner" style="display:none">Recording in progress — file
   management is disabled to protect the recording. Stop recording first.</div>

 <div class="card">
   <div id="ssd">…</div>
   <div id="ssdbtn" style="margin-top:8px"></div>
 </div>

 <div class="card" id="xfercard" style="display:none">
   <div>Transferring… <span id="xferpct">0%</span></div>
   <div class="bar"><div id="xferbar"></div></div>
   <div id="xferline" class="muted" style="margin-top:6px"></div>
 </div>

 <div class="card">
   <button class="go" id="btnxfer" onclick="transfer()">&#8681; Transfer selected</button>
   <button class="danger" id="btndel" onclick="del()">&#128465; Delete selected</button>
   <button onclick="load()">&#8635; Refresh</button>
   <div class="muted" id="selnote" style="margin-top:8px"></div>
 </div>

 <div class="card">
   <table>
     <thead><tr>
       <th><input type="checkbox" id="all" onchange="toggleAll()"></th>
       <th>File</th><th class="sz">Size</th><th>On SSD</th>
     </tr></thead>
     <tbody id="rows"></tbody>
   </table>
 </div>
</div>
<script>
const $ = id => document.getElementById(id);
const post = (u,b) => fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},
                              body:JSON.stringify(b||{})}).then(r=>r.json());
let mounted=false, recording=false;

function selected(){ return [...document.querySelectorAll('.sel:checked')].map(c=>c.dataset.name); }
function toggleAll(){ document.querySelectorAll('.sel').forEach(c=>c.checked=$('all').checked); updateSel(); }
function updateSel(){
  const n=selected().length;
  $('selnote').textContent = n? (n+' selected') : 'Select files above, then Transfer or Delete.';
  $('btnxfer').disabled = recording || !mounted || !n;
  $('btndel').disabled  = recording || !n;
}

function ssdCell(f){
  if(!mounted) return '<span class="muted">—</span>';
  if(f.match) return '<span class="ok">&#10003; verified</span>';
  if(f.on_ssd) return '<span class="warn">&#9888; size differs</span>';
  return '<span class="muted">not copied</span>';
}

async function load(){
  const s = await (await fetch('/api/files')).json();
  recording = s.recording; mounted = s.ssd.mounted;
  $('recbanner').style.display = recording ? 'block' : 'none';

  // SSD status + mount/unmount button
  let html, btn='';
  if(!s.ssd.present){ html='No SSD detected — plug it in, then Refresh.'; }
  else if(!s.ssd.mounted){ html='SSD detected, not mounted.';
    btn='<button class="go" onclick="mount()">Mount SSD</button>'; }
  else { html='Mounted at '+s.ssd.mountpoint+' — free '+s.ssd.free+' of '+s.ssd.total
             +' &nbsp;(copies go to '+s.ssd.mountpoint+'/'+s.ssd.subdir+'/)';
    btn='<button onclick="unmount()">Unmount SSD</button>'; }
  $('ssd').innerHTML = html; $('ssdbtn').innerHTML = recording ? '' : btn;

  // file rows
  $('rows').innerHTML = s.files.length ? s.files.map(f =>
    '<tr><td><input type="checkbox" class="sel" data-name="'+f.name+'" onchange="updateSel()"></td>'+
    '<td class="name">'+f.name+'</td><td class="sz">'+f.size_h+'</td><td>'+ssdCell(f)+'</td></tr>'
  ).join('') : '<tr><td colspan="4" class="muted">No files in '+s.output_dir+'.</td></tr>';
  $('all').checked=false;
  updateSel();
}

async function mount(){ const r=await post('/api/mount'); if(!r.ok) alert(r.error||'mount failed'); load(); }
async function unmount(){ const r=await post('/api/unmount'); if(!r.ok) alert(r.error||'unmount failed'); load(); }

async function transfer(){
  const names = selected(); if(!names.length) return;
  const r = await post('/api/transfer', {names});
  if(!r.ok){ alert(r.error||'transfer failed'); return; }
  $('xfercard').style.display='block'; $('btnxfer').disabled=true; $('btndel').disabled=true;
  const t = setInterval(async () => {
    const s = await (await fetch('/api/transfer_status')).json();
    $('xferpct').textContent = s.percent+'%'; $('xferbar').style.width = s.percent+'%';
    $('xferline').textContent = s.line||'';
    if(s.done){ clearInterval(t); $('xfercard').style.display='none';
      if(!s.ok) alert('Transfer error: '+(s.error||'unknown')); load(); }
  }, 1000);
}

async function del(){
  const names = selected(); if(!names.length) return;
  if(!confirm('Delete '+names.length+' file(s) from the Orin? This cannot be undone.\\n\\n'
              +'(Verify the SSD copies first — deletes are permanent.)')) return;
  const r = await post('/api/delete', {names});
  if(!r.ok) alert('Some files could not be deleted:\\n'+JSON.stringify(r.errors,null,1));
  load();
}

load();
setInterval(() => { if($('xfercard').style.display==='none') load(); }, 5000);
</script></body></html>"""


if __name__ == "__main__":
    print(f"Camera rig control panel on http://0.0.0.0:{PORT}  (find IP: hostname -I)")
    # No preview on boot: the camera stays idle until a browser opens /preview.mjpg.
    try:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    finally:
        _stop_idle_preview()                     # tidy up any on-demand pipeline
