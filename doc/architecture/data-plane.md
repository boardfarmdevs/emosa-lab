# The data plane: carrying OpenSync pods' traffic in an EasyMesh network

[Architecture index](README.md) · [Specification](../../spec/README.md)

EMOSA puts a pod's **control** under an EasyMesh controller. A pod is only
useful if its clients' **traffic** also reaches the gateway's LAN, and that
takes more than the control plane. This document covers:
- how the data plane works today (§2);
- what an RDK or prpl integration must provide (§3);
- the two ways to provide it: terminate the OpenSync GRE next to the EasyMesh
  gateway (§4, always available), or switch the pod's uplink to an EasyMesh
  backhaul (§5, optional);
- their performance and compatibility (§4, §5, §6);
- whether OpenSync 6.6 supports option 1 (§7).

**Decision.** EMOSA always provides option 2, the GRE termination point. It
works with an unchanged pod and an unchanged EasyMesh gateway. Option 1 is an
optional mode for pods and platforms that qualify for it (§7). A pod can be
moved between the two, and a failed switch to option 1 falls back to option 2.

## 1. Terms

| Term | Meaning |
| --- | --- |
| Underlay | The pod's Wi-Fi uplink: its backhaul station (`bhaul-sta-*`) associated to an access point, with its own IP address |
| Overlay | The client LAN carried over the underlay: the pod's `br-home` bridged into the gateway's LAN |
| gretap | An Ethernet-over-GRE tunnel (Linux `gretap`, OpenSync `if_type=gre`) |
| GTP | GRE termination point: the far end of every pod's gretap, bridged into the gateway LAN |
| 3-address / 4-address station | A normal Wi-Fi client (it can only send frames from its own MAC), versus a WDS/Multi-AP backhaul station that can forward other devices' frames |

## 2. How the data plane works today (opensync-lab)

Everything below was read from the running lab (VM `emosa-osl-0923`, six pods).
All of it is built and owned by OpenSync and opensync-lab's `local-noc`, never
by EMOSA.

```
 Wi-Fi client ─2.4 GHz─▶ pod: home-ap-24 ─┐
                                          br-home (Linux bridge, 192.168.0.229/24 by DHCP)
                                          └─ g-bhaul-sta-50  gretap 169.254.1.50 → 169.254.1.1, MTU 1500
                                               │  over bhaul-sta-50: 3-address STA, 5 GHz ch 44,
                                               │  169.254.1.50/25 by DHCP, MTU 1600
 ════════════════════ Wi-Fi backhaul "opensync-lab-bhaul" ════════════════════
                                               │
 mv3 (RDK-B + OpenSync): wl1.1 backhaul AP, 169.254.1.1/25 static, MTU 1600, DHCP .10–.126 (option 26 = 1600)
                                          ┌─ pgd1_50  gretap 169.254.1.1 → 169.254.1.50, MTU 1562
                          brlan0 (OVS) ───┤  pgd1_62, pgd1_103, … (one per pod), eth1–eth4
                                          └─ 192.168.0.1: DHCP, DNS, NAT ─▶ WAN
```

### 2.1 Who builds what, and when

1. **The pod joins the backhaul.**
   - The pod's `bhaul-sta-50` joins the gateway's backhaul AP `wl1.1`
     (`opensync-lab-bhaul`, WPA2-PSK) with the credentials in its own
     `Wifi_Credential_Config`.
   - It is a plain 3-address station (`Wifi_VIF_Config`: `mode=sta`,
     `multi_ap=none`, `wds=false`, no bridge).
   - It leases `169.254.1.x/25` from the gateway's DHCP server on `wl1.1`,
     which also sets its MTU to 1600 (option 26).
2. **The pod's `cm` builds its end of the tunnel** (`cm2_bh_gre.c`, OpenSync
   6.6.1.0).
   - It inserts a `Wifi_Inet_Config` row `g-bhaul-sta-50`: `if_type=gre`, MTU
     `CONFIG_CM2_MTU_ON_GRE` = 1500, `gre_ifname=bhaul-sta-50`.
   - The local address is the station's lease. The **remote is always the
     first host address of that subnet**: `(inet_addr & netmask) | 1`, here
     `169.254.1.1`. It is neither the DHCP router option nor the DHCP server.
   - `cm` builds the tunnel **only if that lease is link-local
     (`169.254.0.0/16`)**. With any other address it logs a warning and builds
     nothing (`cm2_bh_gre_vif_derive_enable`).
   - OpenSync creates the gretap, and `cm` adds it to `br-home` as the uplink.
