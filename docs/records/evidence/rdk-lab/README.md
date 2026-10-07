# EMOSA in the RDK lab: the record, 25 to 29 September 2026

**Record.** This is how EMOSA came into the RDK EasyMesh lab: the first pod, the
room integration and the 24-room qualification, and EMOSA as an option of the
lab with the pods on Python, on C and mixed. It is dated and not edited
afterwards. The current design and how to run it are in
[`docs/concepts/rdk-lab.md`](../../../concepts/rdk-lab.md); section
references (§N) below are to that document as it was then (emosa-lab of
29 September). VM names, run directories and commits are as they were: `rdk-emosa`
was the first lab VM, stopped on 29 September and kept as a snapshot, and
`rdk-emosa-0929` its successor built from scratch. Run directories are on
rev120 under `test-results/` or `easymesh-builds/`.

## 1. The first pod (25 September)

The experiment ran in its own RDK lab VM, `rdk-emosa` on rev120, built from
the same images as the fresh reference VM `rdk-0925`.

### Steps and acceptance

| Step | Done when | Result (2026-09-25) |
| --- | --- | --- |
| 1. VM `rdk-emosa` | the lab's own build gates pass | passed: health audit, ready with 105 radios, room and survey |
| 2. Wired LAN port | `bpibroadband` has `eth2` in `brlan0`; a host on `br-emosa` gets a lease | passed: a test host leased `10.0.0.98` from `10.0.0.1` |
| 3. EMOSA and GTP | fleet at `10.101.0.40:6640`; the GTP serves `opensync-lab-bhaul` on a pool radio on the medium | passed: channel 44, underlay `169.254.2.1/25` |
| 4. Pod | the unchanged pod joins the GTP, dials `.40`, gets an agent | passed: GRE up, `br-home` `10.0.0.155` from the RDK router, agent `02:72:f9:7f:07:85` |
| 5. Onboarding | the RDK controller onboards the agent; the pod runs its fronthaul; a client has internet | passed: the five-BSS M2 set applied (`private_ssid`, `iot_ssid`, `lnf_radius`, `hotspot`, `mesh_backhaul`); a client on the pod's `private_ssid` reached the internet |
| 6. Topology | the pod in the controller's topology and in em_cli, marked as an OpenSync pod | passed: Agent-1 with kind `opensync-pod`, its own icon, model and manufacturer on hover and in the dashboard (meta-cmf of 25 September, controller image `…20260925081052`; installed in place in `rdk-emosa`) |
| 7. Agent behaviour | client steering (BTM) through the pod; metrics on the medium | steering passed: the controller's `steer.sh` → Client Steering Request → EMOSA → `owm` BTM request to the target; the station left the pod's BSS in each run (§7). Metrics on the medium: not started |
| 8. C agent | the C lab prototype (`c/`) in place of the Python agent for the pod, same configuration | passed: `lab.sh agent MVXPOD023F87E628DD c`; onboarded, the controller configured the radio, a `steer.sh` mandate ended with the station off the pod's BSS. `lab.sh agent POD python` switches back |

### Found on the way

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
- **wmediumd.** The generator includes guest radios (meta-cmf of 24 September); the
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

### Client steering, seen live

