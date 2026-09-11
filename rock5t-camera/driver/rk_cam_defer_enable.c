// SPDX-License-Identifier: GPL-2.0
/*
 * rk_cam_defer_enable: apply a device-tree overlay at module load time.
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
 * That is this module's single job: it is listed in
 * /etc/modules-load.d/ right after the sensor module, reads a runtime
 * overlay (fragments flipping the pipeline nodes to status = "okay") and
 * applies it via of_overlay_fdt_apply(). The built-in pipeline drivers
 * then probe, parse their fwnode notifiers fresh, and match the
 * already-registered sensors immediately. The boot-time clear has long
 * since run (harmlessly, on an empty pipeline) and never runs again.
 *
 * The module has no exit on purpose: removing the overlay would tear the
 * live camera pipeline down through vendor unbind paths known to oops.
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/kernel_read_file.h>
#include <linux/of.h>
#include <linux/vmalloc.h>

static char *dtbo_path = "/lib/firmware/rock5t-cam-enable.dtbo";
module_param(dtbo_path, charp, 0444);
MODULE_PARM_DESC(dtbo_path, "Path to the runtime camera-enable .dtbo");

static int ovcs_id;

static int __init rk_cam_defer_enable_init(void)
{
	void *buf = NULL;
	size_t file_size = 0;
	ssize_t bytes;
	int ret;

	bytes = kernel_read_file_from_path(dtbo_path, 0, &buf, SZ_1M,
					   &file_size, READING_FIRMWARE);
	if (bytes < 0) {
		pr_err("rk_cam_defer_enable: cannot read %s (%zd)\n",
		       dtbo_path, bytes);
		return bytes;
	}

	ret = of_overlay_fdt_apply(buf, bytes, &ovcs_id);
	vfree(buf);
	if (ret) {
		pr_err("rk_cam_defer_enable: overlay apply failed (%d)\n", ret);
		return ret;
	}

	pr_info("rk_cam_defer_enable: applied %s (ovcs id %d), camera pipeline enabled\n",
		dtbo_path, ovcs_id);
	return 0;
}

module_init(rk_cam_defer_enable_init);

MODULE_SOFTDEP("pre: imx477");
MODULE_DESCRIPTION("Late-enable the RK3588 camera pipeline via runtime DT overlay");
MODULE_AUTHOR("Joe Wylie");
MODULE_LICENSE("GPL");
