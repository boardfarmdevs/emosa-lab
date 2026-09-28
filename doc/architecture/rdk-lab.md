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

## 8. Room integration and the 24-room qualification

Goal (2026-09-25): at least two wired OpenSync pods, with GRE termination, in
the RDK lab's room model, then the full room suite
(`gen/tests/run-easymesh-suite.sh rooms` in meta-cmf-bananapi-vcpe: 24 ordinary
rooms and 3 geometry rooms) with the pods present. prplMesh follows separately.

Decisions:

- **Pods are extra APs.** Every room keeps its gateway and four extenders and
  gains `pod_1` and `pod_2`, in pod-variant golden files. The native rooms stay
  as they are and remain the baseline.
- **Wired means the GTP path, held fixed.** A pod reaches the controller's LAN
  as today (onboarding SSID to `em-gtp`, GRE terminated there, `em-gtp` wired
  into `brlan0`). Its backhaul station's link to the GTP is a fixed link on the
  medium, outside every room geometry (`user.wmediumd.links`,
  `bhaul-sta-50=em-gtp/wlan0:45`, meta-cmf `f2bb9e7`). Only the pod's 2.4 GHz
  fronthaul is in the room.
- **Measurements are real or absent.** See below.

### Step 1: two pods (done)

`pod-1` (`MVXPOD023F87E628DD`, agent `02:72:f9:7f:07:85`) and `pod-2`
(`MVXPOD02D7777EF0D9`, agent `02:c2:b8:31:3a:f8`, fronthaul `82:00:00:00:6a:00`)
are onboarded, each with the five-BSS set. On the way:

- **M1 without M2.** em_ctrl restarted between pod-2's M1 and its M2 and forgot
  the M1; its other queries kept the agent's 130 s silence rule from firing.
  Both agents now search again after 30 s without M2 (spec §2.5).
- **Five children per topology node.** RDK's topology tree held at most
  `EM_MAX_NETWORKS` (5) children per node. Rebuilt after a controller restart,
  before the extenders' backhaul parent is known, it hangs every agent off the
  root, and with two pods the gateway's own agent and Extender-4 fell out of the
  topology API and em_cli. meta-cmf `0211` bounds the children by
  `EM_MAX_DEVICES` (16).
- **Agent-1 is the gateway's agent.** em_cli named Ethernet-attached agents
  Agent-N in traversal order, so the pods took Agent-1 and Agent-2, while the
  lab relies on Agent-1 being the gateway's co-located agent (the room's gateway
  role, the layout's anchor). meta-cmf `0212` names pods Pod-N from their own
  counter. Both patches are installed in place in `rdk-emosa`'s
  `bpibroadband` (`onewifi_em_ctrl`, `onewifi_em_cli`; the previous binaries
  kept as `*.pre-0211`, `*.pre-0212`); the images still need a rebuild.

### Step 2: the native baseline

The room suite runs on rev120 from `~/git/easymesh-labs/meta-cmf-bananapi-vcpe`
(Node 22 in `~/opt/node22`; `EASYMESH_LXD_NAME=rdk-emosa`,
`EASYMESH_HOST_ADDRESS=192.168.2.120`, ports 21020/21021/21022,
`run-easymesh-suite.sh rooms --yes-act`), with EMOSA idle: the pods and `emc-1`
stopped, the fleet stopped, and the pods' rows removed from the controller's
model (the room preflight counts devices, radios and BSSes exactly).

Found before it could run:

