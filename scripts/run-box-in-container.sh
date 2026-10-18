#!/usr/bin/env bash
# The lab in a box in an Ubuntu 24.04 container, as CI runs it, on a host whose own toolchain
# is older: the image once, then the C agent, ovsdb-server and the registrar into this
# checkout's caches, then the box's tests. Needs Docker; the box makes its own user and
# network namespace inside the container.
#
#   scripts/run-box-in-container.sh [PYTEST ARGUMENTS...]     e.g. -k steering -x
#   scripts/run-box-in-container.sh -- COMMAND...             e.g. -- python -m emosa_lab.box
#                                                                  onboard --agent c --directory /tmp/b
# EMOSA_BOX_SANITIZE=1: the C agent, fleet and GTP built with AddressSanitizer and
# UndefinedBehaviorSanitizer (EMOSA_SANITIZE, as CI's box (c, sanitizers)): a memory error stops
# the binary, and its scenario fails with the report in agent.log.
# The upgrade scenarios' previous release (lab/upgrade-from): its tree unpacked into the cache
# once (.cache/previous, its commit in .release), its C agent built beside this one's.
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
image=emosa-box:24.04
read -r previous _ < "$root/lab/upgrade-from"
if [ "$(cat "$root/.cache/previous/.release" 2>/dev/null)" != "$previous" ]; then
    rm -rf "$root/.cache/previous"
    mkdir -p "$root/.cache/previous"
    git -C "$root" cat-file -e "$previous^{commit}" 2>/dev/null ||
        git -C "$root" fetch -q --no-tags origin "$previous"
    git -C "$root" archive "$previous" | tar -x -C "$root/.cache/previous"
    echo "$previous" > "$root/.cache/previous/.release"
fi
docker build -q -t "$image" -f "$root/scripts/box-container.Dockerfile" "$root/scripts" >/dev/null
docker run --rm --user "$(id -u):$(id -g)" \
    --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
    -e HOME=/tmp/home -e XDG_CACHE_HOME=/src/.cache/xdg \
    -e UV_CACHE_DIR=/src/.cache/uv -e UV_PYTHON_INSTALL_DIR=/src/.cache/uv-python \
    -e UV_PROJECT_ENVIRONMENT=/src/.cache/box-venv -e EMOSA_BOX_SANITIZE="${EMOSA_BOX_SANITIZE:-}" \
    -v "$root:/src" -w /src "$image" bash -ec '
        mkdir -p "$HOME"
        uv python install 3.13.7 >/dev/null 2>&1
        uv sync --frozen >/dev/null 2>&1
        python3 scripts/fetch-evidence.py >/dev/null    # the test session checks for it (cached)
        [ -x .cache/upstream/openvswitch-4.0.0/ovsdb/ovsdb-server ] || bash scripts/build-ovsdb.sh
        [ -x .cache/wsc-registrar/component-registrar ] || python3 scripts/build-wsc-registrar.py
        build=.cache/box-c-build previous=.cache/previous-c-build flags=
        if [ -n "$EMOSA_BOX_SANITIZE" ]; then
            build=.cache/box-c-asan previous=.cache/previous-c-asan
            flags="-DEMOSA_SANITIZE=ON -DCMAKE_BUILD_TYPE=Debug"
            export ASAN_OPTIONS=detect_leaks=0 UBSAN_OPTIONS=print_stacktrace=1
        fi
        cmake -S c -B "$build" $flags >/dev/null
        cmake --build "$build" -j >/dev/null
        cmake -S .cache/previous/c -B "$previous" $flags >/dev/null
        cmake --build "$previous" -j >/dev/null
        export EMOSA_BOX_REQUIRED=1 EMOSA_C_AGENT="$build/emosa-agent-c"
        export EMOSA_BOX_PREVIOUS=/src/.cache/previous EMOSA_BOX_PREVIOUS_C="/src/$previous/emosa-agent-c"
        if [ "${1:-}" = -- ]; then shift; uv run "$@"; else uv run pytest -m box "$@"; fi
    ' bash "$@"
