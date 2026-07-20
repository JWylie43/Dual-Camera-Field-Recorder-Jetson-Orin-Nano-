# Orin Nano Stereo Recorder & Stitcher

An end-to-end field rig for a side-by-side stereo camera on a **Jetson Orin Nano**:
**record** a wide dual-camera feed, **calibrate** the two cameras, and **stitch** the
result into a single cylindrical panorama.

Built around the Arducam B0577 dual global-shutter kit (3840×1200 combined) in a
3D-printed housing, tuned to run for hours outdoors at **30 fps with the CPU
near-idle** (maximum thermal headroom).

---

## Project layout

```
orin-nano-recorder/
├── recorder/            Runs ON THE ORIN — capture + web control panel
│   ├── record.py          hardware-MJPEG → MKV recorder (GStreamer)
│   └── server.py          Flask web panel (Start/Stop, live preview, thermals, snapshots)
├── calibration/         Runs ON YOUR MAC/DESKTOP — ChArUco stereo calibration
│   ├── show_board.py      generate/display the ChArUco board
│   ├── calibrate.py       intrinsics (L+R) + stereo extrinsics from snapshots
│   ├── images/            calibration snapshots (git-ignored)
│   └── *_intrinsics.json, stereo_extrinsics.json   calibration results
├── stitching/           Runs ON YOUR MAC/DESKTOP — panorama stitcher (C++)
│   ├── stitch_pipeline.cpp   calibration-driven cylindrical stitch (image or video)
│   ├── CMakeLists.txt
│   └── include/json.hpp
├── 3d-housing-model/    Printable enclosure (Housing.zip)
└── setup.sh             Orin dependency install / L4T element check
```

## The workflow

1. **Record** on the Orin (`recorder/`) → combined `3840×1200` MKV on the NVMe, and
   full-res calibration snapshots via the web panel's Snapshot button.
2. **Calibrate** on your computer (`calibration/`) → per-camera intrinsics +
   the stereo extrinsic rotation between the two cameras (JSON files).
3. **Stitch** on your computer (`stitching/`) → feed a frame or video + the
   calibration and get a cylindrical panorama. Alignment is driven purely by the
   calibration — no feature matching.

---

# 1. Recorder (on the Orin)

`recorder/record.py` (CLI) and `recorder/server.py` (web panel).

## The one thing to understand first

**The Jetson Orin Nano (and Orin NX) have no hardware video encoder (no NVENC).**
That single fact drives every design decision here:

1. **No `nvv4l2h264enc` / `nvv4l2h265enc`** — they don't exist on this board. H.264/
   H.265 must be done in *software* (x264, CPU-heavy) or you use the **hardware JPEG
   engine (NVJPG)** for MJPEG. This project uses **MJPEG** so the CPU stays idle and
   the board runs cool — the right trade for a hot enclosure. (Files are bigger:
   ~40 GB/hr vs ~7 for H.264. A 500 GB drive holds ~12 hours.)

2. **Capture with `nvv4l2camerasrc`, never `v4l2src`.** Plain `v4l2src` copies every
   9 MB frame on one CPU thread, pegging a core and capping the pipeline at ~28 fps.
   `nvv4l2camerasrc` captures zero-copy into NVMM (GPU memory) → clean, sustained
   30 fps with the CPU barely moving. **This was the single fix that unblocked everything.**

3. **Every queue is `leaky=downstream`.** `nvvidconv` (the VIC) owns a tiny output
   buffer pool; a non-leaky queue behind it grabs all the buffers and *deadlocks* the
   pipeline. Leaky queues drop a frame under pressure instead of stalling.

