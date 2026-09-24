# Capability and topology reports: explain the represented extender

Continue here after [discovery and WSC](autoconfiguration.md). EMOSA must tell
an EasyMesh controller what the represented OpenSync extender can do and what
is currently present. This increment constructs restricted **Early AP Capability
Reports** and **Topology Responses**, checks their source facts and exercises
them offline and over real Ethernet sockets. It does not yet connect discovery,
reporting and WSC to a running controller coordinator or to pod operations.

## 1. Understand what each report answers

An **Early AP Capability Report**, message `0x8043`, describes capabilities before
configuration: radios, technology support and security suites. EasyMesh requires
it before M1 when the controller indicates early reporting. A **Topology
Response**, message `0x0003`, answers a Topology Query: interfaces, neighbors,
radios, active BSSs and clients. Capability is not current configuration. A radio
can support several BSSs while currently operating only one.

An **AP Capability Report**, message `0x8002`, is a separate procedure with its own
required fields. The early report cannot substitute for it. Full AP Capability
reporting, including the Metric Collection Interval dependency, remains pending.

| Observation | What it establishes | What still needs evidence |
| --- | --- | --- |
| Offline exercise decodes a response | The synthetic facts can be encoded, reassembled and interpreted | Socket delivery and independent controller behavior |
| Isolated Ethernet exercise receives those bytes | The packet endpoint transports the selected messages | Native controller acceptance and its own inventory |
| A native controller displays the represented AL/radio/BSS | That controller has interpreted the reports | WSC causing a guarded pod change and independent client behavior |
| Real controller request changes an unchanged physical extender | The target adaptation path works for the qualified case | Broader procedures, faults and supported device coverage |

The first two rows are implemented and tested here. Neither requires a radio:
these are management-message exercises. Use hwsim and the independent
wpa_supplicant client when testing whether an admitted configuration actually
produces working Wi-Fi.

## 2. Run the offline exercise on HOST

Use your development or learning checkout after `uv sync --frozen`. Do not run
these commands inside the radio containers. Choose a new output directory; the
command rejects an existing one to preserve earlier evidence.

```bash
uv run python -m emosa_lab.simulation.wire_reports --output .lab/reports-demo-01
uv run emosa-lab wire-inspect --capture .lab/reports-demo-01/synthetic-reports.pcap
```

The first command invents a controller, one agent, a 2.4 GHz HT radio, one
fronthaul BSS and an explicitly empty client list. It does not read a pod or open
a socket. It emits an Early Report, a Topology Query and the matching Response
into a three-frame PCAP. Synthetic timestamps are not timing measurements.

Read `result.json` in this order:

1. `passed: true` means this exercise's assertions succeeded.
2. `query_mid` and `response_mid` are both **65535**. A response echoes its query's
   message identifier; the independent early report uses a newly allocated MID.
3. `receiver_inventory` contains RUID `02:00:00:00:40:10` and BSSID
   `02:00:00:00:40:11`, decoded from the received Topology Response. Its source is
   explicitly synthetic, and `native_controller_inventory` is false.
4. `socket_io` is false and `operations_created` is zero. Both onboarding and
   physical-pod proof remain false. No Config transaction accompanies a report.

In the inspector, report messages have `procedure_validation` set to
`diagnostic_report_review_only`. Their `report_review` checks selected required
TLVs and supported values. Empty issue lists mean those checks passed;
`conditional_procedure_audit_complete` and `profile_qualified` remain false.
This inspector is not a complete EasyMesh conformance validator.

## 3. Inspect the individual values

A **TLV** is a typed value inside a management message. New codecs cover AKM
suites, cipher suites, BSS configuration and associated clients. AKM describes
authentication/key management; a cipher suite identifies data encryption.

```bash
uv run emosa-lab payload --type 0xcc --value-hex 0001000fac02
uv run emosa-lab payload --type 0xed --value-hex 01000fac04
uv run pytest tests/test_wire_reports.py -q
python3 scripts/check-report-reference.py
```

The first value has zero backhaul AKM selectors and one fronthaul selector:
`00-0F-AC:2`, PSK. The second has one cipher selector: `00-0F-AC:4`, CCMP-128.
An empty list means no advertised entry; the implementation never converts it
into presumed PSK support. Generic codecs preserve four-byte vendor selectors;
the restricted report builder admits only the explicitly selected PSK/CCMP pair.

The tests check complete message contents, identities, conditional fields,
malformed values, fragmentation, source changes and deadlines. The independent
checker requires `tshark` and reads the retained native capture without importing
EMOSA. Its five reference cases cover raw field/value comparisons, including a
selected-edition negative. Read the [fixture provenance](../../tests/fixtures/protocol/reports/README.md)
before changing expected bytes.

## 4. Cross the Ethernet boundary in the dedicated VM

Follow the staging and ownership checks in the
[packet endpoint runbook](../../deploy/wire/README.md), then select its `--reports`
mode. The driver creates two private namespaces connected only by a veth pair.
The right process receives the Early Report, sends a Query and checks the
Response. The left process builds its replies from the same synthetic facts as
the offline exercise. Both use Linux AF_PACKET sockets.

Keep the driver output, each worker's JSON, received-byte PCAP and logs. Expect
one received Query on the left, and an Early Report followed by a Topology
Response on the right. The Query/Response MID remains 65535. Confirm the created
namespaces disappeared after the run. The retained [report evidence](../evidence/reports/README.md)
contains two successful executions.

The receiver's decoded inventory is useful transport evidence. It is not the
prplMesh controller database. PCAP bytes came from receive calls, but timestamps
were generated for inspection; do not calculate wire latency from them.

## 5. Supply facts, not guesses

