# Kernel build: IMX477 built in (`CONFIG_VIDEO_IMX477=y`)

The Radxa/Rockchip vendor kernel assumes camera sensor drivers are built
into the kernel: the builtin pipeline (csi2-dphy → mipi-csi2 → rkcif →
rkisp) purges any sensor that hasn't async-registered by the end of kernel
init (`late_initcall`, i.e. before `/init` runs) — permanently. A loadable
`imx477.ko` therefore can never join the media graph on a stock kernel, no
matter how it's ordered (softdep, modules-load.d, initramfs: all tried,
all lose by design; full autopsy in `../../ROCK5T_CAMERA.md`, bring-up log
2026-09-11). Building the driver in — exactly like every in-tree Rockchip
sensor — makes the problem not exist.

## Build + install (on the Rock, ~1–2 h)

```
cd ~/orin-recorder/rock5t-camera/kernel-build
./build-kernel.sh              # clone radxa/kernel, inject driver, build debs
./build-kernel.sh install      # dpkg -i + u-boot-update
sudo reboot
```

The new kernel installs alongside the stock one (release string suffix
`-imx477`); the stock kernel remains in the u-boot menu as a fallback.
No reflash. Use the normal full-enable boot overlay
(`../overlay/rock-5t-dual-rpi-hq-imx477.dts`) — rebuild it and the boot
dtbo installs unchanged.

## After the new kernel boots

Remove the stock-kernel workaround stack (harmless but obsolete):

```
sudo rm -f /etc/modules-load.d/imx477.conf /etc/modprobe.d/imx477-order.conf
sudo rm -f /lib/firmware/rock5t-cam-enable*.dtbo
sudo rm -f /lib/modules/6.1.84-8-rk2410/extra/imx477.ko \
           /lib/modules/6.1.84-8-rk2410/extra/rk_cam_defer_enable.ko
```

(Those files belong to the old kernel's module tree anyway — the new
kernel never loads them.)

## Radxa kernel updates

A Radxa kernel update does NOT carry our driver: either hold the kernel
(`sudo apt-mark hold 'linux-image*' 'linux-headers*'`) or re-run this
script against the new source (`rm -rf ~/radxa-kernel`, adjust `BRANCH`
if Radxa moved on, run both steps again).

## Known-broken vendor bits (independent of kernel choice)

- `rkaiq_3A.service` kills its own daemon at start (oneshot wrapper +
  `ExecStop=killall`). Run `sudo rkaiq_3A_server` manually, or fix the
  unit with a drop-in (`Type=forking` or `RemainAfterExit=yes`).
