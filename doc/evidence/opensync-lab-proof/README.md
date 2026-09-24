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
