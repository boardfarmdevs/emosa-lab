# Final-statistics component and byte-unit regression

The final-disassociation sender, counter codec and guarded onboarding handoff
are implemented. **The simulator's final measurement source remains
unqualified**, so it does not send invented final statistics in the native lab.
These are two distinct validation results:

| Result | What it establishes | What remains unproved |
| --- | --- | --- |
| Three synthetic wire vectors | Wireshark decodes `0x8022`, mandatory station/reason/traffic TLVs, byte/KiB/MiB conversion and rollover as expected | Actual session counters/reason or live final-statistics delivery |
| `native-byte-counters-01`, 150.06 seconds | Correct byte-unit declaration throughout native discovery/WSC/capability reporting; both recovery faults preserve fresh onboarding and one total Config write | Complete 15-minute sustained acceptance or measured final disassociation reports |

The native regression uses the same patched controller candidate as the earlier
908-second run. It creates three authenticated operations; reconnect completes
in 7.32 seconds including the deliberate outage, and SIGKILL recovery in 1.21
seconds. The original controller is restored, cleanup reports no errors and the
owned lab was verified idle. The previous native shutdown SIGABRT behavior is
retained. See [the independent result](native-byte-counters-01/independent-check.json).

Earlier Profile-1 experiments advertised KiB in the accompanying Profile-2
capability TLV but sent no traffic-statistics TLVs. New experiments advertise
bytes to agree with EasyMesh 6.1 Table 58 before traffic reporting is enabled.
Their run metadata explicitly records `agent_counter_units: 0`; the checker
cross-checks every captured agent capability TLV against that declaration.
Earlier captures remain unchanged and keep their original bounded scope.

Recheck both results on HOST:

```bash
python3 scripts/check-disassociation-reference.py
python3 scripts/check-native-recovery.py \
  doc/evidence/final-statistics/native-byte-counters-01 --minimum-seconds 150
```

The [retained full test report](full-test-results.xml) records 1,081 passing tests
and three intentionally gated skips. New tests cover explicit field presence,
reserved reasons, stale/source-changed/rejoined clients, conflicting records,
unit changes, rollover, authenticated observed-leave handoff, deadlines,
bounded queues and send failure. The
[independent encoding check](independent-encoding-check.json) uses no EMOSA
imports. Its fixtures are explicitly synthetic and do not claim measurement.

For the publisher requirements, commands and normative references, follow the
[final-session guide](../../protocol/final-session-statistics.md).
Mandatory policy/AP/STA/neighbor metrics, a qualified final-session publisher
and complete sustained acceptance remain pending. Physical acceptance still
requires real EasyMesh messages → EMOSA → unchanged OpenSync pod → independently
observed behavior.

Publication uses the reviewed synthetic-run allowlist. Private operation
journals, secrets, native configuration/logs, binaries and standards PDFs are
excluded. The raw run and failed attempts remain on the owned VM.
