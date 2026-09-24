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

### 2.2 OpenSync's safety net: stability checks and the bootstrap restart

The pod's `cm` checks its uplink (`cm2_stability.c`, `target_kconfig.c`):
- every 40 s when healthy (`CM2_STABILITY_INTERVAL`), and every few seconds
  while failing;
- **link check:** for a GRE uplink, `cm` pings the tunnel's remote address
  (the gateway end, `.1`) through the backhaul station. There is no ARP
  fallback: an end that doesn't answer ICMP echo is a dead link;
- **router check:** `cm` pings the default router through `br-home`, and falls
  back to arping.

After 8 consecutive router failures (`CM2_STABILITY_THRESH_FATAL=8`) `cm`
logs "Restart managers due to exceeding the threshold for fatal failures" and
restarts OpenSync. The database is rebuilt from the pod's bootstrap, so every
cloud-written change is gone, and the pod rejoins its bootstrap uplink with
its built-in credentials. The lab saw this three times (§4.7, §7).

**This restart is the pod's real safety net.** Whatever an uplink change does,
a pod that loses its router for roughly half a minute returns to its bootstrap
path by itself. It then reconnects to its cloud, and through the redirector to
EMOSA's fleet.

### 2.3 MTU

| Hop | MTU | Set by |
| --- | --- | --- |
| Client ↔ `home-ap-24`, `br-home`, `g-bhaul-sta-50` | 1500 | pod defaults; `CONFIG_CM2_MTU_ON_GRE` |
| gretap encapsulation | +38 bytes: outer IPv4 20, GRE 4, inner Ethernet 14 | |
| `bhaul-sta-50` ↔ `wl1.1` (underlay) | 1600 | DHCP option 26 on the gateway; `Wifi_Inet_Config.mtu` |
| `pgd1_*` on the gateway | 1562 | local-noc |

A full 1500-byte client frame becomes a 1538-byte underlay packet, which fits
in 1600. Nothing is fragmented, and clients keep a standard 1500 MTU. This
depends on the backhaul Wi-Fi link carrying frames above 1500 bytes.

### 2.4 What EMOSA does and does not touch

- **Changed by the handover:** only the pod's `AWLAN_Node.manager_addr`.
  local-noc keeps managing mv3, including every `pgd` tunnel, and stops writing
  the pod's fronthaul.
- **Written by EMOSA:** the fronthaul VIF (`home-ap-24`) and, with multi-BSS,
  the slot VIFs, all in the bridge the pod profile names (`br-home`).
- **Never touched:** the backhaul station, the GRE and the gateway.
- **Reported to the controller:** the pod's agent is declared Ethernet-attached,
  with the controller as its 1905 neighbour. The Wi-Fi uplink and the tunnel
  are not reported.

### 2.5 Observed behaviour

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
| ICMP | The GTP MUST answer ICMP echo on `.1`: `cm`'s link check pings it without an ARP fallback (§2.2). The gateway LAN's router MUST answer ICMP echo or ARP from pods (router check). |
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

### 4.7 In the lab

`lab.sh gtp` builds `em-gtp`, standing in for the EasyMesh gateway side:
- a hwsim radio serves the pod-backhaul SSID `emosa-podbh` (2.4 GHz channel 6)
  into the underlay bridge `podbh`;
- the GTP from the adapter kit (`emosa-gtp`) holds `169.254.2.1/25`;
- `br-gtp` has a LAN leg on mv3's LAN port bridge (`lan-p4`), which stands in
  for the EasyMesh gateway's LAN.

`lab.sh uplink POD gtp` moves a pod's uplink to its 2.4 GHz backhaul station
in credential-list mode.

**Results:**
- **Switch:** pod-3 moved from mv3's GRE to the GTP with OpenSync's
  protection on. Within 30 s it leased `169.254.2.53`, its `cm` built the
  GRE to `.1`, and the GTP built `gtp2_53` into `br-gtp`. `br-home` kept its
  LAN address, the fronthaul and both clients stayed up, and OpenSync did not
  restart.
- **Fault workload** `gtp-m7-01` (900 s, pod-3 on the GTP): passed, every check
  true. The backhaul-loss fault took down `bhaul-sta-24` and recovered 25.7 s
  after the link returned, against 25.9 s on mv3's path.

  | Fault | Recovered after |
  | --- | --- |
  | client leave/join em-wc2 | 14.7 s |
  | adapter restart (pod-1) | 4.7 s |
  | OVSDB transport cut 20 s (pod-2) | 9.6 s |
  | backhaul loss 30 s (pod-3, via the GTP) | 25.7 s |
  | controller restart + policy re-entry | 1.9 s |
  | client leave/join em-wc4 | 14.5 s |

