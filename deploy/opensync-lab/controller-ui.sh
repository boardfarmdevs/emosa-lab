#!/bin/bash
# Build prplmesh-lab's controller UI and topology adapter at the commit controller-ui.env
# pins, into .cache/opensync-lab-artifacts/controller-ui (lab.sh stage runs it):
# easymesh-controller (its page is the topology page of the medium that commit pins),
# config/, topology-adapter.py and SOURCE. Nothing is done when SOURCE names the pin.
#
#   PRPLMESH_LAB=../prplmesh-lab deploy/opensync-lab/controller-ui.sh
#
# PRPLMESH_LAB is a prplmesh-lab checkout (default: the sibling in an easymesh-labs
# workspace); a missing commit is fetched, the medium from its submodule or cloned. Needs Go.
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
source "$ROOT/deploy/opensync-lab/controller-ui.env"
commit=$PRPLMESH_LAB_COMMIT
lab=${PRPLMESH_LAB:-$ROOT/../prplmesh-lab}
out=$ROOT/.cache/opensync-lab-artifacts/controller-ui
if [ -x "$out/easymesh-controller" ] && grep -q "^prplmesh-lab $commit " "$out/SOURCE" 2>/dev/null; then
    exit 0
fi
[ -d "$lab/.git" ] || [ -f "$lab/.git" ] || { echo "no prplmesh-lab checkout at $lab: set PRPLMESH_LAB" >&2; exit 1; }
have() { git -C "$1" cat-file -e "$2^{commit}" 2>/dev/null || git -C "$1" fetch -q origin "$2"; }
have "$lab" "$commit"
medium=$(git -C "$lab" rev-parse "$commit:medium")
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
# the medium: the checkout's submodule, else a clone from the pinned commit's .gitmodules
medium_repo=$lab/medium
if [ ! -e "$medium_repo/.git" ]; then
    medium_repo=$work/medium.git
    url=$(git -C "$lab" config --blob "$commit:.gitmodules" submodule.medium.url)
    case $url in ../*) url="$(dirname "$(git -C "$lab" remote get-url origin)")/${url#../}" ;; esac
    git clone -q --bare "$url" "$medium_repo"
fi
have "$medium_repo" "$medium"
git -C "$lab" archive "$commit" controller-ui topology-adapter | tar -x -C "$work"
mkdir "$work/medium"
git -C "$medium_repo" archive "$medium" topology-ui configurator/worlds/viewer | tar -x -C "$work/medium"
(
    cd "$work/controller-ui"
    bash prepare-web-assets.sh
    go test -buildvcs=false ./...
    CGO_ENABLED=0 go build -trimpath -buildvcs=false -o "$work/easymesh-controller" ./cmd/easymesh-controller
)
rm -rf "$out"
mkdir -p "$out"
install -m 0755 "$work/easymesh-controller" "$out/easymesh-controller"
cp -a "$work/controller-ui/config" "$out/"
install -m 0755 "$work/topology-adapter/server.py" "$out/topology-adapter.py"
printf 'prplmesh-lab %s (controller-ui, topology-adapter), medium %s, built %s with %s\n' \
    "$commit" "$medium" "$(date -u +%F)" "$(go env GOVERSION)" > "$out/SOURCE"
cat "$out/SOURCE"
