# Run record: EasyMesh controller onboards OpenSync pods through EMOSA

2026-09-24 (VM clock, UTC), branch `claude/0923-clean`, following the
[proof plan](../../project/proof-plan.md). Lab VM `emosa-osl-0923` on rev140,
built by opensync-lab branch `claude/emosa-hooks` (from `main` 53aadd9) with
pod image `mvx-pod-20260923124229`; deployment in
[deploy/opensync-lab](../../../deploy/opensync-lab/README.md).

**Result: Proof v1 of the plan holds on this lab.** A native prplMesh
controller (EMOSA's recorded candidate `candidate-ap-esp-02`, with its two
patches) onboarded
three unchanged OpenSync 6.6.1.0 pods as EasyMesh agents through EMOSA. Their
fronthaul came only from the controller's WSC M2, each pod's own `owm` applied
it, and six wireless clients used it to reach the internet. It held through an
EMOSA restart, a pod restart (cold start) and a controller-side policy change.

## What ran

| Component | Identity |
| --- | --- |
| Controller | prplMesh 6.0.0 (prplmesh-lab `fcb0b97` artifacts) + `candidate-ap-esp-02` (`beerocks_controller` cec287b9…, `libbpl` f25812f7…), controller + colocated agent, AL `02:00:00:e0:00:01`, container `em-ctl` |
| EMOSA | this branch, CPython 3.13.7, `uv.lock`; one `emosa-agent@pod-N` per pod, `python -m emosa.agent.pod`, profile `opensync-lab-hwsim-6.6.1-v1` |
| Virtual agents | AL `02:00:00:5e:00:01..03`, macvlans `em1..3` on the `em-1905` bridge |
| Pods | opensync-lab pod-1..3, OpenSync `6.6.1.0-0-g1dad64-mods-mvx-local`, serials `MVXPOD023F87E628DD`, `MVXPOD02D7777EF0D9`, `MVXPOD02288DCB5DCC` |
| Gateway | opensync-lab mv3 (RDK-B, OpenSync, local-noc cloud); pods' backhaul Wi-Fi + GRE unchanged |
| Controller UI | prplmesh-lab `e1fd7cd` controller-ui + topology adapter, unchanged, on the controller's NBAPI; host `http://192.168.2.140:8660/` |

## Sequence and observations

1. **opensync-lab regression gate** (`opensync-lab-regression.log`): with the
   new local-noc options at their defaults, mv3 + 3 pods + 6 clients pass,
   topology PASS. One pod check first failed on a 50 ms race with `cm`
   reconnecting after its fronthaul came up; re-run PASS (not caused by the change:
   the redirect map was empty).
2. **Handover (M2)**: `noc-ctl redirect <id> tcp:10.101.0.1:665N`. local-noc
   writes `AWLAN_Node.manager_addr` and ends the pod's session; the pod's own `cm`
   then connects to EMOSA through an LXD proxy into EMOSA's loopback-only listener.
   `cm` does not act on a new `manager_addr` while connected (cm2_event.c), so
   ending the session is required. Mesh stops writing that pod's fronthaul; the
   GRE on mv3 stays with local-noc. `pod-N.txt`: `Manager.target
   tcp:[10.101.0.1]:665N`, `is_connected true`.
3. **First onboarding (M4)**, pod-1 at 01:35:43: Search/Response, Early AP
   Capability, M1 from the pod's real radio (`02:00:00:00:01:00`, channel 6),
   authenticated M2 → one operation → one guarded transaction → `CONFIG_COMMITTED`
   (.639) → `OBSERVED_APPLIED` (.918) from the pod's own `Wifi_VIF_State`.
   Independent checks: nl80211 `ssid emosa-mesh` on `82:00:00:00:01:00`; PSK slot
   `key--1` replaced by `key`; backhaul STA and `g-bhaul-sta-50` unchanged.
4. **Controller's own view**: DataElements Device `02:00:00:5e:00:01`, profile
   1, SoftwareVersion `6.6.1.0-…`, parent controller over Ethernet, radio
   `02:00:00:00:01:00`, fronthaul BSS `82:00:00:00:01:00` "emosa-mesh", stations
   from the pod's `Wifi_Associated_Clients`.
5. **Clients**: `em-wc1` with only the controller's SSID/key, pinned to pod-1's
   BSSID: association, DHCP from mv3 across pod-1's GRE, ICMP, DNS, HTTP 200. A
   wrong-key client stayed in SCANNING with no lease.
6. **Three pods (M5)**: pod-2 and pod-3 handed over; one policy for all ALs;
   each operation `OBSERVED_APPLIED`. Six clients `em-wc1..6`, two per pod, all
   PASS (`clients.status`). Controller and UI: Controller → Agent-1 (gateway) →
   Extender-1/2/3 → 2 stations each (`controller-topology.json`,
   `controller-ui-topology.json`).
