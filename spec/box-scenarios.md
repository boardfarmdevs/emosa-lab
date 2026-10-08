# The lab in a box: its scenarios

**Reference.** Every behaviour the RDK lab's rooms and the reference workload rely on,
and every finding of the lab suites, as a scenario of the lab in a box
(`lab/src/emosa_lab/box.py`, easymesh-labs alignment plan step 8.2). Each scenario runs
the real agent binary, C or Python, in a private network namespace against:

- **a pod:** a disposable `ovsdb-server` loaded with an unchanged OpenSync 6.6.1 pod's
  recorded rows (`tests/fixtures/opensync/pod-6.6.1-hwsim-tables.json`, or the pod on a
  Multi-AP backhaul, `pod-6.6.1-hwsim-uplink-multi-ap.json`), which dials the agent as a
  pod does after its redirect. The box plays the pod's managers where a scenario needs
  them: `wm` applying an AP's Config to its State, `owm` taking a steering window, `cm`
  and the supplicant moving the backhaul station, `qm` publishing the pod's statistics;
- **a controller:** the reference's wire code on the other end of a veth pair, answering
  with an M2 from hostap's own registrar (`.cache/wsc-registrar`);
- **a broker:** `mosquitto`, for the scenarios with the pod's statistics.

Both implementations run every scenario (`tests/test_box.py`, marker `box`); CI runs them
all (`checks.yml`, job `box`, one run per agent). On a host without CI's toolchain (cmake, clang 18,
Ubuntu 24.04), `scripts/run-box-in-container.sh` runs them in a container. One scenario
alone, its result as JSON:

```sh
python -m emosa_lab.box SCENARIO --agent c|python --directory DIR
```

The vectors (`spec/conformance`) fix exact outputs for recorded inputs; the box shows the
same behaviour end to end, with real timing, a real OVSDB server and a real WSC exchange.

