# Current EMOSA status

> **Update 2026-09-24: real OpenSync pods onboarded.** A native prplMesh
> controller onboarded three unchanged OpenSync 6.6.1.0 pods (opensync-lab, hwsim)
> as EasyMesh agents through EMOSA virtual agents.
> - The fronthaul came only from the controller's M2 and was applied by each
>   pod's own `owm`, and six clients reached the internet.
> - A 900 s recovery workload with transport, backhaul, adapter and controller
>   faults passed.
> - The controller shows the pods' channel through Operating Channel Reports.
> - A second controller, RDK-B unified-wifi-mesh, gets as far as M2 with the
>   `r1` message set.
>
> Start with the [run record](../evidence/opensync-lab-proof/README.md) and the
> status table in the [proof plan](proof-plan.md). The rows below about the
> "actual OpenSync container" predate this and are superseded by the run record.
> The frozen baseline `opensync-integration-20260923-01` is retired as an
> integration target; see [integration-baseline](integration-baseline.md).

**Reviewed 2026-09-23. This is the current-status entry point.** Historical
experiment records describe their own revisions and results; they are not a list
of today's unresolved tasks. The [roadmap](viability-roadmap.md) gives the next
work order, and the [acceptance levels](integration-acceptance.md) define what a
new integration may claim.

EMOSA is the **EasyMesh to OpenSync Adapter**, including the controller-facing
virtual agent. OVSDB is its selected southbound management interface. The final
goal remains real EasyMesh messages → EMOSA → an unchanged physical OpenSync
extender → independently observed behavior.

## What is established, and on which system?

| Boundary | Current evidence | Remaining limit |
| --- | --- | --- |
| Model, real OVSDB and persistent service | Durable operations, guards, identity, independent simulated application, TLS, multiple connecting simulated pods and restart/reconnect exercises | These component/service results are not native-controller or physical-pod results. |
| Native controller → EMOSA → simulated pod | Bounded discovery, Early-before-M1, authenticated WSC, one Config operation, independently observed hostapd/hwsim application, controller radio/BSS inventory and client traffic | One explicit profile with a patched prplMesh candidate; simulated OpenSync manager, incomplete mandatory procedures and no general conformance claim. |
| Continued operation and recovery | Retained 908-second run; later 901.473-second run adds measured neighbor replies and clean candidate controller/helper exits | Full sustained acceptance remains pending: qualified complete AP/STA and final-session reports are absent. |
| Controller parser | Sparse-ESP candidate passes native receipt/malformed-input checks and bounded onboarding; its selected processes exit cleanly | Synthetic parser values are not AP measurements. This candidate is distinct from the one used in the 901-second run. |
| Measurements and reporting | Policy receipt/Ack and schedule, report encoders/dispatch, live reason/removal correlation, selected kernel accounting and bounded BBF conversions exist | Complete live native reporting, source/width/ESP qualification and final-session delivery remain unfinished. |
| Actual OpenSync container | Read-only inspection of opensync-lab's working gateway/pod/client system; source/image baseline retained | No EMOSA connection handover, formal `qualify-pod` result, admitted mapping or native-controller integration yet. |
| Unchanged physical extender | Read-only qualification tooling and private-connection examples exist | No populated private physical connection configuration, qualified profile or physical proof. |

Evidence: [native onboarding](../evidence/native-onboarding/README.md),
[operational soak](../evidence/native-soak/README.md),
[lifecycle/neighbor recovery](../evidence/native-lifecycle/README.md),
[sparse ESP](../evidence/ap-esp/README.md),
[live disconnect source](../protocol/live-session-reasons.md),
[BBF review](../protocol/bbf-data-elements.md) and
[OpenSync lab evaluation](opensync-lab-integration-plan.md).
The retained BBF increment has **1,517 passing unit tests**; the existing OVSDB
suite has 60 passing checks. These are retained suite results, not new lab runs
performed while consolidating this documentation.

## The selected preparation baseline

[Baseline `opensync-integration-20260923-01`](integration-baseline.md) preserves:

- EMOSA code at `58763f3`, archived at documentation-only successor `4dece3c`.
- Both the lifecycle-soak native candidate and the newer sparse-ESP candidate,
  separately identified by executable/library hashes and existing evidence.
- opensync-lab commit `77318dc` **plus its captured dirty working tree**, the four
  OpenSync source trees, applied provider overlay, selected pod image and selected
  live startup/binary observations. The baseline does not claim those current
  sources exactly reproduce every running image file.
- The evaluated **`mvx-opensync-0922`** VM on rev140. A separate
  **`opensync-lab-0923`** was also running when the baseline was captured, with
  only `mv3` observed. It is not silently substituted for the evaluated system.

The pod runs OpenSync **6.6.1**; the gateway reports **4.4**. Keep gateway, pod and
wireless client in the same hwsim VM for the first integration. The baseline is a
protected artifact/source copy, not a VM snapshot or a completed integration.

## Resolved historical statements

| Earlier statement | Current interpretation |
| --- | --- |
| IEEE 1905 base/amendment unavailable | Both exact editions are obtained. Selected implementations use them; full normative/profile coverage remains incomplete. Do not request these PDFs again. |
| No controller-visible EMOSA radio/BSS or WSC-to-operation path | The bounded native simulated-pod experiment establishes this path. The regular component service's diagnostic `agents` list remains a different interface. |
| Controller shutdown always aborts | Retained older baseline builds have that defect. The optional lifetime candidate fixes the selected Linux/no-WHM profile and passes real exit checks. |
| Advertise KiB for the current Profile-1 experiment | Current runs advertise **bytes**, `agent_counter_units: 0`, following Table 58 and the [byte-unit regression](../evidence/final-statistics/README.md). Earlier KiB captures remain historical evidence. The controller's ability to handle KiB/MiB is a separate fact. |
| Wait for the WFA spreadsheet before any metric work | Public BBF definitions now support selected conversions. Exact WFA DEr3 equivalence, source qualification and remaining representation questions are still open. |
| Integrated neighbor reporting is wholly absent | It passes for the explicitly calibrated software Ethernet profile, including the lifecycle soak. That profile does not qualify opensync-lab's Wi-Fi/GRE path. |
| Fifteen minutes plus recovery means sustained acceptance passed | Operational recovery passed. Missing required reporting prevents a complete sustained verdict. |

## What comes next

The next bounded work is the [opensync-lab integration plan](opensync-lab-integration-plan.md):
formal read-only qualification, explicit NOC writer exclusion and per-node routing,
a truthful container profile, native semantic application, then warm wire
onboarding. Cold startup and full sustained reporting have their own gates.
**That integration has not been executed or authorized by this preparation record.**

The three preparation items are now recorded: current status is consolidated,
the selected source/image artifacts are privately retained and hash-verified,
and separate acceptance levels are published. No broad C/Rust port, multi-pod
wire implementation, complete ODH deployment or finished synthetic publisher is
needed before read-only container qualification. Essential pre-write integration
work must still be implemented and tested.

Remaining specification and physical access requests are in
[open inputs](open-inputs.md). Keep unresolved Early/Ack and AP delivery
interpretations, ESP conversion, BBF width/source rules and exact WFA comparison
visible; do not hide them behind an onboarding success label.
