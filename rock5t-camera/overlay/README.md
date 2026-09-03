# ROCK 5T dual IMX477 overlay

`rock-5t-dual-rpi-hq-imx477.dts` enables both Pi HQ cameras with XVS genlock
roles: CAM0 connector = source (master), CAM1 connector = sink (slave). Chains
are copied verbatim from Radxa's shipped `rock-5t-cam{0,1}-radxa-camera-8m-219`
overlays (same 2-lane Sony-sensor path), sensor nodes swapped to `imx477@1a`.

## Build (on the Rock, needs kernel headers for the dt-bindings includes)

```bash
H=/usr/src/linux-headers-$(uname -r)
cpp -nostdinc -I "$H/include" -undef -x assembler-with-cpp \
    rock-5t-dual-rpi-hq-imx477.dts \
  | dtc -I dts -O dtb -@ -o rock-5t-dual-rpi-hq-imx477.dtbo
```

(`dtc` warnings about unit names are normal; errors are not.)

## Install

```bash
sudo cp rock-5t-dual-rpi-hq-imx477.dtbo /boot/dtbo/
sudo u-boot-update       # or enable via rsetup -> Overlays
sudo reboot
```

Overlays in `/boot/dtbo/` ending in `.dtbo` are active; rename to
`.dtbo.disabled` to deactivate (same convention as the shipped overlays).

## Depends on

- `../driver/imx477.ko` loaded (the overlay's `compatible = "sony,imx477"` and
  `trigger-mode` property are consumed by it)
- `../iqfiles/imx477_RPI-HQ_default.json` in `/etc/iqfiles/` (matched via
  `rockchip,camera-module-name = "RPI-HQ"`)

## Verify after reboot

```bash
sudo i2cdetect -y -r 3     # 0x1a = UU on i2c3 (CAM0)
sudo i2cdetect -y -r 4     # 0x1a = UU on i2c4 (CAM1)
dmesg | grep -i imx477
v4l2-ctl --list-devices    # rkcif/rkisp video nodes
```
