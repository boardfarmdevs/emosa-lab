# Observed egress accounting through OVSDB

The selected software-drop path now has a measured source. A passive pod
collector reads interface and traffic-control statistics, tracks kernel
configuration changes, and supplies bounded observations through the simulated
OpenSync manager and OVSDB. EMOSA computes separate successful-transmit,
driver-drop and action-drop deltas. This is not yet a complete IEEE 1905
neighbor-metric publisher.

## Controlled loss: `egress-loss-01`

The owned idle-lab experiment sends 17 requests in each normal, drop and restored
phase. Independent captures show all 17 selected requests entering the pod and
none crossing its backhaul during the drop phase. The action counts 17 drops;
ordinary interface error/drop counters remain unchanged. Both normal phases
deliver all requests and replies.

The production accounting source, replayed against the retained observer log,
computes **14 intervals and 17 total egress losses**, with four baseline samples
and seven configuration epochs. The independent checker verifies the original
counter differences and packet sequences, and confirms that parent qdisc drops
are not added again. Captures contain **85 ingress and 68 backhaul frames**, with
matching file/filter/capture counts and zero reported kernel drops. Traffic
control is restored; the collector exits without errors.

This replay is a component check, not evidence of a live native OVSDB session.
The separate native run below establishes that handoff. See the
[loss audit](egress-loss-01/independent-egress-check.json) and
[restoration record](egress-loss-01/restoration-check.json).

## Live handoff and recovery: `native-egress-01`

The **210.484-second** native run passes the independent onboarding, client,
channel, policy-receipt and recovery checks. Its
[egress audit](native-egress-01/independent-egress-check.json) matches the raw
collector log to manager publications, OVSDB observations and **292 accounting
intervals**, across **three connection generations and two adapter processes**.
Five baseline samples and 13 observed configuration epochs preserve lifetime
boundaries. No egress losses were observed in this normal-traffic experiment.

The telemetry-only pause keeps egress observations and control authority live.
Actual pod-connection loss withdraws accounting. The resumed connection and
adapter SIGKILL each require a fresh baseline and authenticated onboarding.
[Recovery](native-egress-01/independent-recovery-check.json) takes **7.329 seconds**
and **1.318 seconds**, respectively, with **three operations and one total Config
write attempt**. All 27 connected controller inventories place the client under
the represented BSS. Wired traffic passes all 824 probes; eight intentional
Wi-Fi disconnects explain its recorded traffic gaps.

All three captures pass their applicable completeness checks: 114 control
Ethernet frames, 11,428 radio frames, and the independently checked backhaul
capture. The passive collectors finish without errors and the harness records
no cleanup errors. The native candidate baseline is restored and the owned-lab
idle check passes. All **96 recorded Python source files** match the
reviewed source; the remaining reference-file digest is retained separately in
`source-hashes.json`.

Native controller and agent shutdown again report SIGABRT/core-dump status;
this remains visible in `result.json`. It does not indicate clean native process
shutdown. The scoped operational checks and restored baseline pass, but broader
native lifecycle qualification remains open.

## Recheck and interpret the result

```bash
uv run python scripts/replay-egress-accounting.py \
  doc/evidence/egress-accounting/egress-loss-01 > /tmp/emosa-egress-replay.json
cmp /tmp/emosa-egress-replay.json \
  doc/evidence/egress-accounting/egress-loss-01/egress-projection.json
python3 scripts/check-egress-accounting.py \
  doc/evidence/egress-accounting/egress-loss-01
python3 scripts/check-egress-accounting.py --native \
  doc/evidence/egress-accounting/native-egress-01
python3 scripts/check-native-recovery.py \
  doc/evidence/egress-accounting/native-egress-01 --minimum-seconds 210
```

The [unit suite](unit-results.xml) passes **1,306 tests** and the
[OVSDB suite](ovsdb-results.xml) passes **57 tests**. New checks exercise the
selected drop path, unsupported path rejection, counter/collector/configuration
resets, replay, event loss, freshness and real OVSDB publication.

Read the [learning guide](../../protocol/egress-accounting-source.md) to reproduce
the experiments. Receive-side loss, media/capacity/availability qualification,
complete native neighbor delivery, AP/STA reporting and final-session statistics
remain pending. The original 15-minute operational result is preserved; this
short regression does not complete sustained acceptance or qualify a physical
pod. No physical pod was changed.
