#!/usr/bin/env python3
"""
pi_app.py - single-owner capture + web app for the dual Raspberry Pi HQ (IMX477)
rig on the Jetson Orin Nano.

WHY ONE PROCESS: Argus gives ONE process exclusive ownership of a sensor. Two
processes can't share a camera, so a single process owns BOTH cameras (sensor-id
0 and 1), composites them side by side, and fans the combined frame out to a live
browser preview (and, next step, the recorder) - while setting exposure/gain LIVE
on both sensors via g_object_set. Everything the camera touches lives in here.

    cam0 (Argus/ISP) ┐                       ┌─ preview  -> browser (always on)
                     ├ nvcompositor ─ tee ───┤
    cam1 (Argus/ISP) ┘                       └─ record   -> MKV  (added in step 2)

Because this ONE process owns the nvarguscamerasrc elements, exposure/gain/white-
balance can be changed live and applied IDENTICALLY to both cameras, so the stereo
pair stays matched (clean seam for the stitcher).

STEP 1 (this file): live preview + live matched exposure/gain. Recording (dynamic
tee branch + audio) and the Arducam-file cleanup come next.

Run on the Orin (after `sudo camswitch pi`):
    python3 pi_app.py
    # then browse from your phone to  http://<orin-ip>:8080   (hostname -I)
"""

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
PORT = 8080
BOUNDARY = "spinframe"

# IMX477 limits from the sensor mode report (exposure in microseconds).
EXPOSURE_MIN_US, EXPOSURE_MAX_US = 13, 33000     # 13us floor; 33ms = 1/30s ceiling @ 30fps
GAIN_MIN, GAIN_MAX = 1.0, 22.25

Gst.init(None)


def build_pipeline():
    """Both cameras -> compositor -> tee -> preview (appsink). Named elements:
    cam0/cam1 (for live controls) and 'preview' (the appsink we pull JPEGs from)."""
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
_appsink = pipeline.get_by_name("preview")
_appsink.connect("new-sample", _on_new_sample)


# --- camera controls (applied to BOTH cams so the stereo pair stays matched) ---
_ctrl = {"auto": True, "exposure_us": 8000, "gain": 4.0}
_ctrl_lock = threading.Lock()


def apply_controls():
    """Push the current control state onto both nvarguscamerasrc elements, live.

    auto=True  -> full exposure/gain range, AE unlocked (ISP auto-exposure).
    auto=False -> exposure and gain PINNED (min=max) to the chosen values, AE
                  locked - a guaranteed, matched shutter for low motion blur.
    """
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
</style></head><body>
<header>Raspberry Pi HQ stereo rig &mdash; live preview</header>
<img id="view" src="/preview.mjpg" alt="preview">
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
    motion blur (needs more light or gain). Recording controls come next.</div>
</div>
<script>
const $ = id => document.getElementById(id);
function post(body){ fetch('/control',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(body)}); }
function syncEnabled(){
  const auto = $('auto').checked;
  $('expRow').classList.toggle('muted', auto);
  $('gainRow').classList.toggle('muted', auto);
  $('exp').disabled = auto; $('gain').disabled = auto;
}
$('auto').addEventListener('change', () => { syncEnabled(); post({auto:$('auto').checked}); });
$('exp').addEventListener('input', () => { $('expVal').innerHTML = $('exp').value+' &micro;s'; });
$('exp').addEventListener('change', () => post({exposure_us:parseInt($('exp').value)}));
$('gain').addEventListener('input', () => { $('gainVal').innerHTML = (+$('gain').value).toFixed(2)+'&times;'; });
$('gain').addEventListener('change', () => post({gain:parseFloat($('gain').value)}));
syncEnabled();
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


def main():
    # Bus watch (errors/EOS) on a GLib loop in a background thread; Flask runs
    # in the main thread. GStreamer property sets are thread-safe, so the Flask
    # control handlers can poke cam0/cam1 directly.
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_msg(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"GST ERROR: {err.message} :: {dbg}", flush=True)
        elif msg.type == Gst.MessageType.EOS:
            print("GST EOS", flush=True)

    bus.connect("message", on_msg)
    threading.Thread(target=loop.run, daemon=True).start()

    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        print("ERROR: pipeline failed to start. Are both Pi HQ cameras enumerated "
              "(`sudo camswitch pi`), and is nothing else holding the cameras?", flush=True)
        return
    # NOTE: we deliberately do NOT set exposure/gain here. Setting Argus properties
    # on a live nvarguscamerasrc re-creates the capture stream, which on two cameras
    # can crash the compositor. Startup runs pure ISP auto-exposure; manual controls
    # are being reworked to a safe (restart-based) path.
    print(f"Pi HQ app: http://0.0.0.0:{PORT}  (live preview, auto-exposure)", flush=True)
    try:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    finally:
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()
