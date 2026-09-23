# Loss-checked native station counter accounting

`native-counter-audit-01` is a **159.68-second** focused native recovery and
measurement experiment. It does not replace the earlier 908-second operational
soak or establish complete sustained acceptance.

- 8,921 radio packets and 101 Ethernet packets: capture and filter totals match,
  zero reported drops, and all pcap records pass completeness checks.
- Seven new station events and six final records. All six correlate with actual
  reason-3 radio disconnects and EasyMesh leaves.
- Each final record reconciles exactly with the independently decoded trace:
  TX byte accounting excludes 16 CCMP bytes per protected frame; RX packet
  accounting counts four management frames twice. All 754 transmitted frames
  in the six completed sessions have their own captured ACK.
- Pod connection recovery takes 7.32 seconds including the deliberate outage;
  adapter SIGKILL recovery takes 1.22 seconds. Three authenticated operations,
  one total Config write, and traffic surviving both management faults.
- Cleanup has no errors and the baseline controller is restored. The existing
  native-controller shutdown SIGABRT remains recorded, not silently corrected.
- [Unit suite](unit-results.xml): **1,058 passed**, including 12 capture health
  cases. The independent scripts also pass against these retained artifacts.

The [accounting guide](../../protocol/station-counter-accounting.md) gives the
commands, source references, arithmetic, learning sequence and remaining work.
Inspect [independent accounting](native-counter-audit-01/independent-accounting-check.json),
[event correlation](native-counter-audit-01/independent-observation-check.json),
and [recovery](native-counter-audit-01/independent-recovery-check.json) separately.

`source-provenance.json` pins the reconstructed Ubuntu source subset;
`kernel-runtime.json` identifies the installed runtime packages/modules. The
source archives and extracted source remain outside Git. The earlier
`native-final-source-01-radio-capture.log` is retained here to document the 120
capture drops in that historical trial. It does not change that trial's successful
six-event correlation into a complete accounting result.

Publication contains the collector's reviewed synthetic-run allowlist plus
independent results and source/runtime provenance. No private journal, secret
store, native configuration/full logs, standards PDFs or binaries are included.
The simulated traffic uses disposable lab credentials. No physical pod is involved.

`raw_counter_passthrough_valid`, `final_counter_source_qualified`,
`sustained_operation_proven` and `physical_pod_proven` remain **false**. The native
run still has unanswered policy/neighbor requests and no measured final-statistics
delivery. Only the observed normal-traffic accounting has been established.
