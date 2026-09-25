# EMOSA Lab

![EMOSA — エモさ — Emotional resonance: a Japanese riverside at sunset](assets/emosa-banner.png)

**EMOSA (EasyMesh to OpenSync Adapter)** puts unchanged OpenSync pods into an
EasyMesh network. It runs next to an EasyMesh controller. Every pod handed to it
shows up at the controller as a standard EasyMesh agent. The controller
configures it and sees its clients, and the pod keeps running its own OpenSync
software.

**[See how it works: the interactive guide](https://boardfarmdevs.github.io/emosa-lab/)**

<!-- labs block: the same in the four lab repositories -->
**Site:** <https://boardfarmdevs.github.io/emosa-lab/>. Part of the boardfarmdevs labs, which serve two
goals: the EasyMesh optimizer ([RDK EasyMesh](https://boardfarmdevs.github.io/meta-cmf-bananapi-vcpe/),
[prplMesh](https://boardfarmdevs.github.io/prplmesh-lab/)) and the OpenSync adapter
([EMOSA](https://boardfarmdevs.github.io/emosa-lab/), [OpenSync](https://boardfarmdevs.github.io/opensync-lab/)), on the way to one
EasyMesh system on wmediumd with native agents and OpenSync pods together.

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

Verified in the lab with six unchanged OpenSync 6.6.1 pods on simulated radios
and a prplMesh 6.0 controller:

- **Onboards every pod automatically.** Each pod handed to EMOSA gets its own
  agent. The agent's identity is derived from the pod's serial number.
- **Applies the controller's network.** The SSID and WPA2 passphrase arrive in
  WSC M2. EMOSA writes them to the pod as one guarded change, and counts them
  as applied only when the pod reports them running.
- **Reports the live topology.** Radios, BSSes, channel and associated clients
  come from the pod's own state tables. Client joins and leaves are reported as
  they happen.
- **Recovers without help** from an adapter restart, a pod connection cut, a lost
  pod backhaul and a controller restart. It passed a 900-second fault workload.

Not yet supported:
- 5 and 6 GHz radios, and WPA3;
- AP and station metrics;
- physical pods, which need EMOSA to be trusted by the pod's TLS.

The RDK controller accepts EMOSA's configuration, but bringing up its full
network is still being verified.

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
