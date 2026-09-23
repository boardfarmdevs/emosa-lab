# Backhaul packet/byte reconciliation and controlled loss

The independent audit reconciles **325** `eth1` counter intervals from the
retained [native identity-binding run](../neighbor-binding/README.md) with its
backhaul pcap. **602 of 650** directional packet windows have exact count bounds;
all packet/byte deltas fit the fixed timing bounds. Clock-offset spread is
**3,657 ns**; the audit uses a fixed **1 ms** allowance. The full
[interval audit](independent-interval-check.json) retains every window's bounds.

This establishes consistency of recorded packet/byte values, including transit
traffic and Ethernet headers. It does not establish complete IEEE packet-loss
fields, capacity, availability or native metric delivery.

The separate owned idle-lab **`backhaul-loss-02`** experiment answers an important
counter question. All 17 selected wired echo requests arrive on pod `eth2` and
are dropped by its `eth1` egress action. The action records **17 packets and 17
drops**. None crosses the backhaul capture; no reply appears. Nevertheless,
ordinary backhaul **`tx_errors` and `tx_dropped` both remain zero**.
The complete link path passes 17 requests before the fault and 17 after it.

The [loss checker](backhaul-loss-02/independent-loss-check.json) passes exact
request/reply sequence checks and capture health: **85 ingress** and **68
backhaul** frames, matching capture/filter/file counts, zero kernel drops.
The qdisc/filter is removed and original traffic-control state restored. A
separate owned-lab idle check passes. Source hash, actual kernel/module and tool
hashes are retained in the result, provenance and restoration records.

`backhaul-loss-01-limited` is retained as a failed capture audit. Its first
capture shutdown left buffered packets unread: 61/85 ingress and 38/68 backhaul
captured/filter counts. There were no reported kernel drops. The corrected helper
uses immediate mode and a drain period before shutdown; the second run passes
without weakening capture completeness. Both attempts restore traffic control.

```bash
python3 scripts/check-backhaul-accounting.py \
  doc/evidence/neighbor-binding/native-link-binding-04
python3 scripts/check-backhaul-accounting.py --loss-probe \
  doc/evidence/backhaul-accounting/backhaul-loss-02
uv run pytest -q tests/test_backhaul_accounting.py
```

The [unit suite](unit-results.xml) passes **1,278 tests**. New tests reject missing
or added frames, wrong byte accounting, clock jumps and truncated captures, and
preserve boundary uncertainty. No adapter or OVSDB implementation changed in
this accounting step; the retained [56-test OVSDB result](../neighbor-binding/ovsdb-results.xml)
remains the current integration evidence.

Read the [learning guide](../../protocol/backhaul-counter-accounting.md) for the
packet path, normative references, reproduction, and the distinction between
veth's fixed reported speed and measured capacity. Next work must address the
observed software-drop blind spot and qualify capacity/availability before
publishing complete neighbor metrics. Full sustained acceptance and physical-pod
acceptance remain pending.
