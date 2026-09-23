# Combined shaped-backhaul loss and service evidence

The selected action, root scheduler and interface loss counters now share one
measurement interval with the declared 100 Mb/s service. Independent packet
checks cover queue overflow and both action directions; a real native/OVSDB
regression verifies lifetimes across connection loss and process restart.
Complete native neighbor metric publication remains pending.

## Controlled losses

| Run | Observed loss | Valid combined intervals | Independent result |
| --- | ---: | ---: | --- |
| `shaped-ingress-01` | 17 incoming action drops | 17 | [Passed](shaped-ingress-01/independent-shaped-check.json) |
| `shaped-egress-02` | 17 outgoing action drops | 16 | [Passed](shaped-egress-02/independent-shaped-check.json) |
| `shaped-queue-02` | 19,124 root scheduler drops | 33 | [Passed](shaped-queue-02/independent-shaped-check.json) |

The action probes each retain normal/drop/restored phases of 17 ICMP requests.
The independent checks compare every request/reply and direction-bearing SLL2
capture, including unrelated traffic. Ordinary interface drop counts stay zero
for these selected action losses. The selected loss counters supply what those
ordinary counters miss. Capture file/captured/filter totals agree with zero
reported kernel drops in all three experiments.

The queue probe uses eight UDP sockets, each with an observed 212,992-byte send
buffer. It retains the original 100 Mb/s TBF, 65,536-byte burst, 524,288-byte
queue and framing table. No sysctl is changed. Phase results are:

| Workload | Packets offered | Packets received | Queue drops | Observed service rate |
| --- | ---: | ---: | ---: | ---: |
| Large frames, requested 50 Mb/s | 16,255 | 16,255 | 0 | 50.003456 Mb/s |
| Large frames, requested 160 Mb/s | 52,015 | 32,891 | 19,124 | 100.000760 Mb/s |
| Small frames, requested 5 Mb/s | 29,762 | 29,762 | 0 | 5.000016 Mb/s |

The 98,036 input and 78,912 output capture records include unrelated ARP. Every
offered UDP sequence is present at pod input; the missing output sequences
match exactly 19,124 root drops; all output UDP sequences match application
receiver records. Both captures are complete and losslessly gzip compressed.
The queue drains before phase-end snapshots. The 115,095 token waits are not
packet losses. Ordinary interface/action loss components remain zero.

The checker separately reconciles all 33 combined packet/byte and service
intervals, with 641 ns observed clock-offset spread and the unchanged fixed
1 ms timing allowance. Incoming and outgoing action probes use 3,215 ns and
972 ns offset spreads. Counter intervals spanning configuration events are
discarded and new baselines are recorded.

## Preserve the unsuccessful qualification attempts

`shaped-queue-01-limited` retains the original result and collector observations
from a temporary 1 Mb/s, single-socket attempt. Sender backpressure prevented
queue overflow: it exercised zero queue drops. This is not successful loss
qualification and cannot enter the final 100 Mb/s source. The final helper
keeps the original service and changes the workload to eight sockets. Private
preliminary captures/receiver records are hashed in its failure analysis.

`shaped-egress-01-limited` retains an earlier probe that observed the selected
17 outgoing losses but lacked the complete direction-bearing capture. Its
selected-flow captures remain private with recorded hashes. The new
`shaped-egress-02` includes the whole-interface capture and passes the full
interval audit. No failed attempt was overwritten or relabeled as complete.

## Actual OVSDB and recovery

`native-shaped-01` runs for **210.015 active seconds** and passes the
[combined source check](native-shaped-01/independent-shaped-check.json) and
[operational recovery check](native-shaped-01/independent-recovery-check.json).
It contains 291 accepted common intervals, five baselines, three connection
generations and two worker processes. One repeated observation is recognized
without adding its counters again; 14 observed configuration epochs preserve
path lifetimes. The independent audit reconciles the complete 4,163-record
backhaul capture with zero reported drops, a 3,737 ns clock-offset spread and
the fixed 1 ms allowance.

Actual connection restoration takes **7.335 seconds**; actual SIGKILL/restart
takes **1.216 seconds**. Three authenticated operations produce only one total
Config-write attempt. All 27 connected controller inventories place the client
under the represented BSS; eight deliberate client disconnections are recorded.
All 822 wired probes pass. Wi-Fi has 689/800 replies and eight gaps matching the
intentional disconnects. Sampled RSS stays between 55,508 and 59,152 KiB, with
23 file descriptors. The independent policy, channel and telemetry-gap checks
also pass. OVSDB loss withdraws the combined source; a station-telemetry pause
does not invalidate this independent observation.

The control and radio captures contain 112 and 11,453 complete records with
matching totals and zero kernel drops. All [100 Python source files and the
trial-time candidate reference](native-shaped-01/source-match.json) match their
recorded digests. Cleanup restores the original queue and native candidate,
records no errors and passes the owned-lab idle check. Native controller/agent
shutdown still records SIGABRT/core-dump; it is an open lifecycle issue.

Three native neighbor queries remain unanswered (control frames 33, 68, 101).
The source is whole-interface accounting, not a fully qualified per-neighbor
publisher. Adapter multicast reflection, the selected media/availability
contract, AP/STA metrics and final-session reporting still require work. The
original 908-second operational soak is retained separately; this short run
does not complete integrated 15-minute acceptance or physical-pod proof.

## Reproduce and inspect

Use the [step-by-step guide](../../protocol/shaped-backhaul-accounting.md) and
[manual §13.27](../../guides/team-manual.md#1327-account-for-loss-while-the-virtual-link-is-shaped).
The independent checker imports no EMOSA implementation. CI replays all three
controlled experiments through the production source and audits their captures
plus the native recovery result. The retained suites contain **1,346 passing
unit tests and 60 passing real-OVSDB tests**. Existing unshaped/service replays
remain unchanged.

Kernel interpretation builds on the exact previously reconstructed
[scheduler](../virtual-capacity/source-provenance.json),
[veth/TC](../receive-accounting/source-provenance.json) and
[iproute size-table](../virtual-capacity/iproute-source-provenance.json) sources.
Run results pin the actual helper/collector versions and kernel; the native
run retains full source hashes. No physical pod has been changed or qualified.
