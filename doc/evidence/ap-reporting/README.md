# AP report assembly, durable dispatch and native recovery

The [guide](../../protocol/ap-metric-reports.md),
[manual §13.29](../../guides/team-manual.md#1329-build-complete-ap-reports-and-preserve-reporting-deadlines)
and learning-path step 19 explain this evidence. **Selected message composition
and scheduling are verified; live AP measurements and native AP report delivery
remain pending.** This milestone does not complete the integrated 15-minute
acceptance or qualify an unchanged physical pod.

## Synthetic composition and schedule

`vectors/` contains eight deterministic Ethernet frames and the
[independent byte/tshark audit](vectors/independent-check.json). Three AP Metrics
Responses exercise all seven selected TLV types, sparse BE/VI ESP entries,
one-BSSID station metrics, explicit byte-counter rollover, encoded radio and
extended values, and both populated and explicitly empty known TID inventories.
The missing-link-companion query receives no incomplete response.

The fake-clock periodic dispatcher sends one complete report at time 70 and
records one unavailable report at time 130. Reopening its durable store emits
no extra frame. The pcap SHA-256 is
`fad04b6f85ceb3862cb95d570ee6ffb473dfa70a4e466165908a1aeda429ac2c`.
CI regenerates and compares those bytes. The checker imports no adapter code;
tshark independently decodes the scalar fields. ESP octets and Data Elements
values are synthetic already encoded inputs, not qualified sensor conversions.
The fixture does not run a controller or kill a process.

## Native repeat: `native-ap-report-02`

The real candidate, simulated OpenSync-schema pod, separate hwsim manager and
independent clients run for **210.208 active seconds**. The
[AP withholding audit](native-ap-report-02/independent-ap-withholding-check.json),
[operational recovery audit](native-ap-report-02/independent-recovery-check.json),
[peer report audit](native-ap-report-02/independent-peer-check.json) and
[telemetry-gap audit](native-ap-report-02/independent-telemetry-check.json) pass.

The native policy still contains its 60-second interval and all three client
inclusion flags. The lab has no qualified AP measurement publisher, so no AP
Metrics Response is emitted. Three reporting periods retain `NOT_READY` outcomes,
zero successful AP transmissions and the same schedule origin through both
faults. This proves honest withholding and schedule recovery, not reporting
fulfillment.

| Observation | Result |
| --- | --- |
| Actual OVSDB connection interruption | Fresh authenticated recovery in 7.339 seconds |
| Actual adapter SIGKILL/restart | Fresh authenticated recovery in 1.225 seconds |
| Configuration effects | Three operations, one total Config-write attempt; no new writes on either recovery |
| Independent wired traffic | 823/823 replies |
| Independent Wi-Fi traffic | 691/801 replies, eight intentional client-disconnection gaps; continuous during management recovery |
| Native channel procedures | Three preference queries, four accepted selections and four acknowledged measured operating reports |
| Process samples | RSS 56,052–59,480 KiB; 23 descriptors |
| Owned peer source | 256 published observations, 279 common counter windows, three control generations and two worker processes |

The peer audit matches three actual replies to fresh measured inputs and later
native controller statistics:

| Query → reply frames | MID | Latency | Controller TX/RX packets | TX/RX errors |
| --- | ---: | ---: | --- | --- |
| 33 → 34 | 32 | 337.203 ms | 2 / 2 | 0 / 0 |
| 69 → 70 | 49 | 8.257 ms | 7 / 4 | 0 / 0 |
| 103 → 104 | 65 | 1.026 ms | 15 / 15 | 0 / 0 |

All live peer queries are answered. Capture checks require matching record totals
and zero reported kernel drops. Source provenance matches **105 runtime inputs**,
including the candidate reference actually used by the trial. Exact original
offloads, isolation and queue setup are restored, cleanup errors are empty and
the owned idle check passes. Native controller/helper shutdown still reports
SIGABRT/core-dump; restoration success does not resolve that lifecycle defect.

## First attempt and the timing fix

`native-ap-report-01` passes the operational recovery, policy and AP withholding
checks but **fails peer acceptance**. Request frame 29/MID 31 was unanswered
outside the previously classified fault and raw-baseline windows. The publisher
made the complete interval available about 140 ms after the query, still inside
the original response deadline. The old coordinator discarded the request
immediately when no qualified sample was available.

The [failure record](native-ap-report-01/peer-audit-failure.json), complete selected
evidence and exact pre-fix runtime sources remain retained. CI requires this
attempt to keep failing the peer audit for the original reason. No timing bound
was enlarged and the independent audit's query classification was not relaxed.

The coordinator now retains at most four pending queries under their original
one-second deadline and control/topology binding. It sends only after the current
measurement source passes its existing guards. Duplicate queries cannot extend
the deadline; expiry, authority loss, changed topology and partial send failure
withdraw pending work. Nine new deterministic tests cover these boundaries.
The repeat's 337 ms response demonstrates the wait on the actual native path.
A final defensive review also found that an uncertain send triggered by a
repeated pending query must remove the pending entry before I/O. That additional
failure branch is unit-tested after the native repeat; the repeat's exact
`link_metrics.py` is retained under `executed-source/`. Its runtime hashes refer
to that executed version, not to an unexecuted final checkout version.

## Verification and remaining work

Retained suites contain **1,408 passing unit tests and 60 passing real-OVSDB
tests**. Component tests cover policy-required companions, source invalidation,
missing versus empty values, fragmentation guards, durable reservation, an
uncertain post-send crash outcome and restart without replay. CI checks the
synthetic bytes, native evidence and the preserved failed attempt.

Next qualify AP/STA measurement definitions and conversions, connect their
publisher and verify native controller receipt. Acquire the referenced Wi-Fi
Data Elements package, review ESP conversion and unsolicited-report reliability,
complete final-session reporting, resolve native shutdown and repeat the full
15-minute integrated acceptance with every required source enabled. The original
[908-second operational soak](../native-soak/README.md) remains a separate scoped
result. Physical acceptance still requires real EasyMesh messages through EMOSA
to an unchanged OpenSync pod and independently observed behavior.
