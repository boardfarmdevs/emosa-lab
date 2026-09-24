# EMOSA Lab documentation

EMOSA is the **EasyMesh to OpenSync Adapter**. EMOSA Lab contains the adapter,
simulators and experiments used to evaluate it. Start with the
[team manual](guides/team-manual.md) for setup, exercises and demonstrations, or
the [architecture overview](architecture/overview.md) for the main building blocks.
New team members should follow the [learning sequence](guides/learning-path.md):
model → real database → persistent service → authenticated TLS → fleet/recovery
measurements → clean LXD reproduction → wire envelope/discovery/reports → read-only report coordination →
discovery-to-topology lifecycle → durable WSC handoff → Ethernet WSC driving
hwsim and independent clients → eventual native-controller-to-physical-pod proof.

## Current status and next integration

Start with [current status](project/current-status.md), then the
[OpenSync integration plan](project/opensync-lab-integration-plan.md),
[frozen baseline](project/integration-baseline.md) and
[separate acceptance levels](project/integration-acceptance.md).
The actual OpenSync container integration has not been executed.

## Guides: learn, run and demonstrate

[Browse guides](guides/README.md) for the full team manual, the simulated extender
that connects to EMOSA, and read-only physical-pod preparation.

## Architecture: understand the implementation

[Browse architecture](architecture/README.md) for the overview, authoritative
requirements, OpenSync operation mapping, recovery behavior and design decisions.

## Protocol: specifications and procedure boundaries

[Browse protocol documentation](protocol/README.md) for selected specification
inputs, the acquisition checklist, the protocol matrix and WSC components.

## Evaluation: interpret experiments and profiles

[Browse evaluation](evaluation/README.md) for native controller/agent baseline
results, OVSDB/hwsim integration, dependency qualification and pod profile scope.

## Project: implementation plan and current gaps

[Browse project records](project/README.md) for the coding handoff, delivery
status, viability roadmap, open inputs, traceability and third-party notices.

## Evidence: inspect retained results

[Browse evidence](evidence/README.md) for reviewed reports, captures, test results
and artifact hashes. Historical evidence keeps its original dates and scope.

## Other entry points

- [Interactive explorer](https://boardfarmdevs.github.io/emosa-lab/): architecture,
  experiment modes, retained results and searchable references.
- [Deployment entry point](../deploy/README.md): VM/container preparation and
  links to the specialized native and radio harnesses.
- [Repository README](../README.md): quick start and implementation status.

The acceptance goal remains **real EasyMesh messages → EMOSA → unchanged physical
pod → independently observed behavior**. A simulator pass or local diagnostic
agent record does not complete wire onboarding or physical-pod qualification.
