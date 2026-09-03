// SPDX-License-Identifier: GPL-2.0
/*
 * imx477 camera driver for Rockchip RK3588 vendor kernels
 *
 * Driver framework (probe/power/pinctrl/RKMODULE ioctls) based on the
 * Rockchip imx577 vendor driver:
 *   Copyright (C) 2022 Rockchip Electronics Co., Ltd.
 *
 * IMX477 sensor facts (register tables, control implementation,
 * XVS trigger mode) based on the Raspberry Pi imx477 driver:
 *   Copyright (C) 2020, Raspberry Pi (Trading) Ltd
 *   (itself based on the Sony imx219 camera driver,
 *    Copyright (C) 2019-2020 Raspberry Pi (Trading) Ltd)
 *
 * The Raspberry Pi HQ camera module is a 2-lane MIPI CSI-2 device with an
 * on-board 24MHz oscillator.
 *
 * XVS hardware genlock: optional device tree string property "trigger-mode"
 * on the sensor node selects "source" (drive XVS) or "sink" (slave to XVS).
 * Absent property = free running.
 *
 * V0.0X01.0X00 first version.
 */

// #define DEBUG
#include <linux/clk.h>
#include <linux/device.h>
#include <linux/delay.h>
#include <linux/gpio/consumer.h>
#include <linux/i2c.h>
#include <linux/module.h>
#include <linux/pm_runtime.h>
#include <linux/regulator/consumer.h>
#include <linux/sysfs.h>
#include <linux/slab.h>
#include <linux/version.h>
#include <linux/compat.h>
#include <linux/rk-camera-module.h>
#include <media/media-entity.h>
#include <media/v4l2-async.h>
#include <media/v4l2-ctrls.h>
#include <media/v4l2-subdev.h>
#include <media/v4l2-fwnode.h>
#include <media/v4l2-mediabus.h>
#include <linux/of_graph.h>
#include <linux/pinctrl/consumer.h>

#define DRIVER_VERSION			KERNEL_VERSION(0, 0x01, 0x00)

#ifndef V4L2_CID_DIGITAL_GAIN
#define V4L2_CID_DIGITAL_GAIN		V4L2_CID_GAIN
#endif

static int dpc_enable = 1;
module_param(dpc_enable, int, 0644);
MODULE_PARM_DESC(dpc_enable, "Enable on-sensor DPC");

/* Fallback when the DT "trigger-mode" property is absent: 1=source, 2=sink */
static int trigger_mode;
module_param(trigger_mode, int, 0644);
MODULE_PARM_DESC(trigger_mode, "Set vsync trigger mode: 1=source, 2=sink");

/*
 * All modes run the CSI-2 link at 450MHz (900Mbps/lane, 2 lanes = 1800Mbps
 * total: registers 0x0820/0x0821 = 0x0708 in the mode tables).
 */
#define IMX477_LINK_FREQ_450MHZ		450000000U

/*
 * The IMX477 internal pixel array clock is fixed at 840MHz for all of these
 * modes.  HTS/VTS below are in units of this clock, so expose it as the
 * V4L2 pixel rate to keep fps = pixel_rate / (hts * vts) exact.  (The
 * Rockchip imx577 driver instead derives pixel rate from the link
 * frequency; see NOTES.md for why we deviate.)
 */
#define IMX477_PIXEL_RATE		840000000U

#define IMX477_XVCLK_FREQ		24000000

#define CHIP_ID				0x0477
#define IMX477_REG_CHIP_ID		0x0016

#define IMX477_REG_CTRL_MODE		0x0100
#define IMX477_MODE_SW_STANDBY		0x0
#define IMX477_MODE_STREAMING		BIT(0)

/* Exposure control */
#define IMX477_REG_EXPOSURE		0x0202
#define IMX477_EXPOSURE_OFFSET		22
#define IMX477_EXPOSURE_MIN		4
#define IMX477_EXPOSURE_STEP		1

/* V_TIMING internal */
#define IMX477_REG_VTS			0x0340
#define IMX477_FRAME_LENGTH_MAX		0xffdc

/* Long exposure multiplier */
#define IMX477_LONG_EXP_SHIFT_MAX	7
#define IMX477_LONG_EXP_SHIFT_REG	0x3100

/* Analog gain control: gain = 1024 / (1024 - code), code 0..978 */
#define IMX477_REG_ANALOG_GAIN		0x0204
#define IMX477_ANA_GAIN_MIN		0
#define IMX477_ANA_GAIN_MAX		978
#define IMX477_ANA_GAIN_STEP		1
#define IMX477_ANA_GAIN_DEFAULT		0x0

/* Digital gain control: 8.8 fixed point, 0x0100 = 1.0x */
#define IMX477_REG_DIGITAL_GAIN		0x020e
#define IMX477_DGTL_GAIN_MIN		0x0100
#define IMX477_DGTL_GAIN_MAX		0xffff
#define IMX477_DGTL_GAIN_DEFAULT	0x0100
#define IMX477_DGTL_GAIN_STEP		1

/* Test pattern control */
#define IMX477_REG_TEST_PATTERN		0x0600
#define IMX477_TEST_PATTERN_DISABLE	0
#define IMX477_TEST_PATTERN_SOLID_COLOR	1
#define IMX477_TEST_PATTERN_COLOR_BARS	2
#define IMX477_TEST_PATTERN_GREY_COLOR	3
#define IMX477_TEST_PATTERN_PN9		4

/* On-sensor DPC */
#define IMX477_REG_DPC1			0x0b05
#define IMX477_REG_DPC2			0x0b06

/* XVS trigger mode registers (validated genlock register set) */
#define IMX477_REG_MC_MODE		0x3f0b
#define IMX477_REG_MS_SEL		0x3041
#define IMX477_REG_XVS_IO_CTRL		0x3040
#define IMX477_REG_EXTOUT_EN		0x4b81

#define IMX477_FETCH_EXP_H(VAL)		(((VAL) >> 8) & 0xFF)
#define IMX477_FETCH_EXP_L(VAL)		((VAL) & 0xFF)

#define REG_NULL			0xFFFF

#define IMX477_REG_VALUE_08BIT		1
#define IMX477_REG_VALUE_16BIT		2
#define IMX477_REG_VALUE_24BIT		3

#define OF_CAMERA_PINCTRL_STATE_DEFAULT	"rockchip,camera_default"
#define OF_CAMERA_PINCTRL_STATE_SLEEP	"rockchip,camera_sleep"

#define IMX477_NAME			"imx477"

/* Defaults used when the rockchip,camera-module-* DT properties are absent */
#define IMX477_DEFAULT_MODULE_NAME	"RPI-HQ"
#define IMX477_DEFAULT_LENS_NAME	"default"
#define IMX477_DEFAULT_FACING		"back"

/*
 * Initialisation delay between XCLR low->high and the moment when the sensor
 * can start capture (i.e. can leave software standby), given by T7 in the
 * datasheet is 8ms.  This does include I2C setup time as well.
 */
#define IMX477_XCLR_MIN_DELAY_US	8000
#define IMX477_XCLR_DELAY_RANGE_US	1000

static const char * const imx477_supply_names[] = {
	"avdd",		/* Analog power */
	"dovdd",	/* Digital I/O power */
	"dvdd",		/* Digital core power */
};

#define IMX477_NUM_SUPPLIES ARRAY_SIZE(imx477_supply_names)

enum imx477_trigger_mode {
	IMX477_TRIGGER_MODE_NONE = 0,
	IMX477_TRIGGER_MODE_SOURCE = 1,
	IMX477_TRIGGER_MODE_SINK = 2,
};

struct regval {
	u16 addr;
	u16 val;
};

struct imx477_mode {
	u32 bus_fmt;
	u32 width;
	u32 height;
	struct v4l2_fract max_fps;
	u32 hts_def;
	u32 vts_def;
	u32 exp_def;
	u32 link_freq_idx;
	u32 bpp;
	const struct regval *reg_list;
	u32 hdr_mode;
	u32 vc[PAD_MAX];
};

struct imx477 {
	struct i2c_client	*client;
	struct clk		*xvclk;
	struct gpio_desc	*power_gpio;
	struct gpio_desc	*reset_gpio;
	struct gpio_desc	*pwdn_gpio;
	struct regulator_bulk_data supplies[IMX477_NUM_SUPPLIES];

	struct pinctrl		*pinctrl;
	struct pinctrl_state	*pins_default;
	struct pinctrl_state	*pins_sleep;

	struct v4l2_subdev	subdev;
	struct media_pad	pad;
	struct v4l2_ctrl_handler ctrl_handler;
	struct v4l2_ctrl	*exposure;
	struct v4l2_ctrl	*anal_gain;
	struct v4l2_ctrl	*digi_gain;
	struct v4l2_ctrl	*hblank;
	struct v4l2_ctrl	*vblank;
	struct v4l2_ctrl	*pixel_rate;
	struct v4l2_ctrl	*link_freq;
	struct v4l2_ctrl	*test_pattern;
	struct mutex		mutex;
	bool			streaming;
	bool			power_on;
	bool			common_regs_written;
	const struct imx477_mode *cur_mode;
	u32			cur_vts;
	/* Current long exposure factor in use. Set through V4L2_CID_VBLANK */
	unsigned int		long_exp_shift;
	/* XVS genlock role, from DT "trigger-mode" (or module param) */
	enum imx477_trigger_mode xvs_trigger_mode;
	u32			module_index;
	const char		*module_facing;
	const char		*module_name;
	const char		*len_name;
	struct v4l2_fwnode_endpoint bus_cfg;
	struct rkmodule_awb_cfg	awb_cfg;
	struct rkmodule_lsc_cfg	lsc_cfg;
};

#define to_imx477(sd) container_of(sd, struct imx477, subdev)

/*
 * Common (one-time) register set, written once after each power on.
 * Taken verbatim from the Raspberry Pi driver's mode_common_regs[].
 * Ends with 12-bit RAW format defaults and 2-lane CSI (0x0114 = lanes - 1).
 */
