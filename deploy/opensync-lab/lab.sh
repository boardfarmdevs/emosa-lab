#!/bin/bash
# EMOSA on an opensync-lab VM (host side). Stages this checkout, the pinned
# native artifacts and uv into the VM, then runs vm/lab.sh there.
#
#   EMOSA_VM=emosa-osl-0923 deploy/opensync-lab/lab.sh stage
#   deploy/opensync-lab/lab.sh <vm/lab.sh command> [args]   (see vm/lab.sh)
#
# Artifacts (.cache/opensync-lab-artifacts): prplMesh 6.0.0 install/runtime from
# prplmesh-lab, EMOSA's bwl/hostap overlays and controller candidate
# candidate-ap-esp-02, uv 0.11.17. Hashes: deploy/opensync-lab/artifacts.sha256.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
VM=${EMOSA_VM:-emosa-osl-$(date +%m%d)}
ART=$ROOT/.cache/opensync-lab-artifacts

stage() {
    (cd "$ART" && sha256sum -c --quiet "$ROOT/deploy/opensync-lab/artifacts.sha256")
    lxc exec "$VM" -- sh -c 'rm -rf /opt/emosa-lab/source /opt/emosa-lab/deploy; mkdir -p /opt/emosa-lab/artifacts /opt/emosa-lab/source'
    tar -C "$ROOT" --exclude=.git --exclude=.cache --exclude=.lab --exclude=.venv --exclude=__pycache__ \
        --exclude=.pytest_cache --exclude=.ruff_cache -czf - . | lxc exec "$VM" -- tar -C /opt/emosa-lab/source -xzf -
    lxc exec "$VM" -- sh -c 'mkdir -p /opt/emosa-lab/deploy && cp -a /opt/emosa-lab/source/deploy/opensync-lab /opt/emosa-lab/deploy/'
    tar -C "$ART" -cf - . | lxc exec "$VM" -- tar -C /opt/emosa-lab/artifacts -xf -
    # the adapter itself goes in as the kit any other lab would use (deploy/adapter)
    kit=$(UV="$ART/uv" "$ROOT/deploy/adapter/build.sh" "$ROOT/.cache/adapter-kit" | tail -1)
    lxc file push -q "$kit" "$VM/opt/emosa-lab/adapter-kit.tar.gz"
    echo "staged $(git -C "$ROOT" rev-parse --short HEAD)$(git -C "$ROOT" diff --quiet || echo +dirty) into $VM:/opt/emosa-lab"
}

expose_ui() {    # host port -> the VM's controller UI (like opensync-lab's noc-ui proxy)
    local port=${EMOSA_UI_HOST_PORT:-8660} ip listen
    ip=$(lxc config device get "$VM" eth0 ipv4.address)
    listen=$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')
    lxc config device remove "$VM" em-ui >/dev/null 2>&1 || true
    lxc config device add "$VM" em-ui proxy nat=true listen="tcp:$listen:$port" connect="tcp:$ip:${EMOSA_UI_PORT:-8093}" >/dev/null
    echo "EasyMesh controller UI: http://$listen:$port/"
}

case ${1:-} in
    stage) stage ;;
    ui) lxc exec "$VM" -- bash /opt/emosa-lab/deploy/opensync-lab/vm/lab.sh ui && expose_ui ;;
    ""|-h|--help) sed -n '2,11p' "$0" ;;
    *) exec lxc exec "$VM" -- bash /opt/emosa-lab/deploy/opensync-lab/vm/lab.sh "$@" ;;
esac
