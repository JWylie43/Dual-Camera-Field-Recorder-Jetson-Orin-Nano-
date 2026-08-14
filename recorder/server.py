#!/usr/bin/env python3
"""
server.py - single-owner capture + web app for the dual Raspberry Pi HQ (IMX477)
rig on the Jetson Orin Nano. (This branch's server.py IS the Pi app; the Arducam
server.py lives on main. camera-rig.service runs whichever branch's server.py is
checked out, so startup is unchanged.)

WHY ONE PROCESS: Argus gives ONE process exclusive ownership of a sensor. Two
processes can't share a camera, so a single process owns BOTH cameras (sensor-id
0 and 1), composites them side by side, and fans the combined frame out to a live
browser preview AND the recorder - while setting exposure/gain LIVE on both
sensors via g_object_set. Everything the camera touches lives in here.

    cam0 (Argus/ISP) ┐                       ┌─ preview  -> browser   (always on)
                     ├ nvcompositor ─ tee ───┤
    cam1 (Argus/ISP) ┘                       └─ record   -> MKV       (toggled live)

The camera session is created once at startup and NEVER cycled (measured: sensors
+ ISP ~2.5 W, encode ~free, thermals a non-issue - so always-on is simplest and
avoids the Argus session-cycling wedge risk). Clicking Record just adds a full-res
MJPEG->MKV branch to the already-running tee; Stop EOSes and drops just that
branch, so the preview never blinks and the file finalizes seekable.

Run on the Orin (after `sudo camswitch pi`): camera-rig.service runs this on boot.
For dev, stop the service and run it by hand to see logs:
    sudo systemctl stop camera-rig.service
    python3 server.py
    # browse from your phone to  http://<orin-ip>:8080   (hostname -I)
"""

import datetime
import glob
import os
import re
import shutil
import signal
import subprocess
import threading
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

from flask import Flask, Response, jsonify, request  # noqa: E402

# --- camera / pipeline config ---------------------------------------------
SENSOR_IDS = (0, 1)                 # nvarguscamerasrc sensor-id for (left, right)
EYE_W, EYE_H, FPS = 1920, 1080, 30  # per-camera; mode 1 is 1920x1080@60 max
COMBINED_W = EYE_W * 2              # 3840 wide, side-by-side
PREVIEW_W, PREVIEW_H = 1280, 360    # downscaled preview (keeps the 32:9 combined shape)
PREVIEW_QUALITY = 50
PREVIEW_FPS = 15                    # preview is for framing - a low rate keeps the
                                    # Python MJPEG serving (GIL-bound) light and smooth
REC_QUALITY = 85                    # full-res record JPEG quality (visually ~lossless)
OUTPUT_DIR = "/mnt/video"           # NVMe mount; recordings land here
FILENAME_PREFIX = "game"            # game_YYYY-MM-DD_HH-MM-SS.mkv
# External USB SSD for field offload (the Manage Files page mounts/unmounts it).
SSD_UUID = "5E64-018F"              # this drive's exFAT UUID (lsblk -f)
USB_MNT = "/mnt/usb"                # where we mount it
USB_SUBDIR = "orin-video"           # folder recordings are copied into on the SSD
THERMAL_INTERVAL_MS = 5000          # tegrastats sampling for the per-recording log
PORT = 8080
BOUNDARY = "spinframe"

# IMX477 limits from the sensor mode report (exposure in microseconds).
EXPOSURE_MIN_US, EXPOSURE_MAX_US = 13, 33000     # 13us floor; 33ms = 1/30s ceiling @ 30fps
GAIN_MIN, GAIN_MAX = 1.0, 22.25

Gst.init(None)


def log(msg):
    print(msg, flush=True)


def build_pipeline():
    """Both cameras -> compositor -> tee -> preview (appsink). Named elements:
    cam0/cam1 (live controls), 't' (the tee we tap for recording), and 'preview'
    (the appsink we pull JPEGs from)."""
    eye_caps = f"video/x-raw(memory:NVMM),width={EYE_W},height={EYE_H},framerate={FPS}/1"
    desc = (
        f"nvarguscamerasrc name=cam0 sensor-id={SENSOR_IDS[0]} ! {eye_caps} ! comp.sink_0 "
        f"nvarguscamerasrc name=cam1 sensor-id={SENSOR_IDS[1]} ! {eye_caps} ! comp.sink_1 "
        f"nvcompositor name=comp "
        f"sink_0::xpos=0 sink_0::ypos=0 sink_0::width={EYE_W} sink_0::height={EYE_H} "
        f"sink_1::xpos={EYE_W} sink_1::ypos=0 sink_1::width={EYE_W} sink_1::height={EYE_H} "
        f"! video/x-raw(memory:NVMM),width={COMBINED_W},height={EYE_H},framerate={FPS}/1 "
        f"! tee name=t "
        f"t. ! queue leaky=downstream max-size-buffers=4 "
        f"! nvvidconv ! video/x-raw,format=I420,width={PREVIEW_W},height={PREVIEW_H} "
        f"! videorate drop-only=true ! video/x-raw,framerate={PREVIEW_FPS}/1 "
        f"! nvjpegenc quality={PREVIEW_QUALITY} "
        f"! appsink name=preview emit-signals=true max-buffers=1 drop=true"
    )
    return Gst.parse_launch(desc)


# --- latest preview frame, shared to HTTP clients -------------------------
_frame = {"data": None}
_frame_cond = threading.Condition()


def _on_new_sample(appsink):
    sample = appsink.emit("pull-sample")
    if sample is None:
        return Gst.FlowReturn.OK
    buf = sample.get_buffer()
    ok, mapinfo = buf.map(Gst.MapFlags.READ)
    if ok:
        data = bytes(mapinfo.data)
        buf.unmap(mapinfo)
        with _frame_cond:
            _frame["data"] = data
            _frame_cond.notify_all()
    return Gst.FlowReturn.OK


