# Use public BBF definitions for metric encoding

**Public BBF TR-181 2.17.0 definitions now support selected representation
conversions. The exact WFA DEr3 comparison and live measurement qualification
remain pending.** This removes the need to wait for the spreadsheet before
implementing every metric-related component. It does not turn raw hwsim counters
into qualified EasyMesh measurements.

## Which authority we are using

The operator supplied the official [USP model](https://usp-data-models.broadband-forum.org/tr-181-2-17-0-usp.html)
and [CWMP model](https://cwmp-data-models.broadband-forum.org/tr-181-2-17-0-cwmp.html).
These are two management-protocol views of BBF's Device:2.17 data model. Reading
their parameter definitions does not require adding a USP or CWMP connection to
EMOSA. OpenSync's selected southbound connection remains OVSDB, with separately
qualified telemetry inputs.

We retrieved their fully expanded publisher XML on **2026-09-23**. A small
`wifi.xml` file contains amendment changes and imports; by itself it does not
contain every inherited definition. The full XML contains the resolved parameters
and named types, including the counter-unavailable rules.

| Source | SHA-256 of full XML |
| --- | --- |
| [USP 2.17.0 full XML](https://usp-data-models.broadband-forum.org/tr-181-2-17-0-usp-full.xml) | `4907bdbd5802a40b9d0e493d9c5c1884a25ab3b0437f725f71a39604eaefb5f7` |
| [CWMP 2.17.0 full XML](https://cwmp-data-models.broadband-forum.org/tr-181-2-17-0-cwmp-full.xml) | `5c0f218337ec4831b3583c1ec7ed7b974dd5e57ef3281d01052f84337538a303` |

The selected **33 parameter definitions**, the two statistics types and the
associated-device statistics object's description match after XML formatting is
normalized. This is a selected-field comparison, not a claim that the complete
USP and CWMP models are identical. The [retained review](../evidence/bbf-data-elements/source-review.json)
contains each exact path, type, units, range, definition digest and publisher link.
Full specification files stay outside Git.

There is an edition distinction: BBF's [2.17 release notes](https://github.com/BroadbandForum/usp-data-models/blob/master/CHANGELOG.md#2024-01-18-tr-106-amendment-13-and-tr-181-issue-2-amendment-17)
identify Data Elements **R2.1** additions. A versioned TR-181 URL does not prove
that its contents equal the WFA file `TR-181-2-17_DEr3.xlsx`. The currently served
full XML also contains shared bibliography entries dated 2024. We pin the actual
retrieved bytes and explicitly use them as BBF definitions; we do not label them
the acquired WFA 3.0 package. That package remains in the
[acquisition checklist](specification-acquisition.md) for a direct comparison.

## Translate the quantities, not just the field names

The paths below are relative to
`Device.WiFi.DataElements.Network.Device.{i}.`. Here `{i}` means an object instance,
such as one radio or one associated station. It is not a literal pod identifier.
EasyMesh 6.1 Tables 58 and 82–85 supply the target wire layouts. The BBF definitions
supply the explicit meanings and types listed here.

| BBF parameter | Representation and consequence for EMOSA |
| --- | --- |
| `CollectionInterval` | Unsigned 32-bit milliseconds between successive collections of the most frequently measured element. Encode `1000` as 1000 ms in `0xC5`; do not copy the controller's 60-second reporting period. The BBF schema has no explicit positive minimum. Encoding zero alone does not qualify a zero-interval publisher. |
| `Radio.{i}.Noise` | Unsigned ANPI representation, referencing IEEE 802.11-2020 §11.10.9.4. A signed dBm survey reading cannot be copied into it. The bridge accepts an already qualified encoded value; no new dBm conversion is claimed. |
| `Radio.{i}.Utilization`, `.Transmit`, `.ReceiveSelf`, `.ReceiveOther` | Primary-channel time fractions scaled so 255 represents 100%. TX includes successful and failed transmissions. RX-self covers stations in this radio's BSSs; RX-other covers valid non-local PPDUs. A value of 100 is not 100 percent. The bridge consumes encoded observations; it supplies no airtime estimator. |
| `Radio.{i}.BSS.{i}.UnicastBytesSent/Received`, `.MulticastBytesSent/Received`, `.BroadcastBytesSent/Received` | Six distinct BSS totals with type `StatsCounter64`. Table 84 uses six 32-bit fields in the advertised byte units. Scale first; reject an unavailable value or an encoded result too wide for this reviewed mapping. Do not substitute a station's byte counter for a BSS traffic-class total. |
| `Radio.{i}.BSS.{i}.STA.{i}.LastDataDownlinkRate/UplinkRate` | Unsigned 32-bit **kbps**, AP-to-STA and STA-to-AP respectively. These are last-used data rates, not estimates of achievable MAC throughput. |
| `Radio.{i}.BSS.{i}.STA.{i}.UtilizationReceive/Transmit` | Unsigned 64-bit accumulated **milliseconds** receiving from/transmitting to this station. These are not percentages. Table 85 allocates 32 bits; this implementation withholds values exceeding that width instead of guessing a wrap rule. |
| `Radio.{i}.BSS.{i}.STA.{i}.EstMACDataRateDownlink/Uplink` | Unsigned 32-bit **Mbps** estimates under the stated full-airtime/bandwidth assumption. Base link reports need a qualified estimate separately from the last-used rates. |

`StatsCounter64` reserves `2**64 - 1` for unavailable data. It must be recognized
**before** scaling or narrowing. Otherwise an unknown input becomes a believable
large counter. `StatsCounter32` has its own `2**32 - 1` unavailable value. An
ordinary `unsignedInt`/`unsignedLong` is a different type; do not apply statistics
sentinels to every integer. For example, a BBF 64-bit BSS counter of `2**32 - 1`
is available and fits the 32-bit wire field.

The new bridge does not apply Table 58's explicit rollover rule to Tables 84 or
85, which do not repeat it. Wider values are withheld pending a reviewed mapping.
That is a bounded implementation limitation, not a claim that the standards
require agents to stop reporting at those boundaries.

## What the public definitions resolve for station counters

BBF names all seven STA byte, packet, error and retransmission counters and
defines them as `StatsCounter64` under DataElements. Its linked
`Device.WiFi.AccessPoint.{i}.AssociatedDevice.{i}.Stats.` object specifies 802.11
frame counts, includes framing characters in byte counts, and describes resets
on parent AP status transitions. Those source objects do **not** have identical
types: the linked error and retry counters are `StatsCounter32`. A source mapper
must bind the actual namespace and its sentinel/reset rules.

Several questions still prevent raw kernel passthrough:

- EasyMesh Table 58 explicitly requires successfully sent packets. BBF's
  `PacketsSent` wording alone does not establish that a kernel submission counter
  excludes failed transmissions.
- `RetransCount` counts repeat transmissions; retransmitting one packet twice
  contributes two. The [completion audit](tx-status-accounting.md) already shows
  that the selected kernel can suppress some retries in its aggregate counter.
- Including framing characters does not, by itself, settle every encryption,
  aggregation, FCS and management-frame boundary. Existing
  [station accounting evidence](station-counter-accounting.md) exposes real
  differences between the selected TX and RX sources.
- A reset rule on AP enable/disable does not establish a final counter epoch for
  each association. The publisher still needs the same-session final sample and
  the [live reason join](live-session-reasons.md).
- BBF `STA.SignalStrength` describes RCPI and reserved values, while its XML units
  say dBm. The bridge does not resolve that inconsistency by converting the field;
  it keeps the already implemented, separately qualified RCPI input.

For these reasons this step adds no generic BBF-to-`TrafficCounters` passthrough.
The source audit now gives the follow-on counter work concrete public definitions
to use, rather than treating every definition as inaccessible.

## Reproduce the review on HOST

Use the development checkout and its normal `uv` environment. This exercise
needs no VM, container, radio, controller or pod credentials.

1. Run the source review. It downloads about 8.6 MB of public XML on the first run
   into `~/.local/share/emosa/specifications/bbf-tr181-2.17.0/`, outside Git.

   ```bash
   python3 scripts/review-bbf-data-elements.py .lab/bbf-learning-01.json
   ```

   Choose a new result filename. `--cache /absolute/path` selects another source
   directory; `--offline` requires the pinned files to exist already. A changed
   publisher file causes a hash failure, not an automatic upgrade. Compare new
   definitions deliberately before changing a pin.

2. Read the result. Expect `selected_parameters: 33` and
   `usp_cwmp_selected_definitions_equal: true`. Follow the parameter links for
   `CollectionInterval`, `UtilizationReceive` and `RetransCount`. The false WFA
   equivalence and measurement-qualification flags are intentional.

3. Exercise the internal conversion bridge and existing report guards:

   ```bash
   uv run pytest -q tests/test_bbf_metrics.py tests/test_ap_metrics.py tests/test_disassociation.py
   ```

   The new tests check exact TLV bytes, kbps versus Mbps, millisecond durations,
   all six unavailable BSS counters, scaling before width checks, and rejection
   of overflow, absent values and changed source hashes.

4. Inspect a simple conversion interactively:

   ```bash
   uv run python - <<'PY'
   from emosa_lab.wire.bbf_metrics import collection_interval_tlv
   print(collection_interval_tlv(1000).encode().hex())
   PY
   ```

   Expect `c50004000003e8`: type `c5`, four value octets, then unsigned big-endian
   1000. This builds a value in memory. It does not advertise that any real
   measurement is collected every second or transmit a capability report.

5. Read [AP report assembly](ap-metric-reports.md) again. The functions in
   [bbf_metrics.py](../../lab/src/emosa_lab/wire/bbf_metrics.py) return the existing typed
   AP/radio/link inputs. A live publisher must still provide identities, epoch,
   complete membership, age, source authority and every mandatory companion.
   Existing freshness and reporting guards remain in effect.

## Next connected work

Qualify actual AP/STA measurement sources against these public definitions,
settle the remaining width/representation ambiguities, and connect them to the
guarded publisher. The selected hwsim survey has dummy noise and busy values;
the selected wmediumd timing does not qualify the HT profile's airtime. Neither
becomes valid merely because the destination units are now documented. Mandatory
BE ESP estimation and its IEEE-to-EasyMesh byte conversion also remain separate.

After qualifying the complete source, verify AP/STA and final-session values in
the native controller, then repeat the **15-minute run with recovery checks** and
all required reports. The existing operational recovery evidence still passes;
complete sustained acceptance remains pending. Physical proof still follows
real EasyMesh messages → EMOSA → unchanged OpenSync pod → independent observation.

**Learning checkpoint:** explain why a field definition, a unit conversion and a
qualified measurement are three different achievements. Identify the BBF source
of an encoded duration, its wire-width limit, and the evidence still needed to
call it a real pod measurement.
