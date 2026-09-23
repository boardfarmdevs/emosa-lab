# Kernel completion flags explain the retry discrepancy

The passive observer recorded 1,029 loss-checked kernel completion records in
`native-tx-status-01`, a 214.76-second experiment with the pinned wmediumd
and 20% AP-to-client loss. Independent correlation accounts for 962 completions
across ten station lifetimes, including two brief failed association attempts.

| Accounted observation | Total |
| --- | ---: |
| Medium retry attempts | 2,421 |
| Completions with A-MPDU control but no aggregate completion status | 833 |
| Retries suppressed by that kernel rule | 2,094 |
| Predicted and observed final kernel retry count | 327 |

The equality holds separately for each lifetime. Previous-lifetime completions
inside the next lifetime's pre-event allowance are explicitly accounted for and
never counted twice. The checker correlates header, length, rate chain, ACK flag
and timestamp, then checks the observed final counter. It does not import the
EMOSA implementation or the trace collector.

All capture/filter/record counts agree with zero reported drops: 11,574 radio,
136 Ethernet and 62,241 netlink records. The trace has zero overruns/drops and
its per-CPU entry totals match the projected records. Runtime kernel/BTF hashes,
probe offsets and event format are retained. The private raw trace, including
kernel pointers, remains outside Git; the projection records only whether a
station context was present.

```bash
python3 scripts/check-tx-status-accounting.py \
  doc/evidence/tx-status/native-tx-status-01
uv run pytest -q tests/test_tx_status_accounting.py
```

Read [the operator guide](../../protocol/tx-status-accounting.md) before repeating
the trace. The helper refuses a different kernel/BTF layout. Kernel lockdown
remained enabled, and the owned probe/instance were removed after collection.
The candidate controller was restored. No physical pod was connected or changed.

**This run does not pass onboarding/recovery acceptance.** Its initial two-packet
Wi-Fi probe lost one packet under intentional medium loss. It also created four
onboarding operations, with one total Config-write attempt: a telemetry gap caused
an extra discovery while OVSDB remained connected. Both intended faults completed,
but those facts do not cancel the additional operation. The strict zero-loss
onboarding and three-request policy gates correctly reject this run; see
[the retained limitations](native-tx-status-01/acceptance-limitations.json).
The unwanted restart is fixed and tested separately in the
[telemetry freshness regression](../telemetry-freshness/README.md).

The traced flag explains the old discrepancy's mechanism; this is a separate
corpus from the original 2,405-versus-185 run. Raw retries still omit attempts,
so raw passthrough and the online measurement source remain unqualified.
Required AP/radio/STA/neighbor reporting, final-session publication, full
sustained acceptance and physical-pod proof remain pending.
