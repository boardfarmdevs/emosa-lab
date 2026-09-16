#!/usr/bin/env bash
# Run explicitly on an LXD host. Only creates the named project-owned VM.
set -euo pipefail
cd "$(dirname "$0")/.."
if lxc info emosa-lab >/dev/null 2>&1; then
  echo 'emosa-lab already exists; inspect it before reusing this setup script.' >&2
  exit 1
fi
vm_fingerprint=$(python3 -c 'import json; print(json.load(open("deploy/images.lock.json"))["vm_fingerprint"])')
lxc launch "ubuntu:$vm_fingerprint" emosa-lab --vm -c limits.cpu=2 -c limits.memory=2GiB
echo 'VM created. Follow deploy/README.md to provision the inner daemon and qualify packages.'
