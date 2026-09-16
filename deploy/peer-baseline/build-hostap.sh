#!/bin/bash
set -euo pipefail

test "$(hostname)" = emosa-lab
test "$(id -u)" = 0
test "$(systemd-detect-virt --vm)" = kvm

lab_build=/opt/emosa-baseline/build-hostap
printf '%s\n' \
  'c09dc125bcf20fb8c8334e39447831b62c25a7aa683b9d6f12cb7eb49e5f1cb8  /opt/peer-artifacts/hostap-source.tar.gz' \
  | sha256sum -c -
source_dir="$lab_build/hostap"
if [ "${1:-}" = --resume ]; then
  test -f "$source_dir/hostapd/.config"
  test -f "$source_dir/wpa_supplicant/.config"
elif [ -z "${1:-}" ]; then
  test ! -e "$lab_build"
  mkdir "$lab_build"
  tar -C "$lab_build" -xzf /opt/peer-artifacts/hostap-source.tar.gz
else
  exit 2
fi

# Pinned hostap source plus the recorded fixed-BSS UPDATE compatibility patch.
patch_path=/opt/emosa-baseline/0001-hostapd-fixed-bss-file-update.patch
if [ ! -f "$lab_build/applied-update-patch.sha256" ]; then
  patch --batch --fuzz=0 -p1 -d "$source_dir" < "$patch_path"
  sha256sum "$patch_path" > "$lab_build/applied-update-patch.sha256"
else
  sha256sum -c "$lab_build/applied-update-patch.sha256"
fi
# The companion lab artifact lacks WPS; explicitly enable it for bootstrap.
cp "$source_dir/hostapd/defconfig" "$source_dir/hostapd/.config"
sed -i \
  -e 's/^#CONFIG_WPS=y/CONFIG_WPS=y/' \
  -e 's/^#CONFIG_WNM=y/CONFIG_WNM=y/' \
  -e 's/^#CONFIG_IEEE80211AC=y/CONFIG_IEEE80211AC=y/' \
  -e 's/^#CONFIG_DEBUG_FILE=y/CONFIG_DEBUG_FILE=y/' \
  "$source_dir/hostapd/.config"
grep -qx 'CONFIG_WPS=y' "$source_dir/hostapd/.config"
grep -qx 'CONFIG_IEEE80211AC=y' "$source_dir/hostapd/.config"
cp "$source_dir/wpa_supplicant/defconfig" "$source_dir/wpa_supplicant/.config"
sed -i \
  -e 's/^#CONFIG_WPS=y/CONFIG_WPS=y/' \
  -e 's/^#CONFIG_WNM=y/CONFIG_WNM=y/' \
  -e 's/^#CONFIG_DEBUG_FILE=y/CONFIG_DEBUG_FILE=y/' \
  -e 's/^CONFIG_CTRL_IFACE_DBUS_NEW=y/#CONFIG_CTRL_IFACE_DBUS_NEW=y/' \
  -e 's/^CONFIG_CTRL_IFACE_DBUS_INTRO=y/#CONFIG_CTRL_IFACE_DBUS_INTRO=y/' \
  "$source_dir/wpa_supplicant/.config"
grep -qx 'CONFIG_WPS=y' "$source_dir/wpa_supplicant/.config"

timeout 600 make -C "$source_dir/hostapd" -j1 >> "$lab_build/hostapd-build.log" 2>&1
timeout 600 make -C "$source_dir/wpa_supplicant" -j1 >> "$lab_build/supplicant-build.log" 2>&1
install -Dm0755 "$source_dir/hostapd/hostapd" "$lab_build/stage/sbin/hostapd"
install -Dm0755 "$source_dir/hostapd/hostapd_cli" "$lab_build/stage/bin/hostapd_cli"
install -Dm0755 "$source_dir/wpa_supplicant/wpa_supplicant" "$lab_build/stage/sbin/wpa_supplicant"
install -Dm0755 "$source_dir/wpa_supplicant/wpa_cli" "$lab_build/stage/bin/wpa_cli"
cp "$source_dir/hostapd/.config" "$lab_build/hostapd.config"
cp "$source_dir/wpa_supplicant/.config" "$lab_build/supplicant.config"
dpkg-query -W -f='${Package}\t${Version}\n' build-essential gcc make pkg-config \
  libssl-dev libnl-3-dev libnl-genl-3-dev libnl-route-3-dev > "$lab_build/build-packages.txt"
tar -C "$lab_build/stage" -czf /opt/peer-artifacts/emosa-hostap-wps-2.10.tar.gz .
sha256sum /opt/peer-artifacts/emosa-hostap-wps-2.10.tar.gz
printf '%s\n' 'Built WPS-enabled hostap; runtime qualification still required'
