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

`camswitch pi` also re-enables the stock IMX477 driver (see the gotcha below) —
that's automatic, no manual step needed.

Revert to the pre-`camswitch` config at any time:

```bash
sudo cp /boot/extlinux/extlinux.conf.camswitch.bak /boot/extlinux/extlinux.conf && sudo reboot
```

---

## Gotcha: the Arducam kernel ships the IMX477 driver *disabled*

The Arducam kernel install renames the stock driver
`nv_imx477.ko` → `nv_imx477.ko.bak` so it can't load (it would otherwise clash
with Arducam's own camera drivers). With it disabled, the symptom is confusing:
the overlay is correct and the sensors appear in the device tree
(`/sys/bus/i2c/devices/9-001a`, `10-001a` exist), but **no driver binds**, there's
**no probe attempt in dmesg**, and `i2cdetect` shows `--` (not `UU`) at `0x1a`. It
looks like a dead camera when nothing is actually wrong with the hardware.

Fix (now done automatically by `camswitch pi`):

```bash
sudo mv /lib/modules/$(uname -r)/updates/drivers/media/i2c/nv_imx477.ko.bak \
        /lib/modules/$(uname -r)/updates/drivers/media/i2c/nv_imx477.ko
sudo depmod -a && sudo modprobe nv_imx477
```

After this, `i2cdetect` shows `UU` at `0x1a` (driver claiming the sensor) and
`/dev/video0` + `/dev/video1` appear.

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

## Genlock (master/slave sync) — VALIDATED 2026-09-03

The Pi HQ exposes **XVS** and **GND** solder pads. Tying XVS between the two
cameras (one master/source, one slave/sink) hard-syncs their frame timing.
**Proven on this rig**: XVS pads wired directly (1.8 V logic — no level shift,
no pull-up needed for two boards), then `DUR=60 POKE=1 ./recorder/sync_test.sh`:

- baseline: offset creeping at ~3 µs/s (free-running, crystals ~3 ppm apart —
  ~10 ms drift per hour, a third of a frame, hence genlock)
- at the 20 s register poke the offset **snapped to 0.000 ms and held for the
  rest of the run** — sub-microsecond master/slave lock

The sensor does it all; the driver only has to set four registers (from
Raspberry Pi's GPL `imx477.c`, its `trigger_mode` support) before/at stream-on:

| Reg | Name | Source | Sink |
|---|---|---|---|
| `0x3F0B` | MC_MODE | 1 | 1 |
| `0x3041` | MS_SEL | 1 | 0 |
| `0x3040` | XVS_IO_CTRL | 1 | 0 |
| `0x4B81` | EXTOUT_EN | 1 | 0 |

`sync_test.sh POKE=1` writes these mid-stream with `i2ctransfer -f` (sink first,
then source — **never both source**: two drivers fighting on the line). That's
out-of-spec (datasheet wants standby) and doesn't persist across stream restarts,
so it's the validation tool, not the solution.

**Remaining work**: port those writes into `nv_imx477.c` (keyed per camera:
i2c-9 → source, i2c-10 → sink, e.g. via a DT property) so every stream start
comes up genlocked. Kernel-module build on the Orin. Sources: L4T
`public_sources` (`nv_imx477.c`), `raspberrypi/linux`
`drivers/media/i2c/imx477.c` (GPL).

Debug gotcha hit during wiring: with a camera ribbon mis-seated after the
soldering session, powering the sensors took the whole board's network down
(rail brownout at probe). Symptom: boots and reaches the internet, unreachable
on the LAN, fine with cameras unplugged. Reseat the ribbons.