7. **Recovery**:
   - EMOSA process restart (pod-2): fresh discovery/M1/M2 within 0.3 s, clients
     stayed associated with internet.
   - Pod restart (pod-3): the pod rebuilt its database from its bootstrap with no
     fronthaul; its `cm` came back to EMOSA through the redirector (~90 s). The
     first attempt was `REJECTED NOT_READY` (the engine required VIF State
     before planning). After the cold-start change, the controller's M2 created
     `home-ap-24` in one guarded transaction and it was `OBSERVED_APPLIED`; both
     clients rejoined the same BSSID on their own. The rejected attempt stays in
     the journal.
   - Controller policy change to `emosa-mesh-2`: Renew → fresh M1/M2 per agent,
     all three pods applied it within 25 s; clients with the old key dropped;
     reverting brought all six back with internet.

## 900-second recovery workload (M7), run m7-01

`deploy/opensync-lab/vm/workload.py` (`lab.sh workload LABEL`). All six clients
ping the internet once a second for 901 s; a sampler records agents, the
controller's data model and the pods every ~6 s (146 samples). Evidence in
[m7-01/](m7-01/summary.json): timeline, faults, per-client ping logs.
**Result: passed**, every check true.

| Fault (offset) | Fault length | Recovered after it ended | Client data outage |
| --- | --- | --- | --- |
| client leave/join em-wc2 (60 s) | 40.6 s | 14.0 s | em-wc2 only, 54 s |
| adapter process restart, pod-1 (150 s) | 0.5 s | 1.6 s | none |
| OVSDB transport cut 20 s, pod-2 (240 s) | 21.7 s | 39.8 s (pod `cm` backoff) | none |
| backhaul loss 30 s, pod-3 (360 s) | 30.6 s | 29.4 s | em-wc5/6, 49 s |
| controller restart + policy re-entry 20 s later (510 s) | 45.1 s | 2.7 s | none |
| client leave/join em-wc4 (690 s) | 40.6 s | 13.9 s | em-wc4 only, 53 s |

"Recovered" is the first sample where every agent is provisioning with a live
source, every pod is connected to EMOSA, the controller shows each pod with its
expected stations and every client's ping succeeds. Every M2 during the run was
an observed no-op (0 writes in the running agents: no duplicate effects); no
operation is INDETERMINATE, FAILED, TIMED_OUT or in conflict. Agent RSS stayed at
50 to 54 MB. The controller restart recovers because each agent now sends IEEE
1905.1 Topology Discovery every 60 s, so the new controller finds the agents
again; prplMesh keeps BML credentials in memory, so the operator re-enters the
policy, and the resulting Renew is answered with a fresh M1.

## Channel and reporting policy (M8, first part)

With a channel policy store and the pod's measured channel and power, the
controller's Channel Preference Query and Channel Selection Requests are
answered and each agent sends Operating Channel Reports, which the controller
acknowledges. The controller now shows operating class 81, channel 6 for every
pod (it showed channel 0 before). Its Multi-AP Policy Config is acknowledged;
metric reports then fall due with no qualified source and EMOSA sends none
(recorded as `metric_reporting_due_without_qualified_source`).

Metrics need the pod's own statistics: OpenSync `sm`/`qm` publish over MQTT with
mutual TLS only (`/var/certs/ca.pem`, `client.pem`, `client_dec.key`, peer
verification on). The lab image has no device certificate.

**Statistics spike (pod-1, then reverted).**
- *Setup:* `lab.sh telemetry` sets up a lab CA and a Mosquitto broker in the
  `emosa` container. Pods reach it with mutual TLS at `10.101.0.1:8883`, and EMOSA
  subscribes on loopback. `lab.sh provision pod-1` puts a lab device certificate
  in the pod's `/var/certs`, which is device provisioning as a factory does it.
- *Trial:* with `mqtt_settings` and two `Wifi_Stats_Config` rows (client and
  survey, 2.4 GHz, 5 s / 10 s), `dm` started `qm`. `qm` connected with the
  pod's certificate, and `sm` reports arrived every 60 s. EMOSA's pinned
  `sts.Report` decoder read them: per station, MAC, SSID, per-interval rx/tx bytes
  and frames, rx/tx rate, RSSI and interval length. No survey report was produced
  on hwsim.
- *Revert:* the trial config was written with `ovsh` on the pod, outside EMOSA's
  session, so it was removed afterwards.

