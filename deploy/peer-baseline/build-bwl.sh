#!/bin/bash
set -euo pipefail
test "$(hostname)" = emosa-lab
test "$(id -u)" = 0
test "$(systemd-detect-virt --vm)" = kvm
lab_root=/opt/emosa-baseline
lab_build="$lab_root/build-bwl"
printf '%s\n' \
 'ebc771d43279cdb6b1cf5e2c09eb51d66ef3a8afab6b97f4561439d2606ac549  /opt/peer-artifacts/prplmesh-patched-source.tar.gz' \
 'a98a6903fb9db1494ece0c292dd85c7d4d8ff2f570b5b85768ed6f299f72fb1f  /opt/peer-artifacts/prpl-install-nl80211-6.0.0.tar.gz' \
 | sha256sum -c -
if [ "${1:-}" = --resume ]; then
 test -d "$lab_build/source"
elif [ -z "${1:-}" ]; then
 test ! -e "$lab_build"
 mkdir -p "$lab_build/source"
 tar -C "$lab_build/source" -xzf /opt/peer-artifacts/prplmesh-patched-source.tar.gz
 tar -C "$lab_build" -xzf /opt/peer-artifacts/prpl-install-nl80211-6.0.0.tar.gz \
  prpl-install-nl80211/include prpl-install-nl80211/lib
else
 exit 2
fi
patch_path="$lab_root/0002-prplmesh-primary-bss-identity.patch"
if [ ! -e "$lab_build/applied-patch.sha256" ]; then
 patch --batch --fuzz=0 -p1 -d "$lab_build/source" < "$patch_path"
 sha256sum "$patch_path" > "$lab_build/applied-patch.sha256"
else
 sha256sum -c "$lab_build/applied-patch.sha256"
fi
cmake -S "$lab_root/bwl-overlay" -B "$lab_build/build" \
 -DCMAKE_BUILD_TYPE=RelWithDebInfo \
 -DPEER_SOURCE="$lab_build/source" \
 -DPEER_INSTALL="$lab_build/prpl-install-nl80211" \
 -DHOSTAP_SOURCE="$lab_root/build-hostap/hostap" \
 >> "$lab_build/configure.log" 2>&1
grep -q '/nl80211/ap_wlan_hal_nl80211.cpp' "$lab_build/build/compile_commands.json"
if grep -q '/dummy/' "$lab_build/build/compile_commands.json"; then
 echo 'Refusing a dummy HAL build' >&2
 exit 1
fi
timeout 600 cmake --build "$lab_build/build" --parallel 1 >> "$lab_build/build.log" 2>&1
cmake --install "$lab_build/build" --prefix "$lab_build/stage" \
 >> "$lab_build/build.log" 2>&1
tar -C "$lab_build/stage" -czf /opt/peer-artifacts/emosa-prpl-bwl-6.0.0.tar.gz .
dpkg-query -W -f='${Package}\t${Version}\n' cmake g++ gcc libnl-3-dev \
 libnl-genl-3-dev libnl-route-3-dev > "$lab_build/build-packages.txt"
sha256sum /opt/peer-artifacts/emosa-prpl-bwl-6.0.0.tar.gz \
 "$lab_build/stage/lib/libbwl.so.6.0.0"