static __maybe_unused const struct regval imx477_common_regs[] = {
	{0x0136, 0x18},
	{0x0137, 0x00},
	{0x0138, 0x01},
	{0xe000, 0x00},
	{0xe07a, 0x01},
	{0x0808, 0x02},
	{0x4ae9, 0x18},
	{0x4aea, 0x08},
	{0xf61c, 0x04},
	{0xf61e, 0x04},
	{0x4ae9, 0x21},
	{0x4aea, 0x80},
	{0x38a8, 0x1f},
	{0x38a9, 0xff},
	{0x38aa, 0x1f},
	{0x38ab, 0xff},
	{0x55d4, 0x00},
	{0x55d5, 0x00},
	{0x55d6, 0x07},
	{0x55d7, 0xff},
	{0x55e8, 0x07},
	{0x55e9, 0xff},
	{0x55ea, 0x00},
	{0x55eb, 0x00},
	{0x574c, 0x07},
	{0x574d, 0xff},
	{0x574e, 0x00},
	{0x574f, 0x00},
	{0x5754, 0x00},
	{0x5755, 0x00},
	{0x5756, 0x07},
	{0x5757, 0xff},
	{0x5973, 0x04},
	{0x5974, 0x01},
	{0x5d13, 0xc3},
	{0x5d14, 0x58},
	{0x5d15, 0xa3},
	{0x5d16, 0x1d},
	{0x5d17, 0x65},
	{0x5d18, 0x8c},
	{0x5d1a, 0x06},
	{0x5d1b, 0xa9},
	{0x5d1c, 0x45},
	{0x5d1d, 0x3a},
	{0x5d1e, 0xab},
	{0x5d1f, 0x15},
	{0x5d21, 0x0e},
	{0x5d22, 0x52},
	{0x5d23, 0xaa},
	{0x5d24, 0x7d},
	{0x5d25, 0x57},
	{0x5d26, 0xa8},
	{0x5d37, 0x5a},
	{0x5d38, 0x5a},
	{0x5d77, 0x7f},
	{0x7b75, 0x0e},
	{0x7b76, 0x0b},
	{0x7b77, 0x08},
	{0x7b78, 0x0a},
	{0x7b79, 0x47},
	{0x7b7c, 0x00},
	{0x7b7d, 0x00},
	{0x8d1f, 0x00},
	{0x8d27, 0x00},
	{0x9004, 0x03},
	{0x9200, 0x50},
	{0x9201, 0x6c},
	{0x9202, 0x71},
	{0x9203, 0x00},
	{0x9204, 0x71},
	{0x9205, 0x01},
	{0x9371, 0x6a},
	{0x9373, 0x6a},
	{0x9375, 0x64},
	{0x991a, 0x00},
	{0x996b, 0x8c},
	{0x996c, 0x64},
	{0x996d, 0x50},
	{0x9a4c, 0x0d},
	{0x9a4d, 0x0d},
	{0xa001, 0x0a},
	{0xa003, 0x0a},
	{0xa005, 0x0a},
	{0xa006, 0x01},
	{0xa007, 0xc0},
	{0xa009, 0xc0},
	{0x3d8a, 0x01},
	{0x4421, 0x04},
	{0x7b3b, 0x01},
	{0x7b4c, 0x00},
	{0x9905, 0x00},
	{0x9907, 0x00},
	{0x9909, 0x00},
	{0x990b, 0x00},
	{0x9944, 0x3c},
	{0x9947, 0x3c},
	{0x994a, 0x8c},
	{0x994b, 0x50},
	{0x994c, 0x1b},
	{0x994d, 0x8c},
	{0x994e, 0x50},
	{0x994f, 0x1b},
	{0x9950, 0x8c},
	{0x9951, 0x1b},
	{0x9952, 0x0a},
	{0x9953, 0x8c},
	{0x9954, 0x1b},
	{0x9955, 0x0a},
	{0x9a13, 0x04},
	{0x9a14, 0x04},
	{0x9a19, 0x00},
	{0x9a1c, 0x04},
	{0x9a1d, 0x04},
	{0x9a26, 0x05},
	{0x9a27, 0x05},
	{0x9a2c, 0x01},
	{0x9a2d, 0x03},
	{0x9a2f, 0x05},
	{0x9a30, 0x05},
	{0x9a41, 0x00},
	{0x9a46, 0x00},
	{0x9a47, 0x00},
	{0x9c17, 0x35},
	{0x9c1d, 0x31},
	{0x9c29, 0x50},
	{0x9c3b, 0x2f},
	{0x9c41, 0x6b},
	{0x9c47, 0x2d},
	{0x9c4d, 0x40},
	{0x9c6b, 0x00},
	{0x9c71, 0xc8},
	{0x9c73, 0x32},
	{0x9c75, 0x04},
	{0x9c7d, 0x2d},
	{0x9c83, 0x40},
	{0x9c94, 0x3f},
	{0x9c95, 0x3f},
	{0x9c96, 0x3f},
	{0x9c97, 0x00},
	{0x9c98, 0x00},
	{0x9c99, 0x00},
	{0x9c9a, 0x3f},
	{0x9c9b, 0x3f},
	{0x9c9c, 0x3f},
	{0x9ca0, 0x0f},
	{0x9ca1, 0x0f},
	{0x9ca2, 0x0f},
	{0x9ca3, 0x00},
	{0x9ca4, 0x00},
	{0x9ca5, 0x00},
	{0x9ca6, 0x1e},
	{0x9ca7, 0x1e},
	{0x9ca8, 0x1e},
	{0x9ca9, 0x00},
	{0x9caa, 0x00},
	{0x9cab, 0x00},
	{0x9cac, 0x09},
	{0x9cad, 0x09},
	{0x9cae, 0x09},
	{0x9cbd, 0x50},
	{0x9cbf, 0x50},
	{0x9cc1, 0x50},
	{0x9cc3, 0x40},
	{0x9cc5, 0x40},
	{0x9cc7, 0x40},
	{0x9cc9, 0x0a},
	{0x9ccb, 0x0a},
	{0x9ccd, 0x0a},
	{0x9d17, 0x35},
	{0x9d1d, 0x31},
	{0x9d29, 0x50},
	{0x9d3b, 0x2f},
	{0x9d41, 0x6b},
	{0x9d47, 0x42},
	{0x9d4d, 0x5a},
	{0x9d6b, 0x00},
	{0x9d71, 0xc8},
	{0x9d73, 0x32},
	{0x9d75, 0x04},
	{0x9d7d, 0x42},
	{0x9d83, 0x5a},
	{0x9d94, 0x3f},
	{0x9d95, 0x3f},
	{0x9d96, 0x3f},
	{0x9d97, 0x00},
	{0x9d98, 0x00},
	{0x9d99, 0x00},
	{0x9d9a, 0x3f},
	{0x9d9b, 0x3f},
	{0x9d9c, 0x3f},
	{0x9d9d, 0x1f},
	{0x9d9e, 0x1f},
	{0x9d9f, 0x1f},
	{0x9da0, 0x0f},
	{0x9da1, 0x0f},
	{0x9da2, 0x0f},
	{0x9da3, 0x00},
	{0x9da4, 0x00},
	{0x9da5, 0x00},
	{0x9da6, 0x1e},
	{0x9da7, 0x1e},
	{0x9da8, 0x1e},
	{0x9da9, 0x00},
	{0x9daa, 0x00},
	{0x9dab, 0x00},
	{0x9dac, 0x09},
	{0x9dad, 0x09},
	{0x9dae, 0x09},
	{0x9dc9, 0x0a},
	{0x9dcb, 0x0a},
	{0x9dcd, 0x0a},
	{0x9e17, 0x35},
	{0x9e1d, 0x31},
	{0x9e29, 0x50},
	{0x9e3b, 0x2f},
	{0x9e41, 0x6b},
	{0x9e47, 0x2d},
	{0x9e4d, 0x40},
	{0x9e6b, 0x00},
	{0x9e71, 0xc8},
	{0x9e73, 0x32},
	{0x9e75, 0x04},
	{0x9e94, 0x0f},
	{0x9e95, 0x0f},
	{0x9e96, 0x0f},
	{0x9e97, 0x00},
	{0x9e98, 0x00},
	{0x9e99, 0x00},
	{0x9ea0, 0x0f},
	{0x9ea1, 0x0f},
	{0x9ea2, 0x0f},
	{0x9ea3, 0x00},
	{0x9ea4, 0x00},
	{0x9ea5, 0x00},
	{0x9ea6, 0x3f},
	{0x9ea7, 0x3f},
	{0x9ea8, 0x3f},
	{0x9ea9, 0x00},
	{0x9eaa, 0x00},
	{0x9eab, 0x00},
	{0x9eac, 0x09},
	{0x9ead, 0x09},
	{0x9eae, 0x09},
	{0x9ec9, 0x0a},
	{0x9ecb, 0x0a},
	{0x9ecd, 0x0a},
	{0x9f17, 0x35},
	{0x9f1d, 0x31},
	{0x9f29, 0x50},
	{0x9f3b, 0x2f},
	{0x9f41, 0x6b},
	{0x9f47, 0x42},
	{0x9f4d, 0x5a},
	{0x9f6b, 0x00},
	{0x9f71, 0xc8},
	{0x9f73, 0x32},
	{0x9f75, 0x04},
	{0x9f94, 0x0f},
	{0x9f95, 0x0f},
	{0x9f96, 0x0f},
	{0x9f97, 0x00},
	{0x9f98, 0x00},
	{0x9f99, 0x00},
	{0x9f9a, 0x2f},
	{0x9f9b, 0x2f},
	{0x9f9c, 0x2f},
	{0x9f9d, 0x00},
	{0x9f9e, 0x00},
	{0x9f9f, 0x00},
	{0x9fa0, 0x0f},
	{0x9fa1, 0x0f},
	{0x9fa2, 0x0f},
	{0x9fa3, 0x00},
	{0x9fa4, 0x00},
	{0x9fa5, 0x00},
	{0x9fa6, 0x1e},
	{0x9fa7, 0x1e},
	{0x9fa8, 0x1e},
	{0x9fa9, 0x00},
	{0x9faa, 0x00},
	{0x9fab, 0x00},
	{0x9fac, 0x09},
	{0x9fad, 0x09},
	{0x9fae, 0x09},
	{0x9fc9, 0x0a},
	{0x9fcb, 0x0a},
	{0x9fcd, 0x0a},
	{0xa14b, 0xff},
	{0xa151, 0x0c},
	{0xa153, 0x50},
	{0xa155, 0x02},
	{0xa157, 0x00},
	{0xa1ad, 0xff},
	{0xa1b3, 0x0c},
	{0xa1b5, 0x50},
	{0xa1b9, 0x00},
	{0xa24b, 0xff},
	{0xa257, 0x00},
	{0xa2ad, 0xff},
	{0xa2b9, 0x00},
	{0xb21f, 0x04},
	{0xb35c, 0x00},
	{0xb35e, 0x08},
	{0x0112, 0x0c},
	{0x0113, 0x0c},
	{0x0114, 0x01},
	{0x0350, 0x00},
	{0xbcf1, 0x02},
	{0x3ff9, 0x01},
	{REG_NULL, 0x00},
};

/*
 * 12 mpix 10fps, RAW12, full pixel array.
 * From the Raspberry Pi driver's mode_4056x3040_regs[], with the RAW format
 * and lane count registers prepended so mode switches away from the 10-bit
 * mode restore 12-bit output.
 */
