#!/usr/bin/env python3
"""
calib_server.py - tiny calibration panel for the ROCK 5T rig.

Live preview + a Snapshot button, for walking a ChArUco board around each
camera's frame. Runs on the Rock, viewed from any browser on the LAN (phone
works). Python stdlib only.

    python3 calib_server.py            # then browse to http://<rock-ip>:8081

How it works around the hardware:
  - PREVIEW comes from the ISP *selfpath* (video23/video32) at 1920x1080 5fps
    (the ONLY selfpath size proven safe on this vendor stack - 720p suspected
    in a lockup 2026-09-18)
    JPEG -> /dev/shm, served as MJPEG. The selfpath can hold a stale crop
    window (found 2026-09-13), so its crop selection is reset to the full
    3840x2160 input every time the preview starts.
  - SNAPSHOTS come from the *mainpath* (video22/video31) at full 3840x2160 -
    a different device node, so the preview keeps running while you snap.
  - Files land in ~/calib0 / ~/calib1 as img_NNN.png, matching calibrate.py's
    --single workflow (see snap_pair.sh header for the rig/extrinsics flow).
"""

import http.server
import json
import os
import socket
import socketserver
import subprocess
import threading
import time

PORT = 8081
PREV_JPG = {"0": "/dev/shm/calib_prev0.jpg", "1": "/dev/shm/calib_prev1.jpg"}
FULL_JPG = {"0": "/dev/shm/calib_full0.jpg", "1": "/dev/shm/calib_full1.jpg"}
CAMS = {
    "0": {"main": "/dev/video22", "self": "/dev/video23", "dir": os.path.expanduser("~/calib0")},
    "1": {"main": "/dev/video31", "self": "/dev/video32", "dir": os.path.expanduser("~/calib1")},
}

_state = {"gst": {}, "lock": threading.Lock()}

PAGE = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>Rig Calibration</title><style>
body{font-family:sans-serif;background:#111;color:#eee;margin:0;text-align:center}
img{width:100%;max-width:960px;background:#000}
button{font-size:1.4em;padding:.6em 1.2em;margin:.4em;border-radius:.5em;border:0}
#snap{background:#2a2;color:#fff;font-size:2em;padding:.8em 2em}
#snap:disabled{background:#555}
.cam{background:#444;color:#fff}.cam.on{background:#06c}
#msg{min-height:1.5em;color:#8f8}
</style></head><body>
<h3>Rig Calibration Panel</h3>
<div>cam0</div><img src="/preview0.mjpg">
<div>cam1</div><img src="/preview1.mjpg">
<div><button id=snap onclick="snap()">&#128247; SNAPSHOT BOTH</button></div>
<div id=msg></div>
<script>
function refresh(){fetch('/status').then(r=>r.json()).then(s=>{
  document.getElementById('msg').textContent='cam0: '+s.count0+'  |  cam1: '+s.count1+' snapshots';});}
function snap(){var b=document.getElementById('snap');b.disabled=true;
  b.textContent='capturing...';
  fetch('/snap').then(r=>r.json()).then(s=>{
    b.disabled=false;b.textContent='\\ud83d\\udcf7 SNAPSHOT BOTH';
    document.getElementById('msg').textContent=(s.ok?('saved '+s.file+'  (total '+s.count+')'):('ERROR: '+s.error));});}
refresh();
</script></body></html>"""


def sh(cmd, timeout=30):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def start_previews():
    """Start BOTH cameras' preview pipelines once, at server start. They are
    never stopped/restarted while the server runs: this vendor stack dislikes
    pipeline teardown churn (dphy rebind oops, selfpath stale crops, a lockup
    blamed on the old cam-switcher), so the design has no switching at all."""
    for cam, c in CAMS.items():
        sh(f"v4l2-ctl -d {c['self']} --set-selection target=crop,top=0,left=0,width=3840,height=2160")
        try:
            os.remove(PREV_JPG[cam])
        except FileNotFoundError:
            pass
        pipeline = (f"gst-launch-1.0 v4l2src device={c['self']} ! "
                    f"video/x-raw,format=NV12,width=1920,height=1080 ! videorate ! "
                    f"video/x-raw,framerate=5/1 ! jpegenc quality=80 ! "
                    f"multifilesink location={PREV_JPG[cam]}")
        log = open(f"/tmp/calib_gst{cam}.log", "w")
        _state["gst"][cam] = subprocess.Popen(pipeline, shell=True, stdout=log, stderr=log)
        # full-res grabber: latest 4K frame always fresh in /dev/shm, so a
        # snapshot is just a file copy (instant) - no per-tap pipeline starts
        full = (f"gst-launch-1.0 v4l2src device={c['main']} ! "
                f"video/x-raw,format=NV12,width=3840,height=2160 ! videorate ! "
                f"video/x-raw,framerate=2/1 ! jpegenc quality=97 ! "
                f"multifilesink location={FULL_JPG[cam]}")
        logf = open(f"/tmp/calib_gst_full{cam}.log", "w")
        _state["gst"][cam + "f"] = subprocess.Popen(full, shell=True, stdout=logf, stderr=logf)


def stop_previews():
    for p in _state["gst"].values():
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
    _state["gst"] = {}


def snap_count(cam):
    d = CAMS[cam]["dir"]
    try:
        return len([f for f in os.listdir(d) if f.endswith((".png", ".jpg"))])
    except FileNotFoundError:
        return 0


def take_snapshot(cam):
    """Copy the grabber's latest full-res frame - effectively instant."""
    c = CAMS[cam]
    src = FULL_JPG[cam]
    try:
        age = time.time() - os.path.getmtime(src)
    except FileNotFoundError:
        return {"ok": False, "error": f"no full-res frame yet from cam{cam} grabber"}
    if age > 3:
        return {"ok": False, "error": f"cam{cam} full-res frame is stale ({age:.0f}s) - grabber dead? see /tmp/calib_gst_full{cam}.log"}
    os.makedirs(c["dir"], exist_ok=True)
    idx = snap_count(cam)
    out = os.path.join(c["dir"], f"img_{idx:03d}.jpg")
    with open(src, "rb") as f:
        data = f.read()
    with open(out, "wb") as f:
        f.write(data)
    return {"ok": True, "file": os.path.basename(out), "count": snap_count(cam)}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/status":
            self._json({"count0": snap_count("0"), "count1": snap_count("1")})
        elif path == "/snap":
            with _state["lock"]:
                res = {}
                for cam in CAMS:
                    r = take_snapshot(cam)
                    res["cam" + cam] = r
                    if not r.get("ok"):
                        break
                ok = all(v.get("ok") for v in res.values())
                res = {"ok": ok,
                       "file": " + ".join(v.get("file", "?") for v in res.values()),
                       "count": max((v.get("count", 0) for v in res.values()), default=0),
                       "error": "; ".join(v.get("error", "") for v in res.values() if v.get("error"))}
            self._json(res)
        elif path in ("/preview0.mjpg", "/preview1.mjpg"):
            jpg_path = PREV_JPG[path[8]]
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    try:
                        with open(jpg_path, "rb") as f:
                            jpg = f.read()
                    except FileNotFoundError:
                        jpg = b""
                    if jpg:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                         + f"Content-Length: {len(jpg)}\r\n\r\n".encode()
                                         + jpg + b"\r\n")
                    time.sleep(0.25)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_error(404)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    start_previews()
    ip = "unknown"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    print(f"Calibration panel:  http://{ip}:{PORT}   (Ctrl-C to stop)")
    try:
        Server(("0.0.0.0", PORT), Handler).serve_forever()
    finally:
        stop_previews()


if __name__ == "__main__":
    main()
