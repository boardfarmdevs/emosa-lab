# The target system: OpenSync pods in an EasyMesh network, without the OpenSync cloud

[Architecture index](README.md) · [Specification](../../spec/README.md) ·
[Component design](../../spec/design.md) · [Data plane](data-plane.md)

What EMOSA and the router must do so that unchanged OpenSync pods work as full
members of an EasyMesh network, with no OpenSync cloud; which of it the labs
have shown, which is missing, and whether the design holds as it grows. The
gaps are steps of the alignment plan (easymesh-labs `docs/alignment-plan.md`,
phases 6 and 7).

Status words: **shown** (in the RDK or OpenSync lab, with evidence),
**partial**, **gap** (not built), **decision** (a choice not made yet),
**out of scope** (not EasyMesh's and not EMOSA's).

## 1. The system

```
                 WAN
                  │
  Router (RDK-B gateway; today the bpibroadband container, later a physical BPI or mv3)
  ├─ EasyMesh controller + the gateway's own agent (1905 on brlan0)
  ├─ LAN bridge brlan0: DHCP, DNS, NAT, IPv6
  ├─ EMOSA: fleet (front port), one virtual agent per pod (own AL MAC, own macvlan
  │         on brlan0), GTP (option 2), MQTT broker for the pods' statistics
  └─ fronthaul and backhaul BSSes
       │                 │                      │
   native extenders   OpenSync pods        clients
   (EasyMesh agents)  (unchanged; managed over OVSDB by their EMOSA agent,
                       uplink: GRE over Wi-Fi, Multi-AP Wi-Fi backhaul, or Ethernet)
```

EMOSA takes the OpenSync cloud's place for the pods (spec §1): each pod
connects its OVSDB to EMOSA, and its EMOSA agent represents it to the
controller as one EasyMesh agent. The controller decides; the agent translates
its decisions into the pod's OVSDB and reports the pod back in EasyMesh terms.

Deployment target (decided 29 Sep): the RDK lab's containers (bpibroadband,
extenders, pods, virtual radios and wmediumd) for development and for testing
EMOSA against the optimizer, now and long term; physical BPI or mv3 routers
later. The design must not depend on the container placement (spec/design.md
§2).

## 2. What the OpenSync cloud does for a pod, and who does it here

