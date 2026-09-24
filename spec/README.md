# EMOSA adapter specification

**Version 1.0-draft.** This describes what the reference implementation (Python,
`src/emosa`) does today. Any implementation of EMOSA, in any language, is
conformant when it:
- meets every **MUST** below;
- accepts and produces the formats in [`schemas/`](../schemas);
- passes the conformance vectors in [`conformance/`](conformance).

While two implementations run side by side, this document is the contract
between them. A change to either implementation that changes behaviour
described here starts with a change here.

## 1. What EMOSA is

EMOSA makes each OpenSync pod appear to an EasyMesh controller as a standard
EasyMesh agent. The pod runs unchanged, and EMOSA takes the place of its cloud
manager.

```
EasyMesh controller ──IEEE 1905.1──▶ agent (one per pod) ──▶ translation ──OVSDB──▶ pod
                                              ▲
                     pod handed over ──▶ fleet (front port, one registry)
```

It has three parts:
- **Fleet** (§4): one front port. It identifies each pod that connects,
  allocates the pod's agent, starts it, and hands the pod to it.
- **Agent** (§2, §3, §5): one per pod. It speaks 1905.1/EasyMesh for the pod,
  monitors the pod's OVSDB, and applies the controller's configuration as
  guarded OVSDB transactions.
- **Translation** (§3.3): a pure mapping between the pod's OVSDB model and the
  EasyMesh model.

## 2. Northbound: EasyMesh / IEEE 1905.1

### 2.1 Transport

- Every agent MUST use its own L2 interface on the controller's LAN. Its source
  MAC and 1905 AL MAC are the agent's AL MAC. The reference implementation uses
  one macvlan per agent on a shared trunk.
- Frames use EtherType `0x893A`. Multicast goes to `01:80:c2:00:00:13`.
- CMDUs are at most 1500 bytes per fragment, carry at most 256 TLVs, and hold
  TLV values up to `0x3FFF` bytes. Fragmented CMDUs MUST be reassembled.
- The agent accepts messages only from the configured controller AL MAC.
- Relayed messages and security-envelope TLVs MUST be refused.
- Input MUST be rate-limited. The reference limit is 16 messages per second
  with a burst of 32.

### 2.2 Identity: the AL MAC

A fleet derives the AL MAC from the pod's serial number. Both implementations
MUST derive the same address, so a pod keeps its EasyMesh identity when one
implementation replaces the other.

```
for n in 0, 1, 2, …:
    d = SHA-256( UTF-8( "emosa-agent-al:" + serial + ":" + decimal(n) ) )
    al = 0x02 ‖ d[0..4]                         # 6 bytes, locally administered, unicast
    if al is not the controller's AL and not already assigned: use al
```

### 2.3 Message sets

The `message_set` setting selects how the agent talks to a given controller:

| `message_set` | For | Difference |
| --- | --- | --- |
| `easymesh-6.1` (default) | EasyMesh 6.1 controllers, e.g. prplMesh 6.0 | Search and M1 carry the profile TLVs. The Response MUST echo the agent's profile. |
| `r1` | R1-style controllers, e.g. RDK unified-wifi-mesh | Search and M1 carry only basic capabilities. The Response's profile TLV is not matched. Topology Response omits profile-gated TLVs. |

### 2.4 Messages

Sent by the agent:

| Type | Message | When |
| --- | --- | --- |
| `0x0000` | Topology Discovery | on start, then every 60 s |
| `0x0001` | Topology Notification | after admission: when the pod's BSS set changes, and on every client join or leave (with a Client Association Event TLV `0x92`) |
| `0x0003` | Topology Response | in answer to a Topology Query |
| `0x0006` | Link Metric Response | in answer to a Link Metric Query, once provisioned |
| `0x0007` | AP-Autoconfiguration Search | up to 3 times, 1 s apart, when onboarding starts |
| `0x0009` | AP-Autoconfiguration WSC (M1) | after an admitted Response |
| `0x8000` | 1905 Ack | acknowledging a Multi-AP Policy Config Request |
| `0x8002` | AP Capability Report | in answer to an AP Capability Query |
| `0x8005` | Channel Preference Report | in answer to a Channel Preference Query |
| `0x8007` | Channel Selection Response | in answer to a Channel Selection Request: accepted (code 0), without moving the radio |
| `0x8008` | Operating Channel Report | after a Channel Selection Request, while the BSS operates |
| `0x800A` | Client Capability Report | in answer to a Client Capability Query (declares the capability unavailable) |
| `0x800C` | AP Metrics Response | in answer to an AP Metrics Query, only with qualified measurements |
| `0x8022` | Client Disassociation Stats | after an observed client departure, only with qualified statistics |
| `0x8043` | Early AP Capability Report | with M1, before configuration |

