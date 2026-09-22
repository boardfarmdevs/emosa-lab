# EasyMesh value components and offline inspection

EMOSA now encodes and decodes five selected EasyMesh **TLV values**. This is
preparation for discovery and topology reporting while the IEEE 1905 documents
are being acquired. It does not enable a packet endpoint, controller discovery,
onboarding or pod writes. Run this exercise on **HOST**, in your development or
learning checkout; no LXD, OVSDB server, radio or physical pod is required.

## What is a value, and where will it fit?

A TLV has a **type**, a **length**, and a **value**. For example, SupportedService
uses type `0x80`. Its value `01 01` means “one service, Multi-AP Agent.” The first
octet is the count; the second is the service identifier. Those two octets alone
are neither a whole TLV nor an EasyMesh discovery message.

The intended nesting is Ethernet frame → IEEE 1905 message → TLVs → values.
This module handles only the last part, whose selected fields are explicitly
defined in EasyMesh. The missing IEEE base and amendment are still needed for
complete message framing and processing. An eventual protocol endpoint must also
associate the decoded values with a trusted peer, exchange and represented pod.

The Python implementation is [easymesh_payloads.py](../../src/emosa/easymesh_payloads.py).
It is a component of the EasyMesh-to-OpenSync adapter, not a separate virtual-agent
process. Its offline inspection CLI is an evaluation tool. The running adapter
now uses the Operational BSS codec in an optional [read-only topology report](../guides/observed-topology.md)
with explicit persisted bindings and a complete observed graph. It does not
advertise those values to a controller.

## Exactly which definitions are implemented?

The normative sources are the privately retained **EasyMesh 6.1** PDF and the
operator-supplied **IEEE 802.11-2024** PDF, identified by their digests in the
[protocol matrix](protocol-matrix.json). Page numbers below are printed pages.
No licensed document or page extract is redistributed.

| Type / Python object | Value fields implemented | Source |
| --- | --- | --- |
| `0x80` / `SupportedServices` | One-octet count, then that many service identifiers; `0` controller, `1` agent | EasyMesh §17.2.1, Table 24, p.125 |
| `0x81` / `SearchedServices` | One-octet count, then that many searched services; only `0` controller is defined | EasyMesh §17.2.2, Table 25, p.125 |
| `0x82` / `RadioIdentifier` | Six RUID octets | EasyMesh §17.2.3, Table 26, pp.125–126 |
| `0x83` / `APOperationalBss` | Radio count; per-radio RUID and BSS count; per-BSS AP_MAC, SSID byte length and SSID octets | EasyMesh §17.2.4, Table 27, p.126 |
| `0xB3` / `MultiAPProfile` | One profile octet; defined profiles 1, 2 and 3 | EasyMesh §17.2.47, Table 70, p.157 |
| SSID representation | Preserve original octets and the 0–32-octet structural bound; do not assume UTF-8 | IEEE 802.11-2024 §9.4.2.2, Figure 9-209, p.934 |

All counts in these selected values are single octets. Addresses and SSIDs are
opaque octet sequences. This work therefore needs no invented multi-octet IEEE
byte-order rule. It does **not** encode the outer type/length fields.

Receive and send behavior differ deliberately. EasyMesh §3.1.2 requires receivers
to ignore reserved service codes for interpretation, while senders must not use
them. The decoder retains raw codes for diagnosis and exposes `known_services`
separately. The encoder rejects reserved codes. A decoded native value containing
a reserved code therefore cannot simply be re-encoded for transmission.

For a reserved profile value, Table 70 explicitly defines an interpretation using
the receiver's implemented profile. `effective_profile(receiver_profile)` requires
that input and preserves the original received value. The command has no default
receiver profile, and this helper does not select or qualify EMOSA's profile.

Other deliberate boundaries:

- Counts must match the available bytes exactly. Truncated values, trailing
  octets, incorrect fixed lengths and overlong SSIDs fail with `INVALID_INPUT`.
- A local **16,384-byte budget** bounds work and allocation. Exceeding it produces
  `UNSUPPORTED_OPERATION`; this is not a normative IEEE message-size decision.
- SSIDs remain bytes, including embedded NULs and non-UTF-8 values. JSON displays
  hexadecimal bytes. A zero-length structural field does not establish a valid
  operational BSS in a specific procedure.
- `AP_MAC` can mean an affiliated AP address for MLD operation or a BSSID otherwise.
  These value bytes alone cannot determine which applies.
- The component preserves order and repeated identities. It checks structure;
  identity uniqueness, real inventory completeness, message inclusion and actual
  supported roles/profile require the later procedure layer.
- Unsupported types, including capability and associated-client TLVs, remain
  explicit errors. This standalone API makes no assertion about how a complete
  IEEE receiver handles unknown TLVs.

## 1. Decode a service value

