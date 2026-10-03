#!/bin/bash
# EMOSA adapter kit: install or upgrade on this host or container (root).
#   ./install.sh                          run from the unpacked kit directory
#   EMOSA_IMPLEMENTATION=c ./install.sh   the adapter in C only: no Python installed
# Installs into /opt/emosa-adapter, the systemd units, /usr/local/sbin/emosa-agent-link,
# and on a first install /etc/default/emosa and /etc/emosa-fleet.json (edit it, then
# `systemctl enable --now emosa-fleet`). An upgrade keeps the configuration, state and
# running agents' identities, and restarts running units.
#
# EMOSA_IMPLEMENTATION=python (the default): the Python reference, with its own Python
# 3.13.7 (the system Python is untouched); needs a C compiler (the ovs package builds an
# extension) and network access to PyPI and python-build-standalone. With cmake,
# pkg-config and the cJSON, OpenSSL and SQLite headers it also builds the C programs,
# selectable per program (EMOSA_AGENT, EMOSA_FLEET, EMOSA_GTP in /etc/default/emosa).
# EMOSA_IMPLEMENTATION=c: the C programs only (they need those headers to build), and a
# Python adapter installed before is removed. Both need systemd and iproute2. The choice
# is kept in /etc/default/emosa-implementation, which the installer owns; settings in
# /etc/default/emosa override it.
set -euo pipefail
cd "$(dirname "$0")"
test "$(id -u)" = 0 || { echo "run as root" >&2; exit 1; }
# unset: the implementation installed before (an upgrade keeps the choice), else python
installed=$(sed -n 's/^EMOSA_IMPLEMENTATION=//p' /etc/default/emosa-implementation 2>/dev/null || true)
IMPLEMENTATION=${EMOSA_IMPLEMENTATION:-${installed:-python}}
case $IMPLEMENTATION in python|c) ;; *) echo "EMOSA_IMPLEMENTATION is python or c" >&2; exit 2 ;; esac
sha256sum -c --quiet SHA256SUMS
ROOT=/opt/emosa-adapter
install -d -m 0755 "$ROOT/bin"
if [ "$IMPLEMENTATION" = python ]; then
    install -m 0755 uv "$ROOT/bin/uv"
    export UV_PYTHON_INSTALL_DIR=$ROOT/python UV_CACHE_DIR=$ROOT/cache
    "$ROOT/bin/uv" python install -q 3.13.7
    rm -rf "$ROOT/venv.new"
    "$ROOT/bin/uv" venv -q --python 3.13.7 "$ROOT/venv.new"
    "$ROOT/bin/uv" pip install -q --python "$ROOT/venv.new/bin/python" --require-hashes -r requirements.txt
    "$ROOT/bin/uv" pip install -q --python "$ROOT/venv.new/bin/python" --no-deps emosa-*.whl
    "$ROOT/venv.new/bin/python" -c 'import emosa.agent.fleet, emosa.agent.pod'
    rm -rf "$ROOT/venv.old"
    [ -d "$ROOT/venv" ] && mv "$ROOT/venv" "$ROOT/venv.old"
    mv "$ROOT/venv.new" "$ROOT/venv"
    # the venv's scripts carry its build path; point them at the final one
    sed -i "1s|$ROOT/venv.new/|$ROOT/venv/|" "$ROOT"/venv/bin/emosa-fleet "$ROOT"/venv/bin/emosa-agent "$ROOT"/venv/bin/emosa-gtp
    rm -rf "$ROOT/venv.old"
fi
install -d -m 0755 "$ROOT/share/profiles"
install -m 0644 profiles/*.json "$ROOT/share/profiles/"
install -d -m 0755 "$ROOT/share/schemas"
install -m 0644 schemas/*.schema.json "$ROOT/share/schemas/"
C_PROGRAMS="emosa-agent-c emosa-fleet-c emosa-gtp-c"
c_built="not built (needs cmake, pkg-config, libcjson-dev, libssl-dev, libsqlite3-dev)"
if command -v cmake >/dev/null && pkg-config --exists libcjson libcrypto sqlite3 2>/dev/null; then
    rm -rf "$ROOT/c-build"
    cmake -S c -B "$ROOT/c-build" -DCMAKE_BUILD_TYPE=Release >/dev/null
    for program in $C_PROGRAMS; do
        cmake --build "$ROOT/c-build" --target "$program" >/dev/null
        install -m 0755 "$ROOT/c-build/$program" "$ROOT/bin/$program.new"
        mv "$ROOT/bin/$program.new" "$ROOT/bin/$program"
    done
    rm -rf "$ROOT/c-build"
    c_built="$ROOT/bin: $C_PROGRAMS"
elif [ "$IMPLEMENTATION" = c ]; then
    echo "the C adapter cannot be built here: $c_built" >&2
    exit 1
fi
if [ "$IMPLEMENTATION" = c ]; then
    # no Python on a router: a Python adapter installed before goes
    rm -rf "$ROOT/venv" "$ROOT/python" "$ROOT/cache" "$ROOT/bin/uv"
    printf '%s\n' "# written by the EMOSA adapter installer; /etc/default/emosa overrides it" \
        "EMOSA_IMPLEMENTATION=c" "EMOSA_AGENT=$ROOT/bin/emosa-agent-c" "EMOSA_FLEET=$ROOT/bin/emosa-fleet-c" \
        "EMOSA_GTP=$ROOT/bin/emosa-gtp-c" > /etc/default/emosa-implementation
else
    printf '%s\n' "# written by the EMOSA adapter installer; /etc/default/emosa overrides it" \
        "EMOSA_IMPLEMENTATION=python" "EMOSA_AGENT=$ROOT/venv/bin/emosa-agent" "EMOSA_FLEET=$ROOT/venv/bin/emosa-fleet" \
        "EMOSA_GTP=$ROOT/venv/bin/emosa-gtp" > /etc/default/emosa-implementation
fi
install -m 0755 files/emosa-agent-link /usr/local/sbin/emosa-agent-link
install -m 0644 files/emosa-agent@.service files/emosa-fleet.service files/emosa-gtp.service /etc/systemd/system/
[ -e /etc/default/emosa ] || install -m 0644 files/default-emosa /etc/default/emosa
[ -e /etc/emosa-fleet.json ] || install -m 0644 files/fleet.example.json /etc/emosa-fleet.json
[ -e /etc/emosa-gtp.json ] || install -m 0644 files/gtp.example.json /etc/emosa-gtp.json
install -d -m 0755 /etc/emosa
install -d -m 0700 /var/lib/emosa
systemctl daemon-reload
running=$(systemctl list-units --no-legend --plain --state=active 'emosa-fleet.service' 'emosa-agent@*.service' 'emosa-gtp.service' | awk '{print $1}')
[ -n "$running" ] && systemctl restart $running
echo "emosa $(cat VERSION) installed in $ROOT, implementation $IMPLEMENTATION${running:+; restarted: $(echo $running)}"
echo "C programs: $c_built"
