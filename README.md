# Orin Nano Stereo Recorder

Live 30 fps recording of a side-by-side stereo camera on a **Jetson Orin Nano**,
plus a phone/laptop web control panel with live preview. Built for a field rig
(Arducam B0577 dual global-shutter kit, 3840×1200 combined) that runs for hours
outdoors, so it's tuned for **30 fps sustained with the CPU near-idle** (maximum
thermal headroom).

- `record.py` — the recorder (hardware-MJPEG → MKV on the NVMe)
- `server.py` — a Flask web control panel (Start/Stop, live preview, status, thermals)

---

## The one thing to understand first

**The Jetson Orin Nano (and Orin NX) have no hardware video encoder (no NVENC).**
That single fact drives every design decision here, and it's the thing that costs
people the most time. Consequences:

1. **No `nvv4l2h264enc` / `nvv4l2h265enc`** — they don't exist on this board. H.264/
   H.265 must be done in *software* (x264, CPU-heavy) or you use the **hardware JPEG
   engine (NVJPG)** for MJPEG. This project uses **MJPEG** so the CPU stays idle and
   the board runs cool — the right trade for a hot enclosure. (Files are bigger:
   ~40 GB/hr vs ~7 for H.264. A 500 GB drive holds ~12 hours.)

2. **Capture with `nvv4l2camerasrc`, never `v4l2src`.** Plain `v4l2src` copies every
   9 MB frame on one CPU thread, which pegs a core and caps the *entire* pipeline at
   ~28 fps regardless of the encoder. `nvv4l2camerasrc` captures zero-copy into NVMM
   (GPU memory); with it you get a clean, sustained 30 fps and the CPU barely moves.
   **This was the single fix that unblocked everything.**

3. **Every queue is `leaky=downstream`.** `nvvidconv` (the VIC) owns a tiny output
   buffer pool; a normal (non-leaky) queue behind it grabs all the buffers and
   *deadlocks* the pipeline (one core spins at 100%, no frames flow). Leaky queues
   drop a frame under pressure instead of stalling.

4. **Stop with SIGINT, never SIGTERM/SIGKILL.** The MKV gets its duration + seek
   index only on a clean end-of-stream, which `gst-launch -e` emits on SIGINT. Kill
   it any other way and you get an unfinalized file (`Duration: N/A`, won't scrub).

---

## Hardware / prerequisites

- Jetson Orin Nano (or NX) with JetPack / L4T installed.
- Arducam B0577 stereo kit (or any V4L2 camera that outputs **UYVY** over CSI).
  Install the vendor camera driver per Arducam's instructions first — confirm with
  `v4l2-ctl --list-formats-ext`.
- An NVMe (or fast storage) mounted at **`/mnt/video`** and writable by your user.
- The NVIDIA L4T GStreamer elements (`nvv4l2camerasrc`, `nvvidconv`, `nvjpegenc`) —
  these ship with JetPack.

> **Different camera or resolution?** Edit `width`/`height`/`fps`/`pixel_format` in
> the `Config` class in `record.py` (and the preview caps in `server.py`). The
> design is camera-agnostic as long as the source is a V4L2 YUV device.

---

## Install

```bash
git clone <your-repo-url> orin-recorder
cd orin-recorder
./setup.sh          # apt deps + checks the L4T elements are present
```

`setup.sh` installs `v4l-utils`, `python3-flask`, and the GStreamer good/base
plugins, then verifies the NVIDIA elements exist.

---

## Usage

### Recorder (CLI)

```bash
python3 record.py                 # record until Enter / Ctrl+C
python3 record.py --seconds 30    # fixed duration
python3 record.py --measure --seconds 20         # benchmark fps, no disk write
python3 record.py --seconds 600 --log-thermals   # soak test + tegrastats log
python3 record.py --quality 90    # higher MJPEG quality (bigger files)
python3 record.py --dry-run       # print the pipeline, don't run
```

Stop a running recording with **Enter** or **Ctrl-C** — both finalize cleanly.

### Web control panel

```bash
sudo python3 server.py            # sudo enables the "log thermals to file" toggle
hostname -I                       # find the Orin's LAN IP
```

Browse from your phone/laptop on the same network to **`http://<orin-ip>:8080`**.
You get: live preview (before *and* during recording), Start/Stop, elapsed time,
a live on-screen thermal readout, a show/hide-preview toggle, and an optional
thermal-log-to-file toggle.

Install Flask via apt (no pip needed): `sudo apt install -y python3-flask`.

---

## How it works (architecture)

```
record.py  ── builds & runs a GStreamer pipeline:
  nvv4l2camerasrc → NVMM,UYVY → nvvidconv → I420 → queue(leaky)
    → nvjpegenc → jpegparse → matroskamux → filesink           (the MKV)

  with --preview-port it tees a second branch:
    → tee ─┬→ [record branch above]                            (NVJPG engine)
           └→ queue(leaky) → downscale → nvjpegenc → tcpserversink   (NVJPG1 engine)

server.py  ── owns an idle-preview pipeline when stopped, hands the camera to
  record.py (with its preview tee) on Start, and relays the MJPEG to the browser
  at /preview.mjpg. State lives in the server; the page is a thin client that polls
  /status and /thermals. Stop = SIGINT to record.py = clean finalize.
```

The camera is single-open, so only one pipeline holds it at a time; the idle and
recording previews serve on the same port, so the browser sees one continuous
viewfinder that just blinks once on Start/Stop.

---

## Playback

MJPEG-in-MKV doesn't open in some default players. **Use VLC:**

```bash
sudo apt install -y vlc
xdg-mime default vlc.desktop video/x-matroska   # double-click .mkv opens VLC
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Caps "not-negotiated" at start | Camera doesn't offer that format/res — check `v4l2-ctl --list-formats-ext`. |
| One core at 100%, ~26–28 fps | You're using `v4l2src`. Switch to `nvv4l2camerasrc`. |
| Pipeline freezes, one core spinning | A non-leaky `queue` after `nvvidconv` deadlocked the VIC pool. Make queues `leaky=downstream`. |
| `Duration: N/A`, won't scrub in VLC | File wasn't finalized — it was killed with SIGTERM. Stop with SIGINT (Enter/Ctrl-C). |
| Green / tiny file | No real camera signal — check the ribbon and driver. |
| `nvv4l2h265enc` missing | Expected on Orin Nano/NX (no NVENC). Use MJPEG. |
| Preview pane black | Confirm `multipartmux` exists (`gst-inspect-1.0 multipartmux`, in plugins-good). |

---

## Auto-start on boot (optional)

Run the control panel as a service so it survives reboots/logout. See
`systemd/` if present, or create a unit with **`KillSignal=SIGINT`** so
`systemctl stop` finalizes the current recording cleanly.

---

## License

MIT — see `LICENSE`.
