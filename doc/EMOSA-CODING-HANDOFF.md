# EMOSA coding-agent handoff

Version 1.4 — accompanies architecture version 3.6, dated 2026-09-15.

## 1. Outcome and reading order

Build EMOSA to evaluate whether, and within which limits, a controller-side adapter can let unchanged OpenSync pods work seamlessly with an EasyMesh controller. Provide repeatable experiments, live visibility and inspectable results. A reference or independent controller sends real IEEE 1905/EasyMesh packets to the virtual agent, which maps supported operations to existing OVSDB interfaces. Simulators support development; physical pods establish actual behavior. All new code stays off the pods. No prplMesh build-time or runtime dependencies.

Read this handoff first, then architecture Sections 1–4, 7, 9–10, 16, 18–19 and 21. Consult the remaining sections while implementing their components. Files supplied with this handoff:

- `minimal-easymesh-architecture-requirements.md`: authoritative architecture and behavioral requirements, version 3.6.
- `emosa-input-manifest.example.json`: honest inventory of known and missing inputs. Copy to a local manifest; never commit secrets.

The architecture is ready for staged implementation. It does not contain the normative EasyMesh/WSC specifications, actual pod connection credentials/schema, or an independent controller installation. Do not manufacture these facts. Missing inputs block only dependent stages.

**Priority:** evaluate the adaptation hypothesis on unchanged physical pods through the real EasyMesh and OVSDB boundaries. Seamless behavior is a result to test, not an assumption. Show successes, incompatibilities, timing limits and manual interventions honestly, and make them easy to inspect and compare across experiments. Ubuntu containers are the initial reference; Alpine size/portability work is deferred. The optional native OpenSync build must not delay a ready physical-pod experiment. See architecture Sections 1.3 and 18.6 for the acceptance questions and evaluation experience.

## 2. Concrete implementation defaults

These are selected project defaults, not claims that an environment has already been provisioned.

| Concern | Initial decision |
| --- | --- |
| Runtime | CPython 3.13; pin a supported patch version during bootstrap |
| Reference execution | Linux x86-64 guest in an LXD VM; Ubuntu 24.04 guest as the initial default |
| Inner containers | LXD daemon inside the VM manages unprivileged LXD system containers `em-controller` and `emosa`; an optional third LXD container hosts the simulated pod |
| Container base | Ubuntu 24.04 for both Python application containers; pin LXD image fingerprint and packages; qualify the pinned CPython 3.13 runtime rather than assuming the distribution's default Python version |
| Lab orchestration | VM-local `emosa-lab` runner and LXD profiles/setup scripts using `lxc`; no Docker or Compose; keep LXD administration outside application containers |
| OpenSync reference backend | Separately qualified `opensync-native` Ubuntu LXD container with pinned upstream managers, compiled C dependencies and simulated driver; not a prerequisite for physical-pod proof |
| Alpine | Optional later portability/footprint target after the main adapter proof passes |
| Packaging | One Python package, `pyproject.toml`, `src/emosa/` layout, console entry points `em-controller`, `emosa`, `emosa-lab`; use `uv` and commit its lockfile |
| Core libraries | Standard `asyncio`, `dataclasses`/`enum`, `argparse`, `json`, `sqlite3`, `logging`; add dependencies only for a concrete need |
| Data validation | Versioned JSON Schema plus one validator implementation; JSON configuration/scenarios first, YAML optional |
| Tests | `pytest`, registered markers `unit`, `ovsdb`, `wire`, `hardware`, `external`; Ruff for formatting/lint; pin tools |
| Runtime persistence | SQLite journal plus mounted private secret files; no external database service |
| Internal protocol link | Dedicated isolated Linux bridge between the two protocol endpoints; explicit interface selection; separately routed management network |
| Initial cardinality | One virtual agent, one represented pod, one designated existing AP BSS; multiple simulated pods come next |
| Initial actuation | One qualified SSID/PSK configuration through the selected provisioning procedure; preserve unrelated fields and VIF lifecycle |
| Visibility and reports | Live CLI watch/inspect; JSON results and human-readable HTML/Markdown timeline and run comparison; artifact manifest/hashes; a full graphical dashboard is optional |

Do not silently install/configure host infrastructure merely to run a unit test. First inspect the supplied repository and execution environment. Pure model tests can run without LXD, raw sockets, hardware or normative wire definitions. Record a missing Linux/container environment and continue those tests. Provide deployment instructions/scripts for the intended environment and a documented service/supervisor definition. Do not make Alpine compatibility an initial delivery gate.