Received by the agent:

| Type | Message | Effect |
| --- | --- | --- |
| `0x0002` | Topology Query | answered with Topology Response |
| `0x0005` | Link Metric Query | answered when provisioned |
| `0x0008` | AP-Autoconfiguration Response | admission check, then M1 |
| `0x0009` | AP-Autoconfiguration WSC (M2) | authenticated, then becomes one operation (§5) |
| `0x000A` | AP-Autoconfiguration Renew | onboarding starts again with a fresh M1 |
| `0x8000` | 1905 Ack | acknowledgement for the agent's own reports |
| `0x8001` | AP Capability Query | answered |
| `0x8003` | Multi-AP Policy Config Request | stored and acknowledged. Nothing is applied to the pod. |
| `0x8004` / `0x8006` | Channel Preference Query / Channel Selection Request | answered (§3.4) |
| `0x8009` | Client Capability Query | answered |
| `0x800B` | AP Metrics Query | answered only with qualified measurements |

Any other message is ignored and counted as `unsupported_message_<type>`.

### 2.5 Onboarding

| State | Meaning |
| --- | --- |
| `waiting_source` | the pod's current state is not available yet |
| `discovering` | Searches sent (at most 3, 5 s window) |
| `admitting` | a Response was admitted |
| `awaiting_m2` | M1 sent |
| `provisioning` | M2 accepted as an operation; the agent is configured |
| `source_lost` | the pod became unavailable |
| `failed` | discovery timed out or the exchange failed |
| `incompatible` | the controller's Response is outside what the agent supports |
| `closed` | the session ended |

Rules:
- A session that fails, loses its source, or receives a Renew is replaced by a
  new one. Restarts after failures back off by `min(30, 2^failures)` seconds.
- If nothing arrives from the controller for 130 s, the agent MUST start
  onboarding again.
- Once the agent is `provisioning` and the controller has sent its next
  Topology Query, the agent MUST announce every current client again, once.
  A controller that restarted may otherwise never learn clients that joined
  before it did.

### 2.6 What the agent reports

Everything reported comes from the pod's State tables (§3.3), never from what
was written to Config:
- one radio, operating class 81 (2.4 GHz, 20 MHz), where only the current
  channel is operable;
- maximum EIRP from `tx_power`;
- no HT, VHT, HE or EHT capability claims;
- WPA2-PSK with CCMP-128;
- no DPP.

The agent's own 1905 interface is declared Ethernet (media type `0x0001`). Each
BSS is an 802.11n 2.4 GHz interface (`0x0103`). The pod's own Wi-Fi uplink is
not reported.

## 3. Southbound: OpenSync OVSDB

### 3.1 Connections

- The pod always connects to EMOSA. EMOSA listens (passive OVSDB) and acts as
  the OVSDB client, like any OpenSync cloud manager.
- **Front port (fleet):** the fleet reads `AWLAN_Node`, writes
  `AWLAN_Node.manager_addr = tcp:<advertise>:<agent port>`, and MUST then end
  the session. OpenSync's connection manager acts on a new manager address only
  once disconnected.
- **Agent port:** one per pod, listening on loopback only. A forwarder (in the
  lab, an LXD proxy) carries the pod's connection to it. The agent keeps one
  `monitor` session and reconnects with 1 to 9 s backoff. Echo probes run every
  5 s.
- The agent MUST refuse to act on a pod whose `AWLAN_Node.serial_number` differs
  from its configured `serial`.

### 3.2 What EMOSA reads and writes

Monitored, read-only:

| Table | Columns |
| --- | --- |
| `AWLAN_Node` | `serial_number`, `model`, `firmware_version`, `id` |
| `Wifi_Radio_Config` | `if_name`, `freq_band`, `enabled`, `vif_configs` |
| `Wifi_Radio_State` | `if_name`, `radio_config`, `vif_states`, `freq_band`, `channel`, `mac`, `enabled`, `country`, `tx_power` |
| `Wifi_VIF_Config` | `if_name`, `mode`, `ssid`, `enabled`, `wpa`, `wpa_key_mgmt`, `wpa_psks`, `security`, `rsn_pairwise_ccmp`, `wpa_pairwise_tkip`, `wpa_pairwise_ccmp`, `wpa_oftags`, `bridge`, `multi_ap` |
| `Wifi_VIF_State` | the Config columns above, plus `vif_config`, `mac`, `associated_clients`, `multi_ap` |
| `Wifi_Associated_Clients` | `mac`, `state` |

