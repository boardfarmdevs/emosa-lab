# Native active-worker client pilot

This is the earlier 95-second pilot. Continue with the
[908-second operational recovery evidence](../native-soak/README.md) for the
later channel, pod-reconnect and adapter-SIGKILL checks. Complete mandatory
policy/metrics and disassociation reporting remain pending in that later run.

Run `native-active-03` extends the bounded native onboarding path with actual
client association telemetry and an EMOSA worker that remains active during
traffic and deliberate client reconnections. Recheck its retained inputs with:

```bash
python3 scripts/check-native-active.py doc/evidence/native-active/run-03
```

The checker uses the standard library and tshark, with no EMOSA imports. It first
rechecks onboarding capture/receipt correlation, Config withholding, observed
application, exact native radio/BSS and controller restoration. It then checks
the client event bytes and response timing against independent native STA
inventory and interface-bound nonce/traffic observations.

| Observation | Retained result |
| --- | --- |
| Active client phase | 95.37 seconds, 15 samples, one adapter PID |
| Native STA placement | Exact client MAC under the virtual agent's BSS in all 15 connected samples; absent in three deliberate disconnect samples |
| Wire events | Four joins and three leaves; four correlated capability-unavailable responses |
| Application | One authenticated WSC operation and one Config write; observed application and client traffic pass |
| Resources | RSS 54,272–54,328 KiB; 17 file descriptors in every sample; short pilot only |
| Original controller | Candidate wrapper records exact baseline restoration |

This is **not sustained-operation acceptance**. The agreed target is 15 minutes
with recovery checks. Channel selection/preference, reporting policy/metrics,
final disassociation counters/reason and native adapter/pod reconnection remain
outstanding. The controller's existing shutdown aborts are retained. Zero-valued
native metric fields are defaults, not measured results.

The first attempt `native-active-01` failed because Paho's wakeup socket needed
loopback enabled inside the owned packet namespace. Investigation also found that
this pinned hostap build returns an empty datagram for an empty station list.
The reader now accepts that reply only with a confirming enabled AP/zero-station
STATUS result. These failures and original logs remain under their VM labels.
Run 02 kept a client connected but the original inventory read depth stopped at
the BSS; a matching MAC appeared only in the gateway's neighbor list. Run 03
increases the read depth and explicitly checks the STA beneath the represented
BSS, and adds repeated disconnect/reconnect observations.

The publication allowlist contains only reviewed synthetic lab captures, public
operation receipts, native inventory, client observations and source/runtime
hashes. It excludes journals, credential files, full native logs, binaries,
backups and standards PDFs. See the [guide](../../protocol/sustained-operation.md)
for the measured telemetry contract and the remaining acceptance criteria.