Reference facts: [Python supported versions](https://devguide.python.org/versions/), [LXD VM/container creation](https://canonical.com/lxd/docs/latest/howto/instances_create/). Exact dependency versions and LXD image fingerprints are outputs of bootstrap compatibility checks; retain image exports/artifact hashes for reproduction. None has been validated for this project yet.

### Dependency decisions to resolve with small experiments

1. **OVSDB client.** First evaluate the upstream Open vSwitch Python implementation behind the session abstraction. Test schema retrieval, monitor snapshot/updates, set/map decoding, guarded transactions, reconnect, and both relevant connection directions. If synchronous, use a bounded worker. Record whether the chosen library supports listening-manager operation; a dialing-only client does not satisfy a pod-initiated deployment. Select another maintained client only with a documented reason. Do not build a custom full OVSDB stack by default.
2. **OVSDB simulator.** Use a real compatible `ovsdb-server` loaded with a pinned sanitized schema, plus a separate simulated manager. The manager, not EMOSA's adapter, changes State after accepted Config edits. Do not require a switching datapath merely to host this test database.
3. **Provisioning cryptography.** After the protocol gate is populated, verify that the selected maintained crypto library supports the exact required primitives on the selected Python/base image. Use independently checked known-answer vectors. If unavailable, block that procedure and document the dependency issue; do not replace the cryptography with a fake exchange.
4. **Native OpenSync reuse.** Run the bounded R0 experiment in architecture Section 22 after basic OVSDB functionality exists. Start from the pinned OWM native test script and OSW dummy-driver API. Keep actual managers in a separate test process/container; retain normal OVSDB access from Python. Record C harness glue, platform stubs and failed build assumptions. Native build failure does not block the simpler simulators or justify manufacturing State results.

## 3. Input gates and decision ownership

| Input | Who resolves it | Needed before |
| --- | --- | --- |
| Repository location and Linux execution access | Environment/operator; agent inspects available context | Running deployment or privileged packet tests; not pure model code |
| Exact IEEE 1905/EasyMesh/WSC editions and target procedure subset | Project owner provides accessible material or agent obtains authoritative accessible specifications and records the selection | Implementing affected wire encodings/procedures |
| Independent packet/crypto vectors | Agent derives from specifications or obtains a permitted independent reference, recording provenance | Claiming wire/provisioning validation |
| Pod model/build, endpoint direction, trust and actual schema | Operator supplies access; agent retrieves and records non-secret facts | Hardware mapping qualification |
| Managed radio/VIF, cloud/local writer controls and recovery path | Operator's lab configuration, verified by agent | Hardware writes |
| Independent controller build and evaluator environment | Project/evaluator | Third-party interoperability run |
| Exact packages/LXD image fingerprints and retained exports | Agent through compatibility experiments | Reproducible integration build |

The supplied manifest uses `not_provided`/`not_selected`, not fictitious values. `qualified` is an evidence-backed status: validate referenced artifacts and current configuration, rather than trusting a boolean. An upstream schema is a simulation reference, not the actual pod's fingerprint.

Create `docs/decisions.md` for implementation choices and `docs/open-inputs.md` for external gaps. State the affected tasks and proceed with independent work. Ask only for genuinely missing facts that cannot be inspected or responsibly resolved. Do not ask the user to choose ordinary function names, JSON libraries or test directory layouts.

## 4. Ordered implementation tasks

Each task produces working code and focused evidence. The task IDs below are implementation order, distinct from architecture acceptance-test IDs.

| Task | Deliverable and dependencies | Exit condition |
| --- | --- | --- |
| I0: bootstrap | Inspect repo; package layout, entry points, lockfile, CI/check commands, decision/input/traceability files | Clean install; CLI help; JSON contract validation; pure tests run without privileged resources |
| I1: domain and model | Typed records, operation transition guards, idempotency, SQLite journal, deterministic model; depends I0 | Delayed/rejected/no-op/conflict/lost-outcome/deadline/restart tests; no desired-to-observed shortcut |
| I2: OVSDB adapter and simulation | Compatible library experiment, real test database, manager simulator, initial snapshot/references, guarded patch, reconciliation; depends I1 | One direct semantic component scenario changes Config and independently converges State; lost reply and competing-writer cases pass; record that no wire procedure was tested |
| R0: OpenSync reference experiment | Pinned native build and dummy-driver harness; depends I2 and a Linux build environment | N01–N04, or specific build/target blockers; add `opensync-native` only when qualified |
| I3: packet endpoint | Frozen P0 matrix, bounded framing/TLV/reassembly, independent vectors, controller/agent discovery and selected capability procedures; depends I0 plus P0 | Selected W01–W03/W05 evidence with real captures; unsupported mandatory data is visible |
| I4: real provisioning | Genuine WSC procedure and semantic binding; depends I2, I3 and crypto compatibility | Wire-driven simulation passes valid provisioning, invalid authentication, duplicate/retry and lost-reply cases; full request scope is respected |
| I5: physical pod | Read-only qualification then one wired BSS change, client check and reconnect; then wireless management; depends I4 plus M0 | W06 and applicable A-tests supported by real-pod/client evidence; two-pod cases remain pending until hardware exists |
| I6: evaluation release | Multi-pod isolation, parameterized scenarios, bounded fault injection, live watch/inspect, timelines, run comparison, packaging and coverage; incremental from I1 onward | Selected A/W/E matrices including E07–E08; gaps visible; clean-environment rerun works; platform completion is separate from interoperability verdict |
| I7: independent evaluation | External-controller attachment and evaluator package; depends I6 plus X1 | T01–T05 for the declared independent peer and scope; no universal compatibility claim |

I2 and I3 may be developed independently once their prerequisites exist. Start with I0–I2 while protocol inputs are missing; do not invent a private protocol and present it as I3/I4. Missing hardware or an external peer does not block simulator/package development. R0 is a bounded evaluation of direct upstream reuse, not a requirement to port the entire OpenSync stack before implementing the wire path.

For the first runnable demonstration, deliver the smallest integrated scenario: one controller, one virtual agent, one simulated pod, one existing BSS, valid provisioning, then a deliberately lost reply and recovery. Follow with the same path against one physical pod. A simulator-only result is valuable but is not the completed hardware milestone.

## 5. Contracts and error behavior to implement first

Architecture Section 21.2 is authoritative for operation lifecycle, idempotency, deadlines, late evidence, recovery attribution and cancellation. Section 21.3 defines record and local API fields. Implement a transition table with tested guards before adding asynchronous side effects.

Create the following repository files early:

```text
schemas/config.schema.json
schemas/scenario.schema.json
schemas/local-api.schema.json
schemas/run-result.schema.json
docs/protocol-matrix.json
docs/supported-pods.json
docs/traceability.json
docs/decisions.md
docs/open-inputs.md
tests/fixtures/model/              # explicitly synthetic, fixed identities
tests/fixtures/opensync/           # schema provenance and sanitized observations
tests/fixtures/protocol/           # independently sourced/derived vectors
scenarios/component-bss-change.json
scenarios/provision-one-bss.json
scenarios/lost-reply.json
```

Do not fabricate a complete protocol fixture directory while P0 is unresolved. Keep missing scenarios/vectors listed as pending, with loader preconditions preventing execution as a purported success.

Each mapping entry links a semantic operation to qualified config fields, transaction guards and fresh observed-state predicates. Each protocol entry links specification sections and message/TLV rules to implementation and tests. Each acceptance row links to a test and captured evidence, with its mode (`model`, `ovsdb-sim`, `opensync-native`, `hardware`, external peer) recorded.

For local diagnostics use stable error reasons such as `INVALID_INPUT`, `UNSUPPORTED_OPERATION`, `SCHEMA_MISMATCH`, `NOT_READY`, `OWNERSHIP_CONFLICT`, `PRECONDITION_FAILED`, `APPLY_TIMEOUT`, `OUTCOME_UNKNOWN`, and `MISSING_PREREQUISITE`. These are internal API reasons, not invented EasyMesh error values. A wire handler maps only to errors actually supported by its selected specification.

### Small initial resource policy

Use configurable limits and test exhaustion behavior. Start with one modifying operation in flight per pod, at most 16 outstanding OVSDB requests per session, and finite per-pod queues. Reject excess modifying requests with an explicit busy result. Never silently discard monitor updates: if backpressure loses observation continuity, mark the cache unready and resynchronize before further writes.

Measure maximum valid initial snapshots before pinning a decoded-message limit. Fragment sizes/counts/timers come from the protocol matrix; do not turn an arbitrary resource budget into a conflicting protocol rule. A 30-second simulated apply deadline is a test scenario choice, not a universal Wi-Fi or EasyMesh timer.

## 6. Reproducible commands and checks

Provide these developer commands after implementation; they are a target interface, not a claim that code already exists:

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest -m unit
uv run pytest -m ovsdb
uv run pytest -m wire
emosa-lab run scenarios/provision-one-bss.json --backend ovsdb-sim
emosa-lab report RUN_ID --format json
emosa-lab watch RUN_ID
emosa-lab inspect RUN_ID --operation OPERATION_ID
emosa-lab compare RUN_A RUN_B --format html
```

Resolve and commit the lockfile during I0 before `--frozen` is expected to work. Integration commands invoke the supplied LXD profiles/setup scripts; document exactly how. The VM-local runner executes endpoint commands with `lxc exec em-controller -- ...` or `lxc exec emosa -- ...` against the inner LXD daemon. The runner itself may be executed through `uv run` or from an installed entry point. Separate optional `hardware` and `external` jobs with explicit target profiles; default CI must not discover or modify real pods.

Selected suites fail when a required prerequisite is absent, or report a blocked run explicitly at the runner layer; they must not pass because zero tests executed. Ordinary unit CI can exclude hardware tests. A run report records the selection and all omitted coverage. Prefer deterministic event/fault triggers to arbitrary sleeps.

At each increment run tests relevant to the changes and the affected integration boundary. Preserve independent expected bytes/results; testing an encoder only by decoding its own output is insufficient. Do not repeat broad suites after no code changed merely to create more output.

## 7. Completion and reporting

Each delivery includes:

- Changed behavior and scope, implementation task IDs and requirement/test links.
- Reproducible commands and actual results, including unexecuted checks and why.
- Artifact location and evidence manifest; no fabricated pcaps, pod schemas or hardware output.
- Current blockers, decisions and next unblocked task.
- No accidental claim of full EasyMesh compliance, real-pod support or third-party qualification from a lower test level.

Do not stop at empty interfaces, generated fixtures or CLI help when substantive work is unblocked. Do not expand into MQTT, steering, radio control, a GUI or production HA to avoid completing the first end-to-end scenario.

## 8. Ready-to-paste assignment

```text
Implement EMOSA using EMOSA-CODING-HANDOFF.md and architecture version 3.6
in minimal-easymesh-architecture-requirements.md. Inspect the repository
and execution environment first, then implement the ordered I0–I7 tasks
as far as their actual prerequisites permit. Keep the implementation
in Python, with no prplMesh dependencies or new software on physical pods.
The primary objective is to evaluate whether unchanged OpenSync pods work
seamlessly with an EasyMesh controller through the adapter. Treat this as
a hypothesis: expose compatibility gaps and negative results as well as
success. Live visibility, controlled experiments, correlated evidence and
run comparison are required delivery features.
The separate OpenSync reference fixture may compile pinned upstream C
components and minimal lab-only driver glue as specified in Section 22.
Use an LXD VM with inner LXD-managed Ubuntu containers for em-controller
and emosa. The optional native OpenSync fixture uses a separate Ubuntu
LXD container. Do not substitute Docker/Compose for this layout.
Defer Alpine footprint/portability work until the adapter proof succeeds.
Prioritize the real-pod test when its prerequisites are available; do not
make completion of the optional native OpenSync build a hardware-test gate.

Begin with a working package, typed contracts, durable operation state,
model tests and a real OVSDB-backed simulator. Evaluate pinned OpenSync
native-manager reuse through the bounded R0 experiment without making a
full OpenSync port a prerequisite for the basic simulator. The final evaluation path
must use real IEEE 1905/EasyMesh frames into EMOSA virtual agents. A direct
semantic simulation is an intermediate component test only.

Copy and complete the input manifest from verified evidence. Record
ordinary implementation decisions yourself. If protocol specifications,
hardware inputs or an independent peer are unavailable, mark only those
dependent tasks blocked and continue other useful implementation work.
Never invent protocol encodings, crypto, capabilities or hardware results.

Implement the first narrow provisioning-and-recovery scenario before
adding extensions. Run meaningful checks, preserve evidence, and maintain
requirement-to-test traceability. Deliver working code, reproducible
commands, actual test results and a precise account of remaining gates.
Do not mark the whole project complete based on scaffold or simulation
results alone, and do not change real pods without the qualified target
and ownership profile described in the design.
```
