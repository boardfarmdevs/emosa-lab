#!/usr/bin/env bash
# Run as root only inside the dedicated, disposable runtime-build container.
set -euo pipefail
test "$(id -u)" = 0
test -d /root/emosa-bundle
test ! -e /opt/emosa-runtime
cd /root/emosa-bundle
sha256sum -c SHA256SUMS
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y build-essential pkg-config libssl-dev curl ca-certificates python3
install -m 0755 uv /usr/local/bin/uv
uv --version | grep -Fx 'uv 0.11.17 (x86_64-unknown-linux-gnu)' || uv --version | grep -E '^uv 0\.11\.17([ (]|$)'
export UV_PYTHON_INSTALL_DIR=/opt/emosa-python
uv python install 3.13.7
uv venv --python 3.13.7 /opt/emosa-runtime
uv pip sync --python /opt/emosa-runtime/bin/python --require-hashes requirements.txt
uv pip install --python /opt/emosa-runtime/bin/python --no-deps emosa-*.whl emosa_lab-*.whl
bash scripts/build-ovsdb.sh
mkdir -p /opt/emosa-ovs /opt/emosa-evidence /opt/emosa-tests
install -m 0755 .cache/upstream/openvswitch-4.0.0/ovsdb/ovsdb-{server,tool} /opt/emosa-ovs/
cp tests/test_tls_listener.py /opt/emosa-tests/
cp .cache/upstream/openvswitch-4.0.0/*-emosa.log /opt/emosa-evidence/
cp SHA256SUMS requirements.txt /opt/emosa-evidence/
dpkg-query -W > /opt/emosa-evidence/packages.tsv
uv pip freeze --python /opt/emosa-runtime/bin/python > /opt/emosa-evidence/python-packages.txt
sha256sum /opt/emosa-ovs/* > /opt/emosa-evidence/ovs-sha256.txt
/opt/emosa-runtime/bin/python --version > /opt/emosa-evidence/python-version.txt
# The image is published before any experiment generates keys, databases or journals.
useradd --create-home --shell /bin/bash emosa
chmod -R a+rX /opt/emosa-runtime /opt/emosa-python /opt/emosa-tests /opt/emosa-evidence
cat > /opt/emosa-evidence/pytest.ini <<'EOF'
[pytest]
markers =
    ovsdb: disposable real OVSDB transport test
EOF
