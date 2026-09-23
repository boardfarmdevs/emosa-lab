#!/usr/bin/env bash
# Explicit, unprivileged test dependency build. No install, daemon or datapath.
set -euo pipefail
cd "$(dirname "$0")/.."
version=4.0.0
archive=.cache/upstream/openvswitch-$version.tar.gz
mkdir -p .cache/upstream
if [[ ! -f "$archive" ]]; then
  curl --fail --location "https://www.openvswitch.org/releases/openvswitch-$version.tar.gz" -o "$archive"
fi
echo "6c94e1e019a7f36ef40a9ae34fb21ab2534dbb78e5bea83338452e07a11becf2  $archive" | sha256sum -c -
tar -xf "$archive" -C .cache/upstream
cd ".cache/upstream/openvswitch-$version"
pkg-config --exists openssl
./configure --enable-ssl --without-libcapng > configure-emosa.log 2>&1
cat > emosa-generated.mk <<'EOF'
emosa-generated: $(BUILT_SOURCES)
EOF
make -f Makefile -f emosa-generated.mk -j"${EMOSA_BUILD_JOBS:-4}" emosa-generated > generated-emosa.log 2>&1
make -j"${EMOSA_BUILD_JOBS:-4}" ovsdb/ovsdb-server ovsdb/ovsdb-tool > build-emosa.log 2>&1
./ovsdb/ovsdb-server --version
sha256sum ovsdb/ovsdb-server ovsdb/ovsdb-tool