static __maybe_unused const struct regval imx477_linear_12bit_4056x3040_10fps_regs[] = {
	{0x0112, 0x0c},
	{0x0113, 0x0c},
	{0x0114, 0x01},
	{0x0342, 0x5d},
	{0x0343, 0xc0},
	{0x0344, 0x00},
	{0x0345, 0x00},
	{0x0346, 0x00},
	{0x0347, 0x00},
	{0x0348, 0x0f},
	{0x0349, 0xd7},
	{0x034a, 0x0b},
	{0x034b, 0xdf},
	{0x00e3, 0x00},
	{0x00e4, 0x00},
	{0x00fc, 0x0a},
	{0x00fd, 0x0a},
	{0x00fe, 0x0a},
	{0x00ff, 0x0a},
	{0x0220, 0x00},
	{0x0221, 0x11},
	{0x0381, 0x01},
	{0x0383, 0x01},
	{0x0385, 0x01},
	{0x0387, 0x01},
	{0x0900, 0x00},
	{0x0901, 0x11},
	{0x0902, 0x02},
	{0x3140, 0x02},
	{0x3c00, 0x00},
	{0x3c01, 0x03},
	{0x3c02, 0xa2},
	{0x3f0d, 0x01},
	{0x5748, 0x07},
	{0x5749, 0xff},
	{0x574a, 0x00},
	{0x574b, 0x00},
	{0x7b75, 0x0a},
	{0x7b76, 0x0c},
	{0x7b77, 0x07},
	{0x7b78, 0x06},
	{0x7b79, 0x3c},
	{0x7b53, 0x01},
	{0x9369, 0x5a},
	{0x936b, 0x55},
	{0x936d, 0x28},
	{0x9304, 0x00},
	{0x9305, 0x00},
	{0x9e9a, 0x2f},
	{0x9e9b, 0x2f},
	{0x9e9c, 0x2f},
	{0x9e9d, 0x00},
	{0x9e9e, 0x00},
	{0x9e9f, 0x00},
	{0xa2a9, 0x60},
	{0xa2b7, 0x00},
	{0x0401, 0x00},
	{0x0404, 0x00},
	{0x0405, 0x10},
	{0x0408, 0x00},
	{0x0409, 0x00},
	{0x040a, 0x00},
	{0x040b, 0x00},
	{0x040c, 0x0f},
	{0x040d, 0xd8},
	{0x040e, 0x0b},
	{0x040f, 0xe0},
	{0x034c, 0x0f},
	{0x034d, 0xd8},
	{0x034e, 0x0b},
	{0x034f, 0xe0},
	{0x0301, 0x05},
	{0x0303, 0x02},
	{0x0305, 0x04},
	{0x0306, 0x01},
	{0x0307, 0x5e},
	{0x0309, 0x0c},
	{0x030b, 0x02},
	{0x030d, 0x02},
	{0x030e, 0x00},
	{0x030f, 0x96},
	{0x0310, 0x01},
	{0x0820, 0x07},
	{0x0821, 0x08},
	{0x0822, 0x00},
	{0x0823, 0x00},
	{0x080a, 0x00},
	{0x080b, 0x7f},
	{0x080c, 0x00},
	{0x080d, 0x4f},
	{0x080e, 0x00},
	{0x080f, 0x77},
	{0x0810, 0x00},
	{0x0811, 0x5f},
	{0x0812, 0x00},
	{0x0813, 0x57},
	{0x0814, 0x00},
	{0x0815, 0x4f},
	{0x0816, 0x01},
	{0x0817, 0x27},
	{0x0818, 0x00},
	{0x0819, 0x3f},
	{0xe04c, 0x00},
	{0xe04d, 0x7f},
	{0xe04e, 0x00},
	{0xe04f, 0x1f},
	{0x3e20, 0x01},
	{0x3e37, 0x00},
	{0x3f50, 0x00},
	{0x3f56, 0x02},
	{0x3f57, 0xae},
	{REG_NULL, 0x00},
};

/*
 * 2x2 binned, 40fps max, RAW12.
 * From the Raspberry Pi driver's mode_2028x1520_regs[], with RAW format and
 * lane count registers prepended (see above).
 */
static __maybe_unused const struct regval imx477_linear_12bit_2028x1520_40fps_regs[] = {
	{0x0112, 0x0c},
	{0x0113, 0x0c},
	{0x0114, 0x01},
	{0x0342, 0x31},
	{0x0343, 0xc4},
	{0x0344, 0x00},
	{0x0345, 0x00},
	{0x0346, 0x00},
	{0x0347, 0x00},
	{0x0348, 0x0f},
	{0x0349, 0xd7},
	{0x034a, 0x0b},
	{0x034b, 0xdf},
	{0x0220, 0x00},
	{0x0221, 0x11},
	{0x0381, 0x01},
	{0x0383, 0x01},
	{0x0385, 0x01},
	{0x0387, 0x01},
	{0x0900, 0x01},
	{0x0901, 0x22},
	{0x0902, 0x02},
	{0x3140, 0x02},
	{0x3c00, 0x00},
	{0x3c01, 0x03},
	{0x3c02, 0xa2},
	{0x3f0d, 0x01},
	{0x5748, 0x07},
	{0x5749, 0xff},
	{0x574a, 0x00},
	{0x574b, 0x00},
	{0x7b53, 0x01},
	{0x9369, 0x73},
	{0x936b, 0x64},
	{0x936d, 0x5f},
	{0x9304, 0x00},
	{0x9305, 0x00},
	{0x9e9a, 0x2f},
	{0x9e9b, 0x2f},
	{0x9e9c, 0x2f},
	{0x9e9d, 0x00},
	{0x9e9e, 0x00},
	{0x9e9f, 0x00},
	{0xa2a9, 0x60},
	{0xa2b7, 0x00},
	{0x0401, 0x00},
	{0x0404, 0x00},
	{0x0405, 0x20},
	{0x0408, 0x00},
	{0x0409, 0x00},
	{0x040a, 0x00},
	{0x040b, 0x00},
	{0x040c, 0x0f},
	{0x040d, 0xd8},
	{0x040e, 0x0b},
	{0x040f, 0xe0},
	{0x034c, 0x07},
	{0x034d, 0xec},
	{0x034e, 0x05},
	{0x034f, 0xf0},
	{0x0301, 0x05},
	{0x0303, 0x02},
	{0x0305, 0x04},
	{0x0306, 0x01},
	{0x0307, 0x5e},
	{0x0309, 0x0c},
	{0x030b, 0x02},
	{0x030d, 0x02},
	{0x030e, 0x00},
	{0x030f, 0x96},
	{0x0310, 0x01},
	{0x0820, 0x07},
	{0x0821, 0x08},
	{0x0822, 0x00},
	{0x0823, 0x00},
	{0x080a, 0x00},
	{0x080b, 0x7f},
	{0x080c, 0x00},
	{0x080d, 0x4f},
	{0x080e, 0x00},
	{0x080f, 0x77},
	{0x0810, 0x00},
	{0x0811, 0x5f},
	{0x0812, 0x00},
	{0x0813, 0x57},
	{0x0814, 0x00},
	{0x0815, 0x4f},
	{0x0816, 0x01},
	{0x0817, 0x27},
	{0x0818, 0x00},
	{0x0819, 0x3f},
	{0xe04c, 0x00},
	{0xe04d, 0x7f},
	{0xe04e, 0x00},
	{0xe04f, 0x1f},
	{0x3e20, 0x01},
	{0x3e37, 0x00},
	{0x3f50, 0x00},
	{0x3f56, 0x01},
	{0x3f57, 0x6c},
	{REG_NULL, 0x00},
};

/*
 * 2x2 binned and cropped, 120fps max, RAW10.
 * From the Raspberry Pi driver's mode_1332x990_regs[] (this table already
 * carries its own 0x0112/0x0113/0x0114 values).
 */
static __maybe_unused const struct regval imx477_linear_10bit_1332x990_120fps_regs[] = {
	{0x420b, 0x01},
	{0x990c, 0x00},
	{0x990d, 0x08},
	{0x9956, 0x8c},
	{0x9957, 0x64},
	{0x9958, 0x50},
	{0x9a48, 0x06},
	{0x9a49, 0x06},
	{0x9a4a, 0x06},
	{0x9a4b, 0x06},
	{0x9a4c, 0x06},
	{0x9a4d, 0x06},
	{0x0112, 0x0a},
	{0x0113, 0x0a},
	{0x0114, 0x01},
	{0x0342, 0x1a},
	{0x0343, 0x08},
	{0x0340, 0x04},
	{0x0341, 0x1a},
	{0x0344, 0x00},
	{0x0345, 0x00},
	{0x0346, 0x02},
	{0x0347, 0x10},
	{0x0348, 0x0f},
	{0x0349, 0xd7},
	{0x034a, 0x09},
	{0x034b, 0xcf},
	{0x00e3, 0x00},
	{0x00e4, 0x00},
	{0x00fc, 0x0a},
	{0x00fd, 0x0a},
	{0x00fe, 0x0a},
	{0x00ff, 0x0a},
	{0xe013, 0x00},
	{0x0220, 0x00},
	{0x0221, 0x11},
	{0x0381, 0x01},
	{0x0383, 0x01},
	{0x0385, 0x01},
	{0x0387, 0x01},
	{0x0900, 0x01},
	{0x0901, 0x22},
	{0x0902, 0x02},
	{0x3140, 0x02},
	{0x3c00, 0x00},
	{0x3c01, 0x01},
	{0x3c02, 0x9c},
	{0x3f0d, 0x00},
	{0x5748, 0x00},
	{0x5749, 0x00},
	{0x574a, 0x00},
	{0x574b, 0xa4},
	{0x7b75, 0x0e},
	{0x7b76, 0x09},
	{0x7b77, 0x08},
	{0x7b78, 0x06},
	{0x7b79, 0x34},
	{0x7b53, 0x00},
	{0x9369, 0x73},
	{0x936b, 0x64},
	{0x936d, 0x5f},
	{0x9304, 0x03},
	{0x9305, 0x80},
	{0x9e9a, 0x2f},
	{0x9e9b, 0x2f},
	{0x9e9c, 0x2f},
	{0x9e9d, 0x00},
	{0x9e9e, 0x00},
	{0x9e9f, 0x00},
	{0xa2a9, 0x27},
	{0xa2b7, 0x03},
	{0x0401, 0x00},
	{0x0404, 0x00},
	{0x0405, 0x10},
	{0x0408, 0x01},
	{0x0409, 0x5c},
	{0x040a, 0x00},
	{0x040b, 0x00},
	{0x040c, 0x05},
	{0x040d, 0x34},
	{0x040e, 0x03},
	{0x040f, 0xde},
	{0x034c, 0x05},
	{0x034d, 0x34},
	{0x034e, 0x03},
	{0x034f, 0xde},
	{0x0301, 0x05},
	{0x0303, 0x02},
	{0x0305, 0x02},
	{0x0306, 0x00},
	{0x0307, 0xaf},
	{0x0309, 0x0a},
	{0x030b, 0x02},
	{0x030d, 0x02},
	{0x030e, 0x00},
	{0x030f, 0x96},
	{0x0310, 0x01},
	{0x0820, 0x07},
	{0x0821, 0x08},
	{0x0822, 0x00},
	{0x0823, 0x00},
	{0x080a, 0x00},
	{0x080b, 0x7f},
	{0x080c, 0x00},
	{0x080d, 0x4f},
	{0x080e, 0x00},
	{0x080f, 0x77},
	{0x0810, 0x00},
	{0x0811, 0x5f},
	{0x0812, 0x00},
	{0x0813, 0x57},
	{0x0814, 0x00},
	{0x0815, 0x4f},
	{0x0816, 0x01},
	{0x0817, 0x27},
	{0x0818, 0x00},
	{0x0819, 0x3f},
	{0xe04c, 0x00},
	{0xe04d, 0x5f},
	{0xe04e, 0x00},
	{0xe04f, 0x1f},
	{0x3e20, 0x01},
	{0x3e37, 0x00},
	{0x3f50, 0x00},
	{0x3f56, 0x00},
	{0x3f57, 0xbf},
	{REG_NULL, 0x00},
};

