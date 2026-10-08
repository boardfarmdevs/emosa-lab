# Architecture and behavior

[Documentation index](../README.md)

| Document | Scope |
| --- | --- |
| [Component design](../../spec/design.md) | Processes, interfaces, thread model, state, timing and dependencies: what an independent team builds to |
| [Architecture overview](overview.md) | Main building blocks, current implementation and intended acceptance path |
| [OpenSync pods as EasyMesh agents](opensync-easymesh-mapping.md) | The fleet, the OVSDB ↔ EasyMesh/1905.1 translation, and plugging into an existing controller |
| [The target system](target-system.md) | What EMOSA and the router must do for unchanged pods without the OpenSync cloud: what is shown, what is missing, and whether the design scales |
| [The data plane](data-plane.md) | How pods' client traffic reaches the gateway LAN today, and in RDK/prpl: GRE termination (baseline) or EasyMesh backhaul (optional) |
| [Connection-flow comparison](connection-flows.svg) | OpenSync and gateway EasyMesh side by side, then multi-pod EMOSA control and telemetry toward the network-center data lake |
| [Architecture requirements](../reference/requirements.md) | Authoritative version 3.6 requirements, operation contracts and acceptance criteria |
| [Operation mappings](operation-mappings.md) | Qualified synthetic field updates, resource binding and observed application |
| [Recovery semantics](recovery.md) | Idempotency, lost replies, deadlines, restart and late observations |
| [Implementation decisions](decisions.md) | Recorded choices and qualification boundaries |

For the requirement-to-evidence mapping, see the
[traceability matrix](../project/traceability.json); for the specification-to-test map,
the [handover's traceability](../handover/traceability.md).
