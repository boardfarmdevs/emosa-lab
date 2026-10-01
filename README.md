# EMOSA Lab

![EMOSA — エモさ — Emotional resonance: a Japanese riverside at sunset](assets/emosa-banner.png)

**EMOSA (EasyMesh to OpenSync Adapter)** puts unchanged OpenSync pods into an
EasyMesh network. It runs next to an EasyMesh controller. Every pod handed to it
shows up at the controller as a standard EasyMesh agent. The controller
configures it and sees its clients, and the pod keeps running its own OpenSync
software.

**[See how it works: the interactive guide](https://boardfarmdevs.github.io/emosa-lab/)**

<!-- labs block: the same in every repository of the EasyMesh labs, but for the Site line -->
**Site:** <https://boardfarmdevs.github.io/emosa-lab/>.
The [EasyMesh labs](https://boardfarmdevs.github.io/easymesh-labs/) serve three
goals: EasyMesh optimizer development
([easymesh-optimizer](https://github.com/boardfarmdevs/easymesh-optimizer)) in a rich
virtual lab, on both stacks
([RDK EasyMesh](https://boardfarmdevs.github.io/meta-cmf-bananapi-vcpe/),
[prplMesh](https://boardfarmdevs.github.io/prplmesh-lab/)); unchanged OpenSync
pods as EasyMesh agents under a local controller, without the OpenSync cloud
([EMOSA](https://boardfarmdevs.github.io/emosa-lab/), with the
[OpenSync lab](https://boardfarmdevs.github.io/opensync-lab/)'s pods); and
EasyMesh on physical hardware
([Protocol lab](https://boardfarmdevs.github.io/easymesh-lab/)). Two core
components carry them: the RF medium
([easymesh-medium](https://github.com/boardfarmdevs/easymesh-medium)) and EMOSA's
OVSDB ⇄ EasyMesh conversion. The rest is infrastructure and learning around them.
<!-- /labs block -->

```text
EasyMesh controller (prplMesh, RDK, ...)
        │  IEEE 1905.1 / EasyMesh messages
        ▼
EMOSA: fleet · one virtual agent per pod · OVSDB ⇄ EasyMesh translation
        │  the pod's own OVSDB management connection
        ▼
Unchanged OpenSync pods
```

## What it does today

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
  implementation pass the same conformance vectors and the same lab suites, with
  either or both in one lab. The C is being taken to production quality.

Not yet: TLS on the pod connections with a trust anchor an unchanged field pod
accepts; 5 and 6 GHz radios, several radios per pod and WPA3; channel and power
changes applied; pods on Ethernet; backhaul metrics and pods as parents of other
pods. [The target system](doc/architecture/target-system.md) says what each
needs.

## Run it

To add EMOSA to an EasyMesh lab you already have (RDK, prplMesh or another),
use the [adapter kit](deploy/adapter/README.md). It is a single tarball with
an install script, and it states what the lab must provide.

The reference lab runs on one Linux host with LXD. The gateway, pods and clients come from
[opensync-lab](https://github.com/boardfarmdevs/opensync-lab). Every step is in
the [lab manual](deploy/opensync-lab/README.md); in short:

```sh
deploy/opensync-lab/lab.sh stage && deploy/opensync-lab/lab.sh emosa
deploy/opensync-lab/lab.sh controller && deploy/opensync-lab/lab.sh ui
deploy/opensync-lab/lab.sh fleet
deploy/opensync-lab/lab.sh policy emosa-mesh 'EmosaMesh2026!'
deploy/opensync-lab/lab.sh admit pod-1 pod-2 pod-3
```

## Develop

The repository is a `uv` workspace with two packages:

| Package | Where | What |
| --- | --- | --- |
| `emosa` | `src/emosa` | The adapter. Runtime dependencies: `ovs`, `cryptography`, `jsonschema`. Commands: `emosa-fleet`, `emosa-agent`. |
| `emosa-lab` | `lab/src/emosa_lab` | Simulators, evaluation and experiment tooling. It depends on the adapter, never the other way round (`tests/test_adapter_boundary.py`). |

`uv sync --frozen --no-dev` installs the adapter alone. A development sync
installs both.

```sh
uv sync --frozen
uv run ruff check . && uv run ruff format --check .
uv run pytest -m unit
bash scripts/build-ovsdb.sh && python3 scripts/build-wsc-registrar.py
uv run pytest -m ovsdb            # real ovsdb-server and WSC registrar
```

Start reading the code with these three modules:
- `src/emosa/agent/fleet.py`: an agent for every pod;
- `src/emosa/opensync/easymesh_view.py`: the translation;
- `src/emosa/agent/pod.py`: the virtual agent.

The design is in
[OpenSync pods as EasyMesh agents](doc/architecture/opensync-easymesh-mapping.md).
The [documentation index](doc/README.md) lists everything else, including run
records and history.

## The name

*EMOSA* also echoes **エモさ (*emosa*)**, a Japanese word for emotional
resonance, often with a nostalgic feeling. See
[Sanseido on エモい (*emoi*)](https://dictionary.sanseido-publ.co.jp/topic/shingo2016/2016Best10.html).