3. **The gateway's cloud builds the other end.** local-noc is mv3's cloud,
   and `local-noc/mesh.py` runs a reconcile loop over mv3's OVSDB. Once a pod
   is associated to `wl1.1` and holds a lease (`DHCP_leased_IP` joined with
   `Wifi_Associated_Clients`), local-noc:
   - inserts `Wifi_Inet_Config` `pgd<b3>_<b4>` (`if_type=gre`, MTU 1562,
     local `169.254.1.1`, remote the pod's lease);
   - adds it as an OVS `Interface`/`Port` of `brlan0`.
4. **The overlay comes up.** `br-home` now reaches `brlan0` at layer 2:
   - the pod gets `192.168.0.x` from mv3's dnsmasq across the tunnel, plus
     IPv6 from mv3's router advertisements;
   - Wi-Fi clients on `home-ap-24` are bridged into mv3's LAN, and get their
     DHCP, DNS and default route from mv3.
5. **Management rides the overlay.** The pod's default route is via `br-home`.
   Its OVSDB connection to its manager (local-noc, and after handover EMOSA at
   `10.101.0.1:665x`) runs inside the tunnel, through mv3's NAT. **If the
   tunnel is down, the pod cannot reach EMOSA.**

### 2.2 MTU

| Hop | MTU | Set by |
| --- | --- | --- |
| Client ↔ `home-ap-24`, `br-home`, `g-bhaul-sta-50` | 1500 | pod defaults; `CONFIG_CM2_MTU_ON_GRE` |
| gretap encapsulation | +38 bytes: outer IPv4 20, GRE 4, inner Ethernet 14 | |
| `bhaul-sta-50` ↔ `wl1.1` (underlay) | 1600 | DHCP option 26 on the gateway; `Wifi_Inet_Config.mtu` |
| `pgd1_*` on the gateway | 1562 | local-noc |

A full 1500-byte client frame becomes a 1538-byte underlay packet, which fits
in 1600. Nothing is fragmented, and clients keep a standard 1500 MTU. This
depends on the backhaul Wi-Fi link carrying frames above 1500 bytes.

### 2.3 What EMOSA does and does not touch

- **Changed by the handover:** only the pod's `AWLAN_Node.manager_addr`.
  local-noc keeps managing mv3, including every `pgd` tunnel, and stops writing
  the pod's fronthaul.
- **Written by EMOSA:** the fronthaul VIF (`home-ap-24`) and, with multi-BSS,
  the slot VIFs, all in the bridge the pod profile names (`br-home`).
- **Never touched:** the backhaul station, the GRE and the gateway.
- **Reported to the controller:** the pod's agent is declared Ethernet-attached,
  with the controller as its 1905 neighbour. The Wi-Fi uplink and the tunnel
  are not reported.

### 2.4 Observed behaviour

- **Clients** get mv3's DHCP and reach the internet (the client check reports
  "served across the GRE backhaul").
- **Backhaul loss:** taking pod-3's `bhaul-sta-50` down for 30 s cut its two
  clients off for 48 to 49 s. The underlay reassociates, the lease returns,
  both tunnel ends rebuild, and the recovery took 25.9 s after the link came
  back (runs fleet-m7-01 and fleet-m7-03). EMOSA was not involved.
- **Radio:** the backhaul runs at 20 MHz without HT on a single shared hwsim
  medium, so lab throughput says nothing about real radios (§4.4, §5.4).

## 3. What an RDK or prpl integration must provide

In the RDK and prpl labs, the gateway is an EasyMesh controller plus agent
(RDK unified-wifi-mesh, or prplMesh on prplOS). It has fronthaul BSSes for
clients, and Multi-AP backhaul BSSes that accept only Multi-AP (4-address)
backhaul stations. **Nothing on it terminates OpenSync GRE, and nothing adds
tunnels to its LAN bridge.** Whichever option is used, the data plane MUST
provide:

