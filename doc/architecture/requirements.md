# EMOSA — EasyMesh to OpenSync Adapter

## Evaluation Platform: Architecture, Design, and Implementation Requirements

| Field | Decision |
| --- | --- |
| Document version | 3.6 |
| Date | 2026-09-15 |
| Status | Ready for staged implementation; protocol, hardware and independent-peer gates remain explicit |
| Project | EMOSA — EasyMesh to OpenSync Adapter |
| Primary purpose | Evaluate whether controller-side adaptation lets unchanged OpenSync pods work seamlessly with an EasyMesh controller; expose results, limitations and repeatable experiments |
| Managed devices | Existing OpenSync 6.6.0 pods only |
| Planned hardware access | Physical pods reachable over wired or wireless management paths |
| Device changes | No firmware changes, new services, packages, scripts, or protocol agents on pods |
| Confirmed access | Direct authorized OVSDB access is available |
| Confirmed ownership mechanism | Existing cloud configuration writers can be disabled or redirected |
| Adaptation location | Entirely within the new controller |
| Recommended language | Python for all new controller components |
| Deployment baseline | LXD VM containing two Ubuntu LXD system containers; inner LXD runs in the VM; Alpine is a later portability target |
| Runtime dependency restriction | No prplMesh dependencies |
| OpenSync core reuse | Pinned schema/test assets immediately; separately qualified native-manager simulation profile |
| Required evaluation front end | Real IEEE 1905.1/EasyMesh messages between a reference or independent EasyMesh controller and EMOSA virtual agents |
| Core evaluation experience | Controlled experiments, live visibility, correlated evidence, comparable run results and explicit compatibility gaps |
| Pod-facing protocol | Existing pod OVSDB management interface |
| Fast development path | Direct semantic API for component tests; insufficient for end-to-end acceptance |
| Independent evaluation | Reproducible evaluator package and replaceable test-controller endpoint; independent wire-peer results required for claimed interoperability |

Naming clarified on 2026-09-16: EMOSA means **EasyMesh to OpenSync Adapter**.
OVSDB is the selected OpenSync management interface, not the expansion of EMOSA.

**Primary objective:** evaluate whether, and within which limits, a controller-side adaptation layer can make unchanged OpenSync pods work seamlessly with an EasyMesh controller. The platform must let an evaluator observe the whole interaction, change experiment parameters, reproduce results, and identify why an operation succeeds or fails. Seamless interoperability is the hypothesis under test, not an assumed outcome.

EMOSA combines real protocol exchanges, controlled simulations and physical-pod experiments. Normal configuration changes through the pods' existing OVSDB interface remain in scope; pod firmware and software remain unchanged. Successful delivery of the evaluation platform means it produces trustworthy, inspectable results, including evidence of incompatibility. A passing interoperability claim additionally requires the selected procedures to meet their declared acceptance criteria. Production suitability remains a separate qualification outcome.

### Reading guide

- Sections 1–4: architectural decisions, language, deployment, and software boundaries.
- Sections 5–10: device model, ownership, adapter contract, OVSDB mappings, transactions, and recovery.
- Sections 11–13: CLI, configuration, security, observability, and limits.
- Sections 14–15: acceptance tests and implementation milestones.
- Section 16: required EasyMesh wire interface, virtual agents, protocol scope and acceptance.
- Section 17: sources and verification boundaries.
- Section 18: EMOSA evaluation modes, scenario contract, evidence, and reporting.
- Section 19: third-party evaluation contract and independent controller tests.
- Section 20: assessment of the supplied controller/cloud-adapter proposal.
- Section 21: coding-agent handoff, contract clarifications and readiness gates. Start with the companion `../project/EMOSA-CODING-HANDOFF.md`.
- Section 22: direct reuse of OpenSync core as a separate reference backend and its qualification experiment.

## 1. Executive decision and meaning of EasyMesh

Build EMOSA in Python as a controller-side EasyMesh virtual-agent service with an OpenSync OVSDB backend. A reference or independent EasyMesh controller sends real IEEE 1905/EasyMesh messages to EMOSA over the isolated lab Ethernet link. A small included controller is a development tool, and must be replaceable by the independent controller being evaluated. EMOSA terminates the selected agent procedures, translates supported operations into existing OVSDB operations, and monitors the pods' published state to determine what actually happened.

```text
Scenario runner / controller CLI
          ▼
EasyMesh controller (reference or independent)
          │ real IEEE 1905 / EasyMesh frames inside the VM
          ▼
EMOSA virtual agent (one logical identity per represented pod)
          │ internal semantic operations and observations
          ▼
EMOSA OpenSync adapter
          │ existing OVSDB sessions
          ▼
Unchanged OpenSync pods
          │ existing managers and drivers
          ▼
Actual radios, BSSs, and clients
```

The virtual agent is an actual protocol endpoint inside the controller environment. The physical pod remains an OpenSync device managed through OVSDB. EasyMesh discovery, CMDUs (control message data units), and supported WSC provisioning terminate at EMOSA; physical pods do not transmit these exchanges. Representing a pod through a proxy does not establish native EasyMesh operation or certification of that pod.

This distinction determines the implementation:

- Exercise the real packet path for every claimed end-to-end EasyMesh procedure.
- Keep packet handling, semantic operations, OVSDB mapping, and pod application as separately observable stages.
- Use the same adapter against simulated OVSDB devices and physical OpenSync pods.
- Keep all virtual-agent and adaptation code in the controller environment, with no pod software changes.

**Confirmed architecture:** the user selected real EasyMesh messages through the adapter as the main evaluation path. Direct semantic API tests remain useful for isolating bugs and running fast simulations, but cannot substitute for wire-path acceptance. The protocol subset must be frozen against a named specification revision before its implementation; Section 16 defines that gate.

### 1.1 Requirements language

**MUST** and **MUST NOT** are mandatory project requirements. **SHOULD** allows a documented tradeoff. These words do not imply that the project requirement is mandated by an external standard.

**ARCH-01.** All adaptation code MUST run in the controller environment. It MUST NOT require installing or executing a new agent/helper on a pod.

**ARCH-02.** The physical-pod backend MUST use the already available OVSDB interface and supported existing behavior. An operation requiring an unavailable device hook MUST be reported as unsupported.

**ARCH-03.** The public device model MUST identify its source as OpenSync and its management transport as OVSDB. It MUST NOT advertise these pods as certified or natively interoperable EasyMesh agents.

**ARCH-04.** The controller MUST have zero prplMesh build-time and runtime dependencies. It may use a maintained OVSDB client library and ordinary Python dependencies.

**ARCH-05.** Main evaluation scenarios MUST traverse the test controller, real IEEE 1905/EasyMesh frames, EMOSA virtual-agent procedure handling, and the selected backend. Record component-test results separately from end-to-end results.

### 1.2 Core scope

| Capability | First release | Later extension |
| --- | --- | --- |
| Pod inventory and session health | Required | More pod models/site scale |
| Radio/BSS/client observation | Required where exposed by the qualified schema | Additional existing telemetry sources |
| Capability/operation availability matrix | Required | More operations per supported build |
| Live experiment visibility and correlated result reports | Required | Richer dashboards/visual analysis |
| Parameterized reruns and comparisons | Required | Larger sweeps and automated regression campaigns |
| Configure one designated existing fronthaul BSS per qualified pod | Required | Additional BSSs and qualified lifecycle operations |
| Desired-versus-observed reconciliation | Required | Broader policies |
| Transaction journal and reconnect/restart recovery | Required | Multi-controller failover |
| Channel/power changes | Observe in core; actuate only after shared-radio qualification | Qualified radio control |
| Steering/scans/detailed metrics | Explicitly unsupported unless an existing usable interface is qualified | Add independently, with real results |
| Pod firmware changes or new pod software | Excluded | Excluded under this architecture |
| Native EasyMesh southbound traffic to pods | Excluded | Excluded under this architecture |
| Real EasyMesh packets inside the VM | Required for the selected procedure subset | Additional qualified procedures and independent controller interoperability |

The initial scope is a small evaluation platform and management adapter. Removing a cloud Wi-Fi writer does not prove unrelated onboarding, certificate, telemetry, upgrade, or connectivity functions can also be removed. The reproducible scenario runner in Section 18 is part of the evaluation deliverable, rather than an optional production feature.

### 1.3 Primary objective and meaning of seamless operation

The primary result is evidence showing whether a defined EasyMesh procedure causes the correct action on an unchanged physical OpenSync pod through EMOSA, and where the adaptation falls short. Container size and distribution portability are secondary. Use Ubuntu LXD containers for the initial evaluation to keep one reference distribution across the Python services and optional native OpenSync fixture. Alpine remains a later measured portability/footprint exercise.

For each declared controller/build, procedure and pod/build combination, evaluate these dimensions of seamless operation:

| Dimension | Acceptance question |
| --- | --- |
| Controller workflow | Can the controller use its ordinary supported EasyMesh procedure without adapter-specific protocol patches or an internal-API shortcut? |
| Pod compatibility | Does the unchanged pod apply the requested behavior through existing authorized management interfaces? |
| Semantic accuracy | Do capabilities, responses, state and error reports mean what the selected procedure requires, with no fabricated success? |
| Operational continuity | Do duplicate requests, disconnects and restarts recover according to the declared scope, without undocumented per-operation repair? |
| Timing and observability | Are protocol deadlines and declared application/service-interruption budgets met, and can the evaluator follow the evidence? |

Initial setup may establish existing endpoint configuration, trust, resource bindings and writer ownership. Record that setup as a deployment prerequisite. Manual intervention during an operation or recovery is a reported limitation, not invisible evidence of seamlessness. "As is" refers to unchanged firmware/software; the intended managed Wi-Fi configuration is allowed to change.

The first proof package must contain:

1. A captured real EasyMesh procedure into EMOSA with the selected request independently interpretable against the protocol matrix.
2. A correlated, guarded OVSDB transaction that changes only the qualified fields on the designated existing BSS.
3. Fresh pod-reported state and a separate Wi-Fi client's verification of the intended behavior.
4. An invalid/unsupported request that produces no unintended write, plus a lost-connection/recovery case that preserves uncertainty correctly.
5. A repeat of the claimed procedure with an independent controller before claiming third-party interoperability.

The report names the procedure, EMOSA/controller builds, pod firmware/schema and topology, and answers each dimension above with pass/fail/unknown evidence. One passing provisioning flow establishes that specific mapping, not every EasyMesh procedure or full profile compliance. The physical-pod test is the decisive target; an optional native OpenSync simulator improves diagnosis and regression coverage but must not delay that test once its prerequisites are available. A well-evidenced negative result is useful evaluation output; it does not become a passing adapter result.

## 2. Language decision: Python versus C

**Use Python for the new controller and adapter.** The firmware constraint that previously favored a C pod agent no longer applies because no new code runs on the pods.

This is an engineering judgment for the stated workload, not a measured benchmark:

| Concern | Python | C | Decision |
| --- | --- | --- | --- |
| OVSDB and JSON | Natural fit for structured messages, maps, sets, and state normalization | More parsing, ownership, and serialization work | Python |
| Event-driven sessions | `asyncio` plus a compatible client library | Native event loop and callbacks | Python is simpler for this controller |
| Reconciliation and policy | Readable models and state machines | More lifecycle and memory-management code | Python |
| Runtime footprint | Larger interpreter/runtime | Smaller native process possible | VM resources make this a secondary concern |
| Pod integration | Uses the existing remote management interface | No firmware-library benefit when running remotely | No present reason to choose C |
| Performance | Expected to suit small control-plane workloads; qualify with tests | Greater low-level control | Measure before optimizing |
| Long-term changes | Fast schema/mapping/test iteration | Useful if a mandatory native dependency appears | Python remains the default |

**LANG-01.** Use CPython 3.13 for the initial implementation, with an exact supported patch release pinned during bootstrap. Keep CLI, domain model, reconciliation, and OpenSync mapping in Python. A compatibility-driven change of Python minor release requires a recorded technical decision and updated lock/test evidence, not a redesign of the architecture.

**LANG-02.** Prefer a maintained OVSDB library compatible with the deployed servers, transport direction, TLS configuration, and event loop. Qualify its handling of partial updates, reconnects, JSON value types, and feature negotiation. The design does not mandate a particular package before this compatibility test.

**LANG-03.** If the available library is synchronous, isolate it behind a bounded worker/queue or its supported event-loop integration. Do not let a blocked socket call stall all pods.

**LANG-04.** Introduce a native extension or C component only for a measured bottleneck or mandatory dependency. OVSDB translation alone is not a reason to rewrite the controller in C.

