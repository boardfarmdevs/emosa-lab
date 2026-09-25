# Architecture and behavior

[Documentation index](../README.md)

| Document | Scope |
| --- | --- |
| [Architecture overview](overview.md) | Main building blocks, current implementation and intended acceptance path |
| [OpenSync pods as EasyMesh agents](opensync-easymesh-mapping.md) | The fleet, the OVSDB ↔ EasyMesh/1905.1 translation, and plugging into an existing controller |
| [The data plane](data-plane.md) | How pods' client traffic reaches the gateway LAN today, and in RDK/prpl: GRE termination (baseline) or EasyMesh backhaul (optional) |
| [Connection-flow comparison](connection-flows.svg) | OpenSync and gateway EasyMesh side by side, then multi-pod EMOSA control and telemetry toward the network-center data lake |
| [Architecture requirements](requirements.md) | Authoritative version 3.6 requirements, operation contracts and acceptance criteria |
| [Operation mappings](operation-mappings.md) | Qualified synthetic field updates, resource binding and observed application |
| [Recovery semantics](recovery.md) | Idempotency, lost replies, deadlines, restart and late observations |
| [Implementation decisions](decisions.md) | Recorded choices and qualification boundaries |

For the implementation order and requirement-to-evidence mapping, see the
[coding handoff](../project/EMOSA-CODING-HANDOFF.md) and
[traceability matrix](../project/traceability.json).
