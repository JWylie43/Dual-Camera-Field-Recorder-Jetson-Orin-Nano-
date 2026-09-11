// SPDX-License-Identifier: GPL-2.0
/*
 * rk_cam_defer_enable: apply device-tree overlays at module load time.
 *
 * Why this exists (ROCK 5T + out-of-tree sensor drivers):
 *
 * The Rockchip vendor kernel builds the whole camera pipeline (csi2-dphy,
 * mipi-csi2, rkcif, rkisp) into the kernel, and rkcif/rkisp run a
 * late_initcall ("clear unready subdev") that PERMANENTLY drops any sensor
 * that has not async-registered by the end of kernel init. late_initcalls
 * complete before /init ever runs, so an out-of-tree sensor module (loaded
 * from the rootfs) loses that race on every boot, deterministically: the
 * sensor probes fine but never joins the media graph. Runtime driver
 * rebinding of csi2-dphy oopses the vendor kernel, so the only clean fix
 * without rebuilding the kernel is to keep the pipeline nodes disabled in
 * the boot DT and enable them AFTER the sensor driver has registered.
 *
 * This module is listed in /etc/modules-load.d/ right after the sensor
 * module and applies TWO runtime overlays via of_overlay_fdt_apply():
 *
 *   phase 1: csi2 receivers + CIF + sditf bridges + ISP vir devices.
 *   phase 2 (after a settle delay): the D-PHYs, which match the sensors
 *            and let the async completion cascade run.
 *
 * The split matters: rkcif registers the sditf bridge toward the ISP from
 * a ONE-SHOT kernel thread that wakes on the CIF notifier completing and
 * silently skips (no retry) if the sditf has not yet attached to the CIF.
 * Enabling everything in one overlay completes the CIF notifier
 * milliseconds after the sditf device is created and loses that race;
 * holding the D-PHYs back keeps the notifier incomplete until the sditf
 * bridges are guaranteed attached.
 *
 * The module has no exit on purpose: removing the overlays would tear the
 * live camera pipeline down through vendor unbind paths known to oops.
 */

#include <linux/delay.h>
#include <linux/kernel.h>
#include <linux/kernel_read_file.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/vmalloc.h>

static char *dtbo_p1 = "/lib/firmware/rock5t-cam-enable-p1.dtbo";
module_param(dtbo_p1, charp, 0444);
MODULE_PARM_DESC(dtbo_p1, "Phase-1 overlay (csi2/cif/sditf/isp-vir)");

static char *dtbo_p2 = "/lib/firmware/rock5t-cam-enable-p2.dtbo";
module_param(dtbo_p2, charp, 0444);
MODULE_PARM_DESC(dtbo_p2, "Phase-2 overlay (D-PHYs)");

static unsigned int settle_ms = 200;
module_param(settle_ms, uint, 0444);
MODULE_PARM_DESC(settle_ms, "Delay between the two phases (ms)");

static int apply_dtbo(const char *path)
{
	void *buf = NULL;
	size_t file_size = 0;
	ssize_t bytes;
	int ovcs_id = 0;
	int ret;

	bytes = kernel_read_file_from_path(path, 0, &buf, SZ_1M,
					   &file_size, READING_FIRMWARE);
	if (bytes < 0) {
		pr_err("rk_cam_defer_enable: cannot read %s (%zd)\n",
		       path, bytes);
		return bytes;
	}

	ret = of_overlay_fdt_apply(buf, bytes, &ovcs_id);
	vfree(buf);
	if (ret) {
		pr_err("rk_cam_defer_enable: apply of %s failed (%d)\n",
		       path, ret);
		return ret;
	}

	pr_info("rk_cam_defer_enable: applied %s (ovcs id %d)\n",
		path, ovcs_id);
	return 0;
}

static int __init rk_cam_defer_enable_init(void)
{
	int ret;

	ret = apply_dtbo(dtbo_p1);
	if (ret)
		return ret;

	/* let sditf/vir deferred probes settle before triggering the match */
	msleep(settle_ms);

	ret = apply_dtbo(dtbo_p2);
	if (ret)
		return ret;

	pr_info("rk_cam_defer_enable: camera pipeline enabled\n");
	return 0;
}

module_init(rk_cam_defer_enable_init);

MODULE_SOFTDEP("pre: imx477");
MODULE_DESCRIPTION("Late-enable the RK3588 camera pipeline via runtime DT overlays");
MODULE_AUTHOR("Joe Wylie");
MODULE_LICENSE("GPL");
