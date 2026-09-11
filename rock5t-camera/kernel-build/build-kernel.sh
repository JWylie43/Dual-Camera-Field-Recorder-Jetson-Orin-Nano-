#!/bin/bash
# Build the Radxa ROCK 5T vendor kernel with the IMX477 driver built IN
# (CONFIG_VIDEO_IMX477=y), packaged as Debian .debs.
#
# Why builtin: the vendor camera pipeline purges not-yet-registered sensors
# in a late_initcall (before /init), so a loadable sensor module can never
# join the media graph on a stock kernel (see ../../ROCK5T_CAMERA.md,
# bring-up log 2026-09-11). Building the sensor in — exactly like every
# in-tree Rockchip sensor — makes the whole runtime-workaround stack
# (split overlay, rk_cam_defer_enable, modules-load ordering) unnecessary.
#
# Run ON the Rock (native aarch64 build, ~1-2h):
#   ./build-kernel.sh            # clone + patch + build
#   ./build-kernel.sh install    # dpkg -i the built debs + u-boot-update
#
# Re-run after a Radxa kernel update: delete/refresh $SRC (or bump BRANCH)
# and run again. The stock kernel stays installed as a boot-menu fallback.

set -euo pipefail

BRANCH="${BRANCH:-linux-6.1-stan-rkr1}"
SRC="${SRC:-$HOME/radxa-kernel}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"   # rock5t-camera/
LOCALVER="-imx477"
JOBS="$(nproc)"

if [ "${1:-}" = "install" ]; then
    cd "$SRC/.."
    ls linux-image-*imx477*.deb linux-headers-*imx477*.deb
    sudo dpkg -i linux-image-*imx477*.deb linux-headers-*imx477*.deb
    sudo u-boot-update
    echo "Installed. Check /boot/extlinux/extlinux.conf lists the -imx477 kernel, then reboot."
    exit 0
fi

sudo apt-get install -y build-essential bc bison flex libssl-dev libelf-dev \
    debhelper rsync kmod cpio libncurses-dev dwarves

# --- source -----------------------------------------------------------------
if [ ! -d "$SRC" ]; then
    git clone --depth 1 -b "$BRANCH" https://github.com/radxa/kernel.git "$SRC"
fi
cd "$SRC"

# --- inject the driver ------------------------------------------------------
cp "$REPO_DIR/driver/imx477.c" drivers/media/i2c/imx477.c

if ! grep -q "CONFIG_VIDEO_IMX477" drivers/media/i2c/Makefile; then
    sed -i '/obj-$(CONFIG_VIDEO_IMX415) += imx415.o/a obj-$(CONFIG_VIDEO_IMX477) += imx477.o' \
        drivers/media/i2c/Makefile
fi

if ! grep -q "config VIDEO_IMX477" drivers/media/i2c/Kconfig; then
    # insert right before the VIDEO_IMX415 entry, mirroring its shape
    sed -i '/^config VIDEO_IMX415$/i config VIDEO_IMX477\n\ttristate "Sony IMX477 sensor support"\n\tdepends on I2C \&\& VIDEO_DEV\n\tdepends on MEDIA_CAMERA_SUPPORT\n\tselect MEDIA_CONTROLLER\n\tselect VIDEO_V4L2_SUBDEV_API\n\thelp\n\t  This is a Video4Linux2 sensor driver for the Sony\n\t  IMX477 camera (Raspberry Pi HQ Camera), with Rockchip\n\t  RKMODULE support and XVS trigger-mode genlock.\n' \
        drivers/media/i2c/Kconfig
fi

# --- config: running kernel's config + our driver builtin -------------------
cp "/boot/config-$(uname -r)" .config
./scripts/config --enable CONFIG_VIDEO_IMX477
# distinct release string so the package coexists with the stock kernel
./scripts/config --set-str CONFIG_LOCALVERSION "$(uname -r | sed "s/^$(make -s kernelversion 2>/dev/null || echo 6.1.84)//")$LOCALVER"
# don't fail the build over missing signing/debug artifacts from the distro config
./scripts/config --disable CONFIG_MODULE_SIG_ALL || true
./scripts/config --set-str CONFIG_SYSTEM_TRUSTED_KEYS "" || true
./scripts/config --set-str CONFIG_SYSTEM_REVOCATION_KEYS "" || true
./scripts/config --disable CONFIG_DEBUG_INFO_BTF || true
make olddefconfig

grep -E "CONFIG_VIDEO_IMX477|CONFIG_LOCALVERSION=" .config

# --- build ------------------------------------------------------------------
make -j"$JOBS" bindeb-pkg
echo
echo "Build done. Debs are in $SRC/.. — run: $0 install"
