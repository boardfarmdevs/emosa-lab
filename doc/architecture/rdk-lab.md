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
| 6. Topology | the pod in the controller's topology and in em_cli, marked as an OpenSync pod | passed: Agent-1 with kind `opensync-pod`, its own icon, model and manufacturer on hover and in the dashboard (meta-cmf `37c70e8`, controller image `…20260925081052`; installed in place in `rdk-emosa`) |
| 7. Agent behaviour | client steering (BTM) through the pod; metrics on the medium | steering passed: the controller's `steer.sh` → Client Steering Request → EMOSA → `owm` BTM request to the target; the station left the pod's BSS in each run (§7). Metrics on the medium: not started |
| 8. C agent | the C lab prototype (`c/`) in place of the Python agent for the pod, same configuration | passed: `lab.sh agent MVXPOD023F87E628DD c`; onboarded, the controller configured the radio, a `steer.sh` mandate ended with the station off the pod's BSS. `lab.sh agent POD python` switches back |

The lab driver is `deploy/rdk-lab/` in emosa-lab, like `deploy/opensync-lab/`:
a host-side wrapper and a VM-side script that reuses the same components (the
adapter kit, `emosa-gtp`, the fleet).
`lab.sh agent POD python|c` picks the implementation for one pod
(`/etc/default/emosa-POD` in the `emosa` container); the adapter kit builds the
C agent at install.

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
- **Radios come back renamed, and late.** A deleted pod's phys return carrying
  OpenSync's VIFs and no `virt-wlanN`; the driver reclaims its own radios
  before a start, as the lab's allocator does for its roles. They return only
  when the kernel tears the container's network namespace down, which it does
  asynchronously (seconds): the driver waits for them. A radio taken as a
  replacement is a new device to the controller (EMOSA refuses the changed
  radio identity) and is not on the medium until wmediumd is regenerated.
- **The gateway reboots itself.** Under load RDK's self-heal reboots
  `bpibroadband` (last reboot reason `CPU_THRESHOLD`), and brlan0 is rebuilt
  from RDK's own configuration, without `eth2`. A VM timer
  (`emosa-lab-lanport.timer`) puts the port back within 15 s.
- **wmediumd.** The generator includes guest radios (meta-cmf `215535b`); the
  room demo stops while the medium is regenerated.
- **The controller gave the pod's radio up.** RDK's controller configures a
  radio in steps and waits for each answer: a Multi-AP Policy Config Request
  (Ack), channel preference, a Channel Selection Request (response), and the
  Operating Channel Report. Only a radio that completes them is `configured`,
  and only a configured radio is steered (`steer_sta` is otherwise cancelled).
  EMOSA withheld two answers: the policy request carries TLVs it does not
  interpret (Default 802.1Q, Traffic Separation, Channel Scan Reporting,
  Unsuccessful Association, a vendor TLV), and the channel request carries
  preferences for the 40 MHz classes and a transmit power limit of 0 dBm (the
  controller's unset value). Unanswered, the controller retried every second,
  timed out, marked the radio misconfigured and sent a Renew: the uncounted
  `rejected_UNSUPPORTED_OPERATION` seen since step 5. EMOSA now acknowledges
  the policy (companions recorded as not applied) and answers the channel
  request: other classes ignored, the power limit declined (code 2), the
  Operating Channel Report after it. The radio then reaches `configured`.
- **A replaced radio stays in the controller.** After the lost-radio
  replacement the controller kept the pod's old radio (RUID `…:6a:00`) under
  the agent, and its topology sync waited for that radio for ever. Removing
  its rows (`RadioList`, `BSSList`, `OperatingClassList`, `PolicyList`) with
  the controller stopped cleared it. The driver now waits for returning radios
  instead of replacing them.
- **The agent's source lease.** The agent re-reads the pod every 0.5 s and its
  report lives 1.5 s; a refresh that comes later ends the controller session
  (source lost, a fresh onboarding). Under this VM's load that happened a few
  times, and the controller then needs about 40 s to configure the radio
  again. The agent logs each loop step slower than 0.5 s.
- **The Channel Scan Request.** Configuring an agent, RDK's controller sends
  a Channel Scan Request (`0x801B`) and keeps the radio scan-pending until the
  Ack arrives. Unanswered, a steering request later left the radio
  unconfigured. EMOSA now acknowledges it and reports the scan as not
  supported (spec §3.4).
- **The controller's own identity.** The controller writes every M1's
  manufacturer and model onto its own device record as well: its node reads
  "Banana Pi - R4", or "OpenSync via EMOSA" once the pod has onboarded. em_cli
  never classifies the root; the controller itself is unchanged there.

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

On hwsim that never produced a BTM request. Four faults, fixed in opensync-lab
(`daca9a9`):
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
chooses. A target that is a better link needs the guests in the room model
(the room integration step).

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

**Seen live** (2026-09-25): the controller's `steer.sh 02:00:00:00:6c:00
02:00:00:12:75:2c` reached the agent as a Client Steering Request; EMOSA
acknowledged it, opened the window, `owm` took it and sent the BTM request
naming the target after the kick, the station left the pod's BSS, and the
window's rows were deleted. Seven of eight runs ended so; the eighth arrived
while the agent was restarting. Where the station went was its own choice
(§7.1).

**No BTM Report.** OpenSync 6.6 does not expose the station's BTM status: its
band-steering report (`sts.BSReport`) has a `CLIENT_BTM_STATUS` event and a
`btm_status` field, but `owm` never sets either, and no table carries the
response. EMOSA therefore sends no `0x8015` rather than a guessed status. The
controller sees the outcome in the topology: the station leaves the pod's BSS
(Client Association Event) and joins the target.

**Known limitation.** The pod does not tell EMOSA whether a station supports
BTM (`Wifi_Associated_Clients.capabilities` is empty). `owm` deauthenticates a
station without BTM support at once, also for a gentle request.
