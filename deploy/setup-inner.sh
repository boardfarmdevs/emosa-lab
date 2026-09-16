#!/usr/bin/env bash
# Run explicitly inside the dedicated emosa-lab VM, with an initialized inner LXD daemon.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $(hostname) != emosa-lab ]]; then
  echo 'This script is restricted to the dedicated emosa-lab VM.' >&2
  exit 1
fi
for name in em-controller emosa; do
  if lxc info "$name" >/dev/null 2>&1; then
    echo "$name already exists; setup refuses to replace it." >&2
    exit 1
  fi
done
container_fingerprint=$(python3 -c 'import json; print(json.load(open("deploy/images.lock.json"))["container_fingerprint"])')
lxc network create em-mgmt ipv4.address=auto ipv4.nat=true ipv6.address=none
lxc network create em-protocol ipv4.address=none ipv6.address=none
lxc profile create emosa-app
lxc profile edit emosa-app < deploy/lxd/app-profile.yaml
for name in em-controller emosa; do
  lxc launch "ubuntu:$container_fingerprint" "$name" -p emosa-app
done
mkdir -p .cache/lxd-exports
lxc image export "$container_fingerprint" .cache/lxd-exports/ubuntu24-container
echo 'Containers created. Install the locked Python applications as described in deploy/README.md.'
