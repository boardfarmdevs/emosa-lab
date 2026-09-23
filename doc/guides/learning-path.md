# A learning sequence from first checkout to a defensible demonstration

[Documentation index](../README.md) · [Full team manual](team-manual.md)

EMOSA is the **EasyMesh to OpenSync Adapter**. The lab teaches and tests its
parts separately before the complete controller-to-pod proof is possible. Follow
the sequence below in order on your first visit. At each checkpoint, explain
what the observation proves before moving on. A passing software experiment does
not put an extender into a real EasyMesh controller's inventory.

## Before you start: choose the right machine and directory

**HOST** means your development machine and checkout. **VM** means a dedicated
Ubuntu virtual machine managed by the host's LXD. **CONTAINER** means an
unprivileged system container inside that VM. Commands below identify the level;
never infer it from a root prompt. The model, OVSDB and TLS fleet need only HOST.
The clean reproduction uses all three levels, with its commands issued on HOST.
The optional radio exercises use the separate radio VM.

Use a separate learning clone if you plan to edit scenarios while someone else
develops in the canonical checkout. Running the documented commands in either
clone is also supported: generated files go in ignored `.cache`/`.lab` directories.
Use a new experiment directory for each run; an existing one is deliberately
rejected so that yesterday's evidence cannot be overwritten.

## 1. Explain the target before installing anything