- **The room service had failed since 07:21 UTC** (first an incomplete
  topology, then the pods' leftover rows). Restarted once the model was native.
- **The gateway's own agent reported no client metrics.** Every client on
  Agent-1's BSSes had RCPI 0 in the controller and em_cli, so every room failed
  `metricsFresh`, and the room's hero preflight failed. The extenders were
  fine; the controller builds (0211) and em_cli (0212) were ruled out by A/B
  with the image's binaries. OneWifi's EasyMesh app on the gateway was not
  collecting associated-client stats (no `assoc_client_response` in
  `/rdklogs/logs/wifiEM.txt`), although the controller's stored policy asks for
  them and the agent acknowledged the Policy Config Request. Repair: restart
  the gateway's `onewifi`, then `em_agent`, then post the gateway agent's own
  policy again (`/api/v1/wifipolicy`, its current entry unchanged); OneWifi then
  collects and the room converges (20 of 20 clients measured). A policy post
  alone did not help. Probable trigger: the gateway's self-heal reboot earlier
  that day. Seen again on 26 September: the controller sends a Policy Config
  Request only when the posted policy differs from the stored one, so an
  unchanged post sends nothing (checked on the wire). The repair that works:
  restart `onewifi`, then `em_agent`, then post the gateway agent's policy
  with one value changed (the AP metrics interval + 1) and then as it was;
  both requests go out and OneWifi collects within a minute (12 of 12 gateway
  clients measured).

Result (rev120 `test-results/emosa-baseline-20260925T211427Z`, 25 September
2026 14:14–15:49 PDT, meta-cmf `00694d3` with 0211/0212 installed, EMOSA idle):
7 steps passed, 2 failed.

| Step | Result |
| --- | --- |
| guest-audit, default-readiness | passed |
| catalog (24 rooms) | failed: 22 passed; `fifty-client-counter-roam` (at its 18 s pause the 50 clients first converged after 56.4 s of the 60 s window, too late to hold), `received-discovery-recovery` (24 s pause: kernel audit, one station associated to Extender-4 while the controller still had it on Extender-3, `native_owner_mismatch`) |
| geometry | failed: `backhaul-branch-formation` did not converge after loading (candidate measurements 9 of 10), the failure the lab already records for this room; the other two geometry rooms were not reached; default recovery passed |
| rf-hover, rf-access, rf-properties, world-switch (all worlds), restore-default | passed |

This is the reference for the pod-variant run: the same rooms, with pods.

### What the room model needs (meta-cmf-bananapi-vcpe)

- **A pod node kind.** A world must keep every bound `fronthaul_ap` role, so
  pods cannot be bound as that kind without breaking the native rooms. Pods
  get their own kind, which a world may include.
- **Single-band nodes.** The inventory (`wmdcfg/inventory.py`) and the world
  loader (`room_demo/worlds.py`) require tri-band mesh nodes on one PHY; a pod
  in the room is one 2.4 GHz radio. Its links on 5 and 6 GHz do not exist.
- **Pods in the layouts.** Positions for `pod_1` and `pod_2` in each layout and
  new golden files. Outside the room model a pod's radio has the medium's
  default SNR (40 dB) to every radio, i.e. strong everywhere.
- **The acceptance's fixed topology.** `room-feature-acceptance.js` requires
  six topology nodes and four backhaul parents; the pod variant has eight nodes,
  the pods attached over Ethernet.

### Measurements from the pods

The optimizer and the room gates need two measurements per client:

| Measurement | Used for | Pod source | Freshness |
| --- | --- | --- | --- |
| Serving RCPI of each client on a pod | `metricsFresh` (≤ 30 s), the policy's current metric (≤ 60 s) | OpenSync client reports (`sts.Report`, EMOSA telemetry) | `sm` reporting interval plus `qm`'s publish interval, which `AWLAN_Node.mqtt_settings` `agg_stats_interval` sets (default 60 s) |
| Candidate RCPI of a client at a pod | the optimizer's candidates (Unassociated STA Link Metrics, per agent) | probe requests: hostapd `RX-PROBE-REQUEST` signal → `ow_steer_bm` `PROBE` events with RSSI in the band-steering report | only for stations with a `Band_Steering_Clients` row; the report every 60 s (fixed) |

Both travel over the pods' MQTT telemetry, which the RDK lab does not run yet:
it needs the broker stage of the OpenSync lab driver (mutual TLS, lab CA) at
`10.101.0.40:8883`. A candidate measurement is reported with its age (the
response carries it); whether the optimizer's candidate handling accepts an
age of up to about a minute is to be checked. If the pods cannot measure
honestly, how the gates treat an abstaining agent is the user's decision.

### Step 4: measurements (in progress)

**Telemetry** runs in the lab (`lab.sh telemetry`): the broker stage at
`10.101.0.40:8883` (mutual TLS, lab CA, a device certificate per pod), each
pod publishing to `emosa/stats/<serial>` with raw client and on-channel survey
reports every 5 s and `qm` publishing every 5 s (`agg_stats_interval`).

**Serving metrics** (spec §3.8). Each pod's agent sends an AP Metrics Response
every 5 s, as the controller's Metric Reporting Policy asks: the channel
utilization measured by the survey (about 41 % busy on channel 6), the
declared best-effort ESP of the profile (`3fff00`, as the native agents), and
per station with a fresh report its link metrics (RCPI from the SNR) and
traffic counters. Two defects hid this at first: a changed telemetry request
(survey, publish interval) was not written on the same OpenSync start, and the
session did not monitor `Wifi_Stats_Config.survey_type`, so the agent never
saw its own survey row and the write timed out. Also corrected: a survey
sample's time is the report's time minus its `offset_ms` (dppline.c), not plus.

**Candidate measurements** (spec §3.9), checked on `pod-1` by hand with a
`Band_Steering_Config` group on `home-ap-24` and inert `Band_Steering_Clients`
rows (`cs_mode` off, every kick and preference off):

- hostapd delivers `RX-PROBE-REQUEST sa=… signal=…` to `owm`, which attaches
  with `probe_rx_events=1` (it also emits the older `NL80211-CMD-FRAME
  type=EVT-FRAME-PROBE-REQ`, hostap patch 991, which `owm` 6.6 ignores);
- `owm` records a `PROBE` event only for a station with a client row, with the
  SNR over its fixed -96 dBm floor (28 for a -68 dBm probe);
- the band-steering report (`sts.BSReport`) follows every 60 s (fixed in
  `ow_steer_bm`), each event with `offset_ms` before the report's time. A
  recorded report is the fixture `tests/fixtures/opensync/pod-6.6.1-hwsim-bs-probe.hex`.

An associated station probes only when it scans (the probes above came from
`iw scan` on `wlan-client-008`). A pod therefore measures few candidates, and
each measurement can be a minute or more old; the response carries its age,
and a probe older than two minutes is not reported.

Stage A (done): both agents answer the Unassociated STA Link Metrics Query.
The Ack refuses every station the pod cannot report (Error Code `0x01` when it
is associated with the pod, `0x02` otherwise), and the response lists the
others. RDK's controller completes a query that refuses every station on the
Ack alone (meta-cmf patch 0140), so the optimizer's candidate snapshot is
complete. The C agent has no telemetry and refuses every station; the Python
agent measures stations whose probes the pod reports.

Stage B: the agent writes the group and a watch row for each station the
controller asks about on the pod's channel (spec §3.9: at most 32, marked
`cs_params` `{"emosa": "watch"}`, dropped after 10 minutes without a query,
written at most every 10 s), so the pods hear their probes. A steering window
replaces the station's watch row. Deployed in `rdk-emosa` (EMOSA `2815061`)
for runs 6 to 8 of the pod-variant suite.

