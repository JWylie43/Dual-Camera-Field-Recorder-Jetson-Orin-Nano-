#!/usr/bin/env python3
"""
server.py - Web control panel for record.py (Jetson Orin Nano camera rig)

Mobile-friendly LAN page: live camera preview, Start/Stop, status, live thermal
readout, calibration snapshots, and an optional thermal-log toggle. Drives record.py.

  START -> stop idle preview, launch `record.py --preview-port <P> ...`
  STOP  -> SIGINT the recorder (clean EOS), then restart idle preview

PREVIEW (works idle AND while recording):
  The camera can only be opened by one process. So when idle, THIS server runs a
  lightweight preview pipeline (camera -> downscale -> JPEG -> tcpserversink on
  PREVIEW_TCP). When recording, record.py's own `tee` serves the preview on the
  SAME port. Either way the browser reads /preview.mjpg, which relays the TCP
  MJPEG stream as multipart/x-mixed-replace into an <img> tag. The source behind
  the port swaps transparently; the page just reconnects across the brief gap.

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

app = Flask(__name__)

_lock = threading.Lock()
_state = {"proc": None, "started": None, "preview": None}


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
    return jsonify(running=running,
                   file=_newest_recording() if running else None,
                   elapsed=elapsed,
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
    """Relay the gst tcpserversink MJPEG stream to the browser as multipart."""
    def gen():
        s = None
        try:
            s = socket.create_connection(("127.0.0.1", PREVIEW_TCP), timeout=3)
            while True:
                chunk = s.recv(8192)
                if not chunk:
                    break
                yield chunk
        except OSError:
            return                               # no producer yet; browser will retry
        finally:
            if s is not None:
                s.close()
    return Response(gen(),
                    mimetype=f"multipart/x-mixed-replace; boundary={PREVIEW_BOUNDARY}")


@app.route("/start", methods=["POST"])
def start():
    with _lock:
        if _is_running():
            return jsonify(ok=False, error="already recording"), 409
        data = request.get_json(silent=True) or {}
        _stop_idle_preview()                     # free the camera + preview port
        time.sleep(SETTLE)
        cmd = ["python3", RECORD_SCRIPT, "--output-dir", OUTPUT_DIR,
               "--preview-port", str(PREVIEW_TCP)]
        if data.get("log_thermals"):
            cmd.append("--log-thermals")
        if data.get("quality"):
            cmd += ["--quality", str(int(data["quality"]))]
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _state.update(proc=proc, started=time.time())
        return jsonify(ok=True)


@app.route("/stop", methods=["POST"])
def stop():
    with _lock:
        if not _is_running():
            return jsonify(ok=False, error="not recording"), 409
        proc = _state["proc"]
        proc.send_signal(signal.SIGINT)          # clean EOS -> finalized MKV
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.terminate()
        f = _newest_recording()
        _state.update(proc=None, started=None)
        time.sleep(SETTLE)
        _start_idle_preview()                    # resume idle preview
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
        os.makedirs(CALIB_DIR, exist_ok=True)
        _stop_idle_preview()
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
        _start_idle_preview()
        if not saved:
            return jsonify(ok=False, error="capture failed (no frame)"), 500
        count = len(glob.glob(os.path.join(CALIB_DIR, "calib_*.jpg")))
        return jsonify(ok=True, file=os.path.basename(saved), count=count)


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
 #start { background:#1f8a3b; } #stop { background:#b3271e; } #snap { background:#2a6f9e; }
 button:disabled { opacity:.35; }
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
   <label><input type="checkbox" id="showpv" checked> Show preview</label>
   <div id="pvnote" class="muted"></div>
 </div>
 <div class="card">
   <span class="dot" id="dot"></span><span id="statetext">&hellip;</span>
   <div id="detail" class="muted"></div>
 </div>
 <div class="card">
   <label><input type="checkbox" id="logth"> Log thermals to file</label>
   <label>Quality <input type="number" id="quality" value="85" min="1" max="100"></label>
   <div id="rootnote" class="muted"></div>
 </div>
 <button id="start">&#9679; Start Recording</button>
 <button id="stop" disabled>&#9632; Stop Recording</button>
 <div class="card">
   <button id="snap">&#128247; Snapshot (calibration)</button>
   <div id="snapnote" class="muted">Full-res stills &rarr; /mnt/video/calib</div>
 </div>
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

// Preview show/hide. When off, drop the stream (src='') so the browser closes the
// connection -> no Wi-Fi traffic, no phone battery, server stops relaying.
function reconnectPreview(){ if($('showpv').checked) $('preview').src = '/preview.mjpg?' + Date.now(); }
$('preview').onerror = () => { if($('showpv').checked) setTimeout(reconnectPreview, 1200); };
$('showpv').onchange = () => {
  if($('showpv').checked){ $('preview').style.display='block'; $('pvnote').textContent=''; reconnectPreview(); }
  else { $('preview').style.display='none'; $('preview').src='';
         $('pvnote').textContent='Hidden — no stream to this device (camera still records normally).'; }
};

let wasRunning = null;
async function refreshStatus(){
  try{
    const s = await (await fetch('/status')).json();
    $('dot').className = 'dot' + (s.running?' rec':'');
    $('statetext').textContent = s.running ? 'RECORDING — '+fmt(s.elapsed) : 'Idle';
    $('detail').textContent = s.running && s.file ? s.file : '';
    $('start').disabled = s.running;
    $('stop').disabled  = !s.running;
    $('snap').disabled  = s.running;
    $('rootnote').textContent = (!s.server_root && $('logth').checked)
      ? 'Note: logging to file needs the server run under sudo.' : '';
    // camera handed over on a state change -> nudge the preview to reconnect
    if(wasRunning !== null && wasRunning !== s.running) setTimeout(reconnectPreview, 1500);
    wasRunning = s.running;
  }catch(e){ $('statetext').textContent = 'server unreachable'; }
}
$('start').onclick = async () => { $('start').disabled=true;
  await post('/start',{log_thermals:$('logth').checked, quality:+$('quality').value});
  refreshStatus(); };
$('stop').onclick  = async () => { $('stop').disabled=true; await post('/stop'); refreshStatus(); };
$('snap').onclick  = async () => {
  $('snap').disabled=true; $('snapnote').textContent='Capturing…';
  const r = await post('/snapshot');
  $('snapnote').textContent = r.ok
    ? 'Saved '+r.file+' — '+r.count+' shots in /mnt/video/calib'
    : 'Error: '+(r.error||'failed');
  $('snap').disabled=false;
  setTimeout(reconnectPreview, 1500);   // preview blinked during the grab
};
$('logth').onchange = refreshStatus;
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


if __name__ == "__main__":
    print(f"Camera rig control panel on http://0.0.0.0:{PORT}  (find IP: hostname -I)")
    _start_idle_preview()                        # live preview as soon as we boot
    try:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    finally:
        _stop_idle_preview()