/*
 * HTS values are in units of the 840MHz internal pixel clock (as in the
 * Raspberry Pi driver's line_length_pix).  VTS defaults are computed from
 * the mode's max frame rate: vts = IMX477_PIXEL_RATE / (fps * hts).
 */
static const struct imx477_mode supported_modes[] = {
	{
		/* 12MPix full-res 10fps mode */
		.width = 4056,
		.height = 3040,
		.max_fps = {
			.numerator = 10000,
			.denominator = 100000,
		},
		.exp_def = 0x0640,
		.hts_def = 0x5dc0,	/* 24000 */
		.vts_def = 0x0dac,	/* 3500 -> 10.0 fps */
		.bpp = 12,
		.bus_fmt = MEDIA_BUS_FMT_SRGGB12_1X12,
		.reg_list = imx477_linear_12bit_4056x3040_10fps_regs,
		.hdr_mode = NO_HDR,
		.link_freq_idx = 0,
		.vc[PAD0] = 0,
	},
	{
		/* 2x2 binned 40fps mode */
		.width = 2028,
		.height = 1520,
		.max_fps = {
			.numerator = 10000,
			.denominator = 400000,
		},
		.exp_def = 0x0640,
		.hts_def = 0x31c4,	/* 12740 */
		.vts_def = 0x0670,	/* 1648 -> 40.0 fps */
		.bpp = 12,
		.bus_fmt = MEDIA_BUS_FMT_SRGGB12_1X12,
		.reg_list = imx477_linear_12bit_2028x1520_40fps_regs,
		.hdr_mode = NO_HDR,
		.link_freq_idx = 0,
		.vc[PAD0] = 0,
	},
	{
		/* 2x2 binned and cropped 120fps mode */
		.width = 1332,
		.height = 990,
		.max_fps = {
			.numerator = 10000,
			.denominator = 1200000,
		},
		.exp_def = 0x0400,
		.hts_def = 0x1a08,	/* 6664 */
		.vts_def = 0x041a,	/* 1050 -> 120.0 fps */
		.bpp = 10,
		.bus_fmt = MEDIA_BUS_FMT_SRGGB10_1X10,
		.reg_list = imx477_linear_10bit_1332x990_120fps_regs,
		.hdr_mode = NO_HDR,
		.link_freq_idx = 0,
		.vc[PAD0] = 0,
	},
};

static const s64 link_freq_items[] = {
	IMX477_LINK_FREQ_450MHZ,
};

static const char * const imx477_test_pattern_menu[] = {
	"Disabled",
	"Color Bars",
	"Solid Color",
	"Grey Color Bars",
	"PN9",
};

static const int imx477_test_pattern_val[] = {
	IMX477_TEST_PATTERN_DISABLE,
	IMX477_TEST_PATTERN_COLOR_BARS,
	IMX477_TEST_PATTERN_SOLID_COLOR,
	IMX477_TEST_PATTERN_GREY_COLOR,
	IMX477_TEST_PATTERN_PN9,
};

/* Write registers up to 4 at a time */
static int imx477_write_reg(struct i2c_client *client, u16 reg,
			    u32 len, u32 val)
{
	u32 buf_i, val_i;
	u8 buf[6];
	u8 *val_p;
	__be32 val_be;

	dev_dbg(&client->dev, "write reg(0x%x val:0x%x)!\n", reg, val);

	if (len > 4)
		return -EINVAL;

	buf[0] = reg >> 8;
	buf[1] = reg & 0xff;

	val_be = cpu_to_be32(val);
	val_p = (u8 *)&val_be;
	buf_i = 2;
	val_i = 4 - len;

	while (val_i < 4)
		buf[buf_i++] = val_p[val_i++];

	if (i2c_master_send(client, buf, len + 2) != len + 2)
		return -EIO;

	return 0;
}

static int imx477_write_array(struct i2c_client *client,
			      const struct regval *regs)
{
	u32 i;
	int ret = 0;

	for (i = 0; ret == 0 && regs[i].addr != REG_NULL; i++)
		ret = imx477_write_reg(client, regs[i].addr,
				       IMX477_REG_VALUE_08BIT,
				       regs[i].val);

	return ret;
}

/* Read registers up to 4 at a time */
static int imx477_read_reg(struct i2c_client *client, u16 reg,
			   unsigned int len, u32 *val)
{
	struct i2c_msg msgs[2];
	u8 *data_be_p;
	__be32 data_be = 0;
	__be16 reg_addr_be = cpu_to_be16(reg);
	int ret;

	if (len > 4 || !len)
		return -EINVAL;

	data_be_p = (u8 *)&data_be;
	/* Write register address */
	msgs[0].addr = client->addr;
	msgs[0].flags = 0;
	msgs[0].len = 2;
	msgs[0].buf = (u8 *)&reg_addr_be;

	/* Read data from register */
	msgs[1].addr = client->addr;
	msgs[1].flags = I2C_M_RD;
	msgs[1].len = len;
	msgs[1].buf = &data_be_p[4 - len];

	ret = i2c_transfer(client->adapter, msgs, ARRAY_SIZE(msgs));
	if (ret != ARRAY_SIZE(msgs))
		return -EIO;

	*val = be32_to_cpu(data_be);

	return 0;
}

static int imx477_get_reso_dist(const struct imx477_mode *mode,
				struct v4l2_mbus_framefmt *framefmt)
{
	return abs(mode->width - framefmt->width) +
	       abs(mode->height - framefmt->height);
}

static const struct imx477_mode *
imx477_find_best_fit(struct v4l2_subdev_format *fmt)
{
	struct v4l2_mbus_framefmt *framefmt = &fmt->format;
	int dist;
	int cur_best_fit = 0;
	int cur_best_fit_dist = -1;
	unsigned int i;

	for (i = 0; i < ARRAY_SIZE(supported_modes); i++) {
		dist = imx477_get_reso_dist(&supported_modes[i], framefmt);
		if (cur_best_fit_dist == -1 || dist < cur_best_fit_dist) {
			cur_best_fit_dist = dist;
			cur_best_fit = i;
		}
	}

	return &supported_modes[cur_best_fit];
}

static int imx477_set_fmt(struct v4l2_subdev *sd,
			  struct v4l2_subdev_state *sd_state,
			  struct v4l2_subdev_format *fmt)
{
	struct imx477 *imx477 = to_imx477(sd);
	const struct imx477_mode *mode;
	s64 h_blank, vblank_def, vblank_max;

	mutex_lock(&imx477->mutex);

	mode = imx477_find_best_fit(fmt);
	fmt->format.code = mode->bus_fmt;
	fmt->format.width = mode->width;
	fmt->format.height = mode->height;
	fmt->format.field = V4L2_FIELD_NONE;
	if (fmt->which == V4L2_SUBDEV_FORMAT_TRY) {
#ifdef CONFIG_VIDEO_V4L2_SUBDEV_API
		*v4l2_subdev_get_try_format(sd, sd_state, fmt->pad) = fmt->format;
#else
		mutex_unlock(&imx477->mutex);
		return -ENOTTY;
#endif
	} else {
		imx477->cur_mode = mode;
		imx477->cur_vts = imx477->cur_mode->vts_def;
		h_blank = mode->hts_def - mode->width;
		__v4l2_ctrl_modify_range(imx477->hblank, h_blank,
					 h_blank, 1, h_blank);
		vblank_def = mode->vts_def - mode->height;
		vblank_max = ((1 << IMX477_LONG_EXP_SHIFT_MAX) *
			      IMX477_FRAME_LENGTH_MAX) - mode->height;
		__v4l2_ctrl_modify_range(imx477->vblank, vblank_def,
					 vblank_max, 1, vblank_def);
		__v4l2_ctrl_s_ctrl(imx477->link_freq,
				   mode->link_freq_idx);
	}

	mutex_unlock(&imx477->mutex);

	return 0;
}

static int imx477_get_fmt(struct v4l2_subdev *sd,
			  struct v4l2_subdev_state *sd_state,
			  struct v4l2_subdev_format *fmt)
{
	struct imx477 *imx477 = to_imx477(sd);
	const struct imx477_mode *mode = imx477->cur_mode;

	mutex_lock(&imx477->mutex);
	if (fmt->which == V4L2_SUBDEV_FORMAT_TRY) {
#ifdef CONFIG_VIDEO_V4L2_SUBDEV_API
		fmt->format = *v4l2_subdev_get_try_format(sd, sd_state, fmt->pad);
#else
		mutex_unlock(&imx477->mutex);
		return -ENOTTY;
#endif
	} else {
		fmt->format.width = mode->width;
		fmt->format.height = mode->height;
		fmt->format.code = mode->bus_fmt;
		fmt->format.field = V4L2_FIELD_NONE;
		fmt->reserved[0] = mode->vc[PAD0];
	}
	mutex_unlock(&imx477->mutex);

	return 0;
}

static int imx477_enum_mbus_code(struct v4l2_subdev *sd,
				 struct v4l2_subdev_state *sd_state,
				 struct v4l2_subdev_mbus_code_enum *code)
{
	struct imx477 *imx477 = to_imx477(sd);

	if (code->index != 0)
		return -EINVAL;
	code->code = imx477->cur_mode->bus_fmt;

	return 0;
}