Complete the [basic installation](../guides/team-manual.md#3-set-up-a-developer-checkout),
then run:

```bash
uv run emosa-lab payload --type 0x80 --value-hex 0101
```

Read `decoded.services` and `decoded.known_services`: both contain `[1]`, the
agent role. The result also identifies the value length and SHA-256. Its
`wire_envelope_validated`, `controller_onboarding_by_emosa_proven` and
`physical_pod_proven` fields are all false. Successful decoding cannot establish
any of those outcomes. This command writes no journal, opens no socket and starts
no service. It exits after printing JSON.

Compare a reserved service from the retained native capture:

```bash
uv run emosa-lab payload --type 0x80 --value-hex 0201a1
```

Now the raw services are `[1, 161]`, known services are `[1]`, and reserved services
are `[161]`. The native Search and Topology Response contain this `0xA1` code.
It is reserved under the selected 6.1 definition; EMOSA does not copy that sender
behavior into its encoder. This is a bounded source-backed observation about
those frames, not a complete conformance assessment of the native build.

## 2. Inspect a complete Operational BSS value from the native fixture

The repository retains a public synthetic capture from the native controller and
standard agent. Wireshark independently extracts its selected values; the fixture
contains the decoded interpretation for comparison. Copy only the selected value
to an ignored local file:

```bash
mkdir -p .cache/easymesh-payload-demo
uv run python - <<'PY'
import json
from pathlib import Path

fixture = Path("tests/fixtures/protocol/easymesh/native-values.json")
case = next(c for c in json.loads(fixture.read_text())["cases"] if c["type"] == "0x83")
Path(".cache/easymesh-payload-demo/operational-bss.bin").write_bytes(
    bytes.fromhex(case["value_hex"])
)
PY
uv run emosa-lab payload --type 0x83 \
  --value-file .cache/easymesh-payload-demo/operational-bss.bin
```

Expected: one radio, RUID `02:00:00:ec:02:00`, and two BSS entries with AP MACs
ending in `02:00` and `02:01`. The SSIDs are hexadecimal representations of the
synthetic names in that native agent's frame 20. This is historical captured
inventory, not a fresh OpenSync observation or EMOSA's controller-visible agent.

The CLI accepts **raw value bytes** in a regular file, or hexadecimal value bytes
as an argument. Supplying a whole TLV or Ethernet frame is outside its contract.
Files and hex input have bounded reads; special files such as FIFOs are rejected.
Output includes SSID bytes and identities, so use private input/output locations
for future physical observations. Hex encoding is not anonymization.

## 3. Understand reserved profile interpretation

```bash
uv run emosa-lab payload --type 0xb3 --value-hex ff --receiver-profile 2
```

Expected: received `profile` 255, `reserved` true and `effective_profile` 2.
Here 2 is an explicit **example receiver input**, not a declaration that EMOSA
implements Profile 2. Omit `--receiver-profile` to inspect the raw field without
applying that rule. The option is rejected for other value types.

## 4. Build value bytes in Python

```bash
uv run python - <<'PY'
from emosa.easymesh_payloads import SupportedServices, encode_value

value = encode_value(SupportedServices((1,)))
print(value.hex())  # 0101; only the value, no header and no transmission
PY
```

Radio/BSS objects use immutable tuples and `bytes`, with counts generated from
the objects. See [the tests](../../tests/test_easymesh_payloads.py) for multi-radio,
non-UTF-8 SSID, empty-count and boundary examples. A caller remains responsible
for qualified identity allocation and truthful observed inventory. In particular,
the existing selected-BSS OVSDB inventory is not automatically a complete-radio
Operational BSS report. The newer topology report performs separate complete-graph
and identity checks. No Config-to-State substitution or fabricated capability
mapping was added to produce these values.

## 5. Reproduce the checks and interpret their evidence

Run the component tests without system packages or a lab:

```bash
uv run pytest tests/test_easymesh_payloads.py
```

They compare exact independently extracted bytes and decoded fields, hand-derived
table examples, malformed/truncated input, reserved-code behavior, count and SSID
boundaries, resource limits and the offline CLI. There are 71 selected cases.

For the independent re-extraction, install `tshark` on a machine where you can
inspect the retained public capture. On Ubuntu, the package can be installed
with `sudo apt-get install tshark`; capture privileges are unnecessary because
the tool only reads a file. Then run on HOST:

```bash
python3 scripts/check-easymesh-reference.py
```

The script validates the retained input digests, invokes Wireshark's decoder,
uses its reassembly and field boundaries, and compares **nine** retained values
and interpretations. It imports no EMOSA code and never regenerates expected
values during the check. Both tshark 3.6.2 and 4.2.2 matched locally; CI includes
the same check. The [provenance](../../tests/fixtures/protocol/easymesh/provenance.json)
identifies the capture, extractor, fixture and original dissector.

These native implementation observations supplement the specification. They
cannot qualify missing IEEE rules or prove the complete native exchange conforms
to EasyMesh 6.1. The [retained component evidence](../evidence/easymesh-payloads/summary.json)
keeps this distinction explicit.

## What follows this component?

Stable per-pod radio/BSS identity binding and complete observed topology projection
now have a [synthetic service exercise](../guides/observed-topology.md). Next are
truthful radio capability values from qualified inputs. Profile advertisement must wait for the complete
mandatory-function audit. Actual discovery/topology/WSC exchange processing also
needs the missing **IEEE 1905.1-2013 and IEEE 1905.1a-2014** review, independent
full-message vectors, peer/exchange binding and recovery rules.

Then connect real controller messages through the adapter to the simulation and
independent hwsim clients. The eventual acceptance path remains **real EasyMesh
messages → EMOSA adapter → unchanged physical pod → independently observed
behavior**. Component tests and simulator results alone do not complete it.