**Found and fixed on the way:**
- **Lab: VLAN filtering.** The `lan-p*` bridges filter VLANs, and mv3's port
  is an untagged access port in its own VLAN, so the GTP's LAN leg must join
  that VLAN. Until it did, even ARP failed. `cm` then restarted pod-3 to its
  bootstrap uplink, which is the safety net of §2.2 working as designed.
- **GTP: tunnels pinned to a device.** hostapd deletes and recreates a bridge
  it created itself whenever it restarts. Tunnels pinned to that bridge then
  pointed at a vanished device. Now the GTP routes tunnels by address and
  rebuilds any tunnel pinned elsewhere, and the lab creates both bridges before
  hostapd starts.
- **EMOSA: pods whose database is rebuilt.** An agent refused a pod after
  OpenSync restarted on it (new row UUIDs, same serial and radio), until the
  agent itself was restarted. The agent no longer compares with an earlier
  exchange's anchor. After OpenSync restarts on the pod, the same agent
  process now re-onboards it (verified twice, in about 2 minutes).

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
   as one guarded operation like any other (§5.3): the controller's backhaul
   SSID and passphrase with `multi_ap=backhaul_sta`. This can be done either
   on the station's `Wifi_VIF_Config`, or as a sole `Wifi_Credential_Config`
   entry with `onboard_type=multi_ap`.

   `wpa_supplicant` gets a network with `multi_ap_backhaul_sta=1`, bridged into
   the LAN bridge (`br-home`). No lower-priority `gre` entry is kept, because
   osw does not hold that fallback (§5.3, §7).
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
| EMOSA agent | the uplink switch (credentials, Multi-AP backhaul STA, bridge), and the truthful backhaul report |
| Pod bootstrap | credentials that lead to the option-2 path, so every restart lands the pod there (§5.3) |
| GTP | none for this pod while it is on option 1. It is where the pod returns after any restart. |

### 5.3 Switching safely

Switching the uplink moves the path EMOSA itself uses to reach the pod. EMOSA
cannot undo a bad switch from outside, so the design relies on OpenSync's own
safety net (§2.2): a pod whose new uplink gives it no router for about half a
minute restarts to its bootstrap uplink.

That leads to the key rule:

**In an RDK or prpl deployment, the pods' bootstrap credentials MUST lead to
the option-2 path**, meaning the pod-backhaul SSID and the GTP. Then every
restart, planned or not, lands the pod on a working path, and EMOSA's fleet
re-onboards it (§4.7). Option 1 is a runtime upgrade that EMOSA re-applies
after each re-onboarding, for pods that qualify.

The switch:
1. starts only from a working option-2 path;
2. first confirms that the gateway's backhaul BSS is up and advertising
   Multi-AP;
3. writes the new backhaul station configuration in one guarded transaction:
   the controller's backhaul SSID and passphrase, with `multi_ap=backhaul_sta`;
4. waits for the pod to come back on its agent port with the new uplink in its
   State. That means `multi_ap=backhaul_sta` and `wds=true` in
   `Wifi_VIF_State`, bridged into `br-home`, `cm` using the station as its
   uplink, and no `g-bhaul-sta-*`;
5. counts as applied only then (the operation lifecycle, spec §5).

If the pod doesn't come back within the deadline, it has already restarted to
option 2, or will. EMOSA records the operation as `TIMED_OUT`, marks the pod
option-2-only, and never retries on its own.

**Credential-list fallback is not the safety net.** OpenSync can hold a
`multi_ap` credential above a `gre` one, and `wpa_supplicant` does connect to
the `gre` network when the Multi-AP SSID is absent (§7, Q5). But osw treats
"connected to the lower-priority network" as never settled, and aborts `owm`
after 180 s. The pod was left without a working uplink until OpenSync
restarted.

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
| OpenSync 6.6 support | Works live (§5.6), provided the platform reports `multi_ap=backhaul_sta` in `Wifi_VIF_State` for a Multi-AP link. The open-source cfg80211 platform does that only for MediaTek drivers. The lab's pods carry opensync-lab's platform patch (`d1dc985`), which does it for every driver. |
| Driver and platform | The pod's Wi-Fi driver and `wpa_supplicant` must support a 4-address STA with the Multi-AP element (`multi_ap_backhaul_sta`), and vendor platforms differ. mac80211 and `mac80211_hwsim` support 4-address. |
| Gateway backhaul BSS | prplMesh and RDK both run standard hostapd Multi-AP backhaul BSSes. Profile-2 backhaul BSSes may expect a Multi-AP Profile subelement and an R2 primary VLAN (Traffic Separation). The station must match. |
| 1905 source interface | The agent's 1905 frames come from EMOSA on the LAN while the pod's link is on the backhaul BSS. prplMesh and RDK tolerance of that is unknown. It must be tested; the fallback is to keep reporting the declared Ethernet attachment. |
| Controller-driven backhaul procedures | Backhaul Steering, backhaul link metrics and Backhaul STA capability queries need new translations to OVSDB, or explicit refusals |
| Loss of OpenSync's own backhaul logic | Where OpenSync's `cm` and `wano` expect GRE, some of their fallback and health checks change behaviour. See §7. |
| Risk | Management depends on the new link. That is why the switch is staged (§5.3) and always falls back to option 2. |