**Finding:** EMOSA's AP Metrics Response needs a complete measured bundle, and
the pod supplies none of it on hwsim. The missing parts are:
- channel utilization;
- the mandatory best-effort ESP;
- per-BSS byte counters;
- radio noise and utilization.

EMOSA therefore still sends no AP metrics rather than inventing values. Station
metrics have a qualified source:
- rates directly;
- traffic counters accumulated from the per-interval deltas.

RCPI needs the noise floor a survey would give. Next steps:
- EMOSA writes the telemetry configuration through its own session.
- A per-pod subscriber feeds station link and traffic metrics.
- AP metrics wait for a platform with survey data.

## Second controller: RDK-B unified-wifi-mesh (M9, first attempt)

**Setup:**
- The controller image `X86EMLTRBPIBB_rdk-next_20260922213132` (from
  meta-cmf-bananapi-vcpe, layer `3beda39`; its build checkout carries 21
  uncommitted changes) was launched in the lab VM with that project's own
  `gen/bpi.sh -b br-wan101 -l em-1905`.
- Its LAN port joined `brlan0`, which put the RDK controller on EMOSA's
  EasyMesh LAN.
- In the VM's copy of `gen-util.sh` only, one guard was skipped. It requires a
  cfg80211 patched against a netns cleanup bug seen on Ubuntu 7.0 kernels, and
  this VM runs the stock 6.8 module.
- The prplMesh controller was stopped, and the three agents were bound to the
  RDK controller's AL `00:60:2f:da:68:d4`.

**Observed:**
- The RDK controller and its colocated agent (`…68:e4`) found all three EMOSA
  agents through their Topology Discovery. They sent them Topology and Link
  Metric Queries.
- EMOSA's AP-Autoconfiguration Search was rejected
  ([log](rdk-controller-search-rejection.log)): "Received autoconfig search with
  profile type 1 … Failed TLV: 17.2.47 … failed validation".
- No Response was sent, so no M1/M2 followed.

**Cause:** unified-wifi-mesh `em_msg.cpp` validates against EasyMesh 5.0 with
profile-gated presence rules. For a Profile-1 peer it marks the following as
not allowed:
- the Multi-AP Profile TLV in Search;
- the Profile-2 AP Capability and AP Radio Advanced Capabilities TLVs in M1.

EasyMesh 6.1 (§6.1, §17.1.1, as EMOSA's procedure audit records) has a
Profile-1 device include one Profile TLV and the Profile-2 AP Capability TLV.
EMOSA also admits a controller only with the 6.1 Controller Capability fields.

**Result: not onboarded, EasyMesh edition mismatch.** Getting through needs a
deliberate choice:
- an EMOSA "R1 compatibility" message set across Search, M1 and admission; or
- EMOSA implementing and advertising Profile-2/3; or
- a controller that accepts 6.1 Profile-1 messages.

The prplMesh setup was restored afterwards: the RDK container was stopped and
kept, the agents rebound, the policy re-entered, and all three pods are back
with 2 clients each.

## Changes made during the run

- `pod_profile.py`: the observed 6.6 encoding (`wpa-psk` + RSN, `key` slot);
  cold-start creation from M2; "ready to plan" means pod and radio bound,
  application still needs the VIF's own State.
- `agent/pod.py`: the virtual-agent service; Renew (0x000A) from the bound
  controller starts a fresh attempt (`OnboardingRecovery.renew`, EasyMesh 6.1
  §7.1); source lease 1.5 s (`(t + 2) - t` can exceed 2 in floating point).
- `operation_bridge.py`: the WSC bridge admits the qualified profile mode.
- Deployment: the controller bridge is created with its AL address (udev
  otherwise overwrote it); one EasyMesh NIC per container plus a macvlan per
  agent (LXD allows one NIC per managed network); UI on 8093 (8091 is boardfarm's).
- opensync-lab `claude/emosa-hooks`: `--redirect`/`noc-ctl redirect` with session
  end on handover, fronthaul writer exclusion, `MVX_CLIENT_SSID/PSK`.

Tests: 1523 unit (6 new) and 59 OVSDB pass; lint clean.

## Limits of this result

- hwsim pods, not a physical pod; the controller carries EMOSA's two patches.
- Declared representation: each pod appears Ethernet-attached to the controller
  (EMOSA's port on `em-1905`); its real path is Wi-Fi + GRE to mv3, owned by
  OpenSync and local-noc.
- 2.4 GHz fronthaul only; the controller shows channel 0 for pods (no Operating
  Channel Report yet). Station association age is time since EMOSA first saw
  the station.
- No sustained reporting (AP/STA metrics, MQTT stats); R and S levels not run.
- A raw `lxc restart` of a pod needs opensync-lab's `hwsim_reclaim` first
  (lab mechanics, not EMOSA).
