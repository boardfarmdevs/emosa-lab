# Live disconnect reasons and the 15-minute recovery workload

The owned hwsim observer joins actual radio disconnect reasons to kernel
removal-time samples while EMOSA runs. This closes the offline-only correlation
gap. **The raw counters remain unqualified and no final disassociation statistics
message is sent.** This evidence does not complete sustained acceptance or
qualify an unchanged physical OpenSync pod.

Follow the [reproduction guide](../../protocol/live-session-reasons.md),
[manual §13.31](../../guides/team-manual.md#1331-capture-the-actual-disconnect-reason-while-the-system-runs)
and learning-path step 21. The observer is part of the evaluation platform;
EMOSA remains the EasyMesh-to-OpenSync adapter.

## What the observer establishes

The passive collector reads the existing VM monitor and the owned AP's kernel
station-event log. Each record binds an actual unprotected disconnect frame,
its reason, creation/removal events and final raw values to one association.
No reason is inferred from silence or a disappeared row. Missing/conflicting
reasons, uncertain clocks, changed identity, capture loss or missed deadlines
invalidate the source. The independent checker uses the separate radio pcap,
tshark decoding, original kernel events and EasyMesh leave notifications.

Wall-clock reads are bracketed with monotonic reads. Read uncertainty must be
at most 100 microseconds, with at most three attempts; the existing maximum
clock-offset discrepancy remains one millisecond. A join waits at least 250 ms
after removal and must complete within one second. These implementation bounds
are specific to the owned profile, not additional normative EasyMesh limits.

## Preserved attempts

| Attempt | Observation | Acceptance limits |
| --- | --- | --- |
| `native-reason-01` | 211.794 active seconds; eight live joins, zero observer errors/drops | Raw acquisition passes; final native peer reply lacks a later inventory observation |
| `native-reason-02` | Observer fails on the clock-domain guard before any joins | Actual negative retained; runtime fails and restores the baseline |
| `native-reason-03` | 212.261 active seconds; eight joins with bracketed clock reads | Raw acquisition passes; final native peer reply again lacks a later inventory observation |

The old clock check used consecutive wall/monotonic reads without bounding
scheduling delay. Attempt 02 did not retain the offending read pair, so its exact
trigger cannot be reconstructed. The correction measures read uncertainty
explicitly and still rejects real clock steps over the original bound. Unit
checks cover preemption, persistent uncertainty and actual offset change.
Attempt 01 predates bracketed sampling; its independent result explicitly marks
that check as absent. Later attempts retain and audit the clock brackets.

The first peer audit also failed to account for a query during deliberate
discovery-observer loss. Its correction requires an observed expired heartbeat,
withdrawn measurements, unchanged configuration operation and the actual return
of a complete measurement interval. Only a query whose entire original
one-second budget precedes that interval can be classified as unavailable.
Every received reply retains its original deadline and value checks. The older
unanswered-query negative in `ap-reporting/native-ap-report-01` still fails.

Attempts 01 and 03 still fail peer receipt after that classification correction:
a packet being sent does not prove native-controller receipt. The final harness
now reads the controller inventory after confirmed worker exit and before
controller shutdown. The audit allows this bounded final observation only within
two seconds of worker exit and after the final reply. Earlier replies still
require receipt before subsequent replies can replace their statistics.

## Full run: `native-reason-04`

All six independent audits pass:
[raw reasons](native-reason-04/independent-reason-check.json),
[recovery](native-reason-04/independent-recovery-check.json),
[native lifecycle](native-reason-04/independent-lifecycle-check.json),
[peer metrics](native-reason-04/independent-peer-check.json),
[AP withholding](native-reason-04/independent-ap-withholding-check.json) and
[telemetry freshness](native-reason-04/independent-telemetry-check.json).

| Observation | Result |
| --- | --- |
| Active duration | 901.565326631 seconds; 129 connected controller inventory samples |
| Live raw joins | 35 distinct associations; 250.014–259.667 ms after removal |
| Observer health | Zero errors and socket drops; separate packet captures also report zero drops |
| OVSDB interruption | Automatic recovery in 6.331 seconds; fresh authenticated onboarding |
| Adapter SIGKILL | Automatic recovery in 1.221 seconds; fresh authenticated onboarding |
| Config effects | Three operations, one total Config-write attempt; zero additional recovery writes |
| Wired traffic | 3,527/3,527 replies; no gaps |
| Wi-Fi traffic | 2,939/3,433 replies; all 35 gaps match deliberate client outages |
| Channel procedures | Three preference queries, three accepted selections, three acknowledged measured operating reports |
| Peer measurements | 1,182 checked publications; 1,209 common counter windows |
| Native peer receipt | All 14 eligible queries answered in 0.395–6.679 ms, with matching later native interface statistics |
| Deliberate peer-path loss | One query withheld while the independently observed path was invalid |
| Resources | RSS 55,896–60,368 KiB; 23–24 descriptors |
| Required AP reports | Fifteen due periods explicitly unavailable; no invented report |
| Cleanup | No errors; clean native main-process exits; original binaries, BPL, references, queue and offload/isolation restored |

The worker uses three control generations and two processes across both faults.
The final controller snapshot is independently timed after worker exit and before
native shutdown. Earlier receipt observations still precede any subsequent
reply that could replace the reported statistics. The actual restored bytes and
idle lab are checked in the [post-restoration observation](native-reason-04/post-restoration-observation.json).

## Source provenance and restoration

Every attempt has 111 hashed runtime inputs. Attempts 01 and 02 each retain four
exact earlier executed source files and resolve the other 107 inputs against the
repository/reference. Attempt 03 retains its earlier harness and resolves 110.
Attempt 04 resolves all 111 against the current repository/reference.
The source audits point to the retained bytes; they never substitute current
code for historical execution. Candidate controller/BPL provenance is unchanged
from the [lifecycle build](../native-lifecycle/README.md).

Post-trial observations independently read restored controller, helper, BPL and
reference hashes and check the owned lab is idle. Failed attempts remain failed.
No physical pod, credentials, private configuration or process memory is included.

## Remaining acceptance

The retained unit execution contains **1,451 passing tests**. CI reproduces the
raw-join and operational audits and requires the earlier observer/receipt
failures to remain rejected. The existing 60-test OVSDB suite remains separately
retained under `native-lifecycle`; it was not rerun for this observer change.

Next obtain the selected Wi-Fi Data Elements definitions, qualify all AP/STA
counter conversions and other required measurements, connect the online source
to the implemented report senders, and independently verify native receipt.
Then repeat the full integrated acceptance with complete reporting enabled.
The eventual viability path remains **real EasyMesh messages → EMOSA → unchanged
physical OpenSync pod → independently observed behavior**.
