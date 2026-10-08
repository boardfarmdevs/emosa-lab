# EMOSA's documents

[Repository](../README.md) · [Site](https://vcpe.dev/emosa-lab/)

EMOSA, the **EasyMesh to OpenSync Adapter**, is one of the two core components
of the [EasyMesh labs](https://mesh.vcpe.dev/): it makes
each unchanged OpenSync pod a complete EasyMesh agent under a local EasyMesh
controller, with no OpenSync cloud. This page maps the documents by what they
are for. **Reference** documents are normative and kept current; **guides** say
how to run things and are kept current; **records** are dated and not edited
afterwards; **learning** material shows how the project got here and may
describe older code.

## Start here: the contract (reference)

A team that builds or takes over EMOSA needs these, in this order; none of them
needs a lab.

| Document | What it fixes |
| --- | --- |
| [Specification](../spec/README.md) | what EMOSA does on each interface (MUST/SHOULD) |
| [Component design](../spec/design.md) | processes, interfaces, concurrency, state, timing and resource budgets |
| [Conformance vectors](../spec/conformance/README.md) | exact outputs for recorded inputs; an implementation conforms when it reproduces them |
| [The lab in a box](../spec/box-scenarios.md) | every behaviour the labs' rooms rely on and every suite finding, as a scenario both implementations run against a recorded pod, a scripted controller and a broker, with no lab |
| [Schemas](../schemas) | the configuration, status and record files |
| [The Python reference](../src/emosa) and [the C implementation](../c/README.md) | two implementations of the contract, interchangeable (the C's state and known differences are in its README) |
| [The C's production bar](../c/QUALITY.md) | the coding standard (CERT C) and the gates the C must meet, and where each stands |
| [The target system](concepts/target-system.md) | what EMOSA and the router must do for unchanged pods without the OpenSync cloud: what is shown, what is missing, whether the design scales |
| [Decisions](concepts/decisions.md) | recorded choices and qualification boundaries |

Where the specification and any other document disagree, the specification
wins.

## Run it (guides)

| Guide | For |
| --- | --- |
| [The adapter kit](../deploy/adapter/README.md) | installing EMOSA into an EasyMesh lab (both implementations) |
| [The reference lab](../deploy/opensync-lab/README.md) | EMOSA with a prplMesh controller on the OpenSync lab: several pods, the fault workload (`lab.sh workload`), the Wi-Fi backhaul |
| [Physical pods](guides/physical-pods.md), [their qualification](guides/pod-qualification.md) | an operator-provisioned pod into opensync-lab and EMOSA; a pod's read-only qualification (`emosa qualify-pod`) |
| [EMOSA in the RDK lab](concepts/rdk-lab.md) | the RDK lab's EMOSA option: its design, how to run it, pods in the lab's rooms, the room suite with them |
| [Repository README](../README.md) | what EMOSA does today, how to develop and test it |

## Architecture background (reference)

| Document | Covers |
| --- | --- |
| [Data plane](concepts/data-plane.md) | how the pods' client traffic reaches the LAN: GRE termination or the Multi-AP Wi-Fi backhaul |
| [OpenSync pods as EasyMesh agents](concepts/opensync-easymesh-mapping.md) | the fleet and the OVSDB ↔ EasyMesh translation |
| [Recovery semantics](concepts/recovery.md), [operation mappings](concepts/operation-mappings.md) | operations, idempotency, lost replies, restarts |
| [Architecture overview](concepts/overview.md), [requirements](reference/requirements.md) | the original architecture and requirements (version 3.6) the specification grew from |

## Records

- [Evidence](records/evidence/README.md): dated reports, captures and results; each
  keeps its date and scope. The latest: the reference workload with Python, C
  and mixed agents ([opensync-lab-proof](records/evidence/opensync-lab-proof/README.md)),
  and EMOSA's way into the RDK lab up to the room suite on either agent
  ([rdk-lab](records/evidence/rdk-lab/README.md)).
- [Handover](handover/README.md): for the team taking EMOSA over: architecture, module
  guide, coding standard, test guide, traceability, decision log
- [Project files](project/README.md): the third-party notices, the traceability
  matrix and the input manifest template. The current plan and status of all the labs
  are the easymesh-labs alignment plan (in [easymesh-labs](https://mesh.vcpe.dev/)).

## Learning

- [Protocol](reference/protocol/README.md): specification inputs, the protocol matrix, WSC.
- [Evaluation](records/README.md): native controller and agent baselines,
  OVSDB and hwsim observations, dependency qualification.

The [interactive explorer](https://vcpe.dev/emosa-lab/) presents
the architecture and results in a browser.