The separately qualified OpenSync reference backend in Section 22 may compile upstream C components and a minimal lab-only driver harness. This does not change the Python implementation of EMOSA or install software on physical pods. Keep it in its own process/container rather than making the adapter call OpenSync internals directly.

Python remains the default with the required packet front end. Use ordinary binary parsing for bounded CMDU/TLV messages, Linux packet sockets for Ethernet, and a maintained cryptographic library supporting the exact primitives required by the selected provisioning procedure. Do not implement cryptographic primitives from scratch. Verify dependencies on the initial Ubuntu reference images; qualify Alpine separately if portability work is selected. Native dependencies in standard libraries do not create a prplMesh dependency. C is not inherently easier for the protocol state machines, OVSDB mapping or evaluation tooling.

## 3. Deployment and connection topology

### 3.1 Reference evaluation deployment

```text
LXD host
└── Ubuntu Linux VM: emosa-lab
    ├── inner LXD daemon + VM-local scenario runner
    ├── em-controller: Ubuntu LXD container, Python controller + CLI
    │             │
    │       isolated Ethernet bridge: IEEE 1905 / EasyMesh
    │             │
    └── emosa: Ubuntu LXD container, Python virtual-agent service + CLI
          ├── per-pod protocol identities and procedure state
          ├── semantic model / ownership / operation journal
          └── OpenSync adapter and per-pod OVSDB sessions
                         │ management IP interface
                         ├── simulated OVSDB pod + manager (simulation mode)
                         └── wired or wireless route → unchanged physical pods
```

The two principal containers are Ubuntu LXD system containers managed by an LXD daemon inside the outer VM. EMOSA supplies the virtual agent role. Docker and Compose are not part of the reference deployment; selecting LXD preserves the proposed lab environment without adding another orchestration system. An OVSDB simulator may run as a separate managed process or a third disposable LXD container. The optional native OpenSync reference backend also uses a separate Ubuntu LXD container. Hardware mode replaces the simulated backend with physical pods. The internal protocol bridge and pod management network are distinct; the physical pods need no connection to the internal bridge.

The VM-local runner uses `lxc exec` against the inner LXD daemon to invoke the endpoint CLIs. Keep LXD administration on the VM; do not mount its administrative socket into application containers. Pin and retain LXD image fingerprints/exports plus package versions. Use Ubuntu 24.04 as the initial guest/container release, subject to explicit dependency qualification. Alpine portability is a separate later test, not a prerequisite for adapter proof. This is the reference layout to provision, not a claim that it has already been created.

**DEP-01.** The test controller and EMOSA wire endpoints MUST share the isolated protocol bridge with verified multicast/unicast delivery. The pod-facing path requires authorized OVSDB reachability and MUST NOT require IEEE 1905 forwarding or a shared Ethernet segment with the physical pods.

**DEP-02.** Qualify how the existing deployment establishes OVSDB sessions: the controller may connect to a listening endpoint, or a pod's existing OVSDB server may establish the configured manager connection. Implement the supported deployment mode; do not assume every pod exposes a TCP listener.

**DEP-03.** Existing trust, identity, certificates, endpoint settings, and credentials MUST remain part of connection qualification. Direct access being available does not imply a specific port, TLS mode, or unrestricted table permissions.

**DEP-04.** Pin images and dependencies. Grant `CAP_NET_RAW` only to processes that open protocol packet sockets. Configure bridges/interfaces in deployment setup; runtime services SHOULD NOT require `CAP_NET_ADMIN` or privileged-container mode. Do not forward the isolated EasyMesh bridge onto the physical pod network.

**DEP-05.** Keep persistent controller data outside the disposable image. The reference deployment uses a mounted state directory for the SQLite operation journal, inventory identity bindings, desired configuration, and migration/version metadata. Store credentials separately.

**DEP-06.** Start with one controller instance and one qualified pod model. Scale to multiple pods with independent sessions and namespaces. Two independent controller processes MUST NOT write the same managed resources concurrently.

### 3.2 Physical-pod evaluation over wired and wireless paths

The planned physical pods provide the hardware validation target. Record which of these topologies each run actually uses:

| Topology | What it tests | Setup detail to record |
| --- | --- | --- |
| Wired pod management | Baseline mapping, application, and recovery with an independent management path | Controller route, pod uplink, designated test BSS, and existing writer controls |
| Pod reached through wireless backhaul | Management and recovery when a pod's path includes its existing wireless uplink | Upstream pod/AP, backhaul radio/VIF if observable, channel, and dependencies shared with the test BSS |
| Controller host reaches a pod as a Wi-Fi client | Management through the client's association with that pod | Host association/BSSID and whether the target operation changes that same BSS |

These are different experiments. A Wi-Fi-connected controller does not by itself demonstrate a working multi-pod wireless backhaul. Reachability is insufficient until the authorized OVSDB session, schema retrieval, and monitor snapshot succeed.

The controller VM needs a working management route. When the host or gateway supplies that route, a Wi-Fi adapter inside the VM is unnecessary. Use a separate Wi-Fi test client to check the real BSS. Begin with wired management and a designated existing fronthaul BSS, then repeat supported scenarios over the wireless path available on the pods.

**DEP-07.** Every hardware run MUST record the actual management path and whether its target BSS/radio carries that path. Do not infer physical topology from OVSDB connectivity alone.

**DEP-08.** Changes that can break the management path MUST use an explicit disruptive-test scenario and a documented recovery method available with unchanged pods, such as reconnecting Ethernet or using existing local controls. A controller cannot promise remote rollback over a connection that its change has removed.

**DEP-09.** Qualify wired and wireless operation separately. Automatic failover between them is outside the initial guarantee and requires its own test if later claimed. A test of wireless-only management MUST verify that a connected recovery cable is not silently carrying management traffic.

### 3.3 OVSDB roles, endpoint direction and existing cloud functions

