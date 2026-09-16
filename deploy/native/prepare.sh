#!/usr/bin/env bash
# Provision only the candidate container, not an enabled/qualified backend.
set -euo pipefail
cd "$(dirname "$0")/../.."
if [[ $(hostname) != emosa-lab || $(systemd-detect-virt --vm) != kvm ]]; then
  echo 'Run only inside the dedicated emosa-lab VM.' >&2
  exit 1
fi
for kind in 'info opensync-native-r0' 'profile show emosa-native-r0'; do
  read -ra arguments <<< "$kind"
  if lxc "${arguments[@]}" >/dev/null 2>&1; then
    echo 'The candidate container/profile already exists; inspect it before reuse.' >&2
    exit 1
  fi
done
fingerprint=$(python3 -c 'import json; print(json.load(open("deploy/images.lock.json"))["container_fingerprint"])')
lxc image info "$fingerprint" >/dev/null
lxc network show em-mgmt >/dev/null
lxc profile create emosa-native-r0
lxc profile edit emosa-native-r0 < deploy/native/lxd-profile.yaml
lxc launch "$fingerprint" opensync-native-r0 -p emosa-native-r0
echo 'Candidate created; native build and N01-N04 qualification still required.'
