# syntax=docker/dockerfile:1
FROM debian:bookworm AS uboot
RUN apt-get update \
  && apt-get -y install gcc-arm-linux-gnueabihf bc bison flex libssl-dev make swig \
       python3-dev python3-setuptools python3-distutils device-tree-compiler git ca-certificates \
  && rm -rf /var/lib/apt/lists/*
ARG XCHIP_UBOOT_REV=0e17d167ce72977420e4e54656d97de1f3237885
ARG UBOOT_TAG=v2022.01
WORKDIR /build
RUN git clone -q https://github.com/NextThingCo/x-chip-uboot \
  && git -C x-chip-uboot checkout -q "$XCHIP_UBOOT_REV" \
  && git clone -q --depth 1 --branch "$UBOOT_TAG" https://github.com/u-boot/u-boot
WORKDIR /build/u-boot
RUN for p in sunxi-Add-support-for-slc-emulation-on-mlc-NAND \
      sunxi-nand-Undo-removal-of-DMA-specific-code-that-br \
      cmd-w1-read-add-optional-dest-address; do \
      git apply "../x-chip-uboot/0001-$p.patch"; \
    done \
  && grep -v USE_FLASH_BBT ../x-chip-uboot/nand.cfg >> configs/CHIP_defconfig \
  && echo "# CONFIG_SYS_NAND_USE_FLASH_BBT is not set" >> configs/CHIP_defconfig \
  && make -s ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf- CHIP_defconfig \
  && make -s -j"$(nproc)" ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf- \
  && ! grep -q "^CONFIG_SYS_NAND_USE_FLASH_BBT=y" .config \
  && grep -q "^CONFIG_ENV_IS_NOWHERE=y" .config

FROM debian:trixie AS tools
RUN apt-get update \
  && apt-get -y install --no-install-recommends sunxi-tools dfu-util u-boot-tools \
       python3 python3-numpy python3-tqdm python3-pip python3-setuptools \
  && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md /src/
COPY src /src/src
RUN pip install --no-cache-dir --no-deps --no-build-isolation --break-system-packages /src \
  && rm -rf /src
COPY --from=uboot /build/u-boot/u-boot-sunxi-with-spl.bin /opt/pocketrechip/