### Step 5: the pod-variant suite (in progress)

`run-easymesh-suite.sh rooms` with `EASYMESH_ROOM_WORLDS_ROOT=gen/wmediumd/configurator/worlds-pods`
on `rdk-emosa`. Run 8 (2026-09-26 12:01 UTC, meta-cmf `1b6b454`, EMOSA
`2815061`): 7 of 9 steps passed; the catalog passed 23 of 24 rooms; geometry
failed one of its three rooms. The catalog failure also failed in the native
baseline; geometry was not repeatable natively either. Both are native-lab
issues the pods make somewhat worse, not pod defects:

| Room | Cause | Fix |
| --- | --- | --- |
| `band-ap-counter-roam` (runs 6, 7) | the band init's REASSOCIATE scan is active and 40 ms long on one channel; the client missed the gateway's 2.4 GHz BSS at -37 dBm and joined a pod or an extender at -63 to -69 dBm; after the optimizer's AP steer, wpa_supplicant 2.11 roamed on to 5 GHz on its own (within-ESS, better estimated throughput) and the expected 2.4 to 5 GHz step was never verified | meta-cmf `1b6b454`: a passive scan-only scan of the initial band before reassociating; passed in run 8 |
| `fifty-client-counter-roam` (native baseline, three of four pod runs) | the optimizer's candidate collection is too slow for 50 clients: one round over 16 radios, one agent at a time, takes 10 to 13 s, and a steered client's snapshot needs up to two more rounds; convergence lands near the window's end (checkpoint 40 to 56 s of 60, load 65 to 91 s of 90). Two pod-side stalls on top: a second steer to a pod while its window was open was refused as busy after its Ack (the verification timed out), and after a pod rejoined, the controller dropped the Channel Scan Request ACK (patch 0011 routes only metrics, steering and policy ACKs), kept both pod radios in `channel_scan_pending` for 40 s and more, refused candidate queries to them (Error_Not_Ready) and held every action on an incomplete snapshot | meta-cmf `d228a62` (em_cli: candidate steps first for the native lock), `762a5ec`, `daa72e3`, `9e9b4db`, `b7b6e6d` (optimizer: one inventory read per second, rounds ask only for due pairs, missing pairs first): a query 530 to about 440 ms, a round 16 to 7 queries (about 4 s), checkpoint 31 to 49 s. EMOSA `57449bd` (mandates queue behind an open window) and meta-cmf `1e253bc` (unified-wifi-mesh 0215: the scan ACK completes the request). Then meta-cmf `a915f2d`: profiling acts on five unsettled steers, and a steer the client did not follow held its slot for the whole 40 s verification (four such steers left one slot, one steer per round); a steer now counts for 8 s (1058 verified steers: p99 5.8 s). Four of four runs passed, load 54 to 83 s |
| geometry (three rooms) | not repeatable, with or without pods: two full passes in twelve runs. Two native causes, found with complete journals (meta-cmf `bab9032`, `gen/lab-journal-evidence.sh`), hwsim captures and in-namespace captures: (1) an extender revoked its backhaul when one root proof renewal stayed unanswered for 2 s, and a 2 to 3 s hiccup at an RF change expired all four at once (about 45 s mesh outage); (2) an extender's 1905 daemon deleted the controller from its topology when one Topology Query went unanswered for 5 s, then dropped every CMDU to it ("No destination_mac found") until the proof expired | meta-cmf `c86b2da` (unified-wifi-mesh 0213: renewals retry for 8 s) and `c27015f` (ieee1905 0009: keep a live node, reset only the stalled query). With both (seven runs): all three rooms passed in six, the default restore in five; no 1905 drops to the controller remain. The misses are convergence times at the edge of the windows (load 90 s, restore 60 s): clients converge, dip by one and reconverge seconds after the deadline |

