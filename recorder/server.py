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
import os
import threading

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
  } catch(e){}
}
$('recBtn').addEventListener('click', async () => {
  $('recBtn').disabled = true;
  try { await post(recording ? '/stop' : '/start'); } finally { await refreshStatus(); $('recBtn').disabled = false; }
});
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
    return jsonify(recording=active, file=os.path.basename(path) if (active and path) else None)


def main():
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