### 5.6 In the lab

**Backhaul BSS.** The prplMesh gateway's own agent doesn't configure its BSSes in
this lab (§8), so the Multi-AP backhaul BSS is hostapd's standard
implementation (`multi_ap=1`, `wds_sta=1`), in `em-gtp` (`emosa-lab-bh`,
bridged into the LAN bridge). prplMesh and RDK use the same hostapd feature.
`lab.sh uplink POD multi-ap` writes the pod's backhaul station with a sole
`multi_ap` credential.

**Unpatched pod image.** The data path worked: pod-6 joined as a 4-address
station bridged into `br-home`, with no GRE, and the client had internet.
But the cfg80211 platform left `Wifi_VIF_State.multi_ap` at `none` on hwsim.
So `cm` never adopted the link, and OpenSync restarted to its bootstrap after
about 2 minutes (§7).

**Patched pod image.** opensync-lab `d1dc985` (`pod/opensync/patches/platform/cfg80211/0001`)
reports the Multi-AP link state for every driver. The pod image is
`mvx-pod-20260924130806`. pod-6 was relaunched from it, re-admitted by the
fleet (same serial, same AL MAC), and switched with OpenSync's fatal-restart
protection on:
- within 27 s, `Wifi_VIF_State` showed `multi_ap=backhaul_sta` and `wds=true`,
  and `cm` used `bhaul-sta-24` as its uplink: 4-address, bridged into
  `br-home`, no GRE;
- the client had internet;
- the pod's management connection to its EMOSA agent ran over the new uplink,
  the agent stayed `provisioning`, and the controller showed the agent with
  its client;
- it held for 4.6 minutes, past osw's 180 s window, with no `owm` abort and no
  OpenSync restart;
- after a 30 s backhaul loss, the client was back 13 s after the link
  returned, `cm` re-adopted the station, and nothing restarted.

**Found on the way (EMOSA):** after a release, a re-admitted pod was refused
with `OWNERSHIP_CONFLICT`. Its old journal still recorded that someone else
(local-noc, after the release) had changed the fronthaul. `forget` now
archives the agent's state directory, so a pod handed over again starts a new
ownership period with its history kept.

## 6. Comparison

| | Option 2: GRE + GTP (baseline) | Option 1: EasyMesh backhaul (optional) |
| --- | --- | --- |
| Pod changes | none, if the pod-backhaul SSID matches its credentials | uplink reconfiguration through OVSDB |
| Gateway changes | a pod-backhaul SSID on its own segment, and a GTP (on or next to it) | none |
| Works with unchanged OpenSync 6.6 | yes: shown in the lab, including the fault workload (§4.7) | on MediaTek platforms per the source. Other platforms need the one-function platform change that opensync-lab applies (§5.6, §7). |
| Client MTU | 1500, if the underlay allows at least 1538 | 1500 native |
| Overhead | 38 bytes per frame, software GRE | none |
| Backhaul loss 30 s, lab | client back 25.7 s after the link returned | client back 13 s after the link returned |
| Controller's view of the uplink | declared (Ethernet), plus the pod's station as a client | truthful Wi-Fi backhaul, once translated |
| Backhaul optimisation by the controller | none | possible |
| Failure mode | tunnel rebuilds automatically | a staged switch with fallback to option 2 |
| Multiple hops | not yet | native |

## 7. OpenSync 6.6 support for option 1

Investigated in the OpenSync 6.6.1.0 source the lab pods are built from
(`mvx-pod-work/core`, release `1dad64e9`, platform `cfg80211`) and on a live
pod, without changing it.