These were first installed in place in `rdk-emosa`: controller patches 0211,
0212 and 0215; OneWifi 0040 (64 unassociated stations per channel) in the
gateway and extenders, 0041 (one backhaul connection attempt per scan) in the
extenders; unified-wifi-mesh 0213 and ieee1905 0009 in all of them; em_cli 0214.
Since 28 Sep all of them, and every later one, are in the images (below).

### Step 6: the pods on Wi-Fi backhaul

`lab.sh backhaul wifi` moves both pods from the GTP path to the gateway's
5 GHz `mesh_backhaul` (EMOSA's option 1, spec §8.3): the fleet gives each pod
`uplink.mode = multi-ap` pinned to that BSS (per-pod settings from the pod
container's `user.emosa.backhaul`, so `lab.sh fleet` keeps them), and the pod's
station gets a fixed 50 dB link to the gateway's radio, as strong as the lab's
own gateway-extender backhaul (`fixed-startup-mesh`). `lab.sh backhaul wired`
returns them: it drops both and restarts the pod's OpenSync, which comes up on
its bootstrap GTP path; EMOSA never switches a pod back by itself. Both pods
switched within a minute of their agent being provisioned.

What the RDK side needed:

| Found | Fix |
| --- | --- |
| The pod's station on the gateway's backhaul, but the controller hung the pod off the root as if wired: it builds its backhaul tree from backhaul-STA rows, and only RDK's vendor operational BSS TLV creates them. EMOSA reports the station in standard TLVs (Device Information: 802.11, non-AP STA, upstream BSSID as network membership) | meta-cmf `343db84`, unified-wifi-mesh 0216: without the vendor snapshot such a station gets a backhaul-STA row of its own (its radio is the station, which the controller does not model), keyed by its upstream BSSID, removed when no longer reported. A first version keyed it by the station MAC: the database write never found it and inserted it again on every topology response (about 100 rows in 20 minutes) |
| em_cli drew the gateway's agent as a pod (Pod-2) and shifted every Agent-N and Pod-N name: it read a device's Manufacturer with a depth-first, prefix-matching subtree search, which found the pod below the gateway's agent first | meta-cmf `ce6906b`, patch 0217: a device's identity from its own keys |
| The room's health stayed false with the pods on Wi-Fi: a pod on the backhaul adds a backhaul-STA row and an association at its parent | meta-cmf `05d376b` (mesh_health counts pods on Wireless LAN backhaul) and `b2398a4` (the room compares with mesh_health's expectations instead of its own copy) |
| emosa-fleet had refused its own configuration since the telemetry settings were added (30383 restarts; the running agents hid it): the fleet contract lacked the agent contract's `publish_interval` and `survey` | EMOSA `20b64a3`, with a test holding the fleet's per-agent settings equal to the agent's |
| A pod's station could not be pinned to a lab mesh node on the medium | meta-cmf `abca04c`: `user.wmediumd.links` may name a mesh node's radio |

The suite with both pods on Wi-Fi backhaul (rev120
`test-results/emosa-pods-wifi-20260927T071344Z`, reruns in
`emosa-pods-wifi-rerun-20260927T083826Z`): the catalog passed 23 of 24 rooms;
the one miss, `large-room-extender-evacuation` (load 90.5 s of 90), passed
twice on its own (34 and 41 s). Geometry passed two of three reruns, as
before without pods on Wi-Fi. Three more fixes came out of it:

| Found | Fix |
| --- | --- |
| Every pod-variant room failed its convergence checks with the mesh intact: `meshConnected` wanted exactly four backhaul edges, and a pod on Wi-Fi adds its own | meta-cmf `335e5fd`: each of the world's own extenders must reach the gateway, and every other edge too |
| rf-hover: one pod's hover lost all its BSSes. The tooltip skipped a whole group named `mesh_backhaul`, and em_cli names a pod's single group after its first BSS | meta-cmf `476cd3f`: backhaul BSSes are left out one by one (installed in place: `room-topology.js` in em_cli's static directory, `*.pre-476cd3f`) |
| world-switch and restore-default: the room session died. A candidate reply measuring a station its query did not ask for killed the optimizer worker (four times since 26 Sep, all on native radios) | meta-cmf `1b6e007`: such a reply is discarded and the query retried |

The pods' backhaul is fixed, like the native extenders' in every room except
the geometry rooms; in those, the native backhaul follows the geometry and the
pods' stays at 50 dB to the gateway (EMOSA pins the upstream BSSID and refuses
Backhaul Steering). Modelling the pods' backhaul in the geometry is a later
step. Installed in place in `rdk-emosa`: controller 0216 (`*.pre-0216`), em_cli
0217 (`*.pre-0217`), the fleet contract in the emosa container.

### Step 7: a wired EasyMesh extender

The pod rooms with one more AP: the lab's own extender image as `bpiap-004`,
its LAN port bridged into the controller's LAN (`br-emosa`) instead of a
Wi-Fi backhaul station, room role `extender_5` (meta-cmf
`worlds-pods-wired/`, manifest `private-client-room-walk-pods-wired.json`).
`lab.sh rooms pods-wired` runs the room service on it; meta-cmf
`gen/wired-extender.sh up|down|status` makes or removes the extender. It
onboards over Ethernet with its three radios and ten BSSes and em_cli names it
`Extender-N` (unified-wifi-mesh 0218: only the gateway's co-located agent is
`Agent-1`).

A wired extender must never have a second path into the LAN, and the lab has
three ways of giving it one; each is closed:

| Found | Fix |
| --- | --- |
| A pool radio handed to a new container is on the medium at once (idle pool radios at the default SNR), so creating the extender with its LAN port let its backhaul station associate too: a short L2 loop | `wired-extender.sh up` creates it without a LAN port, marks it `user.easymesh.backhaul=wired`, regenerates the medium (gen-config gives it -20 dB to every mesh node, meta-cmf `c9e1ca6`), and only then bridges `eth1` |
| The room engine set AP-to-AP 5180 MHz overrides from positions, one of them 23 dB between the wired extender and the gateway: its station associated while `eth1` was bridged, a live L2 loop; both pods' uplink switches timed out in it and EMOSA held them on option 2 | meta-cmf `113d925`: no AP pair that includes a `wired_backhaul` role; `57bdd87`: the extender's unit keeps every station interface down |
| OneWifi's station selfheal disables and enables every radio once the extender station has been disconnected for half the selfheal publish time (5 minutes): on a wired extender it never connects, so every AP went down 5 minutes after each OneWifi start | meta-cmf `69eafdb`: `up` sets `/nvram/selfheal_event_publish_time` beyond reach (OneWifi's own Ethernet backhaul signal needs `RDKB_EXTENDER_ENABLED`, which the image does not build) |

