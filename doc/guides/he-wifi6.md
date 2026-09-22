# Understand HE and Wi-Fi 6 capability values

This exercise runs on **HOST**, in your installed EMOSA Lab checkout. It needs
Python/uv from manual chapter 3, but no VM, radio, OVSDB server or pod. It adds
two useful building blocks: an IEEE HE MCS field codec and an EasyMesh AP Wi-Fi 6
capability value codec. Neither starts an agent or sends a packet.

The goal remains a controller onboarding an unchanged OpenSync extender through
EMOSA. The controller will need truthful capabilities for that represented agent.
These codecs prepare part of that report; actual pod facts, complete message
processing and independently observed behavior are separate requirements.

## 1. Learn what these fields describe

**HE** means High Efficiency, the 802.11 technology associated with Wi-Fi 6.
**MCS** means modulation and coding scheme. **NSS** is the number of spatial
streams. A capability map describes which MCS range a device supports for each
stream count. It does not measure throughput or select the current connection rate.

Each direction has eight two-bit codes, for NSS 1 through 8. A code of 0 means
MCS 0–7, 1 means 0–9, 2 means 0–11, and 3 means that stream count is unsupported.
Receive and transmit capabilities can differ. The IEEE field puts the receive
map first, followed by transmit, with each 16-bit map in little-endian octet order.
The pair for widths up to 80 MHz is always present. A 160 MHz pair and an
80+80 MHz pair follow when their corresponding support bits are set.

| Supported widths in this field | Pairs, in order | Length |
| --- | --- | --- |
| Up to 80 MHz | Receive/transmit up to 80 | 4 octets |
| Plus 160 MHz | Up to 80, then 160 | 8 octets |
| Plus 80+80 MHz | Up to 80, then 80+80 | 8 octets |
| Both wider modes | Up to 80, then 160, then 80+80 | 12 octets |

Eight octets alone cannot identify the wider mode. This is why the decoder
requires explicit width flags instead of guessing from the byte count.
Operating-mode restrictions can further limit usable rates; this codec does not
implement rate selection, regulatory checks or a complete HE information element.

Normative references: IEEE 802.11-2024 §9.2.2 p.656 and §9.4.2.247.4,
Figure 9-901 / Tables 9-377–378 pp.1455–1457; EasyMesh 6.1 §17.2.72,
Table 95 pp.169–170. The [matrix](../protocol/protocol-matrix.json) records the
private source artifacts and scope. Licensed documents are not redistributed.

## 2. Inspect a synthetic AP Wi-Fi 6 value

EasyMesh type `0xAA` contains a radio identifier, a role count and capabilities
for each role. AP and non-AP STA roles are distinct; a pod may have a backhaul STA
role as well as AP operation. An eventual mapping must establish the actual roles.
The example below contains one invented AP role:

```bash
uv run emosa-lab payload --type 0xaa \
  --value-hex 0200000140010104e41bc6e4a53912345a
```

Expected exit code: **0**. Read the JSON as follows:

- `decoded.ruid` is the synthetic radio `02:00:00:01:40:01`.
- `decoded.roles` contains one entry, with `known_role: "ap"`.
- Its `mcs_length` is 4; `he160` and `he8080` are false.
- `mcs.up_to_80_mhz` gives separate receive/transmit lists, ordered NSS 1–8.
  They deliberately differ to expose byte-order mistakes. They are layout test
  data, not a qualified radio capability declaration.
- The AP user limits are 3 downstream MU-MIMO users, 9 upstream MU-MIMO users,
  18 downstream OFDMA users and 52 upstream OFDMA users. Other bits describe
  beamforming and features. These invented values demonstrate parsing only.
- All wire-envelope, controller-onboarding and physical-proof flags remain false.

The `04` immediately after the role count holds the role/width flags and the
four-octet MCS length. The encoder derives that length and the width bits from
the supplied pairs, so callers cannot accidentally encode contradictory lengths.
The decoder rejects contradictory lengths, truncation, trailing bytes and zero
MCS lengths. Reserved role codes 2/3 remain visible with `known_role: null` for
diagnostics; the encoder refuses to send them. A structurally empty role list
does not establish that a real radio has no roles.