The box's pod also serves a real EMOSA elsewhere, on a router or in a lab, before a physical pod
is there: `python -m emosa_lab.remote_pod --fleet HOST:PORT [--serial SERIAL]` hands the recorded
pod to that fleet's front port and then behaves as a pod's `cm` and managers (it follows
`manager_addr` to its agent and applies the agent's writes). Outbound TCP only, no radio; the
fleet must admit the serial and give it the profile `opensync-lab-hwsim-6.6.1-v1`
(`tests/test_remote_pod.py`). From an older host, in the container:
`scripts/run-box-in-container.sh -- python -m emosa_lab.remote_pod --fleet HOST:PORT`.

## Onboarding and the session (spec 2.5)

| Scenario | What it shows | Relied on by |
| --- | --- | --- |
| `boot` | the agent takes the pod's connection, sends Topology Discovery and searches for its controller | every room with pods |
| `agent-netns` | the agent's interface in a network namespace of its own (`EMOSA_NETNS`, a `/proc/PID/ns/net` path in the box): no interface of the agent's namespace has its AL MAC, and it onboards (spec 2.1) | finding 17 |
| `onboard` | an EasyMesh 6.1 Response, the Early AP Capability Report, M1; a registrar's M2 written to the pod as one guarded change, counted applied only when the pod's State shows it | every room with pods |
| `refuse` | a controller without the Controller Capability TLV: incompatible, no M1 | finding 1 (below) |
| `early-report` | the Early AP Capability Report retried with a new MID each time, three at most, until its Ack | RDK's controller |
| `renew` | a Renew: at once a Search and a fresh M1; provisioning again with the new M2 | the controller re-onboarding a pod |
| `no-m2` | an M1 left unanswered: onboarding again after 30 s | finding 2 |
| `silent-controller` | nothing from the controller for 130 s: onboarding again | finding 3 |
| `forgotten-agent` | with `topology_query_window` (20 s in the box): kept while the controller sends a Topology Query every 5 s with its other queries; onboarding again once only the others come, and provisioning again | finding 15 |
| `unserved-pod` | provisioned, but the pod serves no BSS of the controller's and nothing is written: onboarding again after 60 s | finding 4 |
| `new-source` | the pod back with a new database (its OpenSync started again): a new session, and the configuration written again | findings 5 and 11 |
| `pod-recreated` | the pod recreated in place: a new database, the same serial, another AP radio from the lab's pool; the agent takes the new pod and onboards it on the new radio | finding 19 |

## Reports and answers (spec 2.4, 2.6, 3.4)

| Scenario | What it shows | Relied on by |
| --- | --- | --- |
| `answers` | frames not for the agent ignored, the agent running on (a Topology Query to the broadcast address, a runt shorter than a CMDU header); every request answered with its MID (Topology, AP Capability, Channel Preference, Client Capability, Backhaul STA Capability, Unassociated STA Link Metrics, Multi-AP Policy); Link Metric and AP Metrics withheld without statistics, their reason recorded | the controller's model of the pod; a runt on the link stopped the reference agent (3 October, fixed: the endpoint drops what it cannot decode) |
| `clients` | a station joining and leaving: a Topology Notification with a Client Association Event each time; its age in each Topology Response as of that response | the rooms' clients on pods; finding 6 |
| `reannounce` | at the controller's next Topology Query once provisioned, every current client announced again, once | finding 7 |
| `channel-selection` | a request the pod can honour accepted (code 0), RDK's declined (code 2), the radio not moved; each followed by the Operating Channel Report | RDK's channel planning |
| `channel-scan` | acknowledged, then a Channel Scan Report: each requested channel, status 1 (not supported) | RDK's controller |

## The pod's statistics (spec 3.6, 3.8, 3.9)

| Scenario | What it shows | Relied on by |
| --- | --- | --- |
| `telemetry` | the broker, the pod's topic and its reports written as one guarded change, applied once in the pod's database; reports through the broker reach the agent; a repeated one is dropped | the rooms' client signal on pods |
| `foreign-broker` | another manager's broker in the pod's MQTT settings is not taken over: the write is refused, the settings stay | pods under an operator's cloud |
| `metrics` | the Metric Reporting Policy kept; an AP Metrics Query answered with the channel utilization from the survey, the BSS's stations, their link metrics, and traffic statistics for the stations whose counters the pod measures; unsolicited at the policy's interval | RDK's controller, which learns a client's signal only this way; the optimizer |
| `unassociated` | the stations asked about watched on the pod; one the pod heard reported with its RCPI; an associated one refused with reason 1, one not heard with reason 2 | the optimizer's candidate measurements for pods' clients |

## Steering (spec 3.7, 8.3)

| Scenario | What it shows | Relied on by |
| --- | --- | --- |
| `steering` | a mandate: acknowledged, a steering window opened as one write (the client row, a group, the target as a neighbor), applied when `owm` steers, the directed kick, the window closed deleting exactly its rows | the optimizer steering pods' clients |
| `steering-refusals` | a station not on the source (Error Code, reason 2), a steering opportunity (Steering Completed at once), an agent-selected target: none opens a window | the optimizer |
| `backhaul-capability` | on a Multi-AP backhaul: the uplink switch (option 1) applied once the pod's State shows the station on its parent; the Backhaul STA Capability Report names the station | the geometry rooms with pods; finding 8 |
| `backhaul-steering` | the backhaul station re-pinned to the target; once the pod's State shows it there, the Backhaul Steering Response with the request's MID, success | the geometry rooms with pods |
| `backhaul-steering-renewal` | a Renew and a new onboarding during the move: the answer still comes, with the request's MID | finding 9 |
| `backhaul-steering-own-bss` | a target that is one of the pod's own BSSes: refused at once, failure with an Error Code; the station stays pinned | finding 10 |
| `pod-child` | spec 8.5: the pod on its EasyMesh backhaul under another pod's backhaul BSS (that pod's agent in the box as the peer directory reads it: its status beside the agent's): the Topology Response names that agent as a 1905 neighbor on the backhaul station, besides the controller on Ethernet, and a Link Metric Query is answered from the parent's measurement of the station, the directions swapped | alignment plan 9.5 |
| `pod-parent` | spec 8.5: another pod's station on this pod's backhaul BSS, that pod's parent: the Topology Response names its agent on the BSS, and a Link Metric Query for it is answered from this pod's own client report of the station | alignment plan 9.5 |
| `pod-parent-loop` | spec 8.5: a Backhaul Steering Request whose target is the backhaul BSS of a pod hanging off this one: refused at once (failure), the station left where it is | alignment plan 9.5 |
| `backhaul-steering-refused` | without option 1 (the pod on GRE): refused at once, failure with an Error Code | finding 12 |

## The reference workload's faults

| Scenario | What it shows | Relied on by |
| --- | --- | --- |
| `adapter-restart` | the agent killed and started again on its journal: onboarded again, the configuration found running, nothing written again | the workload's adapter restart |
| `transport-cut` | the pod's management connection cut for 20 s: the source lost; when the pod dials again, onboarded anew and provisioning | the workload's transport cut and pod backhaul loss |
| `write-lost-unapplied` | the pod's link lost while the agent's write commits (the box's reply cut: the write in the pod's Config, its reply lost, INDETERMINATE) and the pod never applying it: past its deadline (120 s) the operation ends `TIMED_OUT`, not blocking the pod; the agent onboards again, and the next M2 is written and, once the pod applies it, observed applied | finding 22 |

The workload's controller restart is the controller's silence, its Renew or its new
Topology Query: `silent-controller`, `renew` and `reannounce`. A client leaving and
joining is `clients`.

## The features' faults and refusals (plan 8.4)

The paths a working lab seldom takes, each with either agent
(`lab/src/emosa_lab/box_features.py`). The box's registrar also answers an M1 with an M2
set: a fronthaul and a backhaul BSS (the backhaul with the Backhaul STA bit, as prplMesh
sends it), each from its own registrar session with its own BSS index.

| Scenario | What it shows | Relied on by |
| --- | --- | --- |
| `multi-bss` | an M2 set of two BSSes: both written as one guarded change and applied, the backhaul BSS on the profile's backhaul slot; its credentials serve the uplink switch (`credentials: m2`), pinned to the configured parent | RDK's controller (`r1`, multi-BSS) and the Wi-Fi backhaul |
| `set-beyond-slots` | RDK's five-BSS M2 set to a radio whose profile has no extra slots (the Pis' MT7921U, one AP): taken, not refused; the primary fronthaul BSS written and applied, the other four left out, only the primary's passphrase stored | RDK's controller and pods with fewer BSSes than its set (alignment plan 9.A1) |
| `fronthaul-role` | an M2 set of two fronthaul BSSes and a backhaul BSS: each written with its M2's role in `multi_ap` and applied, the recorded fronthaul (its `multi_ap` unset) updated to `fronthaul_bss`, the second fronthaul created on a slot as `fronthaul_bss`, the backhaul BSS `backhaul_bss` | finding 21 |
| `fronthaul-role-cold` | the Pis' case: a pod whose bootstrap made its radio only (the box leaves the recorded fronthaul out and gives a VIF the agent creates a BSSID, as `wm` does) gets RDK's five-BSS set; the fronthaul the agent creates carries `fronthaul_bss` and is applied | finding 21 |
| `wired-uplink` | a wired pod, cm using its Ethernet port eth1 as the uplink, and uplink mode ethernet: the agent bridges eth1 into br-home (`Connection_Manager_Uplink.bridge`) in one guarded write, applied, and reports the uplink as ethernet, in use | wired pods (the Pis, alignment plan 9.A1) |
| `uplink-held` | a switch the pod never confirms: `TIMED_OUT` after 90 s, the pod held on option 2, no second switch | spec 8.3's hold; the RDK lab's `backhaul wifi` releasing it |
| `uplink-foreign-change` | another manager changes the switched station's credential on the same start: held, the change left alone | spec 8.3 |
| `backhaul-kept-gone` | a kept target whose BSS is gone at the agent's start: its switch `TIMED_OUT` after 90 s, nothing written while the station still carries it, then (the box playing `cm`'s revert) the configured parent switched to on the same start, applied, no hold, the kept target dropped | finding 18 |
| `backhaul-steering-kept` | the agent started again after a Backhaul Steering move keeps the station on the target, not the configured parent | the RDK lab's geometry rooms (the controller moves pods) |
| `telemetry-broker-restart` | the broker gone and back: the agent notices, reconnects with its back-off, subscribes again and takes the next report | the pods' statistics in every metrics room |
| `steering-conflict` | another manager's client row for the station: a mandate opens no steering window and leaves that row as it was | spec 3.7 |
| `steering-window-expired` | a window the pod's owm never takes ends at its deadline: the rows it inserted deleted, the outcome `not_applied` | spec 3.7 |
| `steering-restart` | the agent killed with a window open; started again on its journal, it closes the window it finds left over | spec 3.7, design 6 |
| `steering-leftover` | a window's close failed (the pod's link cut while it was open), its rows left in the pod; with the pod back, the rows closed by their UUIDs (the sweep) and a second mandate for the station carried out, not refused as another manager's | finding 20 |
| `steering-close-refused` | a window's close refused by the pod's database with the link up (the box's reply cut answers the delete with an error): its rows stay on the same source; the sweep tries again after 30 s and closes them by their UUIDs, and a second mandate for the station is carried out, not refused as another manager's | finding 23 |

## The adapter around the agents (spec 4, 8.2; plan 8.3)

Each implementation's fleet and GTP, Python or C, as each agent above
(`lab/src/emosa_lab/box_adapter.py`). The fleet's scenarios hand the box's pod to the real
fleet at its front port, as an operator's redirector does, and play the pod's `cm`: when the
fleet writes `manager_addr`, the pod's database dials the agent there. The agents' units are
a `systemctl` of the box's (plain processes, as `emosa-agent@.service` runs them).

| Scenario | What it shows | Relied on by |
| --- | --- | --- |
| `fleet-handover` | a pod at the front port gets its agent: the registry entry (the first port, `em1`, its AL MAC), its configuration, the unit enabled and started, `manager_addr` written; the agent onboards the pod | every lab's pods (opensync-lab's local-noc, the RDK lab's redirector) |
| `fleet-return` | the pod back at the front port (a reboot, its cloud's redirect): the same entry, one more handover, the unit started (a no-op), never restarted; the same agent provisions it again | pods that restart |
| `fleet-upgrade` | an image upgrade: the agent gone with its unit, the fleet started again on the kept files starts the registry's agent at once (enabled, started), with no pod at the front port; the pod, still dialing the agent's port, provisioned again | EMOSA in the gateway image (4 October: after an upgrade the agents waited for each pod to come back through the front port) |
| `fleet-refusals` | a pod not admitted, one with an unusable serial, one with no free port: nothing written to the pod, no agent configured or started for it (the registry's pod that holds the port gets its agent when the fleet starts) | spec 4 steps 1 and 2 |
| `fleet-takeover` | the other implementation's fleet hands the pod over first; this one, on the same registry and configurations, lists the same entry and leaves the running agent alone (its configuration's text is the same) | switching the adapter's implementation in a lab |
| `fleet-forget` | `forget` (its own process, while the fleet serves): the agent stopped and disabled, entry and configuration gone, state archived without the agent's status; handed over again, a new entry and agent | finding 14; opensync-lab's `release`; finding 16 |
| `fleet-supervised` | the C fleet without systemd (`"agents": "supervised"`), either agent its child: handed over with no `systemctl`; an agent killed is started again and provisions the pod again; the fleet stopped stops it, started again starts the registry's agent at once; `forget` stops it for good; its output in `run_root/POD/agent.log` | a router with busybox init (the MV3) |
| `gtp` | `setup` addresses the underlay (raising its port's MTU), bridges the LAN port and writes a dnsmasq configuration dnsmasq accepts; lease events through the hook setup wrote add one gretap per pod into the LAN bridge (a renewal changes nothing) and delete it; `reconcile` makes the tunnels the leases; a lease outside the underlay is refused | the RDK lab's `em-gtp`, the pods' GRE path |

## The suite findings

| # | Finding | Where seen | Scenario |
| --- | --- | --- | --- |
| 1 | the C admitted controllers the reference refuses | the box, 29 September | `refuse` |
| 2 | a controller that restarted after M1 never sends M2 | RDK lab suite | `no-m2` |
| 3 | a controller that goes silent, in any state | RDK lab suite | `silent-controller` |
| 4 | a provisioned pod whose configuration was lost (a write lost to an uplink move) | RDK lab suite | `unserved-pod` |
| 5 | the pod dropped with its extender and back on a new source | RDK lab suite | `new-source` |
| 6 | stations' ages taken as of each Topology Response | RDK lab suite | `clients` |
| 7 | clients that joined before a controller restart never re-learned | RDK lab suite | `reannounce` |
| 8 | the uplink switch reported done before the pod's State showed it (pods held on the GRE path) | RDK lab, 1 October | `backhaul-capability` |
| 9 | Backhaul Steering's answer lost to a session renewal during the move | RDK lab geometry rooms | `backhaul-steering-renewal` |
| 10 | a pod's station on its own backhaul BSS looped `br-home` | reference lab, 25 September | `backhaul-steering-own-bss` |
| 11 | a pod whose OpenSync started again counted as another manager's change (an ownership conflict), so its configuration was never written again; fixed (spec 5) | the box, 3 October | `new-source` |
| 12 | without option 1, the failure answered without its Error Code TLV; fixed | the box, 3 October | `backhaul-steering-refused` |
| 13 | the refresh cadence counted from a refresh's end, the lease from its start: one slow refresh (0.8 s) lapsed the lease and ended the session just after provisioning; fixed (design 4.3) | the box in CI, 3 October | `onboard` |
| 14 | `forget` run while the fleet served did not take: the serving fleet kept the registry in memory and wrote the forgotten entry back at the pod's next handover; fixed (spec 4: the registry file is the state, read for each pod) | the box, 3 October | `fleet-forget` |
| 15 | after a controller restart the pods stayed unregistered: the controller forgot the agents but kept sending them its other queries (no Topology Query), so the silence rule never fired; fixed with `topology_query_window` (spec 2.5) | RDK lab, EMOSA in the gateway, 4 October | `forgotten-agent` |
| 16 | a released pod's archived state kept its agent's status: the RDK lab's uplink check never passed after the C adapter released a hold (its last status said the switch timed out), and in a run directory the archive's link would show the next agent's status; fixed (spec 4: archived without the status), the lab's tools skip archives | RDK lab, the build option's first run, 4 October | `fleet-forget` |
| 17 | RDK's native backhaul steering failed in the geometry rooms with EMOSA in the gateway: the agents' interfaces, each with its agent's AL MAC, were the gateway's own, so the controller took a pod's agent for its co-located agent and rooted the backhaul topology at it (no station rooted, no candidate queried); fixed with the agents' interfaces in a namespace of their own (spec 2.1, `EMOSA_NETNS`) | RDK lab rdk-1004, EMOSA in the gateway, 5 October | `agent-netns` |
| 18 | after a redeploy both pods stayed on GRE: each agent switched to the backhaul BSS a room's move had left as its kept target, out of reach where the pods then stood, and the timed-out switch held the pod, which a held pod's Backhaul Steering cannot end; fixed (spec 8.3: a kept target that fails falls back to the configured upstream). A fallback written at once was reverted with the failed switch by the pod's `cm` on the same start (the redeploy after the fix): it now waits for the station to be off the failed credential | RDK lab rdk-1004, 5 October | `backhaul-kept-gone` |
| 19 | an agent never got its recreated pod back: the pod, recreated in place, came back with another AP radio from the lab's pool, and both agents kept the first source's radio capabilities for their whole run, so every refresh of the new source was refused ("pod radio identity or channel changed") and the session stayed `recovering` until the agent was restarted; fixed (spec 2.4: the capabilities are fixed per source, a new database generation takes them again) | RDK lab rdk-1004, 7 October (finding (a)) | `pod-recreated` |
| 20 | pods' clients that could never be steered again: both EMOSA pods held their own windows' client rows (`cs_mode` `away`, `btm_deauth`, `owm` long `expired`) for the eight stations they served, left by closes that had failed; every later mandate saw a row for the station and refused it as another manager's (61 refusals each), the optimizer's steers timed out and the room never converged; fixed (spec 3.7: the sweep of the rows a window left, by the UUIDs the journal recorded) | RDK lab rdk-emosa-1005, 8 October | `steering-leftover` |
| 21 | the agents' fronthaul BSSes did not say they were a Multi-AP agent's: each BSS took its role from its M2 (`0x20` fronthaul, `0x40` backhaul) but only the backhaul's reached the pod (`multi_ap` `backhaul_bss` from the profile); a fronthaul the agent created kept the profile row's unset `multi_ap`, an existing one whatever it had, so the pod's AP did not take the fronthaul role (hostapd `multi_ap` unset: no Multi-AP element to a Multi-AP station); fixed (spec 3.4: every BSS written carries its M2's role in `multi_ap`) | the Pis' qualification under EMOSA, 8 October (`ordinary_ap_role_not_explicit`) | `fronthaul-role`, `fronthaul-role-cold` |
| 22 | a write whose reply was lost could block its pod for good: an `INDETERMINATE` operation ended past its deadline only when the pod's Config lacked the write, so with the write in the Config and a State that never showed it, it never ended, and every later M2's operation was `REJECTED` `BUSY`; fixed (spec 5: past the deadline and not applied, it ends `TIMED_OUT` as a committed write does) | found in review, answering the mv3's Pis' `BUSY` operations after OpenSync restarts (V39), 8 October | `write-lost-unapplied` |
| 23 | pods' stations that could never be steered again after finding 20's fix: the windows that had left their rows in the pods were no longer in the agents' journals (each kept its 16 most recent operations and its latest reconciled one, and the refused mandates filled it), so the sweep, which closes only rows whose UUIDs the journal records, could not take them for its own; the default room never converged (the two pods' OpenSync restarted to clear them). Fixed (spec 3.7, 6): the journal keeps every window holding rows until they are released, and a sweep whose close fails is tried again after 30 s on the same source | RDK lab rdk-emosa-1005, image 16's redeploy, 8 October | `steering-close-refused` |

## Not scenarios

- The data plane (spec 8): the GRE termination and the bridges are the labs', checked by
  their suites and the reference workload.
- The Topology Discovery period (60 s) and Client Disassociation Stats: shown by the
  vectors and the unit tests; no room depends on their timing.
- Several pods under one agent process, TLS on the pod connections: not implemented
  (spec 9).