Written, each as one guarded transaction. A cold create and a multi-BSS set are
preceded by a read-only `select` of `Wifi_Inet_Config`.

| Change | Operations |
| --- | --- |
| Hand over (fleet) | `update AWLAN_Node manager_addr` |
| Update the fronthaul | `wait` on the AWLAN_Node serial, the radio's references (`if_name`, `vif_configs`) and the VIF's guarded fields → `update Wifi_VIF_Config ssid` → `mutate wpa_psks` (the single slot becomes key `key`) |
| Cold pod: create the fronthaul | `wait` on the serial and that the VIF is absent → `insert Wifi_VIF_Config` (profile row, received SSID and PSK) → `mutate Wifi_Radio_Config vif_configs` → `update Wifi_Radio_Config channel, ht_mode, enabled` → `insert Wifi_Inet_Config` if absent |
| Multi-BSS set | as above for the primary BSS, plus: insert, update or delete each profile slot VIF so the slots are **exactly** the received set, with the matching `vif_configs` mutations and `Wifi_Inet_Config` rows |

Every write MUST be guarded: if the pod's graph or guarded fields changed,
nothing is written. Guarded VIF fields: `if_name`, `mode`, `enabled`, `ssid`,
`wpa`, `wpa_key_mgmt`, `wpa_psks`, `security`, and the pairwise cipher flags.
EMOSA MUST NOT write any other table or column.

### 3.3 Translation: OVSDB → EasyMesh

| OpenSync (State) | EasyMesh / 1905.1 |
| --- | --- |
| `AWLAN_Node.serial_number`, `firmware_version` | Device Inventory serial number and software version |
| `Wifi_Radio_State.mac` | Radio unique identifier (RUID) |
| `Wifi_Radio_State.freq_band`, `channel`, `tx_power` | operating class (`2.4G` → 81), channel, maximum EIRP |
| `Wifi_VIF_State` with `mode=ap`, `enabled=true`, a `mac`, and listed in its radio's `vif_states` | a BSS: BSSID = `mac`, SSID = `ssid` |
| `Wifi_VIF_State.multi_ap` | BSS Configuration Report flags: `backhaul_bss` → `0x80`, anything else → `0x40` |
| `Wifi_Associated_Clients` with `state=active`, listed in the VIF's `associated_clients` | associated clients of that BSS |
| `Wifi_VIF_State` with `mode=sta` | the pod's uplink: not reported |

The primary BSS is represented only while its State shows a WPA2-PSK AP in the
6.6 encoding (below). Before that, the radio is advertised without a BSS so the
controller can configure it.

The 6.6 encoding of WPA2-PSK:
- `wpa=true`;
- `wpa_key_mgmt=["wpa-psk"]`;
- `rsn_pairwise_ccmp=true`;
- no `security` map;
- neither TKIP nor WPA-CCMP;
- exactly one `wpa_psks` slot.

### 3.4 Translation: EasyMesh → OVSDB

- **M2 → one intent.** The first fronthaul BSS's SSID and passphrase are the
  primary intent. With `multi_bss`, up to 7 further BSSes follow. Each BSS's
  role comes from the WSC Multi-AP extension flags in its M2: `0x20` is
  fronthaul, `0x40` is backhaul. Combined or teardown flags are refused.
- **Checked before any write.** Settings are refused when:
  - an SSID is longer than 32 bytes or contains an embedded NUL;
  - a passphrase is outside 8 to 63 characters or is not printable ASCII;
  - authentication or encryption is not WPA2-PSK/AES;
  - the set has more BSSes of a role than the profile has slots.
- **Shared M2 session.** With `m2_session=shared`, an M2 set from one
  registrar session (one nonce, one public key) is accepted, as RDK sends it.
- **Channel selection** is accepted (response code 0), but EMOSA does not move
  the pod's radio. The Operating Channel Report that follows gives the channel
  the pod actually uses.

### 3.5 Pod profiles

