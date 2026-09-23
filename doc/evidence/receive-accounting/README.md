# Receive accounting and native common-window recovery

Selected receive loss is now carried through the same OVSDB observation path as
transmit accounting. The common source retains separate interface/action values
and computes both directions over the same bounded reads. Complete neighbor
metrics remain unqualified; no native report substitutes invented capacity.

## Controlled ingress loss

`ingress-loss-03` passes the independent audit. In each normal/drop/restored
phase the wired client sends 17 requests. All 51 requests and replies cross the
backhaul, but the pod's ingress action drops the 17 middle-phase replies before
they reach the client. Those packets still contribute to interface arrival
counts. Ordinary `rx_errors` and `rx_dropped` remain unchanged for this loss.

The production source replay computes **17 common intervals and 17 receive
losses**, with four baseline samples and six configuration epochs. The
[independent checker](ingress-loss-03/independent-receive-check.json) verifies the
original counters, exact selected packet sequences and absence of duplicate
parent drop counts. It reconciles **12 common packet/byte intervals** against a
separate direction-bearing capture, including unrelated ARP. Its fixed timing
allowance is 1 ms; wall/monotonic offset spread is 1,563 ns.

All captures are complete under the scoped Linux capture gate: **85 client-side
Ethernet, 102 backhaul Ethernet and 107 SLL2 records**, each matching file,
captured and filter counts, with zero kernel drops. The probe restores traffic
control and the collector exits without errors.

`ingress-loss-01-limited` and `ingress-loss-02-limited` preserve the unsuccessful
direction-selected Ethernet capture audits. The first recorded 53 packets
against 106 received-by-filter; the second recorded 53 against 54. Both report
zero kernel drops. The former uses `-Q out`; the latter an `outbound` filter.
These files cannot support complete
counter reconciliation. The successful run captures both directions using SLL2
and classifies them from recorded packet type and interface index. The existing
capture-health gate was not relaxed. All three attempts restore their rule.

## Live OVSDB and recovery

`native-receive-01` runs for **210.487 active seconds**. Its
[receive audit](native-receive-01/independent-receive-check.json) matches raw
collector values to the live manager/OVSDB handoff and recomputes **293 common
intervals**, across **three connection generations and two adapter processes**.
Five baseline samples and 15 observed configuration epochs preserve counter
lifetimes. One repeated observation is identified without counting it twice.
Both summed loss values are zero in this normal-traffic experiment.

The separate raw-forwarding packet audit reconciles **295 intervals** against
the backhaul trace; **546 of 590** directional packet windows have exact count
bounds. Its timing allowance remains 1 ms and offset spread is 1,803 ns. These
raw forwarding read bounds are distinct from the passive collector's common
accounting bounds; the evidence records their scopes separately.

The [operational recovery check](native-receive-01/independent-recovery-check.json)
passes pod-connection restoration in **7.328 seconds** and actual adapter SIGKILL
recovery in **1.224 seconds**. Fresh authenticated sessions produce **three
operations and one total Config-write attempt**. All 27 connected inventories
place the client under the represented BSS. All 824 wired probes pass, and the
eight recorded Wi-Fi traffic gaps correspond to intentional client disconnects.
Selected channel, policy-receipt, telemetry-gap and unavailable-metric checks
also pass.

Control/radio captures contain 114 and 11,473 records, respectively, with matching
capture/filter/file totals and zero reported kernel drops. The independent
backhaul capture passes too. Cleanup records no errors, restores the native
candidate baseline and passes the owned-lab idle check. All **97 recorded Python
source hashes** match the reviewed source. Native agent/controller shutdown
again reports SIGABRT/core-dump status; this remains in the evidence and is an
open lifecycle issue, not a clean-shutdown claim.

## Source, tests and reproduction

[Source provenance](source-provenance.json) pins seven reconstructed files from
Ubuntu Linux 6.8.0-139.139, using the previously verified source archives and
distribution patch. [Runtime provenance](runtime-provenance.json) records the
matching kernel package, actual veth module, tool/package versions and digests.
The source-review helper extracts and checks files without changing a kernel.

```bash
python3 scripts/review-backhaul-kernel.py .lab/backhaul-source-review-new
uv run python scripts/replay-egress-accounting.py --bidirectional \
  doc/evidence/receive-accounting/ingress-loss-03 > /tmp/emosa-receive-replay.json
cmp /tmp/emosa-receive-replay.json \
  doc/evidence/receive-accounting/ingress-loss-03/backhaul-projection.json
python3 scripts/check-receive-accounting.py \
  doc/evidence/receive-accounting/ingress-loss-03
python3 scripts/check-receive-accounting.py --native \
  doc/evidence/receive-accounting/native-receive-01
python3 scripts/check-native-recovery.py \
  doc/evidence/receive-accounting/native-receive-01 --minimum-seconds 210
```

The [unit suite](unit-results.xml) passes **1,320 tests** and the
[real OVSDB suite](ovsdb-results.xml) passes **58 tests**. Focused checks cover
common-direction accounting, wrong/shared action paths, reset/epoch boundaries,
SLL2 direction/header rejection and actual OVSDB publication. The previous
egress replay remains byte-for-byte unchanged after the shared-source refactor.

Follow the [learning guide](../../protocol/receive-counter-accounting.md) for
commands and interpretation. Selected TC loss is established; all relevant
per-neighbor loss paths, media/capacity/availability, complete native delivery,
AP/STA reporting and final-session statistics still precede full sustained
acceptance. This short regression does not replace the original 15-minute
operational result or prove an unchanged physical pod. No physical pod changed.
