#!/usr/bin/env python3
"""Single-camera, full-resolution focus preview.

Opens ONE sensor at the driver's full-pixel mode (3840x2160, mode 0 - the
stock nv_imx477 driver exposes no larger readout) and serves it as an MJPEG
stream displayed 1:1 in the browser (scroll to pan; click the image to toggle
fit-to-window). Low fps keeps full-res frames flowing over WiFi.

The recorder service owns BOTH sensors, so stop it first:

    sudo systemctl stop camera-rig
    CAM=1 python3 ~/orin-recorder/recorder/focus_cam.py

then open http://<orin>:8081. Restart the rig when done:

    sudo systemctl start camera-rig

Env overrides: CAM (sensor-id, default 1), FPS (default 5), QUALITY (JPEG,
default 90), PORT (default 8081).
"""

import os
import threading

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

from flask import Flask, Response  # noqa: E402

CAM = int(os.environ.get("CAM", "1"))
FPS = int(os.environ.get("FPS", "5"))
QUALITY = int(os.environ.get("QUALITY", "90"))
PORT = int(os.environ.get("PORT", "8081"))
FULL_W, FULL_H, MODE_FPS = 3840, 2160, 30   # nv_imx477 mode 0 (full pixel readout)
BOUNDARY = "focusframe"

# Same ISP tuning the recorder bakes in (see server.py) so focus judgments
# match what recordings will look like.
ARGUS_TUNING = ('tnr-mode=2 tnr-strength=0.5 ee-mode=2 ee-strength=0.3 '
                'gainrange="1 8" ispdigitalgainrange="1 2" aeantibanding=3')

Gst.init(None)

DESC = (
    f"nvarguscamerasrc name=cam sensor-id={CAM} {ARGUS_TUNING} "
    f"! video/x-raw(memory:NVMM),width={FULL_W},height={FULL_H},framerate={MODE_FPS}/1 "
    f"! nvvidconv ! video/x-raw,format=I420 "
    f"! videorate drop-only=true ! video/x-raw,framerate={FPS}/1 "
    f"! nvjpegenc quality={QUALITY} "
    f"! appsink name=preview emit-signals=true max-buffers=1 drop=true"
)

pipeline = Gst.parse_launch(DESC)

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


pipeline.get_by_name("preview").connect("new-sample", _on_new_sample)

app = Flask(__name__)

PAGE = f"""<!doctype html><html><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Focus cam {CAM} ({FULL_W}x{FULL_H})</title>
<style>
  body {{ margin:0; background:#000; color:#ddd; font:14px system-ui; }}
  header {{ position:fixed; top:0; left:0; right:0; padding:6px 10px;
            background:rgba(0,0,0,.7); z-index:1; }}
  /* Default is 1:1 - no CSS sizing, so one sensor pixel = one screen pixel.
     Scroll to pan; click toggles fit-to-window. */
  #view.fit {{ max-width:100vw; max-height:100vh; }}
</style></head><body>
<header>cam {CAM} &middot; {FULL_W}&times;{FULL_H} @ {FPS}fps &middot;
        1:1 (scroll to pan) &mdash; click image to toggle fit</header>
<img id="view" src="/preview.mjpg" alt="focus preview">
<script>
  document.getElementById('view').addEventListener('click', function () {{
    this.classList.toggle('fit');
  }});
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


def main():
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_msg(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"GST ERROR: {err.message} :: {dbg}", flush=True)

    bus.connect("message", on_msg)
    threading.Thread(target=loop.run, daemon=True).start()

    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        print(f"ERROR: pipeline failed to start. Is the recorder stopped "
              f"(`sudo systemctl stop camera-rig`) and sensor {CAM} enumerated?",
              flush=True)
        return
    print(f"Focus preview (cam {CAM}, {FULL_W}x{FULL_H}): http://0.0.0.0:{PORT}",
          flush=True)
    try:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    finally:
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()
