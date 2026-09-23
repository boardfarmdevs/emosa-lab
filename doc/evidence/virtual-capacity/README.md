# Virtual-link service calibration and recovery

The owned simulator can now opt into a declared 100 Mb/s egress service. A
passive observer carries its detailed queue/framing configuration and counters
through OpenSync-schema OVSDB. EMOSA computes bounded service work and unused
modeled service; it still withholds complete native neighbor metrics.

## Independent calibration

`virtual-link-03` passes the [independent check](virtual-link-03/independent-capacity-check.json).
All **78,617 UDP packets** reconcile by phase/sequence across sender, pod-input
capture, independent output capture and application receiver. Each capture has
78,621 complete records, equal file/captured/filter totals and zero reported
kernel drops. Unrelated ARP is retained. Packet files are losslessly gzip
compressed; the checker expands them under a fixed size budget.

The checker reconciles **33 service windows** with independent frames, including
the modeled 24-byte framing cost, 84-byte minimum and two-byte table resolution.
A fixed 1 ms timing
allowance is used; measured wall/monotonic offset spread is 571 ns.

| Workload | Packets delivered | Measured middle two-second service rate | Steady unused-service estimate |
| --- | ---: | ---: | ---: |
| Large frames, requested 50 Mb/s | 16,255 | 50.003456 Mb/s | 49.886–50.052% |
| Large frames, requested 160 Mb/s | 32,600 | 99.994608 Mb/s | 0–0.309% |
| Small frames, requested 5 Mb/s | 29,762 | 5.000016 Mb/s | 94.978–95.009% |

The requested saturation rate is not a measured offered rate. The synchronous
sender actually sustains about 100.272 Mb/s over its four-second phase. The
shaper records 97,295 token waits and no queue drops; those waits must not be
reported as packet loss. This run calibrates steady service and framing; it
does not prove arbitrary offered-load or queue-overflow behavior.

The nominal maximum 1,500-byte payload service is `100 × 1500 / 1538`, about
97.529 Mb/s. This is a declared Ethernet service model, not a physical PHY
measurement or application throughput guarantee. The source records unused
modeled service separately from any normative link-availability claim.

## Retained incomplete attempts

`virtual-link-01-limited` and `virtual-link-02-limited` preserve setup/results and
raw observer records from the initial calibration attempts. Their basic
`tc -j -s` dump omits STAB framing. The strict source refuses to calculate a
service estimate. Full preliminary captures and endpoint records remain private,
with hashes in each failure analysis; no public packet qualification is claimed
for those two attempts.

`native-capacity-01-limited` retains its complete synthetic review allowlist.
It passes the independent 210-second operational recovery check, restores the
original queue and candidate, and reports no cleanup errors. Its service
source remains unavailable because of the same missing framing input. The
independent capacity checker must reject it. The corrected collector uses
`tc -j -d -s`; `virtual-link-03` and `native-capacity-02` are separate new runs.

## Native OVSDB and recovery

`native-capacity-02` runs for **214.468 active seconds**. Its
[service audit](native-capacity-02/independent-capacity-check.json) reconciles
**321 service windows** across three connection generations and two adapter
processes. Five baselines and 15 observed configuration epochs preserve
lifetimes; one duplicate observation is recognized without double counting.
The 4,266-record backhaul capture has matching file/captured/filter totals and
zero drops. Clock offset spread is 2,214 ns; the timing allowance stays 1 ms.

The [initial audit failure](native-capacity-02/initial-audit-failure.json)
preserves a 929-versus-930 byte discrepancy. The original independent formula
assumed one-byte table resolution. Reconstructing Ubuntu's exact
[iproute2 source](iproute-source-provenance.json), including its distribution
patch, shows the selected table rounds odd adjusted lengths up to two bytes.
The adapter already used observed charged-byte counters. Correcting the
auditor and documenting the actual service model reconciles all windows;
runtime code, raw counters, captures and timing bounds remain unchanged.

The [recovery check](native-capacity-02/independent-recovery-check.json) passes
connection restoration in **6.423 seconds** and actual SIGKILL/restart in
**1.215 seconds**. Fresh WSC sessions create three operations with only one
total Config-write attempt. All 28 connected inventories place the client under
the represented BSS; eight deliberate Wi-Fi disconnects are retained separately.
Policy receipt/schedule, channel and telemetry-gap checks also pass. The service
estimate survives the independent telemetry pause and withdraws when its OVSDB
connection is lost, then starts a new baseline after recovery.

The control/radio captures contain 115 and 12,032 complete records with zero
reported kernel drops and matching totals. RSS stays between 55,432 and 59,804
KiB with 23 file descriptors. Cleanup restores the original queue and native
candidate, reports no errors and passes the owned-lab idle check.
All [99 recorded Python source files and the trial-time candidate reference](native-capacity-02/source-match.json)
match their reviewed inputs. All 840 wired probes pass; the eight Wi-Fi probe
gaps coincide with the deliberate client disconnects. Native
agent/controller shutdown still records SIGABRT/core-dump status; this remains
an open lifecycle issue rather than a clean-shutdown claim.

Four actual neighbor queries remain unanswered because the full measurement
source is incomplete. These observations are not counted as successful native
metric delivery or as a complete sustained acceptance pass.

## Reproduction and scope

See the [step-by-step guide](../../protocol/virtual-link-capacity.md) and
[manual exercise 13.26](../../guides/team-manual.md#1326-measure-a-declared-virtual-link-service).
The [source provenance](source-provenance.json) pins five reconstructed scheduler
files from the exact Ubuntu kernel package. [Runtime provenance](runtime-provenance.json)
records the loaded-kernel version, veth/TBF modules and tool digests. No kernel
is built or installed by source review.

The retained suites contain **1,338 passing unit tests and 59 passing real-OVSDB
tests**. Earlier egress and receive replay outputs remain byte-for-byte unchanged.
The missing-framing attempt is explicitly rejected in CI, alongside positive
calibration and native checks.

The optional TBF service is distinct from the previously checked unshaped loss
path. Combining losses, peer attribution, selected media and protocol availability
semantics remains necessary before native publication. AP/STA metrics and final
session statistics remain incomplete. The original 908-second operational soak
is retained separately; a short regression cannot complete integrated 15-minute
acceptance. No physical pod has been changed or qualified.