| # | Requirement |
| --- | --- |
| D1 | **Transparency:** a pod's clients are on the gateway's LAN at layer 2. They get the gateway's DHCP, IPv6 RAs, DNS, broadcast and multicast, exactly like the gateway's own clients. |
| D2 | **Management reachability:** the pod reaches EMOSA's front port and agent port over the data plane it gets, and keeps reaching it. |
| D3 | **Unchanged pods:** no pod firmware change. Configuration only through the pod's OVSDB, and only as specified. |
| D4 | **Client MTU 1500,** with no fragmentation in normal operation. |
| D5 | **Recovery:** after a backhaul loss, reboot or gateway restart, the path rebuilds by itself. A failed change never strands a pod out of reach. |
| D6 | **Isolation:** the pods' underlay does not share a subnet with the client LAN (§4.5) and is not a way into the LAN for other stations. |
| D7 | **Honest representation:** what the controller is told about the pod's uplink is either true or declared as a simplification. |

## 4. Option 2: terminate the OpenSync GRE next to the EasyMesh gateway (baseline)

The pod keeps exactly the data plane it has today. What changes is who plays
mv3's part: an EMOSA **GRE termination point (GTP)** provides the `.1` end of
the underlay, one gretap per pod, and a bridge port into the EasyMesh
gateway's LAN.

```
 client ─▶ pod home-ap-24 ─ br-home ─ g-bhaul-sta-50 (gretap → 169.254.1.1)   [unchanged pod]
                                          │ bhaul-sta-50: 3-address STA, 169.254.1.x/25, MTU 1600
 ═══════════ pod-backhaul SSID on the EasyMesh gateway (fronthaul-type BSS) ═══════════
                                          │ own L2 segment: VLAN or bridge "podbh"
 GTP: 169.254.1.1/25 on podbh, DHCP .10–.126 (option 26 = 1600)
      pgd-<pod> gretap 169.254.1.1 → pod, one per pod ── br-gtp ── port on the gateway LAN
                                                                 │
 EasyMesh gateway (RDK / prpl): LAN bridge ── DHCP, DNS, NAT ─▶ WAN
```

### 4.1 The pieces

1. **The pod-backhaul SSID.** The gateway needs one access point the pods'
   3-address backhaul stations can join. A Multi-AP backhaul BSS cannot serve,
   because it rejects stations without the Multi-AP element. It is a
   **fronthaul-type BSS with its own SSID**, configured the way the
   controller configures any additional SSID:
   - prplMesh: an extra credential set in its policy;
   - RDK: one of its extra haul types, for example the IoT SSID.

   The simplest setup reuses the SSID and passphrase the pods already have in
   their `Wifi_Credential_Config` (in the lab, `opensync-lab-bhaul`), so the
   pods need no change at all.
2. **Its own L2 segment.** Traffic on that SSID MUST land on a segment
   separate from the client LAN (D6). EasyMesh R2 Traffic Separation maps an
   SSID to an 802.1Q VLAN; otherwise the gateway maps the SSID to its own
   bridge (`podbh`) locally.
3. **The GTP.** A small EMOSA component with one leg on `podbh` and one on the
   gateway's LAN.
   - On `podbh` it is `169.254.1.1/25`, the address every pod's `cm` will use
     as its tunnel remote (§2.1). It serves DHCP for `.10` to `.126` with
     option 26 set to 1600.
   - The underlay MUST be link-local (`169.254.0.0/16`). OpenSync 6.6 builds
     no tunnel over any other address (§2.1).
   - For each lease held by an associated pod, it creates `pgd-<pod>`, a
     gretap from `.1` to the pod with MTU 1562, and puts it in `br-gtp`.
   - `br-gtp` has one port on the gateway's LAN (a veth, VLAN or NIC).

   This is the job `local-noc/mesh.py` does on mv3 today, done locally.
