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

**Checkpoint:** distinguish a reassembled message, a correlated discovery response,
an authenticated WSC candidate and an admitted operation. The new components
stop before operation creation. The complete profile/capability and controller
coordinator still precede the full wire scenario. A decoded fixture inventory
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
