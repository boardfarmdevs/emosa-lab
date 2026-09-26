#!/bin/bash
# EMOSA adapter kit: install or upgrade on this host or container (root).
#   ./install.sh            run from the unpacked kit directory
# Installs into /opt/emosa-adapter (its own Python 3.13.7; the system Python is
# untouched), the systemd units, /usr/local/sbin/emosa-agent-link, and on a
# first install /etc/default/emosa and /etc/emosa-fleet.json (edit it, then
# `systemctl enable --now emosa-fleet`). An upgrade keeps the configuration,
# state and running agents' identities, and restarts running units.
# Needs: systemd, iproute2, a C compiler (the ovs package builds an extension)
# and network access to PyPI and python-build-standalone. With cmake, pkg-config
# and the cJSON and OpenSSL headers it also builds the C lab prototype,
# /opt/emosa-adapter/bin/emosa-agent-c (selected with EMOSA_AGENT, see
# /etc/default/emosa).
set -euo pipefail
cd "$(dirname "$0")"
test "$(id -u)" = 0 || { echo "run as root" >&2; exit 1; }
sha256sum -c --quiet SHA256SUMS
ROOT=/opt/emosa-adapter
install -d -m 0755 "$ROOT/bin"
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
install -d -m 0755 "$ROOT/share/profiles"
install -m 0644 profiles/*.json "$ROOT/share/profiles/"
c_agent="not built (needs cmake, pkg-config, libcjson-dev, libssl-dev)"
if command -v cmake >/dev/null && pkg-config --exists libcjson libcrypto 2>/dev/null; then
    rm -rf "$ROOT/c-build"
    cmake -S c -B "$ROOT/c-build" -DCMAKE_BUILD_TYPE=Release >/dev/null
    cmake --build "$ROOT/c-build" --target emosa-agent-c >/dev/null
    install -m 0755 "$ROOT/c-build/emosa-agent-c" "$ROOT/bin/emosa-agent-c.new"
    mv "$ROOT/bin/emosa-agent-c.new" "$ROOT/bin/emosa-agent-c"
    rm -rf "$ROOT/c-build"
    c_agent="$ROOT/bin/emosa-agent-c"
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
echo "emosa $(cat VERSION) installed in $ROOT${running:+; restarted: $(echo $running)}"
echo "C lab prototype: $c_agent"
