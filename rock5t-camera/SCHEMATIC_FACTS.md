# ROCK 5T camera wiring — extracted from the schematic

Source: `reference/radxa_rock5t_schematic_v1.2.pdf` (v1.2, 2025-01-09), sheet 18
"Camera_MIPI-CSI" + the RK3588 GPIO domain sheets. These are the facts that fill
the device-tree overlay's placeholders.

## Connectors

Both camera connectors are **30-pin 0.5mm FPC** (Radxa's camera pinout), NOT the
Raspberry Pi 15-pin. Each carries 4 MIPI data lanes with **two clock lanes**
(CLKA + CLKB) — the RK3588 split-PHY wiring: one connector = one 4-lane camera
OR two 2-lane cameras. A 2-lane Pi HQ per connector is squarely supported.

> ⚠ The `rock-5-pcb` flex adapter was drawn for the ROCK 5B+ 31-pin CAM0 —
> needs a connector rework for the 5T's 30-pin. Verify any purchased adapter
> cable against sheet 18's pinout.

## CAM1 (J5002)

| Function | Net | SoC resource |
|---|---|---|
| CSI receiver | MIPI_CSI0_RX_D0..D3, CLK0(A)+CLK1(B) | **csi2_dphy0 hw** (full) / split modes for 2-lane |
| I2C | I2C3_SCL/SDA_M0_MIPI (via 0R) | **i2c3, pinmux M0** |
| MCLK pin 21 "CAM0-CLK" | MIPI_CAM3_CLKOUT | GPIO1_D6, func MIPI_CAMERA3_CLK (M0) |
| MCLK pin 23 "CAM1-CLK" | MIPI_CAM1_CLKOUT | GPIO1_B6, func MIPI_CAMERA1_CLK (M0) |
| CM-PDN0 (pin 24) | MIPI_CSI0_PDN0_H | GPIO1_D3 |
| CM-PDN1 (pin 27) | MIPI_CSI0_PDN1_H | GPIO1_C4 |
| RESET (pin 28) | CAM1_RST_L | GPIO2_C5 |
| Power | VCC_3V3_S3 (ferrite) + VCC_5V | always-on rails, no regulator to control |

## CAM2 (J10)

| Function | Net | SoC resource |
|---|---|---|
| CSI receiver | MIPI_CSI1_RX_D0..D3, CLK0(A)+CLK1(B) | **csi2_dphy1 hw** (full) / split modes |
| I2C | I2C4_SCL/SDA | **i2c4, pinmux M1** (GPIO2_B4=SDA, GPIO2_B5=SCL) |
| MCLK "CAM0-CLK" | MIPI_CAM4_CLKOUT | GPIO1_D7, func MIPI_CAMERA4_CLK (M0) |
| MCLK "CAM1-CLK" | MIPI_CAM2_CLKOUT | GPIO1_B7, func MIPI_CAMERA2_CLK (M0) |
| CM-PDN2 | MIPI_CSI1_PDN2_H | GPIO2_A6 (verify — extraction mangled) |
| CM-PDN4 | MIPI_CSI1_PDN4_H | GPIO2_A7 (verify) |
| RESET | CAM2_RST_L | near GPIO2_B0 (verify) |
| Power | VCC_3V3_S3 + VCC_5V | always-on |

## Consequences for the overlay/driver

- **One Pi HQ per connector**: cam A on CAM1 (i2c3, dphy0, 2 lanes on D0/D1 +
  CLKA), cam B on CAM2 (i2c4, dphy1). Separate i2c buses per camera — the
  Orin-style per-bus source/sink genlock assignment carries over cleanly
  (trigger-mode property per sensor node; wiring decides which is which).
- **MCLK**: the board CAN supply it (two CLKOUTs per connector), but the Pi HQ
  generates its own 24 MHz on-camera — an adapter need not route CAM-CLK.
  Overlay declares a fixed-clock 24 MHz stub either way.
- **Reset/PDN**: the Pi HQ 15-pin has no reset input (only an enable that the
  camera pulls up itself); expect to leave reset-gpios out or declare the pin
  and let it idle. Verify the three flagged GPIOs on hardware if needed.
- **Power**: connector 3V3 is an always-on rail — no regulator nodes needed in
  the sensor node beyond fixed-regulator stubs.
- The unused second clock lane (CLKB) and lanes D2/D3 stay unconnected for a
  2-lane camera; `data-lanes = <1 2>` on both endpoints.
