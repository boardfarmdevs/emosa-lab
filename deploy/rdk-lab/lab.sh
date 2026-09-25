#!/bin/bash
# EMOSA and an OpenSync pod in an RDK EasyMesh lab VM (host side). Stages the
# adapter kit, the pod image and vm/lab.sh into the VM, then runs vm/lab.sh there.
# Design: doc/architecture/rdk-lab.md.
#
#   EMOSA_VM=rdk-emosa EMOSA_POD_IMAGE=~/yocto/mvx-pod-work/out/mvx-pod-<stamp> deploy/rdk-lab/lab.sh stage
#   deploy/rdk-lab/lab.sh <vm/lab.sh command> [args]   (see vm/lab.sh)
#
# The adapter kit is built with uv (UV=, default .cache/opensync-lab-artifacts/uv).
# The VM must be an RDK lab VM of its own: never the reference VMs.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
VM=${EMOSA_VM:-rdk-emosa}

stage() {
    local pod=${EMOSA_POD_IMAGE:?set EMOSA_POD_IMAGE to the pod image (out/mvx-pod-<stamp>)} kit
    [ -f "$pod.rootfs.tar.gz" ] && [ -f "$pod.metadata.tar.gz" ] || { echo "no pod image at $pod" >&2; exit 1; }
    kit=$(UV="${UV:-$ROOT/.cache/opensync-lab-artifacts/uv}" "$ROOT/deploy/adapter/build.sh" \
        "$ROOT/.cache/adapter-kit" | tail -1)
    lxc exec "$VM" -- sh -c 'rm -rf /opt/emosa-lab/pod; mkdir -p /opt/emosa-lab/pod /opt/emosa-lab/vm'
    lxc file push -q "$kit" "$VM/opt/emosa-lab/adapter-kit.tar.gz"
    lxc file push -q "$pod.metadata.tar.gz" "$pod.rootfs.tar.gz" "$VM/opt/emosa-lab/pod/"
    lxc file push -q "$ROOT/deploy/rdk-lab/vm/lab.sh" "$VM/opt/emosa-lab/vm/lab.sh"
    echo "staged $(git -C "$ROOT" rev-parse --short HEAD)$(git -C "$ROOT" diff --quiet || echo +dirty)" \
        "and $(basename "$pod") into $VM:/opt/emosa-lab"
}

case ${1:-} in
    stage) stage ;;
    ""|-h|--help) sed -n '2,10p' "$0" ;;
    *) exec lxc exec "$VM" -- bash /opt/emosa-lab/vm/lab.sh "$@" ;;
esac
