#!/usr/bin/env bash
# The lab in a box in an Ubuntu 24.04 container, as CI runs it, on a host whose own toolchain
# is older: the image once, then the C agent, ovsdb-server and the registrar into this
# checkout's caches, then the box's tests. Needs Docker; the box makes its own user and
# network namespace inside the container.
#
#   scripts/run-box-in-container.sh [PYTEST ARGUMENTS...]     e.g. -k steering -x
#   scripts/run-box-in-container.sh -- COMMAND...             e.g. -- python -m emosa_lab.box
#                                                                  onboard --agent c --directory /tmp/b
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
image=emosa-box:24.04
docker build -q -t "$image" -f "$root/scripts/box-container.Dockerfile" "$root/scripts" >/dev/null
docker run --rm --user "$(id -u):$(id -g)" \
    --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
    -e HOME=/tmp/home -e XDG_CACHE_HOME=/src/.cache/xdg \
    -e UV_CACHE_DIR=/src/.cache/uv -e UV_PYTHON_INSTALL_DIR=/src/.cache/uv-python \
    -e UV_PROJECT_ENVIRONMENT=/src/.cache/box-venv \
    -v "$root:/src" -w /src "$image" bash -ec '
        mkdir -p "$HOME"
        uv python install 3.13.7 >/dev/null 2>&1
        uv sync --frozen >/dev/null 2>&1
        python3 scripts/fetch-evidence.py >/dev/null    # the test session checks for it (cached)
        [ -x .cache/upstream/openvswitch-4.0.0/ovsdb/ovsdb-server ] || bash scripts/build-ovsdb.sh
        [ -x .cache/wsc-registrar/component-registrar ] || python3 scripts/build-wsc-registrar.py
        cmake -S c -B .cache/box-c-build >/dev/null
        cmake --build .cache/box-c-build -j >/dev/null
        export EMOSA_BOX_REQUIRED=1 EMOSA_C_AGENT=.cache/box-c-build/emosa-agent-c
        if [ "${1:-}" = -- ]; then shift; uv run "$@"; else uv run pytest -m box "$@"; fi
    ' bash "$@"
