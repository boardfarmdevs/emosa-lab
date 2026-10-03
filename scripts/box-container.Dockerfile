# The lab in a box's toolchain, as CI's ubuntu-24.04 runner has it (scripts/run-box-in-container.sh),
# with the C bar's tools (c/QUALITY.md §4: clang-tidy, scan-build, libFuzzer).
FROM ubuntu:24.04
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential ca-certificates clang clang-tidy clang-tools cmake curl dnsmasq-base git \
        iproute2 libclang-rt-18-dev libcjson-dev libsqlite3-dev libssl-dev mosquitto pkg-config \
        python3 util-linux xz-utils \
    && rm -rf /var/lib/apt/lists/*
RUN curl -LsSf https://astral.sh/uv/0.11.17/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
