# EMOSA on an opensync-lab VM

The EasyMesh side of the [proof plan](../../doc/project/proof-plan.md): a native
prplMesh controller and EMOSA's virtual agents, added next to an unchanged
[opensync-lab](https://github.com/boardfarmdevs/opensync-lab) stack (RDK-B
gateway `mv3`, OpenSync 6.6.1.0 pods, wireless clients, `local-noc`).

```
em-ctl (prplMesh controller + colocated agent) ──em-1905── emosa (virtual agent per pod)
                                                               │ OVSDB, pod-initiated
pod-N (OpenSync cm) ── local-noc redirector ── tcp:10.101.0.1:665N ─┘ (LXD proxy → 127.0.0.1)
pod-N ──5 GHz backhaul + GRE── mv3 ── WAN      clients ──2.4 GHz── pod-N home-ap-24
```

- **em-ctl**: prplMesh 6.0.0 (prplmesh-lab artifacts) with EMOSA's controller
  candidate `candidate-ap-esp-02`, run as in the peer baseline: controller plus
  colocated agent, one hwsim radio from opensync-lab's pool, `eth1` on `em-1905`.
- **emosa**: this checkout, CPython 3.13.7 and `uv.lock`; one
  `emosa-agent@POD` service per pod (`python -m emosa.agent.pod`), each with its
  own AL MAC `02:00:00:5e:00:0N` on a macvlan `emN` over the container's single
  `em-1905` NIC `emlan` (LXD allows one NIC per managed network per instance).
- **Transport**: the pod's own `cm` dials EMOSA. `local-noc` (opensync-lab
  `--redirect` / `noc-ctl redirect`) writes the pod's `manager_addr`
  `tcp:10.101.0.1:665N` and ends the pod's session (OpenSync's `cm` acts on a new
  `manager_addr` only while not connected); an LXD proxy device carries the pod's
  new connection to EMOSA's loopback-only listener `ptcp:665N:127.0.0.1`. local-noc
  keeps the gateway end of the pod's GRE and stops writing the pod's fronthaul.
- **Cold pods**: a restarted pod has no fronthaul; the controller's M2 makes
  EMOSA create it (see `pod_profile.py`).
- **Controller UI**: prplmesh-lab's topology adapter in `em-ctl` and its
  controller UI on the VM (port 8093; 8091 is boardfarm's), host port 8660.
- **Profile**: `opensync-lab-hwsim-6.6.1-v1` (`src/emosa/opensync/pod_profile.py`).

## Run

opensync-lab VM first (opensync-lab branch `claude/emosa-hooks`: `--redirect`
and `MVX_CLIENT_SSID`), then the EasyMesh side. The run record is in
[doc/evidence/opensync-lab-proof](../../doc/evidence/opensync-lab-proof/README.md).

```sh
# in opensync-lab
export MVX_VM=emosa-osl-0923 MVX_NOC_UI_PORT=8650 MVX_HWSIM_POOL=32 \
       MVX_POD_IMAGE=$HOME/yocto/mvx-pod-work/out/mvx-pod-20260923124229
./setup-vm.sh all
./deploy-mvx.sh all && ./deploy-mvx.sh mesh

# in emosa-lab
export EMOSA_VM=emosa-osl-0923
deploy/opensync-lab/lab.sh stage
deploy/opensync-lab/lab.sh emosa
deploy/opensync-lab/lab.sh controller
deploy/opensync-lab/lab.sh ui                     # http://<host>:8660/
deploy/opensync-lab/lab.sh agent pod-1 1          # pod-1 is handed to EMOSA
deploy/opensync-lab/lab.sh policy emosa-mesh 'EmosaMesh2026!'
deploy/opensync-lab/lab.sh client em-wc1 pod-1 emosa-mesh 'EmosaMesh2026!'
deploy/opensync-lab/lab.sh status
deploy/opensync-lab/lab.sh topology               # the controller's DataElements
deploy/opensync-lab/lab.sh release pod-1          # give pod-1 back to local-noc
```

The lab credentials above are public test values.

### Fleet: an agent for every pod

Instead of one `agent POD N` per pod, the fleet gives every pod handed to it
its own virtual agent. The agent's AL MAC is derived from the pod's serial,
and its port and 1905 interface are allocated and persisted
(`src/emosa/agent/fleet.py`).

```sh
deploy/opensync-lab/lab.sh release pod-1          # per-pod agents first, if any
deploy/opensync-lab/lab.sh fleet                  # front port 10.101.0.1:6650, agents 6651-6690
deploy/opensync-lab/lab.sh policy emosa-mesh 'EmosaMesh2026!'   # saved, re-applied by admit
deploy/opensync-lab/lab.sh admit pod-1 pod-2 pod-3
deploy/opensync-lab/lab.sh status                 # "fleet SERIAL AL emN PORT" per pod
deploy/opensync-lab/lab.sh release pod-2          # back to local-noc; its agent is forgotten
```

`admit` only asks local-noc to redirect the pod to the front port. EMOSA needs
no per-pod input. The pod's own identity decides its agent. `admit` re-applies
the controller policy because prplMesh's lab policy lists credentials per AL
MAC. With an operator's controller, that onboarding policy is the operator's.
`workload` still expects the per-pod agents (`emosa-agent@pod-1`).

## Artifacts

`.cache/opensync-lab-artifacts` (not in git), checked against
`artifacts.sha256` by `lab.sh stage`: the prplMesh install/runtime archives
and EMOSA's bwl/hostap overlays and controller candidate, copied from the
`emosa-lab` VM on rev150 (the same hashes as `deploy/peer-baseline/reference.json`),
uv 0.11.17, and prplmesh-lab `e1fd7cd`'s controller UI binary and topology
adapter (`controller-ui/`, built with Go on rev140).
