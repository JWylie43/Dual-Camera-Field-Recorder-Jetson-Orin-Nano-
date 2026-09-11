# Out-of-tree build of the imx477 sensor driver for the Rockchip
# RK3588 vendor kernel (Radxa ROCK 5T).
#
# Build on the board:   make
# Install:              sudo make install
# Clean:                make clean

obj-m := imx477.o rk_cam_defer_enable.o

KDIR ?= /lib/modules/$(shell uname -r)/build
PWD  := $(shell pwd)

default:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

install: default
	sudo install -D -m 644 imx477.ko \
		/lib/modules/$(shell uname -r)/extra/imx477.ko
	sudo install -D -m 644 rk_cam_defer_enable.ko \
		/lib/modules/$(shell uname -r)/extra/rk_cam_defer_enable.ko
	sudo depmod -a

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean

.PHONY: default install clean
