#!/usr/bin/env python3
"""
calib_server.py - tiny calibration panel for the ROCK 5T rig.

Live preview + a Snapshot button, for walking a ChArUco board around each
camera's frame. Runs on the Rock, viewed from any browser on the LAN (phone
works). Python stdlib only.

    python3 calib_server.py            # then browse to http://<rock-ip>:8081

How it works around the hardware:
  - PREVIEW comes from the ISP *selfpath* (video23/video32) at 960x540 5fps
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
PREV_JPG = "/dev/shm/calib_prev.jpg"
CAMS = {
    "0": {"main": "/dev/video22", "self": "/dev/video23", "dir": os.path.expanduser("~/calib0")},
    "1": {"main": "/dev/video31", "self": "/dev/video32", "dir": os.path.expanduser("~/calib1")},
}

_state = {"cam": "0", "gst": None, "lock": threading.Lock(), "snapping": False}

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
<div>
<button class=cam id=c0 onclick="cam('0')">Camera 0</button>
<button class=cam id=c1 onclick="cam('1')">Camera 1</button>
</div>
<img id=prev src="/preview.mjpg">
<div><button id=snap onclick="snap()">&#128247; SNAPSHOT</button></div>
<div id=msg></div>
<script>
function refresh(){fetch('/status').then(r=>r.json()).then(s=>{
  document.getElementById('c0').className='cam'+(s.cam=='0'?' on':'');
  document.getElementById('c1').className='cam'+(s.cam=='1'?' on':'');
  document.getElementById('msg').textContent='cam'+s.cam+': '+s.count+' snapshots in '+s.dir;});}
function cam(c){fetch('/cam?c='+c).then(()=>{
  document.getElementById('prev').src='/preview.mjpg?'+Date.now();refresh();});}
function snap(){var b=document.getElementById('snap');b.disabled=true;
  b.textContent='capturing...';
  fetch('/snap').then(r=>r.json()).then(s=>{
    b.disabled=false;b.textContent='\\ud83d\\udcf7 SNAPSHOT';
    document.getElementById('msg').textContent=(s.ok?('saved '+s.file+'  (total '+s.count+')'):('ERROR: '+s.error));});}
refresh();
</script></body></html>"""


def sh(cmd, timeout=30):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def start_preview(cam):
    stop_preview()
    c = CAMS[cam]
    # clear any stale selfpath crop window (rkisp keeps the last selection)
    sh(f"v4l2-ctl -d {c['self']} --set-selection target=crop,top=0,left=0,width=3840,height=2160")
    try:
        os.remove(PREV_JPG)
    except FileNotFoundError:
        pass
    pipeline = (f"gst-launch-1.0 v4l2src device={c['self']} ! "
                f"video/x-raw,format=NV12,width=960,height=540 ! videorate ! "
                f"video/x-raw,framerate=5/1 ! jpegenc quality=80 ! "
                f"multifilesink location={PREV_JPG}")
    _state["gst"] = subprocess.Popen(pipeline, shell=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_preview():
    p = _state.get("gst")
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
    _state["gst"] = None


def snap_count(cam):
    d = CAMS[cam]["dir"]
    try:
        return len([f for f in os.listdir(d) if f.endswith(".png")])
    except FileNotFoundError:
        return 0


def take_snapshot(cam):
    c = CAMS[cam]
    os.makedirs(c["dir"], exist_ok=True)
    idx = snap_count(cam)
    out = os.path.join(c["dir"], f"img_{idx:03d}.png")
    r = sh(f"v4l2-ctl -d {c['main']} --set-fmt-video=width=3840,height=2160,pixelformat=NV12 "
           f"--stream-mmap --stream-count=45 --stream-to=/tmp/calib_snap.nv12", timeout=20)
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "capture failed").strip()[:200]}
    r = sh(f"ffmpeg -loglevel error -f rawvideo -pix_fmt nv12 -s 3840x2160 "
           f"-i /tmp/calib_snap.nv12 -update 1 -y {out}", timeout=30)
    if r.returncode != 0 or not os.path.exists(out):
        return {"ok": False, "error": (r.stderr or "png convert failed").strip()[:200]}
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
            cam = _state["cam"]
            self._json({"cam": cam, "count": snap_count(cam), "dir": CAMS[cam]["dir"]})
        elif path == "/cam":
            cam = self.path.split("c=")[-1][:1]
            if cam in CAMS:
                with _state["lock"]:
                    _state["cam"] = cam
                    start_preview(cam)
            self._json({"ok": True})
        elif path == "/snap":
            with _state["lock"]:
                res = take_snapshot(_state["cam"])
            self._json(res)
        elif path == "/preview.mjpg":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    try:
                        with open(PREV_JPG, "rb") as f:
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
    start_preview(_state["cam"])
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
        stop_preview()


if __name__ == "__main__":
    main()
