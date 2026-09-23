# Observed pod interfaces and live controller discovery

The owned simulator now binds its virtual agent topology to the pod's actual
forwarding interfaces and to controller discovery received at the pod's backhaul.
The retained `native-link-binding-04` run passes independent identity, topology,
client-traffic and recovery checks over **214.56 active seconds**. This is a
component regression; full 15-minute sustained acceptance remains incomplete.

The [independent binding result](native-link-binding-04/independent-binding-check.json)
establishes:

- **321** bound observations. Every discovery used by those bindings matches
  bytes and receipt time in the separate backhaul capture: frames **7, 760, 1942
  and 3152**. Capture starts before controller startup.
- All five emitted Topology Responses list observed pod `eth1`, `eth2` and
  `wlan0` MACs, their bridge tuple and the controller as the backhaul neighbor.
  All **28** sampled native controller inventories contain these three actual
  interfaces and the IEEE 1905 controller neighbor under pod `eth1`.
- Pausing the passive observer withdraws stale topology while control authority,
  radio observations and independent wired/Wi-Fi forwarding remain available.
  Observation resumes after **6.57 seconds**, without a false client leave,
  repeated onboarding or Config write.
- **328** raw forwarding samples and **325** counter intervals are checked
  across two worker processes and three OVSDB connection generations. One
  status-only event is retained without counting its interval twice.

The [recovery audit](native-link-binding-04/independent-recovery-check.json)
records actual pod-connection recovery in **6.33 seconds**, including the
deliberate interruption, and adapter SIGKILL recovery in **1.22 seconds**.
Three authenticated operations retain **one total Config-write attempt**.
The separate MQTT observation gap lasts **6.35 seconds** without losing control
authority or forwarding. Eight deliberate Wi-Fi detach/reattach cycles are
observed; Wi-Fi probe loss during those cycles is retained, not called zero loss.
All 840 continuous wired probes receive replies.

Capture health reports matching file/filter/captured counts and zero kernel
drops: **4,266 backhaul**, **114 controller-facing Ethernet** and **12,026 radio**
packets. Independent traffic probes and packet observations do not yet qualify
the raw interface counters as per-neighbor measurements.

Four native neighbor-metric queries remain unanswered. Three reach active
sessions that explicitly record unavailable measurements. Frame **41** falls
strictly inside the independently timed OVSDB source-loss window. The audit
classifies that frame separately; it does not claim the disconnected session
handled it. Clock disagreement, boundary races and unexplained count differences
still fail. Three policy-reporting periods remain unfulfilled.

## Preserved attempts and checker corrections

| Attempt | Outcome and retained limitation |
| --- | --- |
| `native-link-binding-01-limited` | Controller inventory deadline expired; the EtherType-specific pod socket saw no discovery. |
| `native-link-binding-02-limited` | Explicit binding wait expired. Independent capture saw discovery while the pod socket did not, identifying the Linux bridge receive-hook problem. |
| `native-link-binding-03-limited` | Corrected `ETH_P_ALL` collector passed runtime recovery, but its initial discovery preceded the independent capture. The complete binding audit cannot pass for this attempt. |
| `native-link-binding-04` | Capture begins before controller startup. All discoveries used by the binding match independent capture; the scoped checks pass. |

Each limited directory keeps a failure analysis and relevant original artifacts.
Complete private runs remain in the owned VM. The public subset excludes private
configuration, operation databases and full native journals.

The controller inventory checker now selects the exact managed `Device.N.`
object. Previously, a `Neighbor.N.` reference with the same AL ID could be
mistaken for another managed device. Tests retain failure for a neighbor-only
inventory or genuinely duplicated managed device. The metric-query audit also
distinguishes a proven source-loss interval from an active measurement refusal.
Neither correction changes captured messages or fabricates a report.

## Recheck this evidence

```bash
python3 scripts/check-neighbor-binding.py doc/evidence/neighbor-binding/native-link-binding-04
python3 scripts/check-native-recovery.py \
  doc/evidence/neighbor-binding/native-link-binding-04 --minimum-seconds 210
python3 scripts/check-telemetry-gap.py doc/evidence/neighbor-binding/native-link-binding-04
python3 scripts/check-native-policy.py doc/evidence/neighbor-binding/native-link-binding-04
python3 scripts/check-neighbor-metrics.py --native-directory \
  doc/evidence/neighbor-binding/native-link-binding-04
```

The [unit suite](unit-results.xml) passes **1,268 tests** and the
[real-OVSDB suite](ovsdb-results.xml) passes **56 tests**. These cover malformed,
stale, replayed and ambiguous discovery; lifetime and context separation; and
the actual manager → OVSDB → topology-response pipeline.

[Restoration checks](native-link-binding-04/restoration-check.json) confirm the
baseline restored, an independent idle check passed, and all **94** checked
executed Python source hashes match the reviewed source. The existing native
controller/helper-agent shutdown SIGABRT remains recorded; restoration success
does not mean clean native process termination.

## Scope and next work

This proves observed interface identities for the owned sole-backhaul profile.
Ethernet/Wi-Fi media values remain simulator inputs. Native controller interface
statistics defaulting to zero are not measured results. Direct-link LLDP behavior
has component coverage only; the native run observes an intervening bridge.

Next qualify per-neighbor counter attribution and capacity/availability, then
deliver and independently verify native metrics. AP/STA reporting and qualified
final-session statistics are also required before repeating full 15-minute
acceptance. The [learning guide](../../protocol/neighbor-discovery-binding.md)
explains the setup, concepts and observation-pause experiment. No physical pod
was changed or qualified.