pipeline = build_pipeline()
cam0 = pipeline.get_by_name("cam0")
cam1 = pipeline.get_by_name("cam1")
tee = pipeline.get_by_name("t")
_appsink = pipeline.get_by_name("preview")
_appsink.connect("new-sample", _on_new_sample)


# --- camera controls (applied to BOTH cams so the stereo pair stays matched) ---
# NOTE: we never set Argus properties on the cameras during startup - only in
# response to a user control change - because a set re-creates the capture stream,
# and doing that mid-startup on two cameras crashes. Startup is pure auto-exposure.
_ctrl = {"auto": True, "exposure_us": 8000, "gain": 4.0}
_ctrl_lock = threading.Lock()


def apply_controls():
    """Push the current control state onto both nvarguscamerasrc elements, live."""
    with _ctrl_lock:
        auto, us, gain = _ctrl["auto"], _ctrl["exposure_us"], _ctrl["gain"]
    for cam in (cam0, cam1):
        if auto:
            cam.set_property("exposuretimerange",
                             f"{EXPOSURE_MIN_US * 1000} {EXPOSURE_MAX_US * 1000}")
            cam.set_property("gainrange", f"{GAIN_MIN} {GAIN_MAX}")
            cam.set_property("aelock", False)
        else:
            ns = int(us) * 1000
            cam.set_property("exposuretimerange", f"{ns} {ns}")
            cam.set_property("gainrange", f"{gain} {gain}")
            cam.set_property("aelock", True)


# --- recording: add/remove a full-res MKV branch on the LIVE tee -----------
# Preview keeps running the whole time. On stop we push EOS down JUST the record
# branch (so matroskamux writes its seek index -> seekable file), then tear the
# branch down off the streaming thread.
_rec_lock = threading.Lock()
_rec = {"active": False, "elems": None, "tee_pad": None, "path": None}


def _make_output_path():
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(OUTPUT_DIR, f"{FILENAME_PREFIX}_{stamp}.mkv")


def _tee_request_pad():
    # request_pad_simple is the 1.20+ name; get_request_pad is the older one.
    if hasattr(tee, "request_pad_simple"):
        return tee.request_pad_simple("src_%u")
    return tee.get_request_pad("src_%u")


def start_recording():
    with _rec_lock:
        if _rec["active"]:
            return None, "already recording"
        if not os.path.isdir(OUTPUT_DIR):
            return None, f"{OUTPUT_DIR} not found (is the NVMe mounted?)"
        path = _make_output_path()

        # Full-res combined frame -> I420 -> hardware MJPEG -> MKV. leaky=downstream
        # protects the preview: if the encoder ever falls behind, this branch drops
        # a frame instead of stalling the tee (which would freeze the preview too).
        q = Gst.ElementFactory.make("queue", None)
        q.set_property("leaky", 2)               # 2 = downstream
        q.set_property("max-size-buffers", 16)
        conv = Gst.ElementFactory.make("nvvidconv", None)
        capsf = Gst.ElementFactory.make("capsfilter", None)
        capsf.set_property("caps", Gst.Caps.from_string("video/x-raw,format=I420"))
        enc = Gst.ElementFactory.make("nvjpegenc", None)
        enc.set_property("quality", REC_QUALITY)
        parse = Gst.ElementFactory.make("jpegparse", None)
        mux = Gst.ElementFactory.make("matroskamux", None)
        sink = Gst.ElementFactory.make("filesink", None)
        sink.set_property("location", path)
        sink.set_property("sync", False)
        elems = [q, conv, capsf, enc, parse, mux, sink]
        if any(e is None for e in elems):
            return None, "failed to create record elements (missing plugin?)"

        for e in elems:
            pipeline.add(e)
        q.link(conv)
        conv.link(capsf)
        capsf.link(enc)
        enc.link(parse)
        parse.link(mux)
        mux.link(sink)

        tee_pad = _tee_request_pad()
        tee_pad.link(q.get_static_pad("sink"))
        for e in elems:
            e.sync_state_with_parent()

        _rec.update(active=True, elems=elems, tee_pad=tee_pad, path=path)
        _start_thermal_log(path)
        log(f"recording -> {path}")
        return path, None


def stop_recording():
    with _rec_lock:
        if not _rec["active"]:
            return None, "not recording"
        elems = _rec["elems"]
        tee_pad = _rec["tee_pad"]
        path = _rec["path"]
        _rec.update(active=False, elems=None, tee_pad=None, path=None)

    _stop_thermal_log()
    q, sink = elems[0], elems[-1]
    finalized = threading.Event()

    # Know when the file is fully written: EOS reaching the filesink means
    # matroskamux has already pushed its trailer/index. Drop it so this branch's
    # EOS can't bubble up to the pipeline bus.
    def on_sink_event(pad, info):
        ev = info.get_event()
        if ev and ev.type == Gst.EventType.EOS:
            finalized.set()
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK

    sink.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, on_sink_event)

    # Block the tee's output pad; once idle, unlink it and inject EOS into the
    # branch so the file finalizes cleanly. Runs on the streaming thread.
    def on_block(pad, info):
        pad.unlink(q.get_static_pad("sink"))
        q.get_static_pad("sink").send_event(Gst.Event.new_eos())
        return Gst.PadProbeReturn.REMOVE

    tee_pad.add_probe(Gst.PadProbeType.IDLE, on_block)

    # Teardown off the streaming thread: wait for finalize, then NULL + remove.
    def teardown():
        if not finalized.wait(timeout=10):
            log("warning: record branch EOS timed out; tearing down anyway")
        for e in elems:
            e.set_state(Gst.State.NULL)
        for e in elems:
            pipeline.remove(e)
        tee.release_request_pad(tee_pad)
        log(f"recording finalized -> {path}")

    threading.Thread(target=teardown, daemon=True).start()
    return path, None