`src/emosa/wire/reports.py` is a Python component, not a daemon. The future
coordinator must obtain qualified facts, establish controller/link authority and
bind the report to the correct pod. The current synthetic callers demonstrate
that contract; constructing a dataclass does not establish qualification.

The early builder currently admits complete, non-DFS 2.4 GHz inventories with
explicit HT support or absence, explicit absence of VHT/HE/EHT, pure fronthaul
PSK/CCMP and the implemented capability flags. Unknown support is rejected.
HE/EHT, backhaul roles or other features require their complete companion fields
before this builder can expand its scope.

The topology builder requires matching AL, interface, RUID, BSSID and SSID facts.
Operational BSS and BSS Configuration Report must agree. Each active BSS needs
its matching local AP interface and an explicit client list, including an empty
one when there are no clients. Bridge and neighbor references must resolve to
present interfaces. For more than one local interface, the IEEE Bridging
Capability TLV is included even when there are zero bridge tuples.

Powered-off interfaces, L2-neighbor extensions, MLD, backhaul, virtual BSS and
TID-policy conditions must explicitly be absent for this restricted response.
A missing inventory is not evidence of absence. Physical backhaul and the
adapter's virtual adjacency also need a qualified mapping; the fixture's
invented bridge is not a physical-pod discovery result.

Associated Clients reports require **seconds since association**, saturated at
65535. The pinned OpenSync `Wifi_Associated_Clients` schema has no association-age
field. EMOSA's first observation of a client may happen long after association,
particularly after restart. It cannot supply an invented age of zero or treat
first-seen time as association time. Actual pod qualification must identify an
existing trustworthy source, or a qualified observation of the association
episode, before this field can be reported for physical clients. This input
remains pending; the fixture has no clients.

## 6. Separate three different deadlines

The **one-second Topology Response deadline** begins at local receipt of the
complete Query. It comes from IEEE §8.2.2.2. It is unrelated to the potentially
longer time needed for OpenSync to apply a configuration.

The **report freshness window** is a local component policy: at most two seconds
from observation. `ReportStamp` carries an opaque coordinator-issued token and
monotonic times. The token must change when the adapter instance, pod binding,
generation, database revision or relevant input hashes change. Equality provides
correlation, not authentication of the facts. Renewing a token's lease cannot
extend a previously prepared report's original lifetime.

The **pod apply deadline** belongs to a future admitted operation. A packet send
does not start or satisfy it. `PreparedReport.send()` rechecks the source and
clock before and after every fragment. An expired or changed source stops
transmission. A late or partial send raises an error; already emitted bytes
cannot be recalled. A successful return counts sent fragments, not a controller
acknowledgment, inventory update or applied configuration. The coordinator still
needs the applicable EasyMesh acknowledgment/retry and scheduling behavior.

## 7. Read the retained native compatibility findings

Inspect the existing wired baseline using `emosa-lab wire-inspect`. It still has
59 frames and 58 completed messages; structural inspection does not imply full
selected-edition validity. The new diagnostic findings include:

- Frame 3's Early Report lacks Supported Cipher Suites `0xED`, required by the
  selected EasyMesh 6.1 early-report rules. Its AKM lists are both empty.
- Frame 20's Device Information declares five interfaces but has no Bridging
  Capability `0x04` alongside it.
- Its Wi-Fi 6 media entries use ten media-specific bytes. EasyMesh 6.1 §6 Table 14
  specifies zero for media type `0x0108`; the selected decoder rejects that value.

Older dissectors can label these bytes without enforcing the selected edition.
Keep the observations as compatibility work, alongside the earlier profile 2/1
and controller-capability findings. Do not modify the normative rules to make an
older capture pass. No native peer or original capture was changed here.

Continue with the [read-only coordinator walkthrough](report-coordinator.md) to
connect this contract to a real disposable database. Its Query/Ack/retry loop and
source withdrawal are implemented; discovery/profile and WSC-operation admission
remain separate pending work.

## 8. References and next implementation boundary

These are section/page references to the locally reviewed editions, not copies
of licensed text. Hashes and access records are in the
[protocol matrix](protocol-matrix.json).

| Contract | Selected authority |
| --- | --- |
| Device/bridge/neighbor inclusion and value layouts | IEEE 1905.1-2013 §6.3.3 p.31; §§6.4.5–9 pp.35–38 |
| Power-off/L2 conditions; reserved media/role handling | IEEE 1905.1a-2014 §6.3.3 p.7; §6.4.5 p.8; §6.4.7 pp.9–10 |
| Topology Query MID echo and one-second response | IEEE 1905.1-2013 §8.2.2.2 p.48 |
| Wi-Fi 6/7 media-specific lengths; topology extensions | EasyMesh 6.1 §6 Table 14 p.62; §6.2 pp.63–64; §17.1.4 pp.111–112 |
| Early report order and required contents | EasyMesh §5.2.2 p.28; §17.1.62 p.119; Table 22 pp.107–110 |
| Clients, BSS configuration, AKM and ciphers | EasyMesh §§17.2.5, 17.2.75, 17.2.78, 17.2.109; Tables 28, 98, 101, 132 |
| PSK and CCMP selector meanings | IEEE 802.11-2024 §9.4.2.23.2 Table 9-188 p.1048; §9.4.2.23.3 Table 9-190 pp.1049–1050 |

Next, connect qualified per-pod inventory to a running trusted-link coordinator,
resolve peer/profile and full AP Capability obligations, and prove controller
inventory visibility. Then bind an admitted, authenticated WSC candidate to one
durable operation and reproduce the causal change with OVSDB/hwsim and an
independent client. Only after read-only actual-pod qualification can that pod
replace the simulator. The acceptance path remains **real EasyMesh messages →
EMOSA → unchanged physical pod → independently observed behavior**.
