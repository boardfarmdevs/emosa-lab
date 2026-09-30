# EMOSA in the RDK lab: the record, 25 to 29 September 2026

**Record.** This is how EMOSA came into the RDK EasyMesh lab: the first pod, the
room integration and the 24-room qualification, and EMOSA as an option of the
lab with the pods on Python, on C and mixed. It is dated and not edited
afterwards. The current design and how to run it are in
[`doc/architecture/rdk-lab.md`](../../architecture/rdk-lab.md); section
references (§N) below are to that document as it was then (emosa-lab
`e89631b`). VM names, run directories and commits are as they were: `rdk-emosa`
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
| 6. Topology | the pod in the controller's topology and in em_cli, marked as an OpenSync pod | passed: Agent-1 with kind `opensync-pod`, its own icon, model and manufacturer on hover and in the dashboard (meta-cmf `37c70e8`, controller image `…20260925081052`; installed in place in `rdk-emosa`) |
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
replaces the station's watch row. Deployed in `rdk-emosa` (EMOSA `2815061`)
for runs 6 to 8 of the pod-variant suite.

### Step 5: the pod-variant suite (done)

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
recovery). This is the optimizer's collection rate, not the images (below).

### The open items (28 Sep)

What the images left open, each traced to its cause in the lab (meta-cmf
`9a0ac77` to `5616b68`, unified-wifi-mesh 0222 to 0230):

