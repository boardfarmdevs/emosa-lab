# EMOSA in the RDK lab

**Reference and guide.** The RDK EasyMesh optimizer lab (meta-cmf-bananapi-vcpe)
has an EMOSA option: unchanged OpenSync pods, each a complete EasyMesh agent
through EMOSA, under RDK's controller next to the lab's native agents, on one
RF medium, in the lab's rooms. This document is the option's design and how to
run it, kept current. How it got here (the first pod on 25 September 2026, the
room integration, the 24-room qualification, every finding and fix, the suite
results with Python, C and mixed) is the record
[`docs/records/evidence/rdk-lab/README.md`](../records/evidence/rdk-lab/README.md). Which VMs
run it is in the easymesh-labs
lab configurations (in [easymesh-labs](https://mesh.vcpe.dev/))
(#5).

## 1. What the RDK lab offers

- **One VM.** It owns the kernel (the lab's patched hwsim and cfg80211),
  wmediumd ([easymesh-medium](https://github.com/boardfarmdevs/easymesh-medium))
  and nested LXD. Inside it:
  - the gateway and controller with its colocated agent, `bpibroadband`;
  - four Wi-Fi extenders, `bpiap` and `bpiap-001` to `bpiap-003`, and the
    wired extender `bpiap-004`;
  - 100 client containers;
  - boardfarm's WAN side (`wan-cpe1`, `dhcp-cpe1`).
- **The WAN is boardfarm's.** `br-wan101` carries `10.101.0.0/24`. The VM is
  `.1`, and the controller's `erouter0` is `.100`. This is the same layout
  as opensync-lab, where local-noc is `.40`.
- **One wired LAN port.** The controller's `brlan0` (`10.0.0.1/24`) holds its
  Wi-Fi interfaces and internal veth pairs, and one wired port, `eth2`: a VM
  bridge the lab makes for its wired extender (meta-cmf
  `gen/wired-extender.sh lanport`, kept in `brlan0` by
  `easymesh-lanport.timer`). EMOSA and the GTP use the same port. The Wi-Fi
  extenders reach the controller over the Wi-Fi backhaul, as 4-address
  stations on `wifi1.1` (`mesh_backhaul`).
- **Radios.** Each RDK node has one wiphy carrying all bands (the MediaTek
  single-wiphy model). The VM keeps a pool of further hwsim radios
  (`virt-wlan105` and up), and wmediumd carries the medium.
- **The controller toward EMOSA.** It needs the `r1` message set and sends a
  five-BSS M2 set from one registrar session (`multi_bss`,
  `m2_session: shared`), which EMOSA applies.

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
 RDK lab VM
 ├─ br-wan101  10.101.0.0/24 ── bpibroadband erouter0 (.100), boardfarm WAN
 │               └─ emosa: fleet front port 10.101.0.40:6640 (the pod's redirector),
 │                         agent ports 10.101.0.40:6651..
 ├─ the lab's wired LAN port (L2 only) ── bpibroadband eth2 ∈ brlan0
 │               ├─ emosa: 1905 trunk (one macvlan per virtual agent)
 │               ├─ em-gtp: LAN leg (GRE tunnels bridged here)
 │               └─ bpiap-004: the wired extender's LAN port
 └─ wmediumd medium
     ├─ em-gtp radio: SSID opensync-lab-bhaul (the pods' onboarding SSID)
     ├─ each pod (two pool radios): backhaul station; 2.4 GHz fronthaul from the M2 set
     └─ the lab's clients, on every AP's fronthaul, the pods' included
```

- **EMOSA on the WAN at `.40`.** The pod reaches its built-in redirector
  through its GRE → `brlan0` → the RDK router's NAT → `br-wan101`, just as
  opensync-lab pods reach local-noc through mv3. EMOSA's fleet answers there
  directly: it identifies the pod, starts its agent and writes
  `manager_addr`. In a deployment the operator's cloud hands pods to EMOSA
  instead (its redirect). Either way the pod is not changed.
- **The wired LAN port.** EMOSA's 1905 interfaces and the GTP's LAN leg need
  layer 2 with the controller; the lab's wired port gives it, and it is the
  only thing EMOSA needs from the RDK side. On a product, EMOSA runs on the
  gateway itself and uses `brlan0` directly.
- **Data plane: both options.** Every pod starts on option 2: the GTP serves
  its onboarding SSID and terminates its GRE (spec §8.2). Option 1 then moves
  it onto the gateway's 5 GHz `mesh_backhaul` BSS, its station pinned to that
  BSSID (spec §8.3); the controller can move it to another parent with
  Backhaul Steering. `lab.sh up` puts both pods on option 1.
- **Radios on the medium.** The pods, the GTP and hand-made clients take
  radios from the VM's spare pool through the lab's own allocator, and
  `lab.sh medium` puts them on wmediumd's medium (`user.wmediumd.guest`). In
  the rooms the pods' fronthaul is placed by the room model (§6).

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

em_cli (`src/rdkb-cli`, shipped prebuilt as `em-cli.tar.gz` in meta-cmf) passes
`Manufacturer` and `ManufacturerModel` through to the dashboard's device list
and the topology's device objects, derives `kind: opensync-pod` from the
manufacturer, and draws that kind distinctly in the topology views
(`room-topology.js`, `script.js`). Pods are named `Pod-N` from their own
counter (meta-cmf patch 0212) and a device's identity is read from its own
keys (0217); only the gateway's colocated agent is `Agent-1` (unified-wifi-mesh
0218). The artifact is rebuilt with the lab's own
`gen/rebuild-em-cli-artifact.sh`.

## 5. Running it

The option is split between the repositories:

| Part | Owner |
| --- | --- |
| The option and its entry: `EASYMESH_EMOSA=1` on `gen/vm/lxd/build.sh build`, or `build.sh emosa` on an accepted lab VM; `EASYMESH_EMOSA_AGENT=python\|c` picks the pods' agent implementation | meta-cmf-bananapi-vcpe |
| The rooms with the pods: `worlds-pods`, the lab's standard rooms (the four Wi-Fi extenders and the wired extender `extender_5`) plus `pod_1` and `pod_2`, all 31 worlds under the standard IDs | meta-cmf-bananapi-vcpe |
| The steps: `deploy/rdk-lab/lab.sh stage` (host), then `lab.sh up [python\|c]` in the VM | emosa-lab |
| The pod image, pinned in the easymesh-labs `manifest.json` (`EMOSA_POD_IMAGE`) | opensync-lab |

`deploy/rdk-lab/` is the driver, like `deploy/opensync-lab/`: a host-side
wrapper that stages this repository into the VM (`/opt/emosa-lab`) and a
VM-side `lab.sh` that reuses the same components (the adapter kit, `emosa-gtp`,
the fleet). The steps both drivers take, the adapter container and kit, the
fleet's configuration, the pods' telemetry and certificates, the agent
implementation and the GTP's services, are one copy in
[deploy/lib/emosa-vm.sh](../../deploy/lib/emosa-vm.sh); each driver keeps what
its lab differs in (the EasyMesh LAN, the controller, the pods and their radios). `lab.sh up [python|c]` runs every step in order and completes a
partial run; after a reboot it brings the pods back:

| Step | What it does |
| --- | --- |
| `lanport` | the lab's wired LAN port (the RDK lab owns it; an older lab's `br-emosa` is kept) |
| `emosa` | container `emosa`: the adapter kit, both agents; the pods' redirector address `10.101.0.40` on `br-wan101` |
| `implementation python\|c` | every pod's agent on one implementation (when `up` is given one) |
| `fleet` | the fleet on the front port `10.101.0.40:6640`, agents on 6651 to 6690 |
| `gtp` | container `em-gtp`: the onboarding SSID and the pods' GRE, its LAN leg on the wired port |
| `pod pod-1`, `pod pod-2` | the unchanged pod image on two pool radios; its backhaul to the GTP a fixed link (45 dB) |
| `medium` | wmediumd regenerated with the guests' radios |
| `telemetry` | the MQTT broker for the pods' statistics (§6) |
| `backhaul wifi` | both pods on option 1 |
| `rooms pods` | the room service on the pods' room set |
| (check) | every pod's Wi-Fi uplink applied; a pod held on the GTP path is switched once more, else `up` fails |

By hand: `lab.sh agent POD python|c` (one pod's implementation), `lab.sh move
POD TARGET` (the controller moves a pod's backhaul: a mesh node's container,
its 5 GHz backhaul BSS or a BSSID), `lab.sh backhaul wired|wifi [POD...]`
(`wired` returns a pod to the GTP path; `wifi` also releases a hold after a
failed switch), `lab.sh repod`, `lab.sh client NAME SSID KEY`, `lab.sh rooms
native` (the lab's own rooms, pods stopped and their controller rows removed),
`lab.sh status`.

**Acceptance.** An implementation of EMOSA is accepted in this lab (spec
design §13) when, with the pods on it:
1. each unchanged pod joins the GTP, dials `.40` and gets an agent;
2. the RDK controller onboards the agent, the pod runs its fronthaul, and a
   client on it reaches the internet;
3. the pod is in the controller's topology and in em_cli as an OpenSync pod;
4. the controller's `steer.sh` moves a station off the pod's BSS;
5. the lab's room suite (meta-cmf `gen/tests/run-easymesh-suite.sh rooms`)
   passes on the pods' room set: the guest audit, default readiness, the
   catalog, the geometry rooms, the RF hover, access and property rooms, the
   switch through all worlds and the return to the default. Run the browser
   from a host other than the lab VM's when the VM's host is loaded: the
   catalog's convergence windows are tight.

## 6. The pods in the rooms

**Pods are extra APs.** The pods' room set (`worlds-pods`) is the lab's
standard rooms, each with its gateway, four Wi-Fi extenders and the wired
extender, plus `pod_1` and `pod_2`; the lab's own rooms stay as they are and
remain the baseline. The room model has a pod node kind (a world may include
it; the native worlds keep every bound `fronthaul_ap` role), single-band nodes
(a pod in the room is one 2.4 GHz radio: an EMOSA agent is one radio), and the
pods' positions in each layout, with their own golden files.

**The pods' backhaul.** On option 1 a pod's station keeps the lab's own links:
a fixed 50 dB to its upstream (the gateway's 5 GHz backhaul BSS, pinned by the
fleet), as strong as the lab's own gateway-extender backhaul, and the medium's
default to every other radio. In the geometry rooms, where the room models the
backhaul, its links to the native APs' 5 GHz radios follow the room like
theirs. There a pod's strongest parent is an extender, never the gateway, so
before such a room's RF is applied the room moves each pod there: the
controller's `SteerWiFiBackhaul()` sends a Backhaul Steering Request, EMOSA
re-pins the pod's station to the target and answers once the pod's State shows
it there. A Wi-Fi extender starts its backhaul BSS only for a child, so the
room starts it first. The controller's own backhaul policy does not choose a
pod's parent: that needs backhaul link metrics from EMOSA (spec §9).

When a native extender re-parents, OneWifi takes its BSSes down with its
uplink; a pod under it loses its OVSDB connection to EMOSA and is onboarded
again once the extender is back (a new connection is a new source). A loaded
geometry room with pods therefore has a 150 s branch window.

**The wired extender** is the RDK lab's own (meta-cmf
`gen/wired-extender.sh`), on the same wired port as EMOSA. It must never have
a second path into the LAN; the lab keeps its stations down and gives it no
backhaul links in any room.

**Measurements from the pods.** The optimizer and the room gates need two
measurements per client, and the pods give both over their own MQTT telemetry
(`lab.sh telemetry`: a broker at `10.101.0.40:8883`, mutual TLS with the lab
CA and a device certificate per pod; each pod publishes raw client and
on-channel survey reports every 5 s, and `qm` publishes every 5 s):

| Measurement | Used for | From the pod | Freshness |
| --- | --- | --- | --- |
| Serving RCPI of each client on a pod | `metricsFresh` (≤ 30 s), the policy's current metric (≤ 60 s) | the client reports (`sts.Report`); each agent sends an AP Metrics Response every 5 s as the controller's Metric Reporting Policy asks: channel utilization from the survey, the profile's best-effort ESP, and each station's link metrics (RCPI from the SNR) and traffic (spec §3.8) | 5 s reports, 5 s publishing |
| Candidate RCPI of a client at a pod | the optimizer's candidates (Unassociated STA Link Metrics, per agent) | probe requests: hostapd's `RX-PROBE-REQUEST` signal becomes an `ow_steer_bm` `PROBE` event in the band-steering report (`sts.BSReport`), for stations with a `Band_Steering_Clients` row; the agent writes a watch row for each station the controller asks about (spec §3.9) | the band-steering report every 60 s (fixed in `owm`); each measurement is reported with its age, none older than two minutes |

An associated station probes only when it scans, so a pod measures few
candidates. The Ack refuses every station the pod cannot report (Error Code
`0x01` when it is associated with the pod, `0x02` otherwise); RDK's controller
completes a query that refuses every station on the Ack alone (meta-cmf patch
0140).

## 7. Client steering through the pod

The RDK controller steers a station with a Client Steering Request (`0x8014`)
carrying one Steering Request TLV (`0x9B`): the source BSSID, the request mode
(mandate or opportunity) with the BTM flags (disassociation imminent,
abridged), the opportunity window, the BTM disassociation timer, the stations,
and the target BSSs with operating class and channel. The lab drives it with
`/usr/bin/steer.sh STA TARGET` on the controller (through `steer_drv`, also
behind em_cli's `/api/v1/steer-native`): always a mandate with an abridged
candidate list, either with disassociation imminent (timer 5, window 5) or
"gentle" (neither, window 50: a station that declines stays). A native agent:
1. acknowledges within one second (1905 ACK, with an Error Code TLV `0xA3`,
   reason `0x02`, for a station not on the source BSS);
2. sends the station a BTM request toward the target;
3. reports the station's answer in a Client Steering BTM Report (`0x8015`,
   Steering BTM Report TLV `0x9C`: BSSID, station, BTM status, target);
4. for an opportunity, sends Steering Completed (`0x8017`) when the window
   ends.

The controller handles the ACK and `0x8015`; it has no handler for `0x8017`.

### 7.1 On the pod: owm's client steering

OpenSync 6.6's `owm` steers one station away from its BSS when it has a
steering group (`Band_Steering_Config` naming the VIF), the target as a
neighbor (`Wifi_VIF_Neighbors`), and the station's complete
`Band_Steering_Clients` row with client steering `away` for an enforcement
period (`cs_mode`, `cs_params.cs_enforce_period`), `sc_kick_type` `btm_deauth`
and `sc_btm_params` `{bssid, disassoc_imminent}`. Once `cs_state` is
`steering`, a one-shot `force_kick` `directed` makes its executor block the
station on the source (hostapd deny list), send the BTM request naming the
target, and deauthenticate the station if it stays (10 s after the BTM
request, at once for a station without BTM support).

On hwsim that never produced a BTM request. Four faults, fixed in opensync-lab's
pod (25 September 2026):
- core `0003`: the BTM response policy's age overflowed, and with no response
  yet every candidate was masked out;
- the nl80211 driver has no ACL unless the platform names one per phy
  (`OSW_DRV_NL80211_ACL_IMPL_PHY_<phy>`): the block never applied and the
  executor waited on it for ever. The pod bootstrap names hostapd's;
- hostap `992`: hostapd disassociated a station as soon as it was on the deny
  list, before the executor's BTM request. A deny-list entry now refuses new
  associations only, which is what the executor assumes;
- hostap `993`: `owm` reads BTM support from the association request elements
  in hostapd's STA output and AP-STA-CONNECTED (`assoc_ies`), which upstream
  hostapd never reports: every station looked BTM-incapable and was only
  deauthenticated.

Seen live after the fixes: the BTM request carries the target as its only
candidate, and the station answers (BTM response, status 0). The lab's client
(wpa_supplicant 2.10) then keeps its current BSS: it ranks candidates by
estimated throughput and, on a tie, stays, even with disassociation imminent.
Every guest link has the same SNR on this medium, so the target is never
better; `owm` then deauthenticates the station and it reconnects where it
chooses. A target that is a better link needs the room model (§6).

### 7.2 In EMOSA

`emosa.wire.steering` acknowledges every Client Steering Request and hands a
**mandate for one station and one named target**, from a BSS of the pod, to
the steering scope (`emosa.agent.steering`, `emosa.opensync.steering`; spec
§3.7):
- the window opens with one guarded transaction: the pod is the bound serial
  and no `Band_Steering_Clients` row exists for the station (another manager's
  steering is not taken over); group and neighbor rows are reused when
  present and inserted when absent. The window is the request's opportunity
  window, 15 to 120 s;
- it is applied when `owm` reports `cs_state` `steering`; then the directed
  kick, guarded by that state;
- it closes when `owm` stops steering, or at the latest after the window, and
  exactly the rows it inserted are deleted (by UUID). Without disassociation
  imminent it closes 8 s after the kick, before `owm`'s deauthentication, so a
  declining station stays as the controller asked;
- one mandate at a time. Several stations or targets, the wildcard target and
  a foreign source are acknowledged and not carried out. An opportunity is
  completed at once (`0x8017`): EMOSA makes no steering decisions of its own.

**No BTM Report.** OpenSync 6.6 does not expose the station's BTM status: its
band-steering report (`sts.BSReport`) has a `CLIENT_BTM_STATUS` event and a
`btm_status` field, but `owm` never sets either, and no table carries the
response. EMOSA therefore sends no `0x8015` rather than a guessed status. The
controller sees the outcome in the topology: the station leaves the pod's BSS
(Client Association Event) and joins the target.

**Known limitation.** The pod does not tell EMOSA whether a station supports
BTM (`Wifi_Associated_Clients.capabilities` is empty). `owm` deauthenticates a
station without BTM support at once, also for a gentle request.

## 8. Python or C

Both implementations run here with the same configuration, state directory and
status file, and either takes a pod over from the other. `lab.sh agent POD
python|c` picks one pod's (`/etc/default/emosa-POD` in the `emosa` container),
`lab.sh implementation python|c` every pod's, and a lab built with meta-cmf
`EASYMESH_EMOSA_AGENT=c` starts them on C. Each agent's status file names its
implementation. The room suite passed with both pods on Python, both on C and
one of each (the record, §3).

## 9. Open

- A pod's session across a short loss of its OVSDB connection: EMOSA could
  keep it when the pod comes back the same (its capabilities unchanged)
  instead of onboarding again (§6).
- The controller choosing a pod's backhaul parent itself: backhaul link
  metrics and a 1905 neighbor on the backhaul (spec §9).
- One represented radio per pod, its 2.4 GHz fronthaul; the pod's other
  radios are not reported (spec design §14).
- No BTM Report, and whether a station supports BTM is not known (§7.2).
- The controller writes every M1's manufacturer and model onto its own device
  record as well: its node reads "OpenSync via EMOSA" once a pod has onboarded.
  em_cli never classifies the root; the controller is unchanged there.
