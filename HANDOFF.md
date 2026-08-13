# Project Context / Session Handoff

Paste this into a new chat to bring it up to speed on the Orin Nano camera work.

## What this project is

An end-to-end field rig for a **side-by-side stereo camera on a Jetson Orin Nano**:
record a dual-camera feed, calibrate the two cameras, and stitch the result into a
single cylindrical panorama. Originally built around the **Arducam B0577** dual
global-shutter kit (3840×1200 combined MJPEG). See `README.md` for the full picture.

**New track (branch `rpi-hq-camera`):** adding support for two **Raspberry Pi HQ
cameras** (Sony **IMX477**, Adafruit #4561) on the same Orin, aimed at a
**genlocked (hardware-synced) stereo pair** — one master, one slave, XVS pins tied.

## Hardware / software facts

- **Board:** Jetson Orin Nano *Super* — module `p3767-0005`, carrier `p3768`.
- **JetPack 6.2 / L4T 36.4.3**, kernel `5.15.148-tegra`.
- **Booting a custom Arducam kernel** (`/boot/arducam/Image` per
  `/boot/extlinux/extlinux.conf`), not the stock NVIDIA kernel. Overlays live in
  `/boot/arducam/dts/` (a mirror of the stock set is in `/boot/`).
- **CSI:** both 22-pin ports are **2-lane**. Camera I²C buses are **i2c-9** and
  **i2c-10**. IMX477 sits at address **0x1a**.
- **Device nodes when Pi cameras are active:** `/dev/video0` = sensor on i2c-9
  (`9-001a`), `/dev/video1` = sensor on i2c-10 (`10-001a`).
- **Pi HQ cameras use the Tegra ISP path** (`nvarguscamerasrc` / Argus → NV12), not
  the MJPEG path the Arducam recorder currently uses (`nvv4l2camerasrc`). This is the
  main integration difference.
- **IMX477 modes exposed by the stock driver:** only two — `3840x2160@30` and
  `1920x1080@60`, both raw 10-bit Bayer (`RG10`). Full-sensor 4056×3040 is NOT
  exposed; adding modes = device-tree mode tables + driver register sequences.
- **The Orin Nano has NO hardware video encoder (no NVENC)** — `nvv4l2h264enc`/`265`
  do not exist. It DOES have hardware JPEG (`nvjpegenc`). So on-Orin recording is
  MJPEG (light, CPU idle); H.264/H.265 encode happens off-box (Mac VideoToolbox /
  Windows AMF). The IMX477 recorder path should be `nvarguscamerasrc → nvjpegenc`.

## Access

- SSH: `joe@192.168.86.130` (host `joe-orin-nano`). Repo on the Orin: `~/orin-recorder`.
- Repo also checked out on the Mac at `~/Desktop/orin-nano-recorder` (dev machine).
- The user runs all on-Orin commands themselves and pastes output back.

## The two camera setups + how to switch (`camswitch`)

MIPI/CSI cameras are **not self-describing** — the active camera is chosen by the
**device-tree overlay** on the `OVERLAYS` line of `extlinux.conf`, merged by the
bootloader and locked in at boot. So switching cameras = swap the overlay + reboot.
Both overlays already ship on disk:

- `tegra234-p3767-camera-p3768-imx477-dual.dtbo` — Pi HQ (IMX477), 2-lane, both ports
- `tegra234-p3767-camera-p3768-arducam-dual.dtbo` — Arducam B0577 dual kit

The `./camswitch` script (repo root, on branch `rpi-hq-camera`) does this in one step:

```bash
sudo ./camswitch pi        # both ports = Pi HQ (IMX477)
sudo ./camswitch arducam   # back to the Arducam kit
./camswitch status         # show the active overlay
```

Each offers a reboot. See `RPI_HQ_CAMERA.md` for the full write-up.

## CRITICAL gotcha (already solved, but re-appears on re-flash / new board)

The Arducam kernel install **disables the stock IMX477 driver** by renaming
`nv_imx477.ko` → `nv_imx477.ko.bak`. Symptom: the overlay is correct and the sensors
appear in the device tree (`/sys/bus/i2c/devices/9-001a`, `10-001a` exist), but **no
driver binds**, dmesg shows **no probe attempt**, and `i2cdetect` shows `--` (not
`UU`) at `0x1a`. Looks like dead hardware; nothing is actually wrong.

Fix (now automated inside `camswitch pi`):

```bash
sudo mv /lib/modules/$(uname -r)/updates/drivers/media/i2c/nv_imx477.ko.bak \
        /lib/modules/$(uname -r)/updates/drivers/media/i2c/nv_imx477.ko
sudo depmod -a && sudo modprobe nv_imx477
```

After this: `i2cdetect` shows `UU` at `0x1a`, and `/dev/video0` + `/dev/video1` appear.
The rename persists across reboots (module auto-loads via udev).

## Current working state (as of this handoff)

✅ Both Pi HQ IMX477 cameras **enumerate and produce video** on `/dev/video0` and
`/dev/video1`. No R8 resistor mod needed — the genuine RPi HQ boards work as-is on
this Orin. The only fix required was un-hiding the driver above.

## Recorder integration status

✅ **Phase 1 (recording) done.** `record.py --source imx477` records both Pi HQ
cameras as one combined 3840×1080@30 MJPEG MKV (two `nvarguscamerasrc` ISP streams
→ `nvcompositor` side-by-side → the existing record branch), stitcher-ready.
`capture.py` gained `--eos-to pipeline` for the two-source finalize. Confirmed
recording + playing in VLC. Runs ISP auto-exposure (no manual controls yet).
Remaining: Phase 2 web panel (Argus-property controls + preview), then genlock.

Note on frame rate vs exposure: fps is set by the sensor frame duration (blanking),
NOT by dropping frames; it only caps exposure. Motion blur is governed by exposure
time (13 µs–frame duration), chosen independently by AE — cap `exposuretimerange`
to reduce blur for fast action, not the frame rate.

## Goals / next steps (roughly in order)

1. **Pin down record modes** — `v4l2-ctl -d /dev/video0 --list-formats-ext`; choose
   resolution/framerate for recording.
2. **Confirm the full ISP capture path** — save a few seconds from each sensor via
   `nvarguscamerasrc` (headless, to file), and nail down which physical camera is
   `/dev/video0` vs `/dev/video1` (left/right mapping).
3. **Integrate Pi HQ into the recorder** — `recorder/record.py` + `server.py` were
   built for the Arducam MJPEG path; the IMX477 needs an `nvarguscamerasrc`/Argus
   pipeline. This is the main code work.
4. **Genlock (master/slave XVS sync)** — the headline goal. NVIDIA's stock `imx477`
   driver does NOT expose sync registers; Raspberry Pi's kernel driver
   (`raspberrypi/linux`, `drivers/media/i2c/imx477.c`, GPL) does. Port that sync
   support into the driver here → kernel-module build. XVS is 1.8 V logic; tie the
   two cameras' XVS pads directly, one master + one slave.
5. **Custom sensor modes** — new resolution/framerate entries live in the device-tree
   mode tables (`tegra234-camera-imx477-*.dtsi`) + matching register sequences in the
   driver (from `public_sources`). Kernel/DTB rebuild.

## Repo notes

- `recorder/` runs ON THE ORIN (capture + Flask web control panel).
- `calibration/` and `stitching/` run on the Mac/desktop.
- Code style: full block-body arrow functions with explicit `return` (see user's
  global rules); match surrounding conventions.
- The user runs stitch/tuner/VMAF-type commands himself — propose, don't auto-run.