The pod's layout is data, not code: [`schemas/pod-profile.schema.json`](../schemas/pod-profile.schema.json).
A profile gives:
- the band, channel and HT mode a cold pod's BSS is created with;
- the chipset reported in the inventory;
- the fronthaul VIF name and its row;
- the backhaul overrides;
- the extra VIF slots for multi-BSS;
- the `Wifi_Inet_Config` row of a created VIF.

The bundled profile is `opensync-lab-hwsim-6.6.1-v1`.

## 4. The fleet

For each connection on the front port, the fleet:
1. selects `AWLAN_Node` and requires exactly one row with a usable serial:
   letters, digits, `.`, `_` and `-`, at most 64 characters;
2. refuses the pod, leaving it unchanged, if it is not admitted (`admit`) or if
   no port is free;
3. finds or creates the pod's registry entry: the AL MAC (§2.2), the first free
   port, and interface `em<index>`;
4. writes the agent configuration; if it changed, restarts the agent,
   otherwise starts it;
5. writes `manager_addr` and ends the session.

The whole exchange MUST complete within 5 s. A pod returning later gets the
same entry. `forget SERIAL` stops the agent and deletes the entry and the
configuration, but keeps the agent's state directory.

## 5. Operations

Each accepted M2 becomes one durable operation, journalled before anything is
sent to the pod. States:

| State | Meaning |
| --- | --- |
| `REQUESTED` → `VALIDATED` | planned against a fresh, complete snapshot |
| `SUBMITTED` | the transaction was sent (recorded before sending) |
| `CONFIG_COMMITTED` | OVSDB accepted it |
| `OBSERVED_APPLIED` | the pod's State shows it: **the only success** |
| `INDETERMINATE` | the reply was lost; the outcome is unknown |
| `TIMED_OUT` | the deadline (120 s) passed without the change showing in State |
| `REJECTED`, `FAILED`, `OWNERSHIP_CONFLICT`, `CANCELLED` | ended without applying |

Rules:
- Only one operation per pod may be active (`REQUESTED`, `VALIDATED`,
  `SUBMITTED`, `CONFIG_COMMITTED` or `INDETERMINATE`). Another one is
  `REJECTED` with reason `BUSY`.
- A configuration the pod already runs is observed as applied without writing:
  repeated M2s never cause repeated writes.
- An `INDETERMINATE` operation becomes `TIMED_OUT` once its deadline has passed
  and the pod's current Config lacks the write. It MUST NOT block the pod
  forever.
- After a restart, a `SUBMITTED` operation becomes `INDETERMINATE`, and an
  unsent one is cancelled.
- Legal transitions are listed in `src/emosa/operations.py` and MUST be kept.

## 6. Management and state

| Item | Format | Where |
| --- | --- | --- |
| Fleet configuration | [`fleet-config.schema.json`](../schemas/fleet-config.schema.json) | e.g. `/etc/emosa-fleet.json` |
| Agent configuration | [`agent-config.schema.json`](../schemas/agent-config.schema.json) | `<config_dir>/<pod_id>.json` |
| Fleet registry | [`fleet-registry.schema.json`](../schemas/fleet-registry.schema.json) | `<state_root>/fleet.json` |
| Agent status | [`agent-status.schema.json`](../schemas/agent-status.schema.json) | `<state_dir>/status.json`, rewritten on change, at most once a second |
| Pod profile | [`pod-profile.schema.json`](../schemas/pod-profile.schema.json) | bundled, or a path |
| Operation journal, secrets | implementation-private | `<state_dir>/journal`, `<state_dir>/secrets` |

Real examples from the lab are in [`examples/`](examples).

Commands:
- `emosa-fleet serve|list|forget CONFIG [SERIAL]`
- `emosa-agent CONFIG`

Both MUST validate their configuration and refuse an invalid one.

Secrets:
- Passphrases received in M2 live only in the secret store, one file per
  reference (`wsc-<32 hex>` plus `-1`…`-7` for extra BSSes).
- They MUST NOT appear in logs, status, the journal or evidence. The journal
  holds keyed fingerprints only.

Timers:

| Timer | Value |
| --- | --- |
| Topology Discovery interval | 60 s |
| Controller silence before onboarding again | 130 s |
| Pod State re-read and reconcile | 0.5 s |
| Published report lifetime | 1.5 s |
| Operation deadline | 120 s |
| Fleet exchange | 5 s |
| Idle wakeup (frames are handled at once) | 0.2 s |