**Verdict:** OpenSync 6.6.1.0 supports option 1. A Multi-AP backhaul station
("Multi-AP onboarding") works end to end in the lab, provided the platform
reports the Multi-AP link state. The open-source cfg80211 platform does that
only for MediaTek drivers. opensync-lab's one-function platform patch does it
for every driver, and with it option 1 runs stably (§5.6). A vendor platform
qualifies if it sets `multi_ap=backhaul_sta` in `Wifi_VIF_State` for a
connected Multi-AP station.

| # | Question | Answer | Evidence |
| --- | --- | --- | --- |
| Q1 | Can OVSDB express it? | **Yes.** | `Wifi_VIF_Config` and `Wifi_VIF_State` have `multi_ap` (`none`, `backhaul_sta`, `backhaul_bss`, `fronthaul_bss`, `fronthaul_backhaul_bss`), `wds`, `parent` and `bridge`. `Wifi_Credential_Config.onboard_type` is `gre` or `multi_ap` (`schema_consts.h`). |
| Q2 | Do `owm`/osw and the platform implement it? | **Yes, in configuration.** | `owm` maps `multi_ap=backhaul_sta`, or a `multi_ap` credential, to a station network with `multi_ap=true` and bridge `br-home` (`ow_ovsdb.c`, `ow_ovsdb_cconf.c`). osw writes `multi_ap_backhaul_sta=1` into `wpa_supplicant`'s network block (`osw_wpas_conf.c`) and passes the bridge to `wpa_supplicant`. After a Multi-AP backhaul association, `wpa_supplicant` itself switches the interface to 4-address mode. State reports `wds=true` and `multi_ap=backhaul_sta` while such a link is up. The pod's `wpa_supplicant` (v2.11-devel) contains `multi_ap_backhaul_sta`, and its hwsim radios support `AP/VLAN`. |
| Q3 | Does `cm` skip the GRE and bridge the station? | **Yes.** | `cm2_util_is_wds_station` treats a station with `multi_ap=backhaul_sta` in State as a WDS station. It runs no DHCP on it and makes the station itself the bridged uplink in `Connection_Manager_Uplink` (`cm2_ovsdb.c`). `cm2_bh_gre` builds a tunnel only for a station with a link-local address, and a WDS station has none. |
| Q4 | Does it work end to end in the lab? | **Yes, with the platform patch (§5.6).** Without it, the data path works but uplink adoption fails on hwsim: | pod-6's `bhaul-sta-24` joined a hostapd Multi-AP backhaul BSS (`multi_ap=1`, `wds_sta=1`; `em-gtp`, `emosa-lab-bh`) in 29 s. The station ran in 4-address mode and was bridged into `br-home` with no GRE, and the pod and its client had internet. But `Wifi_VIF_State` said `multi_ap=none`, `wds=false`. `osw_plat_cfg80211` copies `multi_ap` into the link state only for known drivers, and hwsim is `drv_id: unknown`. So `cm` skipped the station as a legacy one, never selected an uplink, and OpenSync restarted to bootstrap after about 2 minutes. The prplMesh gateway's own agent doesn't configure its BSSes in this lab ("Not all BSSes from M2 configured by agent"), hence the hostapd stand-in. |
| Q5 | Is there a fallback? | **Yes: OpenSync's restart to its bootstrap uplink.** | With a missing Multi-AP SSID above a working `gre` credential, `wpa_supplicant` connected to the `gre` network within 24 s, and the GTP path came up. But osw never considered that settled, and `owm` aborted 180 s later. What does recover every case is `cm`'s fatal restart (§2.2): the pod restarts and rejoins its bootstrap uplink. It was seen after a broken tunnel (about 40 s), after the hwsim uplink-adoption failure (about 2 minutes), and when forced with OpenSync's own `restart.sh`. |

## 8. Next steps

1. **Option 2 is ready** for the RDK and prpl labs: the adapter kit ships
   `emosa-gtp`. The gateway side's obligations are in spec §8 and §4.3,
   including the pods' bootstrap credentials leading to the GTP (§5.3).
2. **Option 1 works in the lab** on the patched pod image (§5.6). What's left
   for EMOSA is to perform the switch itself, as an operation (§5.3): take the
   backhaul credentials from the controller's M2 set, write the backhaul
   station, confirm it from State, and re-apply after every re-onboarding. It
   also needs to report the Wi-Fi backhaul to the controller truthfully
   (§5.1, step 6).
3. **Against prplMesh itself,** its gateway agent must first configure its own
   BSSes in this lab ("Not all BSSes from M2 configured by agent"). That's a
   prplMesh lab issue, separate from EMOSA.