| Found | Fix |
| --- | --- |
| Candidate collection over 20 radios took about 12 s a round, longer than the candidate stream's 7.5 s refresh: the controller admitted one Unassociated STA Link Metrics Query at a time for the whole mesh | 0222: one query in flight per agent, em_cli runs four agents at once (`/api/v1/coordination`); the optimizer's parallel collection defers a query the controller did not admit to a second pass instead of failing the agent (`9a0ac77`). 0229: a reply completes its own radio's query, and a query times out on its own (the type's timer never went idle with four agents queried continuously, so every check cancelled every query and radios stayed "not ready"). A round over 24 queries (500 candidates, 100 clients) now takes 3.2 to 3.8 s with four agents and 7.0 to 7.7 s one at a time (12 s before); the room settles 2 minutes after the topology, all 20 clients checked |
| The RDK agents never answered the Channel Preference Query. The agent built its command on the AL node in `ap_cap_report`, a state only the radios reach since 0180; the query was dropped without a word, the controller re-sent it about once a second, its radios stayed in `channel_query_pending` (em_config's fourth step) and refused every candidate query as not ready | 0223: the AL node answers every query at once. Completing em_config then needs: 0224, a Channel Selection Request without preferences keeps the channel; 0225, no default channel preferences in the controller (its presets, op classes 83/6, 128/42 and 135/7, would move every agent to those channels and widths, off the rooms' 20 MHz 6, 36 and 37); 0226, the policy ACK configures every radio of the agent (the others waited in `set_policy_pending`). In the lab all 20 radios reach topology publish, the channels stay put and the extenders' backhaul stays up through the gateway's full policy (`4eec210`) |
| em_config's timeouts read the type's time (the longest any agent's em_config took since the type was last idle): with every agent now running all em_config steps, one slow agent got every other agent reaching topology sync cancelled and renewed | 0230 (`5616b68`): each agent's em_config times out from its own start and cancels only itself. After a controller restart, topology sync takes 1 to 16 s for most agents; an agent caught between the controller's renews and its own restart can still loop through renews (118 and 258 s), as before these patches (the 04:13 bring-up needed two agent restarts); `lab-bringup.sh up` restarts the agents again and the topology completes |
| After a controller restart the gateway's agent sometimes stayed with a radio in WSC (Agent-1 with 4 of 10 BSSes). The renewed radios' M2s arrive in one second; the agent pushes one OneWifi subdoc at a time, a queued radio asks again with M1, and the controller ignored an M1 for a radio in `wsc_m2_sent` | 0228 (`ce6c84a`): the controller answers it. `lab-bringup.sh up` restarts the agents up to three times and names the short nodes (`ce6f2c1`) |
| `received-discovery-recovery` (`native_owner_mismatch`): a Topology Response claim for a station another BSS holds is taken only if younger, but the existing claim's age stops at its AP's last report. A station back on A after a short stint on B kept B's small frozen age (live: 316 s against 57 s) | 0227 (`8134cb7`): the controller keeps when each claim's association began (boot clock) and compares those |
| "Geometry native outage": all four extenders without a parent at once in `backhaul-branch-formation`. The room's own geometry: extender_1 and extender_2 hear extender_3 and extender_4 better than the gateway (20 against 13 dB on 5 GHz), so the native tree may start with them hanging off extender_3 and extender_4; the midpoint then inverts the whole tree, and re-forming took 62 s of the room's 60 s | `4f024f7`: the branch convergence has 150 s and the report keeps the initial native parents. The 27 Sep failure was another room (`backhaul-parent-handover`: extender_3 never moved to extender_2, native stickiness) |
| The gateway container reached its 1 GiB memory limit: the evidence journal (271 MB, in `/run`) and the daemons (450 to 700 MB). Memory-cgroup thrash: VM load past 100, every `lxc exec` and `wpa_cli` timed out, all four Wi-Fi extenders lost their backhaul | `825b76e`: 96 MB of evidence journal on the gateway, 256 MB on the extenders |

The suite with all of them installed in place (rev120
`test-results/emosa-pods-wired-0230-20260928T162152Z`): eight steps of nine,
the catalog 23 of 24, geometry all three rooms (0 of 3 on the images before).
`fifty-client-counter-roam`, `home-a-flash-crowd`, `home-a-fast-transit` and
`large-room-extender-evacuation` passed. The one miss,
`received-discovery-recovery`, was the test's own audit: its query of the
client's state to the VM's LXD API took longer than 5 s (meta-cmf `3f5a8e0`:
20 s); the room's checks had passed.

The images carrying all of them (28 Sep 18:07): controller
`X86EMLTRBPIBB_rdk-next_20260928161525`, extender
`X86EMLTRBPIAP_rdk-next_20260928162627`, both from meta-cmf `5616b68`, no
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
(unified-wifi-mesh 0231, meta-cmf `239f9f6`): rounds 3.5 s under the load,
no timeout. The room then passed 2 of 3 alone; the miss was coverage
flapping around 210 of 250: a client counts as measured only while all its
candidates are younger than 15 s, and refreshed at 7.5 s a candidate whose
query met a busy radio could expire before the next round. The room now
refreshes at 5 s (meta-cmf `a8b256b`): `fifty-client-counter-roam` passed 3
of 3 alone, loaded in 81, 84 and 81 s of its 90 s (first convergence 75 to
79 s). Images with 0231: controller `X86EMLTRBPIBB_rdk-next_20260928203255`,
extender `X86EMLTRBPIAP_rdk-next_20260928204402`.

EMOSA in C (emosa-lab `beef95a`): the statistics decoder reads the pod's
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

The images from meta-cmf `239f9f6` (0231; controller
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
timer and its direct attach (meta-cmf `5bba511`); the second ran through.

The RDK controller's `SteerWiFiBackhaul()` turned out to be an upstream stub
that answered Success and sent nothing; unified-wifi-mesh 0232 (meta-cmf
`96188ea`) hands the request to the lab's native backhaul steering, which sends
the Backhaul Steering Request. With it, redeployed with `gen/lab-redeploy.sh`,
the controller moved the pods: pod-1 onto extender_1's backhaul BSS in 6 s and
back to the gateway in 6 s, pod-2 onto the wired extender's in 16 s, and the
controller got EMOSA's Backhaul Steering Response. Two findings on the way: a
Wi-Fi extender starts its backhaul BSS only for a child, so the move first
starts it (the room does this for its geometry rooms, meta-cmf `0286c5e`); and
the move changes the pod's reported topology, over which the agent renews its
session, so the answer to a move under way is now the agent's, shared by its
sessions (emosa-lab `a924b26`).

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
  room with pods now gets the 150 s branch window (meta-cmf `82704f1`).

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

- **Both pods on C** (emosa-lab `34d2254`, run
  `suite-rdk-emosa-0929-c-20260929T184235Z`): 8 of 9 stages on rev120. The one
  catalog room that failed, `fifty-client-counter-roam`, is the one that failed
  with Python on rev120: its initial convergence ran out of the 90 s (the
  roster came at 51 s) while the software-rendered browser shared rev120's 12
  cores with the lab VM. From rev150 it passed, converged in 13.6 s.
- The geometry rooms passed on rev120 with the pods on C. The Python runs on
  rev120 predate the 150 s branch window for loaded rooms with pods (meta-cmf
  `82704f1`); with it, Python passed them from rev150. The two are not
  compared on rev120 here.
- **One of each** (emosa-lab `e3a5133`, run
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