4. **Stop with SIGINT, never SIGTERM/SIGKILL.** The MKV gets its duration + seek
   index only on a clean end-of-stream (`gst-launch -e` on SIGINT). Kill it any other
   way and you get an unfinalized file (`Duration: N/A`, won't scrub).

## Usage

```bash
cd recorder
# CLI recorder
python3 record.py                 # record until Enter / Ctrl+C
python3 record.py --seconds 30    # fixed duration
python3 record.py --measure --seconds 20         # benchmark fps, no disk write
python3 record.py --seconds 600 --log-thermals   # soak test + tegrastats log
python3 record.py --dry-run       # print the pipeline, don't run

# Web control panel
sudo python3 server.py            # sudo enables the "log thermals to file" toggle
```

Browse from a phone/laptop on the same network to **`http://<orin-ip>:8080`** (find
the IP with `hostname -I`, or use `http://<hostname>.local:8080`). The panel gives you
live preview (idle *and* recording), Start/Stop, elapsed time, live thermals, and a
**Snapshot** button that writes full-res stills to `/mnt/video/calib` for calibration.

## How it works

```
record.py builds & runs a GStreamer pipeline:
  nvv4l2camerasrc → NVMM,UYVY → nvvidconv → I420 → queue(leaky)
    → nvjpegenc → jpegparse → matroskamux → filesink            (the MKV)
  with --preview-port it tees a second branch to a tcpserversink (MJPEG preview)

server.py owns an idle-preview pipeline when stopped, hands the camera to record.py
  on Start, and relays the MJPEG to the browser at /preview.mjpg. Stop = SIGINT =
  clean finalize. The camera is single-open, so one pipeline holds it at a time.
```

## Auto-start on boot (systemd)

Run the panel as a service so it survives reboots. Point it at `recorder/server.py`
and use **`KillSignal=SIGINT`** so `systemctl stop` finalizes the current recording:

```ini
# /etc/systemd/system/camera-rig.service
[Unit]
After=network-online.target
Wants=network-online.target
[Service]
WorkingDirectory=/home/<user>/orin-nano-recorder/recorder
ExecStart=/usr/bin/python3 /home/<user>/orin-nano-recorder/recorder/server.py
Restart=on-failure
User=root
[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now camera-rig
```

For a **portable** rig, the Orin can broadcast its own Wi-Fi hotspot (NetworkManager
`ipv4.method shared`), so a phone/laptop connects directly at a fixed address with no
router. Give the Orin higher auto-connect priority on your home Wi-Fi so it rejoins
that when in range and falls back to the hotspot in the field.

---

# 2. Calibration (on your computer)

ChArUco-based stereo calibration. **Run on your Mac/desktop, not the Orin** — it needs
`opencv-contrib-python` (the `aruco` module):

```bash
pip install -U opencv-contrib-python numpy
```

## Steps

1. **Board.** `python3 calibration/show_board.py` generates/opens `charuco_board.png`
   (7×10, `DICT_5X5_1000`). **Print it** (matte, glued flat) — printing beats a screen
   (screens add glare/moiré that break detection). **Measure one black square with
   calipers** — you'll pass that size.
2. **Capture.** From the web panel, tap **Snapshot** ~30–40× with the board at varied
   angles/distances. Cover each camera's whole frame **and** get a good batch with the
   board centred where **both** cameras see it (that overlap set drives the extrinsics).
3. **Pull the shots** to `calibration/images/`:
   ```bash
   scp -r <user>@<orin-host>:/mnt/video/calib ./calibration/images
   ```
4. **Calibrate** (from `calibration/`, pass your measured board sizes in mm):
   ```bash
   python3 calibrate.py --square-mm 66.5 --marker-mm 48.5
   ```

Outputs `left_intrinsics.json`, `right_intrinsics.json`, `stereo_extrinsics.json`
(baseline in mm + rotation), plus undistort/rectified sample images.

- Intrinsics are **mount-independent**; extrinsics describe the **fixed housing** —
  re-run if the cameras are disturbed.
- To re-solve only the extrinsics from a fresh overlap batch without recomputing
  intrinsics: `python3 calibrate.py --use-intrinsics . --square-mm 66.5 --marker-mm 48.5`.
- Good result: RMS < ~1 px per camera and for the stereo solve, ≥80% frame coverage,
  ≥10 stereo views, and a baseline matching your real lens spacing.

---

# 3. Stitching (on your computer)

`stitching/stitch_pipeline.cpp` — a calibration-driven **cylindrical** stitcher. Reads
the calibration JSON and stitches the combined LEFT|RIGHT feed into one panorama.
**No feature detection** (no BRISK / findHomography); alignment is the extrinsic
rotation `R`. Per-frame work runs on `cv::UMat`, so it uses the **GPU via OpenCL**
when available and falls back to CPU otherwise — same source on macOS and desktop,
OpenCV 4.x or 5.x.

## Build

```bash
# macOS: brew install cmake opencv
cd stitching
cmake -S . -B build && cmake --build build
```

## Run

```bash
# single image → pipeline_out/pano.jpg (+ overlap_5050.jpg)
./build/StitchPipeline --source ../calibration/images/calib_114.jpg

# video → pipeline_out/stitched_video.mp4
./build/StitchPipeline --source recording.mp4 --start 300 --end 900
```

Source type is auto-detected: images stitch one frame; videos (`.mp4/.mkv/.mov/...`)
loop frames. The remap maps depend only on the calibration, so they're built once and
reused for every frame.

| Flag | Default | Meaning |
|---|---|---|
| `--source PATH` | (required) | Combined LEFT\|RIGHT image or video (alias `--image`) |
| `--calib-dir DIR` | `../calibration` | Folder with the intrinsics/extrinsics JSON |
| `--out DIR` | `pipeline_out` | Output folder |
| `--start N` / `--end N` | `0` / `-1` | Video frame range (`-1` = end) |
| `--degrees D` | `0` | Horizon roll correction |
| `--seam X` | auto | Straight-seam column (default: overlap centre). Ignored when `--smart-seam` is on |
| `--shift-top N` | `0` | Horizontal shift of the right image's **top** rows (aligns the **far** edge) |
| `--shift-bottom N` | `0` | Horizontal shift of the **bottom** rows (aligns the **near** edge) |
| `--shift-y N` | `0` | Vertical shift of the right image |
| `--shift-x N` | `0` | Convenience: sets both top and bottom to N (plain horizontal shift) |
| `--bands N` | `6` | Multi-band (Laplacian) blend levels — smooths the seam *without* blurring detail; `0` = hard seam |
| `--exposure` | off | Match the right image's brightness/color to the left (per-channel gain measured in the overlap) — for when one camera faces brighter light |
| `--smart-seam` | off | Route the seam through the min-difference path so it weaves *around* moving objects — reduces player ghosting/duplication at the seam. Auto-searches the known overlap region (no `--seam` needed), biased toward the overlap centre, and is temporally stabilised so it stays put frame-to-frame (won't jitter with tripod sway) unless a moving object makes a detour clearly cheaper |
| `--tune` | off | Launch the interactive browser tuner (see below) |
| `--port N` | `8090` | Port for the `--tune` web server |

If `--shift-top` ≠ `--shift-bottom`, the right image is **sheared** — the per-row
horizontal shift is interpolated between them. This aligns a **receding field**
(near edge at the bottom, far edge at the top) along a straight vertical seam:
dial `--shift-bottom` for the front of the field and `--shift-top` for the back.

### Interactive tuning (`--tune`)

Rotation-only alignment is exact only at infinity; at finite distance a small
horizontal **shift** can sharpen a chosen plane (e.g. a screen). `--tune` gives you
a one-command visual tuner:

```bash
./build/StitchPipeline --source <image-or-video> --tune
```

This warps the first frame, starts a tiny localhost web server, and **opens your
browser** to a live tuner. There you:
- adjust **Shift far (top)**, **Shift near (bottom)**, **Shift-y**, and **Seam** with
  the **◀ ▶** arrows or by typing (←/→ shifts both top & bottom together; `[` `]`
  moves the seam) over a live stitched preview — align the far and near edges of the
  field so the whole thing lines up under a straight vertical seam. Leave
  **multi-band blend** on to smooth the join in the output (it keeps detail sharp,
  unlike a linear feather), and enable **exposure/color match** if one camera faces
  brighter light, and turn on **seam avoidance** if players ghost/duplicate at the
  seam (it routes the cut around moving objects); the preview shows the raw seam so
  you can align precisely, and blend/exposure/seam-avoidance apply to the output,
- **for a video**, use the **Frame** slider, its ◀ ▶, or the frame box to jump to
  any frame and align on it — the current frame and total (`N / 1591`) are shown.
  The total comes from `ffprobe` packet-counting (fast, exact for MJPEG even when the
  container reports no frame count); if `ffprobe` isn't available, the frame box +
  prev/next still work, just without a fixed slider range. (These recordings can't be
  seeked, so frames are reached by reading sequentially — scrubbing **forward** is
  fast; a big jump **backward** re-reads from the start and takes a moment.)
- every value is a **typeable box** — click the ◀ ▶ arrows or just type a number,
- toggle **overlap blend** (50/50) to check alignment,
- click **Stitch all frames** to run the full stitch on the whole image/video with
  the current values — a **progress bar** tracks it and a **✅ Done** message shows
  the saved path when finished (no need to re-run anything),
- click **Finish & stop** (appears when done) or **Quit** to stop the server so the
  terminal command exits; then close the tab.

For a video it previews/​tunes on the first frame (or `--start N`), then applies your
chosen shift/seam to every frame. You can also skip the UI and pass the values
directly: `--shift-x N --seam X`. Use `--port N` if 8090 is taken.

> **Exposure/seam:** in high-contrast scenes the seam can show a brightness step (one
> camera facing a bright window). Exposure compensation + seam feathering are future
> additions; on evenly-lit footage it isn't an issue.

---

## Hardware / prerequisites (Orin)

- Jetson Orin Nano (or NX) with JetPack / L4T.
- Arducam B0577 stereo kit (or any V4L2 camera that outputs **UYVY** over CSI). Install
  the vendor driver first — confirm with `v4l2-ctl --list-formats-ext` (expect
  `UYVY 3840x1200`).
- An NVMe mounted at **`/mnt/video`**, writable by your user.
- NVIDIA L4T GStreamer elements (`nvv4l2camerasrc`, `nvvidconv`, `nvjpegenc`) — ship
  with JetPack.

Install on the Orin:
```bash
git clone <your-repo-url> orin-nano-recorder
cd orin-nano-recorder
./setup.sh          # apt deps + checks the L4T elements are present
```

> **Different camera/resolution?** Edit the `Config` in `recorder/record.py` (and the
> preview caps in `recorder/server.py`). Camera-agnostic as long as the source is a
> V4L2 YUV device.

### Tested environment

| Component | Tested version |
|---|---|
| Board | Jetson Orin Nano Developer Kit |
| JetPack | 6.2 (L4T R36.4.3, kernel 5.15.148-tegra) |
| OS | Ubuntu 22.04 (jammy), arm64 |
| Python / GStreamer / Flask | 3.10 / 1.20.3 / 2.0.1 |
| Camera | Arducam B0577 dual global-shutter (2.3 MP × 2, onboard ISP) |
| Stitcher OpenCV | 4.x or 5.x (built per machine) |

**Kernel pinning (important):** the Arducam driver is a kernel package built for the
exact kernel/L4T. Don't `apt upgrade` the kernel without a matching Arducam build, or
the camera stops enumerating after reboot. Pin the kernel, or upgrade both together.

---

## Playback

MJPEG-in-MKV doesn't open in some default players. **Use VLC:**
```bash
sudo apt install -y vlc
xdg-mime default vlc.desktop video/x-matroska
```

## Troubleshooting (recorder)

| Symptom | Cause / fix |
|---|---|
| Caps "not-negotiated" at start | Camera doesn't offer that format/res — check `v4l2-ctl --list-formats-ext`. |
| One core at 100%, ~26–28 fps | You're using `v4l2src`. Switch to `nvv4l2camerasrc`. |
| Pipeline freezes, one core spinning | Non-leaky `queue` after `nvvidconv` deadlocked the VIC pool. Use `leaky=downstream`. |
| `Duration: N/A`, won't scrub in VLC | File wasn't finalized — killed with SIGTERM. Stop with SIGINT (Enter/Ctrl-C). |
| Green / tiny file | No real camera signal — check the ribbon and driver. |
| `nvv4l2h265enc` missing | Expected on Orin Nano/NX (no NVENC). Use MJPEG. |

## Troubleshooting (calibration / stitching)

| Symptom | Cause / fix |
|---|---|
| `No module named cv2.aruco` | `pip install -U opencv-contrib-python` (calibration needs contrib). |
| 0 stereo views / no overlap | Board never fully visible to both cameras at once — get it centred and big in **both** preview halves; print it (screens fail). |
| Baseline scale looks wrong | `--square-mm`/`--marker-mm` must match the **measured** printed board. |
| Stitch seam ghosting on near objects | Parallax from the baseline — unavoidable rotation-only; minimal on distant scenes. |

---

## License

MIT — see `LICENSE`.
