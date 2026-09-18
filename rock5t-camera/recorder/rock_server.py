#!/usr/bin/env python3
"""
rock_server.py - web control panel for the ROCK 5T stereo recorder.

The Rock-side successor to the Orin's recorder/server.py, rebuilt on this
rig's hard-won rules. Python stdlib only.

    sudo python3 rock_server.py        # sudo: AE toggle drives systemctl
    # browse to http://<rock-ip>:8080

Design rules (all learned the hard way on this stack):
  - Previews (both cams, side by side) run CONTINUOUSLY on the ISP selfpath
    at 1920x1080/5fps (the proven size) and are NEVER stopped/restarted -
    pipeline churn has caused lockups. Selfpath crop is reset at start
    (stale-crop gotcha).
  - Recording uses the mainpath - a different node, so previews keep
    running while recording. TWO independent files by design (fault
    isolation; the stitcher pairs by timestamp): recordings/<take>_cam0.mkv
    + _cam1.mkv. MKV so a crash mid-take still leaves readable files.
  - The record pipeline is the validated one: io-mode=dmabuf + videorate
    framerate stamp (without it mpph265enc silently ignores bps) + CBR +
    decoupling queue. Stop = SIGINT -> gst -e sends EOS -> finalized file.
  - Exposure: AE ON = rkaiq_3A runs (tuned daylight strategy). AE OFF =
    rkaiq_3A is stopped and manual exposure/gain sliders drive BOTH sensors
    identically via v4l2 (matched halves for the pano). Toggling back ON
    restarts rkaiq_3A.
  - Sensor mode: boot default is 4K30; this server never switches modes
    (mode churn = risk; change the boot default instead if ever needed).
"""

import glob
import http.server
import json
import os
import re
import signal
import socket
import socketserver
import subprocess
import threading
import time

PORT = 8080
REC_DIR = os.path.expanduser("~/recordings")
PREV_JPG = {"0": "/dev/shm/rec_prev0.jpg", "1": "/dev/shm/rec_prev1.jpg"}
CAMS = {
    "0": {"main": "/dev/video22", "self": "/dev/video23",
          "sensor": ("/dev/media0", "m00_b_imx477 3-001a")},
    "1": {"main": "/dev/video31", "self": "/dev/video32",
          "sensor": ("/dev/media1", "m01_b_imx477 4-001a")},
}
BITRATES = [12, 20, 28, 42]           # Mbit choices; 28 = rig target
LINE_TIME_US = 13.333                  # 4K30 mode: hts 11200 / 840MHz

_state = {
    "gst": {},                # continuous preview procs
    "rec": {},                # cam -> recording Popen
    "rec_name": None,
    "rec_started": None,
    "bps": 28,
    "lock": threading.Lock(),
    "subdev": {},             # cam -> /dev/v4l-subdevN
    "ctl_range": {},          # exposure/gain min/max
}