Read [manual §2](team-manual.md#2-understand-the-architecture-and-vocabulary),
especially the language/upstream inventory and cloud/EasyMesh/ODH diagrams.
Identify the Python adapter, the virtual agent it will represent, the existing
OpenSync managers, and the independent client. OVSDB is the adapter's southbound
management interface; it is not the name or full purpose of the adapter.

**Checkpoint:** draw the intended path: real controller messages → EMOSA → an
unchanged physical pod → independently observed client behavior. Name the missing
wire and physical qualification inputs without assuming a simulator supplies them.

## 2. Install and verify the developer environment — HOST

Follow [manual §3](team-manual.md#3-set-up-a-developer-checkout): selected `uv`,
Python 3.13.7, clone, `uv sync --frozen`, CLI help and unit checks. This establishes
the Python environment. It does not create a VM, start a radio, or build prplMesh.

**Checkpoint:** identify your checkout revision, Python version and test result.
Explain why the lockfile matters when another team member repeats the exercise.

## 3. Learn desired, committed and observed — HOST

Follow [manual §4](team-manual.md#4-run-the-model-and-learn-to-read-a-result).
Run a successful model change and a deliberately late/failed one. “Run the model”
means execute the operation engine against a small simulated backend. Nothing is
sent to a pod. Read the timeline, desired/observed differences and original outcome.

**Checkpoint:** distinguish a request, a database commitment and application
evidence. Explain why late evidence can resolve uncertainty while the deadline
failure remains in the record.

## 4. Cross a real database boundary — HOST

Follow [manual §5](team-manual.md#5-build-and-exercise-the-real-ovsdb-simulator).
Build the pinned OVSDB tools with OpenSSL development headers. Run normal and
lost-reply scenarios. A real database now stores Config, and a separate simulated
manager decides when to publish State. There is still no radio.

**Checkpoint:** locate a Config transaction and the separately observed State.
Explain why `CONFIG_COMMITTED` is insufficient to claim that Wi-Fi works.

## 5. Operate one persistent adapter and connecting pods — HOST

Use [manual §6](team-manual.md#6-use-the-long-running-adapter-and-every-local-cli-operation)
and the [connecting-pod walkthrough](connecting-pod.md). Inspect `agents`, plan a
change, submit it, inspect the operation and retry the same idempotency key.
Then run the two-pod process-crash exercise in [service integration](service-integration.md).

The pod initiates the connection, but EMOSA still issues management requests to
the pod's database. The `agents` command is a local diagnostic view. It is useful
evidence of the adapter's representation; it is not a native controller inventory.

**Checkpoint:** show which two pod identities share one adapter process and
explain what makes an identity unavailable after disconnect.

## 6. Authenticate the connecting pod — HOST, priority 1

Follow [secure fleet §2](secure-fleet.md#2-authenticate-before-admitting-json-rpc).
Run the TLS tests and the two-pod exercise. Learn the separate jobs of the CA,
certificate, private key, peer pin and expected database serial. Review missing
certificates, wrong CA, wrong pin and stalled handshake results.

**Checkpoint:** explain why an unauthenticated connection cannot replace the
valid session, and why a trusted certificate with the wrong database serial
still fails admission. All certificates here are disposable synthetic test inputs.

## 7. Measure several pods sharing the service — HOST, priority 2

Follow [secure fleet §3](secure-fleet.md#3-measure-4-8-16-and-32-real-database-sessions).
Run 4, 8, 16 and 32 pods sequentially. Read operation latency, adapter resource
samples, per-pod database checks and disjoint operation histories.

**Checkpoint:** quote the pod count, workload, machine, sample interval and
measured percentile together. A run at 32 configured pods is a bounded lab
measurement, not a supported production capacity or universal latency guarantee.

## 8. Preserve truth through faults — HOST, priority 3

Follow [secure fleet §4](secure-fleet.md#4-repeat-recovery-and-interpret-failures).
Repeat crash/reconnect/database-restart/late-State cycles. Keep one pod waiting
or offline while another progresses. Observe a competing writer and the durable
ownership refusal after adapter restart.

**Checkpoint:** show the original timeout, late resolution, one transaction
attempt, stable AL identity and peer progress. Explain why deleting the journal
to clear a conflict would invalidate this recovery experiment.

## 9. Reproduce priorities 1–3 from an installed image — HOST → VM → CONTAINER

Use [clean reproduction](../../deploy/reliability/README.md). Preflight, create
the owned VM, build a wheel/runtime, publish it before experiments create keys,
and run a new container from that image. Retain reports and image hashes, then
use the ownership-checked cleanup. This track needs no prplMesh, hwsim or pod.

**Checkpoint:** prove the new container loaded the installed package outside a
checkout, reproduced TLS/fleet/recovery checks and used the recorded image. Point
to the export location; a digest alone does not make an image downloadable.

## 10. Learn the wire envelope, then choose the next evidence boundary

Both IEEE 1905 PDFs are now obtained. On HOST, follow the
[frame/CMDU/TLV learning exercise](../protocol/ieee1905-envelope.md): inspect the
retained native PCAP, explain why 59 frames produce 58 messages, run the boundary
and negative tests, then compare with the independent dissector. Optionally run
the isolated VM packet check. No radio or pod is required for this step.

Then follow the [discovery and WSC exchange walkthrough](../protocol/autoconfiguration.md)
and [manual §13.8](team-manual.md#138-follow-controller-discovery-into-a-radio-bound-wsc-exchange).
Compare the native Search/Response profiles, explain why their matching MID is
insufficient, and reproduce rejection of wrong-radio, stale-generation, replayed
and unsupported complete configuration requests.

Continue with [capability and topology reports](../protocol/reports.md) and
[manual §13.9](team-manual.md#139-explain-the-agent-with-capability-and-topology-reports).
Run the three-frame offline exercise, read its synthetic receiver inventory,
and optionally repeat over isolated VM Ethernet sockets. Explain the one-second
response deadline and why absent association-age data cannot be invented.

Next run the [database-backed report coordinator](../protocol/report-coordinator.md)
and [manual §13.10](team-manual.md#1310-keep-reports-current-with-the-read-only-coordinator).
Observe a Config-only change, a separate manager State change, missing client age,
disconnection and fresh reconnect. Then repeat with `--coordinator` in the isolated
VM packet runbook. Explain why an Ack establishes receipt rather than WSC admission.

Then run [discovery before reporting](../protocol/discovery-session.md) and
[manual §13.11](team-manual.md#1311-discover-the-controller-before-reporting-the-simulated-pod).
Compare an incompatible advertisement with a correlated response, then reconnect
the database and observe the requirement for a new Search. Repeat with
`--discovery` in the VM. Automatic Early Report and M1 remain blocked; explain
the distinction between correlation, read-only reporting and full admission.

Continue with the [authenticated WSC-to-OVSDB handoff](../protocol/wsc-provisioning.md)
and [manual §13.12](team-manual.md#1312-turn-authenticated-wsc-input-into-a-durable-operation).
Build the small pinned hostap payload peer, run the four owned database cases
and compare commit evidence with separately published State. Show that one
M2 creates one component operation without a semantic submission, and that
a real adapter crash after commit cannot cause an automatic second write.

Now cross the actual packet/radio boundary with [Ethernet WSC to observed Wi-Fi](../protocol/wsc-wire-radio.md)
and [manual §13.13](team-manual.md#1313-drive-wi-fi-from-an-ethernet-wsc-exchange).
First run the packet-only case, then normal and lost-reply radio runs. Match the
received M1 digest to the operation receipt, show old State while application is
withheld, and verify new SSID/authentication/traffic from separate clients.
Explain why the synthetic hostap peer cannot establish native-controller inventory.

Next follow [native discovery](native-discovery.md) and
[manual §13.14](team-manual.md#1314-observe-native-discovery-before-claiming-onboarding).
Observe the real controller answering EMOSA's Profile-1 Search and creating a
device entry. Its zero radio/BSS counts and remaining response capability issues
explain why discovery visibility is still short of completed onboarding.

Then follow the [isolated controller fix](controller-counter-candidate.md) and
[manual §13.15](team-manual.md#1315-fix-and-compare-one-native-controller-capability).
Build on HOST, run the native conversion checks, stage in the VM, exercise
post-install failure recovery, and compare baseline/candidate/restored packets.
Explain why `40 → c0 → 40` establishes a corrected flag and successful restoration,
while zero represented radios/BSSs still leave the onboarding objective open.

Next join those boundaries in [native simulated-pod onboarding](../protocol/native-onboarding.md)
and [manual §13.16](team-manual.md#1316-join-the-real-controller-to-the-simulated-opensync-extender).

Continue with [sustained operation](../protocol/sustained-operation.md) and
[manual §13.17](team-manual.md#1317-keep-the-virtual-agent-active-while-clients-use-it).
Learn to distinguish an AP that retains its configuration from an adapter that
continues reporting clients to its controller. The active pilot adds measured
OpenSync-format telemetry. Work through these exercises in order:

1. Run the 90-second active-client pilot and locate each join/leave in the packet
   capture and the controller's exact virtual BSS inventory.
2. Follow a channel preference/selection exchange. Compare measured operating
   power with the advertised maximum; explain why they are separate inputs.
3. Run a 150-second pilot with `--recovery-checks`. Correlate the connection loss
   and SIGKILL with fresh M1 hashes and new no-op operation receipts.
4. Reproduce for 900 seconds, retaining continuous probes, intentional client
   outage windows, process samples and the independent check. Explain why a
   scoped recovery pass still leaves mandatory policy/metrics and final
   disassociation reporting as gaps in complete sustained acceptance.
5. Follow [final-session statistics](../protocol/final-session-statistics.md) and
   [manual §13.18](team-manual.md#1318-understand-final-session-counters-before-reporting-a-client-leave).
   Decode the synthetic messages, check rollover and byte units, and exercise
   the authenticated observed-leave handoff. Then identify why the lab still
   needs a qualified source for the actual final counters and disconnect reason.
   A passing codec test does not fill in missing measurements.
6. Run the [kernel station-removal observation](../protocol/station-removal-observations.md)
   with the native controller and both recovery faults. Trace a removal-time
   counter record to the independent radio reason and EasyMesh leave. Distinguish
   verified acquisition from the remaining qualification of counter meanings.
7. Follow the [counter accounting exercise](../protocol/station-counter-accounting.md)
   on HOST. Check capture completeness before comparing final counters against
   packets. Reproduce the first-session byte arithmetic and source provenance;
   explain the extra management-frame counts and why normal-traffic agreement
   leaves failure/retry semantics and online reporting unqualified.
   Then use the optional [medium-loss experiment](../protocol/medium-loss-accounting.md)
   to observe failed transmissions while association remains intact. Compare the
   independent netlink status trace with kernel counters, and explain why modeled
   attempts, reported retries and independently observed delivery can differ.
   Continue with [kernel completion flags](../protocol/tx-status-accounting.md):
   correlate original aggregation flags and rate chains with the final retry
   count. Explain why accounting for suppressed retries still does not qualify
   the raw counter for EasyMesh.
8. Follow [reporting policy receipt](../protocol/reporting-policy.md). Decode the
   native controller's 60-second policy, correlate its receipt Ack, and inspect
   the persisted schedule through both faults. Find the explicitly missing
   reports; explain why a timely Ack does not fulfill the reporting obligation.
9. Reproduce [telemetry freshness and recovery](../protocol/telemetry-freshness.md).
   Pause telemetry while OVSDB and client traffic remain healthy. Verify that
   observations become unavailable without a false leave or another onboarding
   operation. Then compare actual pod disconnection and adapter restart, which
   must revoke the old authority and authenticate a fresh operation.
10. Follow [neighbor link metrics](../protocol/neighbor-link-metrics.md). Decode
    TX-only, RX-only and combined responses, then inspect the native query and
    explicit missing-source status. Trace the separate adapter and pod
    interfaces through the VM bridge. Explain why controller-facing packet
    counts cannot automatically measure traffic forwarded by the pod.
11. Follow [forwarding observations](../protocol/forwarding-observations.md).
    Trace independently read port identities/counters through the pinned OVSDB
    graph into EMOSA. Recompute an interval, inspect fresh baselines after both
    faults, and find client transit traffic plus the controller's interface
    advertisement in the separate backhaul capture. Explain the remaining
    difference between raw interface counts and qualified neighbor metrics.
12. Follow [live neighbor discovery binding](../protocol/neighbor-discovery-binding.md).
    Match discovery received on the pod's actual backhaul to the independent
    capture, then inspect the observed interface identities in topology replies.
    Pause the observer and explain why topology becomes unavailable while
    control authority, radio observations and traffic remain live. Review the
    retained failed attempts and distinguish media fixtures from measurements.
13. Follow [backhaul counter accounting](../protocol/backhaul-counter-accounting.md).
    Reconcile raw packet/byte intervals with independent capture, then inspect
    the controlled egress-drop experiment. Explain why 17 lost packets can leave
    interface error/drop counters at zero, and why a veth speed constant cannot
    supply measured capacity. Reproduce the short loss probe only in the idle lab.

The real controller must supply WSC configuration, the independent manager must
apply it, and the controller must report the observed radio/BSS before client
traffic is checked. Read the retained negative trials and the bounded scope first.

**Checkpoint:** distinguish a reassembled message, a correlated discovery response,
an authenticated WSC candidate and a durable component operation. Explain why
`wsc-component` does not mean the regular service has admitted a real controller.
The complete profile/capability and controller coordinator still precede the full wire scenario. A decoded fixture inventory
is not the native controller’s managed-agent inventory.


| Question | Next guide | Additional inputs |
| --- | --- | --- |
| Did Wi-Fi clients authenticate and carry traffic after a semantic change? | [Manual §11](team-manual.md#11-run-emosa-through-ovsdb-to-hwsim-and-real-clients) | Existing radio VM, owned hwsim radios, hostapd and wpa_supplicant client containers |
| Can the native controller onboard its native agent? | [Manual §10](team-manual.md#10-prepare-and-run-native-controlleragent-onboarding) | Pinned native artifacts and separately qualified topology |
| Can we inspect our actual OpenSync extender safely? | [Read-only qualification](pod-qualification.md) | Private local connection path, authorized endpoint and existing trust |
| What completes real controller-to-EMOSA onboarding? | [First wire experiment](first-wire-experiment.md) | IEEE PDFs now obtained; remaining procedure/profile rules and exchange integration |

For capability development, work through observed topology → radio capability
inputs → technology/inventory → HE/Wi-Fi 6 guides in the [guide index](README.md).
These exercises refine claims the virtual agent can eventually make; they do
not themselves transmit a complete AP Capability Report.

## 11. Rehearse a demonstration and hand over your evidence

Use [manual §15](team-manual.md#15-deliver-a-demonstration). Start with the
architecture, state the selected evidence boundary, perform one normal change
and one fault, then inspect the result. A concise new demonstration is a two-pod
TLS run plus one retained 32-pod report. Keep VM builds outside the presentation.

Give the next operator the revision, command, private output path, reviewed
report, image/binary hashes and remaining gates. Do not send test keys, physical
credentials or raw physical captures to the public repository. The new
[secure fleet evidence](../evidence/reliability/README.md) is an example handover.
