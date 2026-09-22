# Discover the native controller with EMOSA's own Search

This experiment connects EMOSA's bounded Profile-1 Search directly to the pinned
prplMesh controller. The controller replies and creates a device entry for the
probe AL, `02:00:00:00:30:01`. **The entry has zero radios and zero BSSs.** This is
the first native discovery visibility measurement, not completed onboarding.

The [retained evidence](../evidence/native-discovery/README.md) includes two clean
runs, independent Ethernet captures and the controller's own before/after
inventory. No OVSDB service, physical pod, M1/M2 or configuration operation is
involved. The complete service's admission gate remains closed.

## 1. Understand why this is a separate experiment

The earlier standard-agent baseline sends a Profile-2 Search and receives
Profile 1. EMOSA's bounded component sends Profile 1. Testing the latter exchange
directly distinguishes an observed baseline mismatch from the behavior of the
actual message EMOSA would send.

The native response does match EMOSA's Profile 1, band and message identifier.
It still carries Controller Capability `0x40`: the named Early Capability bit is
set, but the required KiB/MiB bit is absent. The Security Capability TLV is also
absent. Their applicability and the Table 117 ambiguity remain in the
[procedure audit](../protocol/procedure-audit.md).

The controller inserts a device object while processing Search, before WSC. Even
its `EasyMeshAgentOperationMode: Running` field does not establish onboarding:
the represented radio count is zero and no BSS exists. Always inspect the device,
radio and BSS together and correlate them with the actual protocol exchange.

## 2. Prepare the existing lab — HOST

Complete the [native baseline setup](../../deploy/peer-baseline/README.md) and
the [controller preparation prerequisites](service-integration.md). Use the
existing owned VM and pinned artifacts. This experiment does not build a new
controller or adopt an optional external 1905 backend.

Check that other native/radio trials are stopped and collected. The runner checks
ownership and acquires the existing radio-manager experiment lock. It starts the
controller and its colocated native helper in `em-baseline-controller`; the
external native agent remains stopped. The helper needs its already assigned
hwsim radio to initialize the native transport. No client test runs here.

From the HOST checkout, stage these new files after the normal baseline and
radio-manager staging:

```bash
uv sync --frozen
lxc file push src/emosa/wire/controller_probe.py \
  emosa-lab/opt/emosa-radio-manager/source/emosa/wire/controller_probe.py
lxc file push deploy/peer-baseline/controller-trial.py \
  deploy/peer-baseline/discovery-trial.py emosa-lab/opt/emosa-baseline/
```

The existing staged `run.py`, `setup.py`, `node.py`, references and Python runtime
must match the baseline. The run records their hashes and validates the native
binary/HAL hashes through the baseline launcher.

## 3. Run a fresh trial — HOST command, VM execution

Choose a new lowercase label of at most 24 characters:

```bash
lxc exec emosa-lab -- env \
  PYTHONPATH=/opt/emosa-radio-manager/source \
  /opt/emosa/.venv/bin/python /opt/emosa-baseline/discovery-trial.py \
  --label learning-discovery-01
```

The runner waits for the native transport to own a packet socket on the selected
bridge/interface. A successful inventory API call alone is too early. It creates
a temporary namespace/veth on the owned `em-base-bh` network, captures that link
independently, and sends at most three Searches within five seconds. A live
correlated Response ends the probe. The packet receiver has bounded reassembly,
input rate and receive count. It cannot emit M1 or invoke the operation engine.

The expected run status is `response_observed_admission_pending`. This means the
measurement completed, not that the controller passed all compatibility checks.
A timeout, failed capture or cleanup error fails the run. The runner retains
results under `/opt/emosa-baseline/discovery-trials/LABEL/`, removes its namespace
and veth, and stops/collects its native services. Existing native shutdown aborts
are recorded separately as `native_shutdown_abnormal`; they are not erased from
the result. Containers and hwsim assignments remain available for later trials.

## 4. Read the evidence in order

1. In `probe.json`, find Search and Response profile **1**, matching MID and
   `profile_correlated: true`. Read both `selected_response_issues` entries.
2. In `controller-before.json`, confirm the probe AL is absent. In
   `controller-after.json`, locate its device object and verify
   `RadioNumberOfEntries: 0`. The result's `inventory_after.probe_entry` is a
   compact projection of these raw controller fields.
3. Decode `ethernet.pcap`: it contains the probe's Search and the native Response,
   not WSC. This tcpdump capture uses actual capture timestamps.
4. Confirm `wsc_started: false`, `operations_created: 0` and both onboarding and
   physical proof flags false. Inspect cleanup and shutdown results too.

On HOST, reproduce the independent check of the published runs:

```bash
python3 scripts/check-controller-probe.py \
  --directory doc/evidence/native-discovery/run-06 \
  --directory doc/evidence/native-discovery/run-07
```

This needs tshark and imports no EMOSA code. It verifies the captured fields,
request/response ordering, absence of WSC and the controller's initial device
entry. To check a new private run, copy only the same named evidence files to a
private HOST directory and pass that directory instead. Keep native logs and
configurations private; do not publish a whole run directory.

## 5. Continue toward full onboarding

The next step is to resolve the actual controller capability omissions and the
selected procedure contract, then connect Early/AP capabilities, topology and
WSC to the running virtual-agent coordinator. The pinned controller source
already exposes a KiB/MiB support field but its Search-response builder sets only
the Early flag. A candidate fix must validate the associated behavior and be
rebuilt/tested separately; it must not simply fabricate a compatible response in
EMOSA. The [counter-capability candidate exercise](controller-counter-candidate.md)
now supplies that isolated build, native conversion regression, differential
packet measurement and automatic baseline restoration.

After admission is qualified, reuse the tested
[packet-to-Wi-Fi WSC path](../protocol/wsc-wire-radio.md). Require the controller
to acquire the correct radio/BSS inventory and cause the observed configuration
change in the same run. Then qualify and substitute an unchanged physical pod.
