# Neighbor-metric component and native unavailable-source evidence

The IEEE 1905 query/response implementation now has independent encoding
evidence and a guarded measurement handoff. **Native measured response delivery
remains pending.** This directory does not turn missing counters into acceptance.

The ten [synthetic frames](vectors/synthetic-neighbor-metrics.pcap) cover TX-only,
RX-only, both directions, a specified neighbor and an invalid neighbor. Positive
responses include two interface pairs. Wireshark independently checks the exact
values, conditional query lengths, field ordering, MID correlation and amended
PHY-rate/RSSI sentinels. See [the vector result](independent-vector-check.json).

The real `native-neighbor-01` regression completed **213.52 active seconds** and
29 connected-phase samples. It observed four all-neighbor TX/RX queries, at
Ethernet frames **33, 68, 100 and 113**. Each maps to an explicit
`neighbor_measurement_unavailable` decision. There are no fabricated metric
responses or invalid-neighbor replies. The controller is a valid neighbor;
an unqualified measurement source does not make it an invalid one.

The independent freshness, recovery and policy-receipt checks also pass:

- A **6.36-second** telemetry-only gap preserves the control session and
  withdraws stale client/radio observations.
- Pod reconnect completes in **7.33 seconds**, including the deliberate
  four-second outage; adapter SIGKILL/restart completes in **1.21 seconds**.
- Three authenticated operations have **one total Config-write attempt**.
- Capture/filter/record counts agree with zero reported drops: **113 Ethernet**
  and **11,628 radio** packets.
- The original controller is restored and the separately checked lab is idle.
  The known native shutdown SIGABRT remains recorded; it is not hidden as clean
  native shutdown.

`neighbor-link-observations.json` provides new read-only evidence for measurement
qualification. The pod's `eth1`, controller's `eth1` and adapter's `probe0` have
separate identities and veth peers on the actual `em-base-bh` bridge. Their raw
interface counters are single snapshots, not qualified per-link measurements.
The old virtual-interface fixture still needs a justified mapping to the pod's
forwarding interface before an online publisher can supply native metrics.

```bash
python3 scripts/check-neighbor-metrics.py --directory doc/evidence/neighbor-metrics/vectors
python3 scripts/check-neighbor-metrics.py --native-directory \
  doc/evidence/neighbor-metrics/native-neighbor-01
python3 scripts/check-native-recovery.py \
  doc/evidence/neighbor-metrics/native-neighbor-01 --minimum-seconds 210
python3 scripts/check-telemetry-gap.py doc/evidence/neighbor-metrics/native-neighbor-01
python3 scripts/check-native-policy.py doc/evidence/neighbor-metrics/native-neighbor-01
```

The [unit suite](unit-results.xml) passes **1,197 tests**, and the
[real OVSDB suite](ovsdb-results.xml) passes **54 tests**. Tests exercise direction
selection, complete interface pairs, missing measurements versus absent neighbors,
stale samples, generation/topology changes, invalidation/replay, partial send and
the authenticated onboarding handoff without extra Config effects. The source
hash record distinguishes the native execution from a subsequent validation-order
hardening in the positive metric encoder; final-source component/dissector tests
cover that change. It does not alter the native unavailable-source branch.

The [operator guide](../../protocol/neighbor-link-metrics.md) explains source
references, reproduction and the remaining measurement work. AP/radio/STA
reporting and final-session statistics also remain incomplete. The original
15-minute operational run remains separate; complete sustained acceptance and
physical-pod qualification remain false.
