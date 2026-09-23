# Technology capabilities and Device Inventory

This exercise shows how EMOSA can describe a represented OpenSync pod using
selected EasyMesh capability values. It adds **HT/VHT technology inputs** and
**Device Inventory** to the existing read-only radio-capability diagnostic.
It also provides an offline HE value inspector. These are components needed
for capability reporting; they do not complete controller onboarding.

Run the commands on **HOST**, in your development or learning checkout. The
single-value exercises need only the Python installation from
[manual chapter 3](team-manual.md#3-set-up-a-developer-checkout). The service demo
also needs the repository's disposable OVSDB binaries. It uses no LXD container,
hwsim radio, wireless client or physical pod.

## 1. Learn what these values describe

HT, VHT and HE are the IEEE technology families commonly called Wi-Fi 4, Wi-Fi 5
and Wi-Fi 6. A radio can support more than one family. They are not interchangeable
with the radio's current channel or the speed of a currently connected client.

| Information | Meaning | Why the adapter needs it |
| --- | --- | --- |
| Basic radio capabilities | Supported operating classes, channels and BSS capacity | Describes where and how many BSSs the represented radio can support |
| HT/VHT capabilities | Stream limits, guard intervals, channel-width features and selected beamforming capabilities | Describes technology-specific capabilities a controller may use when choosing configuration |
| VHT MCS maps | Supported modulation/coding range for each number of spatial streams, separately for transmit and receive | Preserves asymmetric hardware capabilities rather than assuming Tx and Rx are identical |
| Device Inventory | Stable device serial, active firmware, active execution environment and each radio's chipset vendor | Identifies the represented device and the software whose behavior must be qualified |

A **spatial stream** is an independent stream used by the radio's MIMO operation.
An **MCS** identifies a modulation and coding combination. A capability describes
what could be supported; it is not a measurement of throughput or a promise that
all combinations are currently usable.

Device Inventory must describe the **represented pod**. EMOSA's Python version,
the developer laptop's operating system and a container image tag are not
substitutes for the pod's firmware and execution environment.

## 2. Inspect standalone values first

Decode the source-derived HT example:

```bash
uv run emosa-lab payload --type 0x86 --value-hex 0200000140017e
```

The first six octets are the RUID, the stable radio identifier. The last octet
indicates two transmit streams, four receive streams, short guard intervals for
20/40 MHz and 40 MHz HT support. Its reserved bit is zero. The decoder reports
these fields; it does not contact a radio or verify their truth.

Now inspect an asymmetric VHT example:

```bash
uv run emosa-lab payload --type 0x87 --value-hex 020000014001fff6ffc92a20
```

Read `tx_mcs_codes` and `rx_mcs_codes` in **NSS 1 through NSS 8 order**, where NSS
is the number of spatial streams. Each two-bit code means:

| Code | Supported VHT MCS range for that NSS |
| --- | --- |
| 0 | MCS 0–7 |
| 1 | MCS 0–8 |
| 2 | MCS 0–9 |
| 3 | That NSS is unsupported |

This example has two transmit streams and three receive streams. The wire-value
map words are big-endian, while NSS 1 occupies the lowest two bits of each word.
Reversing those meanings would report the wrong radio capabilities. The tests
use asymmetric maps specifically to expose that error. No data-rate calculation
or raw IEEE Information Element parser is implemented here.

## 3. Understand the HE boundary

The HE codec preserves an already ordered MCS field and decodes the separate
stream/feature flags:

```bash
uv run emosa-lab payload --type 0x88 --value-hex 020000ec020004000000402436
```

`mcs_interpretation: "opaque_mapping_pending"` is intentional. The source defines
an IEEE-derived variable-length field reordered for EasyMesh. IEEE 802.11 defines
the component receive/transmit maps, optional widths and per-NSS codes. The
conversion between those structures still needs a reviewed mapping and
independent asymmetric examples. Older dissector labels do not settle it.

The codec accepts MCS-field lengths of 4, 8 or 12 octets. It reports whether the
length matches the 160 MHz and 80+80 MHz flags; the encoder rejects a mismatch.
It does not identify the individual Tx/Rx maps or infer their supported MCSs.
A complete HE service mapper must also supply the required **Wi-Fi 6 Capabilities
value (`0xAA`)** when HE is supported. A working `0x88` codec alone is insufficient.
The [HE/Wi-Fi 6 walkthrough](he-wifi6.md) now provides that standalone codec
and an IEEE MCS field parser. The [Wi-Fi 6 input exercise](wifi6-inputs.md)
now maps explicit role inputs in a separate service section; the full HE set
stays blocked until the `0x88` conversion is resolved.

## 4. Run the complete local service demonstration

Build the disposable OVSDB tools if this checkout does not already have them:

```bash
bash scripts/build-ovsdb.sh
```

Choose a fresh output directory; the demo refuses to overwrite an existing run:

```bash
uv run python -m emosa.simulation.radio_capabilities \
  --with-extensions --output .lab/technology-inventory-first
```

Expect `passed: true` and a path to `report.json`. The demo starts a real EMOSA
process and two disposable OpenSync-shaped OVSDB databases. Each database
connects to its adapter listener. The adapter reads both pods' complete synthetic
topology and maps explicitly supplied capability inputs.

The fixture invents an HT/VHT pod with two radios. Radio 1 has HT and VHT;
radio 2 has HT and explicitly lacks VHT. Both explicitly lack HE for this
selected demonstration. These declarations describe the fixture, **not hwsim or
your physical hardware**. Its 5 GHz inventory includes classes 115, 116, 117
and 128; the only operable 80 MHz center channel is 42. Current primary channel
36 is validated separately using the primary-channel classes.

The run checks input/evidence tampering, regulatory-context and firmware
changes, disconnection, reconnection and adapter process restart. These are
controlled changes to the disposable fixtures. It verifies that all monitored
rows match their initial values after restoring those deliberate faults. The
adapter itself runs in read-only mode. The demo stops its own services at the end.

Inspect the successful mapping:

```bash
uv run python - <<'PY'
import json
from pathlib import Path
report = json.loads(Path('.lab/technology-inventory-first/report.json').read_text())
pod = report['stages']['connected'][0]
print('Basic radio inputs ready:', pod['ready'])
for name, extension in pod['extensions'].items():
    print(name, 'ready:', extension['ready'], 'blockers:', extension['blockers'])
    print('Types:', [value['type'] for value in extension['values']])
PY
```

Expect technology types `0x86`, `0x87`, `0x86` and one Device Inventory `0xD4`.
The inventory contains both bound RUIDs. The same extension values should appear
after reconnect and restart. The retained [service evidence](../evidence/technology-inventory/summary.json)
records this exercise and its limits.

## 5. Read each readiness result separately

The existing command for an independently running service is:

```bash
uv run emosa --socket /absolute/path/to/control.sock pod pod-1 radio-capabilities --json
```

Replace the socket path with that service's local socket. The demonstration
above has already stopped, so its socket is no longer available.

| Result | What `ready: true` means |
| --- | --- |
| Top-level `ready` | Existing Basic Radio Capabilities checks passed; `ready_scope` explicitly identifies this scope |
| `extensions.technology.ready` | All radios have complete selected HT/VHT declarations and explicit HE absence, consistent with the audited input subset |
| `extensions.device_inventory.ready` | The declared inventory maps to fresh observed identity/firmware and the complete bound radio list |
| `complete_ap_capability_report` | Still false: additional fields and procedures remain missing |
| `qualified_easymesh_profile` | Still null: no profile qualification is asserted |

The CLI's existing exit codes still follow **top-level Basic readiness**: 0 when
ready, 5 when blocked. Inspect both extension results separately. An older valid
input file can therefore return Basic readiness with missing extension inputs.
A stale or inconsistent Basic context withdraws every extension value. A
technology-only fault withdraws that entire technology value set while leaving
an independently valid Device Inventory diagnostic available, and vice versa.

## 6. Understand the input contract before editing it

The existing `radio_capabilities` configuration reference points to a private
absolute path and an exact SHA-256 digest. Its
[schema](../../schemas/radio-capabilities.schema.json) now accepts optional
`extensions.technology` and `extensions.device_inventory` sections. The demo
creates complete examples under its private `pod-1/` and `pod-2/` directories.
Use those as learning fixtures, not as qualified pod profiles.

Each technology row names an existing `radio_id` and evidence ID:

- `ht` and `vht` are `null` for unknown, `false` for explicitly unsupported, or
  an object containing **every** required normalized field.
- `he` is explicitly `null`, `false` or `true`. Unknown blocks the technology
  result; true reports `he_and_wifi6_mapping_pending` and emits no technology values.
- Eight VHT MCS codes are required for each direction. Their maximum supported
  NSS must agree with the separately declared stream count. Stream counts are
  actual counts, not the count-minus-one wire encoding.
- Short-GI claims must agree with their width flags. HT40 requires an audited
  40 MHz class; VHT requires audited class 128. Selected service mapping of
  160/80+80 MHz remains blocked until the wider class/context review is completed.
- EHT/Wi-Fi 7 is not assessed by this extension. Readiness is deliberately scoped
  to these selected fields, not a complete technology inventory.

The inventory declares serial number, software version, execution environment
and one chipset-vendor entry per bound radio. Serial and firmware must match the
fresh `AWLAN_Node` observation. Execution environment and chipset vendor remain
explicit evidence-bound synthetic declarations; the pinned observed schema does
not establish them. The mapper uses UTF-8 for these synthetic strings and checks
the **encoded byte length**, 1–64, without truncation. The standalone codec
preserves arbitrary 0–64-octet fields, including NULs, without guessing a charset.

The referenced evidence must declare the relevant `technology` or
`device_inventory` coverage. Files and hashes are rechecked on each request,
with the existing identity, model, firmware, schema, validity-window and topology
checks. Hashes establish which bytes were used; they do not establish that an
operator's claims are true. Changing a profile requires an intentional new digest
in its private adapter configuration. Do not remove the checks to make a sample
look ready.

## 7. Reproduce tests and interpret independent evidence

```bash
uv run pytest tests/test_technology_values.py tests/test_capability_extensions.py
uv run pytest tests/test_radio_capability_service.py
python3 scripts/check-easymesh-reference.py
```

The last command reads the already published native prplMesh capture. Twenty
selected values are cross-checked, including HT, opaque HE and Device Inventory.
No VHT value occurs in that selected capture; VHT uses source-derived asymmetric
unit examples pending independent target evidence. Inventory extraction uses the
dissector's byte positions and lengths because its text rendering can hide a
trailing NUL. Native HE fields remain uninterpreted rather than treating the
peer's example as a qualified hardware declaration.

The source review is recorded in the [protocol matrix](../protocol/protocol-matrix.json):
EasyMesh 6.1 §9.1, §17.2.8–10/Tables 31–33 and §17.2.76/Table 99; IEEE 802.11-2024
§9.4.2.54, §9.4.2.156.3/Figure 9-707, §9.4.2.247.4/Figure 9-901/Tables 9-377–378,
and selected Table E-4 classes. Exact references and limitations accompany the
[codec provenance](../../tests/fixtures/protocol/easymesh/provenance.json).

Next, resolve the HE conversion using independently derived asymmetric examples,
combine it with the selected Wi-Fi 6 companion mapping and continue the complete capability-report
and profile procedure audit. The [IEEE envelope component](../protocol/ieee1905-envelope.md) now uses the obtained
IEEE 1905.1-2013/1905.1a-2014 texts; full procedure integration remains unfinished. Actual pod access and qualification remain pending.
The acceptance path remains **real EasyMesh messages → EMOSA adapter → unchanged
physical OpenSync pod → independently observed behavior**.
