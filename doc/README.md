# EMOSA documentation

EMOSA, the **EasyMesh to OpenSync Adapter**, is one of the two core components
of the [EasyMesh labs](https://boardfarmdevs.github.io/easymesh-labs/): it makes
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
| [Schemas](../schemas) | the configuration, status and record files |
| [The Python reference](../src/emosa) and [the C implementation](../c/README.md) | two implementations of the contract, interchangeable (the C's state and known differences are in its README) |
| [The C's production bar](../c/QUALITY.md) | the coding standard (CERT C) and the gates the C must meet, and where each stands |
| [The target system](architecture/target-system.md) | what EMOSA and the router must do for unchanged pods without the OpenSync cloud: what is shown, what is missing, whether the design scales |
| [Decisions](architecture/decisions.md) | recorded choices and qualification boundaries |

Where the specification and any other document disagree, the specification
wins.

## Run it (guides)

| Guide | For |
| --- | --- |
| [The adapter kit](../deploy/adapter/README.md) | installing EMOSA into an EasyMesh lab (both implementations) |
| [The reference lab](../deploy/opensync-lab/README.md) | EMOSA with a prplMesh controller on the OpenSync lab: several pods, the fault workload (`lab.sh workload`), the Wi-Fi backhaul |
| [EMOSA in the RDK lab](architecture/rdk-lab.md) | the RDK lab's EMOSA option: its design, how to run it, pods in the lab's rooms, the room suite with them |
| [Repository README](../README.md) | what EMOSA does today, how to develop and test it |

## Architecture background (reference)

| Document | Covers |
| --- | --- |
| [Data plane](architecture/data-plane.md) | how the pods' client traffic reaches the LAN: GRE termination or the Multi-AP Wi-Fi backhaul |
| [OpenSync pods as EasyMesh agents](architecture/opensync-easymesh-mapping.md) | the fleet and the OVSDB ↔ EasyMesh translation |
| [Recovery semantics](architecture/recovery.md), [operation mappings](architecture/operation-mappings.md) | operations, idempotency, lost replies, restarts |
| [Architecture overview](architecture/overview.md), [requirements](architecture/requirements.md) | the original architecture and requirements (version 3.6) the specification grew from |

## Records

- [Evidence](evidence/README.md): dated reports, captures and results; each
  keeps its date and scope. The latest: the reference workload with Python, C
  and mixed agents ([opensync-lab-proof](evidence/opensync-lab-proof/README.md)),
  and EMOSA's way into the RDK lab up to the room suite on either agent
  ([rdk-lab](evidence/rdk-lab/README.md)).
- [Project records](project/README.md): the plans, status pages and handoff
  of the development up to September 2026. The current plan and status of all
  the labs are the easymesh-labs
  [alignment plan](https://github.com/boardfarmdevs/easymesh-labs/blob/main/docs/alignment-plan.md).

## Learning

- [Protocol](protocol/README.md): specification inputs, the protocol matrix, WSC.
- [Evaluation](evaluation/README.md): native controller and agent baselines,
  OVSDB and hwsim observations, dependency qualification.
- [Guides](guides/README.md): the team manual, the learning path and the
  experiment guides from the prototype phase.

The [interactive explorer](https://boardfarmdevs.github.io/emosa-lab/) presents
the architecture and results in a browser.
