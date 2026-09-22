#!/bin/bash
# Build in a NEW directory. Never replace the retained baseline build/artifact.
set -euo pipefail
test "$(hostname)" = emosa-lab
test "$(id -u)" = 0
test "$(systemd-detect-virt --vm)" = kvm
lab_root=/opt/emosa-baseline
lab_build=${1:?Pass a new absolute build directory below /opt/emosa-baseline/}
case "$lab_build" in /opt/emosa-baseline/candidate-*) ;; *) exit 2 ;; esac
test ! -e "$lab_build"
mkdir "$lab_build"
printf '%s\n' \
 'ebc771d43279cdb6b1cf5e2c09eb51d66ef3a8afab6b97f4561439d2606ac549  /opt/peer-artifacts/prplmesh-patched-source.tar.gz' \
 'a98a6903fb9db1494ece0c292dd85c7d4d8ff2f570b5b85768ed6f299f72fb1f  /opt/peer-artifacts/prpl-install-nl80211-6.0.0.tar.gz' \
 | sha256sum -c - > "$lab_build/input-checks.txt"
mkdir "$lab_build/source"
tar -C "$lab_build/source" -xzf /opt/peer-artifacts/prplmesh-patched-source.tar.gz
tar -C "$lab_build" -xzf /opt/peer-artifacts/prpl-install-nl80211-6.0.0.tar.gz \
 prpl-install-nl80211/include prpl-install-nl80211/lib
for patch_name in 0002-prplmesh-primary-bss-identity.patch 0003-prplmesh-he-mcs-length.patch; do
 patch --batch --fuzz=0 -p1 -d "$lab_build/source" < "$lab_root/$patch_name" \
  >> "$lab_build/patch.log"
 sha256sum "$lab_root/$patch_name" >> "$lab_build/patches.sha256"
done
cmake -S "$lab_root/bwl-overlay" -B "$lab_build/build" \
 -DCMAKE_BUILD_TYPE=RelWithDebInfo \
 -DPEER_SOURCE="$lab_build/source" -DPEER_INSTALL="$lab_build/prpl-install-nl80211" \
 -DHOSTAP_SOURCE="$lab_root/build-hostap/hostap" \
 -DEMOSA_HE_REGRESSION="$lab_root/compatibility/he-length.cpp" \
 > "$lab_build/configure.log" 2>&1
grep -q '/nl80211/ap_wlan_hal_nl80211.cpp' "$lab_build/build/compile_commands.json"
if grep -q '/dummy/' "$lab_build/build/compile_commands.json"; then exit 1; fi
timeout 600 cmake --build "$lab_build/build" --parallel 1 > "$lab_build/build.log" 2>&1
cmake --install "$lab_build/build" --prefix "$lab_build/stage" >> "$lab_build/build.log" 2>&1
"$lab_build/build/emosa-he-length" > "$lab_build/regression.txt"
printf '%s\n' \
 '0dfa6fab56238bf0817c9a3299e671ab6ca66798858179b9262dd3d285d261b1  /opt/emosa-baseline/build-bwl/stage/lib/libbwl.so.6.0.0' \
 | sha256sum -c - >> "$lab_build/input-checks.txt"
if LD_LIBRARY_PATH="$lab_root/build-bwl/stage/lib:$lab_build/prpl-install-nl80211/lib" \
 "$lab_build/build/emosa-he-length" > "$lab_build/regression-before.txt"; then
 echo 'Expected the pinned old HAL to fail this regression' >&2
 exit 1
else
 test "$?" = 1
fi
sha256sum "$lab_build/stage/lib/libbwl.so.6.0.0" > "$lab_build/library.sha256"
dpkg-query -W -f='${Package}\t${Version}\n' cmake g++ gcc libnl-3-dev \
 libnl-genl-3-dev libnl-route-3-dev > "$lab_build/build-packages.txt"
cat "$lab_build/regression.txt" "$lab_build/library.sha256"
