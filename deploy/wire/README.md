# Isolated IEEE 1905 Ethernet endpoint check

Read the [envelope learning exercise](../../doc/protocol/ieee1905-envelope.md)
first. Default mode tests packet transport with empty Queries. The optional
`--reports` mode exchanges restricted capability/topology reports using synthetic
facts. Neither mode starts a native controller or writes pod configuration.

Use the existing dedicated **emosa-lab VM**. Commands below begin on **HOST** at
the checkout root. Its retained `/opt/emosa/.venv/bin/python` supplies Python 3.13;
the staged source directory supplies the new packet code. This is an explicitly
staged-source test, not another qualified installed runtime image. The selected
packet module uses Python's standard library; the report path also imports the
existing EMOSA protocol components and their installed dependencies.

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
     src/emosa deploy/wire tests/fixtures/opensync schemas
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
   right-hand process becomes ready before the left sends queries. In default mode both receive
   MIDs 65535 and 0. No IP address, default route or external bridge is configured.
   The driver stops only its children and deletes only namespaces it created,
   including on a caught failure. An abrupt kill of the driver/VM may require the
   lab owner to inspect and clean its recorded namespace names manually.

   To exercise reports instead, use a separate new output directory:

   ```bash
   lxc exec emosa-lab -- env PYTHONPATH=/opt/emosa-wire-check/src \
     /opt/emosa/.venv/bin/python \
     /opt/emosa-wire-check/deploy/wire/check-endpoint.py \
     --reports --directory /opt/emosa-wire-check/reports-01
   ```

   The left sends an Early AP Capability Report. The right validates its cipher
   field, sends a Profile-1 Topology Query with MID 65535, and receives a Response
   with the same MID and the fixture RUID/BSSID. This uses synthetic, explicitly
   complete facts; it does not attach the running adapter service or real pod.
   Left receives one frame; right receives two. Read the
   [report walkthrough](../../doc/protocol/reports.md) for source-freshness and
   one-second response-deadline checks.

4. Retain and inspect the result on HOST:

   ```bash
   lxc file pull -r emosa-lab/opt/emosa-wire-check/run-01 .cache/ieee1905/
   uv run emosa-lab wire-inspect --capture .cache/ieee1905/run-01/left.pcap
   uv run emosa-lab wire-inspect --capture .cache/ieee1905/run-01/right.pcap
   lxc exec emosa-lab -- ip netns list
   ```

   In default mode each worker report should show two received frames and
   `passed: true`. For `--reports`, substitute `reports-01` for `run-01` in the
   pull/inspection commands and expect the one/two-frame counts described above.
   Compare the namespace listing with the preflight: the created pair should be
   gone. PCAP timestamps are synthetic; bytes came from actual receive calls.
   This exercise measures delivery, not a physical Ethernet timing or FCS property.
   Keep a failed worker log and the command tuple before correcting a run.

For controller-facing acceptance continue with the
[first complete wire experiment](../../doc/guides/first-wire-experiment.md).

## Database-backed report coordinator

Read the [coordinator walkthrough](../../doc/protocol/report-coordinator.md)
first. Stage the full bundle from step 2; it includes the pinned schema needed by
the disposable database. This mode needs the selected OVSDB 4.0.0 tools and the
existing installed Python dependencies. It needs no hwsim PHY or native controller.

In the retained VM, the earlier native-manager experiment left the selected
tools at `/opt/native-ovsdb-server` and `/opt/native-ovsdb-tool`. Verify them and
create symlinks only inside the new staging directory:

```bash
lxc exec emosa-lab -- /opt/native-ovsdb-server --version
lxc exec emosa-lab -- /opt/native-ovsdb-tool --version
lxc exec emosa-lab -- mkdir /opt/emosa-wire-check/bin
lxc exec emosa-lab -- ln -s /opt/native-ovsdb-server /opt/emosa-wire-check/bin/ovsdb-server
lxc exec emosa-lab -- ln -s /opt/native-ovsdb-tool /opt/emosa-wire-check/bin/ovsdb-tool
lxc exec emosa-lab -- env PYTHONPATH=/opt/emosa-wire-check/src \
  EMOSA_OVS_BIN=/opt/emosa-wire-check/bin \
  /opt/emosa/.venv/bin/python /opt/emosa-wire-check/deploy/wire/check-endpoint.py \
  --coordinator --directory /opt/emosa-wire-check/coordinator-01
```

On a fresh VM those retained paths may not exist. Build the selected tools using
[manual chapter 5](../../doc/guides/team-manual.md#5-build-and-exercise-the-real-ovsdb-simulator)
in that environment and point `EMOSA_OVS_BIN` to their directory. Do not assume the
binaries are included in a Git clone. Use a new staging name if `bin` already
exists; do not replace another experiment's links.

The left worker starts an owned real database and separate manager, with a
pod-initiated read-only monitor. The right worker deliberately drops one Ack and
queries before/after fixture Config and manager State changes. Each worker
receives five frames. Queries 600/601/602 receive Responses; Query 603 receives
none after database disconnection. Both Early Report MIDs (1 and 2) differ, and
Ack MID 2 appears in the left capture. Root is required only for the namespace
and packet-socket setup. The driver allows extra readiness time for its database.

Pull `coordinator-01` using step 4's command pattern and inspect worker JSON and
PCAPs. Fixture marker files synchronize stage readiness; their role is described
in the walkthrough. Finalizers preserve received-byte traces, close the owned
database/manager and remove owned namespaces. As with other modes, an abrupt VM
kill can require owner inspection. The adapter reports zero Config writes; the
fixture administrator/manager explicitly perform the simulated changes.
