# Raspberry Pi HQ Camera (IMX477) on the Orin Nano

This rig was built around the **Arducam B0577** dual kit. This branch adds support
for running the **Raspberry Pi HQ Camera** (Sony **IMX477**, Adafruit #4561) on the
same Orin Nano — two of them, one per CSI port, for a genlocked stereo pair.

Both camera setups live on the board at once. You pick one per boot.

---

## Why switching is a device-tree change (not a driver change)

MIPI/CSI cameras are **not self-describing** — unlike USB, there is no enumeration
handshake on the bus. The sensor is a dumb I²C chip plus a raw pixel stream, and it
won't even answer on I²C until its power rails and reset/power-down GPIOs are driven
the exact way its carrier board is wired. So the whole camera description —
I²C bus + address, lane count, pixel clock, GPIOs, and which resolution/framerate
modes are legal — has to be declared **statically in the device tree**.

Boot flow on the Orin Nano:

1. **UEFI bootloader** reads `/boot/extlinux/extlinux.conf`.
2. It merges the base `FDT` with the `.dtbo` overlay(s) on the `OVERLAYS` line.
3. It hands the finished, merged tree to the kernel.
4. The **kernel** (generic, with drivers for many sensors already built in) binds
   its `imx477` driver to whatever the tree declared, powers the sensor, and reads
   its chip-ID register to confirm.

The kernel never *chooses* the camera — it obeys the tree it was handed. That choice
is locked in at boot, which is why **switching cameras requires a reboot**. No kernel
rebuild is needed to detect the IMX477: the stock `imx477` driver (with its mode
tables and register sequences) already ships in the Arducam kernel this board boots.

| Layer | Role | Change to switch Arducam ↔ Pi? |
|---|---|---|
| Bootloader + `extlinux.conf` | picks & merges the device tree | **Yes** (the overlay swap) |
| Device-tree overlay (`.dtbo`) | describes the wired camera | **Yes** |
| Kernel + `imx477` driver | drives whatever the tree declares | **No** (driver already present) |

---

## The overlays (all already on disk)

Installed with the Arducam kernel, under `/boot/arducam/dts/` (and `/boot/`):

| Overlay | Config |
|---|---|
| `...-imx477-dual.dtbo` | IMX477 on **both** ports, **2-lane** ← the stereo pair |
| `...-imx477-dual-4lane.dtbo` | IMX477 both ports, 4-lane (not our wiring — Pi HQ is 2-lane) |
| `...-imx477-A.dtbo` | single IMX477 on port A (CAM0) |
| `...-imx477-C.dtbo` | single IMX477 on port C (CAM1) |
| `...-imx219-imx477.dtbo` / `...-imx477-imx219.dtbo` | mixed IMX477 + IMX219 |
| `...-arducam-dual.dtbo` | the Arducam B0577 dual kit |

The Orin Nano's 22-pin CSI ports are wired for **2 lanes**, and the Pi HQ board is
only 2-lane anyway — so `imx477-dual` (2-lane) is the correct dual choice.

---

## Switching: `camswitch`

`./camswitch` flips the active overlay and offers to reboot. It backs up
`extlinux.conf` on first run and auto-elevates with sudo.

```bash
sudo ./camswitch pi        # both ports = Raspberry Pi HQ (IMX477), 2-lane
sudo ./camswitch arducam   # back to the Arducam B0577 dual kit
./camswitch status         # show the active overlay
./camswitch list           # list camera overlays on disk
```

Also: `pi-a` / `pi-c` for a single Pi HQ on one port.

Revert to the pre-`camswitch` config at any time:

```bash
sudo cp /boot/extlinux/extlinux.conf.camswitch.bak /boot/extlinux/extlinux.conf && sudo reboot
```

---

## Verify after switching + rebooting

```bash
ls -l /dev/video* 2>/dev/null                       # expect /dev/video0 and /dev/video1
for b in 9 10; do echo "== i2c-$b =="; sudo i2cdetect -y -r $b; done   # expect 0x1a on each
sudo dmesg | grep -iE 'imx477|nvcsi|probe' | tail   # imx477 probe lines
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext          # the sensor's supported modes
```

Reading the result:

- **`0x1a` on both i2c-9 and i2c-10 + `/dev/video0`,`video1`** → detected. Done.
- **`0x1a` absent but dmesg shows imx477 probe/timeout** → sensor powered, not talking.
- **`0x1a` absent, no probe attempts** → suspect the genuine-RPi-HQ **R8 power-down
  resistor** issue (the Pi board holds the sensor in power-down; the Jetson doesn't
  release it). See RidgeRun's "Raspberry Pi HQ camera IMX477 Linux driver for Jetson"
  before desoldering.

Lenses are not required to verify detection/streaming — a bare sensor just gives a
defocused blur. Keep the dust caps on when not testing.

> **Power off the board before connecting/disconnecting CSI ribbon cables** — the
> connectors are not hot-plug safe.

---

## Next: genlock (master/slave sync) — future work

The Pi HQ exposes **XVS** and **GND** solder pads. Tying XVS between the two cameras
(one master, one slave) is the documented hardware sync approach. The catch: NVIDIA's
stock `imx477` driver **does not expose master/slave sync registers** — Raspberry Pi's
own kernel driver has that support, so it will need to be ported into the driver here.
That's a kernel-module build, separate from this overlay switch.

- NVIDIA's `imx477` driver + mode tables: L4T `public_sources` (`nv_imx477.c`,
  `tegra234-camera-imx477-*.dtsi`).
- Raspberry Pi's driver with sync support: `raspberrypi/linux`,
  `drivers/media/i2c/imx477.c` (GPL).

XVS is 1.8 V logic — tie the pads directly between the two cameras only; do not
level-shift to anything else.
