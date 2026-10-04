# emosa-lab: EMOSA, unchanged OpenSync pods as EasyMesh agents

![EMOSA — エモさ — Emotional resonance: a Japanese riverside at sunset](assets/emosa-banner.png)

<!-- labs block: the same in every repository of the EasyMesh labs, but for the Site line -->
**Site:** <https://vcpe.dev/emosa-lab/>
The [EasyMesh labs](https://mesh.vcpe.dev/) serve three
goals: EasyMesh optimizer development
([easymesh-optimizer](https://vcpe.dev/easymesh-optimizer/)) in a rich
virtual lab, on both stacks
([RDK EasyMesh](https://vcpe.dev/meta-cmf-bananapi-vcpe/),
[prplMesh](https://vcpe.dev/prplmesh-lab/)); unchanged OpenSync
pods as EasyMesh agents under a local controller, without the OpenSync cloud
([EMOSA](https://vcpe.dev/emosa-lab/), with the
[OpenSync lab](https://vcpe.dev/opensync-lab/)'s pods); and
EasyMesh on physical hardware
([Protocol lab](https://vcpe.dev/easymesh-lab/)). Two core
components carry them: the RF medium
([easymesh-medium](https://vcpe.dev/easymesh-medium/)) and EMOSA's
OVSDB ⇄ EasyMesh conversion. The rest is infrastructure, tools (the
[room builder](https://vcpe.dev/easymesh-room-builder/)) and learning
around them.
<!-- /labs block -->

**EMOSA (EasyMesh to OpenSync Adapter)** puts unchanged OpenSync pods into an EasyMesh
network. It runs next to an EasyMesh controller; every pod handed to it shows up at the
controller as a standard EasyMesh agent, which the controller configures and whose
clients it sees, while the pod keeps running its own OpenSync software. The name also
echoes **エモさ (*emosa*)**, a Japanese word for emotional resonance
([Sanseido on エモい](https://dictionary.sanseido-publ.co.jp/topic/shingo2016/2016Best10.html)).

```text
EasyMesh controller (prplMesh, RDK, ...)
        │  IEEE 1905.1 / EasyMesh messages
        ▼
EMOSA: fleet · one virtual agent per pod · OVSDB ⇄ EasyMesh translation
        │  the pod's own OVSDB management connection
        ▼
Unchanged OpenSync pods
```

Shown in two labs, with unchanged OpenSync 6.6.1 pods on simulated radios: the
OpenSync lab under a prplMesh 6.0 controller, and the RDK lab under RDK-B's
unified-wifi-mesh, where the pods are part of the lab's rooms and its full room
suite passes with them.

- **Onboards every pod automatically.** Each pod handed to EMOSA gets its own
  agent, whose identity is derived from the pod's serial number.
- **Applies the controller's network.** The SSIDs and passphrases arrive in WSC
  M2; EMOSA writes them to the pod as one guarded change and counts them applied
  only when the pod reports them running.
- **Reports the pod as an agent.** Topology, radios, BSSes and clients from the
  pod's own state; AP metrics from the pod's statistics; unassociated station
  metrics from the probe requests the pod hears; Link Metric and AP Metrics
  answers.
- **Carries out the controller's decisions.** Client steering through the pod's
  own band steering (BTM), and Backhaul Steering: the pod's Wi-Fi backhaul moves
  to the parent the controller picks.
- **Carries the pods' client traffic.** Through the pod's GRE to EMOSA's
  termination point, or natively over a Multi-AP Wi-Fi backhaul.
- **Recovers without help** from an adapter restart, a pod connection cut, a lost
  pod backhaul and a controller restart (a 900-second fault workload).
- **Two implementations, one behaviour.** The Python reference and a C
  implementation of every program (the agent, the fleet, the GTP) pass the same
  conformance vectors, the same lab in a box and the same lab suites, with either or
  both in one lab; either fleet takes over from the other, and the C installs without
  Python. The C is being taken to production quality.

Not yet: TLS on the pod connections with a trust anchor an unchanged field pod
accepts; 5 and 6 GHz radios, several radios per pod and WPA3; channel and power
changes applied; pods on Ethernet; backhaul metrics and pods as parents of other
pods. [The target system](docs/concepts/target-system.md) says what each
needs.

## Components

| Part | What it is |
| --- | --- |
| [spec/](spec/README.md) | the specification, the component design and the conformance vectors: the contract |
| [schemas/](schemas) | the configuration, status and record files |
| [src/emosa/](src/emosa) | the adapter, the Python reference: `emosa-fleet`, `emosa-agent`, `emosa-gtp`; runtime dependencies `ovs`, `cryptography`, `jsonschema` |
| [c/](c/README.md) | the C implementation (`emosa-fleet-c`, `emosa-agent-c`, `emosa-gtp-c`), interchangeable with the reference, and [its production bar](c/QUALITY.md) |
| [lab/](lab) | `emosa-lab`: simulators, evaluation and experiment tooling; it depends on the adapter, never the other way round |
| [deploy/](deploy/README.md) | the adapter kit for any EasyMesh lab, and the drivers for the OpenSync lab and the RDK lab |
| [scenarios/](scenarios), [tests/](tests) | the experiment scenarios and the tests |
| [site/](site) | the interactive guide |

## Getting started

To add EMOSA to an EasyMesh lab you already have (RDK, prplMesh or another), use the
[adapter kit](deploy/adapter/README.md): one tarball with an install script, which states
what the lab must provide. The reference lab runs on one Linux host with LXD, with the
gateway, pods and clients of the [OpenSync lab](https://vcpe.dev/opensync-lab/); every
step is in the [lab manual](deploy/opensync-lab/README.md):

```sh
deploy/opensync-lab/lab.sh stage && deploy/opensync-lab/lab.sh emosa    # emosa c: the adapter in C
deploy/opensync-lab/lab.sh controller && deploy/opensync-lab/lab.sh ui
deploy/opensync-lab/lab.sh fleet
deploy/opensync-lab/lab.sh policy emosa-mesh 'EmosaMesh2026!'
deploy/opensync-lab/lab.sh admit pod-1 pod-2 pod-3
```

To develop: the repository is a `uv` workspace (`uv sync --frozen --no-dev` installs
the adapter alone):

```sh
uv sync --frozen
uv run ruff check . && uv run ruff format --check .
uv run pytest -m unit
bash scripts/build-ovsdb.sh && python3 scripts/build-wsc-registrar.py
uv run pytest -m ovsdb            # real ovsdb-server and WSC registrar
uv run pytest -m box              # the lab in a box (spec/box-scenarios.md), both implementations
```

Start reading the code at `src/emosa/agent/fleet.py` (an agent for every pod),
`src/emosa/opensync/easymesh_view.py` (the translation) and `src/emosa/agent/pod.py`
(the virtual agent); the design is in
[OpenSync pods as EasyMesh agents](docs/concepts/opensync-easymesh-mapping.md).

## Documentation

The [site](https://vcpe.dev/emosa-lab/) shows how it works, one message at a time. The
documents are indexed in [docs/README.md](docs/README.md): the contract (the
specification first), the concepts, the guides, the protocol reference, the project's
plans and status, and the run records and their evidence.

## License

EMOSA is licensed under the [Apache License 2.0](LICENSE). Third-party material in the
repository keeps its own license: see
[third-party notices](docs/project/third-party-notices.md).
