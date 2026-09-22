# EMOSA Lab — Integration and Evaluation

![EMOSA — エモさ — Emotional resonance: a Japanese riverside at sunset](assets/emosa-banner.png)

EMOSA Lab is the integration and evaluation platform for **EMOSA (EasyMesh to OpenSync Adapter)**. This repository contains the adapter implementation and tools for exploring whether a controller-side adaptation layer can make **unchanged OpenSync pods work seamlessly with an EasyMesh controller**. OVSDB is the current OpenSync management interface used by the adapter.

The concrete proof is an EasyMesh controller **discovering and onboarding an
OpenSync extender as another EasyMesh agent**, represented by EMOSA around the
controller. The extender remains an OpenSync device; the adapter translates
supported management procedures to its existing OVSDB interface.

The goal is to demonstrate what works, expose compatibility gaps, and provide repeatable experiments with clear visibility into protocol exchanges, configuration changes, and actual device behavior.

The name also echoes **エモさ (*emosa*)**, a Japanese expression for emotional resonance, often with a nostalgic feeling. The banner illustrates this wordplay; see [Sanseido's explanation of エモい (*emoi*)](https://dictionary.sanseido-publ.co.jp/topic/shingo2016/2016Best10.html), from which エモさ is formed.

The IEEE 1905.1-2013 and 1905.1a-2014 PDFs are now obtained. Follow the
[wire-envelope learning exercise](doc/protocol/ieee1905-envelope.md) to inspect
native captures and test bounded reassembly and isolated Ethernet delivery.
Continue with [controller discovery and WSC exchange handling](doc/protocol/autoconfiguration.md)
for peer/radio binding, replay controls and the actual native compatibility findings.
Then construct and inspect [capability and topology reports](doc/protocol/reports.md),
including the new offline and isolated Ethernet exercises. Complete controller
onboarding and physical acceptance remain pending.

## Architecture

[**Documentation index and subject areas**](doc/README.md)

[**New team members: step-by-step setup, operator and demo manual**](doc/guides/team-manual.md)

[**Start here: the new learning sequence**](doc/guides/learning-path.md) ·
[Secure fleet and recovery exercises](doc/guides/secure-fleet.md) ·
[Clean runtime reproduction](deploy/reliability/README.md)

[Interactive explorer & lab manual](https://boardfarmdevs.github.io/emosa-lab/) ·
[Architecture diagram and boundaries](doc/architecture/overview.md) ·
[Next viability experiments](doc/project/viability-roadmap.md)

[Simulate an extender connecting to EMOSA](doc/guides/connecting-pod.md): pod-initiated
OVSDB, a local northbound virtual-agent directory, semantic configuration and
reconnect. Actual EasyMesh-controller discovery/onboarding remains pending.

```text
EasyMesh controller
        │ Real IEEE 1905 / EasyMesh messages
        ▼
EMOSA virtual agent + OpenSync mapping
        │ Existing OVSDB management interface
        ▼
Unchanged OpenSync pods
```

All adaptation runs in the controller environment. Pods require no firmware changes or additional software; configuration uses their existing authorized management interfaces. EasyMesh procedures terminate at EMOSA's virtual agents, while the physical pods remain OpenSync devices.

## Planned evaluation capabilities

- Real EasyMesh message handling and translation into supported OpenSync operations.
- Physical-pod testing over wired and wireless management paths.
- Deterministic simulations and an optional backend using selected OpenSync core components.
- Configurable scenarios, fault injection, repeatable runs, and result comparisons.
- Live status and correlated evidence across EasyMesh messages, OVSDB transactions, pod state, and independent Wi-Fi client checks.
- Evaluation with independent EasyMesh controllers.

Success is assessed per procedure, controller version, and pod firmware. Unsupported behavior and negative results are part of the evaluation; full EasyMesh interoperability or certification is not assumed.

## Implementation direction

- **Language:** Python for the controller tools and adapter.
- **Reference lab:** an LXD VM with Ubuntu LXD containers.
- **Initial device target:** existing OpenSync 6.6.0 pods.
- **Dependencies:** no prplMesh build-time or runtime dependency in EMOSA.

The first milestone is controller-visible discovery and onboarding, followed by
one supported BSS provisioning change and a failure/recovery experiment, with
independently verifiable results on the unchanged extender.

## Status and quick start

The Python foundation and direct semantic component evaluation are implemented.
The real OVSDB simulator uses the pinned OpenSync schema and a separate manager
process. Wire provisioning, physical-pod mapping and independent-controller
acceptance remain gated; simulator passes do not establish interoperability.

The [WSC payload component](doc/protocol/wsc-messages.md) builds M1 and authenticates
M2 AP settings against its exact bytes, with independent hostap fixtures. It
checks complete sets of payloads before returning settings. The bounded exchange
component now adds peer/link, RUID and transcript-lifetime checks. Controller trust,
complete profile/coordinator behavior and qualified radio admission still precede
connecting those results to OVSDB.

The [independent controller candidate](deploy/peer/README.md) now emits real
discovery frames captured at the EMOSA container. Its agent inventory is empty:
EMOSA has not answered or onboarded an extender. Ubuntu 24.04 nested-container
component tests and VM-driven semantic scenarios pass; the peer's shutdown
aborts are recorded as an unresolved recovery issue.

The separate [native controller–agent baseline](doc/evaluation/peer-baseline.md) exercises
that controller against a normal prplMesh agent over Ethernet and hwsim wireless
backhaul, with independent wired and wpa_supplicant client containers. It retains
real discovery/WSC captures, applied BSS configuration, client traffic and failed
restart procedures. This establishes the existing peer's behavior; EMOSA and
OpenSync are absent from that experiment.

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest -m unit
bash scripts/build-ovsdb.sh
uv run pytest -m ovsdb
uv run emosa-lab run scenarios/component-bss-change.json --backend ovsdb-sim
uv run emosa-lab run scenarios/component-lost-reply.json --backend ovsdb-sim
uv run emosa-lab report RUN_ID --format json
uv run emosa-lab watch RUN_ID
uv run emosa-lab inspect RUN_ID --operation OPERATION_ID
uv run emosa-lab compare RUN_A RUN_B --format html
```

`--backend model` runs deterministic model scenarios without OVSDB binaries or
privileged resources. Each run retains its journal, effective inputs, redacted
observations, HTML/Markdown timeline and artifact hashes under `.lab/runs/`.
Fault scenarios cover conflicts, partial/withheld application, lost responses,
controller/server restart and multi-pod isolation. `--ssid`, `--seed`, `--repeat`
and `--apply-seconds` create parameterized reruns with new identities.

`scenarios/provision-one-bss.json` and `scenarios/lost-reply.json` explicitly
require genuine EasyMesh provisioning. They currently produce blocked reports,
exit 5, and perform no semantic fallback. `pytest -m wire`, `-m hardware` or
`-m external` fails explicitly at its missing gate instead of reporting success
with no applicable tests. Default CI runs unit/OVSDB component suites and rebuilds
the independent hostap WSC reference vectors; it never selects hardware tests.

Prepare actual pod evidence without changing it using
[read-only qualification](doc/guides/pod-qualification.md). Review
[deployment](deploy/README.md), [mapping scope](doc/architecture/operation-mappings.md),
[dependency/R0 findings](doc/evaluation/dependency-qualification.md),
[WSC component scope](doc/protocol/wsc-component.md),
[open inputs](doc/project/open-inputs.md), and [traceability](doc/project/traceability.json).
The Ubuntu 24.04 LXD layout has pinned base images. The separate
[secure-fleet runtime](deploy/reliability/README.md) builds and retains an installed
wheel/container image for TLS, fleet and recovery reproduction. This is a semantic
component runtime; full wire/physical application deployment remains pending.

The [service integration walkthrough](doc/guides/service-integration.md) adds two
connecting pods through one adapter, actual service-process crash recovery with
hwsim clients, guarded restoration after VM reboot, and a live controller
preparation that explicitly retains the missing wire connection.

The [integrated OVSDB/hwsim experiment](doc/evaluation/radio-manager.md) now connects the
semantic EMOSA engine to a separate hostapd/nl80211 manager. Three repeat runs
passed 13 change, failure and recovery cases with independent wired and wireless
clients. Genuine EasyMesh initiation and physical-pod qualification remain pending.

When a simulated radio is useful, use the optional
[mac80211_hwsim AP and wpa_supplicant LXD client](deploy/hwsim/README.md).
That standalone radio smoke harness is separate from the current OVSDB simulator.
Physical-pod acceptance requires an independent client with a real Wi-Fi interface.
