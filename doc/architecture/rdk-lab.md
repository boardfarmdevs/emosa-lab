# EMOSA and an OpenSync pod in the RDK EasyMesh lab

The goal is to take an unchanged OpenSync pod into the RDK-B EasyMesh lab
(meta-cmf-bananapi-vcpe) through EMOSA. The pod should be onboarded by RDK's
controller, appear in its topology (em_cli), and be shown there as an OpenSync
pod. Later it should act like an EasyMesh agent: metrics, BTM steering. First
the Python reference adapter runs there. A C implementation follows,
interchangeable with it (same configuration, status and conformance vectors).

The experiment runs in its own RDK lab VM, `rdk-emosa` on rev120, built from
the same images as the fresh reference VM `rdk-0925`. The reference VMs
(`demo-a`, `rdk-0925`) are not touched.

## 1. What the RDK lab offers

Seen on `rdk-0925` and in the lab's documentation:

- **One VM.** It owns the kernel (the lab's patched hwsim and cfg80211),
  wmediumd and nested LXD. Inside it:
  - the controller with its colocated agent, `bpibroadband`;
  - four extenders, `bpiap` and `bpiap-001` to `bpiap-003`;
  - 100 client containers;
  - boardfarm's WAN side (`wan-cpe1`, `dhcp-cpe1`).
- **The WAN is boardfarm's.** `br-wan101` carries `10.101.0.0/24`. The VM is
  `.1`, and the controller's `erouter0` is `.100`. This is the same layout
  as opensync-lab, where local-noc is `.40`.
- **No wired LAN.** The controller's only NIC is `eth0` on `br-wan101`. Its
  `brlan0` (`10.0.0.1/24`) holds the Wi-Fi interfaces and internal veth
  pairs. Extenders reach it over the Wi-Fi backhaul only, as 4-address
  stations on `wifi1.1` (`mesh_backhaul`).
- **Radios.** Each RDK node has one wiphy carrying all bands (the MediaTek
  single-wiphy model). The VM keeps a pool of further hwsim radios
  (`virt-wlan105` and up), and wmediumd carries the medium.
- **Controller behaviour toward EMOSA,** from the first attempt (evidence
  README, M9):
  - it needs the `r1` message set;
  - it sends a five-BSS M2 set from one registrar session (`multi_bss`,
    `m2_session: shared`);
  - EMOSA accepted and committed that set. Application was never verified,
    because the RDK controller then ran in the wrong VM.

## 2. What the pod expects, unchanged

The pod image's service-provider profile (`mvx-local`) fixes two things:
- its redirector, `tcp:10.101.0.40:6640`, in the database template;
- its onboarding backhaul credentials, `opensync-lab-bhaul`, with
  `onboard_type` `gre`.

On every OpenSync start the pod:
1. joins the onboarding SSID as a 3-address station;
2. leases a link-local address and builds GRE to the first host of that
   subnet;
3. bridges the GRE into `br-home`, gets its LAN address through it, and
   dials the redirector.

A field pod behaves the same way with its own operator's values. The lab
meets those expectations; it does not change the pod.

## 3. Placement

```
 rdk-emosa VM
 ├─ br-wan101  10.101.0.0/24 ── bpibroadband erouter0 (.100), boardfarm WAN
 │               └─ emosa: fleet front port 10.101.0.40:6640 (the pod's redirector),
 │                         agent ports 10.101.0.40:6651..
 ├─ br-emosa   (new, L2 only) ── bpibroadband eth2 ∈ brlan0   (the gateway's wired LAN port)
 │               ├─ emosa: 1905 trunk (one macvlan per virtual agent)
 │               └─ em-gtp: LAN leg (GRE tunnels bridged here)
 └─ wmediumd medium
     ├─ em-gtp radio: SSID opensync-lab-bhaul (the pod's onboarding SSID)
     ├─ pod-1 radios (two spare pool radios): station to em-gtp; fronthaul from the M2 set
     └─ client radio (one spare pool radio): station on the pod's fronthaul
```

- **EMOSA on the WAN at `.40`.** The pod reaches its built-in redirector
  through its GRE → `brlan0` → the RDK router's NAT → `br-wan101`, just as
  opensync-lab pods reach local-noc through mv3. EMOSA's fleet answers there
  directly: it identifies the pod, starts its agent and writes
  `manager_addr`. In a deployment the operator's cloud hands pods to EMOSA
  instead (its redirect). Either way the pod is not changed.
- **A wired port into `brlan0`.** EMOSA's 1905 interfaces and the GTP's LAN
  leg need layer 2 with the controller. A new VM bridge `br-emosa` becomes a
  NIC of `bpibroadband` (`eth2`), added to its `brlan0`. This is the only
  change to the RDK side. On a product, EMOSA would run on the gateway itself
  and use `brlan0` directly. The same port is what a wired extender needs
  later.
- **Data plane: option 2 first.** The GTP serves the pod's onboarding SSID and
  terminates its GRE (spec §8.2). Option 1 comes later: the pod's station on
  RDK's `mesh_backhaul` BSS, pinned to its BSSID (spec §8.3).
- **Radios on the medium.** The pod, the GTP and the client take radios from
  the VM's spare pool, through the lab's own allocator. They have to be
  placed in wmediumd's model within range of each other and of the
  controller. How the lab places pool radios in its room model is the open
  RF question for this step. A radio outside the model gets no link. Placing
  them also gives the pod real survey data, which the AP metrics need.

## 4. Identity in the controller and em_cli

EMOSA's M1 already names the pod: manufacturer `OpenSync via EMOSA`, model
`OpenSync pod`, the pod's firmware as model number and its serial. The RDK
controller stores the M1 manufacturer, model name and serial number per
device (`em_configuration.cpp`), and its data model serializes them as
`Manufacturer`, `ManufacturerModel` and `SerialNumber` (`dm_device.cpp`). So
the marker needs no protocol change.

The topology tree (`get_network`) encodes each device in summary form, which
leaves these fields out (`em_network_topo.cpp`); the full device encoding,
used for the device list, has them. em_cli therefore joins the device list to
the topology by device ID, and the controller stays unchanged.

em_cli (`src/rdkb-cli`, shipped prebuilt as `em-cli.tar.gz` in meta-cmf) does
not use them yet:
- the dashboard's live device list labels every device `RDK` / `EasyMesh R6`;
- the topology tab's device object (`NetworkDevice`) carries only its ID,
  backhaul and radios.

The meta-cmf patch passes `Manufacturer` and `ManufacturerModel` through to
both, derives `kind: opensync-pod` from the manufacturer, and draws that kind
distinctly in the topology views (`room-topology.js`, `script.js`). The
artifact is rebuilt with the lab's own `gen/rebuild-em-cli-artifact.sh`.

## 5. Steps and acceptance

| Step | Done when | Result (2026-09-25) |
| --- | --- | --- |
| 1. VM `rdk-emosa` | the lab's own build gates pass | passed: health audit, ready with 105 radios, room and survey |
| 2. Wired LAN port | `bpibroadband` has `eth2` in `brlan0`; a host on `br-emosa` gets a lease | passed: a test host leased `10.0.0.98` from `10.0.0.1` |
| 3. EMOSA and GTP | fleet at `10.101.0.40:6640`; the GTP serves `opensync-lab-bhaul` on a pool radio on the medium | passed: channel 44, underlay `169.254.2.1/25` |
| 4. Pod | the unchanged pod joins the GTP, dials `.40`, gets an agent | passed: GRE up, `br-home` `10.0.0.155` from the RDK router, agent `02:72:f9:7f:07:85` |
| 5. Onboarding | the RDK controller onboards the agent; the pod runs its fronthaul; a client has internet | passed: the five-BSS M2 set applied (`private_ssid`, `iot_ssid`, `lnf_radius`, `hotspot`, `mesh_backhaul`); a client on the pod's `private_ssid` reached the internet |
| 6. Topology | the pod in the controller's topology and in em_cli, marked as an OpenSync pod | in the controller's topology (Agent-1, 5 BSSes, its station); the em_cli marking is next |
| 7. Agent behaviour | client steering (BTM) through the pod; metrics on the medium | not started |

The lab driver is `deploy/rdk-lab/` in emosa-lab, like `deploy/opensync-lab/`:
a host-side wrapper and a VM-side script that reuses the same components (the
adapter kit, `emosa-gtp`, the fleet).

## 6. Found on the way

- **Country code 99.** In this VM every hwsim phy reports the custom
  regulatory domain `99`. OSW copied it into hostapd's `country_code`, which
  hostapd rejects, so no BSS started: the same fault as `98` in the EMOSA VM.
  opensync-lab's pod now writes only an ISO 3166 alpha-2 code (core patch
  0002).
- **The LAN is shared.** `brlan0` carries every RDK agent's 1905 traffic. The
  agent spent its input budget on frames that were not for it and could fail
  an admission on one, and the first M2 set was lost. Frames that do not
  match the agent's binding are now dropped first, without either effect.
- **A bridge takes its lowest port MAC.** LXD's `00:16:3e:…` port would have
  become `brlan0`'s address; the port gets a locally administered MAC above
  the lab's.
- **Radios come back renamed.** A deleted pod's phys return carrying
  OpenSync's VIFs and no `virt-wlanN`; the driver reclaims its own radios
  before a start, as the lab's allocator does for its roles.
- **wmediumd.** The generator includes guest radios (meta-cmf `215535b`); the
  room demo stops while the medium is regenerated.