## 7. Dependencies

- **Runtime:** OVSDB JSON-RPC (RFC 7047); Diffie-Hellman group 5 (MODP-1536),
  AES-CBC and HMAC-SHA256 for WSC; JSON Schema draft 2020-12 for contracts.
- **Operating system:** raw packet sockets and one macvlan (or other L2
  interface) per agent. A supervisor, systemd in the reference, starts one
  agent process per pod.
- **Not needed:** nothing on the pod, and nothing from the controller's
  software (prplMesh, RDK) at build time or run time.

## 8. Data plane (RDK and prpl integration)

EMOSA carries a pod's control. A pod's clients also need a data path to the
gateway LAN. The design and its reasoning are in
[`doc/architecture/data-plane.md`](../doc/architecture/data-plane.md). Any
integration MUST meet the following:

| # | Requirement |
| --- | --- |
| D1 | A pod's clients are on the gateway LAN at L2, getting its DHCP, IPv6 RAs, DNS, broadcast and multicast. |
| D2 | The pod reaches EMOSA's front port and agent port over its data path. |
| D3 | Pods are not modified. Only OVSDB configuration, and only as specified. |
| D4 | Client MTU is 1500, without fragmentation in normal operation. |
| D5 | The path rebuilds by itself after backhaul loss or reboot. A failed change never strands a pod. |
| D6 | The pods' underlay has its own L2 segment and subnet, separate from the client LAN. |
| D7 | What the controller is told about the uplink is true, or declared as a simplification. |

**GRE termination point (GTP), the baseline:** always provided. The pod keeps
OpenSync's 3-address backhaul station and gretap. The GTP:
- MUST use a link-local underlay (`169.254.0.0/16`), because OpenSync 6.6 `cm`
  builds no tunnel over any other address;
- MUST own the first host address (`.1`) of the underlay subnet, because
  OpenSync 6.6 `cm` uses it as the tunnel remote;
- MUST serve DHCP on the underlay with option 26 (interface MTU) of at least
  1538 (1600 in the reference);
- MUST create one gretap per associated pod lease (MTU 1562), bridged to the
  gateway LAN, and remove it when the lease or association ends;
- MUST NOT bridge the underlay segment itself into the LAN;
- MUST answer ICMP echo on `.1`: OpenSync 6.6 `cm`'s link check pings it,
  without an ARP fallback.

OpenSync restarts a pod to its bootstrap uplink after 8 consecutive failed
router checks (`CM2_STABILITY_THRESH_FATAL`), which is roughly half a minute
while failing. So:
- the gateway LAN's router MUST answer ICMP echo or ARP from pods;
- an uplink change MUST restore router reachability within that time;
- in an RDK or prpl deployment, the pods' bootstrap credentials MUST name
  the pod-backhaul SSID, so that every restart lands a pod on the GTP path.

The gateway provides:
- a fronthaul-type pod-backhaul SSID on the underlay segment, because a
  Multi-AP backhaul BSS rejects 3-address stations;
- the steering-disallowed entries for the pods' backhaul stations.

The agent keeps reporting a declared Ethernet attachment.

**EasyMesh backhaul, optional:** only for pods whose platform qualifies (data
plane document §7). A pod qualifies only if its platform reports
`multi_ap=backhaul_sta` in `Wifi_VIF_State` for a Multi-AP link. OpenSync
6.6's cfg80211 platform does so for MediaTek drivers only, which excludes
hwsim. The agent writes the controller's backhaul SSID and passphrase with
`multi_ap=backhaul_sta` on the pod's backhaul station. OpenSync then joins as a
4-address Multi-AP backhaul station bridged into `br-home`, without GRE.
EMOSA MUST NOT rely on a lower-priority `gre` credential as the fallback: osw
aborts `owm` when it stays on a lower-priority network. The fallback is the
pod's restart to its bootstrap (GTP) path. The switch is one guarded operation that
counts as applied only when the pod is back with the new uplink in its State.
If the pod doesn't come back, it returns to the GTP path by its own restart,
and EMOSA MUST NOT retry on its own.

## 9. Not covered yet

- 5 and 6 GHz radios, and WPA3;
- more than one radio per agent;
- the pod's Wi-Fi uplink in the topology;
- AP and station metrics from real measurements;
- TLS on the front port and agent ports (a physical pod requires it);
- DPP onboarding.