## 3. Work with the IEEE field in Python

```bash
uv run python - <<'PY'
from emosa.he_mcs import decode_he_mcs, describe_he_mcs, encode_he_mcs

raw = bytes.fromhex("e41bc6e4fafffdff")
field = decode_he_mcs(raw, he160=True, he8080=False)
print(describe_he_mcs(field))
assert encode_he_mcs(field) == raw
PY
```

This prints the up-to-80 and 160 MHz maps; the 80+80 entry is `None`.
It verifies a local field conversion, without opening a socket. If you change
both width inputs to false while keeping eight octets, decoding fails: four
octets would be required. That error protects the boundary between adjacent
fields in the containing value.

**Do not copy these bytes into the separate AP HE capability value (`0x88`).**
EasyMesh Table 33 describes a big-endian reordering for that field. Table 95
instead references Figure 9-901 directly; the new `0xAA` codec uses that IEEE
field representation. The exact `0x88` conversion remains under review. Its
existing CLI still reports opaque bytes and `opaque_mapping_pending`.

## 4. Reproduce the native peer incompatibility

The retained synthetic native controller/agent capture contains an AP Wi-Fi 6
value in frame 22. Both role flag octets have a zero length nibble, despite
following MCS bytes. Inspect the actual captured value:

```bash
uv run emosa-lab payload --type 0xaa \
  --value-hex 020000ec020002000000004003221212d0400000004003221212d0
```

Expected exit code: **2**, invalid input. This is a useful negative result:
EMOSA does not silently insert lengths into a received capability value.
The native peers interoperating with one another does not prove those bytes
meet the selected EasyMesh 6.1 definition.

With tshark installed, independently re-extract the evidence:

```bash
python3 scripts/check-easymesh-reference.py
uv run pytest tests/test_he_wifi6.py tests/test_easymesh_payloads.py
```

The independent check reports **20 matching selected values and 1 rejected
case**. It imports no EMOSA module. The last count means a captured value used
as an EMOSA negative test, not a Wireshark conformance verdict. Older Wireshark
labels the length nibble reserved; only its raw bytes/field boundaries are used.
Both tshark 3.6.2 and 4.2.2 reproduce the extraction.

The [retained source review](../evidence/he-wifi6/native-source-review.json)
identifies two native work items. The pinned controller hardcodes Profile 1 in
its response to a Search containing a profile, explaining the retained
Profile-2 Search/Profile-1 Response. The nl80211 implementation adds its base
MCS length in the VHT branch, allowing zero for the HE-without-VHT case.
Its Wi-Fi 6 builder also reorders MCS pairs; that needs a compatibility test
against the selected Table 95 interpretation. No native binary was rebuilt or
live lab service changed during this review. Source findings alone are not a
qualified replacement build.

## 5. Understand what remains and prepare the next experiment

Run `uv run emosa-lab profile-audit --format markdown`. Exit **5** still means
the full advertised profile is blocked. The Wi-Fi 6 row now has a value codec,
but no qualified mapped value. The read-only radio-capability service still
blocks technology readiness when HE support is true or unknown: its complete
HE/Wi-Fi 6 input mapping has not been implemented. Hardware support alone also
cannot justify features such as spatial reuse or anticipated channel usage
when the adapter cannot perform the associated configuration/reporting.

The next useful work is to resolve the `0x88` conversion with independent,
asymmetric examples, then add evidence-backed per-role HE/Wi-Fi 6 inputs and
atomic report projection. In parallel, qualify a native peer/profile and BSS
policy intersection: changing a profile number alone does not implement its
mandatory features, and the retained two-M2-plus-M8 request exceeds the current
narrow projection. The [acquisition checklist](../protocol/specification-acquisition.md)
also now identifies Data Elements 3.0 for the required Metric Collection Interval
definition; the ODH transport remains a separate design input.

After the exact IEEE 1905 base/amendment review, bind the components into complete
exchanges and run a controller-to-simulated-pod onboarding trial. Then qualify
the actual unchanged pod and independently observe its client behavior. The
[retained checks](../evidence/he-wifi6/summary.json) establish this increment's
component scope; they do not complete that acceptance path.
