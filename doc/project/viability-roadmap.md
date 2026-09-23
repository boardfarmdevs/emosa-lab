# Next experiments toward bounded viability

Read [current status](current-status.md) first. It separates completed scoped
experiments from unresolved acceptance. The [integration baseline](integration-baseline.md)
is preserved, and [acceptance levels](integration-acceptance.md) distinguish warm
onboarding, cold startup, operational recovery and full sustained service.

The next target is **actual OpenSync managers in opensync-lab's hwsim pod**, reached
from EMOSA while its gateway and wireless client stay in the same VM. This is an
intermediate software integration milestone. Final physical acceptance remains
real EasyMesh messages → EMOSA → unchanged physical pod → independently observed
behavior.

## Preparation completed

- Consolidated current status, with explicit corrections to historical KiB,
  missing-wire, native shutdown and reporting claims.
- Preserved EMOSA/native inputs and the selected OpenSync source/image state in
  private, hash-verified baseline `opensync-integration-20260923-01`.
- Published separate acceptance levels and an initial register. No OpenSync-lab
  integration level has been executed or passed.

The [detailed integration plan](opensync-lab-integration-plan.md) is the execution
handoff when that work is authorized. Nothing in this roadmap authorizes changing
the shared running lab merely to make a status entry green.

## Next work, in order

| Order | Work | Required exit evidence |
| --- | --- | --- |
| 1 — Q | Recheck baseline drift and provide a controlled transport to the existing pod socket; run formal read-only qualification | Actual schema, complete radio/BSS/backhaul graph, endpoint trust, draft `writable=false` profile and no configuration writes |
| 2 — ownership | Implement per-pod NOC redirect assignment and writer exclusion; retain gateway AP/GRE duties; validate a pod-initiated EMOSA session read-only | Selected pod bound to the authorized session; gateway/client path preserved; no competing fronthaul writer after reconnect |
| 3 — A | Add a truthful OpenSync-container profile, native security/resource mapping and configurable runner; test offline/OVSDB before native writes | Real `owm` application and independent client observations; untouched backhaul; duplicate/lost-reply/conflict/failure checks; restoration |
| 4 — W | Drive a warm fronthaul change from the real native controller through EMOSA | Real discovery/WSC, causal operation and State, actual radio/BSS inventory and independent authentication/traffic; limitations explicit |
| 5 — C | Qualify bootstrap, PSM persistence and missing-resource creation from the declared cold state | Complete causal onboarding after cold start; NOC did not pre-provision the controller's target SSID/key |
| 6 — R/S | Connect and qualify native telemetry; complete the 900-second workload with recovery and all required reporting | R records operational recovery separately. S requires complete selected AP/STA/neighbor/final reporting and controller receipt, including management-over-backhaul recovery |
| 7 — reproduction | Consume pinned opensync-lab artifacts/build inputs in a fresh isolated lab if useful | Same profile and acceptance repeated; no silent image/source substitution or transfer of old verdicts |

The 5 GHz underlay is Wi-Fi/GRE. EMOSA's existing calibrated software-Ethernet
neighbor profile cannot be copied onto it without new measurement/media review.
Native OpenSync may be the best source for the next reporting work; there is no
requirement to finish a duplicate synthetic publisher before Q.

## Work that can proceed alongside qualification

- Complete remaining normative/procedure review: Early/Ack and AP-delivery
  interpretation, ESP byte conversion, BBF representation/source limits and exact
  WFA DEr3 comparison. Use the already obtained IEEE base/amendment; do not reopen
  their acquisition request.
- Qualify real native publisher output, membership, epochs and finality. A matching
  `.proto` file establishes format compatibility only. Preserve unavailable-data
  and incomplete-report outcomes until their sources are qualified.
- Collect a physical-pod draft when its populated private connection path and
  endpoint trust are supplied. A ready physical experiment need not wait for
  optional router ports, UI expansion or every synthetic lab feature.

Router C/Rust deployment, a complete ODH ingestion service, multiple wire agents
and another controller remain later or separately scoped work. They are not
prerequisites for the first container-pod experiment and will not be proven by it.

## Keep existing evidence in its original scope

| Retained result | What to reuse | What not to infer |
| --- | --- | --- |
| [Native controller/standard-agent baseline](../evaluation/peer-baseline.md) | Named peer startup, wired/wireless onboarding and independent clients | Universal onboarding or OpenSync integration |
| [Native simulated-pod onboarding](../protocol/native-onboarding.md) | Selected real-wire discovery/WSC and durable operation/application chain | Actual OpenSync managers or physical firmware |
| [901-second lifecycle run](../protocol/native-lifecycle.md) | Recovery, software-Ethernet neighbor publisher and clean selected native exits | Complete AP/STA/final reporting or a Wi-Fi/GRE metric profile |
| [Sparse-ESP candidate](../protocol/native-ap-esp.md) | Native parser fix and its own bounded onboarding/exit checks | A new full sustained run or measured ESP |
| [BBF conversions](../protocol/bbf-data-elements.md) | Pinned public definitions and selected encodings | WFA package equivalence or qualified native source values |
| [Secure fleet/reproduction](../guides/secure-fleet.md) | Real-service TLS, multi-pod simulation and recovery tools | Multiple EasyMesh wire agents or physical-pod support |

Earlier milestones and failed attempts remain in their linked evidence and
[the pre-consolidation roadmap](https://github.com/boardfarmdevs/emosa-lab/blob/4dece3c1c7581bb7eec0372c587e4f10cf8e05d0/doc/project/viability-roadmap.md).
They explain development history, not today's work order. Use the
[learning path](../guides/learning-path.md) for the broader component curriculum.