PAGE = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>Rock Recorder</title><style>
body{font-family:sans-serif;background:#111;color:#eee;margin:0;text-align:center}
.row{display:flex;flex-wrap:wrap;justify-content:center}
.row div{flex:1 1 320px;max-width:50%}
.row img{width:100%;background:#000}
button{font-size:1.2em;padding:.5em 1em;margin:.3em;border-radius:.5em;border:0}
#rec{background:#c22;color:#fff;font-size:1.8em;padding:.7em 2em}
#rec.on{background:#2a2}
select,input[type=range]{font-size:1.1em;margin:.3em}
.panel{background:#1c1c1c;margin:.6em auto;padding:.6em;max-width:820px;border-radius:.6em}
.small{color:#999;font-size:.85em}
#files{max-width:820px;margin:auto;text-align:left}
#files a{color:#8cf}
label{margin:0 .5em}
</style></head><body>
<h3>Rock Stereo Recorder</h3>
<div class=row>
<div><b>cam0</b><img src="/preview0.mjpg"></div>
<div><b>cam1</b><img src="/preview1.mjpg"></div>
</div>

<div class=panel>
<button id=rec onclick="rec()">&#9210; RECORD</button>
<span id=timer></span><br>
<label>bitrate <select id=bps onchange="setbps()"></select> Mbit/cam</label>
<div class=small>4K30 H.265, two independent MKV files (stitcher pairs by timestamp)</div>
</div>

<div class=panel>
<label><input type=checkbox id=ae checked onchange="setae()"> Auto exposure (tuned daylight strategy)</label>
<div id=manual style="display:none">
  <label>shutter <input type=range id=exp min=1 max=2400 value=600 oninput="lbl()" onchange="setctl()">
  <span id=expms></span></label><br>
  <label>gain <input type=range id=gain min=0 max=1000 value=0 oninput="lbl()" onchange="setctl()">
  <span id=gainx></span></label>
  <div class=small>applied to BOTH sensors identically (matched pano halves)</div>
</div>
</div>

<div class=panel><b>Recordings</b><div id=files></div></div>
<div id=msg class=small></div>

<script>
let S={};
function lbl(){
  const e=document.getElementById('exp').value;
  document.getElementById('expms').textContent=(e*%(LTUS)s/1000).toFixed(2)+' ms';
  document.getElementById('gainx').textContent='code '+document.getElementById('gain').value;
}
function refresh(){fetch('/status').then(r=>r.json()).then(s=>{S=s;
  const b=document.getElementById('rec');
  b.className=s.recording?'on':''; b.innerHTML=s.recording?'&#9209; STOP':'&#9210; RECORD';
  document.getElementById('timer').textContent=s.recording?(' REC '+s.elapsed+'s  '+s.sizes):'';
  document.getElementById('ae').checked=s.ae;
  document.getElementById('manual').style.display=s.ae?'none':'block';
  const sel=document.getElementById('bps');
  if(sel.options.length==0){s.bitrates.forEach(v=>{const o=document.createElement('option');
    o.value=v;o.textContent=v;sel.appendChild(o);});}
  sel.value=s.bps;
  if(s.exp_max){const ex=document.getElementById('exp');ex.max=s.exp_max;
    const g=document.getElementById('gain');g.min=s.gain_min;g.max=s.gain_max;}
  document.getElementById('files').innerHTML=s.files.map(f=>
    '<div>'+f.name+' &nbsp; '+f.size+' &nbsp; <a href="/dl/'+f.name+'">download</a></div>').join('')||'(none)';
  lbl();
});}
function rec(){fetch(S.recording?'/stop':'/start').then(r=>r.json())
  .then(s=>{document.getElementById('msg').textContent=s.msg||'';refresh();});}
function setbps(){fetch('/ctl?bps='+document.getElementById('bps').value).then(refresh);}
function setae(){fetch('/ctl?ae='+(document.getElementById('ae').checked?1:0)).then(refresh);}
function setctl(){fetch('/ctl?exposure='+document.getElementById('exp').value+
  '&gain='+document.getElementById('gain').value).then(refresh);}
setInterval(refresh,2000);refresh();
</script></body></html>""".replace("%(LTUS)s", str(LINE_TIME_US))


def sh(cmd, timeout=30):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def resolve_subdevs():
    for cam, c in CAMS.items():
        r = sh(f"media-ctl -d {c['sensor'][0]} -e '{c['sensor'][1]}'")
        _state["subdev"][cam] = r.stdout.strip()
    # control ranges from cam0 (twins)
    r = sh(f"v4l2-ctl -d {_state['subdev']['0']} -l")
    for line in r.stdout.splitlines():
        m = re.search(r"^\s*(exposure|analogue_gain)\b.*min=(-?\d+).*max=(-?\d+)", line)
        if m:
            _state["ctl_range"][m.group(1)] = (int(m.group(2)), int(m.group(3)))


def start_previews():
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
        log = open(f"/tmp/rec_prev{cam}.log", "w")
        _state["gst"][cam] = subprocess.Popen(pipeline, shell=True, stdout=log, stderr=log)


def ae_active():
    return sh("systemctl is-active rkaiq_3A").stdout.strip() == "active"


def start_recording():
    if _state["rec"]:
        return {"ok": False, "msg": "already recording"}
    os.makedirs(REC_DIR, exist_ok=True)
    name = time.strftime("take_%Y%m%d_%H%M%S")
    bps = _state["bps"] * 1000000
    for cam, c in CAMS.items():
        f = os.path.join(REC_DIR, f"{name}_cam{cam}.mkv")
        pipeline = (f"gst-launch-1.0 -e v4l2src device={c['main']} io-mode=dmabuf ! "
                    f"video/x-raw,format=NV12,width=3840,height=2160 ! videorate ! "
                    f"video/x-raw,framerate=30/1 ! "
                    f"queue max-size-buffers=8 max-size-time=0 max-size-bytes=0 ! "
                    f"mpph265enc rc-mode=cbr bps={bps} bps-max={bps * 3 // 2} ! "
                    f"h265parse ! matroskamux ! filesink location={f}")
        log = open(f"/tmp/rec_cam{cam}.log", "w")
        _state["rec"][cam] = subprocess.Popen(pipeline, shell=True, stdout=log, stderr=log,
                                              preexec_fn=os.setsid)
    _state["rec_name"] = name
    _state["rec_started"] = time.time()
    return {"ok": True, "msg": f"recording {name}"}


def stop_recording():
    if not _state["rec"]:
        return {"ok": False, "msg": "not recording"}
    for cam, p in _state["rec"].items():
        if p.poll() is None:
            os.killpg(os.getpgid(p.pid), signal.SIGINT)   # gst -e: EOS + finalize
    msgs = []
    for cam, p in _state["rec"].items():
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            msgs.append(f"cam{cam}: force-killed (file may be truncated but MKV stays readable)")
    name = _state["rec_name"]
    _state["rec"] = {}
    _state["rec_name"] = None
    _state["rec_started"] = None
    sizes = []
    for f in sorted(glob.glob(os.path.join(REC_DIR, f"{name}_cam*.mkv"))):
        sizes.append(f"{os.path.basename(f)} {os.path.getsize(f) // (1 << 20)}MB")
    return {"ok": True, "msg": "saved: " + ", ".join(sizes) + (" | " + "; ".join(msgs) if msgs else "")}


def rec_sizes():
    if not _state["rec_name"]:
        return ""
    tot = sum(os.path.getsize(f) for f in
              glob.glob(os.path.join(REC_DIR, f"{_state['rec_name']}_cam*.mkv")) if os.path.exists(f))
    return f"{tot // (1 << 20)}MB"


def list_recordings():
    out = []
    for f in sorted(glob.glob(os.path.join(REC_DIR, "*.mkv")), reverse=True)[:20]:
        out.append({"name": os.path.basename(f),
                    "size": f"{os.path.getsize(f) // (1 << 20)}MB"})
    return out


def apply_ctl(q):
    if "bps" in q:
        try:
            v = int(q["bps"])
            if v in BITRATES:
                _state["bps"] = v
        except ValueError:
            pass
    if "ae" in q:
        if q["ae"] == "1":
            sh("systemctl start rkaiq_3A")
        else:
            sh("systemctl stop rkaiq_3A")
    for key, ctl in (("exposure", "exposure"), ("gain", "analogue_gain")):
        if key in q and not ae_active():
            try:
                v = int(q[key])
            except ValueError:
                continue
            for cam in CAMS:
                sh(f"v4l2-ctl -d {_state['subdev'][cam]} --set-ctrl {ctl}={v}")


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
        path, _, qs = self.path.partition("?")
        q = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        if path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/status":
            er = _state["ctl_range"].get("exposure", (1, 2400))
            gr = _state["ctl_range"].get("analogue_gain", (0, 1000))
            self._json({
                "recording": bool(_state["rec"]),
                "elapsed": int(time.time() - _state["rec_started"]) if _state["rec_started"] else 0,
                "sizes": rec_sizes(),
                "bps": _state["bps"], "bitrates": BITRATES,
                "ae": ae_active(),
                "exp_max": er[1], "gain_min": gr[0], "gain_max": gr[1],
                "files": list_recordings(),
            })
        elif path == "/start":
            with _state["lock"]:
                self._json(start_recording())
        elif path == "/stop":
            with _state["lock"]:
                self._json(stop_recording())
        elif path == "/ctl":
            with _state["lock"]:
                apply_ctl(q)
            self._json({"ok": True})
        elif path.startswith("/dl/"):
            f = os.path.join(REC_DIR, os.path.basename(path[4:]))
            if not os.path.isfile(f):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Content-Length", str(os.path.getsize(f)))
            self.send_header("Content-Disposition", f"attachment; filename={os.path.basename(f)}")
            self.end_headers()
            with open(f, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
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
    resolve_subdevs()
    start_previews()
    ip = "unknown"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    print(f"Rock recorder panel:  http://{ip}:{PORT}   (Ctrl-C to stop)")
    try:
        Server(("0.0.0.0", PORT), Handler).serve_forever()
    finally:
        if _state["rec"]:
            stop_recording()


if __name__ == "__main__":
    main()