static int imx477_enum_frame_sizes(struct v4l2_subdev *sd,
				   struct v4l2_subdev_state *sd_state,
				   struct v4l2_subdev_frame_size_enum *fse)
{
	if (fse->index >= ARRAY_SIZE(supported_modes))
		return -EINVAL;

	if (fse->code != supported_modes[fse->index].bus_fmt)
		return -EINVAL;

	fse->min_width  = supported_modes[fse->index].width;
	fse->max_width  = supported_modes[fse->index].width;
	fse->max_height = supported_modes[fse->index].height;
	fse->min_height = supported_modes[fse->index].height;

	return 0;
}

static int imx477_enable_test_pattern(struct imx477 *imx477, u32 pattern)
{
	return imx477_write_reg(imx477->client,
				IMX477_REG_TEST_PATTERN,
				IMX477_REG_VALUE_16BIT,
				imx477_test_pattern_val[pattern]);
}

static int imx477_g_frame_interval(struct v4l2_subdev *sd,
				   struct v4l2_subdev_frame_interval *fi)
{
	struct imx477 *imx477 = to_imx477(sd);
	const struct imx477_mode *mode = imx477->cur_mode;

	fi->interval = mode->max_fps;

	return 0;
}

static void imx477_get_module_inf(struct imx477 *imx477,
				  struct rkmodule_inf *inf)
{
	memset(inf, 0, sizeof(*inf));
	strscpy(inf->base.sensor, IMX477_NAME, sizeof(inf->base.sensor));
	strscpy(inf->base.module, imx477->module_name,
		sizeof(inf->base.module));
	strscpy(inf->base.lens, imx477->len_name, sizeof(inf->base.lens));
}

static void imx477_set_awb_cfg(struct imx477 *imx477,
			       struct rkmodule_awb_cfg *cfg)
{
	mutex_lock(&imx477->mutex);
	memcpy(&imx477->awb_cfg, cfg, sizeof(*cfg));
	mutex_unlock(&imx477->mutex);
}

static void imx477_set_lsc_cfg(struct imx477 *imx477,
			       struct rkmodule_lsc_cfg *cfg)
{
	mutex_lock(&imx477->mutex);
	memcpy(&imx477->lsc_cfg, cfg, sizeof(*cfg));
	mutex_unlock(&imx477->mutex);
}

static int imx477_get_channel_info(struct imx477 *imx477,
				   struct rkmodule_channel_info *ch_info)
{
	if (ch_info->index < PAD0 || ch_info->index >= PAD_MAX)
		return -EINVAL;
	ch_info->vc = imx477->cur_mode->vc[ch_info->index];
	ch_info->width = imx477->cur_mode->width;
	ch_info->height = imx477->cur_mode->height;
	ch_info->bus_fmt = imx477->cur_mode->bus_fmt;

	return 0;
}

static long imx477_ioctl(struct v4l2_subdev *sd, unsigned int cmd, void *arg)
{
	struct imx477 *imx477 = to_imx477(sd);
	struct rkmodule_hdr_cfg *hdr;
	struct rkmodule_channel_info *ch_info;
	long ret = 0;
	u32 i, h, w;
	u32 stream = 0;
	const struct imx477_mode *mode;

	switch (cmd) {
	case RKMODULE_GET_MODULE_INFO:
		imx477_get_module_inf(imx477, (struct rkmodule_inf *)arg);
		break;
	case RKMODULE_GET_HDR_CFG:
		hdr = (struct rkmodule_hdr_cfg *)arg;
		hdr->esp.mode = HDR_NORMAL_VC;
		hdr->hdr_mode = imx477->cur_mode->hdr_mode;
		break;
	case RKMODULE_SET_HDR_CFG:
		hdr = (struct rkmodule_hdr_cfg *)arg;
		w = imx477->cur_mode->width;
		h = imx477->cur_mode->height;
		for (i = 0; i < ARRAY_SIZE(supported_modes); i++) {
			if (w == supported_modes[i].width &&
			    h == supported_modes[i].height &&
			    supported_modes[i].hdr_mode == hdr->hdr_mode) {
				imx477->cur_mode = &supported_modes[i];
				break;
			}
		}
		if (i == ARRAY_SIZE(supported_modes)) {
			dev_err(&imx477->client->dev,
				"not find hdr mode:%d %dx%d config\n",
				hdr->hdr_mode, w, h);
			ret = -EINVAL;
		} else {
			mode = imx477->cur_mode;
			imx477->cur_vts = mode->vts_def;
			w = mode->hts_def - mode->width;
			h = mode->vts_def - mode->height;
			mutex_lock(&imx477->mutex);
			__v4l2_ctrl_modify_range(imx477->hblank, w, w, 1, w);
			__v4l2_ctrl_modify_range(imx477->vblank, h,
				((1 << IMX477_LONG_EXP_SHIFT_MAX) *
				 IMX477_FRAME_LENGTH_MAX) - mode->height,
				1, h);
			__v4l2_ctrl_s_ctrl(imx477->link_freq,
					   mode->link_freq_idx);
			mutex_unlock(&imx477->mutex);
		}
		break;
	case RKMODULE_AWB_CFG:
		imx477_set_awb_cfg(imx477, (struct rkmodule_awb_cfg *)arg);
		break;
	case RKMODULE_LSC_CFG:
		imx477_set_lsc_cfg(imx477, (struct rkmodule_lsc_cfg *)arg);
		break;
	case RKMODULE_SET_QUICK_STREAM:

		stream = *((u32 *)arg);

		if (stream)
			ret = imx477_write_reg(imx477->client,
				IMX477_REG_CTRL_MODE,
				IMX477_REG_VALUE_08BIT,
				IMX477_MODE_STREAMING);
		else
			ret = imx477_write_reg(imx477->client,
				IMX477_REG_CTRL_MODE,
				IMX477_REG_VALUE_08BIT,
				IMX477_MODE_SW_STANDBY);
		break;
	case RKMODULE_GET_CHANNEL_INFO:
		ch_info = (struct rkmodule_channel_info *)arg;
		ret = imx477_get_channel_info(imx477, ch_info);
		break;
	default:
		ret = -ENOIOCTLCMD;
		break;
	}

	return ret;
}

#ifdef CONFIG_COMPAT
static long imx477_compat_ioctl32(struct v4l2_subdev *sd,
				  unsigned int cmd, unsigned long arg)
{
	void __user *up = compat_ptr(arg);
	struct rkmodule_inf *inf;
	struct rkmodule_awb_cfg *cfg;
	struct rkmodule_hdr_cfg *hdr;
	struct rkmodule_lsc_cfg *lsc_cfg;
	struct rkmodule_channel_info *ch_info;
	long ret = 0;
	u32 stream = 0;

	switch (cmd) {
	case RKMODULE_GET_MODULE_INFO:
		inf = kzalloc(sizeof(*inf), GFP_KERNEL);
		if (!inf) {
			ret = -ENOMEM;
			return ret;
		}

		ret = imx477_ioctl(sd, cmd, inf);
		if (!ret) {
			ret = copy_to_user(up, inf, sizeof(*inf));
			if (ret)
				ret = -EFAULT;
		}
		kfree(inf);
		break;
	case RKMODULE_AWB_CFG:
		cfg = kzalloc(sizeof(*cfg), GFP_KERNEL);
		if (!cfg) {
			ret = -ENOMEM;
			return ret;
		}

		ret = copy_from_user(cfg, up, sizeof(*cfg));
		if (!ret)
			ret = imx477_ioctl(sd, cmd, cfg);
		else
			ret = -EFAULT;
		kfree(cfg);
		break;
	case RKMODULE_GET_HDR_CFG:
		hdr = kzalloc(sizeof(*hdr), GFP_KERNEL);
		if (!hdr) {
			ret = -ENOMEM;
			return ret;
		}

		ret = imx477_ioctl(sd, cmd, hdr);
		if (!ret) {
			if (copy_to_user(up, hdr, sizeof(*hdr))) {
				kfree(hdr);
				return -EFAULT;
			}
		}
		kfree(hdr);
		break;
	case RKMODULE_SET_HDR_CFG:
		hdr = kzalloc(sizeof(*hdr), GFP_KERNEL);
		if (!hdr) {
			ret = -ENOMEM;
			return ret;
		}

		if (copy_from_user(hdr, up, sizeof(*hdr))) {
			kfree(hdr);
			return -EFAULT;
		}
		ret = imx477_ioctl(sd, cmd, hdr);
		kfree(hdr);
		break;
	case RKMODULE_LSC_CFG:
		lsc_cfg = kzalloc(sizeof(*lsc_cfg), GFP_KERNEL);
		if (!lsc_cfg) {
			ret = -ENOMEM;
			return ret;
		}

		ret = copy_from_user(lsc_cfg, up, sizeof(*lsc_cfg));
		if (!ret)
			ret = imx477_ioctl(sd, cmd, lsc_cfg);
		else
			ret = -EFAULT;
		kfree(lsc_cfg);
		break;
	case RKMODULE_SET_QUICK_STREAM:
		ret = copy_from_user(&stream, up, sizeof(u32));
		if (!ret)
			ret = imx477_ioctl(sd, cmd, &stream);
		else
			ret = -EFAULT;
		break;
	case RKMODULE_GET_CHANNEL_INFO:
		ch_info = kzalloc(sizeof(*ch_info), GFP_KERNEL);
		if (!ch_info) {
			ret = -ENOMEM;
			return ret;
		}

		ret = imx477_ioctl(sd, cmd, ch_info);
		if (!ret) {
			ret = copy_to_user(up, ch_info, sizeof(*ch_info));
			if (ret)
				ret = -EFAULT;
		}
		kfree(ch_info);
		break;
	default:
		ret = -ENOIOCTLCMD;
		break;
	}

	return ret;
}
#endif

/*
 * XVS hardware genlock (validated register set):
 *              MC_MODE  MS_SEL  XVS_IO_CTRL  EXTOUT_EN
 *              (0x3f0b) (0x3041)  (0x3040)   (0x4b81)
 * none            0        1         0           0
 * source          1        1         1           1
 * sink            1        0         0           0
 *
 * Must be written while the sensor is in software standby, after the mode
 * register table and before the streaming bit is set.
 */