What else it needed:

| Found | Fix |
| --- | --- |
| em_agent's start waits for a bridged Wi-Fi backhaul | unified-wifi-mesh bbappend `e8682fa`: an Ethernet port of `brlan0` with carrier also ends the wait |
| The extender needs the reference extender's in-place binaries, and a newer OneWifi with the image's `libwifi_bus` or `libwifi_webconfig` never finishes starting | `wired-extender.sh` copies OneWifi with its own libraries, em_agent and ieee1905 from `bpiap` |
| RDK does not bridge `eth1` in extender mode | the extender's unit keeps `eth1` a port of `brlan0` |
| The room model: a wired AP has no backhaul links, geometry or not; the controller model gains its device but no backhaul association | meta-cmf `5e9a207`, `c6a0b8d`: `"backhaul": "wired"` in the layout, the world's `wired_backhaul`, `expected_lab.wired_devices`, health one association fewer, `meshConnected` expects no edge for it |
| A pod held on option 2 could not be released | EMOSA `ba432a6`: `lab.sh backhaul wifi` releases the hold and restarts the pod's OpenSync (a switch is made once per pod start) |
| A wmediumd restart cost every Wi-Fi extender its station and APs until OneWifi and em_agent were restarted | `wired-extender.sh` and `lab.sh medium` restart the medium only when its configuration changes |

