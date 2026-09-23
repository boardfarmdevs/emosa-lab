# Final client-session statistics

**Status: sender and guarded lifecycle handoff implemented; final measurement
publisher still unqualified.** The retained 15-minute run observed client leaves
but could not report their final counters and reason. This implementation adds
the missing message sender without treating a polling sample as final evidence.

## Why membership is insufficient

EasyMesh 6.1 §6.3 requires a Client Disassociation Stats message when the agent
sends a client-leave notification. The message contains a station identity,
actual IEEE 802.11 reason code and final traffic counters for that association.
OVSDB membership disappearance establishes none of the final counters or reason.
A sample taken before a station leaves can omit its last packets, and the same
MAC may immediately start a different association.

The current hwsim observer still supplies membership and association duration.
It does **not** call the new final-session handoff. Required measurement work
remains listed in [sustained operation](sustained-operation.md#measurement-work-still-needed-before-complete-acceptance).
This component does not close that acceptance gap or qualify an unchanged pod.

The next [native station-removal observation](station-removal-observations.md)
now obtains removal-time kernel counters and correlates actual reasons from an
independent radio capture. Acquisition is verified; counter semantics and an
online reason join remain unqualified. Those raw records are not yet passed to
the sender.

## Implemented handoff and wire behavior

`TrafficCounters` requires seven explicit AP-relative counters: bytes sent and
received, successfully sent packets, received packets, transmit and receive
errors, and packets sent with the retry flag. There are no numeric defaults.
`from_qualified_opensync()` checks Proto2 field presence before reading values;
an absent optional field cannot silently become zero. This helper is only for a
publisher whose units, packet meanings and reset/session behavior have already
been qualified. A field named `tx_frames` does not establish those semantics.

`FinalSession` also requires a unique association identifier, represented BSSID,
station MAC, the current report-source context, a qualified observation time
and actual reason. Reserved reasons are rejected using IEEE 802.11-2024
§9.4.1.7/Table 9-79, printed pp.837–840. The application must join the actual
disconnect event with the final counter sample for the same association. The
class validates the supplied record; constructing it does not qualify a source.

The internal `OnboardingSession.report_final_session()` method is available only
after authenticated provisioning and a recently observed leave. The sender
requires complete current membership with that station absent, a represented
BSSID, the same source context and a final sample younger than two seconds.
Reassociation, source loss, a new database context or changed counter units
withdraws the old report. The clock bound is the owned lab's selected contract;
a physical publisher needs a qualified clock mapping and uncertainty budget.

The sender constructs CMDU `0x8022` with exactly the mandatory `0x95` station,
`0xCA` reason and `0xA2` traffic-statistics TLVs for this non-MLD component.
Delivery tracks the controller's matching `0x8000` Ack, with at most three
transmissions, fresh message IDs and a fixed one-second local retry budget.
Error/security-envelope acknowledgements cannot count as successful delivery.
Retries do not refresh the sample or its deadline. The component retains at
most 16 pending reports, 64 recent final records and 32 diagnostic events.
No Config operation or pod write is created by this reporting path.

## Correct counter units before enabling metrics

EasyMesh 6.1 §17.2.35/Table 58 specifies byte units for Profile-1 traffic
counters. Later profiles use the advertised Profile-2 counter unit. The owned
Profile-1 experiment previously advertised KiB in its accompanying Profile-2
capability TLV but transmitted no traffic-statistics TLVs. New runs advertise
**bytes**, consistently across Early, M1 and AP Capability reports. This avoids
contradictory interpretations before measurement reporting is enabled.

The independent onboarding checker keeps earlier retained runs readable as
historical bounded onboarding evidence. It requires the new run's explicit
`agent_counter_units` declaration to agree with every captured capability TLV,
and requires byte units if Profile-1 traffic-statistics TLVs are present.
Earlier captures are not rewritten or promoted to metrics qualification.

The raw counter codec also tests KiB and MiB conversion for future profiles.
It scales the raw lifetime byte counter **before** taking its low 32 bits.
Packets/errors/retries remain unscaled. Counters roll over rather than saturate;
Table 58 assigns rollover handling to the receiver. These codec tests do not
enable Profile-2 or Profile-3 admission in the current lifecycle.

## Learn and reproduce this component

Use the development checkout on HOST after the manual's Python/tshark setup:

1. Read the [retained evidence](../evidence/final-statistics/README.md). Distinguish
   the native byte-declaration regression from the synthetic encoding vectors.
2. Recheck the retained vectors with the independent dissector:

   ```bash
   python3 scripts/check-disassociation-reference.py
   ```

   This command imports no EMOSA code. It checks the message type, identities,
   required TLV order, reason and seven counters against literal expected values.
3. Generate a fresh set with the adapter encoder, using a new output directory:

   ```bash
   uv run python -m emosa.simulation.disassociation .lab/final-stats-learning-01
   python3 scripts/check-disassociation-reference.py --directory .lab/final-stats-learning-01
   ```

   `inputs.json` identifies these values and timestamps as synthetic. Nothing is
   sent to the lab or a physical pod. The examples demonstrate bytes, KiB and MiB
   conversion at rollover boundaries; they are not radio measurements.
4. Run the state/negative tests:

   ```bash
   uv run pytest -q tests/test_disassociation.py tests/test_native_onboarding.py
   ```

   These exercise the authenticated lifecycle handoff, actual observed-leave
   requirement, absent fields, reserved reasons, stale/rejoined clients,
   duplicate/conflicting records, lost/error/late Acks, queue bounds and send
   failure. A passing encoding test cannot prove counter finality.
5. Continue with publisher qualification: obtain final counters and reason from
   a complete, session-bound source, validate their semantics and timing, then
   attach that source to the internal handoff. Only then run the full native
   15-minute acceptance again and require final reports for every observed leave.

## References and measurement caution

Normative references: EasyMesh 6.1 §6.3, §15.1, §17.1.41, §17.2.23/Table 46,
§17.2.35/Table 58 and §17.2.64/Table 87; IEEE 802.11-2024 §9.4.1.7/Table 9-79.
The authorized PDFs remain outside Git. OpenSync's pinned statistics schema is
an input representation, not a replacement for these specifications.

The kernel's hwsim survey implementation can provide artificial noise/busy
values from scan bookkeeping. Enabling a scan therefore does not establish an
independent channel-utilization measurement. The callback documents this
explicitly in the [kernel source](https://github.com/torvalds/linux/blob/v6.8/drivers/net/wireless/virtual/mac80211_hwsim.c#L2534-L2564).
That observation concerns a different required metric; it must not be used to
claim that AP/STA metrics are now complete.