static int imx477_apply_trigger_mode(struct imx477 *imx477)
{
	struct i2c_client *client = imx477->client;
	enum imx477_trigger_mode tm = imx477->xvs_trigger_mode;
	u32 mc_mode, ms_sel, xvs_io_ctrl, extout_en;
	int ret;

	switch (tm) {
	case IMX477_TRIGGER_MODE_SOURCE:
		mc_mode = 1;
		ms_sel = 1;
		xvs_io_ctrl = 1;
		extout_en = 1;
		break;
	case IMX477_TRIGGER_MODE_SINK:
		mc_mode = 1;
		ms_sel = 0;
		xvs_io_ctrl = 0;
		extout_en = 0;
		break;
	case IMX477_TRIGGER_MODE_NONE:
	default:
		mc_mode = 0;
		ms_sel = 1;
		xvs_io_ctrl = 0;
		extout_en = 0;
		break;
	}

	dev_info(&client->dev, "XVS trigger mode: %s\n",
		 tm == IMX477_TRIGGER_MODE_SOURCE ? "source" :
		 tm == IMX477_TRIGGER_MODE_SINK ? "sink" : "none");

	ret = imx477_write_reg(client, IMX477_REG_MC_MODE,
			       IMX477_REG_VALUE_08BIT, mc_mode);
	ret |= imx477_write_reg(client, IMX477_REG_MS_SEL,
				IMX477_REG_VALUE_08BIT, ms_sel);
	ret |= imx477_write_reg(client, IMX477_REG_XVS_IO_CTRL,
				IMX477_REG_VALUE_08BIT, xvs_io_ctrl);
	ret |= imx477_write_reg(client, IMX477_REG_EXTOUT_EN,
				IMX477_REG_VALUE_08BIT, extout_en);

	return ret;
}

static int __imx477_start_stream(struct imx477 *imx477)
{
	int ret;

	if (!imx477->common_regs_written) {
		ret = imx477_write_array(imx477->client, imx477_common_regs);
		if (ret) {
			dev_err(&imx477->client->dev,
				"failed to set common registers\n");
			return ret;
		}
		imx477->common_regs_written = true;
	}

	ret = imx477_write_array(imx477->client, imx477->cur_mode->reg_list);
	if (ret)
		return ret;

	/* Set on-sensor DPC */
	imx477_write_reg(imx477->client, IMX477_REG_DPC1,
			 IMX477_REG_VALUE_08BIT, !!dpc_enable);
	imx477_write_reg(imx477->client, IMX477_REG_DPC2,
			 IMX477_REG_VALUE_08BIT, !!dpc_enable);

	/* In case these controls are set before streaming */
	ret = __v4l2_ctrl_handler_setup(&imx477->ctrl_handler);
	if (ret)
		return ret;

	/* XVS genlock role - must be set in standby, before streaming */
	ret = imx477_apply_trigger_mode(imx477);
	if (ret)
		return ret;

	return imx477_write_reg(imx477->client,
				IMX477_REG_CTRL_MODE,
				IMX477_REG_VALUE_08BIT,
				IMX477_MODE_STREAMING);
}

static int __imx477_stop_stream(struct imx477 *imx477)
{
	int ret;

	ret = imx477_write_reg(imx477->client,
			       IMX477_REG_CTRL_MODE,
			       IMX477_REG_VALUE_08BIT,
			       IMX477_MODE_SW_STANDBY);

	/* Stop driving XVS out (there is still a weak pull-up) */
	imx477_write_reg(imx477->client, IMX477_REG_EXTOUT_EN,
			 IMX477_REG_VALUE_08BIT, 0);

	return ret;
}

static int imx477_s_stream(struct v4l2_subdev *sd, int on)
{
	struct imx477 *imx477 = to_imx477(sd);
	struct i2c_client *client = imx477->client;
	int ret = 0;

	dev_info(&client->dev, "%s: on: %d, %dx%d@%d, bpp: %d\n",
		 __func__, on, imx477->cur_mode->width,
		 imx477->cur_mode->height,
		 DIV_ROUND_CLOSEST(imx477->cur_mode->max_fps.denominator,
				   imx477->cur_mode->max_fps.numerator),
		 imx477->cur_mode->bpp);

	mutex_lock(&imx477->mutex);
	on = !!on;
	if (on == imx477->streaming)
		goto unlock_and_return;

	if (on) {
		ret = pm_runtime_get_sync(&client->dev);
		if (ret < 0) {
			pm_runtime_put_noidle(&client->dev);
			goto unlock_and_return;
		}

		ret = __imx477_start_stream(imx477);
		if (ret) {
			v4l2_err(sd, "start stream failed while write regs\n");
			pm_runtime_put(&client->dev);
			goto unlock_and_return;
		}
	} else {
		__imx477_stop_stream(imx477);
		pm_runtime_put(&client->dev);
	}

	imx477->streaming = on;

unlock_and_return:
	mutex_unlock(&imx477->mutex);

	return ret;
}

static int imx477_s_power(struct v4l2_subdev *sd, int on)
{
	struct imx477 *imx477 = to_imx477(sd);
	struct i2c_client *client = imx477->client;
	int ret = 0;

	mutex_lock(&imx477->mutex);

	/* If the power state is not modified - no work to do. */
	if (imx477->power_on == !!on)
		goto unlock_and_return;

	if (on) {
		ret = pm_runtime_get_sync(&client->dev);
		if (ret < 0) {
			pm_runtime_put_noidle(&client->dev);
			goto unlock_and_return;
		}

		if (!imx477->common_regs_written) {
			ret = imx477_write_array(imx477->client,
						 imx477_common_regs);
			if (ret) {
				v4l2_err(sd, "could not set init registers\n");
				pm_runtime_put_noidle(&client->dev);
				goto unlock_and_return;
			}
			imx477->common_regs_written = true;
		}

		imx477->power_on = true;
	} else {
		pm_runtime_put(&client->dev);
		imx477->power_on = false;
	}

unlock_and_return:
	mutex_unlock(&imx477->mutex);

	return ret;
}

static int __imx477_power_on(struct imx477 *imx477)
{
	int ret;
	struct device *dev = &imx477->client->dev;

	if (!IS_ERR(imx477->power_gpio))
		gpiod_set_value_cansleep(imx477->power_gpio, 1);

	usleep_range(1000, 2000);

	if (!IS_ERR_OR_NULL(imx477->pins_default)) {
		ret = pinctrl_select_state(imx477->pinctrl,
					   imx477->pins_default);
		if (ret < 0)
			dev_err(dev, "could not set pins\n");
	}

	/*
	 * The RPi HQ camera module carries its own 24MHz oscillator; the
	 * clock is optional and may be a fixed-clock (or absent entirely).
	 */
	if (imx477->xvclk) {
		ret = clk_set_rate(imx477->xvclk, IMX477_XVCLK_FREQ);
		if (ret < 0)
			dev_warn(dev, "Failed to set xvclk rate (24MHz)\n");
		if (clk_get_rate(imx477->xvclk) != IMX477_XVCLK_FREQ)
			dev_warn(dev, "xvclk mismatched, modes are based on 24MHz\n");
		ret = clk_prepare_enable(imx477->xvclk);
		if (ret < 0) {
			dev_err(dev, "Failed to enable xvclk\n");
			return ret;
		}
	}

	if (!IS_ERR(imx477->reset_gpio))
		gpiod_set_value_cansleep(imx477->reset_gpio, 0);

	ret = regulator_bulk_enable(IMX477_NUM_SUPPLIES, imx477->supplies);
	if (ret < 0) {
		dev_err(dev, "Failed to enable regulators\n");
		goto disable_clk;
	}

	if (!IS_ERR(imx477->reset_gpio))
		gpiod_set_value_cansleep(imx477->reset_gpio, 1);

	usleep_range(500, 1000);
	if (!IS_ERR(imx477->pwdn_gpio))
		gpiod_set_value_cansleep(imx477->pwdn_gpio, 1);

	/* T7 in the datasheet: XCLR high to streaming-capable is 8ms */
	usleep_range(IMX477_XCLR_MIN_DELAY_US,
		     IMX477_XCLR_MIN_DELAY_US + IMX477_XCLR_DELAY_RANGE_US);

	return 0;

disable_clk:
	if (imx477->xvclk)
		clk_disable_unprepare(imx477->xvclk);

	return ret;
}

static void __imx477_power_off(struct imx477 *imx477)
{
	int ret;
	struct device *dev = &imx477->client->dev;

	if (!IS_ERR(imx477->pwdn_gpio))
		gpiod_set_value_cansleep(imx477->pwdn_gpio, 0);
	if (imx477->xvclk)
		clk_disable_unprepare(imx477->xvclk);
	if (!IS_ERR(imx477->reset_gpio))
		gpiod_set_value_cansleep(imx477->reset_gpio, 0);

	if (!IS_ERR_OR_NULL(imx477->pins_sleep)) {
		ret = pinctrl_select_state(imx477->pinctrl,
					   imx477->pins_sleep);
		if (ret < 0)
			dev_dbg(dev, "could not set pins\n");
	}
	if (!IS_ERR(imx477->power_gpio))
		gpiod_set_value_cansleep(imx477->power_gpio, 0);

	regulator_bulk_disable(IMX477_NUM_SUPPLIES, imx477->supplies);

	/* Force reprogramming of the common registers when powered up again */
	imx477->common_regs_written = false;
}

static int imx477_runtime_resume(struct device *dev)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct v4l2_subdev *sd = i2c_get_clientdata(client);
	struct imx477 *imx477 = to_imx477(sd);

	return __imx477_power_on(imx477);
}

static int imx477_runtime_suspend(struct device *dev)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct v4l2_subdev *sd = i2c_get_clientdata(client);
	struct imx477 *imx477 = to_imx477(sd);

	__imx477_power_off(imx477);

	return 0;
}

#ifdef CONFIG_VIDEO_V4L2_SUBDEV_API
static int imx477_open(struct v4l2_subdev *sd, struct v4l2_subdev_fh *fh)
{
	struct imx477 *imx477 = to_imx477(sd);
	struct v4l2_mbus_framefmt *try_fmt =
				v4l2_subdev_get_try_format(sd, fh->state, 0);
	const struct imx477_mode *def_mode = &supported_modes[0];

	mutex_lock(&imx477->mutex);
	/* Initialize try_fmt */
	try_fmt->width = def_mode->width;
	try_fmt->height = def_mode->height;
	try_fmt->code = def_mode->bus_fmt;
	try_fmt->field = V4L2_FIELD_NONE;

	mutex_unlock(&imx477->mutex);
	/* No crop or compose */

	return 0;
}
#endif

static int imx477_enum_frame_interval(struct v4l2_subdev *sd,
				      struct v4l2_subdev_state *sd_state,
				      struct v4l2_subdev_frame_interval_enum *fie)
{
	if (fie->index >= ARRAY_SIZE(supported_modes))
		return -EINVAL;

	fie->code = supported_modes[fie->index].bus_fmt;
	fie->width = supported_modes[fie->index].width;
	fie->height = supported_modes[fie->index].height;
	fie->interval = supported_modes[fie->index].max_fps;
	fie->reserved[0] = supported_modes[fie->index].hdr_mode;

	return 0;
}