| Function | With the OpenSync cloud | In this system | Status |
| --- | --- | --- | --- |
| Finding the manager | the pod's redirector, then `manager_addr` from the cloud | the pod's redirector reaches EMOSA's fleet front port; the fleet writes the agent's `manager_addr` | shown with a redirector in the pod image (RDK lab) and with local-noc's redirect (OpenSync lab) |
| Trust | TLS with the operator's CA and the pod's device certificate | TLS on the front port and agent ports with a trust anchor the unchanged pod accepts | **gap** (TCP only in the labs, spec §9); **decision**: where an unchanged field pod's trust comes from (the operator redirecting once, operator-issued certificates for EMOSA, or pod images configured for a local manager). Plan 5.3 |
| Admission and identity | the cloud's inventory | the fleet: admit list by serial, AL MAC from the serial, registry | shown |
| Wi-Fi configuration (SSIDs, security, VIFs) | cloud writes OVSDB | controller's M2 sets, applied by the agent's AP scope as guarded transactions | shown for one 2.4 GHz radio, WPA2-PSK, several BSSes; **gap**: 5 and 6 GHz radios, several radios per pod, WPA3 (spec §9) |
| Channel and power | cloud optimizer | controller's Channel Selection and power limits | **gap**: the agent declines any change (spec §3.4); a real network needs them applied |
| Topology (which parent a pod's uplink uses) | cloud optimizer | controller's Backhaul Steering on option 1 | shown (both labs, across a session renewal); **gap**: backhaul link metrics and a 1905 neighbor on the backhaul, which a controller needs to choose well; pods as parents of other pods (multi-hop) |
| Client steering | cloud band steering | controller's Client Steering, carried out by the pod's owm (BTM) | shown |
| Statistics | the cloud's MQTT and analytics | the router's MQTT broker, the agent's telemetry scope; AP metrics and unassociated station metrics for the controller | shown; metrics complete only where the pod's statistics allow (spec §3.8) |
| Client traffic (data plane) | the gateway's cloud builds the GRE end | option 2: EMOSA's GTP ends the pods' GRE; option 1: Multi-AP backhaul, no GRE; Ethernet: see §3 | options 1 and 2 shown; Ethernet **gap** |
| Recovery | cloud re-applies after reconnects | the pod's own bootstrap restart; the agent's session renewals, journal and re-onboarding | shown (reference workload: six faults, Python and C) |
| Firmware upgrades | cloud | not EasyMesh's | **decision**: out of scope for EMOSA, or a separate router service |
| Troubleshooting, speed tests, parental controls, analytics | cloud product features | not EasyMesh's | out of scope |

## 3. The data plane per uplink

Requirements D1 to D7 (data-plane.md §3) apply to every uplink: clients on the
router's LAN at layer 2 (transparency), management reachable, unchanged pods,
client MTU 1500, self-recovery, underlay isolated from the LAN, honest
representation.

| Uplink | Path | Transparency | Status |
| --- | --- | --- | --- |
| GRE (option 2, baseline) | the pod's 3-address station on a pod-backhaul SSID, gretap to the GTP, bridged into brlan0 | yes: the pod's `br-home` is on the LAN at layer 2 | shown in both labs; the GTP is Python today (`emosa-gtp`), so a router needs it in C or as a small shell/iproute2 service. Software GRE costs router CPU per pod |
| Wireless (option 1) | the pod's station as a Multi-AP backhaul STA (4-address) on a backhaul BSS, bridged into `br-home` | yes, natively | shown in both labs, moved by Backhaul Steering; needs a pod platform that reports the Multi-AP link state (data-plane.md §7). A pod always restarts onto option 2, so the GTP stays on |
| Wired (Ethernet) | the pod's Ethernet uplink on the router's LAN | expected native (OpenSync detects a wired uplink and drops the Wi-Fi one, data-plane.md §4.5) | **gap**: never run with a real Ethernet uplink ("wired" pods in the RDK lab are the GTP path held fixed). Needs: the pod's behaviour on an Ethernet uplink qualified, the loop rule (the GTP must not bridge a pod that is also on Ethernet), and the agent reporting an Ethernet backhaul truthfully |

Honest representation (D7): the agents' 1905 frames always come from EMOSA on
the LAN, not from the pod's radio. With Ethernet and GRE the agent is declared
Ethernet-attached; with option 1 the backhaul station is reported truthfully
but the frames still arrive on Ethernet. Both controllers accept this in the
labs; prplMesh keeps the link type Ethernet. Recorded as a known limitation.

## 4. Scale

| Resource | Per pod | Bound or finding |
| --- | --- | --- |
| Agent process | one (design §2: isolated, restarted on failure) | C about 12 MB resident, Python about 58 MB (reference workload, 29 Sep). A router with few pods fits; many pods on a small router need the later optimization below |
| Ports | one OVSDB port | the fleet's port range (lab: 40 agents) |
| LAN interfaces | one macvlan on brlan0 with the agent's AL MAC | none found; each agent drops foreign 1905 frames before any processing (design §3.3) |
| 1905 load on the controller | one agent per pod, like a native extender | the controller sees as many agents as pods plus extenders |
| GRE (option 2) | one gretap on the router, software encapsulation | router CPU per pod's traffic; option 1 has none |
| MQTT | one topic per pod | one broker |

**Memory, later.** One adapter process hosting every pod's agent (one AL MAC
and one state per pod, as now) would save the per-process cost. It is a later
optimization, not a change of the model: the pods still appear as one agent
each. To keep it possible, the C code keeps all of an agent's state in its
agent context, with no global or function-static state (plan phase 6).

## 5. Is the design sound

For what it covers, yes: the control plane (onboarding, configuration,
steering, metrics, backhaul steering) and the GRE and Multi-AP data planes run
unchanged pods under both RDK's and prplMesh's controllers, recover from
faults, and are identical in Python and C (vectors, reference workload, RDK
room suite).

To be a full OpenSync-supporting EasyMesh system without the cloud it still
needs, in the order they matter:
1. **Trust without the cloud**: TLS on the OVSDB ports and a trust anchor an
   unchanged pod accepts (plan 5.3). Without it, only pods whose images point
   at EMOSA over TCP can join.
2. **Every radio of the pod**: 5 and 6 GHz fronthaul, several radios per
   agent, WPA3.
3. **Channel and power applied**, not declined.
4. **Ethernet-attached pods**, qualified.
5. **Backhaul metrics and neighbors**, so the controller can choose parents;
   pods as parents (multi-hop).
6. **The router side**: EMOSA next to the gateway's own 1905 stack on brlan0
   (plan 5.1); the GTP and the fleet are in C (plan 8.3: no Python on a router),
   and the secret store has an interface for the platform's secure storage when the
   router has one.
7. **Memory** on small routers (§4).