# --- Flask app ------------------------------------------------------------
app = Flask(__name__)

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pi HQ rig</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; background:#111; color:#eee; font:15px/1.4 system-ui,sans-serif; }
  header { padding:10px 14px; font-weight:600; background:#1b1b1b; }
  #view { width:100%; display:block; background:#000; }
  .panel { padding:14px; max-width:720px; }
  .row { display:flex; align-items:center; gap:12px; margin:14px 0; }
  .row label { width:90px; color:#bbb; }
  .row input[type=range] { flex:1; }
  .row .val { width:88px; text-align:right; font-variant-numeric:tabular-nums; color:#8fd; }
  .muted { opacity:.4; }
  .toggle { display:flex; align-items:center; gap:10px; margin:6px 0 16px; }
  .hint { color:#888; font-size:13px; }
  .btnlink { display:inline-block; padding:12px 14px; border-radius:10px; background:#2a6f9e;
             color:#fff; text-decoration:none; font-weight:700; }
  .btnlink.disabled { opacity:.4; pointer-events:none; }
  .big { font-size:1.1rem; font-weight:700; }
  .ok{color:#4caf50}.warn{color:#ffb300}.hot{color:#e33}
  .ztable { width:100%; border-collapse:collapse; font-size:13px; margin-top:6px; }
  .ztable td { padding:3px 6px; border-bottom:1px solid #222; }
  .ztable td.t { text-align:right; font-variant-numeric:tabular-nums; }
  #recBtn { width:100%; padding:16px; font-size:18px; font-weight:700; border:0; border-radius:10px;
            background:#c0392b; color:#fff; cursor:pointer; }
  #recBtn.recording { background:#7a1c12; box-shadow:0 0 0 3px #e74c3c inset; animation:pulse 1.4s infinite; }
  #recBtn:disabled { opacity:.5; cursor:default; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.72} }
  #recState { display:block; margin-top:8px; }
</style></head><body>
<header>Raspberry Pi HQ stereo rig</header>
<img id="view" src="/preview.mjpg" alt="preview">
<div class="panel">
  <button id="recBtn">● Record</button>
  <span id="recState" class="hint"></span>
</div>
<div class="panel">
  <a id="manage" href="/files" class="btnlink">&#128193; Manage Files</a>
  <div id="storage" class="hint" style="margin-top:8px"></div>
</div>
<div class="panel">
  <label class="toggle"><input type="checkbox" id="showth"> Show live thermals</label>
  <div id="thermals" style="display:none; margin-top:8px">
    <div>Max: <span id="maxt" class="big">&ndash;</span> &nbsp; CPU: <span id="cpu">&ndash;</span></div>
    <table id="ztable" class="ztable"></table>
  </div>
</div>
<div class="panel">
  <div class="toggle">
    <input type="checkbox" id="auto" checked>
    <label for="auto">Auto exposure</label>
  </div>
  <div class="row" id="expRow">
    <label>Exposure</label>
    <input type="range" id="exp" min="13" max="33000" step="1" value="8000">
    <span class="val" id="expVal">8000 &micro;s</span>
  </div>
  <div class="row" id="gainRow">
    <label>Gain</label>
    <input type="range" id="gain" min="1" max="22.25" step="0.25" value="4">
    <span class="val" id="gainVal">4.00&times;</span>
  </div>
  <div class="hint">Controls apply to <b>both</b> cameras identically. Lower exposure = less
    motion blur (needs more light or gain).</div>
</div>
<script>
const $ = id => document.getElementById(id);
function post(url, body){ return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
  body: body ? JSON.stringify(body) : undefined}); }

// --- controls ---
function syncEnabled(){
  const auto = $('auto').checked;
  $('expRow').classList.toggle('muted', auto);
  $('gainRow').classList.toggle('muted', auto);
  $('exp').disabled = auto; $('gain').disabled = auto;
}
$('auto').addEventListener('change', () => { syncEnabled(); post('/control',{auto:$('auto').checked}); });
$('exp').addEventListener('input', () => { $('expVal').innerHTML = $('exp').value+' &micro;s'; });
$('exp').addEventListener('change', () => post('/control',{exposure_us:parseInt($('exp').value)}));
$('gain').addEventListener('input', () => { $('gainVal').innerHTML = (+$('gain').value).toFixed(2)+'&times;'; });
$('gain').addEventListener('change', () => post('/control',{gain:parseFloat($('gain').value)}));
syncEnabled();

// --- recording ---
let recording = false;
async function refreshStatus(){
  try {
    const s = await (await fetch('/status')).json();
    recording = s.recording;
    $('recBtn').textContent = recording ? '■ Stop' : '● Record';
    $('recBtn').classList.toggle('recording', recording);
    $('recState').textContent = recording ? ('Recording → ' + (s.file || '')) : '';
    $('storage').innerHTML = storageHtml(s.storage);
    $('manage').classList.toggle('disabled', recording);
  } catch(e){}
}
$('recBtn').addEventListener('click', async () => {
  $('recBtn').disabled = true;
  try { await post(recording ? '/stop' : '/start'); } finally { await refreshStatus(); $('recBtn').disabled = false; }
});

// --- storage + live thermals ---
function storageHtml(st){
  if(!st) return '';
  const gb = st.free_bytes/1e9, cls = gb<40?'hot':gb<150?'warn':'ok';
  return '&#128190; NVMe: <b>'+st.used+'</b> / '+st.total+' used ('+st.pct+'%) &middot; '
       + '<span class="'+cls+'">'+st.free+' free</span>';
}
const tclass = c => c>=80?'hot':c>=65?'warn':'ok';
$('showth').addEventListener('change', () => {
  $('thermals').style.display = $('showth').checked ? 'block' : 'none';
});
async function refreshThermals(){
  if(!$('showth').checked) return;
  try {
    const t = await (await fetch('/thermals')).json();
    $('maxt').textContent = t.max_c!=null ? t.max_c.toFixed(1)+'°C' : '–';
    $('maxt').className = 'big ' + (t.max_c!=null?tclass(t.max_c):'');
    $('cpu').textContent = t.cpu!=null ? t.cpu+'%' : '–';
    $('ztable').innerHTML = t.zones.map(z =>
      '<tr><td>'+z.name+'</td><td class="t '+tclass(z.temp_c)+'">'+
      z.temp_c.toFixed(1)+'°C</td></tr>').join('');
  } catch(e){}
}
setInterval(refreshThermals, 2000);

refreshStatus();
setInterval(refreshStatus, 3000);
</script>
</body></html>"""


@app.route("/")
def index():
    return PAGE


@app.route("/preview.mjpg")
def preview():
    def gen():
        while True:
            with _frame_cond:
                _frame_cond.wait(timeout=5)
                data = _frame["data"]
            if data is None:
                continue
            yield (b"--" + BOUNDARY.encode() + b"\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" + data + b"\r\n")
    return Response(gen(), mimetype=f"multipart/x-mixed-replace; boundary={BOUNDARY}")


@app.route("/control", methods=["POST"])
def control():
    data = request.get_json(silent=True) or {}
    with _ctrl_lock:
        if "auto" in data:
            _ctrl["auto"] = bool(data["auto"])
        if "exposure_us" in data:
            _ctrl["exposure_us"] = max(EXPOSURE_MIN_US, min(EXPOSURE_MAX_US, int(data["exposure_us"])))
        if "gain" in data:
            _ctrl["gain"] = max(GAIN_MIN, min(GAIN_MAX, float(data["gain"])))
        state = dict(_ctrl)
    apply_controls()
    return jsonify(ok=True, **state)


@app.route("/start", methods=["POST"])
def start():
    path, err = start_recording()
    if err:
        return jsonify(ok=False, error=err), 409
    return jsonify(ok=True, file=os.path.basename(path))


@app.route("/stop", methods=["POST"])
def stop():
    path, err = stop_recording()
    if err:
        return jsonify(ok=False, error=err), 409
    return jsonify(ok=True, file=os.path.basename(path) if path else None)


@app.route("/status")
def status():
    with _rec_lock:
        active = _rec["active"]
        path = _rec["path"]
    return jsonify(recording=active, file=os.path.basename(path) if (active and path) else None,
                   storage=_storage_info())


# --------------------------------------------------------------------------
# Live thermals (read from sysfs, no root needed)
# --------------------------------------------------------------------------
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
    except Exception:  # noqa: BLE001
        return None


def _read_sysfs(path):
    with open(path, "rb") as f:
        return f.read().decode("ascii", "replace").strip()


def _read_thermals():
    zones = []
    for tz in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        try:
            name = _read_sysfs(os.path.join(tz, "type"))
            milli = int(_read_sysfs(os.path.join(tz, "temp")))
            zones.append({"name": name, "temp_c": round(milli / 1000.0, 1)})
        except Exception:  # noqa: BLE001
            continue
    return zones


@app.route("/thermals")
def thermals():
    try:
        zones = _read_thermals()
        max_c = max((z["temp_c"] for z in zones), default=None)
        return jsonify(zones=zones, max_c=max_c, cpu=_cpu_percent())
    except Exception as e:  # noqa: BLE001
        return jsonify(zones=[], max_c=None, cpu=None, error=repr(e))


# --------------------------------------------------------------------------
# Thermal logging: tegrastats -> a .tegrastats.log next to each recording, for
# the whole take. The service runs as root, so tegrastats needs no sudo here.
# --------------------------------------------------------------------------
_thermal = {"proc": None}


def _start_thermal_log(mkv_path):
    if shutil.which("tegrastats") is None:
        return
    log_path = os.path.splitext(mkv_path)[0] + ".tegrastats.log"
    try:
        _thermal["proc"] = subprocess.Popen(
            ["tegrastats", "--interval", str(THERMAL_INTERVAL_MS), "--logfile", log_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        _thermal["proc"] = None


def _stop_thermal_log():
    subprocess.run(["tegrastats", "--stop"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=False)
    p = _thermal.get("proc")
    if p is not None:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
    _thermal["proc"] = None


# --------------------------------------------------------------------------
# File management: browse /mnt/video + the external SSD, mount/unmount,
# transfer (rsync + byte-size verify), and delete. Camera-agnostic; ported from
# the Arducam app. Transfer/delete are blocked while recording.
# --------------------------------------------------------------------------
def _is_running():
    with _rec_lock:
        return _rec["active"]


def _human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _run_ok(cmd, timeout=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return False, repr(e)


def _storage_info(path=OUTPUT_DIR):
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        return {"total": _human(total), "free": _human(free), "used": _human(used),
                "pct": round(100 * used / total) if total else 0, "free_bytes": free}
    except OSError:
        return None


def _ssd_dev():
    return f"/dev/disk/by-uuid/{SSD_UUID}"


def _ssd_present():
    return os.path.exists(_ssd_dev())


def _ssd_mounted():
    return os.path.ismount(USB_MNT)


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


LOCS = {"video": OUTPUT_DIR, "ssd": USB_MNT}
_HIDDEN_NAMES = {"$RECYCLE.BIN", "System Volume Information", "lost+found", "found.000"}
_xfer = {"active": False, "percent": 0, "line": "", "done": False,
         "ok": None, "error": None, "warning": None, "names": []}
_xfer_lock = threading.Lock()


def _is_hidden(name):
    return name.startswith(".") or name in _HIDDEN_NAMES


def _safe_join(root, rel):
    rel = (rel or "").strip("/")
    p = os.path.realpath(os.path.join(root, rel))
    root_r = os.path.realpath(root)
    return p if (p == root_r or p.startswith(root_r + os.sep)) else None


def _entry(loc, abspath, rel):
    is_dir = os.path.isdir(abspath)
    e = {"name": os.path.basename(abspath), "path": rel, "is_dir": is_dir,
         "size": None, "size_h": "", "mtime": None, "date": "",
         "on_ssd": None, "match": None}
    try:
        st = os.stat(abspath)
        e["mtime"] = st.st_mtime
        e["date"] = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        if not is_dir:
            e["size"] = st.st_size
            e["size_h"] = _human(st.st_size)
    except OSError:
        pass
    if is_dir:
        return e
    if loc == "video" and _ssd_mounted():
        dp = os.path.join(USB_MNT, USB_SUBDIR, rel)
        if os.path.exists(dp):
            e["on_ssd"] = True
            try:
                e["match"] = (os.path.getsize(dp) == e["size"])
            except OSError:
                e["match"] = False
        else:
            e["on_ssd"] = False
            e["match"] = False
    return e


@app.route("/files")
def files_page():
    return FILES_PAGE


@app.route("/api/browse")
def api_browse():
    loc = request.args.get("loc", "video")
    rel = request.args.get("path", "")
    q = (request.args.get("q") or "").strip()
    show_hidden = request.args.get("hidden") == "1"
    resp = {"loc": loc, "path": "", "parent": None, "entries": [], "search": bool(q),
            "truncated": False, "recording": _is_running(), "ssd": _ssd_info(),
            "storage": _storage_info(), "error": None}
    root = LOCS.get(loc)
    if root is None:
        resp["error"] = "unknown location"
        return jsonify(resp)
    if loc == "ssd" and not _ssd_mounted():
        resp["error"] = "SSD not mounted."
        return jsonify(resp)
    base = _safe_join(root, rel)
    if base is None or not os.path.isdir(base):
        resp["error"] = "folder not found"
        return jsonify(resp)
    root_r = os.path.realpath(root)
    cur = "" if base == root_r else os.path.relpath(base, root_r)
    resp["path"] = cur
    resp["parent"] = None if cur == "" else os.path.dirname(cur)
    try:
        if q:
            n = 0
            for dp, dirs, files in os.walk(base):
                if not show_hidden:
                    dirs[:] = [d for d in dirs if not _is_hidden(d)]
                dirs.sort()
                for fn in sorted(files):
                    if not show_hidden and _is_hidden(fn):
                        continue
                    if q.lower() in fn.lower():
                        fp = os.path.join(dp, fn)
                        resp["entries"].append(_entry(loc, fp, os.path.relpath(fp, root_r)))
                        n += 1
                        if n >= 1000:
                            resp["truncated"] = True
                            return jsonify(resp)
        else:
            names = [n for n in os.listdir(base) if show_hidden or not _is_hidden(n)]
            items = [_entry(loc, os.path.join(base, name),
                            os.path.join(cur, name) if cur else name)
                     for name in names]
            items.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
            resp["entries"] = items
    except OSError as ex:  # noqa: BLE001
        resp["error"] = repr(ex)
    return jsonify(resp)


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
    with _xfer_lock:
        if _xfer["active"]:
            return jsonify(ok=False,
                           error="A transfer is in progress - wait for it to finish."), 409
    if not _ssd_mounted():
        return jsonify(ok=True, already=True)
    _run_ok(["sync"], timeout=30)
    ok, out = _run_ok(["umount", USB_MNT], timeout=30)
    if ok and not _ssd_mounted():
        return jsonify(ok=True)
    return jsonify(ok=False, error=f"unmount failed (drive in use?): {out or 'unknown'}")


def _transfer_worker(rels, dest):
    # --relative + OUTPUT_DIR/./<rel> recreates any subfolder layout under dest.
    # No -a: exFAT has no Unix owner/group/perms, so -a fails "rsync exit 23" even
    # when the data copies fine. Copy data + mtimes; the byte-size verify below is
    # the authoritative success check.
    srcs = [os.path.join(OUTPUT_DIR, ".", r) for r in rels]
    cmd = ["rsync", "-r", "--times", "--relative",
           "--no-perms", "--no-owner", "--no-group", "--omit-dir-times",
           "--info=progress2", "--"] + srcs + [dest + "/"]
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
        bad = [r for r in rels
               if not (os.path.exists(os.path.join(dest, r))
                       and os.path.getsize(os.path.join(dest, r))
                       == os.path.getsize(os.path.join(OUTPUT_DIR, r)))]
        with _xfer_lock:
            _xfer["done"] = True
            _xfer["active"] = False
            _xfer["ok"] = (not bad)                   # authoritative = byte-size verify
            _xfer["percent"] = 100 if not bad else _xfer["percent"]
            _xfer["error"] = f"verify failed for: {bad}" if bad else None
            _xfer["warning"] = (f"rsync exit {rc} (attributes only; data copied "
                                f"and byte-size verified)") if (rc != 0 and not bad) else None
    except Exception as e:  # noqa: BLE001
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
    paths = (request.get_json(silent=True) or {}).get("paths") or []
    rels = []
    for rel in paths:
        p = _safe_join(OUTPUT_DIR, rel)
        if p and os.path.isfile(p):
            rels.append((rel or "").strip("/"))
    if not rels:
        return jsonify(ok=False, error="No valid files selected."), 400
    dest = os.path.join(USB_MNT, USB_SUBDIR)
    os.makedirs(dest, exist_ok=True)
    with _xfer_lock:
        _xfer.update(active=True, percent=0, line="", done=False, ok=None, error=None,
                     warning=None, names=[os.path.basename(r) for r in rels])
    threading.Thread(target=_transfer_worker, args=(rels, dest), daemon=True).start()
    return jsonify(ok=True, count=len(rels))


@app.route("/api/transfer_status")
def api_transfer_status():
    with _xfer_lock:
        return jsonify(dict(_xfer))


@app.route("/api/delete", methods=["POST"])
def api_delete():
    if _is_running():
        return jsonify(ok=False, error="Stop recording before deleting."), 409
    data = request.get_json(silent=True) or {}
    loc = data.get("loc", "video")
    paths = data.get("paths") or []
    root = LOCS.get(loc)
    if root is None or (loc == "ssd" and not _ssd_mounted()):
        return jsonify(ok=False, error="bad location"), 400
    deleted, errors = [], []
    for rel in paths:
        p = _safe_join(root, rel)
        if p and os.path.isfile(p):
            try:
                os.remove(p)
                deleted.append(rel)
            except OSError as e:
                errors.append({"name": rel, "error": repr(e)})
        else:
            errors.append({"name": rel, "error": "not found"})
    return jsonify(ok=not errors, deleted=deleted, errors=errors)


FILES_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Manage Files</title>
<style>
 :root { color-scheme: dark; }
 body { font-family: system-ui, sans-serif; margin:0; background:#111; color:#eee; }
 .wrap { max-width:760px; margin:0 auto; padding:20px; }
 h1 { font-size:1.2rem; }
 a.back { color:#7fb2d9; text-decoration:none; font-size:.9rem; }
 .card { padding:14px; border-radius:10px; background:#1b1b1b; margin-bottom:16px; }
 .banner { background:#5a1f1f; color:#ffdede; padding:12px; border-radius:10px; margin-bottom:16px; }
 button { padding:10px 14px; font-size:1rem; font-weight:700; border:0; border-radius:10px;
          color:#fff; background:#2a6f9e; margin:4px 4px 4px 0; }
 button.danger { background:#b3271e; } button.go { background:#1f8a3b; } button.ghost { background:#333; }
 button:disabled { opacity:.35; }
 .tab { background:#333; } .tab.active { background:#2a6f9e; }
 input[type=text] { width:100%; box-sizing:border-box; background:#222; color:#eee;
                    border:1px solid #444; border-radius:8px; padding:10px; font-size:1rem; }
 table { width:100%; border-collapse:collapse; font-size:.92rem; }
 th,td { padding:6px 8px; border-bottom:1px solid #222; text-align:left; }
 td.sz { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
 td.dt, th.dt { white-space:nowrap; color:#bbb; font-variant-numeric:tabular-nums; }
 th.sort { cursor:pointer; user-select:none; } th.sort:hover { color:#fff; }
 .ok{color:#4caf50}.warn{color:#ffb300}.hot{color:#e33}.muted{color:#888;}
 .bar { height:14px; background:#333; border-radius:7px; overflow:hidden; }
 .bar > div { height:100%; width:0; background:#1f8a3b; transition:width .3s; }
 .name { word-break:break-all; }
 .dir .name { color:#7fb2d9; cursor:pointer; }
 #crumbs a { color:#7fb2d9; text-decoration:none; } #crumbs { margin-bottom:8px; }
</style></head><body><div class="wrap">
 <h1>&#128193; Manage Files &nbsp;<a class="back" href="/">&larr; back to recorder</a></h1>
 <div id="recbanner" class="banner" style="display:none">Recording in progress - transfer
   and delete are disabled to protect the recording. You can still browse and mount.</div>

 <div class="card">
   <div id="nvme" class="muted" style="margin-bottom:12px"></div>
   <button class="tab" id="tab-video" onclick="switchLoc('video')">Orin recordings</button>
   <button class="tab" id="tab-ssd" onclick="switchLoc('ssd')">External SSD</button>
   <div id="ssd" class="muted" style="margin-top:10px">&hellip;</div>
   <div id="ssdbtn" style="margin-top:8px"></div>
 </div>

 <div class="card" id="xfercard" style="display:none">
   <div>Transferring&hellip; <span id="xferpct">0%</span></div>
   <div class="bar"><div id="xferbar"></div></div>
   <div id="xferline" class="muted" style="margin-top:6px"></div>
 </div>

 <div class="card">
   <button class="go" id="btnxfer" onclick="transfer()">&#8681; Transfer &rarr; SSD</button>
   <button class="danger" id="btndel" onclick="del()">&#128465; Delete</button>
   <button class="ghost" onclick="load()">&#8635; Refresh</button>
   <div class="muted" id="selnote" style="margin-top:8px"></div>
 </div>

 <div class="card">
   <input type="text" id="search" placeholder="Search this drive (recursive)&hellip;" oninput="onSearch()">
   <label class="muted" style="display:block; margin:10px 0">
     <input type="checkbox" id="hidden" onchange="load()"> Show hidden / system files</label>
   <div id="crumbs"></div>
   <table>
     <thead><tr>
       <th><input type="checkbox" id="all" onchange="toggleAll()"></th>
       <th class="sort" data-sort="name">Name</th>
       <th class="sort sz" data-sort="size">Size</th>
       <th class="sort dt" data-sort="date">Date</th>
       <th class="sort" id="hssd" data-sort="ssd">On SSD</th>
     </tr></thead>
     <tbody id="rows"></tbody>
   </table>
   <div id="note" class="muted" style="margin-top:8px"></div>
 </div>
</div>
<script>
const $ = id => document.getElementById(id);
const post = (u,b) => fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},
                              body:JSON.stringify(b||{})}).then(r=>r.json());
const esc = s => (''+s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
function storageHtml(st){
  if(!st) return '';
  const gb = st.free_bytes/1e9, cls = gb<40?'hot':gb<150?'warn':'ok';
  return '&#128190; NVMe /mnt/video: <b>'+st.used+'</b> / '+st.total+' used ('+st.pct+'%) &middot; '
       + '<span class="'+cls+'">'+st.free+' free</span>';
}

let state = {loc:'video', path:'', q:'', parent:null, autoSub:false};
let mounted=false, recording=false, xferActive=false;
let lastData = null, sort = {key:'name', asc:true};

function selected(){ return [...document.querySelectorAll('.sel:checked')].map(c=>c.dataset.path); }
function toggleAll(){ document.querySelectorAll('.sel').forEach(c=>c.checked=$('all').checked); updateSel(); }
function updateSel(){
  const n=selected().length;
  $('selnote').textContent = n ? (n+' selected')
    : 'Tap a folder to open it. Tick files, then Transfer or Delete.';
  $('btnxfer').disabled = recording || !mounted || state.loc!=='video' || !n || xferActive;
  $('btndel').disabled  = recording || !n || xferActive;
}
function ssdCell(f){
  if(state.loc!=='video' || !mounted) return '';
  if(f.match) return '<span class="ok">&#10003; verified</span>';
  if(f.on_ssd) return '<span class="warn">&#9888; size differs</span>';
  return '<span class="muted">not copied</span>';
}
function crumbs(){
  const rootLabel = state.loc==='ssd' ? 'SSD' : 'Recordings';
  let html = '<a href="#" data-crumb="">'+rootLabel+'</a>', acc='';
  if(state.path) state.path.split('/').forEach(seg => {
    acc = acc ? acc+'/'+seg : seg;
    html += ' / <a href="#" data-crumb="'+esc(acc)+'">'+esc(seg)+'</a>';
  });
  return html;
}

async function load(){
  const u = '/api/browse?loc='+encodeURIComponent(state.loc)
          + '&path='+encodeURIComponent(state.path)
          + '&q='+encodeURIComponent(state.q)
          + '&hidden='+($('hidden').checked?'1':'0');
  const s = await (await fetch(u)).json();
  if(state.autoSub){
    state.autoSub = false;
    if(!s.error && !state.path && (s.entries||[]).some(e => e.is_dir && e.name==='orin-video')){
      state.path = 'orin-video'; return load();
    }
  }
  recording = s.recording; mounted = s.ssd.mounted; state.parent = s.parent;
  $('nvme').innerHTML = storageHtml(s.storage);
  $('recbanner').style.display = recording ? 'block' : 'none';
  $('tab-video').className = 'tab' + (state.loc==='video'?' active':'');
  $('tab-ssd').className   = 'tab' + (state.loc==='ssd'?' active':'');

  let html, btn='';
  if(!s.ssd.present){ html='No SSD detected - plug it in, then Refresh.'; }
  else if(!s.ssd.mounted){ html='SSD detected, not mounted.';
    btn='<button class="go" onclick="mount()">Mount SSD</button>'; }
  else { html='SSD mounted at '+s.ssd.mountpoint+' - free '+s.ssd.free+' of '+s.ssd.total
             +' (transfers land in '+s.ssd.subdir+'/)';
    btn='<button id="btnunmount" onclick="unmount()"'+(xferActive?' disabled':'')
        +'>Unmount SSD</button>'; }
  $('ssd').innerHTML = html; $('ssdbtn').innerHTML = btn;

  $('crumbs').innerHTML = (s.search || s.error) ? '' : crumbs();

  lastData = s;
  $('note').textContent = s.truncated ? 'Showing first 1000 matches - narrow your search.' : '';
  renderRows();
}

function ssdRank(f){ return f.match ? 3 : f.on_ssd ? 2 : (f.on_ssd===false ? 1 : 0); }
function cmp(a,b){
  let av, bv;
  if(sort.key==='size'){ av=a.size||0; bv=b.size||0; }
  else if(sort.key==='date'){ av=a.mtime||0; bv=b.mtime||0; }
  else if(sort.key==='ssd'){ av=ssdRank(a); bv=ssdRank(b); }
  else { av=(a.path||a.name||'').toLowerCase(); bv=(b.path||b.name||'').toLowerCase(); }
  const d = av<bv ? -1 : av>bv ? 1 : 0;
  return sort.asc ? d : -d;
}
function rowHtml(f, search){
  if(f.is_dir) return '<tr class="dir" data-goto="'+esc(f.path)+'"><td></td>'
    + '<td class="name">&#128193; '+esc(f.name)+'</td><td></td>'
    + '<td class="dt">'+esc(f.date||'')+'</td><td></td></tr>';
  const label = search ? f.path : f.name;
  return '<tr><td><input type="checkbox" class="sel" data-path="'+esc(f.path)+'"></td>'
    + '<td class="name">'+esc(label)+'</td><td class="sz">'+f.size_h+'</td>'
    + '<td class="dt">'+esc(f.date||'')+'</td><td>'+ssdCell(f)+'</td></tr>';
}
function renderRows(){
  const s = lastData; if(!s) return;
  const labels = {name:'Name', size:'Size', date:'Date', ssd:(s.loc==='video'?'On SSD':'')};
  document.querySelectorAll('th[data-sort]').forEach(th => {
    const k = th.dataset.sort;
    th.textContent = labels[k] + (sort.key===k && labels[k] ? (sort.asc?' \\u25B2':' \\u25BC') : '');
  });
  if(s.error){ $('rows').innerHTML='<tr><td colspan="5" class="muted">'+esc(s.error)+'</td></tr>';
    $('all').checked=false; updateSel(); return; }
  const dirs = s.entries.filter(e=>e.is_dir).sort((a,b)=>a.name.toLowerCase()<b.name.toLowerCase()?-1:1);
  const files = s.entries.filter(e=>!e.is_dir).slice().sort(cmp);
  const list = s.search ? files : dirs.concat(files);
  if(!list.length){ $('rows').innerHTML='<tr><td colspan="5" class="muted">'
      + (s.search?'No matches.':'Empty folder.')+'</td></tr>'; $('all').checked=false; updateSel(); return; }
  const up = (!s.search && state.parent!==null)
    ? '<tr class="dir" data-goto="'+esc(state.parent)+'"><td></td>'
      +'<td class="name">&#128193; ..</td><td></td><td></td><td></td></tr>' : '';
  $('rows').innerHTML = up + list.map(f => rowHtml(f, s.search)).join('');
  $('all').checked=false;
  updateSel();
}

$('rows').addEventListener('click', e => {
  const tr = e.target.closest('tr.dir'); if(!tr) return;
  state.path = tr.dataset.goto; state.q=''; $('search').value=''; load();
});
$('rows').addEventListener('change', e => { if(e.target.classList.contains('sel')) updateSel(); });
$('crumbs').addEventListener('click', e => {
  const a = e.target.closest('a[data-crumb]'); if(!a) return;
  e.preventDefault(); state.path = a.dataset.crumb; state.q=''; $('search').value=''; load();
});
document.querySelector('thead').addEventListener('click', e => {
  const th = e.target.closest('th[data-sort]'); if(!th) return;
  const k = th.dataset.sort;
  if(sort.key===k) sort.asc = !sort.asc; else { sort.key=k; sort.asc=true; }
  renderRows();
});

function switchLoc(loc){
  state.loc=loc; state.path=''; state.q=''; $('search').value='';
  state.autoSub = (loc==='ssd');
  load();
}
let searchTimer;
function onSearch(){ clearTimeout(searchTimer);
  searchTimer=setTimeout(()=>{ state.q=$('search').value.trim(); load(); }, 300); }

async function mount(){ const r=await post('/api/mount'); if(!r.ok) alert(r.error||'mount failed'); load(); }
async function unmount(){ const r=await post('/api/unmount'); if(!r.ok) alert(r.error||'unmount failed'); load(); }

async function transfer(){
  const paths = selected(); if(!paths.length) return;
  const r = await post('/api/transfer', {paths});
  if(!r.ok){ alert(r.error||'transfer failed'); return; }
  xferActive=true; $('xfercard').style.display='block';
  $('btnxfer').disabled=true; $('btndel').disabled=true;
  if($('btnunmount')) $('btnunmount').disabled=true;
  const t = setInterval(async () => {
    const s = await (await fetch('/api/transfer_status')).json();
    $('xferpct').textContent = s.percent+'%'; $('xferbar').style.width = s.percent+'%';
    $('xferline').textContent = s.line||'';
    if(s.done){ clearInterval(t); xferActive=false; $('xfercard').style.display='none';
      if(!s.ok) alert('Transfer error: '+(s.error||'unknown')); load(); }
  }, 1000);
}

async function del(){
  const paths = selected(); if(!paths.length) return;
  const where = state.loc==='ssd' ? 'the SSD' : 'the Orin';
  if(!confirm('Delete '+paths.length+' file(s) from '+where+'? This cannot be undone.\\n\\n'
              +'(Verify copies first - deletes are permanent.)')) return;
  const r = await post('/api/delete', {loc: state.loc, paths});
  if(!r.ok) alert('Some files could not be deleted:\\n'+JSON.stringify(r.errors,null,1));
  load();
}

load();
setInterval(() => { if(!xferActive && !state.q && !selected().length) load(); }, 5000);
</script></body></html>"""


def _graceful_stop(signum, _frame):
    """SIGTERM/SIGINT -> release the Argus camera cleanly, then exit.

    Without this, a signal kill skips cleanup and leaves the Argus session
    dangling, which can wedge nvargus-daemon so the next start fails. With it,
    `systemctl restart` (which sends SIGTERM) is all that's ever needed.
    """
    log(f"signal {signum}: finalizing + releasing camera")
    try:
        if _is_running():
            stop_recording()
            time.sleep(2)                # let the record branch EOS-finalize the MKV
    except Exception:                    # noqa: BLE001
        pass
    _stop_thermal_log()
    pipeline.set_state(Gst.State.NULL)   # releases the Argus session cleanly
    os._exit(0)


def main():
    signal.signal(signal.SIGTERM, _graceful_stop)
    signal.signal(signal.SIGINT, _graceful_stop)
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_msg(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log(f"GST ERROR: {err.message} :: {dbg}")
        elif msg.type == Gst.MessageType.EOS:
            log("GST EOS")

    bus.connect("message", on_msg)
    threading.Thread(target=loop.run, daemon=True).start()

    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        log("ERROR: pipeline failed to start. Are both Pi HQ cameras enumerated "
            "(`sudo camswitch pi`), and is nothing else holding the cameras?")
        return
    log(f"Pi HQ app: http://0.0.0.0:{PORT}  (live preview + record)")
    try:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    finally:
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()
