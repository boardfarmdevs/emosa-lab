# Profile readiness and feature capability values

An EasyMesh **profile** is a claim about a set of implemented functions. It is
not a version number we can choose merely because a controller accepts it.
EasyMesh 6.1 §18 requires the mandatory functions for any advertised profile.
Our initial discovery/onboarding/provisioning experiment covers only a subset;
its working components do not yet justify advertising Profile 1.

This guide makes that gap inspectable. It introduces an offline audit and three
new value codecs, using the available EasyMesh 6.1 specification. Run everything
here on **HOST**, from your development or learning checkout after
[manual chapter 3](../guides/team-manual.md#3-set-up-a-developer-checkout).
No service, OVSDB server, LXD, radio, controller or physical pod is required.

The [retained results](../evidence/profile-audit/summary.json) distinguish tested
code, planning assumptions, missing procedures and physical qualification.

## 1. Understand the four different claims

| Claim | What would establish it? |
| --- | --- |
| The value bytes have the selected structure | Source-derived field definitions, codec tests and independent value examples |
| The represented pod supports a feature | Qualified evidence for that pod model, firmware, radio and current context |
| The virtual agent supports a feature | The pod capability **and** EMOSA's complete configuration/reporting behavior, with tested outcomes |
| EMOSA implements a profile | Every applicable mandatory function, a resolved normative contract and independent protocol evidence |

A schema containing a field, a hardware capability bit or a passing codec test
cannot establish all four. For example, a pod may support steering, while EMOSA
still lacks the request, policy, timing, result and error procedures. Advertising
the feature would promise behavior that the adapter cannot yet deliver.

Similarly, the [radio-capability diagnostic](../guides/radio-capabilities.md)
checks explicit synthetic Basic Capabilities inputs. Its `ready: true` applies
to that diagnostic. It does not mean a complete AP Capability Report or an
advertised profile is ready.

## 2. Run the audit with unknown inputs

```bash
uv run emosa-lab profile-audit --format markdown
```

The command prints requirement families, their source sections and the remaining
work. It then lists selected AP Capability Report inclusion decisions. It exits
with **5**, meaning the proposed profile remains blocked. That is the expected
result, not an installation failure. The JSON form is:

```bash
uv run emosa-lab profile-audit
```

Read these fields in order:

1. `input_assurance` identifies this as **unqualified planning conditions**.
   The command does not inspect your pod, local adapter or controller.
2. `requirements` inventories 15 requirement families. `partial_component`
   records useful code without claiming a completed procedure.
3. `ap_capability_report.obligations` separates an inclusion decision from
   whether a value component exists and whether qualified input values exist.
4. `counter_units` remains unresolved until the required controller facts and
   unit choice are supplied explicitly.
5. `profile_advertisement_ready`, `full_clause_audit_complete` and both proof
   flags remain false. This report is neither a certifier nor a wire-enabling gate.

With no file, the audit uses one named placeholder radio and unknown conditions.
That makes missing inputs visible; it does not discover a one-radio device.

## 3. Learn how feature conditions change the obligations

The checked-in [unknown example](../../examples/protocol/profile-features.unknown.json)
matches the [planning-input schema](../../schemas/profile-features.schema.json).
Each radio has explicit `ht`, `vht`, `he`, `eht` and `advanced_qos` fields:

- `true`: inspect the consequences of declaring the feature supported.
- `false`: inspect the consequences of explicitly declaring it unsupported.
- `null`: the answer is unknown, so the relevant inclusion decision is unresolved.

`advanced_qos` means at least one of the MSCS, SCS, DSCP Policy or DSCP-to-UP
functions that trigger Radio Advanced Capabilities in an AP Capability Report
under §9.1. It is a planning condition, not a replacement for the individual bits
or their full implementation requirements.

Run the [synthetic two-radio example](../../examples/protocol/profile-features.synthetic.json):

```bash
uv run emosa-lab profile-audit \
  --features examples/protocol/profile-features.synthetic.json \
  --format markdown
```

This example invents radio features to demonstrate the decision rules. It is
not a statement about either our hwsim fixture or an OpenSync pod. Expect:

| Input condition | Inclusion consequence |
| --- | --- |
| Radio 1 has `he: true` | Both HE (`0x88`) and Wi-Fi 6 (`0xAA`) capability values are required |
| Radio 1 has `vht: false` | Its VHT value is marked `omit_if_unsupported`; the declaration still needs qualification |
| Radio 2 has `advanced_qos: true` | Its AP report needs Radio Advanced Capabilities (`0xBE`) |
| EHT is false on radio 1 and unknown on radio 2 | The agent-wide Wi-Fi 7 and EHT Operations decisions remain unresolved |
| Any radio changes to `eht: true` | Both agent-wide values become required, even if another radio is unknown |

The controller object in this example is a separate planning input. It says a
Profile-1 controller has no KiB/MiB capability indication, so the counter-unit
decision is bytes. It does not select Profile 1 for EMOSA or prove that any
controller was observed.

To experiment, make a private scratch copy instead of editing the checked-in
example:

```bash
mkdir -p .lab/profile-audit
cp -n examples/protocol/profile-features.synthetic.json .lab/profile-audit/features.json
uv run emosa-lab profile-audit --features .lab/profile-audit/features.json
```

Edit that local JSON, then rerun. Changing every feature to `true` adds obligations;
it does **not** make the profile ready. Omitting a field is an invalid contract;
use `null` for unknown. Duplicate radio IDs, strings such as `"false"`, unexpected
qualification claims and oversized/nonregular inputs are rejected. The local
input budget is 16 KiB and eight radios. No runtime state or pod connection is
created, and `--execution lxd` is intentionally rejected for this offline command.

## 4. Keep message context explicit

The selected AP Capability Report audit records these core requirements:
AP Capability, Radio Basic Capabilities per radio, Profile-2 AP Capability,
Metric Collection Interval and Device Inventory. Technology support determines
the additional HT/VHT/HE/Wi-Fi 6 and Wi-Fi 7/EHT requirements.

AKM Suite, Channel Scan, 1905 Layer Security and CAC inclusion remain marked
`applicability_review_pending`. The message list in §17.1.7, the supported-feature
rules in §9.1/§18 and the current Profile-1 conditions must be reconciled for the
actual target. The tool does not silently omit those entries.

WSC M1 has a different context. Sections 7.1 and 17.1.3 name one Radio Basic
Capabilities, one WSC/M1, one Profile-2 AP Capability and one Radio Advanced
Capabilities value for the initiating radio. Thus a false `advanced_qos` planning
condition for the **AP report** does not remove the Advanced Capabilities
requirement from **M1**. The [IEEE envelope](ieee1905-envelope.md) now exists as a
component; neither list supplies trusted controller/radio exchange binding or
actual feature evidence.

## 5. Inspect the new feature value codecs

The [Python codec](../../src/emosa/easymesh_payloads.py) now covers these additional
standalone values. Source pages are printed pages in EasyMesh 6.1:

| Type / object | Fields | Normative reference |
| --- | --- | --- |
| `0xA1` / `APCapability` | One octet: on/off-channel unassociated metrics, agent RCPI steering, M8 backhaul reconfiguration, RSN overriding; three reserved bits | §17.2.6, Table 29, p.127 |
| `0xB4` / `Profile2APCapability` | Rule capacity, reserved octet, counter-unit/feature octet, VID capacity | §17.2.48, Table 71, pp.157–158 |
| `0xBE` / `APRadioAdvancedCapabilities` | RUID and one octet: combined-role traffic separation, MSCS, SCS, QoS Map, DSCP Policy and SCS traffic-description support; one reserved bit | §17.2.52, Table 75, p.160 |

Decode a deliberately constructed value:

```bash
uv run emosa-lab payload --type 0xb4 --value-hex 01007802
```

Its four octets mean a one-rule limit, zero reserved octet, KiB counters plus the
prioritization/DPP/traffic-separation bits, and a two-VID limit. This is an input
to explain byte structure, **not a claim that EMOSA implements those features**.
The CLI includes counter-unit metadata and the false wire/onboarding/physical flags.

The decoder preserves reserved bits and fields for diagnostics while excluding
them from feature interpretation. The encoder rejects nonzero reserved bits or
the reserved counter-unit code. Decoded values therefore are not always valid
outbound values. Constructors require explicit values; they do not fill unknown
capabilities with zero. Encoding alone does not validate all procedure-level
relationships: for example §9.1 requires at least two supported VIDs when
advertising Traffic Separation.

### Counter units and the eventual ODH path

Section 9.1 distinguishes a Profile-1 controller without a KiB/MiB indication
from the other cases. The former uses bytes; the latter uses an explicit KiB or
MiB choice. The [decision helper](../../src/emosa/counter_units.py) requires known
peer facts and refuses to turn unknown Profile-1 capability into an absent bit.
A missing profile fact also remains unknown; it is not automatically a verified
legacy Profile-1 observation.

The diagnostic represents bytes/KiB/MiB using scale factors 1/1024/1048576. A
reserved unit code produces no unit or scale factor. It is never interpreted
as bytes. This prepares correct metadata handling for the eventual ODH data lake
path; no statistics conversion, delivery protocol, negotiated live context or
ODH transport is implemented here.

## 6. Reproduce independent checks and understand their limits

```bash
uv run pytest tests/test_easymesh_payloads.py tests/test_feature_capabilities.py tests/test_profile_audit.py
python3 scripts/check-easymesh-reference.py
```

The second command needs `tshark`, but only reads the already published synthetic
native-peer capture. Twenty values now include AP/Radio Advanced/Profile-2
capabilities from a native AP Capability Report in frame 22, plus the existing
discovery/topology/M1 examples and selected HT/opaque-HE/Device Inventory values. The expected values are independently extracted
without importing EMOSA. Both tshark 3.6.2 and 4.2.2 reproduce them.

Those older dissectors do not know all EasyMesh 6.1 feature meanings: for example,
some call the current DPP bit “enhanced service prioritization” or treat later
QoS bits as reserved. The independent comparison uses raw flags and numeric
fields for those values; the current specification determines their meaning.
This limitation is recorded in the [fixture provenance](../../tests/fixtures/protocol/easymesh/provenance.json).
The source capture is native prplMesh traffic, not EMOSA onboarding traffic.

## 7. What the audit says to implement or qualify next

The [procedure audit](procedure-audit.md) and [protocol matrix](protocol-matrix.json)
retain the exact selected sections and open decisions. Fifteen requirement
families are now inventoried, but this is not a completed clause-by-clause
compliance assessment. Table 133 is an informative summary. Its older Profile-1
and “Profile-1 as of Release 4” columns differ, including reliability and
higher-layer-data rows; blank cells do not silently override normative text.

Before a physical capability claim, verify whether the unchanged pod exposes
the required behavior through its existing managers: complete radio BSS
configuration, feature reporting, WPS/Multi-AP information elements where
applicable, backhaul address handling, steering and recovery. A radio feature
that EMOSA cannot configure or observe through that interface is a compatibility
gap to record, not a bit to advertise optimistically.

The [technology/inventory exercise](../guides/technology-inventory.md) now adds
synthetic HT/VHT and Device Inventory mapping, plus an opaque HE value codec.
The [HE/Wi-Fi 6 exercise](../guides/he-wifi6.md) adds the standalone companion codec and IEEE MCS parser. The [Wi-Fi 6 input exercise](../guides/wifi6-inputs.md) now maps explicit synthetic AP/STA declarations through the service. The separate `0x88` conversion and independently qualified feature inputs remain next. These components do not complete AP reporting. Both **IEEE 1905.1-2013 and IEEE 1905.1a-2014** are now obtained.
Restricted [Early/Topology report builders](reports.md) are implemented; full AP
Capability/profile integration and the native peer's recorded profile mismatch
still need resolution for the chosen build/policy. Physical access remains pending.

The proof remains **real EasyMesh messages → EMOSA adapter → unchanged physical
pod → independently observed behavior**. A complete audit helps choose and
evaluate that experiment; it does not replace the experiment.