static int imx477_g_mbus_config(struct v4l2_subdev *sd, unsigned int pad,
				struct v4l2_mbus_config *config)
{
	struct imx477 *imx477 = to_imx477(sd);

	config->type = V4L2_MBUS_CSI2_DPHY;
	config->bus.mipi_csi2 = imx477->bus_cfg.bus.mipi_csi2;

	return 0;
}

#define CROP_START(SRC, DST) (((SRC) - (DST)) / 2 / 4 * 4)
#define DST_WIDTH_4048 4048

/*
 * The full pixel array is 4056 wide, which is not 16-aligned; report a
 * 4048-wide crop for the ISP in that case (same trick as the Rockchip
 * imx577 driver).
 */
static int imx477_get_selection(struct v4l2_subdev *sd,
				struct v4l2_subdev_state *sd_state,
				struct v4l2_subdev_selection *sel)
{
	struct imx477 *imx477 = to_imx477(sd);

	if (sel->target == V4L2_SEL_TGT_CROP_BOUNDS) {
		if (imx477->cur_mode->width == 4056) {
			sel->r.left = CROP_START(imx477->cur_mode->width,
						 DST_WIDTH_4048);
			sel->r.width = DST_WIDTH_4048;
			sel->r.top = CROP_START(imx477->cur_mode->height,
						imx477->cur_mode->height);
			sel->r.height = imx477->cur_mode->height;
		} else {
			sel->r.left = CROP_START(imx477->cur_mode->width,
						 imx477->cur_mode->width);
			sel->r.width = imx477->cur_mode->width;
			sel->r.top = CROP_START(imx477->cur_mode->height,
						imx477->cur_mode->height);
			sel->r.height = imx477->cur_mode->height;
		}
		return 0;
	}

	return -EINVAL;
}

static const struct dev_pm_ops imx477_pm_ops = {
	SET_RUNTIME_PM_OPS(imx477_runtime_suspend,
			   imx477_runtime_resume, NULL)
};

#ifdef CONFIG_VIDEO_V4L2_SUBDEV_API
static const struct v4l2_subdev_internal_ops imx477_internal_ops = {
	.open = imx477_open,
};
#endif

static const struct v4l2_subdev_core_ops imx477_core_ops = {
	.s_power = imx477_s_power,
	.ioctl = imx477_ioctl,
#ifdef CONFIG_COMPAT
	.compat_ioctl32 = imx477_compat_ioctl32,
#endif
};

static const struct v4l2_subdev_video_ops imx477_video_ops = {
	.s_stream = imx477_s_stream,
	.g_frame_interval = imx477_g_frame_interval,
};

static const struct v4l2_subdev_pad_ops imx477_pad_ops = {
	.enum_mbus_code = imx477_enum_mbus_code,
	.enum_frame_size = imx477_enum_frame_sizes,
	.enum_frame_interval = imx477_enum_frame_interval,
	.get_fmt = imx477_get_fmt,
	.set_fmt = imx477_set_fmt,
	.get_selection = imx477_get_selection,
	.get_mbus_config = imx477_g_mbus_config,
};

static const struct v4l2_subdev_ops imx477_subdev_ops = {
	.core	= &imx477_core_ops,
	.video	= &imx477_video_ops,
	.pad	= &imx477_pad_ops,
};

/*
 * VTS values above IMX477_FRAME_LENGTH_MAX are reached with the long
 * exposure shift register (each step halves the frame length register and
 * doubles the effective line period).
 */
static int imx477_set_frame_length(struct imx477 *imx477, unsigned int val)
{
	int ret;

	imx477->long_exp_shift = 0;

	while (val > IMX477_FRAME_LENGTH_MAX) {
		imx477->long_exp_shift++;
		val >>= 1;
	}

	ret = imx477_write_reg(imx477->client, IMX477_REG_VTS,
			       IMX477_REG_VALUE_16BIT, val);
	if (ret)
		return ret;

	return imx477_write_reg(imx477->client, IMX477_LONG_EXP_SHIFT_REG,
				IMX477_REG_VALUE_08BIT,
				imx477->long_exp_shift);
}

static int imx477_set_ctrl(struct v4l2_ctrl *ctrl)
{
	struct imx477 *imx477 = container_of(ctrl->handler,
					     struct imx477, ctrl_handler);
	struct i2c_client *client = imx477->client;
	s64 max;
	int ret = 0;

	/* Propagate change of current control to all related controls */
	switch (ctrl->id) {
	case V4L2_CID_VBLANK:
		/* Update max exposure while meeting expected vblanking */
		max = imx477->cur_mode->height + ctrl->val -
		      IMX477_EXPOSURE_OFFSET;
		__v4l2_ctrl_modify_range(imx477->exposure,
					 imx477->exposure->minimum, max,
					 imx477->exposure->step,
					 imx477->exposure->default_value);
		break;
	}

	if (!pm_runtime_get_if_in_use(&client->dev))
		return 0;

	switch (ctrl->id) {
	case V4L2_CID_EXPOSURE:
		ret = imx477_write_reg(imx477->client,
				       IMX477_REG_EXPOSURE,
				       IMX477_REG_VALUE_16BIT,
				       ctrl->val >> imx477->long_exp_shift);
		dev_dbg(&client->dev, "set exposure 0x%x\n", ctrl->val);
		break;
	case V4L2_CID_ANALOGUE_GAIN:
		/* gain = 1024 / (1024 - code), code = 0..978 */
		ret = imx477_write_reg(imx477->client,
				       IMX477_REG_ANALOG_GAIN,
				       IMX477_REG_VALUE_16BIT,
				       ctrl->val);
		dev_dbg(&client->dev, "set analog gain 0x%x\n", ctrl->val);
		break;
	case V4L2_CID_DIGITAL_GAIN:
		ret = imx477_write_reg(imx477->client,
				       IMX477_REG_DIGITAL_GAIN,
				       IMX477_REG_VALUE_16BIT,
				       ctrl->val);
		dev_dbg(&client->dev, "set digital gain 0x%x\n", ctrl->val);
		break;
	case V4L2_CID_VBLANK:
		ret = imx477_set_frame_length(imx477,
					      imx477->cur_mode->height +
					      ctrl->val);
		imx477->cur_vts = imx477->cur_mode->height + ctrl->val;
		break;
	case V4L2_CID_TEST_PATTERN:
		ret = imx477_enable_test_pattern(imx477, ctrl->val);
		break;
	default:
		dev_warn(&client->dev, "%s Unhandled id:0x%x, val:0x%x\n",
			 __func__, ctrl->id, ctrl->val);
		break;
	}

	pm_runtime_put(&client->dev);

	return ret;
}

static const struct v4l2_ctrl_ops imx477_ctrl_ops = {
	.s_ctrl = imx477_set_ctrl,
};

static int imx477_initialize_controls(struct imx477 *imx477)
{
	const struct imx477_mode *mode;
	struct v4l2_ctrl_handler *handler;
	s64 exposure_max, vblank_def, vblank_max;
	u32 h_blank;
	int ret;

	handler = &imx477->ctrl_handler;
	mode = imx477->cur_mode;
	ret = v4l2_ctrl_handler_init(handler, 8);
	if (ret)
		return ret;
	handler->lock = &imx477->mutex;

	imx477->link_freq = v4l2_ctrl_new_int_menu(handler, NULL,
				V4L2_CID_LINK_FREQ,
				ARRAY_SIZE(link_freq_items) - 1, 0,
				link_freq_items);
	__v4l2_ctrl_s_ctrl(imx477->link_freq, mode->link_freq_idx);

	/*
	 * The sensor pixel array clock is 840MHz in all these modes; hts/vts
	 * are in units of it, so use it as the fixed pixel rate.
	 */
	imx477->pixel_rate = v4l2_ctrl_new_std(handler, NULL,
					       V4L2_CID_PIXEL_RATE,
					       0, IMX477_PIXEL_RATE,
					       1, IMX477_PIXEL_RATE);

	h_blank = mode->hts_def - mode->width;
	imx477->hblank = v4l2_ctrl_new_std(handler, NULL, V4L2_CID_HBLANK,
					   h_blank, h_blank, 1, h_blank);
	if (imx477->hblank)
		imx477->hblank->flags |= V4L2_CTRL_FLAG_READ_ONLY;

	vblank_def = mode->vts_def - mode->height;
	vblank_max = ((1 << IMX477_LONG_EXP_SHIFT_MAX) *
		      IMX477_FRAME_LENGTH_MAX) - mode->height;
	imx477->vblank = v4l2_ctrl_new_std(handler, &imx477_ctrl_ops,
					   V4L2_CID_VBLANK, vblank_def,
					   vblank_max, 1, vblank_def);
	imx477->cur_vts = mode->vts_def;

	exposure_max = mode->vts_def - IMX477_EXPOSURE_OFFSET;
	imx477->exposure = v4l2_ctrl_new_std(handler, &imx477_ctrl_ops,
					     V4L2_CID_EXPOSURE,
					     IMX477_EXPOSURE_MIN,
					     exposure_max,
					     IMX477_EXPOSURE_STEP,
					     mode->exp_def);

	imx477->anal_gain = v4l2_ctrl_new_std(handler, &imx477_ctrl_ops,
					      V4L2_CID_ANALOGUE_GAIN,
					      IMX477_ANA_GAIN_MIN,
					      IMX477_ANA_GAIN_MAX,
					      IMX477_ANA_GAIN_STEP,
					      IMX477_ANA_GAIN_DEFAULT);

	imx477->digi_gain = v4l2_ctrl_new_std(handler, &imx477_ctrl_ops,
					      V4L2_CID_DIGITAL_GAIN,
					      IMX477_DGTL_GAIN_MIN,
					      IMX477_DGTL_GAIN_MAX,
					      IMX477_DGTL_GAIN_STEP,
					      IMX477_DGTL_GAIN_DEFAULT);

	imx477->test_pattern = v4l2_ctrl_new_std_menu_items(handler,
				&imx477_ctrl_ops, V4L2_CID_TEST_PATTERN,
				ARRAY_SIZE(imx477_test_pattern_menu) - 1,
				0, 0, imx477_test_pattern_menu);

	if (handler->error) {
		ret = handler->error;
		dev_err(&imx477->client->dev,
			"Failed to init controls(%d)\n", ret);
		goto err_free_handler;
	}

	imx477->subdev.ctrl_handler = handler;

	return 0;

err_free_handler:
	v4l2_ctrl_handler_free(handler);

	return ret;
}

static int imx477_check_sensor_id(struct imx477 *imx477,
				  struct i2c_client *client)
{
	struct device *dev = &imx477->client->dev;
	u32 id = 0;
	int ret;