**Seen live** (2026-09-25): the controller's `steer.sh 02:00:00:00:6c:00
02:00:00:12:75:2c` reached the agent as a Client Steering Request; EMOSA
acknowledged it, opened the window, `owm` took it and sent the BTM request
naming the target after the kick, the station left the pod's BSS, and the
window's rows were deleted. Seven of eight runs ended so; the eighth arrived
while the agent was restarting. Where the station went was its own choice
(§7.1).

## 2. Room integration and the 24-room qualification (25 to 29 September)

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
  `bhaul-sta-50=em-gtp/wlan0:45`, meta-cmf of 25 September). Only the pod's 2.4 GHz
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
2026 14:14–15:49 PDT, meta-cmf of 25 September with 0211/0212 installed, EMOSA idle):
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

### Step 4: measurements (done)

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
replaces the station's watch row. Deployed in `rdk-emosa` (EMOSA of 25 September)
for runs 6 to 8 of the pod-variant suite.

### Step 5: the pod-variant suite (done)

`run-easymesh-suite.sh rooms` with `EASYMESH_ROOM_WORLDS_ROOT=gen/wmediumd/configurator/worlds-pods`
on `rdk-emosa`. Run 8 (2026-09-26 12:01 UTC, meta-cmf of 26 September, EMOSA
of 25 September): 7 of 9 steps passed; the catalog passed 23 of 24 rooms; geometry
failed one of its three rooms. The catalog failure also failed in the native
baseline; geometry was not repeatable natively either. Both are native-lab
issues the pods make somewhat worse, not pod defects:

| Room | Cause | Fix |
| --- | --- | --- |
| `band-ap-counter-roam` (runs 6, 7) | the band init's REASSOCIATE scan is active and 40 ms long on one channel; the client missed the gateway's 2.4 GHz BSS at -37 dBm and joined a pod or an extender at -63 to -69 dBm; after the optimizer's AP steer, wpa_supplicant 2.11 roamed on to 5 GHz on its own (within-ESS, better estimated throughput) and the expected 2.4 to 5 GHz step was never verified | meta-cmf of 26 September: a passive scan-only scan of the initial band before reassociating; passed in run 8 |
| `fifty-client-counter-roam` (native baseline, three of four pod runs) | the optimizer's candidate collection is too slow for 50 clients: one round over 16 radios, one agent at a time, takes 10 to 13 s, and a steered client's snapshot needs up to two more rounds; convergence lands near the window's end (checkpoint 40 to 56 s of 60, load 65 to 91 s of 90). Two pod-side stalls on top: a second steer to a pod while its window was open was refused as busy after its Ack (the verification timed out), and after a pod rejoined, the controller dropped the Channel Scan Request ACK (patch 0011 routes only metrics, steering and policy ACKs), kept both pod radios in `channel_scan_pending` for 40 s and more, refused candidate queries to them (Error_Not_Ready) and held every action on an incomplete snapshot | meta-cmf of 26 September (em_cli: candidate steps first for the native lock), (26 September) (optimizer: one inventory read per second, rounds ask only for due pairs, missing pairs first): a query 530 to about 440 ms, a round 16 to 7 queries (about 4 s), checkpoint 31 to 49 s. EMOSA of 26 September (mandates queue behind an open window) and meta-cmf of 26 September (unified-wifi-mesh 0215: the scan ACK completes the request). Then meta-cmf of 26 September: profiling acts on five unsettled steers, and a steer the client did not follow held its slot for the whole 40 s verification (four such steers left one slot, one steer per round); a steer now counts for 8 s (1058 verified steers: p99 5.8 s). Four of four runs passed, load 54 to 83 s |
| geometry (three rooms) | not repeatable, with or without pods: two full passes in twelve runs. Two native causes, found with complete journals (meta-cmf of 26 September, `gen/lab-journal-evidence.sh`), hwsim captures and in-namespace captures: (1) an extender revoked its backhaul when one root proof renewal stayed unanswered for 2 s, and a 2 to 3 s hiccup at an RF change expired all four at once (about 45 s mesh outage); (2) an extender's 1905 daemon deleted the controller from its topology when one Topology Query went unanswered for 5 s, then dropped every CMDU to it ("No destination_mac found") until the proof expired | meta-cmf of 26 September (unified-wifi-mesh 0213: renewals retry for 8 s) and (26 September) (ieee1905 0009: keep a live node, reset only the stalled query). With both (seven runs): all three rooms passed in six, the default restore in five; no 1905 drops to the controller remain. The misses are convergence times at the edge of the windows (load 90 s, restore 60 s): clients converge, dip by one and reconverge seconds after the deadline |

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
| The pod's station on the gateway's backhaul, but the controller hung the pod off the root as if wired: it builds its backhaul tree from backhaul-STA rows, and only RDK's vendor operational BSS TLV creates them. EMOSA reports the station in standard TLVs (Device Information: 802.11, non-AP STA, upstream BSSID as network membership) | meta-cmf of 26 September, unified-wifi-mesh 0216: without the vendor snapshot such a station gets a backhaul-STA row of its own (its radio is the station, which the controller does not model), keyed by its upstream BSSID, removed when no longer reported. A first version keyed it by the station MAC: the database write never found it and inserted it again on every topology response (about 100 rows in 20 minutes) |
| em_cli drew the gateway's agent as a pod (Pod-2) and shifted every Agent-N and Pod-N name: it read a device's Manufacturer with a depth-first, prefix-matching subtree search, which found the pod below the gateway's agent first | meta-cmf of 26 September, patch 0217: a device's identity from its own keys |
| The room's health stayed false with the pods on Wi-Fi: a pod on the backhaul adds a backhaul-STA row and an association at its parent | meta-cmf of 26 September (mesh_health counts pods on Wireless LAN backhaul) and (26 September) (the room compares with mesh_health's expectations instead of its own copy) |
| emosa-fleet had refused its own configuration since the telemetry settings were added (30383 restarts; the running agents hid it): the fleet contract lacked the agent contract's `publish_interval` and `survey` | EMOSA of 26 September, with a test holding the fleet's per-agent settings equal to the agent's |
| A pod's station could not be pinned to a lab mesh node on the medium | meta-cmf of 26 September: `user.wmediumd.links` may name a mesh node's radio |

The suite with both pods on Wi-Fi backhaul (rev120
`test-results/emosa-pods-wifi-20260927T071344Z`, reruns in
`emosa-pods-wifi-rerun-20260927T083826Z`): the catalog passed 23 of 24 rooms;
the one miss, `large-room-extender-evacuation` (load 90.5 s of 90), passed
twice on its own (34 and 41 s). Geometry passed two of three reruns, as
before without pods on Wi-Fi. Three more fixes came out of it:

| Found | Fix |
| --- | --- |
| Every pod-variant room failed its convergence checks with the mesh intact: `meshConnected` wanted exactly four backhaul edges, and a pod on Wi-Fi adds its own | meta-cmf of 27 September: each of the world's own extenders must reach the gateway, and every other edge too |
| rf-hover: one pod's hover lost all its BSSes. The tooltip skipped a whole group named `mesh_backhaul`, and em_cli names a pod's single group after its first BSS | meta-cmf of 27 September: backhaul BSSes are left out one by one (installed in place: `room-topology.js` in em_cli's static directory, `*.pre-476cd3f`) |
| world-switch and restore-default: the room session died. A candidate reply measuring a station its query did not ask for killed the optimizer worker (four times since 26 Sep, all on native radios) | meta-cmf of 27 September: such a reply is discarded and the query retried |

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
| A pool radio handed to a new container is on the medium at once (idle pool radios at the default SNR), so creating the extender with its LAN port let its backhaul station associate too: a short L2 loop | `wired-extender.sh up` creates it without a LAN port, marks it `user.easymesh.backhaul=wired`, regenerates the medium (gen-config gives it -20 dB to every mesh node, meta-cmf of 27 September), and only then bridges `eth1` |
| The room engine set AP-to-AP 5180 MHz overrides from positions, one of them 23 dB between the wired extender and the gateway: its station associated while `eth1` was bridged, a live L2 loop; both pods' uplink switches timed out in it and EMOSA held them on option 2 | meta-cmf of 27 September: no AP pair that includes a `wired_backhaul` role; (27 September): the extender's unit keeps every station interface down |
| OneWifi's station selfheal disables and enables every radio once the extender station has been disconnected for half the selfheal publish time (5 minutes): on a wired extender it never connects, so every AP went down 5 minutes after each OneWifi start | meta-cmf of 27 September: `up` sets `/nvram/selfheal_event_publish_time` beyond reach (OneWifi's own Ethernet backhaul signal needs `RDKB_EXTENDER_ENABLED`, which the image does not build) |

What else it needed:

| Found | Fix |
| --- | --- |
| em_agent's start waits for a bridged Wi-Fi backhaul | unified-wifi-mesh bbappend (27 September): an Ethernet port of `brlan0` with carrier also ends the wait |
| The extender needs the reference extender's in-place binaries, and a newer OneWifi with the image's `libwifi_bus` or `libwifi_webconfig` never finishes starting | `wired-extender.sh` copies OneWifi with its own libraries, em_agent and ieee1905 from `bpiap` |
| RDK does not bridge `eth1` in extender mode | the extender's unit keeps `eth1` a port of `brlan0` |
| The room model: a wired AP has no backhaul links, geometry or not; the controller model gains its device but no backhaul association | meta-cmf of 27 September, (27 September): `"backhaul": "wired"` in the layout, the world's `wired_backhaul`, `expected_lab.wired_devices`, health one association fewer, `meshConnected` expects no edge for it |
| A pod held on option 2 could not be released | EMOSA of 27 September: `lab.sh backhaul wifi` releases the hold and restarts the pod's OpenSync (a switch is made once per pod start) |
| A wmediumd restart cost every Wi-Fi extender its station and APs until OneWifi and em_agent were restarted | `wired-extender.sh` and `lab.sh medium` restart the medium only when its configuration changes |

After any agent restart the restarted agents reported RCPI 0 for their clients:
the controller sent its Multi-AP Policy Config only after a channel preference
exchange the RDK agents never complete, and not at all to an agent onboarding
again, so only a posted policy change (the lab's policy bump) restored the
metrics. unified-wifi-mesh 0219 (meta-cmf of 27 September) sends every agent its
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
and pod default worlds; with the pods-wired default added (meta-cmf of 27 September)
default-readiness, rf-properties, world-switch and restore-default
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
`X86EMLTRBPIAP_rdk-next_20260928035241`, both from meta-cmf of 27 September with
`doc/easymesh/build/scripts/build-images.sh`. Each node was redeployed with
`bpi.sh` and its own nvram (the same AL MAC, radios and controller database),
the gateway's `emosa-lan` port and em_cli drop-in and the wired extender
(`wired-extender.sh up 4`) restored; no `*.pre-*` file is left in any node.
meta-cmf `gen/lab-bringup.sh status|up|room` brings the lab back after a
redeploy, a medium or a controller restart.

| Found | Fix |
| --- | --- |
| Patch 0214 never applied in a clean build: it changed `candidate_coordination.go`, a file the recipe copies in after the patches (the lab had it only from in-place builds) | meta-cmf of 27 September: the change is in the layer's copy of the file |
| Restarted agents reported RCPI 0: the controller sent its Multi-AP Policy Config only after a channel preference exchange the RDK agents never complete, and not to an agent onboarding again | unified-wifi-mesh 0219 (27 September): the Metric Reporting Policy at topology sync. The full policy there broke the gateway's onboarding (a radio stuck in WSC) |
| After a controller restart a pod on Wi-Fi fell back to Ethernet in the model: its learned backhaul-STA row came back from the database without mode or station | 0220 (27 September) |
| `home-a-flash-crowd` failed in both pods-wired suites. An agent rejected every full station snapshot without a client ("unknown reporting RUID": the command's model had no radios), so the withdrawal of an AP's last client was lost and the agent kept reporting the client for about 3 minutes; once the client left its new AP, the controller put it back on the old one. The wired extender at the room's edge usually serves one client. A roam test (client onto bpiap-004, roamed away, disconnected) reproduced it 5 of 5, never on a Wi-Fi extender | 0221 (27 September): 0 of 2 in the roam test; `home-a-fast-transit` then `home-a-flash-crowd` (the suite's order) passed twice |
| Journal evidence mode stopped half way: em_agent's start now waits for a backhaul, longer than the script's 60 s per command | (27 September) |
| The room would not start after a hand test: its preflight counts the whole client pool online (it pauses dormant clients itself) | `lab-bringup.sh room` reconnects the pool first (27 September) |

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
recovery). This is the optimizer's collection rate, not the images (below).

### The open items (28 Sep)

What the images left open, each traced to its cause in the lab (meta-cmf
(28 September) to (28 September), unified-wifi-mesh 0222 to 0230):

| Found | Fix |
| --- | --- |
| Candidate collection over 20 radios took about 12 s a round, longer than the candidate stream's 7.5 s refresh: the controller admitted one Unassociated STA Link Metrics Query at a time for the whole mesh | 0222: one query in flight per agent, em_cli runs four agents at once (`/api/v1/coordination`); the optimizer's parallel collection defers a query the controller did not admit to a second pass instead of failing the agent (28 September). 0229: a reply completes its own radio's query, and a query times out on its own (the type's timer never went idle with four agents queried continuously, so every check cancelled every query and radios stayed "not ready"). A round over 24 queries (500 candidates, 100 clients) now takes 3.2 to 3.8 s with four agents and 7.0 to 7.7 s one at a time (12 s before); the room settles 2 minutes after the topology, all 20 clients checked |
| The RDK agents never answered the Channel Preference Query. The agent built its command on the AL node in `ap_cap_report`, a state only the radios reach since 0180; the query was dropped without a word, the controller re-sent it about once a second, its radios stayed in `channel_query_pending` (em_config's fourth step) and refused every candidate query as not ready | 0223: the AL node answers every query at once. Completing em_config then needs: 0224, a Channel Selection Request without preferences keeps the channel; 0225, no default channel preferences in the controller (its presets, op classes 83/6, 128/42 and 135/7, would move every agent to those channels and widths, off the rooms' 20 MHz 6, 36 and 37); 0226, the policy ACK configures every radio of the agent (the others waited in `set_policy_pending`). In the lab all 20 radios reach topology publish, the channels stay put and the extenders' backhaul stays up through the gateway's full policy (28 September) |
| em_config's timeouts read the type's time (the longest any agent's em_config took since the type was last idle): with every agent now running all em_config steps, one slow agent got every other agent reaching topology sync cancelled and renewed | 0230 (28 September): each agent's em_config times out from its own start and cancels only itself. After a controller restart, topology sync takes 1 to 16 s for most agents; an agent caught between the controller's renews and its own restart can still loop through renews (118 and 258 s), as before these patches (the 04:13 bring-up needed two agent restarts); `lab-bringup.sh up` restarts the agents again and the topology completes |
| After a controller restart the gateway's agent sometimes stayed with a radio in WSC (Agent-1 with 4 of 10 BSSes). The renewed radios' M2s arrive in one second; the agent pushes one OneWifi subdoc at a time, a queued radio asks again with M1, and the controller ignored an M1 for a radio in `wsc_m2_sent` | 0228 (28 September): the controller answers it. `lab-bringup.sh up` restarts the agents up to three times and names the short nodes (28 September) |
| `received-discovery-recovery` (`native_owner_mismatch`): a Topology Response claim for a station another BSS holds is taken only if younger, but the existing claim's age stops at its AP's last report. A station back on A after a short stint on B kept B's small frozen age (live: 316 s against 57 s) | 0227 (28 September): the controller keeps when each claim's association began (boot clock) and compares those |
| "Geometry native outage": all four extenders without a parent at once in `backhaul-branch-formation`. The room's own geometry: extender_1 and extender_2 hear extender_3 and extender_4 better than the gateway (20 against 13 dB on 5 GHz), so the native tree may start with them hanging off extender_3 and extender_4; the midpoint then inverts the whole tree, and re-forming took 62 s of the room's 60 s | (28 September): the branch convergence has 150 s and the report keeps the initial native parents. The 27 Sep failure was another room (`backhaul-parent-handover`: extender_3 never moved to extender_2, native stickiness) |
| The gateway container reached its 1 GiB memory limit: the evidence journal (271 MB, in `/run`) and the daemons (450 to 700 MB). Memory-cgroup thrash: VM load past 100, every `lxc exec` and `wpa_cli` timed out, all four Wi-Fi extenders lost their backhaul | (28 September): 96 MB of evidence journal on the gateway, 256 MB on the extenders |

The suite with all of them installed in place (rev120
`test-results/emosa-pods-wired-0230-20260928T162152Z`): eight steps of nine,
the catalog 23 of 24, geometry all three rooms (0 of 3 on the images before).
`fifty-client-counter-roam`, `home-a-flash-crowd`, `home-a-fast-transit` and
`large-room-extender-evacuation` passed. The one miss,
`received-discovery-recovery`, was the test's own audit: its query of the
client's state to the VM's LXD API took longer than 5 s (meta-cmf of 28 September:
20 s); the room's checks had passed.

The images carrying all of them (28 Sep 18:07): controller
`X86EMLTRBPIBB_rdk-next_20260928161525`, extender
`X86EMLTRBPIAP_rdk-next_20260928162627`, both from meta-cmf of 28 September, no
`*.pre-*` file left in any node; after the redeploy the topology completed
at the first attempt and the room settled 4 minutes later. Their suite
(`test-results/emosa-pods-wired-images-0230-20260928T182827Z`): seven steps
of nine, the catalog 22 of 24, `received-discovery-recovery` passed. The
misses are the known convergence windows: `large-room-extender-evacuation`
converged at 89.5 s and was confirmed at 90.5 s of its 90 s load window,
`fifty-client-counter-roam` was not converged at 90 s (candidates complete,
the steering policy still settling); geometry passed all three rooms'
checks, the branch room from an inverted start (extender_1 under
extender_3), and missed the 60 s default restore with 19 of 20 clients
measured. Rerun alone (`test-results/rerun-20260928T200635Z`),
`large-room-extender-evacuation` loaded in 30 s and passed;
`fifty-client-counter-roam` missed again. With 50 clients the candidate
rounds slow from 3.5 s (idle) to 6.6 s at the median and up to 10.6 s: queries
to radios busy steering are refused as not ready (deferred), and some time
out in em_cli (HTTP 504 after 8 s) having waited 3.4 to 4.7 s for its native
lock behind the room's topology and client reads.

Two more fixes for that room. The waiting was the queries' own polls: each
read the whole station tree (about 70 ms with 50 clients) under the lock,
four agents at once. Polls now share a read no older than 100 ms
(unified-wifi-mesh 0231, meta-cmf of 28 September): rounds 3.5 s under the load,
no timeout. The room then passed 2 of 3 alone; the miss was coverage
flapping around 210 of 250: a client counts as measured only while all its
candidates are younger than 15 s, and refreshed at 7.5 s a candidate whose
query met a busy radio could expire before the next round. The room now
refreshes at 5 s (meta-cmf of 28 September): `fifty-client-counter-roam` passed 3
of 3 alone, loaded in 81, 84 and 81 s of its 90 s (first convergence 75 to
79 s). Images with 0231: controller `X86EMLTRBPIBB_rdk-next_20260928203255`,
extender `X86EMLTRBPIAP_rdk-next_20260928204402`.

EMOSA in C (emosa-lab of 28 September): the statistics decoder reads the pod's
band-steering report (each watched station's last probe request), and the
Unassociated STA Link Metrics answer measures a station from it, as the
Python agent does; checked by new vectors. The C runtime still has no
telemetry scope, so live it refuses every station.

Not done, and why: the pods' backhaul in the geometry rooms. In those rooms
the pods stand where the geometry gives them no usable path to the gateway
(-1 dB), and EMOSA pins its upstream to the gateway's BSS (Backhaul Steering
refused, spec §2.4): modelled, the pods would just lose their backhaul.
It needs EMOSA to take an extender as its parent first. The TP-Link RE653BE
on `br-emosa` needs the device.

### The qualification closed (29 Sep)

The images from meta-cmf of 28 September (0231; controller
`X86EMLTRBPIBB_rdk-next_20260928203255`, extender
`X86EMLTRBPIAP_rdk-next_20260928204402`) went into `rdk-emosa` with the lab's
new `gen/lab-redeploy.sh`, every node keeping its identity. After the
redeploy the wired extender fell into a renew loop (its em_config never
reached topology sync within its time limit, so the controller renewed its
radios about every minute; the room's health stayed false). Restarting its
agent alone did not end it; `gen/lab-bringup.sh up` did. The cause, the
agent's one-subdoc-at-a-time M2 path (0107) against the controller's answer
to a repeated M1 (0228), is open in the RDK lab (alignment plan phase 1).

The pods-wired suite on those images
(`test-results/emosa-pods-wired-239f9f6-20260929T005904Z`): eight steps of
nine, and the catalog passed all 24 rooms for the first time, with both pods
on Wi-Fi backhaul and the wired extender. Geometry failed in the suite: in
`backhaul-branch-formation` the native branch formed, but the controller's
model had not caught up within the 150 s window (extender_3's edge missing,
two of ten clients not active). Run again alone (`geometry-rerun-023705`),
all three geometry rooms passed their checks; the final restore to the
default room missed its 60 s window, as on 28 Sep. With that, steps 3 to 5
below are done and the qualification against the 24 rooms is closed. The
open items are the controller's catch-up after a geometry re-parenting and
the default-restore window.

### Order

1. Two pods through the GTP path, backhaul held fixed (done).
2. Native baseline: the room suite on `rdk-emosa` with EMOSA idle (done).
3. Pods in the room model (done).
4. Measurements: telemetry in the RDK lab; serving metrics; candidates from probe
   requests (done).
5. The pod-variant suite over the 24 rooms, compared with the baseline (done:
   the qualification closed on 29 Sep, above).
6. The pods on Wi-Fi backhaul and its suite (done).
7. A wired EasyMesh extender next to the pods and its suite (done); the
   images carrying every change (done).

## 3. EMOSA as an option of the RDK lab (29 September)

### From scratch: `rdk-emosa-0929`

`rdk-emosa` was stopped and kept as a snapshot (`before-replacement-20260929`).
Its successor was built on rev120 in one command, meta-cmf
`EASYMESH_EMOSA=1 gen/vm/lxd/build.sh build`, from the 0045 images: the lab was
accepted, then `lab.sh up` brought EMOSA, the fleet, the GTP, both pods,
their telemetry, their Wi-Fi backhaul and the pod rooms, with no step by hand.
The first build stopped at the wired LAN port on a race between the port's
timer and its direct attach (meta-cmf of 29 September); the second ran through.

The RDK controller's `SteerWiFiBackhaul()` turned out to be an upstream stub
that answered Success and sent nothing; unified-wifi-mesh 0232 (meta-cmf of
29 September) hands the request to the lab's native backhaul steering, which sends
the Backhaul Steering Request. With it, redeployed with `gen/lab-redeploy.sh`,
the controller moved the pods: pod-1 onto extender_1's backhaul BSS in 6 s and
back to the gateway in 6 s, pod-2 onto the wired extender's in 16 s, and the
controller got EMOSA's Backhaul Steering Response. Two findings on the way: a
Wi-Fi extender starts its backhaul BSS only for a child, so the move first
starts it (the room does this for its geometry rooms, meta-cmf of 29 September); and
the move changes the pod's reported topology, over which the agent renews its
session, so the answer to a move under way is now the agent's, shared by its
sessions (emosa-lab of 29 September).

### The full room suite with the pods

On `rdk-emosa-0929` the lab's room suite (meta-cmf
`gen/tests/run-easymesh-suite.sh rooms`) ran on the pods' room set: the guest
audit, default readiness, the RF hover, access and property rooms, the switch
through all 31 worlds and the return to the default passed; the catalog passed
25 of 27 rooms and the geometry rooms stopped at their first. Both stages were
run again whole, with the browser on rev150 (on rev120 the lab VM and the
software-rendered browser share 12 cores): the catalog passed all 27 rooms and
the geometry rooms all four, the pods moved onto extender_1, extender_2 and the
wired extender.

- The catalog's two failures were the browser's load, not the pods: one band
  move in `band-ap-counter-roam` was requested and never seen, and the fifty
  clients of `fifty-client-counter-roam` converged at 84 s of the 90 s allowed.
  From rev150 both passed; the fifty clients converged in 21 s.
- The first geometry room is the pods' own case. Every re-parenting of a native
  extender drops the pod under it: OneWifi takes an extender's BSSs down with
  its uplink, the pod's OVSDB connection to EMOSA closes, and EMOSA onboards the
  pod again once the extender is back (a new connection is a new source). From
  the default star the room's pods onboarded three times while the native
  branches formed, the last 5 s before the room's 90 s ran out, and each time
  the controller's model lacked the pod for a few seconds. A loaded geometry
  room with pods now gets the 150 s branch window (meta-cmf of 29 September).

Open for a later phase: EMOSA could keep a pod's session across a short loss of
its OVSDB connection when the pod comes back the same (its capabilities
unchanged), instead of onboarding again.

With this, `rdk-emosa-0929` replaces `rdk-emosa`, which stays stopped with its
snapshot until it is deleted by hand.

### Python and C interchangeable: the room suite on either agent

The same suite on `rdk-emosa-0929`, with the pods' agents swapped
(`lab.sh agent POD python|c`; `lab.sh implementation python|c` for every pod; a
lab built with meta-cmf `EASYMESH_EMOSA_AGENT=c` starts them on C). Each agent's
status names its implementation (`c-lab-prototype` for C); the processes were
checked before each run.

| Stage | Both pods on Python (phase 3) | Both pods on C | One of each |
| --- | --- | --- | --- |
| guest audit, default readiness | passed | passed | passed |
| catalog (27 rooms) | 25 on rev120, then 27 of 27 from rev150 | 26 on rev120, the 27th from rev150 | 24 on rev120, the other 3 from rev150 |
| geometry (4 rooms) | stopped at the first on rev120, then 4 of 4 from rev150 | 4 of 4 on rev120 | 4 of 4 on rev120 |
| RF hover, access, properties | passed | passed | passed |
| world switch (31 worlds), restore default | passed | passed | passed |

- **Both pods on C** (emosa-lab of 29 September, run
  `suite-rdk-emosa-0929-c-20260929T184235Z`): 8 of 9 stages on rev120. The one
  catalog room that failed, `fifty-client-counter-roam`, is the one that failed
  with Python on rev120: its initial convergence ran out of the 90 s (the
  roster came at 51 s) while the software-rendered browser shared rev120's 12
  cores with the lab VM. From rev150 it passed, converged in 13.6 s.
- The geometry rooms passed on rev120 with the pods on C. The Python runs on
  rev120 predate the 150 s branch window for loaded rooms with pods (meta-cmf of
  29 September); with it, Python passed them from rev150. The two are not
  compared on rev120 here.
- **One of each** (emosa-lab of 29 September, run
  `suite-rdk-emosa-0929-mixed-20260929T205113Z`): the pod in container `pod-1`
  (the controller's Pod-2) on C, the one in `pod-2` on Python, the same two
  processes from the start to the end of the run. 8 of 9 stages on rev120; the
  catalog's three failures were timing on rev120, none on a pod:
  `fifty-client-counter-roam` again (initial convergence past 90 s),
  `band-ap-counter-roam` (as with Python on rev120: the checkpoints did not
  converge in time), and `home-a-extender-loss-recovery`, where a native client
  of the native extender_4 left that extender's lost fronthaul 6 s after the
  loss (the check allows 5 s). From rev150 all three passed.
- Which implementation each pod ran is recorded in each run's directory on
  rev120 (`agents-start.txt`, and `agents-end.txt` for the mixed run: each
  agent's AL MAC, the implementation its status names, its process); the
  agents' unit journals show no start or stop during either run.

With either agent, and with one of each, the suite's results are the ones the
pods gave with Python: every stage passed, the catalog's 27 rooms and the four
geometry rooms among them; the rooms that failed on rev120 failed on timing
there, on no pod, and passed from rev150.

### The adapter in C with the features at the bar (3 October)

The suite again on `rdk-emosa-1002` (built 2 October), the whole adapter in C
(`lab.sh up c`: its fleet, GTP and agents, no Python in the adapter's
containers), emosa-lab after plan 8.4 (0bbcdc4; no source of the adapter changed
up to 1b5f611), the browser on rev120. Run
`suite-rdk-emosa-1002-c-adapter-20261003T212036Z`, with the room service and the
topology UI through the VM's proxies on rev120's address
(`EASYMESH_HOST_ADDRESS=192.168.2.120`, `EASYMESH_SSH_HOST=rev120`, as the runs
of 1 October; a first run left at 127.0.0.1 failed every host-side stage in
seconds, while its world switch, which runs inside the VM, passed all worlds).

| Stage | The adapter in C |
| --- | --- |
| guest audit, default readiness | passed |
| catalog (27 rooms) | 26 on rev120; the 27th passed in 4 of 4 runs after |
| geometry (4 rooms) | 4 of 4 |
| RF access, properties | passed |
| RF hover | failed once (the native AP metrics 5.2 s old at its second read), then passed |
| world switch (34 worlds, then the default), restore default | passed (and in the first run) |

- The catalog room that failed, `home-a-wired-extender-loss-recovery`, is a
  timing edge on no pod: its three clients leave the wired extender's lost
  fronthaul (at 20 s) for the native extender_4, and the check wants no sample
  at or after 25 s to show one on it, sampling once a second. They were last
  seen on it at 23 to 24 s in every earlier run (29 September to 1 October,
  Python, C and mixed), and at 25 s in the suite's run and in a rerun with the
  adapter in C. To tell whether the implementation mattered, the same VM ran the
  room twice with the Python adapter (installed for it, last seen at 24 s, both
  passed) and four more times with the adapter in C installed again (last seen
  at 23 or 24 s, all passed). A capture of the pods' 1905 traffic (2 minutes)
  showed the controller's queries answered as the specification says: every
  Topology Query from the controller, AP metrics at the reporting interval, the
  other agents' Topology and Link Metric Queries to a new neighbor not answered,
  as with the reference.
- The VM runs the adapter in C after these runs (emosa-lab 1b5f611).

### EMOSA in the gateway container: its footprint (3 October, plans 5.4 and 8.6)

EMOSA's fleet and both pods' agents ran inside the RDK controller's own container
(`bpibroadband`: 2 CPUs, 1 GiB, no swap) on `rdk-emosa-1002`, as on a gateway that
carries the adapter: `deploy/rdk-lab/vm/gateway.sh on` installed the package the image's
recipe builds (emosa-lab d11fc1d; the image of 3 October differs from the running one of
2 October only by that package), took over the adapter container's registry,
configurations and state, and forwarded the front and agent ports to the gateway; the
broker stayed in the adapter container. The pods came back through the gateway's front
port 47 s after its fleet started, with the same AL MACs and ports; RDK's controller
onboarded both agents again (their M2s written and applied) through the agents' trunk, a
veth pair into `brlan0` (`EMOSA_BRIDGE`; a macvlan on the bridge device itself would not
reach the controller, found here and fixed in the package's defaults). The agents logged
through RDK's logger into `/rdklogs/logs/EMOSA_<pod>.txt` and `EMOSAFleetLog.txt`.
`gateway.sh off` moved everything back (provisioning in the adapter container 58 s later).

The same 30 minutes in both arrangements, sampled every 30 s (`gateway.sh measure`), with
readiness and the quick requalification's five rooms run from rev120 meanwhile:

| | EMOSA in its own container | EMOSA in the gateway |
| --- | --- | --- |
| gateway memory (cgroup), median / max | 487 / 498 MiB | 506 / 592 MiB |
| gateway CPU, of one core | 75.6 % | 80.0 % |
| an agent: PSS / RSS / CPU | 7.6 / 13.5 MiB / 2.4 % (x86-64) | 5.0 / 9.7 MiB / 2.8 % (the image: 32-bit) |
| the fleet: PSS / CPU | 1.1 MiB / 0.0 % | 0.7 MiB / 0.0 % |
| RDK's controller (`onewifi_em_ctrl`): PSS median / max, CPU | 35.8 / 48.2 MiB, 35.4 % | 33.6 / 55.8 MiB, 34.6 % |
| the rooms | 4 of 5 | 4 of 5 |

- **Memory fits.** EMOSA adds about 10.7 MiB of its own (two agents and the fleet) and
  about 19 MiB to the gateway's median with page cache; the gateway peaked at 592 MiB of
  its 1024 MiB during the rooms (its controller at 55.8 MiB). An agent costs about 5 MiB
  in the image.
- **CPU is what limits the number of pods.** An idle agent takes 2.4 to 2.8 % of a core.
  A profile of one (`perf`, 60 s) puts 66 % of its time in `malloc`/`free` and 17 % in
  cJSON: the agent parses its whole journal again several times a second, all of its 67
  operations (172 KB of JSON) on each 0.5 s refresh for the engine's reconcile and again
  for the uplink's tick, the latest operation on every loop for the status, the secret
  files on each uplink tick; a regular expression is compiled for each MAC checked. The
  reference does the same. The cost grows with the journal, and the journal keeps every
  operation; the reconcile's search for a newer operation is quadratic in it. Remedy
  (open): the journal's operations parsed once and kept, the search linear, the status
  checked once a second, prepared statements kept, and a bound on what the journal
  retains (the specification has none yet).
- The room that failed in both runs, `traffic-quieter-ap`, failed on the browser's
  screenshot timing (`page.screenshot: Timeout 45000ms exceeded`) on rev120, at load 11 to
  14 with the lab VM and the browser on the one host. A first run of the same rooms (no
  samples: the measurement stopped on an agent without `--version`) passed 3 of 5:
  `traffic-quieter-ap` on the screenshot again, and `band-upgrade-24-5` on a check it
  passed in both measured runs.

Evidence: [footprint-1003](footprint-1003/) (each arrangement's samples, summary, run and
rooms).

### The journal bounded: the gateway again (3 October)

The open remedy above, done in both implementations (spec §6, vectors
`journal-retention.json`): the journal keeps every active operation, each pod's latest
operation in a reconciled state and the 16 most recent, pruning the rest with their WSC
receipts as operations are added; the operations are parsed once and kept, the
reconcile's search is linear, the secret fingerprints are kept, and MACs are checked
without regular expressions (emosa-lab f4011e8). The image recipe's package at that
commit (built on rev140 with the image, the Apache-2.0 license file checked) ran in the
gateway's container the same way, with the same 30 minutes, readiness and five rooms:

| | before (d11fc1d) | after (f4011e8) |
| --- | --- | --- |
| an agent: CPU of one core | 2.84 % | 0.46 % |
| an agent: PSS / RSS | 4.9 / 9.4 MiB | 6.5 / 11.0 MiB |
| the fleet: PSS | 0.7 MiB | 0.7 MiB |
| gateway CPU, of one core | 80.0 % | 75.5 % |
| gateway memory (cgroup), median / max | 506 / 592 MiB | 526 / 538 MiB |
| RDK's controller: CPU, PSS median / max | 34.6 %, 33.6 / 55.8 MiB | 34.7 %, 38.2 / 50.4 MiB |
| the rooms | 4 of 5 | 5 of 5 |

- **An idle agent's CPU is a sixth of what it was**, and no longer grows with the
  journal: each journal held 16 operations after the pods' onboarding in the gateway
  (67 before), with 16 WSC receipts.
- **Memory:** the agents in the gateway started on the 67-operation journals they took
  over, parsed once before the first new operation pruned them, which likely explains
  their higher PSS there (not measured further). Restarted afterwards on the pruned
  journals in the adapter container, the agents used 7.2 MiB PSS, against 7.6 MiB before
  (x86-64).
- **Seen: the reporting policy's writes.** Each agent writes its reporting policy's record
  twice per periodic AP metrics report (the period reserved before sending, then its
  outcome, so a crash overcounts one report and never replays a burst:
  `src/emosa/wire/reporting_policy.py`, `c/src/reporting.c`), each a synchronous SQLite
  commit.
  With RDK's controller asking every 5 s that is about 99 KB a minute of WAL per agent
  (24 page writes), some 140 MB a day per pod on the gateway's storage; SQLite's
  checkpoint keeps the file bounded. The reference does the same. Open: whether a
  gateway on flash wants fewer durable writes there.
- `gateway.sh on` had ignored the package it was given while an earlier run's package
  was installed (fixed: emosa-lab be4beea).

Evidence: [footprint-1003/gateway-2](footprint-1003/gateway-2/) (samples, summary, run and
rooms).

### The reporting policy written only when received (4 October)

The writes seen above, decided away (spec §3.8, emosa-lab f2e5264): the reporting policy's
record is written when a policy is received, before its Ack, and only then; the schedule of
unsolicited reports and its accounting are the session's. On `rdk-emosa-1002` with the
adapter kit at 2a2d009 (the pin of meta-cmf-bananapi-vcpe 57d8336), after both pods'
onboarding (two policy receipts each), over 5 minutes: each agent sent 60 periodic AP
Metrics Responses, one every 5 s as before, and wrote its reporting policy's file not once
(its WAL last written 336 s before the end, at the receipts). Before, with the kit at
d5e577a, each wrote it every 5 s (the WAL last written 4 s before a sample; about 99 KB a
minute). The gateway image built with this package (rev140,
`controller-emosa-20261004T154344Z`) has the same packages as the previous EMOSA image.


### EMOSA from the gateway image, its state on /nvram (4 October)

The step after `gateway.sh` copying a package into the default image: the gateway image
built with EMOSA (meta-cmf-bananapi-vcpe 827eb0f, `EMOSA_ADAPTER = "1"`, emosa-lab 62740b9;
`X86EMLTRBPIBB_rdk-next_20261004180643`, rev140) deployed as `rdk-emosa-1002`'s gateway with
the lab's own `gen/lab-redeploy.sh`, and EMOSA run from it. The package keeps EMOSA's
configuration and state where a gateway keeps what an image upgrade must not lose, `/nvram`
(`/nvram/emosa/fleet-config.json`, `agents/`, `state/`), and the agents' status, rewritten
once a second, in RAM (`/run/emosa`, linked from the state directories; spec §6).

- **Switched on from the image:** `gateway.sh on` (no package given) took the registry and
  both pods' state from the adapter container into `/nvram/emosa` and wrote the fleet
  configuration there; both pods were handed to the gateway's fleet and provisioning. In a
  minute of steady state EMOSA wrote nothing to `/nvram`, only the two status files in
  `/run`.
- **An image upgrade kept it:** `gen/lab-redeploy.sh` with a newer build of the same image
  replaced the gateway container. `/nvram/emosa` came through whole, and the new gateway
  started EMOSA's fleet by itself from it at boot. Once `gateway.sh on` had put back the
  lab's port forwarding (a redeployed container has none), both pods were provisioning
  again within 39 s, with the same AL MACs, ports and interfaces, each handed over once
  more, their journals intact (16 operations each).
- **Found and fixed:** the agents' link helper read `/etc/emosa/POD.json` whatever the
  package's places, so with the configuration on `/nvram` the agents never started (emosa-lab
  62740b9: it reads `EMOSA_AGENT_CONFIG_DIR`).
- **The rooms:** readiness and the quick requalification's five rooms passed, five of five,
  with EMOSA in the gateway from the image, sampled for 30 minutes as before
  ([footprint-1004/gateway-image](footprint-1004/gateway-image/)): the gateway at 506 MiB
  median and 557 MiB peak of its 1 GiB and 70.7 % of a core, an agent 4.7 MiB PSS and 0.54 %
  of a core, the fleet 0.7 MiB, RDK's controller 33 % of a core.
- **Seen on the way:**
  - After an upgrade the agents start only when their pods come back through the front
    port (their units were enabled in the old root file system); the fleet could start its
    registry's agents itself.
  - `gen/lab-redeploy.sh` left the room service unable to start: its recovery journal
    listed 17 pool clients an earlier interactive session had paused, and the redeployed
    lab's inventory no longer matched it, so the room service failed closed as its guard
    requires. By the owner's decision the journal was retired, kept unchanged in the lab's
    evidence (`room-recovery-retired-20261004`), and `gen/lab-bringup.sh room` started the
    room service.
  - RDK's controller crashed once (SIGSEGV, core dumped) while the pods' agents
    re-onboarded, processing an extender's Topology Response; systemd restarted it, and the
    restarted controller had forgotten every agent. The same step repeated did not crash it.
  - After a controller restart the pods' agents stay registered only in their own view: the
    controller keeps sending them Link Metric and other queries, so spec §2.5's silence rule
    never fires, but no Topology Query, and the pods are not in its topology.
    `gen/lab-bringup.sh` restarts RDK's own agents for this reason, not EMOSA's, and its
    topology check passes without the pods. Restarting the two agents brought the pods back
    within 30 s. A renewal rule for this (no Topology Query while `provisioning`) is open.

EMOSA stayed in the gateway (`gateway.sh status`); `gateway.sh off` moved it back into the
adapter container.

### The two fixes in the gateway image, and EMOSA in the gateway as a build option (4 October)

Both of the previous section's open points are fixed, and EMOSA in the gateway is now
something a lab build does by itself. The gateway image was built with the fixes
(meta-cmf-bananapi-vcpe 49e111f, emosa-lab ee34885; `X86EMLTRBPIBB_rdk-next_20261004213513`,
rev140, 10 minutes from the shared state) and deployed as `rdk-emosa-1002`'s gateway with
`gen/lab-redeploy.sh`, EMOSA running in the gateway
([footprint-1004/gateway-option](footprint-1004/gateway-option/)).

- **After an image upgrade the fleet starts its agents (spec §4):** the new gateway started
  EMOSA's fleet at boot from `/nvram`, and the fleet started both agents 6 s later, before
  any pod could reach them: the redeployed gateway had no port forwarding yet
  ([upgrade.txt](footprint-1004/gateway-option/upgrade.txt)). Once `gateway.sh on` had put
  the forwarding back, both pods were provisioning within 93 s; they had been cut off
  through the 26-minute redeploy.
- **The build option:** `gateway.sh off`, then the build's own step,
  `EASYMESH_EMOSA_IN=gateway gen/vm/lxd/build.sh emosa` (meta-cmf fb84eae, emosa-lab
  6d6895c): emosa-lab's `lab.sh up c gateway` set the option up in its adapter container
  as usual, then moved the registry and the agents' state into the gateway with the image's
  own package. The gateway's fleet started both agents from its registry, and they were
  provisioning 12 s later. The rooms settled again with EMOSA in the gateway, every pod on
  its Wi-Fi backhaul. The fleet and both agents carry `topology_query_window` 120
  ([window.txt](footprint-1004/gateway-option/window.txt)); the VM records
  `user.easymesh.emosa-in=gateway`.
- **A controller that forgot the agents (spec §2.5):** RDK's controller restarted
  (`systemctl restart em_ctrl`); the pods left its topology within 6 s and stayed out, while
  the controller logged 11 AP-Autoconfiguration Renews, none of which brought them back.
  120 s after their last Topology Query both agents onboarded again on their own
  ("provisioned, no Topology Query in topology_query_window"), and both pods were back in
  the controller's topology 119 s after its restart
  ([controller-restart.txt](footprint-1004/gateway-option/controller-restart.txt),
  [agents-renewal.txt](footprint-1004/gateway-option/agents-renewal.txt)). On 4 October the
  same situation needed a manual restart of EMOSA's agents.
- **The rooms:** readiness passed, and four of the quick requalification's five rooms; the
  fifth, `traffic-quieter-ap`, stopped at the browser harness's screenshot timeout (45 s,
  eight checks verified, rev120's load average near 12 from its other lab VM) and passed
  when rerun alone ([rooms.txt](footprint-1004/gateway-option/rooms.txt)). Sampled for
  30 minutes as before: the gateway at 522 MiB median and 543 MiB peak of its 1 GiB and
  71.9 % of a core, an agent 4.6 MiB PSS and 0.43 % of a core, the fleet 0.7 MiB, RDK's
  controller 34 % of a core.
- **Found and fixed on the way (finding 16):** the build option's first run stopped at the
  uplink check. After the long redeploy the pods' uplinks were held; the C adapter releases
  a hold with `forget`, which archived each pod's state with its last status (the switch
  timed out), and the lab's tools read every `*/status.json`, the archives too: the check
  never passed and the provisioned counts counted the archives as agents. The tools count
  current agents only (emosa-lab 6d6895c), and `forget` archives a pod's state without its
  agent's status (emosa-lab 68515a2, spec §4): in a gateway's run directory the archive's
  link would show the next agent's status.
- **Seen on the way:** `gen/lab-redeploy.sh` ends with the room service failing to start
  while EMOSA runs in the gateway ("pod-1: no operating AP radio"): the pods cannot reach
  their agents until `gateway.sh on` puts the forwarding back, so the room service rightly
  refuses; `lab.sh up c gateway` (or `gateway.sh on` and `lab.sh rooms pods`) then starts
  it. EMOSA's logs in `/rdklogs/logs` were emptied by RDK's log handling during the redeploy,
  so the fleet's start lines are not in the record; the units' start times are.
- **Not covered:** a fresh `build.sh build` with `EASYMESH_EMOSA_IN=gateway` (this was the
  option's step on the running lab, `build.sh emosa`); the build's image check was shown
  on the EMOSA image (found, 16 s) and on an image without EMOSA (refused).

EMOSA stays in the gateway.

### A fresh lab built with EMOSA in the gateway: rdk-1004 (rev140, 5 October)

The build option's first fresh build, replacing `rdk-1002b` on rev140 (the owner's decision):
`EASYMESH_EMOSA=1 EASYMESH_EMOSA_IN=gateway gen/vm/lxd/build.sh build` from fresh clones
(meta-cmf-bananapi-vcpe d70f1c3, emosa-lab 68515a2), the gateway image built at the same pin
(`X86EMLTRBPIBB_rdk-next_20261005010026`, rev140, 8 minutes from the shared state)
([rdk-1004](footprint-1004/rdk-1004/)).

- **The build:** passed, 92 minutes: the lab 56 (its acceptance at 02:04), the EMOSA option
  36 ([build.txt](footprint-1004/rdk-1004/build.txt)). The build checked the controller image
  for EMOSA before it started. The option ran in the adapter container (both uplinks applied
  at the first switch), then the gateway's own EMOSA took the registry and the state over:
  its fleet started both agents from the registry, provisioning 13 s later, and the rooms
  settled with every pod on its Wi-Fi backhaul. The VM records `user.easymesh.emosa-in=gateway`
  and emosa-lab 68515a2.
- **The rooms:** readiness passed, and four of the five quick rooms. `traffic-quieter-ap`
  stopped three times at the browser harness's screenshot timeout (45 s, six checks
  verified, never played): in the run, rerun alone with the browser on rev140, and rerun
  from rev120's browser, an idle host ([rooms.txt](footprint-1004/rdk-1004/rooms.txt)). The
  same room passed on `rdk-emosa-1002` when rerun, whose lab code is older (meta-cmf 373eefe,
  medium 61f646d, optimizer 6468492); `rdk-1004` runs the medium and optimizer pinned on
  4 October (d74a103, bd7b18e), on another host (rev140). The room service logged no error.
  Resolved the same day (next section): the harness rendered in software and starved the
  host.
- **The footprint**, sampled for 30 minutes as before: the gateway at 471 MiB median and
  489 MiB peak of its 1 GiB and 85.1 % of a core, an agent 4.4 MiB PSS and 0.83 % of a core,
  the fleet 0.7 MiB, RDK's controller 33 % of a core
  ([summary.json](footprint-1004/rdk-1004/summary.json)).
- **On the way:** rev140 has no `uv`, which the adapter kit's build needs; the clone's
  `.cache/opensync-lab-artifacts/uv` took rev120's (0.11.2).

### rdk-1004 as the target configuration: every room, a VM restart, hours of rooms (5 October)

The owner's direction: rdk-1004 (RDK with EMOSA in the gateway) is the target configuration
and must be stable; test more rooms and fix what fails.

- **The room catalog, all 27 rooms with the pods, passed**, the browser on rev140's GPU. The
  `traffic-quieter-ap` failures were the harness's: it rendered both pages in software
  (SwiftShader), Chromium took up to ten of rev140's sixteen threads (load near 20) and the
  topology page's screenshot waited past 45 s. On the GPU (Vulkan) Chromium stayed under one
  core and the room passed. The harnesses now take the host's GPU when it has a usable one
  (easymesh-optimizer 524357d, meta-cmf 36ce8d5).
- **The four geometry rooms passed** on a freshly restarted controller (branch formation,
  parent handover, isolation recovery, wired parent).
- **A VM restart did not bring the lab back, now it does** (verified with `build.sh restart`,
  45 minutes, no manual step): the pods' redirector address was added once at boot, before the
  lab's runtime created its bridge, so the gateway, whose EMOSA forwards listen on that address,
  did not start at all; `build.sh start` then waited for a room that refuses to start without
  the pods; and the steps that bring EMOSA back missed its containers, counted stopped pods as
  running and made the medium before the pods' stations existed (emosa-lab c8715bf, meta-cmf
  7ee964a). On the way: `fleet_cli` printed nothing in the gateway (a POSIX sh ends at a missing
  file sourced with "."), `rooms native` restarted the controller without the lab's bring-up,
  and `rooms pods` returned before the restarted controller had the pods again.
- **RDK's native backhaul steering stopped measuring candidates.** After the catalog,
  `backhaul-parent-handover` failed in both rounds of a three-hour run and on rdk-emosa-1002
  too: the controller's native backhaul module queried no candidate at all (every observation
  `candidate=unknown`), though the extenders' serving samples were fresh and weak enough.
  Concluded here: not EMOSA's placement, and a matter of hours. Both wrong: it was EMOSA's
  agents in the gateway, from the first room on (next section).
- **A timing edge:** `home-a-wired-extender-loss-recovery` failed once in four runs: one sample
  5 s into the wired extender's 40 s outage still showed a client on it.

### The native backhaul failure was EMOSA's: its agents' AL MACs on the gateway's interfaces (5 October)

- **The cause.** RDK's controller takes an agent whose AL MAC is the MAC of one of its own
  host's interfaces for its co-located agent. With EMOSA in the gateway, each agent's macvlan
  (`em1`, `em2` on the trunk `emlan`) carried its AL MAC in the gateway's namespace, and
  `Device.WiFi.DataElements.Network.ColocatedAgentID` was a pod's agent (`02:72:f9:7f:07:85`;
  the gateway's own agent is `00:60:2f:da:68:e4`). The native backhaul module roots its
  topology at the co-located agent. unified-wifi-mesh 0235's state lines showed no extender
  with a root path and no station queried in all 43 rounds of a diagnostic run, whose first
  room, on a restarted controller, failed as well
  ([state-lines.txt](backhaul-1004/state-lines.txt)). The rooms that passed after a restart
  most likely ran before the pods' agents registered again (not checked then).
- **The fix** (emosa-lab 0a4bbcb; spec 2.1, finding 17): the agents' interfaces in a network
  namespace of their own, `EMOSA_NETNS`. The link helper creates it and moves the trunk's end
  of its veth pair there; the bridge port stays in `brlan0`. Both agents open their 1905
  socket there and return to their own namespace. The gateway's package sets `EMOSA_NETNS=emosa`
  (meta-cmf ceebf4c). Box scenario `agent-netns`; CI passed, after 345387c
  (the analyzer's false reading under `_GNU_SOURCE`).
- **Verified on rdk-1004** with the gateway image `X86EMLTRBPIBB_rdk-next_20261005172041`
  (emosa-lab 0a4bbcb, unified-wifi-mesh 0235), redeployed in place: the co-located agent is
  the gateway's again and no interface of its namespace has an agent's AL MAC. Then
  `backhaul-parent-handover`, the room catalog (27 of 27) and the handover again passed, in
  that order. The state lines show the gateway and the four Wi-Fi extenders with root paths
  (43 of 45 rounds), 10 to 12 stations queried per round, and native moves verified. A long run
  after it, two rounds of the catalog and the four geometry rooms, passed whole.
- **Then the pods (unified-wifi-mesh 0236).** In that run the rooms moved the pods (through
  the controller's `SteerWiFiBackhaul`), and the controller marked both pods uncertain for the
  rest of it (61 of 102 rounds): it had never learned their backhaul stations. em_ctrl sends a
  Backhaul STA Capability Query only after a Backhaul STA Radio Capabilities TLV arrives in a
  Topology Response, as RDK's agents send it; EMOSA reports its station the R1 way, in Device
  Information, and answers the query when asked. Without the station a pod had no serving
  sample, a move of it could never be verified, and its mark never cleared. 0236 takes the
  station the controller keeps from Device Information. With the image
  `X86EMLTRBPIBB_rdk-next_20261005213817`: both pods rooted under the gateway with fresh
  serving samples (RCPI 138), and a move of pod-1 to the wired extender's backhaul BSS and back
  (`lab.sh move`) left no mark. The long run on it: the four geometry rooms in both rounds,
  26 of 27 catalog rooms in each (below), no pod left uncertain in any of 87 captured rounds,
  and a pod's move verified by the controller for the first time; one that took EMOSA longer
  than the controller's 15 s check was observed late and cleared.
- **Then the restart (finding 18).** That redeploy ended with both pods on GRE: each agent
  switched to the backhaul BSS a room's move had left as its kept target (spec 8.3), out of
  reach where the pods then stood; the switch timed out and held the pod, and a held pod takes
  no Backhaul Steering. Released by hand (`forget` in the gateway's fleet, OpenSync restarted:
  the lab's `backhaul wifi` refuses with EMOSA in the gateway). emosa-lab 948f0ec: a kept target
  that fails falls back to the configured upstream. In the next redeploy one pod's OpenSync
  restarted and the fallback applied on its new start; the other's `cm` reverted the station
  to its bootstrap credential on the same start, undoing the fallback written at once, and the
  pod was held. emosa-lab 83f4ce4: the fallback waits until the station no longer carries the
  failed credential. Verified in the gateway (its package installed over): both pods given an
  unreachable kept target (`02:00:00:00:19:03`) tried it, timed out after 90 s, waited 40 and
  60 s for `cm`'s revert, then switched to the configured upstream and applied it, no hold.
- **Seen, not failures.** The pods' default uplink is the gateway's backhaul BSS; a pod on the
  wired extender's backhaul BSS has no root path, since the native module has no parent for a
  wired extender, so it is not measured there. `home-a-wired-extender-loss-recovery` failed in
  each round of the last long run: once as before these changes, one sample exactly 5 s into
  the wired extender's 40 s outage, the check's grace, still showing a client on it (2 of 8
  runs now); once at the harness, its page load timed out at a host load of 16 from another
  Yocto build. em_ctrl's journal is
  partial: the image caps it at 1000 lines per 30 s and it writes about 1600.
- **Kept from 0235:** an agent marked uncertain after an unverified move now clears once it is
  seen, fresh, on a known parent 30 s later. Before, the mark stayed until em_ctrl restarted
  and left the agent and everything under it out of every root path.

### EMOSA wholly in the gateway: no adapter or GTP container (6 October)

The goal (user, 6 October): what still ran in the containers `emosa` and `em-gtp` into the
gateway, as a gateway carrying EMOSA would run it, then the containers removed and the
target configuration validated again. Three steps, on rdk-1004 (rev140).

- **1. The fleet and agents on the gateway's LAN** (emosa-lab 95a39da, meta-cmf 1b6a68b).
  The package's forwarder, `emosa-forward-c` (spec 3.1), carries the front and agent ports
  from the gateway's LAN address to the loopback ports the fleet and the agents listen on
  (fleet configuration `"forward": true`, `advertise` the LAN address). The pods' redirector,
  `10.101.0.40:6640` on the WAN side, is the operator's: `operator-redirect.py` on the VM
  answers it and hands each pod to `tcp:10.0.0.1:6640` (plan 5.3), as the operator's cloud
  would. No proxy of the lab's into the gateway any more.
- **2. The pods' broker in the gateway** (emosa-lab cb29316, meta-cmf 165fdb5): the image's
  mosquitto (`EMOSA_BROKER`, inert until configured), mutual TLS on `10.0.0.1:8883` for the
  pods and plain on `127.0.0.1:1883` for the agents; the lab's CA moved to the VM
  (`/var/lib/emosa-lab/pki`), the operator's side, which signs the pods' device certificates.
- **3. The pods' GRE termination point in the gateway.** Their onboarding SSID
  (`opensync-lab-bhaul`) is a VAP of the gateway's: `wifi1.4`, OneWifi's `lnf_radius_5g`
  (index 11) on bridge `brpodbh`; the image's `emosa-gtp` serves DHCP there
  (`169.254.2.1/25`) and ends the pods' GRE, its tunnels (`gtp2_<lease>`) in `brlan0`; the
  gateway's firewall accepts the underlay (syscfg `GeneralPurposeFirewallRule`, kept across
  its rebuilds). Five findings on the way, each fixed in the image (meta-cmf c5abb1d):
  - RDK's dnsmasq bound its DHCP socket to the wildcard address and the GTP's own DHCP server
    failed to bind beside it ("Address already in use"): utopia 0001, `bind-dynamic`.
  - Applying the controller's settings, OneWifi's translator reset an lnf_radius VAP
    (disabled, its default SSID and security): no EasyMesh haul type maps to lnf_radius, so
    the controller never configures one and nothing enabled it again.
  - The gateway's agent reported the VAP in its AP Operational BSS. RDK's controller accepts
    a Topology Response only when every operational BSS carries one of its network SSIDs: each
    of the gateway's failed ("SSID misconfiguration", all 22 in the captured log), its radios stayed in
    `topo_sync_pending`, candidate queries to them were refused (`Error_Not_Ready`) and steers
    from its BSSes ended in `association_timeout`. The room did not settle (11 of 20 clients
    measured), twice. libwebconfig 0014: lnf_radius VAPs stay out of EasyMesh, as hotspot
    VAPs do, in both directions, and their stations are not reported as the agent's clients.
  - The VAP first took the 5 GHz mesh station's slot (`wifi1.3`): the single-phy build makes
    exactly ten interfaces before OneWifi starts and removes them all when it stops, and the
    HAL cannot create another (the VAP has no BSSID yet: `-EADDRNOTAVAIL`). Out of EasyMesh,
    that cost the gateway one of its ten EasyMesh BSSes, which the lab's tools count
    (`Agent-1:9`, "topology incomplete"). The gateway's pre-start now makes the interfaces its
    VAP map names beyond the ten, each with a MAC of its own in `/nvram/mac_addresses.txt`;
    the pods' VAP is `wifi1.4`, beside them.
  - OneWifi keeps no VAP settings across its restarts in this build (no wifidb on `/nvram`),
    and only the EasyMesh VAPs get theirs back, from the controller: after a restart of
    OneWifi alone the pods' VAP came back with OneWifi's default SSID, WPA2-Enterprise and
    hidden, out of its bridge. The image's `emosa-podbh.service` (with `emosa-gtp`, wanted by
    and part of `onewifi.service`, before `em_agent`) gives it the SSID and key in
    `/nvram/emosa/podbh.conf` at each start of OneWifi; inert without the file. After a
    OneWifi restart it had them back within 5 s.
  - Seen in the lab's step, not the image: the gateway's agent applies the controller's
    settings with its copy of the radio's VAPs, taken when it started; `gateway.sh` restarts
    it after the SSID and key changed.
- **Verified at runtime on rdk-1004** (the library and the pre-start step in place, before
  the image): every node complete (the gateway and the five extenders at 10 BSSes, the pods
  at 5), no SSID mismatch, the pods' SSID unchanged through restarts of em_agent and em_ctrl,
  and the room settled with 20 of 20 clients measured and converged, 4 min into the lab's
  bring-up. A pod's bootstrap through the gateway, its OpenSync restarted: on its GRE uplink
  (lease `169.254.2.38`, tunnel `gtp2_38` in `brlan0`) after 16 s, connected to its agent in
  the gateway (`tcp:[10.0.0.1]:6651`) after 22 s, on its Wi-Fi uplink (the gateway's
  `mesh_backhaul`, applied) after 27 s.
- **Validated on rdk-1004** with the gateway image `X86EMLTRBPIBB_rdk-next_20261006083201`
  (meta-cmf c5abb1d; its EMOSA package from emosa-lab 95a39da, whose `c/` is unchanged
  since), redeployed in place with the lab's own redeploy. At the new gateway's first boot
  `emosa-podbh` gave `wifi1.4` its SSID, and both pods came back through it on their own:
  their leases from the gateway's GTP date from a minute after the gateway was up, twenty
  before any lab step ran, and the lab's redeploy passed its bring-up with them. `lab.sh up
  c gateway` then exited 0, and the long run passed whole: the room catalog (27 rooms) and
  the four geometry rooms, twice, 62 of 62, `home-a-wired-extender-loss-recovery` included
  both times (`rdk-1004-soak.sh`, 1 h 58 min). Through it the gateway held 505 MiB (median,
  515 peak, of 1 GiB): each EMOSA agent 5.0 MiB PSS and 0.6 % of a core, the fleet 0.7 MiB,
  the forwarder 1.0 MiB, mosquitto 1.4 MiB, the GTP's dnsmasq 0.4 MiB; em_ctrl 38 MiB and
  41 % of a core.
- **The pods' stations steering-disallowed** (spec 8.2). `lab.sh steering` (from `rooms
  pods`) reads each pod's station from its agent's status (`pod.backhaul.mac`) and puts it
  in every agent's local and BTM steering-disallowed lists through em_cli's policy API, one
  device per request. On rdk-1004 it set all 8 (18 s): the gateway's agent, the five
  extenders and both pods' agents list `02:00:00:00:6c:00` and `02:00:00:00:6e:00`, and
  EMOSA stored them in each agent's reporting policy; an extender re-onboarded (its
  em_agent restarted) had them again from the controller within 65 s. On RDK the
  onboarding SSID is out of EasyMesh, so the controller does not see the stations there at
  all: through a pod's bootstrap (its OpenSync restarted, on GRE after 15 s, on its Wi-Fi
  uplink after 28 s) the controller's topology listed its station in none of 84 polls, one
  a second.
- **em_ctrl's journal under its cap** (meta-cmf 1d42444 to 06ca98f). The image caps it at
  1000 lines per 30 s; through the catalog on rdk-1004 em_ctrl wrote 2,500 to 8,500 and
  journald dropped the rest in every window, the lab's evidence with it. unified-wifi-mesh
  0237 and 0238 print 129 statements of per-message work through em's debug channel
  (`/nvram/emCtrlDbg`, `/nvram/emConfDbg`), the M2 line without the networks' passphrases.
  Measuring it showed a loop too: the wired extender's radio, renewed alone, waited at the
  Channel Preference Query and the policy request for siblings already configured, timed
  out and was renewed again (43 times in 22 min; refusing candidate queries, the catalog
  did not converge). rdk-emosa-1005 showed it with the earlier image too. 0239 lets it on,
  as 0233 and 0234 at the steps before. With `X86EMLTRBPIBB_rdk-next_20261006124653`,
  redeployed: the catalog 27 of 27; em_ctrl 109 lines per 30 s on average, 635 at most,
  no window over the cap but one while `lab.sh steering` set eight devices at once (22
  over: now one a request, 10 s apart); no renewal in 50 min.
- **A fresh lab: rdk-emosa-1005** (rev120, beside rdk-emosa-1002, `EASYMESH_SHARED_HOST=1`),
  built from scratch with `EASYMESH_EMOSA_IN=gateway` (meta-cmf 75462bb, emosa-lab 04189dc,
  the gateway image of rdk-1004 then): no container but the pods from the start. Two
  findings (emosa-lab cdfaddc): on a new VAP map the gateway's agent restarted before the
  pods' SSID was set, the step missed that the keeper set it afterwards (it read systemd's
  last journal line, not the keeper's), and the agent's copy put OneWifi's default SSID back:
  no pod reached EMOSA; and `up c gateway` again on such a lab waited for uplinks never
  asked for. With both, the pods reached their agents within 30 s of the SSID, then their
  Wi-Fi uplinks. Redeployed with rdk-1004's image then (`X86EMLTRBPIBB_rdk-next_20261006124653`)
  and the option run again from the pinned emosa-lab cdfaddc, its suite: readiness, the
  catalog 26 of 27, the four geometry rooms with their recovery. The 27th,
  `home-a-wired-extender-loss-recovery`, failed at its known boundary (one sample at 25 s,
  5 s into the wired extender's outage, still showing a client on it). On the final image,
  run again: 1 of 3 with the browser on rev120, 2 of 3 with it on rev140, every failure that
  one sample at 25 s; rev120 runs two lab VMs at load 10 to 11. On rdk-1004 the room passed
  in all four catalogs of the day. No pod is in the check.
- **The final image, both labs** (`X86EMLTRBPIBB_rdk-next_20261006153516`, meta-cmf dad7504,
  EMOSA's package at the pin cdfaddc): unified-wifi-mesh 0240 takes em_ctrl's data model
  registration traces (some 800 lines at each of its starts) to the debug channel as well.
  rdk-1004, redeployed: the catalog 27 of 27, em_ctrl 103 lines per 30 s on average and 619
  at most, no renewal in 50 min; through the whole run (the redeploy, two bring-ups, the
  option, the catalog) one window over the cap, at a bring-up's controller restart (479
  lines over: every agent onboarding at once, WSC, renewals, Topology Responses), where a
  start had dropped 1,238 before 0240. Of four controller starts on the image three stayed
  under. Seen there, not EMOSA's: the gateway's agent and the wired extender send their
  first Topology Responses without a profile TLV and em_ctrl refuses them (17.2.4, 17.2.47,
  17.2.75).
- **em_ctrl's starts under the cap too** (meta-cmf 8f1522e,
  `X86EMLTRBPIBB_rdk-next_20261006175821`). unified-wifi-mesh 0241 takes the rest of the
  onboarding to the debug channel: each step of each radio's em_config (WSC, the topology
  sync, AP capability, channel preference and selection, the policy, their
  acknowledgements), the data model's nodes and the command candidates cancelled, 912 of
  the 1,386 lines of the restart captured on the image before. rdk-1004, redeployed: no
  em_ctrl line dropped in the whole run (the redeploy, three controller starts, the
  catalog); through the catalog 101 lines per 30 s on average, 459 at most. A bring-up's
  restart with all eight agents, measured on its own (`lab-bringup.sh up`, em_ctrl's
  journal followed throughout): 2,240 lines, 708 in the busiest 30 s, where the image
  before had passed 1,479. The catalog passed 26 of 27: `fifty-client-counter-roam` missed
  its initial convergence while another session's build held rev140 at load 25 to 30, and
  passed 3 of 3 alone afterwards. Open, seen at that restart: the gateway's em_agent passes
  its own cap (526, 2,030 and 618 lines dropped).
- **rdk-emosa-1005 on the same image** (redeployed in place with the lab's own redeploy, the
  option run again from the pinned emosa-lab cdfaddc; no emosa or em-gtp container): its
  suite passed whole, readiness, the catalog 27 of 27 (`home-a-wired-extender-loss-recovery`
  included) and the four geometry rooms with their recovery. No em_ctrl line dropped through
  the redeploy (three controller starts) and the suite. rdk-emosa-1002, which shared rev120,
  was stopped by its owner before the suite (rev120's load from about 9.5 to 4), and retired
  on 7 October: rdk-emosa-1005 is its successor.
- **em_agent's journal under its cap, the keys out** (meta-cmf 4225e97: the gateway image
  `X86EMLTRBPIBB_rdk-next_20261006231122` and the extender image
  `X86EMLTRBPIAP_rdk-next_20261006232411`, the extenders' first since 29 September).
  em_agent passed its own cap all the time: in one bring-up the gateway's agent wrote 8,744
  lines and a Wi-Fi extender's 20,913, most of them whole JSON documents one field to a line
  (OneWifi's Link Reports, which the agent then drops, the unassociated-station queries and
  responses, OneWifi's device configuration and every subdoc the agent sends it), and it
  printed every network's passphrase on the journal, 33 to 40 lines a bring-up.
  unified-wifi-mesh 0242 sends the documents and the per-message traces to the debug
  channel, the two configuration documents as their name and size, and takes the keys out
  of every line. A first image without the configuration documents fixed the agents'
  steady state (no line dropped through a catalog) but not their onboarding (2,739 lines in
  the gateway agent's busiest 30 s of a bring-up, 2,288 of them those two documents).
  rdk-1004, redeployed with both images: the catalog 27 of 27; through the whole run (the
  redeploy and its bring-up, three controller starts, the option, the catalog) no line
  dropped by em_ctrl or by any of the six em_agents, and no passphrase in their journals.
  rdk-emosa-1005, redeployed with both images and the option run again: its suite passed
  whole (readiness, the catalog 27 of 27, the four geometry rooms with their recovery), no
  line dropped by the gateway's em_ctrl or em_agent.
- **The wired extender's outage room, measured** (rdk-emosa-1005 on the image before, three
  runs, all passed; each client that leaves the extender followed with `iw event -t` in its
  container, read only). `home-a-wired-extender-loss-recovery` wants the extender's clients
  gone from it 5 s into its outage, sampled once a second. Its 2.4 and 5 GHz clients
  associate elsewhere 1.0 to 4.6 s after the cut (a scan of their known frequencies, or of
  both bands). Its 6 GHz client, STA-13 (PMF required), every time: beacon loss 0.7 to 1.4 s
  after the cut, a scan of all 50 6 GHz channels (1.9 to 2.0 s), then the target AP refuses
  the association once with a 1000 TU comeback (1.0 to 1.1 s): connected 3.7 to 4.5 s after
  the cut, seen by the room at the next sample. The comeback is 802.11w working as
  specified: the AP still holds STA-13's earlier association, because a roam drops it
  locally without sending a deauth (the client's `disconnected (local request)` with no
  frame transmitted), so APs keep roamed-away stations until their inactivity timeout
  (both extenders listed all three clients after they had left; an explicit
  `wpa_cli disconnect` cleared STA-13 at once), and a protected association from a station
  the AP believes associated starts an SA Query. rdk-1004 shows the same timing (STA-13
  first seen gone at the 25 s sample, no earlier). The room passes or fails on one sample
  on both labs; it is not EMOSA's, and its boundary is the owner's to set.
- **The containers removed.** `lab.sh up c gateway` is now the target configuration without
  them: on a fresh lab EMOSA, its broker and its GTP start in the gateway from the image
  (`gateway.sh on` writes the lab's fleet configuration there), and on a lab from the
  container option `lab.sh retire` deletes `emosa` and `em-gtp` (em-gtp's radio back to the
  pool) once EMOSA runs in the gateway. On rdk-1004 both were deleted on 6 October. The
  lab's `fleet` and `backhaul` (the pods' uplinks, a held pod released) now work with EMOSA
  in the gateway too.

### The physical pods, and an agent that lost its pod (7 October)

- **Two physical pods on rdk-1004's controller** (opensync-rpi's Raspberry Pis, over Ethernet,
  with their own fleet entries). The room service's health, the optimizer's observer, the
  room and geometry acceptance, the health audit and the lab's bring-up all counted them
  as the room's, so every room-service start failed (17:00 to 18:26 UTC), then no room could
  load, then the bring-up looped restarting every agent. Devices the room does not own are
  now listed in the VM's `/etc/easymesh-lab/foreign-devices` and left out by each (meta-cmf
  790e88f, f8e12bd, 277451c, 2432a05, 715c91c; opensync-rpi keeps the file). With both Pis
  present: the catalog 27 of 27 and the four geometry rooms with their recovery. The Pis'
  agents onboarded again about every 2.3 minutes (some 80 M1 each by 22:00 UTC): they accept
  RDK's five-BSS set, but the MT7921U starts one of the five VIFs, so each apply ends in
  APPLY_TIMEOUT and a recovery with a new M1 (9.A1, on the apply side). Meanwhile em_ctrl's
  policy API answered 504 now and then. The Pis were stopped until A1.
- **An agent that never got its pod back.** After the lab's two pods were recreated in place
  (easymesh-resources lab-storage W15: each stopped, cloned into the btrfs pool with its
  files, started again by `lab.sh pod`; the same serials and AL MACs), pod-2's agent
  (`MVXPOD02D7777EF0D9`, C) stayed `recovering` with no pod in its status
  (`report_source_available` false, `source_lost` 6) for over 50 minutes, through the
  bring-up's controller restarts and 10 minutes without them, while the pod's OVSDB
  connection stood through the forwarder (both legs established) and its Manager row said
  connected. The pod stayed on its onboarding GRE path, so the controller listed it with no
  BSS and the bring-up never completed. `systemctl restart emosa-agent@MVXPOD02D7777EF0D9`:
  16 s later it had the pod, the pod moved to the gateway's backhaul BSS and the controller
  listed its five BSSes. Pod-1, restarted the same way a minute later, recovered on its own;
  on rdk-emosa-1005 both did. Open: why the agent did not take the pod's connection again
  (its log went to RDK's logger, which the gateway had emptied).
- **A stored reporting policy that outlived the pod's radio** (open). The recreated pod-2
  came back with other radios from the lab's pool (its AP radio `02:00:00:00:6a:00` for
  `6d:00`; on rdk-emosa-1005 the same step kept the radios), and its identity stayed. Its
  agent's durable reporting policy (`state/<pod>/reporting-policy.sqlite`) still carried the
  old radio in its identity (`ruid`). The C keeps that record: `em_reporting_tick` reports
  nothing for it ("an old intent cannot become another pod's or controller's policy"), and
  every new policy is refused with `EM_NOT_READY` ("stored policy identity mismatch"), so the
  record is never replaced. In the controller the old radio came back after each restart
  (a row of no BSS, with its two policy rows), the model's radios one over the room's, and
  the room service failed its preflight. Cleared by hand: the agent stopped, its stored
  policy set aside (`state/MVXPOD02D7777EF0D9.stale-ruid-20261007/`), em_ctrl stopped, the old
  radio's rows deleted, both started. Wanted: a stored record of another identity
  superseded by the next policy received (and the reference and the specification §3.8 to
  agree), not a refusal that lasts until the store is removed.
