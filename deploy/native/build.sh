#!/usr/bin/env bash
# Build a fresh source extraction only in the dedicated native candidate.
set -euo pipefail
if [[ $(hostname) != opensync-native-r0 || $(systemd-detect-virt --container) != lxc ]]; then
  echo 'Run only in the opensync-native-r0 LXD container.' >&2
  exit 1
fi
lab_sources=$(cd "$(dirname "$0")" && pwd)
native_root=${1:-/opt/native/reproduction}
if [[ $native_root != /opt/native/reproduction || -e $native_root ]]; then
  echo 'The fresh build must use an absent /opt/native/reproduction directory.' >&2
  exit 1
fi
echo '65ff15d01367a8125de2f41a8c81892a13e88f2545ac4080cd81ac405c239a08  /opt/opensync-source.tar' | sha256sum -c -
mkdir -p "$native_root/device/core"
tar -xf /opt/opensync-source.tar -C "$native_root/device/core"
python3 -m venv "$native_root/build-venv"
"$native_root/build-venv/bin/pip" install kconfiglib==14.1.0 jinja2==3.1.6 MarkupSafe==3.0.3
export PATH="$native_root/build-venv/bin:$PATH" TERM=dumb
cd "$native_root/device/core"
for patch in "$lab_sources"/patches/*.patch; do
  git apply --check "$patch"
  git apply "$patch"
done
timeout 300 make TARGET=native CC=gcc src/owm -j2
gcc -shared -fPIC -Wall -Wextra -Wno-unused-parameter -Werror \
  -I src/lib/osw/inc -I src/lib/ds/inc -I src/lib/module/inc \
  -I src/lib/common/inc -I src/lib/osa/inc \
  "$lab_sources/driver.c" -o "$native_root/driver.so"
# Reuse only the separately recorded Ubuntu 24.04 OVSDB tools, no switch daemon.
test -x /opt/native/ovsdb/ovsdb-server
ln -s /opt/native/ovsdb "$native_root/ovsdb"
sha256sum work/native-gcc-ubuntu24.04-x86_64/bin/owm \
  work/native-gcc-ubuntu24.04-x86_64/lib/libow.so \
  work/native-gcc-ubuntu24.04-x86_64/lib/libosw.so "$native_root/driver.so"
