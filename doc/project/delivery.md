# Foundation delivery and remaining acceptance gates

The working delivery covers the I0 package/contracts/bootstrap, I1 durable
semantic operation engine, I2 real OVSDB component simulator, incremental I6
evaluation tooling and a read-only physical-pod preparation command. It does
**not** complete EMOSA's wire/physical/interoperability acceptance objective.
Every architecture requirement and acceptance row is listed in
`traceability.json`; verification is scoped to the recorded mode.

## IEEE envelope and discovery/WSC increment

Both IEEE 1905 PDFs are obtained. The [envelope component](../protocol/ieee1905-envelope.md)
implements bounded Ethernet/CMDU transport and inspection. The
[autoconfiguration component](../protocol/autoconfiguration.md) adds selected
Search/Response and M1/M2 exchanges, explicit peer/radio generation binding,
complete-request checks, duplicate handling and expiration. Retained native
traffic exposes profile and capability gaps. No operation engine or pod writes
are enabled by these components; full topology/capability/coordinator procedures
and qualification remain incomplete. Current evidence is in the
[exchange collection](../evidence/autoconfiguration/README.md).

## Capability and topology report increment

The [report components](../protocol/reports.md) add restricted complete Early AP
Capability Report and Topology Response construction, IEEE device/bridge/neighbor
values, security selectors, BSS configuration and associated-client values.
Identity, complete-inventory, source-change and deadline checks guard delivery.
An offline exercise and two isolated VM AF_PACKET runs decode the advertised
radio/BSS at a synthetic receiver. The [new evidence](../evidence/reports/README.md)
retains 824 unit and 46 OVSDB passes, native field comparisons and zero-operation
wire gating. Native capture review identifies missing cipher/bridge fields and a
Wi-Fi 6 media-length mismatch against the selected edition. A running controller
coordinator, full AP Capability procedure and actual controller inventory remain
pending; no physical pod was contacted or changed.

## Secure fleet and clean reproduction increment

The [secure-fleet workflow](../guides/secure-fleet.md) adds authenticated
pod-initiated TLS, per-pod certificate/serial bindings, bounded handshake rejection,
4/8/16/32 real database sessions through one service, resource/latency observations
and repeated crash/reconnect/database/late-State/conflict checks. The
[clean runtime workflow](../../deploy/reliability/README.md) repeats these from an
installed wheel in a fresh nested-LXD container and retains a private image export.
Current results and preserved earlier failures are in
[reliability evidence](../evidence/reliability/README.md). The
[learning sequence](../guides/learning-path.md) and manual explain each stage.
Earlier delivery tables below remain historical; they are not the current suite
counts or a statement that the new component runtime has not been exercised.

## Explorer and radio-lab increment

