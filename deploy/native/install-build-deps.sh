#!/usr/bin/env bash
# Run explicitly only in the isolated native candidate; no switch daemon package.
set -euo pipefail
if [[ $EUID != 0 || $(hostname) != opensync-native-r0 || $(systemd-detect-virt --container) != lxc ]]; then
  echo 'Run as root only in the opensync-native-r0 LXD container.' >&2
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
mapfile -t packages < "$(dirname "$0")/build-deps.lock"
apt-get update
apt-get install -y --no-install-recommends "${packages[@]}"
