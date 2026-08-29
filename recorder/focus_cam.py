#!/usr/bin/env python3
"""Single-camera, full-resolution focus preview.

Opens ONE sensor at the driver's full-pixel mode (3840x2160, mode 0 - the
stock nv_imx477 driver exposes no larger readout) and serves it as an MJPEG
stream displayed 1:1 in the browser (scroll to pan; click the image to toggle
fit-to-window). Low fps keeps full-res frames flowing over WiFi.

MONITOR=1 additionally renders the live 30fps feed on the display plugged
into the Orin - launchable from SSH, no desktop session needed (it draws via
DRM/KMS). Add ZOOM=1 to show the center 1920x1080 of the 4K frame instead of
scaling it down: on a 1080p monitor that is true 1:1 sensor pixels, the most
honest focus check. If a desktop IS running it owns the display - either
`sudo systemctl stop gdm` first, or use SINK=3d to open an X window instead.

The recorder service owns BOTH sensors, so stop it first:

    sudo systemctl stop camera-rig
    MONITOR=1 ZOOM=1 CAM=1 python3 ~/orin-recorder/recorder/focus_cam.py

The browser stream at http://<orin>:8081 keeps working alongside the
monitor. Restart the rig when done:

    sudo systemctl start camera-rig

Env overrides: CAM (sensor-id, default 1), MONITOR (1 = also draw on the
attached display), ZOOM (1 = center 1080p crop, 1:1 pixels), SINK (drm
default, 3d = X11 window), FPS (browser stream fps, default 5), QUALITY
(JPEG, default 90), PORT (default 8081).
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
MONITOR = os.environ.get("MONITOR") == "1"
ZOOM = os.environ.get("ZOOM") == "1"
SINK = os.environ.get("SINK", "drm")        # drm = direct to display, 3d = X window
FULL_W, FULL_H, MODE_FPS = 3840, 2160, 30   # nv_imx477 mode 0 (full pixel readout)
BOUNDARY = "focusframe"

# Same ISP tuning the recorder bakes in (see server.py) so focus judgments
# match what recordings will look like.
ARGUS_TUNING = ('tnr-mode=2 tnr-strength=0.5 ee-mode=2 ee-strength=0.3 '
                'gainrange="1 8" ispdigitalgainrange="1 2" aeantibanding=3')

Gst.init(None)

def _monitor_branch():
    """Local display branch: full 30fps straight from the capture tee."""
    if SINK == "3d":
        # X11/XWayland window - needs a running desktop session.
        os.environ.setdefault("DISPLAY", ":0")
        sink = "nv3dsink sync=false"
    else:
        # Draws via DRM/KMS - works from SSH with no desktop running.
        sink = "nvdrmvideosink sync=false"
    if ZOOM:
        # Center 1920x1080 cut from the 4K frame: 1:1 sensor pixels on a
        # 1080p monitor. nvvidconv crop props are rectangle coordinates.
        left, top = (FULL_W - 1920) // 2, (FULL_H - 1080) // 2
        conv = (f"nvvidconv left={left} right={left + 1920} "
                f"top={top} bottom={top + 1080} "
                f"! video/x-raw(memory:NVMM),format=NV12,width=1920,height=1080")
    else:
        conv = "nvvidconv ! video/x-raw(memory:NVMM),format=NV12"
    return (f"t. ! queue leaky=downstream max-size-buffers=4 "
            f"! {conv} ! {sink} ")


DESC = (
    f"nvarguscamerasrc name=cam sensor-id={CAM} {ARGUS_TUNING} "
    f"! video/x-raw(memory:NVMM),width={FULL_W},height={FULL_H},framerate={MODE_FPS}/1 "
    f"! tee name=t "
    + (_monitor_branch() if MONITOR else "") +
    f"t. ! queue leaky=downstream max-size-buffers=4 "
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
            if MONITOR and SINK != "3d" and "not-negotiated" in (dbg or ""):
                print("HINT: not-negotiated on the monitor branch usually means "
                      "something else owns the display (desktop/login screen). "
                      "Check `systemctl is-active gdm`; if active: "
                      "`sudo systemctl stop gdm`, then rerun. Or use SINK=3d "
                      "to open a window on the running desktop instead.",
                      flush=True)

    bus.connect("message", on_msg)
    threading.Thread(target=loop.run, daemon=True).start()

    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        print(f"ERROR: pipeline failed to start. Is the recorder stopped "
              f"(`sudo systemctl stop camera-rig`) and sensor {CAM} enumerated?",
              flush=True)
        if MONITOR and SINK != "3d":
            print("If the GST error above is about DRM: a desktop session owns "
                  "the display - `sudo systemctl stop gdm` first, or retry "
                  "with SINK=3d.", flush=True)
        return
    if MONITOR:
        mode = "center 1080p crop, 1:1 pixels" if ZOOM else "scaled to fit"
        print(f"Monitor out: cam {CAM} on the attached display ({mode})",
              flush=True)
    print(f"Focus preview (cam {CAM}, {FULL_W}x{FULL_H}): http://0.0.0.0:{PORT}",
          flush=True)
    try:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    finally:
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()