The [interactive field guide](https://boardfarmdevs.github.io/emosa-lab/) adds a
clickable architecture, lab commands, six retained run timelines/comparisons and
searchable traceability. It centers the intended proof: a real EasyMesh
controller discovers and onboards an unchanged OpenSync extender as an agent
represented by EMOSA, then manages it through the adapter. See the
[viability roadmap](viability-roadmap.md).

The optional [hwsim harness](../../deploy/hwsim/README.md) was executed in a new
dedicated `emosa-lab` VM, with separate unprivileged AP/station containers.
A clean setup/retest passed WPA2 authentication, three wireless-interface-bound
pings and virtual-medium capture. Four unprivileged tests cover VM mutation
guards and cleanup ownership. [Retained evidence](../evidence/hwsim/qualification-summary.json)
includes the initial service-startup failure. Existing unrelated LXD instances
and physical pods were not modified.

This radio smoke is separate from EMOSA's OVSDB manager simulator. Full reference
application deployment, package/image exports, controller-visible onboarding,
physical-pod acceptance and independent-peer interoperability remain pending.
The tables below preserve the earlier foundation delivery's validation scope.

## Native OpenSync R0 increment

The [bounded native experiment](../../deploy/native/README.md) builds the pinned
OWM/OW/OSW stack on Ubuntu 24.04 and passes 39 selected upstream tests. A normal
EMOSA Config transaction reaches the native driver callback; synthetic feedback
produces native State. Withheld feedback yields a timeout. A fresh source rebuild
reproduces a recovery failure: Config changes after database restart stop reaching
the driver. N03 remains blocked and the application native backend stays disabled.
These results add component evidence, with no EasyMesh or physical-pod acceptance.

## Implemented behavior

- CPython 3.13.7, locked uv package, three console entry points, strict versioned
  contracts, Ruff/pytest commands and component CI jobs.
- Guarded operation transitions, scoped idempotency, FULL-synchronous SQLite
  journal, process lock, per-pod serialization, private secret references/HMAC
  fingerprints, caller/application deadline separation and restart recovery.
- Independent desired/committed/observed states, partial/rejected/delayed device
  feedback, observed no-op, durable conflicts, lost-outcome reconciliation and
  late evidence that preserves the original deadline failure.
- Upstream OVS Python sessions in bounded workers; real schema/monitor handling,
  guards, per-operation result checks, map-key preservation, reconnect and both
  tested socket directions. The simulator manager runs in a separate process.
- Immutable completed runs, parameterized reruns, bounded faults, live watch,
  operation inspection, JSON/HTML/Markdown reports, comparisons and evidence hashes.
- Read-only actual endpoint collection with private credential references,
  mutual TLS/peer pin checking, a restricted noncredential monitor and a draft
  profile. Simulator tests establish collector behavior, not physical qualification.
- LXD VM/inner-container setup scripts, explicit VM-local `lxc exec` runner
  routing and a restricted systemd adapter service. The reference deployment
  has not yet been executed/qualified.

## Actual validation

| Check | Result | Evidence |
| --- | --- | --- |
| `uv sync --frozen` | Passed on inspected development host | `bootstrap.json`, `uv.lock` |
| Ruff lint and format | Passed | Final local checks |
| `uv run pytest -m unit` | 40 passed, 15 deselected | `../evidence/unit-results.xml` |
| `uv run pytest -m ovsdb` | 13 passed, 42 deselected | `../evidence/ovsdb-results.xml` |
| Collector checks after schema-artifact correction | 3 passed | `../evidence/qualification-results.xml` |
| Local API checks after read-only capability correction | 2 passed | `../evidence/api-results.xml` |
| Installed wheel outside source checkout | CLI help, model run, contracts, schema, real OVSDB read-only collection passed; final API correction separately tested and wheel rebuilt | `../evidence/wheel-smoke.json` |
| Explicit wire-suite selection | 1 prerequisite failure, no wire exchange executed | `../evidence/wire-gate.xml`, `../evidence/wire-gate.log` |
| Hardware/external suites | Not executed against devices/peers; inputs absent | `open-inputs.md` |
| Native OpenSync R0 | Build stopped at missing `protoc-c`, after resolving isolated Python/OVSDB-tool prerequisites | `../evidence/r0-manifest.json`, build logs |
| Ubuntu 24.04 nested-LXD runtime | Not executed; candidate image fingerprints resolved | `../../deploy/images.lock.json` |

The suites overlap for the pure transaction-result validation test; do not add
their counts to infer a unique-test total. Four real simulated database sessions
and 32 deterministic model pods are tested. No host daemon/packages/networking,
existing LXD instance, physical pod or cloud writer configuration was changed.

## Preserved experiments

Each directory below contains its inputs, events, observations, result, reports
and artifact manifest. Private secrets and SQLite state remain under the ignored
local `.lab` run directories and are not copied into distributable evidence.

| Run | Outcome | Scope |
| --- | --- | --- |
| `run-65264f5d645e4bb7` | Component pass | Baseline SSID/PSK change through real OVSDB and independent simulated manager |
| `run-71210fdc76724ca2` | Component pass | Lost reply, resynchronization, current state satisfied with unknown commit attribution |
| `run-1f7127f0752d4a69` | Component **fail** | Same recovery with a deliberately too-short 0.01-second apply deadline; late application does not turn it into a pass |
| `run-9fe901669f6f4eca` | **Blocked** | Genuine EasyMesh provisioning requested; no semantic fallback or pod write |
| `run-6bde1516e5784e72` | Earlier-build failure retained | Simulated manager reconnect handling failed during server-restart scenario |
| `run-4023fccf9a144b47` | Successful retest | Test manager lifecycle now follows simulated pod/server restart and rebuilds its observation session |

All are under `../../doc/evidence/runs`. Different source hashes identify different builds.
The passing lost-reply run's CLI watch/inspection output and a baseline/recovery
comparison are also retained in `../../doc/evidence`. Reports keep interoperability
`not_evaluated` for semantic experiments, even when the component expectation
passes. No packet capture, real radio metric or independent client result was
fabricated.

## Remaining work

P0 research proposes **IEEE 1905.1-2013 + 1905.1a-2014, EasyMesh 6.1 and WPS
2.0.10**, with a Profile-1 procedure subset. The WFA PDFs were obtained from the
publisher and hashed; the proposal is not a frozen selection. IEEE base/amendment
and 802.11-2024 texts are now obtained and hashed; selected clauses and bounded
components have been implemented. Complete the remaining normative procedure
matrix, profile qualification and endpoint/operation integration for I3/I4. See
`../protocol/protocol-inputs.md` for authoritative links, sections and document ambiguities.

The WSC radio-wide BSS semantics are a concrete scope constraint. Qualify a radio
with only the selected existing BSS, or implement the complete requested radio
configuration. A partial VIF patch on an arbitrary shared radio cannot establish
seamless provisioning.

M0 still needs the named physical pod/build, trusted endpoint, actual schema,
managed resources, verified cloud/local writer controls, management/recovery path
and independent client. The next available device step is the read-only command
in `../guides/pod-qualification.md`; its draft never enables writes automatically. X1 needs
a named independent controller/build and evaluator after the wire/hardware baseline.

The next independent platform work is the Ubuntu 24.04 nested-LXD compatibility
run with retained images and package manifests. Optional R0 can continue in its
separate container with pinned C dependencies and a real dummy-driver harness.
These are not substitutes for the acceptance chain: **real EasyMesh messages →
EMOSA adapter → unchanged physical pod → independently observed behavior**.