After any agent restart the restarted agents reported RCPI 0 for their clients:
the controller sent its Multi-AP Policy Config only after a channel preference
exchange the RDK agents never complete, and not at all to an agent onboarding
again, so only a posted policy change (the lab's policy bump) restored the
metrics. unified-wifi-mesh 0219 (meta-cmf `c4a167f`) sends every agent its
Metric Reporting Policy at topology sync; the full policy there broke the
gateway's onboarding. And 0220 keeps a pod's learned backhaul station across a
controller restart, which had put the pod back on Ethernet in the model.
meta-cmf `gen/lab-bringup.sh up` (in the VM, as root) brings the lab back from
a medium or controller restart in that order, recovering the Wi-Fi extenders
whose fronthaul or backhaul station stayed down, and settles the room; `status`
shows every node, the controller's topology and model, and the room.

The suite with the wired extender and both pods on Wi-Fi backhaul (rev120
`test-results/emosa-pods-wired-20260927T191447Z`, reruns in
`emosa-pods-wired-rerun-20260927T204722Z`): the catalog passed 23 of 24
rooms, and the wired extender kept its six fronthaul BSSes throughout (its
unit never had to repair or take a station down). The one catalog miss,
`home-a-flash-crowd`, did not converge within 90 s of loading: a client the
room makes dormant was still "on" the wired extender. It passed twice on its
own; the cause (an agent bug, fixed by unified-wifi-mesh 0221) is under the
images below. Four more steps failed at once on gates that knew only the native
and pod default worlds; with the pods-wired default added (meta-cmf `81fd17e`,
`98981b9`) default-readiness, rf-properties, world-switch and restore-default
all passed. rf-hover and rf-access passed in the suite. Geometry passed one
run of four, each failure in a room where RDK's own parent selection must
follow the moved APs: `backhaul-parent-handover` in the suite (extender_3
never moved under extender_2), `backhaul-branch-formation` in two reruns
(the branch not formed within 60 s, then recovery not verified; its
ten-client room not converged before the movement). The geometry rooms also
failed in the native baseline and passed two runs of three with the pods on
Wi-Fi; the wired extender has no backhaul links in them.

### The images (28 Sep)

Everything that was installed in place is in the images since 28 Sep:
controller `X86EMLTRBPIBB_rdk-next_20260928034131`, extender
`X86EMLTRBPIAP_rdk-next_20260928035241`, both from meta-cmf `c3d8560` with
`doc/easymesh/build/scripts/build-images.sh`. Each node was redeployed with
`bpi.sh` and its own nvram (the same AL MAC, radios and controller database),
the gateway's `emosa-lan` port and em_cli drop-in and the wired extender
(`wired-extender.sh up 4`) restored; no `*.pre-*` file is left in any node.
meta-cmf `gen/lab-bringup.sh status|up|room` brings the lab back after a
redeploy, a medium or a controller restart.

| Found | Fix |
| --- | --- |
| Patch 0214 never applied in a clean build: it changed `candidate_coordination.go`, a file the recipe copies in after the patches (the lab had it only from in-place builds) | meta-cmf `ba10c66`: the change is in the layer's copy of the file |
| Restarted agents reported RCPI 0: the controller sent its Multi-AP Policy Config only after a channel preference exchange the RDK agents never complete, and not to an agent onboarding again | unified-wifi-mesh 0219 (`c4a167f`): the Metric Reporting Policy at topology sync. The full policy there broke the gateway's onboarding (a radio stuck in WSC) |
| After a controller restart a pod on Wi-Fi fell back to Ethernet in the model: its learned backhaul-STA row came back from the database without mode or station | 0220 (`a624fd1`) |
| `home-a-flash-crowd` failed in both pods-wired suites. An agent rejected every full station snapshot without a client ("unknown reporting RUID": the command's model had no radios), so the withdrawal of an AP's last client was lost and the agent kept reporting the client for about 3 minutes; once the client left its new AP, the controller put it back on the old one. The wired extender at the room's edge usually serves one client. A roam test (client onto bpiap-004, roamed away, disconnected) reproduced it 5 of 5, never on a Wi-Fi extender | 0221 (`c3d8560`): 0 of 2 in the roam test; `home-a-fast-transit` then `home-a-flash-crowd` (the suite's order) passed twice |
| Journal evidence mode stopped half way: em_agent's start now waits for a backhaul, longer than the script's 60 s per command | `225ffc4` |
| The room would not start after a hand test: its preflight counts the whole client pool online (it pauses dormant clients itself) | `lab-bringup.sh room` reconnects the pool first (`6e44b35`) |

The suite on the images (before 0221, rev120
`test-results/emosa-pods-wired-images-20260928T011145Z`): seven steps of nine,
the catalog 21 of 24. Reruns on the 0221 images
(`emosa-pods-wired-0221-rerun-20260928T050447Z`): `home-a-fast-transit` then
`home-a-flash-crowd` 2 of 2, `received-discovery-recovery` 2 of 2,
`fifty-client-counter-roam` 1 of 2, geometry 0 of 3 (one run passed all three
rooms' checks and missed only the 60 s default recovery).

Open: candidate collection with the wired extender's three more radios (20
instead of 17). In `fifty-client-counter-roam` the fleet's candidate count
rises towards 250 and falls back as measurements age out before one round over
all radios completes; the geometry rooms' convergence windows miss for the
same reason (measurements incomplete before the movement or at the default
recovery). This is the optimizer's collection rate, not the images.

### Order

1. Two pods through the GTP path, backhaul held fixed (done).
2. Native baseline: the room suite on `rdk-emosa` with EMOSA idle (done).
3. Pods in the room model (above).
4. Measurements: telemetry in the RDK lab; serving metrics; candidates from probe
   requests.
5. The pod-variant suite over the 24 rooms, compared with the baseline.
6. The pods on Wi-Fi backhaul and its suite (done).
7. A wired EasyMesh extender next to the pods and its suite (done); the
   images carrying every change (done).
