# Raspberry Pi HQ Camera (IMX477) on the ROCK 5T

Goal: run the two genlocked Pi HQ cameras on the **Radxa ROCK 5T** (RK3588).
Three deliverables, none of which exist today: a **kernel driver**, a
**device-tree overlay**, and an **ISP tuning (IQ) file**. The XVS genlock
validated on the Orin (see `RPI_HQ_CAMERA.md`) is sensor-side and ports
straight into the new driver.

## Board recon (2026-09-03, live from the device)

| Item | Found |
|---|---|
| Board | Radxa ROCK 5T (`/proc/device-tree/model`) — NOT the 5B+ the flex adapter targeted; verify camera connectors before ordering cables |
| OS / kernel | Debian 12, vendor kernel `6.1.84-8-rk2410` |
| Kernel headers | installed (`linux-headers-6.1.84-8-rk2410`) → out-of-tree module builds work on-device |
| Build tools | gcc, make, git all present |
| ISP stack | `camera-engine-rkaiq 6.8.0-rk3588`, matches **rkisp v30** → that's the IQ JSON schema to target |
| IQ files | `/etc/iqfiles/` has imx219 (RPi cam v2), ov5647 (RPi cam v1), imx415 4K, others — **no imx477** |
| Camera drivers | built into the kernel (`=y`, not modules): imx219, imx415, imx464, imx214, **imx577** |
| IMX477 driver | none, anywhere |
| Overlays | `/boot/dtbo/` all generic/disabled; radxa-overlays source has camera overlays only for CM3-series boards — none for 5B/5T |
| Overlay management | `rsetup` (Radxa's tool) + `/boot/dtbo/` |
| `/dev/video0` | `stream_hdmirx` (the 5T's HDMI input — not a camera) |

## Why this is tractable

1. **`CONFIG_VIDEO_IMX577=y`** — Rockchip's vendor kernel already carries a
   driver for the IMX577, the IMX477's near-identical sibling (same 12.3MP
   Sony family). The IMX477 driver should be a light adaptation of that code
   (chip-ID, mode tables), not a from-scratch port. Rockchip sensor drivers
   also carry the RKMODULE ioctls the rkisp/rkaiq stack uses to find the
   right IQ file — mimic imx577/imx415, don't transplant the RPi driver.
2. **Radxa already tunes RPi cameras** (v1/v2 IQ files ship in the image), so
   RPi-camera-on-Rock is a trodden path — just not yet for the HQ camera.
3. **RPi publishes the IMX477's lab calibration** (`imx477.json` in the
   libcamera/raspberrypi repos): black level, noise-vs-gain model, AWB CT
   curve, CCMs at ~8 color temperatures, gamma. The IQ file is a translation
   into the rkisp v30 JSON, not a re-measurement.

## Work plan

### 1. Kernel driver (out-of-tree module `imx477.ko`)
- Base: Rockchip's `imx577.c` from the vendor kernel source
  (github.com/radxa/kernel, the 6.1 rkr branch matching `6.1.84-8-rk2410`).
- Adapt: chip ID (0x0477), mode tables (full 4056x3040, 2x2 binned 2028x1520,
  1080p), link freq for the connector's lane count.
- Bake in the XVS genlock from the Orin work: registers 0x3F0B/0x3041/0x3040/
  0x4B81, source=1/1/1/1 sink=1/0/0/0, applied at stream-on in standby.
  Role selection via a DT property (`trigger-mode = "source"|"sink"`) per
  camera node. NEVER both source (bus contention on the shared XVS wire).
- Build on the 5T against the installed headers; load via
  `/etc/modules-load.d/`.

### 2. Device-tree overlay
- No 5T camera overlay exists to copy — author from `rk3588-rock-5t.dts`
  (vendor kernel source) to find each CSI connector's i2c bus, dphy/csi2
  host, clock and power rails.
- Model on the CM3 rpi-camera overlays + the 5B radxa-camera-4k pattern:
  sensor node (i2c addr 0x1a, 24MHz xclk) -> csi2_dphy -> mipi2_csi2 ->
  rkcif -> rkisp virtual nodes.
- OPEN QUESTION: 5T connector pinout/lane count vs the Pi HQ's 2-lane, 15-pin
  FPC — determines the adapter cable AND whether the rock-5-pcb flex design
  (drawn for the 5B+ 31-pin CAM0) needs rework for the 5T.

### 3. IQ / tuning file (`imx477_RPI-HQ_default.json`)
- Skeleton: `/etc/iqfiles/imx415_RADXA-CAMERA-4K_DEFAULT.json` (4K sensor,
  same rkisp v30 schema).
- Transplant from RPi's `imx477.json`: black level, noise model, AWB
  calibration, CCMs, gamma.
- Daylight-first simplification: the rig records outdoors (sun / partly
  cloudy, ~5500-6800K). One daylight CCM + fixed daylight AWB gains covers
  the whole operating range; skip the tungsten-to-shade table initially.
- Lens shading: ships neutral at first; calibrate later from a flat-field
  shot with the actual lenses (matters at the stitch seam).
- Verification without a color chart: shoot the same scene with the Orin
  pipeline (same sensors+lenses, trusted tuning) and fit the residual
  correction — the same method that produced `grade.sh`'s matrix.

### 4. Cross-checks once cables exist
- `i2cdetect` for 0x1a on the connector's bus, then driver probe.
- Streaming smoke test: `v4l2-ctl` raw frames, then rkaiq path.
- Genlock: port `recorder/sync_test.sh` (timestamp drift measurement) — the
  method is platform-neutral.

## Access
- SSH from the Mac: `ssh rock` (radxa@192.168.86.136, key auth works).