	ret = imx477_read_reg(client, IMX477_REG_CHIP_ID,
			      IMX477_REG_VALUE_16BIT, &id);
	if (id != CHIP_ID) {
		dev_err(dev, "Unexpected sensor id(%04x), ret(%d)\n", id, ret);
		return -ENODEV;
	}

	dev_info(dev, "Detected Sony imx%04x sensor\n", CHIP_ID);

	return 0;
}

static int imx477_configure_regulators(struct imx477 *imx477)
{
	unsigned int i;

	for (i = 0; i < IMX477_NUM_SUPPLIES; i++)
		imx477->supplies[i].supply = imx477_supply_names[i];

	return devm_regulator_bulk_get(&imx477->client->dev,
				       IMX477_NUM_SUPPLIES,
				       imx477->supplies);
}

static void imx477_parse_trigger_mode(struct imx477 *imx477)
{
	struct device *dev = &imx477->client->dev;
	struct device_node *node = dev->of_node;
	const char *tm_str = NULL;
	int ret;

	imx477->xvs_trigger_mode = IMX477_TRIGGER_MODE_NONE;

	ret = of_property_read_string(node, "trigger-mode", &tm_str);
	if (!ret) {
		if (!strcmp(tm_str, "source"))
			imx477->xvs_trigger_mode = IMX477_TRIGGER_MODE_SOURCE;
		else if (!strcmp(tm_str, "sink"))
			imx477->xvs_trigger_mode = IMX477_TRIGGER_MODE_SINK;
		else
			dev_warn(dev,
				 "unknown trigger-mode '%s', using none\n",
				 tm_str);
		return;
	}

	/* No DT property; honour the module parameter as a fallback */
	if (trigger_mode == 1)
		imx477->xvs_trigger_mode = IMX477_TRIGGER_MODE_SOURCE;
	else if (trigger_mode == 2)
		imx477->xvs_trigger_mode = IMX477_TRIGGER_MODE_SINK;
}

static int imx477_probe(struct i2c_client *client,
			const struct i2c_device_id *id)
{
	struct device *dev = &client->dev;
	struct device_node *node = dev->of_node;
	struct imx477 *imx477;
	struct v4l2_subdev *sd;
	struct device_node *endpoint;
	char facing[2];
	int ret;

	dev_info(dev, "driver version: %02x.%02x.%02x",
		 DRIVER_VERSION >> 16,
		 (DRIVER_VERSION & 0xff00) >> 8,
		 DRIVER_VERSION & 0x00ff);

	imx477 = devm_kzalloc(dev, sizeof(*imx477), GFP_KERNEL);
	if (!imx477)
		return -ENOMEM;

	/*
	 * The rockchip,camera-module-* properties are optional here (unlike
	 * the imx577 vendor driver): rkaiq uses them to match IQ files, but
	 * the driver still works for raw capture without them.
	 */
	ret = of_property_read_u32(node, RKMODULE_CAMERA_MODULE_INDEX,
				   &imx477->module_index);
	if (ret)
		imx477->module_index = 0;
	ret = of_property_read_string(node, RKMODULE_CAMERA_MODULE_FACING,
				      &imx477->module_facing);
	if (ret)
		imx477->module_facing = IMX477_DEFAULT_FACING;
	ret = of_property_read_string(node, RKMODULE_CAMERA_MODULE_NAME,
				      &imx477->module_name);
	if (ret)
		imx477->module_name = IMX477_DEFAULT_MODULE_NAME;
	ret = of_property_read_string(node, RKMODULE_CAMERA_LENS_NAME,
				      &imx477->len_name);
	if (ret)
		imx477->len_name = IMX477_DEFAULT_LENS_NAME;

	imx477->client = client;
	imx477->cur_mode = &supported_modes[0];

	imx477_parse_trigger_mode(imx477);

	endpoint = of_graph_get_next_endpoint(dev->of_node, NULL);
	if (!endpoint) {
		dev_err(dev, "Failed to get endpoint\n");
		return -EINVAL;
	}
	ret = v4l2_fwnode_endpoint_parse(of_fwnode_handle(endpoint),
					 &imx477->bus_cfg);
	of_node_put(endpoint);
	if (ret) {
		dev_err(dev, "Failed to get bus cfg\n");
		return ret;
	}

	/* The register tables assume a 2-lane link (0x0114 = 0x01) */
	if (imx477->bus_cfg.bus.mipi_csi2.num_data_lanes != 2) {
		dev_err(dev, "only 2 data lanes are supported (dts has %d)\n",
			imx477->bus_cfg.bus.mipi_csi2.num_data_lanes);
		return -EINVAL;
	}

	/*
	 * The RPi HQ camera generates its own 24MHz clock on-board, so the
	 * clock is optional; when present (e.g. a fixed-clock, or an SoC
	 * clock output) it is enabled and checked for 24MHz.
	 */
	imx477->xvclk = devm_clk_get_optional(dev, "xvclk");
	if (IS_ERR(imx477->xvclk)) {
		ret = PTR_ERR(imx477->xvclk);
		if (ret != -EPROBE_DEFER)
			dev_err(dev, "Failed to get xvclk\n");
		return ret;
	}
	if (!imx477->xvclk)
		dev_info(dev, "no xvclk provided, assuming on-module oscillator\n");

	imx477->power_gpio = devm_gpiod_get(dev, "power", GPIOD_OUT_LOW);
	if (IS_ERR(imx477->power_gpio))
		dev_warn(dev, "Failed to get power-gpios, maybe no use\n");

	imx477->reset_gpio = devm_gpiod_get(dev, "reset", GPIOD_OUT_LOW);
	if (IS_ERR(imx477->reset_gpio))
		dev_warn(dev, "Failed to get reset-gpios\n");

	imx477->pwdn_gpio = devm_gpiod_get(dev, "pwdn", GPIOD_OUT_LOW);
	if (IS_ERR(imx477->pwdn_gpio))
		dev_warn(dev, "Failed to get pwdn-gpios\n");

	ret = imx477_configure_regulators(imx477);
	if (ret) {
		dev_err(dev, "Failed to get power regulators\n");
		return ret;
	}

	imx477->pinctrl = devm_pinctrl_get(dev);
	if (!IS_ERR(imx477->pinctrl)) {
		imx477->pins_default =
			pinctrl_lookup_state(imx477->pinctrl,
					     OF_CAMERA_PINCTRL_STATE_DEFAULT);
		if (IS_ERR(imx477->pins_default))
			dev_info(dev, "could not get default pinstate\n");

		imx477->pins_sleep =
			pinctrl_lookup_state(imx477->pinctrl,
					     OF_CAMERA_PINCTRL_STATE_SLEEP);
		if (IS_ERR(imx477->pins_sleep))
			dev_info(dev, "could not get sleep pinstate\n");
	}

	mutex_init(&imx477->mutex);

	sd = &imx477->subdev;
	v4l2_i2c_subdev_init(sd, client, &imx477_subdev_ops);
	ret = imx477_initialize_controls(imx477);
	if (ret)
		goto err_destroy_mutex;

	ret = __imx477_power_on(imx477);
	if (ret)
		goto err_free_handler;

	ret = imx477_check_sensor_id(imx477, client);
	if (ret)
		goto err_power_off;

#ifdef CONFIG_VIDEO_V4L2_SUBDEV_API
	sd->internal_ops = &imx477_internal_ops;
	sd->flags |= V4L2_SUBDEV_FL_HAS_DEVNODE |
		     V4L2_SUBDEV_FL_HAS_EVENTS;
#endif
#if defined(CONFIG_MEDIA_CONTROLLER)
	imx477->pad.flags = MEDIA_PAD_FL_SOURCE;
	sd->entity.function = MEDIA_ENT_F_CAM_SENSOR;
	ret = media_entity_pads_init(&sd->entity, 1, &imx477->pad);
	if (ret < 0)
		goto err_power_off;
#endif

	memset(facing, 0, sizeof(facing));
	if (strcmp(imx477->module_facing, "back") == 0)
		facing[0] = 'b';
	else
		facing[0] = 'f';

	snprintf(sd->name, sizeof(sd->name), "m%02d_%s_%s %s",
		 imx477->module_index, facing,
		 IMX477_NAME, dev_name(sd->dev));
	ret = v4l2_async_register_subdev_sensor(sd);
	if (ret) {
		dev_err(dev, "v4l2 async register subdev failed\n");
		goto err_clean_entity;
	}

	pm_runtime_set_active(dev);
	pm_runtime_enable(dev);
	pm_runtime_idle(dev);

	return 0;

err_clean_entity:
#if defined(CONFIG_MEDIA_CONTROLLER)
	media_entity_cleanup(&sd->entity);
#endif
err_power_off:
	__imx477_power_off(imx477);
err_free_handler:
	v4l2_ctrl_handler_free(&imx477->ctrl_handler);
err_destroy_mutex:
	mutex_destroy(&imx477->mutex);

	return ret;
}

static void imx477_remove(struct i2c_client *client)
{
	struct v4l2_subdev *sd = i2c_get_clientdata(client);
	struct imx477 *imx477 = to_imx477(sd);

	v4l2_async_unregister_subdev(sd);
#if defined(CONFIG_MEDIA_CONTROLLER)
	media_entity_cleanup(&sd->entity);
#endif
	v4l2_ctrl_handler_free(&imx477->ctrl_handler);
	mutex_destroy(&imx477->mutex);

	pm_runtime_disable(&client->dev);
	if (!pm_runtime_status_suspended(&client->dev))
		__imx477_power_off(imx477);
	pm_runtime_set_suspended(&client->dev);
}

#if IS_ENABLED(CONFIG_OF)
static const struct of_device_id imx477_of_match[] = {
	{ .compatible = "sony,imx477" },
	{},
};
MODULE_DEVICE_TABLE(of, imx477_of_match);
#endif

static const struct i2c_device_id imx477_match_id[] = {
	{ "sony,imx477", 0 },
	{},
};

static struct i2c_driver imx477_i2c_driver = {
	.driver = {
		.name = IMX477_NAME,
		.pm = &imx477_pm_ops,
		.of_match_table = of_match_ptr(imx477_of_match),
	},
	.probe		= &imx477_probe,
	.remove		= &imx477_remove,
	.id_table	= imx477_match_id,
};

static int __init sensor_mod_init(void)
{
	return i2c_add_driver(&imx477_i2c_driver);
}

static void __exit sensor_mod_exit(void)
{
	i2c_del_driver(&imx477_i2c_driver);
}

device_initcall_sync(sensor_mod_init);
module_exit(sensor_mod_exit);

MODULE_DESCRIPTION("Sony imx477 sensor driver for Rockchip platforms");
MODULE_AUTHOR("Naushir Patuck <naush@raspberrypi.com>");
MODULE_LICENSE("GPL");
