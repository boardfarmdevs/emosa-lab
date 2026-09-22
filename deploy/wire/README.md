# Isolated IEEE 1905 Ethernet endpoint check

Read the [envelope learning exercise](../../doc/protocol/ieee1905-envelope.md)
first. This tests the packet transport only. It does not start a controller,
respond to a topology query, advertise a virtual agent or write pod configuration.

Use the existing dedicated **emosa-lab VM**. Commands below begin on **HOST** at
the checkout root. Its retained `/opt/emosa/.venv/bin/python` supplies Python 3.13;
the staged source directory supplies the new packet code. This is an explicitly
staged-source test, not another qualified installed runtime image. The selected
module only uses Python's standard library and EMOSA's error definitions.

1. Confirm VM identity, capacity and absence of the new staging directory:

   ```bash
   lxc exec emosa-lab -- hostname
   lxc exec emosa-lab -- df -h /opt
   lxc exec emosa-lab -- ip netns list
   lxc exec emosa-lab -- test ! -e /opt/emosa-wire-check
   ```

   Only a few MiB are needed; do not build a new native source tree here. Existing
   baseline containers and PHYs need no change. Use another absent staging/output
   name for a repeat instead of removing somebody else's evidence.

2. Stage the explicit code bundle. Include `emosa/__init__.py`: without it an
   older installed package can shadow the staged directory.

   ```bash
   mkdir -p .cache/ieee1905
   tar --exclude=__pycache__ -cf .cache/ieee1905/wire-source.tar \
     src/emosa/__init__.py src/emosa/errors.py src/emosa/wire deploy/wire
   lxc file push .cache/ieee1905/wire-source.tar emosa-lab/opt/emosa-wire-source.tar
   lxc exec emosa-lab -- mkdir /opt/emosa-wire-check
   lxc exec emosa-lab -- tar -xf /opt/emosa-wire-source.tar -C /opt/emosa-wire-check
   ```

3. Run the check as the VM's root account. That privilege is needed for creating
   private network namespaces and opening Ethernet packet sockets.

   ```bash
   lxc exec emosa-lab -- env PYTHONPATH=/opt/emosa-wire-check/src \
     /opt/emosa/.venv/bin/python \
     /opt/emosa-wire-check/deploy/wire/check-endpoint.py \
     --directory /opt/emosa-wire-check/run-01
   ```

   The output directory must be new. The driver creates unique names beginning
   `emosa-wire-`, a veth pair with synthetic MACs, and two socket processes. The
   right-hand process becomes ready before the left sends queries. Both receive
   MIDs 65535 and 0. No IP address, default route or external bridge is configured.
   The driver stops only its children and deletes only namespaces it created,
   including on a caught failure. An abrupt kill of the driver/VM may require the
   lab owner to inspect and clean its recorded namespace names manually.

4. Retain and inspect the result on HOST:

   ```bash
   lxc file pull -r emosa-lab/opt/emosa-wire-check/run-01 .cache/ieee1905/
   uv run emosa-lab wire-inspect --capture .cache/ieee1905/run-01/left.pcap
   uv run emosa-lab wire-inspect --capture .cache/ieee1905/run-01/right.pcap
   lxc exec emosa-lab -- ip netns list
   ```

   Each worker report should show two received frames and `passed: true`.
   Compare the namespace listing with the preflight: the created pair should be
   gone. PCAP timestamps are synthetic; bytes came from actual receive calls.
   This exercise measures delivery, not a physical Ethernet timing or FCS property.
   Keep a failed worker log and the command tuple before correcting a run.

For controller-facing acceptance continue with the
[first complete wire experiment](../../doc/guides/first-wire-experiment.md).