The database being managed resides on the pod. EMOSA is the management client issuing OVSDB requests to that database. A pod's OVSDB server can establish an outbound connection to a listening manager; accepting that TCP connection does not make EMOSA the server hosting the pod's database. Open vSwitch supports both connection directions. See [OVSDB connection methods](https://docs.openvswitch.org/en/stable/ref/ovsdb.7/#connection-methods).

**DEP-10.** Model socket direction and database role separately. Support the qualified listening-manager or dialing-client mode without introducing a second authoritative pod database on the controller. A controller-hosted OVSDB server belongs to the simulated-pod backend unless a separate, explicitly scoped replication design is needed.

OpenSync documents redirector/manager configuration and separate MQTT telemetry configuration. Its deployment requirements also describe a cloud arrangement using port 443, so the pasted proposal's ports 6640, 1883 and 8883 are not universal deployment facts. See the [OpenSync FAQ](https://opensync.atlassian.net/wiki/spaces/OCC/pages/39920140758) and [connection requirements](https://opensync.atlassian.net/wiki/spaces/OCC/pages/39920140311).

**DEP-11.** Use the existing authorized OVSDB path confirmed for this project. Record the actual endpoint, connection direction, TLS peer identity, trust material and reconnect behavior. Do not make DHCP options or DNS overrides mandatory. If existing management settings are redirected, qualify their persistence and authentication using the pod's available controls. Changing a hostname or IP alone does not establish the required trust.

**DEP-12.** Inventory which existing cloud functions remain necessary for the test pods, including initial onboarding, endpoint provisioning, certificates and connectivity recovery. Disable/redirect Wi-Fi writers within the established ownership scope. EMOSA does not automatically replace every cloud function merely because configuration transactions succeed.

## 4. Software architecture

### 4.1 Module boundaries

```text
src/emosa/
  app.py                    # startup, shutdown, dependency wiring
  config.py                 # strict versioned config
  cli.py                    # command-line client and service entry point
  local_api.py              # local command/result interface
  model.py                  # normalized device/radio/BSS/client objects
  inventory.py              # authenticated session-to-pod identity binding
  capabilities.py           # operation availability and source evidence
  ownership.py              # managed resources, fields, writer policy
  operations.py             # lifecycle, deadlines, idempotency
  reconcile.py              # desired/committed/observed comparison
  store.py                  # durable operation journal and desired state
  events.py                 # logs, counters, bounded event history
  backends/base.py           # semantic backend contract
  backends/mock.py           # deterministic controller-side test backend
  opensync/session.py        # OVSDB protocol/transport adapter
  opensync/schema.py         # negotiated schema and value normalization
  opensync/mapping.py        # OpenSync/domain translations
  opensync/transactions.py   # guarded, minimal column updates
  opensync/observation.py    # monitors, references, snapshot generations
  ieee1905/ethernet.py       # bound Linux packet sockets and frame filtering
  ieee1905/cmdu.py           # framing, fragmentation and bounded reassembly
  ieee1905/tlv.py            # validated typed TLV codecs
  easymesh/controller.py    # minimal test-controller procedure engine
  easymesh/agent.py         # virtual-agent procedure engine and pod binding
  easymesh/procedures/      # selected discovery, capability and provisioning
  easymesh/provisioning.py  # selected WSC procedure and library integration
  evaluation/scenario.py    # versioned scenarios and preconditions
  evaluation/runner.py      # execution, fault schedule, cleanup tracking
  evaluation/checks.py      # independent expected-result predicates
  evaluation/evidence.py    # run manifest, redaction, reports
  simulation/pod.py         # independent config-to-state device model
  simulation/faults.py      # deterministic controller-side failure injection
  telemetry/               # optional qualified MQTT/Protobuf input, not core
tests/
  unit/
  ovsdb_protocol/
  reconciliation/
  integration/
  fixtures/                 # sanitized schemas, snapshots, event streams
scenarios/                  # reusable simulation and hardware scenarios
deploy/
doc/
  README.md                 # documentation index
  guides/                   # setup, operation, qualification and demos
  architecture/             # requirements, mappings, ownership and recovery
  protocol/                 # specification inputs, matrix and WSC components
  evaluation/               # experiment findings and supported-pods.json
  project/                  # handoff, status, open inputs and traceability
  evidence/                 # reviewed artifacts and original hashes
```

**SW-01.** Use one long-lived test-controller process and one long-lived EMOSA service with per-pod protocol/session objects and bounded queues. Each CLI is a short-lived local client. Simulator and external client probes are separate test components.

**SW-02.** Keep OVSDB row representations out of policy and CLI handlers. Policy operates on normalized semantic objects; only the adapter knows table/column encodings and row UUIDs.

**SW-03.** Keep packet/procedure handling, OVSDB codec/session, schema mapping, operation/reconciliation engine, and user commands independently testable. A parsed update MUST NOT directly execute another configuration write.

**SW-04.** Use monotonic clocks for deadlines and an injectable clock for tests. Persist wall-clock timestamps for audit; do not persist monotonic timestamps across controller restarts.

**SW-05.** The mock backend MUST support delayed application, rejection, partial state updates, disconnects, and conflicts. It cannot make every accepted write immediately succeed.

## 5. Device identity, inventory, and observed state

### 5.1 Normalized model

| Object | Required fields and interpretation |
| --- | --- |
| Pod | Stable controller `pod_id`, trusted endpoint/session binding, reported hardware/software identity, connection state, schema/build fingerprint |
| Radio | Stable logical `radio_id`, actual interface/radio identifiers, band, observed channel/width/power, constraints and available operations |
| BSS | Stable controller `bss_id`, owning radio, current VIF reference, observed BSSID, SSID, mode and operational state |
| Client | MAC, observed BSS membership, available attributes, source timestamp and freshness; no invented association frame |
| Capability | `supported`, `unsupported`, `unknown`, or `temporarily_unavailable`, with reason and evidence |
| Observation | Source pod/session generation, receive time, source time where available, data provenance, validity/stale state |
| Desired configuration | Operation-owned target fields, schema mapping version, and authorized resource scope |

**MODEL-01.** Bind the configured pod ID to an authenticated management endpoint/session. Treat self-reported serial/model fields as cross-checks, not sufficient authentication by themselves.

**MODEL-02.** Keep controller IDs independent of IP addresses and OVSDB UUIDs. IP addresses may change and rows may be recreated. On each database/session generation rebuild UUID references and validate the mapping to the stable device/radio/VIF inventory.

**MODEL-03.** Reconstruct radio/VIF/client relationships using schema references and qualified identifiers. Do not assume interface-name order or a VIF's numeric suffix determines its radio.

**MODEL-04.** Preserve the difference between missing, empty, unknown, stale, and false/zero. A disconnected pod retains its last observation marked stale; its last known Wi-Fi state is not presented as fresh.

**MODEL-05.** Never copy desired configuration into observed state to make an operation look successful. Hardware-mode objects MUST NOT use simulated metrics or fixture capability bits.

**MODEL-06.** Topology views MUST distinguish reported uplink relationships from inferred relationships and unknown links. Remote management connectivity does not prove physical neighbor adjacency.

**MODEL-07.** Capability exposure is the intersection of available remote interfaces, qualified mapping, actual hardware support, and ownership scope. An OVSDB column's existence alone does not establish a usable operation.

## 6. Configuration ownership with unchanged firmware

The confirmed goal is for the new controller to own the managed Wi-Fi settings. The confirmed mechanism is that existing cloud writers can be disabled or redirected. No new ownership enforcement code may be installed on a pod.

### 6.1 Ownership contract

**OWN-01.** Maintain an explicit allowlist of managed pods, radios/VIFs, and fields. The first writable scope is one designated existing fronthaul AP VIF per qualified pod. It covers supported SSID/security/enable settings, not all columns in the Wi-Fi tables.

**OWN-02.** Before writable rollout, disable or redirect the existing cloud policy writers for those settings through the already supported management mechanism. Record the configuration and verify that it remains effective after cloud reconnect and pod reboot. This does not require disabling unrelated cloud services.

**OWN-03.** Inventory local automatic writers and hardware policy that can change the same resources: channel selection, steering, configuration restoration, uplink handling, and manager behavior. Use only existing supported controls to establish ownership. If a conflicting local behavior cannot be disabled or arbitrated without firmware changes, keep that operation outside the writable scope.

**OWN-04.** OVSDB locks do not force unrelated clients to obey field ownership. They may coordinate cooperating controller instances, but MUST NOT be used as proof that legacy cloud/local writers are excluded. See [OVSDB lock semantics](https://www.rfc-editor.org/rfc/rfc7047.html#section-4.1.8).

**OWN-05.** Detect unexpected changes to owned desired fields and classify their source/reason where possible. Do not repeatedly overwrite competing values. Mark the resource `ownership_conflict`, stop further conflicting writes, and expose the evidence through the CLI.

**OWN-06.** Local regulatory, DFS/radar, thermal, and hardware safety decisions remain authoritative. A deviation caused by those constraints is an application/constraint outcome, not automatically an ownership violation.

**OWN-07.** Radio channel/width/power affect all VIFs on the radio, potentially including wireless backhaul. A controller owning one fronthaul VIF cannot treat these as isolated BSS settings. Radio-wide actuation requires a separately qualified impact/ownership policy.

**OWN-08.** Loss of controller connectivity MUST NOT trigger blind restoration of an old configuration. The new controller stops issuing writes and reports degraded/stale state. Test what the unchanged pod/cloud actually does during that outage; do not promise hold-last behavior until it is observed and qualified.

**OWN-09.** Returning control to the prior management system is an explicit operational handover: quiesce controller writes, reconcile in-flight operations, and restore the supported cloud/management settings deliberately. Do not erase current pod configuration as a shutdown action.

### 6.2 Concurrency in the new controller

Core supports one active controller writer. Acquire a process/service lock for its state directory and reject accidental duplicate local instances. A second instance on another host requires external fencing/leadership that all participating new controllers honor; high availability is outside core. This mechanism still does not replace the cloud/local ownership qualification above.

## 7. Semantic adapter contract

The virtual-agent procedure engine converts validated EasyMesh requests into this internal semantic contract. Direct calls are permitted for component tests and diagnostics; main evaluation scenarios must arrive through the actual protocol boundary. The semantic backend contains no assumptions about a particular CMDU encoding.

| Backend method/event | Contract |
| --- | --- |
| `discover_inventory(pod_id)` | Read identity and available radio/VIF/client relationships from an authenticated session |
| `snapshot(pod_id)` | Return observed objects with generation, provenance and freshness |
| `capabilities(resource)` | Return qualified operations/data with unsupported/unknown reasons |
| `validate(change)` | Check resource ownership, schema, field values and shared-resource impact without writing |
| `plan(change)` | Produce a redacted exact field-level change and observed-state success predicate |
| `submit(operation_id, plan)` | Submit guarded desired-state edits; return accepted/rejected/indeterminate outcome |
| `observe_operations()` | Deliver config-commit, applied-state, conflict, failure and timeout events |
| `reconcile(pod_id)` | Resynchronize database state and outstanding operations after reconnect/restart |

**API-01.** Every modifying request MUST have a controller operation ID and idempotency key. The adapter MUST reject reuse of an idempotency key for a different normalized request.

**API-02.** A backend acceptance or database commit MUST NOT be returned as confirmed radio application. Use the lifecycle below and expose the current stage to callers.

**API-03.** Every operation mapping MUST specify writable fields, validation, transaction preconditions, expected observed-state predicates, required freshness, deadlines, and recovery semantics.

**API-04.** A plan MUST preserve unowned columns and sensitive values in redacted form. It must identify the affected radio and any related VIF/backhaul impact.

**API-05.** Missing telemetry or unsupported actions MUST have explicit outcomes. No synthetic success, guessed capability, or newly installed pod hook is permitted.

### 7.1 Operation lifecycle

```text
REQUESTED → VALIDATED → SUBMITTED → CONFIG_COMMITTED → OBSERVED_APPLIED
     │           │          │                 │
     └→ REJECTED │          ├→ INDETERMINATE  ├→ FAILED / TIMED_OUT
                 │          └→ FAILED         └→ OWNERSHIP_CONFLICT
                 └→ REJECTED / OWNERSHIP_CONFLICT

INDETERMINATE → resynchronize → verified current outcome or still unknown
```

`OBSERVED_APPLIED` means the qualified state predicates match a fresh observation. It does not by itself prove every data-plane property. For example, a state row showing the intended SSID does not prove a real client can authenticate with the new key. Release tests add independent client/traffic checks where required.

Section 21.2 defines deadline, late-observation and reconciliation behavior precisely. The diagram shows common paths; implementation MUST use a transition table with tested guards rather than treating every arrow as sufficient evidence.

## 8. OpenSync 6.6.0 schema and mapping plan

### 8.1 Upstream reference versus actual pods

The inspected upstream `osync_6.6.0` branch resolved to commit `78d8a7194d5e77635877cc456231e7be5cf03d68`. Its `.version` reports `6.6.0.0`; its OVSDB schema identifies database `Open_vSwitch`, version `7.11.413`. These are upstream reference facts. The user's actual firmware may include vendor/provider differences. See the pinned [version](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/.version) and [schema](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/interfaces/opensync.ovsschema).

**SCHEMA-01.** Fetch the actual database schema, record a sanitized version/hash, and validate required tables/columns/types before enabling writes. Version strings alone are insufficient.

**SCHEMA-02.** Qualify a mapping per actual firmware/model combination. Record target-specific semantics, enabled manager behavior, and operation evidence. Preserve unknown vendor columns.

**SCHEMA-03.** On incompatible schema change, retain diagnostics/read access where safe and disable affected writes. Never silently reinterpret columns using an older mapping.

### 8.2 Candidate field mappings

The following mappings are starting points from the pinned upstream schema. The actual mapping is accepted only after target qualification.

| Controller concept | Candidate OpenSync data | Required interpretation |
| --- | --- | --- |
| Pod identity | Available identity/version fields in `AWLAN_Node` | Cross-check trusted session binding; do not treat a reported serial alone as authentication |
| Radio desired settings | `Wifi_Radio_Config` | Map qualified `channel`, `channel_mode`, `ht_mode`, `tx_power`, and associated fields only when radio control is enabled |
| Radio observations | `Wifi_Radio_State` | Actual radio MAC, band, channel/width/power and available constraints; resolve `radio_config`/`vif_states` references |
| Radio-to-VIF config | `Wifi_Radio_Config.vif_configs` | Follow UUID relations within the correct pod/database generation |
| BSS desired settings | `Wifi_VIF_Config` | Designated AP VIF: SSID, enabled state, qualified security fields; preserve networking and backhaul fields |
| BSS observed state | `Wifi_VIF_State` | Actual BSSID/MAC, operational state, SSID and qualified security indicators; follow `vif_config` reference |
| Client membership | `Wifi_VIF_State.associated_clients` and `Wifi_Associated_Clients` | Association to a BSS comes from references; idle/active/power-save labels are not complete association history |
| Uplink observations | `Connection_Manager_Uplink`, `Wifi_Master_State`, qualified interface/bridge state | Report existing topology evidence; do not reconfigure uplink during fronthaul changes |
| Statistics setup | `Wifi_Stats_Config` | Configuration of collection/reporting does not imply the measured samples are remotely readable in OVSDB |
| Steering | Qualified existing steering table semantics, if usable remotely | Policy configuration and one-shot action/completion are different; unavailable actions remain unsupported |

**MAP-01.** Read State tables and write the approved Config fields. Never write fabricated state/client entries to make readback agree with a request.

**MAP-02.** Qualify one security representation for each build: modern `wpa`, cipher flags, `wpa_key_mgmt`, `wpa_psks`, `pmf` fields or the applicable legacy `security` mapping. Do not populate competing representations blindly.

**MAP-03.** Keep secrets out of normal logs and plans. Explicitly handle per-key identifiers and map mutation semantics when updating PSKs; a key change must not erase unrelated keys/tags accidentally.

**MAP-04.** The schema's `multi_ap` field describes a VIF role setting. Setting it does not install an EasyMesh protocol engine. Core MUST NOT change it merely to make an unchanged pod look like an EasyMesh agent.

**MAP-05.** First manage an existing designated fronthaul AP VIF. Creating/deleting VIFs, bridge changes, VLAN changes, backhaul STA configuration, and traffic separation are outside initial core.

**MAP-06.** SSID/credential validation MUST follow both the controller's supported UI encoding and the actual device constraints. Validate byte length, not just character count. Reject unsupported authentication modes before submission.

OpenSync's [Wi-Fi OVSDB adapter](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/src/lib/ow/src/ow_ovsdb.c) is useful for understanding config/state behavior, including row deletion semantics. Its [statistics](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/src/lib/ow/src/ow_ovsdb_stats.c) and [steering](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/src/lib/ow/src/ow_ovsdb_steer.c) adapters are separate qualification references. Reading that code does not make its in-process APIs remotely available to the new controller.

### 8.3 Mapping record

For each operation keep a versioned record containing:

```text
operation name, supported firmware/model/schema fingerprints
controller input fields and validation
pod/radio/VIF identity resolution
config tables, columns, conversions, units and allowed values
ownership allowlist and shared-radio impact rules
transaction preconditions and patch/mutate operations
observed-state tables/fields and success predicate
freshness requirement, deadline, constraint/error interpretation
idempotency and reconnect/rollback behavior
positive and negative fixtures; real-pod qualification evidence
```

### 8.4 Optional existing telemetry input

Detailed metrics may need a separate existing telemetry path. OpenSync exposes MQTT configuration through `AWLAN_Node` fields, and the pinned upstream release includes a [statistics Protobuf schema](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/src/lib/protobuf/opensync_stats.proto). Neither establishes which reports are enabled or reachable on the actual pods. The initial inventory/provisioning evaluation does not require an MQTT broker.

```text
Pod's existing telemetry publisher → authorized broker → EMOSA telemetry consumer
                                                        │
                                              normalized fresh observations
                                                        │
                                              qualified EasyMesh reports
```

Use an existing authorized broker/subscription when possible. A lab broker is an optional controller-side test component only if the unchanged pods can use it through existing configuration and trust. MQTT telemetry is an additional observation source; it does not replace OVSDB configuration control.

**TEL-01 [extension].** Qualify endpoint, credentials, topic permissions, authenticated pod binding, payload envelope/compression, exact Protobuf descriptors and enabled report types before using telemetry. Do not assume fixed broker ports, plaintext transport, a universal topic format or high-frequency reporting. No new pod publisher or helper is allowed.

**TEL-02 [extension].** Each metric mapping MUST specify source field, units, sampling interval, aggregation, counter/reset behavior, timestamp meaning, freshness limit and exact destination TLV field. A periodic average is not automatically a valid instantaneous answer. Unsupported or insufficiently fresh mandatory data prevents a claim of support for the affected procedure.

**TEL-03 [extension].** Handle late, duplicate, retained and out-of-order reports. A cached or retained report arriving after reconnect MUST NOT become fresh merely because its receive time is recent. Keep OVSDB observations and telemetry timestamps/provenance distinguishable.

**TEL-04 [extension].** Test conversion against independent expected values, including counter reset, stale source and missing fields. An actual measured metric encoded into an EasyMesh report is translated evidence; generated model data remains explicitly simulated. MQTT delivery does not prove the measurement satisfies a controller's requested semantics.

## 9. OVSDB sessions and update handling

### 9.1 Connection lifecycle

```text
DISCONNECTED → CONNECTING → AUTHENTICATING → SCHEMA_CHECK → SYNCING
                                                             │
                                             complete snapshot → READY
READY → transport loss → STALE / DISCONNECTED
READY → incompatible schema or identity → DEGRADED (writes disabled)
```

**DB-01.** Use persistent OVSDB sessions. Implement supported request/reply handling, notifications, echo, and reconnect backoff with jitter. Do not run a remote shell command for each operation.

**DB-02.** Use a consistent initial monitor snapshot followed by incremental updates, or another qualified sequence without a lost-change window. Apply an update batch atomically to the local cache before emitting normalized events.

**DB-03.** Correctly normalize UUIDs, optional scalars, sets, maps and partial updates. Do not confuse a missing field in an incremental notification with deletion or an empty value.

**DB-04.** Negotiate the deployed server's features. Basic `monitor` support is enough for core; require extensions such as `monitor_cond` only when tested on those pods.

**DB-05.** Route every update/reply through the authenticated pod/session generation and transaction ID. A delayed reply from a previous connection cannot complete a new operation with a reused identifier.

**DB-06.** A session is ready for writes only after identity validation, schema compatibility, complete initial synchronization, and ownership qualification. Reconnect starts a new synchronization epoch.

**DB-07.** Bound decoded message size, outstanding requests, monitor cache size and per-pod queues. Account for a large valid initial snapshot when choosing bounds. Isolate a noisy or unhealthy pod so it cannot starve other sessions or the CLI.

OVSDB protocol operations and encodings are defined in [RFC 7047](https://www.rfc-editor.org/rfc/rfc7047.html). Server extensions should be checked against the deployed implementation and [Open vSwitch server documentation](https://docs.openvswitch.org/en/latest/ref/ovsdb-server.7/).

### 9.2 Writes

**DB-08.** Write minimal field patches or map/set mutations, with stale-precondition protection using supported transaction operations. Do not replace a whole cached row and discard unrelated fields.

**DB-09.** Batch related database edits in one transaction where supported. A successful transaction is atomic database configuration, not atomic multi-radio application.

**DB-10.** Check every operation result inside a transaction, including matched/updated row counts. A transport-level JSON-RPC success with no matching target row is not a successful configuration change.

**DB-11.** Never write State tables as an application acknowledgment. Wait for qualified observed-state predicates or an existing supported completion mechanism.

**DB-12.** Use operation-specific deadlines and validation for radio changes. DFS/CAC or service restart may exceed the timeout appropriate to changing an ordinary scalar field. The CLI wait timeout is independent of the backend operation's deadline.

### 9.3 Example BSS change

1. Resolve `pod_id` and the designated stable BSS ID to fresh schema references.
2. Verify ownership and capability, then normalize the requested SSID/security settings.
3. Record intent and idempotency data in the durable controller journal.
4. Read/validate current target values; build a guarded minimal transaction.
5. Submit the transaction; record `CONFIG_COMMITTED` only after valid per-operation results.
6. Observe fresh VIF/radio state and evaluate the mapping's success predicate.
7. Record `OBSERVED_APPLIED`, failure/constraint, conflict, or timeout.
8. Report outcome through CLI/events; keep desired and observed objects separate.

A request that matches already observed qualified state can complete as a documented no-op. A matching Config row alone is insufficient if State remains unconverged.

## 10. Persistence, recovery, and failure semantics

**REC-01.** Use a bounded durable SQLite journal for operation IDs, normalized intent, mapping version, precondition hashes, phase, timestamps, and reconciliation metadata. Protect any persisted sensitive material using the deployment's approved secret mechanism.

**REC-02.** On connection loss after submission, mark the outcome `INDETERMINATE` unless evidence establishes otherwise. After reconnect, inspect configuration and state before deciding whether to retry. Do not assume a lost reply means the transaction never executed.

**REC-03.** Design core writes as idempotent desired-state changes. Retries must not duplicate map entries, create extra VIFs, or replay an unrelated action. Exactly-once execution across controller/pod failures is not guaranteed by an operation ID alone.

**REC-04.** On controller restart, load desired state and outstanding operations, reconnect, rebuild observations, and reconcile. Do not send an empty cached model to the pods or overwrite working configuration simply because local memory was lost.

**REC-05.** When a field is owned but no longer matches intent, distinguish expected local constraints, still-applying work, a different authorized controller operation, and a conflicting writer. Use bounded retry only for explicitly retryable outcomes.

**REC-06.** Compensating rollback is a separate operation. Restore only owned fields whose current values still match the failed operation's expectations. Never overwrite a newer legitimate update using an old snapshot.

**REC-07.** A batch across several pods can partially apply. Report per-pod outcomes and a batch summary; do not claim a cross-pod atomic transaction. Stop/continue policy must be explicit in the submitted batch request.

**REC-08.** Disconnect, cancellation of CLI waiting, and cancellation of backend work are distinct. A CLI client disappearing does not roll back a committed pod change.

## 11. CLI and local API

Provide separate local CLIs for the test controller and EMOSA, plus a lab runner that invokes them in their project-owned containers. Proposed commands:

```sh
em-controller serve --config /etc/emosa/controller.json
em-controller status --json
em-controller agents
em-controller provision --agent agent-01 --config /run/secrets/test-bss.json

emosa serve --config /etc/emosa/adapter.json
emosa status --json
emosa pods
emosa pod pod-01 capabilities
emosa pod pod-01 radios
emosa pod pod-01 bsses
emosa pod pod-01 clients
emosa plan bss-set --pod pod-01 --bss home-5g --ssid demo-network
emosa operation show op-001 --json
emosa ownership status --pod pod-01
emosa events --last 30

emosa-lab run scenarios/provision-one-bss.yaml --backend ovsdb-sim
emosa-lab run scenarios/provision-one-bss.yaml --backend hardware --target lab-pod-01
emosa-lab report run-001 --format json
emosa-lab watch run-001
emosa-lab inspect run-001 --operation op-001
emosa-lab compare run-001 run-002 --format html
```

These are requirements for the interface to implement, not commands claimed to exist now.

`em-controller provision` initiates the selected wire procedure; it does not directly call the OVSDB backend. It accepts the configuration intent and conducts the required exchange when the virtual agent reaches the appropriate procedure state. Direct semantic submission is available only through an explicitly named component-test/diagnostic interface and labels the result accordingly. The lab runner defaults to `easymesh-wire`; a backend selection changes the simulated/physical target, not whether packets are used.

**CLI-01.** Provide human-readable and versioned JSON output. Every modifying result MUST distinguish accepted, committed, observed-applied, failed, timed-out and indeterminate stages.

**CLI-02.** Read-only commands MUST NOT change pod configuration. The `plan` command uses the same validation and mapping as submission but performs no write. Ordinary authorized submission does not require an extra interactive confirmation flow.

**CLI-03.** Each service exposes a restricted local Unix socket, for example `/run/emosa/control.sock` and `/run/em-controller/control.sock` in its own container. The VM-local lab runner invokes local clients through the inner LXD daemon using `lxc exec`. Keep management local to the controller VM in core; a network API is optional future work.

**CLI-04.** Bound local requests, replies, concurrent clients and wait durations. Paginate inventory/event data; redact credentials. Accept secrets through a file or other existing secret provider, not mandatory plaintext command-line arguments.

**CLI-05.** Exit codes: `0` successful command result, `2` invalid input/configuration, `3` wait timeout, `4` local service unavailable, `5` rejected/unsupported operation, `1` other failure. For `--wait`, a timeout must include the operation ID for later inspection.

**CLI-06.** `status` MUST show software version, backend mode, management backend `opensync_ovsdb`, connection/schema/ownership readiness, pod freshness, enabled operations, and current failure reasons. Show virtual-agent protocol state separately from physical pod readiness. It must not label a pod `EasyMesh onboarded` after merely connecting OVSDB.

Example result:

```json
{
  "schema_version": 1,
  "operation_id": "op-001",
  "run_id": "run-001",
  "initiating_interface": "easymesh-wire",
  "virtual_agent_id": "agent-01",
  "pod_id": "pod-01",
  "backend": "opensync_ovsdb",
  "state": "OBSERVED_APPLIED",
  "resource": "bss:home-5g",
  "evidence": {
    "session_epoch": 4,
    "source": "Wifi_VIF_State",
    "predicate": "qualified-bss-config-v1"
  },
  "data_plane_verified": false
}
```

## 12. Configuration, trust, and secrets

**CFG-01.** Use strict versioned configuration. Reject unknown keys, invalid resource selectors, contradictory modes, missing trust material, and unsupported ownership policies before enabling writes.

**CFG-02.** Endpoint addresses, connection direction, database name, certificate identity, and designated VIF mappings are explicit per-pod/deployment inputs. Do not discover unauthenticated pods by scanning arbitrary networks.

**CFG-03.** Use authenticated transport consistent with the already deployed OVSDB security model. Never silently downgrade authentication or disable certificate verification to make a connection work.

**CFG-04.** Separate credentials from ordinary configuration and operation logs. Controller-held OVSDB credentials and Wi-Fi keys are sensitive. Sanitize schemas/snapshots/captures before storing distributable fixtures.

**CFG-05.** A configured endpoint is not considered write-ready solely because the file says `cloud_writers_disabled: true`. Qualification evidence and current ownership/conflict state must support the operation. The controller need not dynamically prove every external setting for each transaction, but must fail clearly if its ownership assumptions stop holding.

**CFG-06.** Each wire endpoint configuration MUST select its protocol interface, AL MAC, allowed peer/lab scope and frozen protocol matrix. EMOSA additionally specifies each virtual-agent-to-pod binding. Reject duplicate virtual identities, missing bindings and inconsistent matrix versions. Packet sockets bind only to the selected protocol interface.

Illustrative EMOSA hardware configuration; profile names and endpoint values are placeholders that must be qualified, not claims about actual deployed endpoints or existing supported profiles:

```json
{
  "schema_version": 1,
  "backend_mode": "hardware",
  "backend": "opensync_ovsdb",
  "state_directory": "/var/lib/emosa",
  "protocol": {
    "interface": "em0",
    "matrix": "qualified-selected-protocol-subset-v1",
    "virtual_agents": [
      {
        "agent_id": "agent-01",
        "al_mac": "02:00:00:00:01:01",
        "pod_id": "pod-01"
      }
    ]
  },
  "write_mode": "managed-fields",
  "pods": [
    {
      "pod_id": "pod-01",
      "connection": {
        "mode": "dial",
        "host": "pod-01.lab.example",
        "port": 6640,
        "tls_profile": "existing-pod-trust"
      },
      "database": "Open_vSwitch",
      "mapping_profile": "qualified-opensync-6.6-pod-model-a-v1",
      "ownership_profile": "controller-fronthaul-v1",
      "managed_bsses": [
        {
          "bss_id": "home-5g",
          "if_name": "target-qualified-existing-vif"
        }
      ]
    }
  ]
}
```

Provide a read-only inventory configuration for initial mapping work. Enabling writes is a documented deployment choice backed by the ownership qualification, not a firmware feature added to pods.

## 13. Observability and operating limits

**OBS-01.** Log meaningful connection, schema, ownership and operation transitions. Include pod ID, session generation, operation ID, affected resource, mapping version, phase, duration and structured reason. Rate-limit repeated connection and malformed-message errors.

**OBS-02.** Counters include connections/reconnects, parse errors, schema failures, stale pods, pending operations, guard conflicts, rejected operations, committed changes, observed applications, apply timeouts, indeterminate outcomes, ownership conflicts and queue-limit events.

**OBS-03.** Expose a redacted desired-versus-observed diff for each operation. State freshness and missing evidence must remain visible.

**OBS-04.** Record a bounded event history and durable operation summary. Full raw OVSDB messages must not be logged by default because they can include credentials and other sensitive fields.

**OBS-05.** Diagnose both boundaries: verify the controller-to-EMOSA packet exchange, then OVSDB connection/trust, identity, schema, snapshot, ownership, mapping, transaction result, state convergence and independent client behavior. A frame captured on the internal bridge is evidence about the virtual endpoint, not a frame transmitted by the physical pod.

Initial engineering budgets, subject to measurement:

- Reference VM: 2 vCPU, 2 GiB RAM; record actual host/runtime/build details.
- Core qualification: one physical pod first, then two for physical multi-pod isolation; exercise at least four concurrent simulated sessions. Additional hardware scale is reported separately.
- Modelled scale test: 32 mock pod sessions, bounded per-pod and total memory/queues.
- Ordinary read-only local CLI response: target p95 below 250 ms when serving the current cache; remote refresh is explicitly asynchronous.
- Ordinary BSS application: use a target-qualified deadline; begin with a 30-second lab budget and measure real convergence.
- Disconnected-pod retries: exponential backoff with jitter and a configured cap; no retry storm.
- Report actual controller RSS, idle CPU, startup/resynchronization time and image size. No arbitrary footprint target requires C before measurement.

## 14. Acceptance and validation

### 14.1 Mandatory core acceptance matrix

| Test | Procedure and pass condition |
| --- | --- |
| A01: unchanged pods | Verify no new pod files/services/packages/firmware were required; only intended existing configuration fields change |
| A02: access mode | Establish the actual authorized OVSDB connection direction and trust; reconnect successfully without weakening authentication |
| A03: identity isolation | Updates from two pods with the same database name/overlapping row UUID values remain isolated by authenticated session/pod identity |
| A04: actual schema | Retrieve real schema, select mapping, and reject missing/incompatible required semantics before writes |
| A05: initial snapshot | Correct inventory appears from one complete monitor snapshot; update/delete/reference changes normalize correctly |
| A06: observed model | Radio/BSS/client relationships agree with independently inspected pod state; no fixture values appear in hardware mode |
| A07: ownership | Disabled/redirected cloud writers do not change owned settings after reconnect/reboot; local conflicts are identified or excluded |
| A08: field preservation | BSS update patches only permitted fields; unrelated keys/tags, bridges, uplinks and VIFs remain intact |
| A09: true application | Designated BSS changes through OVSDB, observed state converges, and a real test client confirms the expected SSID/security behavior |
| A10: no false success | Config commit with absent/delayed/rejected radio application never produces OBSERVED_APPLIED prematurely |
| A11: stale precondition | Concurrent change before transaction submission produces a guard conflict or a validated recomputation, not a lost update |
| A12: lost reply | Disconnect after submission; resynchronize, identify the verified current outcome, and preserve unknown commit attribution where necessary instead of blindly replaying the operation |
| A13: idempotency | Repeated same-key request does not duplicate work; reuse with different intent is rejected |
| A14: controller restart | Restart with pending work; journal and fresh observations reconcile without overwriting good configuration |
| A15: pod/server restart | Reconnect, rebuild UUID references, preserve stable IDs, revalidate schema and report freshness correctly |
| A16: conflict handling | Competing writer to an owned field yields conflict and halted conflicting writes, not a write-back loop |
| A17: unsupported features | Missing stats/steering/capability access returns unsupported/unknown; no fake results or new device helpers |
| A18: constrained operation | Shared-radio/backhaul and regulatory constraints reject or limit unsafe/unsupported requests before submission where knowable |
| A19: multi-pod isolation | Slow/malformed/noisy pod does not block healthy sessions or the CLI; partial batch outcomes are explicit |
| A20: secrets and CLI | Output/schema/exit codes/wait behavior work; secrets are redacted; raw-socket privileges are restricted to protocol endpoints |
| A21: outage behavior | Measure actual unchanged pod/cloud behavior during controller loss; document it without claiming unverified retention |
| A22: handover | Quiesce new-controller writes and return control using existing management controls without resetting unrelated configuration |
| A23: wired hardware path | Run inventory, one designated BSS change, independent client verification and reconnect over recorded wired management |
| A24: wireless hardware path | Repeat the supported baseline over a recorded wireless management path; verify that traffic actually uses that path and record its shared-radio/BSS dependencies |
| A25: management loss | Induce a bounded management disconnect, report stale/indeterminate state correctly, restore the path, and reconcile before further writes; demonstrate the documented recovery method |

The hardware-management baseline is qualified only after A01–A25 pass for at least one explicitly identified pod model/build and its ownership configuration. Tests that require multiple physical pods remain unqualified if only simulated evidence exists. Simulator-only results do not establish real-pod compatibility. EMOSA platform acceptance additionally requires the evaluation checks in Section 18.5; protocol compatibility has its own scope and evidence in Section 16.

### 14.2 Test layers

1. **Pure unit tests:** schema normalization, identity/reference mapping, desired/observed comparisons, operation state transitions and deadlines.
2. **OVSDB protocol tests:** actual compatible test server, partial updates, set/map values, guard conflicts, disconnects, echo and supported monitor features.
3. **Controller-side mock pods:** multiple connections, delayed/rejected apply, conflicting writer, ambiguous outcome and reconnect epochs. A mock must emulate state changes independently of accepting config writes.
4. **Read-only real pod:** exact schema and state mapping, firmware/model inventory, ownership/writer assessment.
5. **Writable qualified pod:** designated existing fronthaul BSS, real observed state and client verification, restart/outage/handover tests.

Round-trip encode/decode tests are insufficient: independently inspect state and compare against expected field-level changes. Failure tests are mandatory for the operations that can disrupt a BSS.

### 14.3 Physical-pod sequence and evidence

1. **Inventory without writes.** Record model/build, schema fingerprint, trust and connection direction, radios/VIF relationships, active writers, and management path. One pod is sufficient to begin; add a second for physical multi-pod isolation and backhaul scenarios.
2. **Wired baseline.** Select one existing fronthaul BSS and a known recoverable test configuration. Plan and apply a qualified SSID/security change. Verify fresh state plus an independent client's BSSID/SSID, authentication and connectivity to a lab endpoint where the existing network supports it.
3. **Wireless baseline.** Repeat the scenario on the intended wireless management path. First use a target BSS that does not carry management where the hardware permits this. If separation is impossible, record the dependency and use the disruptive-test recovery procedure.
4. **Failures and recovery.** Exercise session loss, controller restart, and then controlled pod restart. Record operation outcomes and restoration time. Changing an uplink credential or channel is a separate scenario after shared-resource qualification, not part of the first BSS demonstration.
5. **Multiple pods and handover.** Check that a failed pod does not block healthy pods, and that ending a run or returning ownership does not restore stale configuration blindly.

Evidence MUST associate the scenario/run ID and controller operation ID with the before/after observations, management path, transaction result, application result, client result, and recovery outcome. Record missing evidence explicitly. Keep Wi-Fi credentials out of reports, fixtures and ordinary captures.

## 15. Implementation milestones and deliverables

These are outcome milestones, not a strict blocking sequence. Protocol-independent implementation and simulations can proceed while P0 specifications or M0 hardware inputs are unavailable. The companion handoff defines task dependencies and distinguishes partial delivery from the completed evaluation baseline.

| Milestone | Work | Exit evidence |
| --- | --- | --- |
| M0: qualify interface and ownership | Record existing access/trust/session direction, actual schema, selected VIFs and writer controls | Supported-pod entry and ownership/mapping plan; no pod software changes |
| P0: freeze protocol scope | Select accessible specification revisions and map the first procedures, fields, timers and limits | Versioned protocol matrix; mandatory gaps identified before claiming profile support |
| P1: packet boundary | Implement controller/virtual-agent transport, selected discovery/capability and provisioning against simulations | W01–W05 and independent packet evidence for the selected subset |
| M1: observe | Python service, local CLI, per-pod session, snapshot/monitor cache, normalized inventory | A02–A06, basic diagnostics |
| M2: model operations | Semantic adapter contract, mock backend, scenario runner, durable journal, guards, idempotency and reconciliation | Repeatable delayed/failed/ambiguous-operation tests without hardware writes |
| M3: configure one BSS | Qualified field mapping and actual apply predicate on one wired-connected test pod, then the selected wireless path | Applicable A07–A18 plus A23–A25 with real client evidence; remaining cases tracked explicitly |
| M4: evaluation baseline | Main scenarios traverse the wire adapter; multi-pod isolation, restart/outage/handover, live visibility, parameterized reruns, comparisons and runbook | A01–A25, W01–W06, E01–E08 and measured behavior for the explicitly scoped subset; execution status and interoperability verdict reported separately |
| M5: expand selectively | Additional qualified BSSs, channel control, telemetry or steering using existing interfaces only | Operation-specific mappings and acceptance evidence |
| R0: OpenSync native feasibility | Build pinned OWM/OW/OSW test components and assess a dummy-driver harness against a disposable database | N01–N04 evidence or a concrete unsupported/dependency report; no implied hardware compatibility |
| X1: independent evaluation | Deliver reproducible package, attach an independent controller and run externally checked scenarios | T01–T05 for the scoped controller/profile/build combination; separate from self-test acceptance |

**DEL-01.** Deliver source, pinned dependencies/container build, deployment scripts, example configs, credential setup instructions, CLI help and a reproducible demonstration.

**DEL-02.** Deliver supported-pod/model/schema records, operation mappings, writer-ownership runbook, sanitized fixtures, acceptance results and measured limits.

**DEL-03.** The demonstration MUST show inventory, plan a field-level change, submit it, distinguish commit from observation, show real outcome, and recover from an induced reconnect. It cannot prepopulate the expected result into observed state.

**DEL-04.** Setup and teardown scripts operate only on project-owned controller/test resources. Stopping or deleting a controller container does not reset real pod configurations.

**DEL-05.** Deliver versioned scenario definitions, sanitized input fixtures, machine-readable results, a human-readable run summary, and instructions for reproducing simulation and hardware runs. Failed, blocked, skipped, unsupported, and inconclusive outcomes MUST remain visible.

**DEL-06.** Deliver the third-party evaluation package in Section 19. It MUST allow another team to run simulations without private developer infrastructure and to replace the included controller without changing EMOSA protocol or mapping code. Hardware credentials are supplied locally by the evaluator.

## 16. Required EasyMesh wire interface and virtual agents

### 16.1 Protocol scope gate

The required wire interface uses real IEEE 1905 Ethernet frames (EtherType `0x893A`) inside the VM. Python can use Linux [`AF_PACKET`](https://docs.python.org/3/library/socket.html#socket.AF_PACKET). The selected message identifiers can be cross-checked against [prplMesh's message definitions](https://gitlab.com/prpl-foundation/prplmesh/prplMesh/-/raw/master/framework/tlvf/yaml/tlvf/ieee_1905_1/eMessageType.yaml), without linking to or importing prplMesh code. Implementation references do not replace normative specifications.

**WIRE-01.** Before implementing a procedure, record the exact accessible IEEE 1905, Wi-Fi EasyMesh and applicable provisioning specification revisions, selected profile, and relevant sections. Build a matrix of message IDs, mandatory/optional TLVs, field encodings, ordering rules, response/ACK rules, timers, retries, fragmentation and error behavior. A reference packet list alone is insufficient. The exact profile/release is an explicit P0 decision, not implicitly the latest release.

Start with this proposed subset, and refine it against that matrix:

| Procedure | Initial purpose | Backend dependency / qualification |
| --- | --- | --- |
| Topology Discovery and Query/Response | Discover and inspect the virtual endpoint | Report the virtual protocol topology honestly; physical relationships need separate evidence |
| AP Autoconfiguration Search/Response | Establish selected controller/agent roles | Real packet and state-machine behavior; no OVSDB write by itself |
| AP Capability Query/Report | Describe the represented radio/BSS scope | Qualified hardware/schema/operation data; never infer full capabilities from table names |
| AP Autoconfiguration WSC M1/M2 | Provision one designated existing fronthaul BSS through the adapter | Genuine selected WSC exchange, authenticated/decrypted configuration, qualified SSID/security mapping and application evidence |

This is a procedure subset, not a declaration of full profile compliance. If a chosen profile requires information or actions unavailable from unchanged pods, record that gap and narrow the evaluation claim. Do not invent data or silently omit mandatory fields and still label the procedure conformant. Multi-BSS provisioning, renew/reconfiguration, channel control, steering and advanced metrics require separate mapping and scope decisions.

### 16.2 Virtual-agent identity and topology

**WIRE-02.** Maintain one logical virtual-agent identity per represented pod, with stable unique locally administered unicast AL MACs allocated for the isolated lab. AL MAC identifies the IEEE 1905 abstraction-layer entity; it is not automatically the pod's hardware MAC. Persist the binding between virtual identity, trusted pod ID, actual radio identifiers and actual BSSIDs. Document any translated identifier and its inverse mapping.

**WIRE-03.** Start with one virtual agent. When adding more, provide verified per-agent Ethernet delivery and independent procedure state, using distinct virtual interfaces/namespaces or another demonstrated demultiplexing design. Multicast received by several agents must not duplicate an operation on a single pod. Scope duplicate detection and message correlation by peer, message type, message ID and procedure/session context; message ID alone is not a durable operation ID.

**WIRE-04.** Keep protocol adjacency separate from measured pod backhaul topology. The virtual agents share a lab bridge; this does not mean their physical pods have Ethernet links to each other. Report only topology/capability information supported by the selected procedure and available evidence. Unsupported mandatory semantics limit compatibility claims.

### 16.3 Framing, procedure state and provisioning

**WIRE-05.** Validate Ethernet/CMDU/TLV lengths, byte order, bounds, flags, end markers and required fields before acting. Implement the selected procedure's fragment handling with bounded message size, concurrent assemblies and expiry. Exercise truncation, unknown TLVs, duplicate/out-of-order fragments, repeated messages and unexpected procedure order. Follow the selected specification for unknown or malformed inputs; never guess a success response.

**WIRE-06.** Implement timers, retransmission, duplicate handling and message-ID rollover per the frozen matrix. Do not reapply configuration because a peer retransmits a valid message. Protocol-session state and semantic operation state are separate; after reconnect or restart, reconcile pod state before replaying writes.

**WIRE-07.** Provisioning must implement the actual selected WSC exchange, including required key derivation, authentication, nonces and encrypted configuration handling using vetted primitives. Plaintext credential TLVs, canned M2 payloads and bypassed authenticator checks cannot pass provisioning acceptance. Keep protocol secrets out of ordinary logs and fixtures. An invalid exchange MUST produce no pod write.

**WIRE-08.** Map a validated provisioning request only to its qualified owned resources. Validate the entire requested configuration before submitting writes. If it requests unsupported BSS creation, deletion, backhaul changes or a security mode, follow the applicable procedure's failure behavior and expose the mapping limitation. Do not apply a convenient subset and claim the entire request succeeded.

### 16.4 Meaning of responses and application results

**WIRE-09.** Preserve each protocol response's defined meaning and timing. A protocol ACK, where specified, is not automatically confirmation of radio application; do not delay or redefine it as a proprietary apply acknowledgment. Track protocol completion, OVSDB commit, observed application and independent client verification separately in EMOSA's local diagnostics and reports.

**WIRE-10.** Use the selected specification's supported response/report mechanisms for wire-visible state. If no wire message expresses a particular internal failure, report it through the local evaluation API and mark the evidence gap. Do not add private TLVs and call them standard EasyMesh behavior. Do not report fresh capabilities or measurements from stale or synthetic hardware data.

The first hardware demonstration follows this chain: test-controller provisioning request → real WSC exchange with EMOSA → validated configuration → guarded OVSDB change → fresh pod state → independent client check. If P0 discovers that the selected provisioning procedure requires a larger scope than one existing BSS, resolve that mapping before claiming this demonstration is supported.

### 16.5 Protocol acceptance

| Test | Pass condition |
| --- | --- |
| W01: actual packet boundary | Captures on the isolated bridge show selected real request/response frames between separate controller and EMOSA endpoints; an in-process call cannot satisfy this test |
| W02: independent interpretation | Selected valid and invalid vectors agree with the pinned specification and an independent parser, independently derived expected bytes, or independent implementation; two EMOSA components agreeing is insufficient |
| W03: robust protocol handling | Malformed input, supported fragmentation, duplicates, retransmissions, timer expiry and ID rollover produce the required bounded behavior without unintended pod writes |
| W04: provisioning validity | Valid selected WSC exchange maps to the expected semantic operation; invalid authentication, nonces, encoding or unsupported scope cannot cause configuration writes |
| W05: identity and reporting | Virtual agents and pod operations remain isolated; reports and capabilities preserve provenance, observed state and honest topology scope |
| W06: hardware end-to-end | One supported procedure traverses real frames and real OVSDB, changes the designated physical BSS, and has independent client evidence; a failed/lost-connection run reports the correct outcome |

An independent controller is the required peer for a claimed third-party controller interoperability result under Section 19. It is not a build-time or runtime dependency of EMOSA's distributed core. Passing the selected subset does not establish arbitrary controller interoperability or EasyMesh certification.

## 17. Sources and verification boundaries

The architectural requirements and proposed CLI/module layout are design decisions. Source links below support platform/protocol facts and provide implementation reference points.

| Source | Use |
| --- | --- |
| [OpenSync 6.6.0 version](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/.version) | Pinned upstream version reference |
| [OpenSync 6.6.0 schema](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/interfaces/opensync.ovsschema) | Tables, types, enums and references |
| [OpenSync Wi-Fi OVSDB adapter](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/src/lib/ow/src/ow_ovsdb.c) | Config/state and row lifecycle behavior |
| [OpenSync statistics adapter](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/src/lib/ow/src/ow_ovsdb_stats.c) | Statistics configuration integration |
| [OpenSync steering adapter](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/src/lib/ow/src/ow_ovsdb_steer.c) | Steering integration qualification |
| [RFC 7047](https://www.rfc-editor.org/rfc/rfc7047.html) | OVSDB operations, types, transactions and cooperative locks |
| [Open vSwitch OVSDB server](https://docs.openvswitch.org/en/latest/ref/ovsdb-server.7/) | Server protocol extensions and compatibility |
| [OVSDB connection methods](https://docs.openvswitch.org/en/stable/ref/ovsdb.7/#connection-methods) | Database client/server roles versus active/passive transport direction |
| [OpenSync FAQ](https://opensync.atlassian.net/wiki/spaces/OCC/pages/39920140758) | Existing manager/redirector and MQTT configuration; actual devices still require qualification |
| [OpenSync connection requirements](https://opensync.atlassian.net/wiki/spaces/OCC/pages/39920140311) | Deployment-specific ports and mutual authentication; not proof of the pods' configuration |
| [OpenSync 6.6 statistics Protobuf](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/src/lib/protobuf/opensync_stats.proto) | Optional telemetry decoding reference; enabled reports require qualification |
| [OpenSync OWM native test script](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/src/owm/scripts/test.sh) | Existing native OWM build/test integration with a disposable OVSDB server |
| [OpenSync OSW dummy driver](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/src/lib/osw/src/osw_drv_dummy.c) | Candidate simulated device boundary; not a standalone pod emulator |
| [prplmesh-lab architecture](https://github.com/boardfarmdevs/prplmesh-lab/blob/main/reference/platform/software-architecture.md) | Context for the original EasyMesh learning lab |
| [Python packet socket API](https://docs.python.org/3/library/socket.html#socket.AF_PACKET) | Required internal protocol transport |
| [prplMesh message IDs](https://gitlab.com/prpl-foundation/prplmesh/prplMesh/-/raw/master/framework/tlvf/yaml/tlvf/ieee_1905_1/eMessageType.yaml) | Implementation-level message identifier cross-check, not normative coverage |

The upstream schema and relevant source were inspected. Actual pod schemas, endpoint trust, manager behavior and apply timings have not been inspected or tested in this task. Direct OVSDB access and the ability to disable/redirect cloud writers are user-confirmed inputs. Physical pods with wired or wireless connectivity are planned for evaluation. M0 qualifies their exact implementation before device writes. No hardware compatibility or production readiness has been demonstrated by this document.

**Final decision:** build EMOSA and its test controller in Python; require real EasyMesh messages into controller-side virtual agents; use one shared OVSDB adapter for simulations and unchanged physical pods. Preserve independent evidence at the packet, database, pod-state and client-behavior boundaries.

## 18. EMOSA evaluation platform

### 18.1 Evaluation questions

Each run should answer a specific question: whether a selected operation translates correctly, whether the pod actually applies it, how long the stages take, and what happens when a stage fails. A successful adapter test is narrower than a successful Wi-Fi system or a production qualification.

### 18.2 Execution modes

Select the backend mode independently from the initiating interface. Main evaluations use `easymesh-wire`; `semantic` is available for component tests. A run initiated through the direct semantic API is not evidence that an EasyMesh message was encoded, received or handled correctly.

| Backend mode | Components exercised | Evidence it can provide |
| --- | --- | --- |
| `model` | Controller operations, semantic backend and deterministic in-memory device model | Fast checks of policy, lifecycle, deadlines and expected failure handling; no OVSDB or RF claim |
| `ovsdb-sim` | Real adapter/session against a compatible OVSDB server plus a separate simulated pod manager | OVSDB transactions, monitors, mapping, delayed/rejected application, reconnect and schema handling; no actual radio behavior |
| `opensync-native` | Real adapter/session against a disposable OVSDB server plus selected compiled OpenSync managers and a qualified simulated driver | Actual selected upstream manager behavior with simulated hardware; requires Section 22 qualification and does not establish vendor-pod or RF behavior |
| `hardware` | Same adapter/session against unchanged physical OpenSync pods | Actual mapping/application and independent client behavior for the named model/build and measured topology |

For `ovsdb-sim`, use the qualified sanitized schema. The simulated manager watches Config and produces State according to its own behavior model. An OVSDB server alone stores configuration; it is not a simulation of the Wi-Fi manager. Test both successful application and ignored/rejected/delayed configuration. Keep model rules separate from the adapter's expected-state computation so the test does not merely reproduce the same bug twice.

Recorded monitor streams can supplement these modes for observation regressions. Replay alone cannot establish that a new write would have been accepted or applied.

**EVAL-01.** A run MUST declare its initiating interface (`semantic` or `easymesh-wire`), backend mode, scenario version, software revision, mapping version, schema fingerprint, topology, pod/build identities where applicable, random seed, deadlines and resource budget.

**EVAL-02.** All backend modes MUST exercise the same operation contract. `ovsdb-sim`, qualified `opensync-native`, and `hardware` MUST use the same production-intended OVSDB mapping/session path, with explicit mapping profiles where schema/builds differ. Hardware mode MUST reject fixture telemetry and synthetic application events.

**EVAL-03.** Model runs MUST support reproducible seeded behavior and an injectable clock. OVSDB and hardware runs MUST use real protocol deadlines and record actual event ordering; a random seed does not make RF behavior or OS scheduling deterministic.

### 18.3 Scenario contract

Store scenarios as versioned JSON or YAML. Validate their structure before execution. Each scenario contains:

- Identifier, purpose, required backend/interface and capability prerequisites.
- Target allowlist, initial state and ownership prerequisites, exact input operation, and allowed field changes.
- Expected protocol outcomes where applicable, transaction predicates, observed-state predicates, and independent client checks where applicable.
- Fault schedule with a named injection point, trigger and bounded duration.
- Deadlines per phase, overall timeout, evidence requirements and explicit verdict rules.
- Cleanup policy, fields eligible for restoration, and recovery instructions for an interrupted run.

**EVAL-04.** The first scenario suite MUST include inventory, one BSS change, unsupported operation, competing writer, device application rejection, lost transaction reply, reconnect, controller restart, pod/server restart, and multi-pod isolation. Hardware availability and operation support determine eligibility; unmet prerequisites produce an explicit blocked/unsupported result.

**EVAL-05.** Inject faults at a declared layer. Dropping an EasyMesh frame, closing an OVSDB connection, interrupting TCP connectivity, and suppressing a simulated manager's State update are different faults. Implement fault injection in controller/test resources without installing helpers on physical pods. On real pods, use only available external controls and existing supported interfaces.

**EVAL-06.** Use independently specified expected outcomes. An adapter and a simulator written from the same assumptions are insufficient interoperability evidence. For wire tests, cross-check selected packets against the pinned specification and independently derived vectors or an independent implementation; keep any external reference outside EMOSA's runtime dependencies.

### 18.4 Run results and limits

**EVAL-07.** Produce a machine-readable run manifest and summary with verdict `pass`, `fail`, `blocked`, `unsupported`, `skipped`, or `inconclusive`. An unsupported mandatory capability prevents the associated profile from passing. Show the eligible, attempted and passing test counts separately; skipped or unknown results MUST NOT be counted as passes.

**EVAL-08.** Correlate scenario/run ID, semantic operation ID, OVSDB transaction/session generation, and EasyMesh message identity when present. Capture separate durations for request handling, database commit, observed application, independent client recovery, and management reconnection. For distributed client measurements, record clock synchronization or use durations measured on a single observer. Report sample count and variability; one successful change is not a latency percentile.

**EVAL-09.** Store redacted plans, state differences, event timelines and report artifacts by run. Capture wire traffic only where required and account for sensitive payloads. Label every result as simulated, replayed, inferred or measured on hardware. Simulated RSSI, throughput, steering or topology MUST NOT be presented as physical measurements.

**EVAL-10.** Separate reset procedures by backend. Simulations can reset their owned databases and state. Hardware cleanup MUST restore only scenario-owned fields when the latest state still satisfies the restoration preconditions; it MUST NOT replay an entire stale database snapshot. If connectivity is lost, report pending recovery rather than a successful cleanup.

**EVAL-11.** Qualify protocol coverage per named EasyMesh release/profile and procedure. Record message/TLV support, state/timer behavior, mapping limits, and unsupported semantics. A subset implementation can be useful for evaluation without qualifying the entire profile. RF propagation, actual roaming performance and standards certification remain outside simulation claims.

The initial platform needs a CLI with live experiment views, versioned scenario files, and JSON plus human-readable HTML/Markdown reports, including run comparison. Visibility and experimentation are core deliverables. A graphical dashboard, large fleet service, multi-controller failover, full RF simulator, and production operations portal are later projects if evaluation results justify them.

### 18.5 Platform acceptance

| Test | Pass condition |
| --- | --- |
| E01: reproducible model | Repeating the same model scenario/seed yields the same logical sequence and verdict |
| E02: realistic OVSDB simulation | Configuration commit can be followed independently by success, rejection, delay or no application; controller verdict distinguishes them |
| E03: shared implementation | The same qualified adapter code runs against simulated OVSDB and hardware, with no fixture fallback in hardware mode |
| E04: evidence and verdicts | A report ties each requested operation to actual evidence, records limitations and excludes blocked/unsupported/skipped results from passes |
| E05: interruption and cleanup | Interrupted runs preserve evidence and expose pending recovery; cleanup neither erases unrelated fields nor masks a failure |
| E06: scoped claims | Report identifies initiating interface, backend, model/build, management path and protocol coverage; simulated results cannot be mistaken for hardware or whole-profile compliance |
| E07: useful live visibility | Evaluator can watch readiness, procedure/operation phases, relevant desired/observed differences, freshness and failure reasons while a run is active |
| E08: controlled experimentation | Evaluator can rerun a parameterized scenario, preserve both runs, and compare changed inputs, stage timings, outcomes and evidence gaps |

The immediate milestone is a repeatable end-to-end demonstration of one supported operation, followed by one deliberately induced failure and recovery. Expand protocol procedures and pod capabilities after this evidence chain works.

### 18.6 Required visibility and experiment controls

**EVAL-12.** Provide a live CLI view (`watch`) showing controller/virtual-agent identity, physical pod binding, connection/schema/ownership readiness, procedure and operation phase, freshness, and the most recent error or limitation. Missing telemetry appears as unknown/unsupported, never as a fabricated zero. The view identifies the actual backend and initiating controller.

**EVAL-13.** Provide per-operation inspection with a correlated timeline: received EasyMesh message, decoded supported intent, selected mapping, redacted OVSDB change/results, pod observations and independent client checks. Link available packet captures and artifacts. Preserve the distinction between the protocol response, database acceptance and actual application.

**EVAL-14.** Scenarios expose supported target/BSS settings, controller/backend profile, management topology, fault schedule, deadline budgets, seed and repetition count as validated inputs. Every execution creates an immutable run record with effective inputs. Replaying recorded observations is labelled replay; rerunning a scenario on hardware starts a new experiment and records its initial conditions.

**EVAL-15.** Provide run comparison in machine-readable and human-readable form. Show changed inputs/builds/mappings, per-check verdicts, phase timings, connectivity interruption and evidence availability. Highlight uncontrolled differences such as a different firmware or wireless topology rather than attributing every change to the adapter. Missing measurements remain explicit.

**EVAL-16.** Reports separate platform execution status from interoperability verdict. A completed experiment may demonstrate unsupported behavior, a timing failure, a need for manual repair or a mapping gap. Classify the observed failure layer and uncertainty with evidence; do not relabel a failed compatibility test as success merely because the runner worked. Coverage of the stated hypothesis and the seamlessness dimensions in Section 1.3 must be visible.

## 19. Third-party evaluation

### 19.1 What is being evaluated

The system under test is the EMOSA virtual-agent service plus its OVSDB adapter and explicitly enabled telemetry extensions. An independent evaluator can assess reproducibility using the included controller, then assess wire interoperability using an independently implemented controller. These are separate results.

| Evaluation | Setup | Supported conclusion |
| --- | --- | --- |
| Independent reproduction | Another team builds/loads EMOSA and runs the documented suite with the included controller | The results are reproducible outside the developer's environment |
| Independent protocol evaluation | Independently derived vectors/parser and a frozen specification matrix | Evidence about the selected encodings and procedure rules |
| Independent controller interoperability | Third-party controller communicates with unmodified EMOSA over the isolated Ethernet boundary | The named controller/build works with the tested EMOSA revision and declared procedure subset |
| Physical-pod evaluation | Independently checked state and client behavior on identified unchanged pods | The specific mapping and tested physical behavior work on those models/builds/topologies |

```text
Third-party controller / evaluator's test driver
                 │ real IEEE 1905 / EasyMesh, observed independently
                 ▼
        Unmodified EMOSA build under test
                 │ observed OVSDB results / optional qualified telemetry
                 ▼
      Simulator or unchanged physical OpenSync pod
                 │
       Evaluator's independent Wi-Fi test client
```

**TP-01.** Publish the protocol version/subset, supported operations, capability limits, virtual identity model and tested firmware/schema matrix before the run. Agree the claimed acceptance scope in advance. Mandatory prerequisites missing from that scope prevent it from passing; a narrower passing subset must be reported as such.

**TP-02.** Allow the evaluator to replace the bundled controller by attaching a physical or virtual Ethernet interface to the isolated lab bridge. Document interfaces, multicast/unicast delivery, MTU, addressing, peer selection and packet capture points. Do not require the evaluator to call EMOSA's internal Python API or modify the controller's protocol logic. Environment configuration is recorded separately from code changes.

**TP-03.** Disable the bundled controller when an external controller owns the experiment. Check for unintended controller announcements and stale lab sessions. Multiple-controller conflict scenarios are explicit fault tests. Extend the isolated bridge to an external host only through the documented test link; do not expose it on the real pod management network.

An external controller may require procedures beyond the first EMOSA subset before it permits provisioning. Record that incompatibility. Do not bypass its mandatory checks and count the modified path as successful interoperability with the original controller. The evaluator may use a manual controller UI or a documented automation API; the driver records how each action was initiated.

### 19.2 Evaluator package

**TP-04.** Supply a release manifest containing source revision, image/dependency versions and digests, build/run instructions, protocol matrix, schema/mapping fixtures, scenario versions, expected outcomes and resource requirements. Include dependency/license notices and identify any external tooling the evaluator must obtain separately. Do not redistribute restricted specifications, firmware images or credentials as fixtures.

**TP-05.** Provide a simulator-only quick start, a hardware setup guide, an external-controller attachment guide, example redacted reports, known limitations and a recovery/cleanup runbook. Generate local test credentials where appropriate and keep hardware secrets out of the distributable package. Document architecture/host prerequisites rather than claiming the VM image runs everywhere.

**TP-06.** The evaluator MUST be able to gather evidence independently of EMOSA's pass/fail flag: packet captures, semantic event journal, sanitized configuration/state differences and client association/authentication/traffic observations. Correlate them by run, virtual agent, pod and operation identity. Provide a versioned result schema and artifact hashes. Preserve failures and incomplete runs; do not retain only successful repetitions.

### 19.3 Independent test procedure

1. **Freeze the target.** Record the EMOSA build, external-controller build, declared protocol subset, pod model/build and initial ownership state. Verify that the declared scope can be exercised without mandatory missing prerequisites.
2. **Reproduce simulation.** Bring up the package in a clean environment and run the baseline and negative scenarios against simulated OVSDB. Confirm that no fixture response is used in hardware mode.
3. **Replace the peer.** Stop the internal controller, attach the evaluator's controller, and inspect discovery/capability/provisioning exchanges through an independent capture point.
4. **Check physical results.** Repeat the selected flow on an unchanged pod using wired management and then the qualified wireless path, with the evaluator's separate Wi-Fi client.
5. **Challenge failure handling.** Include an invalid provisioning exchange, unsupported request, dropped reply, management loss and restart. Add independently selected timing/input cases within the declared scope to avoid relying only on the developer's happy-path fixtures.
6. **Publish scoped results.** Preserve inputs, observed results, failures, limitations, retest history and cleanup status. Measure stage timings and test counts; state what was simulated and what was physically observed.

**TP-07.** Third-party interoperability acceptance requires an independently implemented wire peer for the claimed procedures. An outside team running both EMOSA endpoints is an independent reproduction, not independent implementation interoperability. A parser check alone does not establish full state-machine interoperability.

**TP-08.** Reports MUST identify the evaluator, all relevant builds, procedure coverage, required/optional test classifications, management topology, trial counts and excluded cases. A code change during evaluation creates a new build/run identity. Do not generalize a pass to untested controller families, EasyMesh profiles, pod builds or certification.

### 19.4 Third-party acceptance

| Test | Pass condition |
| --- | --- |
| T01: reproducible handoff | Evaluator can start the simulated baseline from supplied artifacts/instructions without private developer services |
| T02: interchangeable controller | Independent controller drives the scoped real packet procedures against unmodified EMOSA; no internal-API shortcut or hidden test-controller traffic |
| T03: independent hardware evidence | Evaluator observes actual OVSDB/state changes and verifies intended BSS behavior using its client on the recorded physical topology |
| T04: meaningful negative results | Invalid/unsupported input, lost reply and disconnection produce correct protocol and operation outcomes with no unintended write or fabricated success |
| T05: auditable report | Report includes coverage, prerequisite gaps, failures, raw-evidence references/digests, timing methodology and cleanup status |

X1 is a separate qualification milestone after a working local baseline. An independent controller is an external test peer, not a required prplMesh dependency. No third-party assessment or certification has been conducted as part of this design work.

## 20. Assessment of the supplied cloud-adapter proposal

The supplied text contains useful architectural ideas, but its generic source labels do not establish the detailed mappings. The decisions below use the pinned upstream schema/code and official connection documentation; actual pods still require qualification.

| Proposal | Assessment and EMOSA decision |
| --- | --- |
| Keep adaptation in the controller environment | Retain. The physical pods remain unchanged and all protocol termination/mapping is local to EMOSA. |
| Connect the controller through internal structures/IPC only | Useful for component tests, but insufficient for the confirmed evaluation goal. Main tests and third-party controllers use actual IEEE 1905/EasyMesh frames. |
| Controller must host the pods' OVSDB database server | Correct the role distinction. The pod database server remains on the pod; EMOSA issues management requests even if it accepts the underlying TCP connection. A local test database is used for simulated pods. See Section 3.3. |
| Fixed OVSDB/MQTT ports and DHCP Option 43/60 or DNS redirection | Treat as unverified deployment suggestions. Use the confirmed management path, actual port/trust configuration and supported endpoint controls. No evidence establishes those DHCP mechanisms for the user's pods. |
| MQTT/Protobuf metrics consumer | Retain as an optional extension when required measurements are available through an authorized existing telemetry path. A new broker is not mandatory for initial provisioning tests. See Section 8.4. |
| WSC credentials map to Wi-Fi configuration | Retain with the actual cryptographic procedure, field/security mapping and application checks. Do not blindly write both modern and legacy security encodings or change an existing VIF's mode. |
| Channel/width/power are simple table assignments | Table fields are candidates, not a complete operation mapping. Validate the selected message's exact requested values, units, regulatory constraints, radio-wide effect and backhaul impact. Do not change transmit power merely because a channel request arrives. |
| Client steering maps to `Band_Steering_Config` or `Wifi_Route_State` | `Wifi_Route_State` contains IP route fields such as destination, gateway and metric; it is not a steering interface. The pinned `Band_Steering_Clients` schema/code exposes candidate controls such as `cs_mode`, `cs_params`, `cs_state`, BTM parameters and `force_kick`. Their presence warrants investigation, not a promise of exact EasyMesh steering semantics or completion reporting. |
| Associated metrics come from two State tables | `Wifi_Associated_Clients` and `Wifi_Radio_State` do not expose all desired client link metrics in the inspected schema. Map each metric to an actual source, including optional telemetry, with units and freshness. |
| Topology Discovery configures GRE/backhaul | Separate observation/discovery from network reconfiguration. A discovery message does not itself authorize GRE, route or uplink changes. Existing backhaul observation remains in scope; backhaul reconfiguration remains a separate extension. |
| Steering has a generic near-real-time deadline | Use the selected procedure's exact protocol deadlines and independently measure database, manager, radio and client stages. Do not infer performance from TCP latency or table-write success. |

The most useful additions are the optional telemetry consumer, explicit management-connection qualification, and independent external-controller evaluation. None requires abandoning Python or installing new code on the pods.

## 21. Coding-agent implementation handoff

### 21.1 Readiness and authoritative inputs

Use `../project/EMOSA-CODING-HANDOFF.md` as the execution guide and `../project/emosa-input-manifest.example.json` as the input checklist. This architecture remains the behavioral baseline. The handoff selects routine implementation defaults; it cannot silently weaken a requirement. Neither document is a substitute for the selected protocol specifications or evidence from the actual pods.

The project is ready to begin repository setup, the semantic model, state machine, journal, mock backend, scenario runner and OVSDB simulation. Three external gates remain:

| Gate | Required evidence | Work affected while missing |
| --- | --- | --- |
| Protocol gate P0 | Accessible exact specification revisions, chosen subset, populated message/TLV/timer matrix and independent vectors | Affected wire procedures/provisioning; no guessed encodings, timers or cryptography |
| Hardware gate M0 | Named pod/build, actual schema, endpoint direction/trust, managed VIF/radio, writer ownership and recovery path | Hardware qualification and writes; simulations remain unblocked |
| External-peer gate X1 | Named independent controller/build, compatible scope and evaluator environment | Third-party interoperability result; local development remains unblocked |

The implementer may resolve tool/library/image choices with a short compatibility experiment and record the result. This is implementation work, not a reason to wait for a user preference. Missing normative or device facts are different: mark them explicitly and continue unrelated work.

### 21.2 Operation, deadline and recovery rules

1. **Submission durability.** Record intent and a unique attempt/session association before sending a modifying transaction. Persist the result before reporting it through the local API. Restart must never treat a possibly sent request as definitely unsent.
2. **Idempotency scope.** Enforce uniqueness on `(request_source, pod_id, idempotency_key)`, where `request_source` is a configured local caller or bound virtual-agent/procedure identity. Same key and same normalized intent returns the original operation; different intent is rejected. Wire MID alone is not an idempotency key. A protocol procedure instance owns the mapping from retransmissions to a stable internal operation.
3. **Serialization.** Serialize writes affecting the same pod/radio, including its BSSs. Start with one writer queue per pod for simplicity. Validate state/schema/ownership again when a queued operation reaches submission, and use database preconditions as well as the local queue.
4. **Caller wait.** A CLI wait deadline only ends that caller's wait. It does not change the operation to `TIMED_OUT`, cancel a transaction or roll back configuration.
5. **Application deadline.** `TIMED_OUT` means a committed or otherwise sufficiently established operation did not meet its fresh application predicate before its own deadline. Unknown transaction outcome remains `INDETERMINATE`, with an elapsed-deadline flag, until evidence resolves it. A lost reply is never proof of failure.
6. **Late observations.** A scenario that missed its deadline retains that verdict. Store later current observations and a separate resolution such as `applied_after_deadline`; do not rewrite the original timing evidence to make it pass.
7. **Reconciliation evidence.** After a lost reply, matching Config/State establishes the current target condition, not necessarily which writer caused it. Preserve commit attribution as unknown unless separate evidence establishes it. Reconciled satisfaction may be recorded with that evidence qualifier; it does not retroactively prove timely transaction completion.
8. **Conflicts.** An ownership conflict prevents further writes to that scope until ownership is re-established. New legitimate intent is a new operation; do not endlessly retry an old one against it.
9. **No-op.** A request may finish as an observed no-op only when its qualified fresh state predicate already holds. Record `changed=false` and current-state evidence. A protocol response still follows that procedure's actual rules.
10. **Cancellation.** Core need only support cancellation before submission. A submitted operation cannot be claimed cancelled solely because the caller disconnects. Later compensation is separately planned and journaled.

### 21.3 Minimum persisted and public contracts

Define typed domain objects and versioned JSON schemas early. The initial required records are:

| Record | Required information |
| --- | --- |
| Operation | Schema version; operation/run/source IDs; initiating interface; pod/radio/BSS IDs; normalized intent fingerprint and protected secret references; mapping/schema versions; idempotency key; state/reason; timestamps/deadline; attempts/session generations; commit evidence; application predicate/evidence; late resolution; client-verification result |
| Observation | Pod/resource ID; observed values; source (`ovsdb`, qualified telemetry or simulation); backend mode; session generation; receive/source times; freshness and provenance |
| Event | Schema version; event ID and per-run sequence; UTC timestamp and process/clock identity; run/operation/pod IDs; phase; reason; redacted payload |
| Run result | Manifest, selected requirement/test IDs, expected versus observed outcomes, per-check verdicts, original and late outcome, evidence references/digests, timing samples, prerequisites and cleanup status |

Persist UTC timestamps in RFC 3339 form; use monotonic durations only within the originating process lifetime. Never assume monotonic timestamps from different hosts or restarts are comparable. Keep absent, unknown and zero distinct.

Resolve secret references through mounted local files in the first deployment. Do not store Wi-Fi keys in ordinary JSON/SQLite intent records. If restart reconciliation requires a secret that is no longer available, continue read-only observation, mark the operation blocked for resubmission and request the missing reference; do not guess it. Use a protected keyed fingerprint if secret equality is needed, not a public hash of a low-entropy password.

For the local Unix-socket API, use a versioned newline-delimited JSON request/response envelope with `schema_version`, `request_id`, `method`, and `params`; responses echo the IDs and contain exactly one of `result` or `error`. Errors contain stable `code`, readable `message`, and structured redacted `details`. Bound frame size and reject unknown versions/methods. This local protocol is independent of OVSDB JSON-RPC; do not alter the pod-facing protocol to match it. Polling operations/events is sufficient initially; streaming subscriptions are not required.

Each configuration/scenario/response schema has one authoritative validator. CLI, runner and tests use it; duplicated incompatible field definitions are not acceptable. Examples in this document become validated fixtures or are explicitly marked non-executable templates.

### 21.4 Definition of delivery

Track every numbered requirement and acceptance test in a machine-readable matrix with implementation/test/evidence references and status (`not_started`, `implemented`, `verified`, `blocked`, or `deferred_extension`). An implementation status alone is not a verification result. Every blocked row states the missing input and what can proceed independently.

Required extension work is limited to the selected delivery scope; MQTT telemetry, steering, channel actuation, VIF lifecycle, high availability and broad profile support remain deferred until explicitly selected. A foundation delivery can be complete while wire/hardware milestones remain blocked, provided the report says exactly that. EMOSA end-to-end acceptance still requires the real packet and hardware evidence defined earlier.

## 22. Direct incorporation of OpenSync core

### 22.1 Decision and reuse boundary

Incorporate selected pinned OpenSync core assets and evaluate a native-manager reference backend. Keep the EMOSA protocol/OVSDB adapter in Python. The reference backend runs in the controller's lab infrastructure and represents a simulated pod; it is not installed on a real pod and is not a replacement for the physical-pod backend.

```text
EasyMesh controller → EMOSA virtual agent + Python OVSDB adapter
                                         │ unchanged OVSDB boundary
                                         ▼
                        opensync-native test service/container
                          ├── disposable ovsdb-server + pinned schema
                          ├── selected upstream OpenSync OWM / OW / OSW
                          └── lab driver harness → simulated radio/BSS/client state
```

| Reuse candidate | Decision | What it provides / does not provide |
| --- | --- | --- |
| OVSDB schema and applicable seed data | Reuse with version/provenance and explicit synthetic identities | Real table/types; seed data must be checked against the lab target and is not discovered physical topology |
| Protobuf definitions | Reuse when the telemetry extension is selected | Payload decoding types, not an existing accessible stream or proof of measurement semantics |
| OWM/OW/OSW manager code and tests | Recommended separate feasibility experiment, then an additional backend if qualified | More representative upstream config/state processing than a homegrown manager model; still differs from vendor firmware |
| OSW dummy driver | Candidate foundation for the lab device harness | Radio/VIF/client state and driver callbacks; requires wiring and scenario behavior |
| `target_bsal_sim.c` | Candidate for later steering-specific tests | Simulated steering interface; not a general Wi-Fi provisioning backend |
| Entire default OpenSync manager stack | Outside the initial reference scope | Adds network, cloud, platform and service dependencies unnecessary for the first BSS experiment |
| Direct Python FFI into `libopensync.so` | Not the default | Couples event loops and internal APIs and bypasses the OVSDB boundary being evaluated |

OpenSync's OW Kconfig explicitly describes library embedding and cautions about thread safety. Process separation is an EMOSA design choice that preserves the external boundary and keeps failures isolated. See [OW configuration](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/src/lib/ow/kconfig/Kconfig.libs).

### 22.2 Source evidence and remaining work

The pinned release contains a [native OWM test script](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/src/owm/scripts/test.sh) that builds `src/owm` and a database using `TARGET=native`, sets a test OVSDB socket, and runs OWM under `ovsdb-server`. Its default mode runs unit tests; this is evidence of a useful build/test path, not an already functioning EMOSA pod emulator. Preserve the expected source layout when adapting it.

The [OSW dummy-driver API](https://github.com/plume-design/opensync/blob/78d8a7194d5e77635877cc456231e7be5cf03d68/src/lib/osw/inc/osw_drv_dummy.h) provides callbacks for configuration/statistics and functions for simulated PHY, VIF and station state. The implementation must instantiate/register it, provide the configuration callback and seed a consistent radio/VIF/client inventory. Merely compiling `osw_drv_dummy.c` does not create simulated radios automatically.

The [OpenSync FAQ](https://opensync.atlassian.net/wiki/spaces/OCC/pages/39920140758) describes native builds as a debugging/tool path and suggests a target with selected Kconfig/stub implementations for core-only work. Therefore R0 must record which manager/driver path actually runs and which platform functions are stubbed. The actual pods may use a different manager or vendor target implementation despite sharing a 6.6.0 version label.

**CORE-01.** Pin upstream commit, schema, Kconfig, native build dependencies, simulator harness revision and any patch series. Preserve LICENSE/NOTICE and dependency notices. Prefer a separate source checkout/build context and small documented lab patches; do not vendor the entire source tree into the Python package or silently track `master`.

**CORE-02.** Start only the selected managers needed for the qualified Wi-Fi config/state path. Use disposable database/state directories and isolated network namespaces. Do not run default boot scripts that can configure the developer host, cloud endpoints, firmware update or unrelated networking. Required privileges apply only to disposable lab resources.

**CORE-03.** For the native-manager profile, EMOSA must submit Config through its normal OVSDB session. The native manager consumes it and emits State based on driver feedback. The fault harness acts at the simulated driver boundary to delay/deny application or report events; it must not patch State directly merely to satisfy the test. Label every synthetic driver observation as simulated.

**CORE-04.** Keep the deterministic `model` and simple `ovsdb-sim` backends. Use the native backend to compare selected upstream behavior and identify differences in the simple model, without redefining hardware requirements to match a simulation. Native-manager tests do not replace an independent EasyMesh controller or real Wi-Fi client tests.

**CORE-05.** Qualify native OpenSync dependencies separately from the Python services, in its own Ubuntu LXD container. The pinned upstream Dockerfile uses Ubuntu 20.04 and a broad dependency set; inspect it as historical dependency information, without introducing Docker or copying every historical package/version into the new platform. Record actual build/runtime requirements, LXD image fingerprint and measured resource use. Native build difficulties do not block available physical-pod testing through OVSDB.

### 22.3 R0 bounded qualification experiment

1. Fetch the pinned core source into a separate test dependency directory and inspect the native configuration and OWM test script.
2. Build only the needed OWM/OW/OSW path and disposable database, with recorded dependency/Kconfig settings. Run the relevant upstream unit tests and retain their outputs.
3. Establish a lab target with one simulated radio and existing AP VIF using the dummy-driver API or a documented equivalent. Implement only the minimum required native harness glue; do not rewrite EMOSA in C.
4. Submit a qualified BSS configuration through the same Python OVSDB adapter. Show native-manager consumption, driver request, simulated application feedback and manager-generated State.
5. Repeat with delayed/withheld driver application and a database/session restart. Check that EMOSA reports the correct phases and uncertainty.
6. Record whether the backend is qualified for this operation. If dependencies or missing target hooks prevent it, record the exact blocker and continue I0–I4 using the simpler backends; do not conceal the failure by auto-falling back to fabricated State.

Stop expanding the native experiment once these criteria are established; telemetry, full networking and steering are separate future scope. This experiment can run after the basic OVSDB adapter exists and does not require physical-pod credentials. Wire-level acceptance still requires P0.

| Test | Pass condition |
| --- | --- |
| N01: reproducible native build | Pinned minimal build, manifest and relevant upstream test output; selected manager/driver stack documented |
| N02: authentic manager path | Config submitted through EMOSA causes the expected native manager/driver sequence and manager-produced observed State |
| N03: non-application is visible | Delayed/withheld driver feedback does not become false applied success; session restart resynchronizes correctly |
| N04: isolated and honestly labelled | No host or physical-pod changes; simulation provenance, native patches, limits and resource use recorded |

**Verification status:** the bounded [R0 experiment](../../deploy/native/README.md)
builds and runs the pinned target, passes 39 selected upstream tests, and exercises
native application/withholding with a dummy driver. N03 remains blocked by a
reproducible post-database-restart failure to process new Config. The native
application backend remains gated; wire and physical acceptance are still pending.