4. **Placement.** The GTP runs either on the gateway itself (**2a**: a hook
   that adds the `podbh` leg and the gretaps to the gateway's LAN bridge
   directly) or next to it (**2b**: a container or host with a port on
   `podbh` and a port on the LAN, like EMOSA's trunk). 2b needs nothing
   installed on the gateway; 2a saves a LAN hop (§4.4).

### 4.2 Flows

**A pod joins.**
1. Its `bhaul-sta` joins the pod-backhaul SSID and leases `169.254.1.x` from
   the GTP.
2. Its `cm` builds `g-bhaul-sta-*` to `.1`.
3. The GTP sees the lease and the association and builds `pgd-<pod>`.
4. `br-home` joins the LAN, and the pod leases its LAN address from the
   gateway.
5. The pod's `cm` reaches its redirector over the LAN and is handed to EMOSA's
   fleet as today.

**A client joins the pod.** It associates to the pod's fronthaul, which EMOSA
configured from the controller's M2. It is bridged through `br-home`, the
tunnel and `br-gtp` into the gateway LAN, and gets the gateway's DHCP.

**The pod or its backhaul restarts.** The lease and association drop, and the
GTP removes `pgd-<pod>`. The pod's `cm` rebuilds its end when the station is
back, and the GTP rebuilds its end on the new lease (D5).

**The gateway or the controller restarts.** The pod-backhaul SSID returns with
the gateway configuration, and the tunnels rebuild as above. Configuration is
unaffected, because EMOSA's agents re-onboard over 1905 as they do today.

### 4.3 Who configures what

| Party | Configures |
| --- | --- |
| EasyMesh controller or gateway | the pod-backhaul SSID, its VLAN or bridge (`podbh`), and a LAN port for the GTP. Also: exclude the pods' backhaul stations from client steering (§4.5). |
| GTP (EMOSA) | the `podbh` address `.1`, DHCP with option 26, one gretap per pod, and `br-gtp` bridged to the LAN |
| Pod | nothing new: its backhaul credentials already name the pod-backhaul SSID. Otherwise EMOSA writes them once (§4.5). |
| EMOSA agent | nothing on the data plane. Optionally it reports the tunnel state it sees in the pod's `Wifi_Inet_State`. |

### 4.4 Performance

| Aspect | Effect |
| --- | --- |
| Encapsulation | 38 bytes per frame, about 2.5% at 1500 bytes and more for small packets |
| MTU | Needs an underlay MTU of at least 1538 on the pod-backhaul SSID. With 1600, as OpenSync sets it, nothing fragments. If the gateway's Wi-Fi interface stays at 1500, every full-size client frame is split into two outer IPv4 fragments. That roughly doubles packets per second on the backhaul and costs CPU at both ends. The fallback, lowering the client MTU to 1462 over DHCP, affects the whole LAN. So the MTU is a hard requirement of this option (§4.5). |
| CPU | GRE is handled in software on both ends. On the pod, some OpenSync platforms offload GRE and many do not. The GTP is a small load on an x86 host. |
| Airtime | The same as today: one hop, pod ↔ gateway. A 3-address station with GRE uses about as much airtime as a 4-address station, apart from the 38 bytes. |
| 2b hairpin | With the GTP next to the gateway, internet-bound client traffic crosses the gateway's wired LAN twice (gateway ↔ GTP). That is negligible on 1 Gb/s Ethernet compared with a Wi-Fi backhaul, and zero in 2a. Pod-to-pod client traffic takes the same path as through mv3 today. |
| Broadcast and multicast | Every LAN broadcast is copied once per tunnel and sent as a unicast over the air, so the cost grows with the number of pods. This is the same as today, and about the same as 4-address multicast-to-unicast in option 1. |
| Multiple hops | OpenSync chains tunnels through a pod's own backhaul AP. The pod-backhaul SSID exists only on the gateway, so this design is single-hop. More hops need the first pod to offer the pod-backhaul SSID too, which is OpenSync behaviour that EMOSA does not configure yet. |

### 4.5 Compatibility

| Concern | Consequence, and how it is handled |
| --- | --- |
| Backhaul BSS rejects 3-address stations | The pod-backhaul SSID must be a fronthaul-type BSS. Both prplMesh and RDK can add SSIDs. |
| Underlay and client LAN on one subnet | The pod would have `bhaul-sta` and `br-home` in the same subnet, and the route to `.1` would be ambiguous. **Not allowed** (D6). A separate segment and subnet are required, either through Traffic Separation or a local bridge on the gateway. |
| The `.1` and link-local rules | The underlay MUST be a `169.254.0.0/16` subnet, and the GTP MUST be its `.1`. This is what OpenSync 6.6 `cm` requires (§2.1). A gateway that uses `.1` on that segment for itself can only do 2a. |
| Controller treats pod stations as clients | The gateway's own agent reports each pod's `bhaul-sta` as an associated client of the pod-backhaul SSID. The controller may try to steer it (BTM or disassociation) or show it as a client. The operator MUST add those MACs to the Multi-AP Policy's steering-disallowed lists. EMOSA knows them from each pod's `Wifi_VIF_State` and lists them. |
| Pod backhaul credentials | A pod joins only SSIDs in its `Wifi_Credential_Config`. Either the pod-backhaul SSID matches them, or EMOSA writes one entry, which is a change to the pod's uplink configuration and done once. |
| MTU above 1500 on the gateway's BSS | Most Linux Wi-Fi drivers accept up to 2304-byte frames, but the gateway's image may pin 1500. It must be checked per gateway. If it can't be raised, see §4.4. |
| Loops | A pod that also has Ethernet into the LAN would bridge the LAN into itself. OpenSync detects wired backhaul and stops the Wi-Fi one; the GTP must not bridge a pod that appears on both. |
| IPv6, DHCP options, multicast | Transparent at L2, the same as today. |
| RDK and prpl builds | 2b needs nothing on the gateway beyond the SSID and segment. 2a needs `ip_gre` and a hook into the gateway's LAN bridge (Linux bridge or OVS), and RDK-B and prplOS may differ. |

### 4.6 What the controller sees

- The agent stays **declared Ethernet-attached** to EMOSA, which is true of
  the control plane and a stated simplification of the data plane.
- The pod's backhaul station appears as an ordinary client of the gateway,
  which is true.
- EMOSA MUST NOT claim a Wi-Fi backhaul link to the controller in this option.

## 5. Option 1: switch the pod's uplink to an EasyMesh backhaul (optional)

The pod joins the EasyMesh gateway the way an EasyMesh extender does. Its
backhaul station becomes a Multi-AP 4-address backhaul station on the
controller's backhaul BSS and is bridged straight into `br-home`. There is no
GRE, no GTP and no pod-backhaul SSID.

```
 client ─▶ pod home-ap-24 ─ br-home ─ bhaul-sta-50 (4-address, Multi-AP backhaul STA)
 ═══════════ controller's backhaul BSS (multi_ap=backhaul, 4-address) ═══════════
 EasyMesh gateway: per-station 4-address interface in its LAN bridge ── DHCP, DNS, NAT ─▶ WAN
```

### 5.1 How it works

1. **Credentials.** EMOSA takes the backhaul SSID and passphrase from the
   controller.
   - In multi-BSS mode the controller's M2 set includes the backhaul BSS
     (flag `0x40`). Its SSID and key are the network's backhaul credentials.
   - Otherwise they come from the fleet configuration.
2. **Uplink change.** EMOSA writes the pod's backhaul station through OVSDB,
   as one guarded operation like any other (§5.3). OpenSync 6.6 expresses this
   natively as a credential with `onboard_type=multi_ap` (§7):
   - add a `Wifi_Credential_Config` entry (the backhaul SSID and passphrase,
     `onboard_type=multi_ap`, a higher priority than the existing `gre`
     entry), and link it from the station's `credential_configs`;
   - clear the station's own `Wifi_VIF_Config.ssid`, so that `owm` builds
     `wpa_supplicant` networks from the credential list instead.

   `wpa_supplicant` gets the Multi-AP entry as a network with
   `multi_ap_backhaul_sta=1`, bridged into the LAN bridge (`br-home`). The
   `gre` entry stays as a lower-priority fallback.
3. **Link-up.** The station associates with the Multi-AP element (backhaul
   STA). The gateway's hostapd creates a 4-address per-station interface in
   its LAN bridge.
4. **No tunnel.** `cm` treats the station as a WDS station: `multi_ap=backhaul_sta`
   in its State. It runs no DHCP on it, builds no `g-bhaul-sta-*`, and registers
   the station itself as the bridged uplink. This is confirmed in the source
   (§7).
5. **Addressing.** The pod's `br-home` leases from the gateway LAN directly,
   and its management reaches EMOSA over the LAN.
6. **What the controller is told** (D7). EMOSA can now report the uplink
   truthfully:
   - the backhaul station as an 802.11 interface with the controller's
     backhaul BSS as its neighbour;
   - Backhaul STA Radio Capabilities.

   It MUST handle, or explicitly refuse, Backhaul Steering Requests.

   The 1905 frames still come from EMOSA's port on the LAN, not from the pod's
   radio. A controller that checks which interface a neighbour's frames arrive
   on may see an inconsistency (§5.5).

### 5.2 What each party configures

| Party | Configures |
| --- | --- |
| EasyMesh controller or gateway | nothing beyond a normal backhaul BSS |
| EMOSA agent | the uplink switch (credentials, Multi-AP backhaul STA, 4-address, bridge), keeping the option-2 credentials as fallback, and the truthful backhaul report |
| GTP | none for this pod. It remains in place for the fallback. |

### 5.3 Switching safely

Switching the uplink moves the path EMOSA itself uses to reach the pod. A bad
write strands the pod, and EMOSA cannot undo it from outside. So the switch:

1. starts only from a working option-2 path;
2. first confirms that the gateway's backhaul BSS is up and advertising
   Multi-AP. The gateway's own agent reports it, so EMOSA can check this in the
   controller's configuration, or the operator states it;
3. writes, in one guarded transaction:
   - the `multi_ap` credential, at a higher priority than the existing `gre`
     credential, which stays;
   - the station's cleared `ssid` (§5.1);
4. waits for the pod to come back on its agent port with the new uplink in its
   State. That means `bhaul-sta` associated with `multi_ap=backhaul_sta` and
   `wds=true` in `Wifi_VIF_State`, bridged into `br-home`, and no
   `g-bhaul-sta-*`;
5. counts as applied only then (the operation lifecycle, spec §5).

If the Multi-AP network cannot be associated, `wpa_supplicant` falls back to
the lower-priority `gre` credential by itself, and the pod returns over
option 2 (§7, Q5). EMOSA then records the operation as `TIMED_OUT`, removes
the `multi_ap` credential, marks the pod option-2-only, and never retries on
its own.

**Remaining risk:** the Multi-AP link associates but carries no data, for
example because the gateway does not bridge the 4-address station. That does
not trigger `wpa_supplicant`'s fallback. It must be excluded by the live test
(§7, Q4) and by step 2 before any pod is switched.

### 5.4 Performance

| Aspect | Effect |
| --- | --- |
| Encapsulation | none. Native 1500 MTU, and no dependency on underlay MTU above 1500. |
| CPU | 4-address bridging is the standard Linux Wi-Fi path, and hardware offload is possible on platforms that support it |
| Airtime | the same single hop. Slightly better than option 2 through A-MSDU aggregation and no GRE bytes. |
| Broadcast | mac80211 serves 4-address stations through per-station interfaces, so broadcasts become unicast per station, as in option 2 |
| Backhaul management | the controller can see and optimise the backhaul: channel choice, backhaul steering, link metrics. Only if EMOSA translates those procedures (§5.5). |
| Multiple hops | native EasyMesh: a pod could offer a backhaul BSS (EMOSA's multi-BSS `b-ap-24`) to another pod's backhaul station |

### 5.5 Compatibility

| Concern | Consequence, and how it is handled |
| --- | --- |
| OpenSync 6.6 support | In the source: the data model, `owm`/osw, `wpa_supplicant` and `cm` all support a Multi-AP backhaul station bridged into `br-home` without GRE (§7). Not yet shown live. |
| Driver and platform | The pod's Wi-Fi driver and `wpa_supplicant` must support a 4-address STA with the Multi-AP element (`multi_ap_backhaul_sta`), and vendor platforms differ. mac80211 and `mac80211_hwsim` support 4-address. |
| Gateway backhaul BSS | prplMesh and RDK both run standard hostapd Multi-AP backhaul BSSes. Profile-2 backhaul BSSes may expect a Multi-AP Profile subelement and an R2 primary VLAN (Traffic Separation). The station must match. |
| 1905 source interface | The agent's 1905 frames come from EMOSA on the LAN while the pod's link is on the backhaul BSS. prplMesh and RDK tolerance of that is unknown. It must be tested; the fallback is to keep reporting the declared Ethernet attachment. |
| Controller-driven backhaul procedures | Backhaul Steering, backhaul link metrics and Backhaul STA capability queries need new translations to OVSDB, or explicit refusals |
| Loss of OpenSync's own backhaul logic | Where OpenSync's `cm` and `wano` expect GRE, some of their fallback and health checks change behaviour. See §7. |
| Risk | Management depends on the new link. That is why the switch is staged (§5.3) and always falls back to option 2. |

## 6. Comparison

| | Option 2: GRE + GTP (baseline) | Option 1: EasyMesh backhaul (optional) |
| --- | --- | --- |
| Pod changes | none, if the pod-backhaul SSID matches its credentials | uplink reconfiguration through OVSDB |
| Gateway changes | a pod-backhaul SSID on its own segment, and a GTP (on or next to it) | none |
| Works with unchanged OpenSync 6.6 | yes: today's path, with the GTP in place of local-noc | yes in the source, as native Multi-AP onboarding. Live test pending (§7). |
| Client MTU | 1500, if the underlay allows at least 1538 | 1500 native |
| Overhead | 38 bytes per frame, software GRE | none |
| Controller's view of the uplink | declared (Ethernet), plus the pod's station as a client | truthful Wi-Fi backhaul, once translated |
| Backhaul optimisation by the controller | none | possible |
| Failure mode | tunnel rebuilds automatically | a staged switch with fallback to option 2 |
| Multiple hops | not yet | native |

## 7. OpenSync 6.6 support for option 1

Investigated in the OpenSync 6.6.1.0 source the lab pods are built from
(`mvx-pod-work/core`, release `1dad64e9`, platform `cfg80211`) and on a live
pod, without changing it.

**Verdict:** OpenSync 6.6.1.0 supports a Multi-AP backhaul station natively,
under the name "Multi-AP onboarding". This holds for the data model, the Wi-Fi
stack and the connection manager. Two things remain before a pod is switched:
- a live test in the lab (Q4);
- a check that a Multi-AP link that associates but carries no data is caught
  (Q5).

| # | Question | Answer | Evidence |
| --- | --- | --- | --- |
| Q1 | Can OVSDB express it? | **Yes.** | `Wifi_VIF_Config` and `Wifi_VIF_State` have `multi_ap` (`none`, `backhaul_sta`, `backhaul_bss`, `fronthaul_bss`, `fronthaul_backhaul_bss`), `wds`, `parent` and `bridge`. `Wifi_Credential_Config.onboard_type` is `gre` or `multi_ap` (`schema_consts.h`). |
| Q2 | Do `owm`/osw and the platform implement it? | **Yes, in configuration.** | `owm` maps `multi_ap=backhaul_sta`, or a `multi_ap` credential, to a station network with `multi_ap=true` and bridge `br-home` (`ow_ovsdb.c`, `ow_ovsdb_cconf.c`). osw writes `multi_ap_backhaul_sta=1` into `wpa_supplicant`'s network block (`osw_wpas_conf.c`) and passes the bridge to `wpa_supplicant`. After a Multi-AP backhaul association, `wpa_supplicant` itself switches the interface to 4-address mode. State reports `wds=true` and `multi_ap=backhaul_sta` while such a link is up. The pod's `wpa_supplicant` (v2.11-devel) contains `multi_ap_backhaul_sta`, and its hwsim radios support `AP/VLAN`. |
| Q3 | Does `cm` skip the GRE and bridge the station? | **Yes.** | `cm2_util_is_wds_station` treats a station with `multi_ap=backhaul_sta` in State as a WDS station. It runs no DHCP on it and makes the station itself the bridged uplink in `Connection_Manager_Uplink` (`cm2_ovsdb.c`). `cm2_bh_gre` builds a tunnel only for a station with a link-local address, and a WDS station has none. |
| Q4 | Does it work end to end in the lab? | **Not tested.** | The prplMesh gateway (`em-ctl`) has a backhaul BSS, `wlan0.0`, whose Multi-AP role is set at runtime. The test moves one pod's uplink away from the path EMOSA uses to reach it, so it needs a pod that may be lost, and an agreed recovery (reset the pod container). |
| Q5 | Is there a fallback? | **Partly.** | With the station's `ssid` empty, `owm` builds one `wpa_supplicant` network per enabled credential, with its priority. `wpa_supplicant` falls back when the preferred network cannot be associated. Today's pods set `ssid` on the VIF (a single network, no fallback), so the switch must move them to the credential list (§5.1). A link that associates but carries no data is not covered. |

## 8. Next steps

1. **GTP (option 2) in the lab.**
   - Build `emosa-gtp`: DHCP on the underlay, one gretap per lease, and a
     bridge into a LAN.
   - Move one pod from mv3's backhaul to a pod-backhaul SSID on the prplMesh
     gateway (`em-ctl`), terminated by the GTP.
   - Run the fault workload against it.
2. **Spec.** The normative data plane section (spec §8) states D1 to D7 and the
   GTP's contract: `.1`, DHCP option 26, one gretap per lease, and MTU 1562 on
   the GTP side.
3. **Option 1.** The source says OpenSync 6.6 supports it (§7). The next step
   is the live test (Q4) on one expendable pod against the prplMesh backhaul
   BSS. That covers the association, 4-address mode, bridging into `br-home`,
   the absence of GRE, and falling back to the `gre` credential. Then comes
   the staged switch (§5.3) as an EMOSA operation.
