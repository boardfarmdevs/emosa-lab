# EMOSA adapter specification

**Version 1.0-draft.** This describes what the reference implementation (Python,
`src/emosa`) does today. Any implementation of EMOSA, in any language, is
conformant when it:
- meets every **MUST** below;
- accepts and produces the formats in [`schemas/`](../schemas);
- passes the conformance vectors in [`conformance/`](conformance).

The [component design](design.md) gives the processes, interfaces, thread
model, state, timing and resource budgets an implementation is built to.

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
- On a device that also runs the EasyMesh controller, the agents' interfaces MUST
  NOT be interfaces of the controller's network namespace: a controller may take
  an agent whose AL MAC is the MAC of one of its own interfaces for its co-located
  agent (RDK's does, and roots its backhaul topology there). The reference
  implementations open their sockets in the namespace `EMOSA_NETNS` names.
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
| `0x8000` | 1905 Ack | acknowledging a Multi-AP Policy Config Request, a Channel Scan Request, a Client Steering Request (with an Error Code TLV `0xA3` per station not on the source BSS) or a Backhaul Steering Request |
| `0x8002` | AP Capability Report | in answer to an AP Capability Query |
| `0x8005` | Channel Preference Report | in answer to a Channel Preference Query |
| `0x8007` | Channel Selection Response | in answer to every well-formed Channel Selection Request: accepted (code 0) without moving the radio, or declined (code 2) when the pod cannot do what it asks (§3.4) |
| `0x8008` | Operating Channel Report | after a Channel Selection Request, while the BSS operates |
| `0x800A` | Client Capability Report | in answer to a Client Capability Query (declares the capability unavailable) |
| `0x800C` | AP Metrics Response | in answer to an AP Metrics Query, and unsolicited at the controller's AP metrics reporting interval, from the pod's statistics (§3.8) |
| `0x8010` | Unassociated STA Link Metrics Response | after the Ack of an Unassociated STA Link Metrics Query: the stations the pod heard recently (§3.9) |
| `0x8017` | Steering Completed | after the Ack of a steering opportunity: EMOSA steers nothing on its own account (§3.7) |
| `0x801A` | Backhaul Steering Response | the request's MID, once the move is known: result `0x00` (success) when the pod's State shows its station on the target, naming the backhaul STA associated after the move (EasyMesh 6.1, 17.2.33: the pod's station on the target's band, which after a move to another band is not the one the request named), else `0x01` with an Error Code TLV (`0xA3`, the station): reason `0x04` at once when the pod has no backhaul station on the target's band, or that station's radio carries the pod's BSSes on another channel; reason `0x06` otherwise, at once for a request it cannot carry out (§8.3) |
| `0x801C` | Channel Scan Report | after the Ack of a Channel Scan Request: a Timestamp TLV (`0xA8`) and one Channel Scan Result TLV (`0xA7`) per requested channel of the pod's radio, status `0x01` (scan not supported) (§3.4) |
| `0x8022` | Client Disassociation Stats | after an observed client departure, only with qualified statistics |
| `0x8028` | Backhaul STA Capability Report | in answer to a Backhaul STA Capability Query: one Backhaul STA Radio Capabilities TLV (`0xCB`) for each radio of the pod with one of its backhaul stations, enabled or not, the one in use first, then by RUID (EasyMesh 6.1, 9.3; §8.3): the radio's RUID, and the station's MAC with the MAC-included flag once `owm` reports its State; also over GRE |
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
| `0x8003` | Multi-AP Policy Config Request | stored and acknowledged, also with policy TLVs EMOSA does not interpret (recorded as not applied). Nothing is applied to the pod. |
| `0x8004` / `0x8006` | Channel Preference Query / Channel Selection Request | answered (§3.4) |
| `0x8009` | Client Capability Query | answered |
| `0x800B` | AP Metrics Query | answered from the pod's statistics (§3.8) |
| `0x800F` | Unassociated STA Link Metrics Query | acknowledged, with an Error Code TLV for every station the pod cannot report (§3.9) |
| `0x8014` | Client Steering Request | acknowledged; a mandate for one station and one target is carried out by the pod (§3.7) |
| `0x8019` | Backhaul Steering Request | acknowledged; with option 1 on, for the pod's backhaul station, the uplink moves to the target with the pod's backhaul station on the target's band (§8.3), else refused (`0x801A`) |
| `0x801B` | Channel Scan Request | acknowledged and reported as not supported (§3.4) |
| `0x8027` | Backhaul STA Capability Query | answered |

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
- A Response is dropped (the session keeps discovering) unless its MID is one of
  the session's Searches, it names the 2.4 GHz band and, with EasyMesh 6.1, it
  carries a Multi-AP Profile TLV that is not Profile 2 or 3. A Response that is
  not dropped is admitted only if it has no issue; any issue makes the session
  `incompatible`, which only a renewal (a Renew, or the silence rule below) ends.
  The issues, in this order: with EasyMesh 6.1, `controller_capability_absent`
  (no Controller Capability TLV `0xDD`), else `kib_mib_support_absent` (its bit 7
  clear) and `early_ap_capability_bit_absent_for_non_dpp_search` (its bit 6
  clear); `security_capability_length_invalid` or
  `security_capability_reserved_algorithm` for a 1905 Security Capability TLV
  `0xA9` that is not three zero octets (one that is absent is no issue); and
  `outside_profile1_24ghz_contract` when, with EasyMesh 6.1, the profile is not 1
  (a reserved value), or the band is not 2.4 GHz. The status names them
  (`session.admission_issues`). The vectors: `onboarding.json`, `admission`.
- A session starts while the pod's State is available. Discovery sends a
  Search at once and then every second, three at most, and fails when no
  Response is admitted within 5 s.
- The radio's capabilities are fixed for one source. If the radio's identity or channel
  changes under the same source, the source is unavailable. A new source (a new database
  generation: the pod recreated, maybe with another radio) has its capabilities taken
  again, and its new session reports them.
- A session that fails, loses its source (the pod's State lost, or a new
  database generation), or receives a Renew is replaced by a new one. Restarts
  after failures back off by `min(30, 2^failures)` seconds; a Renew restarts at
  once, and provisioning resets the failures.
- With EasyMesh 6.1, the Early AP Capability Report goes out before M1 and is
  sent again with a new MID every 250 ms, three transmissions at most, until
  an Ack names one of its MIDs within 1 s. It is dropped when the pod's State
  changes or lapses before then.
- If nothing arrives from the controller for 130 s, in any state, the agent
  MUST start onboarding again.
- If no M2 arrives within 30 s of M1, the agent MUST start onboarding again. A
  controller that restarted in between has forgotten the M1, and its other
  queries keep the silence rule from firing (seen with RDK).
- With `topology_query_window` in its configuration, if the agent is
  `provisioning` and no Topology Query has arrived from the controller for that
  many seconds since M2 was accepted or since the last one, the agent MUST start
  onboarding again. A controller that restarted and forgot the agent may keep
  sending its other queries, but no Topology Query (seen with RDK, which queries
  each agent it knows about every 15 s). Set it only for a controller that queries
  its agents periodically: prplMesh queries on events only. Absent: the rule is off.
- If the agent is `provisioning` but the pod serves none of the controller's
  BSSes and no write is under way for 60 s, the agent MUST start onboarding
  again: its configuration was lost (e.g. a write lost to an uplink move).
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
reported only while it is an EasyMesh backhaul (§8.3). Over OpenSync's GRE it
is not an EasyMesh link, and the agent reports nothing about it. When that
backhaul's upstream is another pod of the same fleet, the two agents report each
other as 1905 neighbors on it, with its link metrics (§8.5).

## 3. Southbound: OpenSync OVSDB

### 3.1 Connections

- The pod always connects to EMOSA. EMOSA listens (passive OVSDB) and acts as
  the OVSDB client, like any OpenSync cloud manager.
- **Front port (fleet):** the fleet reads `AWLAN_Node`, writes
  `AWLAN_Node.manager_addr = tcp:<advertise>:<agent port>`, and MUST then end
  the session. OpenSync's connection manager acts on a new manager address only
  once disconnected.
- **Agent port:** one per pod, listening on loopback only. A forwarder carries
  the pod's connection to it: on a gateway, the package's own (`forward` in the
  fleet configuration: the front and agent ports on the `advertise` address); in
  the labs' containers, an LXD proxy. The agent keeps one
  `monitor` session and reconnects with 1 to 9 s backoff. Echo probes run every
  5 s.
- The agent MUST refuse to act on a pod whose `AWLAN_Node.serial_number` differs
  from its configured `serial`.

### 3.2 What EMOSA reads and writes

Monitored, read-only:

| Table | Columns |
| --- | --- |
| `AWLAN_Node` | `serial_number`, `model`, `firmware_version`, `id`, `mqtt_settings` |
| `Wifi_Radio_Config` | `if_name`, `freq_band`, `enabled`, `vif_configs` |
| `Wifi_Radio_State` | `if_name`, `radio_config`, `vif_states`, `freq_band`, `channel`, `mac`, `enabled`, `country`, `tx_power` |
| `Wifi_VIF_Config` | `if_name`, `mode`, `ssid`, `enabled`, `wpa`, `wpa_key_mgmt`, `wpa_psks`, `security`, `rsn_pairwise_ccmp`, `wpa_pairwise_tkip`, `wpa_pairwise_ccmp`, `wpa_oftags`, `bridge`, `multi_ap`, `credential_configs`, `wds`, `parent` |
| `Wifi_VIF_State` | the Config columns above except `credential_configs`, plus `vif_config`, `mac`, `associated_clients` |
| `Wifi_Associated_Clients` | `mac`, `state` |
| `Wifi_Credential_Config` | `ssid`, `security`, `onboard_type`, `priority`, `enabled` |
| `Connection_Manager_Uplink` | `if_name`, `if_type`, `is_used`, `has_L2`, `has_L3`, `bridge` |
| `Wifi_Stats_Config` | `stats_type`, `radio_type`, `report_type`, `reporting_interval`, `sampling_interval` |

Written, each as one guarded transaction. A cold create and a multi-BSS set are
preceded by a read-only `select` of `Wifi_Inet_Config`.

| Change | Operations |
| --- | --- |
| Hand over (fleet) | `update AWLAN_Node manager_addr` |
| Update the fronthaul | `wait` on the AWLAN_Node serial, the radio's references (`if_name`, `vif_configs`) and the VIF's guarded fields → `update Wifi_VIF_Config ssid, multi_ap` → `mutate wpa_psks` (the single slot becomes key `key`) |
| Cold pod: create the fronthaul | `wait` on the serial and that the VIF is absent → `insert Wifi_VIF_Config` (profile row, received SSID and PSK, `multi_ap`) → `mutate Wifi_Radio_Config vif_configs` → `update Wifi_Radio_Config channel, ht_mode, enabled` → `insert Wifi_Inet_Config` if absent |
| Multi-BSS set | as above for the primary BSS, plus: insert, update or delete each profile slot VIF so the slots are **exactly** the received set, with the matching `vif_configs` mutations and `Wifi_Inet_Config` rows |
| Uplink switch (§8.3) | `wait` on the AWLAN_Node row and serial, and on the station's guarded fields (`if_name`, `mode`, `enabled`, `ssid`, `credential_configs`, `multi_ap`) → `insert Wifi_Credential_Config` (the backhaul SSID and passphrase, `onboard_type=multi_ap`, `priority` 1) → `update Wifi_VIF_Config` of the station: `enabled=true`, `ssid` and `security` empty, `multi_ap` and `wds` unset, `credential_configs` = that credential only → for each other of the pod's backhaul stations that is enabled, in name order: `wait` on its guarded fields → `update Wifi_VIF_Config` of it: `enabled=false`. Updates only: a station without a row takes no switch |
| Ethernet uplink (§8.4) | `wait` on the AWLAN_Node serial, and on the port's `Connection_Manager_Uplink` row: `if_name`, `if_type=eth`, `is_used=true` and its current `bridge` → `update Connection_Manager_Uplink bridge` (the fronthaul's bridge) |
| Statistics publishing (§3.6) | `wait` on the AWLAN_Node serial and its current `mqtt_settings` → `update AWLAN_Node mqtt_settings` (broker, port, the pod's topic, QoS 0, no compression) → `insert Wifi_Stats_Config` (a raw client report for the radio type) unless one exists |

Every write MUST be guarded: if the pod's graph or guarded fields changed,
nothing is written. Guarded AP VIF fields: `if_name`, `mode`, `enabled`, `ssid`,
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
| `Wifi_VIF_State` with `mode=sta`, `multi_ap=backhaul_sta`, `wds=true`, a `parent`, and the only `Connection_Manager_Uplink` row with `is_used=true` | the EasyMesh backhaul (§8.3): a local interface of media type `0x0103` (2.4 GHz) or `0x0104` (5 GHz) with the station's `mac`, media-specific information `parent` BSSID, role `0x40` (non-AP STA), channel; in the bridging tuple with the BSSes; and in the Backhaul STA Capability Report. The 1905 neighbor stays on the Ethernet interface, where EMOSA's frames go, unless the upstream is another pod of the fleet (§8.5) |
| any other `Wifi_VIF_State` with `mode=sta` | not reported |

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
  primary intent. With `multi_bss`, further BSSes follow: each role fills the
  profile's slots of that role in the order received. BSSes beyond the slots are
  left out: not written, not reported, their passphrases not stored. A radio with
  fewer slots than the controller's set thus serves the BSSes it has (RDK sends five
  to every agent, whatever its maximum); a set may carry up to 16 M2s, each
  authenticated. Each BSS's
  role comes from the WSC Multi-AP extension flags in its M2: `0x20` is
  fronthaul, `0x40` is backhaul. A backhaul BSS may also carry the Backhaul STA
  bit (`0x80`), as prplMesh sends it: the same credentials serve the agent's
  backhaul station (§8.3). Combined or teardown flags are refused.
- **The role in `multi_ap`.** Every BSS EMOSA writes carries its M2's role in
  `Wifi_VIF_Config.multi_ap`: `fronthaul_bss` or `backhaul_bss`, whatever the
  profile row or the pod had before. The pod's AP then takes that role as a
  Multi-AP agent's AP does (hostapd's `multi_ap` 2 or 1): hostapd sends the
  Multi-AP element in its (Re)Association Response to a Multi-AP station, never
  in Beacons or Probe Responses, and admits ordinary stations as before.
- **Checked before any write.** Settings are refused when:
  - an SSID is longer than 32 bytes or contains an embedded NUL;
  - a passphrase is outside 8 to 63 characters or is not printable ASCII;
  - authentication or encryption is not WPA2-PSK/AES;
  - an intent has more BSSes of a role than the profile has slots (one made from an
    M2 set never has: it is fitted to the slots first).
- **Shared M2 session.** With `m2_session=shared`, an M2 set from one
  registrar session (one nonce, one public key) is accepted, as RDK sends it.
- **Channel selection** is answered within one second. It is accepted
  (response code 0), without moving the pod's radio, when it allows the channel
  the pod operates on. Preference groups for operating classes the radio does
  not advertise are ignored. A request that forbids that channel, or limits the
  power below what the pod transmits, is declined (code 2: it violates the
  preferences and capabilities last reported) and does not replace the stored
  policy; EMOSA has no qualified mapping to move or turn down the radio. The
  Operating Channel Report that follows either answer gives what the pod
  actually uses. A controller that gets no answer gives the radio up: RDK's
  then refuses to steer through it.
- **Channel scans** are acknowledged and reported as not supported: the
  Channel Scan Report carries the time of the answer (RFC 3339, UTC) and, for
  each channel the request names for the pod's radio (or its current channel
  when the request names none), result status `0x01`. Radios of other agents in
  the request are left out. An unchanged pod gives EMOSA no scan results it
  could qualify. RDK's controller sends the request as one step of configuring
  an agent and keeps the radio scan-pending until the Ack arrives; a steering
  request in that state leaves the radio unconfigured afterwards.

### 3.5 Pod profiles

The pod's layout is data, not code: [`schemas/pod-profile.schema.json`](../schemas/pod-profile.schema.json).
A profile gives:
- the band, channel and HT mode a cold pod's BSS is created with;
- the chipset reported in the inventory;
- the fronthaul VIF name and its row;
- the backhaul overrides;
- the extra VIF slots for multi-BSS;
- the `Wifi_Inet_Config` row of a created VIF;
- the backhaul station the uplink switch moves (§8.3), one the pod's bootstrap
  creates.

The bundled profile is `opensync-lab-hwsim-6.6.1-v1`.

### 3.6 The pod's own statistics

OVSDB carries no station measurements. OpenSync publishes them itself, as
`sts.Report` protobuf messages (the pinned `opensync_stats.proto`) over MQTT:
`qm` connects to the broker in `AWLAN_Node.mqtt_settings` with the pod's device
certificate, and `owm` produces the reports that `Wifi_Stats_Config` asks for.
With `telemetry.mode = mqtt` the agent (the telemetry scope):
- **writes** the broker, the pod's own topic (default `emosa/stats/<serial>`),
  optionally `qm`'s publish interval (`agg_stats_interval`; OpenSync's default
  is 60 s, too slow for a controller that wants client metrics younger than
  30 s) and one raw client report for the radio type, as one guarded transaction
  (§3.2), once per start of the pod's OpenSync (the database starts from its
  template), like the uplink switch (§8.3). It MUST NOT take over a broker
  another manager set, such as the operator's cloud: that write is refused;
- counts the write as **applied** when the pod's database has it on the same
  start. Whether reports arrive is reported beside it;
- **subscribes** to the pod's topic. A report is bound to its pod by the topic
  (the report's `nodeID` may be empty). Retained, duplicate and out-of-order
  reports are dropped.

What it keeps, per station, is only what the pod measured:
- counters (bytes, frames, retries, errors) summed per counter epoch. A period
  with a join or a leave, a missing period, or a station's absence starts a
  new epoch;
- OpenSync sends a counter only when it is not zero. An absent counter is
  therefore zero only once the pod has shown that counter non-zero; until then
  it is unknown, and so is every total that includes it. `tx_retries` is sent
  only together with `rx_retries`, so its absence never means zero;
- the last rates and the SNR as reported, and the end of the last period, so
  the age of every value is known. A station is current for three periods plus
  `qm`'s one-minute batching. A rate that is not a finite number of Mbit/s from
  0 to 2^32 − 1 (what a Link Metrics TLV's four octets hold) is no measurement
  and is absent; an SNR beyond the RCPI's range gives RCPI 220.

These measurements are in the agent's status. EMOSA sends an EasyMesh metric
only when it can be built completely from them; until then it sends none
(§9).

### 3.7 Client steering

A Client Steering Request (`0x8014`) is acknowledged within one second, with an
Error Code TLV (`0xA3`, reason `0x02`) for each listed station that is not
associated with the source BSS. A **mandate** for one station and one named
target, from a BSS of the pod, is then carried out by the pod's band steering
(`owm`, the steering scope):
- **opens** a steering window as one guarded transaction (§3.2): the pod is the
  bound serial and no `Band_Steering_Clients` row exists for the station
  (another manager's steering MUST NOT be taken over). The window is the
  station's complete client row with client steering `away` for the window
  (`cs_params.cs_enforce_period`), a BTM kick with deauthentication fallback
  (`sc_kick_type` `btm_deauth`) and the target in its BTM parameters
  (`sc_btm_params`: `bssid`, `disassoc_imminent`), plus a steering group
  (`Band_Steering_Config`) and the target as a neighbor (`Wifi_VIF_Neighbors`)
  on the source VIF, each reused when present and inserted when absent;
- counts it **applied** when `owm` reports `cs_state` `steering` for the row,
  then writes the directed kick (`force_kick` `directed`, guarded by that
  state). `owm` sends the BTM request with the target as its candidate;
- **closes** the window when `owm` stops steering, or at the latest after the
  window, by deleting exactly the rows it inserted (by UUID). Without
  disassociation imminent the station is to be left where it is if it
  declines: the window closes 8 s after the kick, before `owm`'s
  deauthentication fallback (10 s after its BTM request).
- **sweeps** the rows its windows left in the pod (a close that failed, the pod
  away, or a process that ended first): once per pod source, with no window
  under way, every row whose UUID its journal records as created by one of its
  windows and not yet released, and that is still in the pod, is deleted by that
  UUID. A window's rows are **released** (`commit_evidence.released`) when its
  close deletes them, when the sweep does, or when the sweep finds them gone (a
  pod restart). A sweep whose close fails is tried again after 30 s on the same
  source. A row left behind would otherwise refuse every later mandate for its
  station as another manager's, and the agent never takes a row for its own by its
  content: only the journal's record makes it its own.

The window is 15 to 120 s (the request's opportunity window, raised to 15 s).
One mandate at a time; a request while a window is open is acknowledged and
not carried out. So are several stations or targets, the wildcard target (the
agent would choose it), and a source that is not a BSS of the pod. A steering
**opportunity** leaves the choice to the agent: EMOSA makes none, so Steering
Completed (`0x8017`) follows the Ack at once.

No Client Steering BTM Report (`0x8015`) is sent: OpenSync 6.6 does not expose
the station's BTM status (its band-steering report never carries it). The
controller sees the outcome in the topology: the station leaves the pod's BSS
(Client Association Event, §2.4) and joins the target.

Known limitation: the pod does not tell EMOSA whether a station supports BTM.
`owm` deauthenticates a station without BTM support at once, also for a
request without disassociation imminent.

### 3.8 AP metrics from the pod's statistics

With telemetry (§3.6), the agent reports AP metrics as a native agent does:
in answer to an AP Metrics Query and, when the controller's Metric Reporting
Policy sets an AP metrics reporting interval, unsolicited at that interval.
RDK's controller learns a client's signal only this way.

The received policy is kept durably: written when it is received, before its
1905 Ack, and only then. The record names what it was received for: the
controller, the agent and its radio. A record of another (a pod whose radio
changed, another controller) is never applied or reported on, and the next
policy received replaces it whole (counted `stored_policy_superseded`). The schedule of unsolicited reports is the session's:
it starts with each session (so with each start of the agent), the first report
due one interval after, and its accounting (reports sent, periods without one)
is the session's too, in the agent's status. A report that is due is accounted
before it is sent, so a failed send never repeats a period.

Telemetry configures two reports on the pod: the raw client report and a raw
on-channel survey of the represented radio, both at the reporting interval.

An AP Metrics Response carries, for each of the pod's operating BSSes:
- **AP Metrics TLV** (`0x94`): the BSSID; the channel utilization, measured:
  the survey sample's busy percentage scaled to 0–255, from a sample at most
  three reporting intervals old; the number of stations on the BSS; and the
  Estimated Service Parameters for best effort, **declared**: the profile's
  `esp_be` value (three octets), reported as configured, not measured, in the
  agent's status. Without a fresh survey sample no AP Metrics TLV is sent for
  the radio, and no response.
- **Associated STA Link Metrics TLV** (`0x96`), when the radio's policy asks
  for link metrics: for each station on the BSS with a fresh client report
  (at most three reporting intervals old): the time since the measurement,
  the last downlink and uplink rates, and the uplink RCPI. The pod reports
  SNR; OpenSync derives it from the received signal with a fixed noise floor,
  and the agent applies the inverse to obtain the RSSI and from it the RCPI.
  A station without a fresh report is left out.
- **Associated STA Traffic Stats TLV** (`0xA2`), when the policy asks for
  traffic statistics: for stations whose counters the pod measures (§3.6).

### 3.9 Unassociated station measurements

The pod hears probe requests. OpenSync's band steering records each probe from
a station it tracks with its SNR and time, and publishes these events in its
band-steering report (every 60 s). The agent keeps a monitor-only
`Band_Steering_Clients` row for each station the controller asks about on the
pod's operating class and channel: client steering off, no kick, no probe
blocking, no band preference, marked `cs_params` `{"emosa": "watch"}` so the
row is recognisably the agent's on the pod itself. At most 32 stations are
watched, the most recently asked; a station not asked about for 10 minutes,
associated with the pod, or with another manager's client row is not watched.
The rows join a `Band_Steering_Config` group on the fronthaul VIF (reused when
present, left in place). The watch set is written in one guarded transaction,
at most every 10 s. A steering window for a watched station (§3.7) replaces its
watch row in the window's transaction. The agent reads the probe events from
the pod's statistics.

On an Unassociated STA Link Metrics Query the agent acknowledges within one
second. The Ack carries an Error Code TLV for every requested station it
cannot report: reason `0x01` for a station associated with one of the pod's
BSSes, reason `0x02` for a station the pod has not heard on the requested
channel within the last two minutes. Then an Unassociated STA Link Metrics
Response, with the query's MID, lists the other stations: the channel, the time since the probe, and
the uplink RCPI derived from the probe's SNR as in §3.8. Channels other than
the pod's operating channel are answered with reason `0x02` for their
stations. Measurements are real or absent: the agent never fills a station
from the radio model or a default.

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
same entry. When it starts, the fleet MUST do step 4 for each admitted entry its
registry has, in the registry's order, without waiting for the pod: an image
upgrade keeps the registry and the configurations on persistent storage but not
the agents' enabled units. The registry file is the fleet's state: a serving fleet MUST read it
again for each pod, so a `forget` run as another process takes effect at once. With `run_root` each agent's configuration has `run_dir` `<run_root>/<pod_id>`. The agent configuration takes the fleet's settings (`message_set`,
`multi_bss`, `m2_session`, `profile`, `uplink`), overridden per pod by
`pods.<serial>`, and the fleet's `topology_query_window` (§2.5), a property of the
controller, when it has one: a different pod model needs its own profile, and the uplink
switch (§8.3) is enabled per pod. `forget SERIAL` stops the agent, deletes the entry and the
configuration, and archives the agent's state directory as
`<pod_id>.released-<stamp>`, without the agent's status (a running agent's: in its run
directory, its link would show the next agent's). A pod handed over
again starts a new ownership period, so conflicts recorded before its release
don't block it.

**The agents' processes.** With `agents` `systemd` (the default) each agent is the unit
`emosa-agent@<pod_id>`:
step 4 enables it and starts or restarts it, and `forget` disables and stops it. With
`supervised`, for a system without systemd (a router's busybox init), the agents are the
fleet's own children, run as that unit would run them:
- Each child gets the fleet's environment, then `/etc/default/emosa` and
  `/etc/default/emosa-<pod_id>` (`KEY=VALUE` lines; the later file wins), and
  `EMOSA_AGENT_CONFIG_DIR` set to `config_dir`. The link helper `emosa-agent-link <pod_id>`
  runs first, then the agent with `<config_dir>/<pod_id>.json`. `EMOSA_AGENT_LINK` and
  `EMOSA_AGENT` name other programs.
- Step 4 waits until the agent runs, as for a unit. If the link helper fails, the step
  fails, and the helper is tried again 3 s later.
- An agent that ends is started again 3 s later while its configuration exists. When its
  configuration is removed, the agent is stopped: SIGTERM, then SIGKILL after 5 s.
  `forget` stops the agent recorded in `<log_root>/<pod_id>/agent.pid` before it deletes
  the configuration.
- A fleet that ends stops its agents, and its next start starts them again (§4). A fleet
  that is killed takes its agents with it.
- The output of each agent and its helper goes to `<log_root>/<pod_id>/agent.log`, which
  becomes `agent.log.1` past 256 KiB. `log_root` is `run_root`, else `state_root`.

When `agents` is absent, `EMOSA_AGENTS` decides: from the fleet's environment, else from
`/etc/default/emosa`. Only the C fleet supervises; the reference refuses `supervised`.

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
- A written configuration belongs to the start of the pod's OpenSync it was
  written on (the start's radio rows, as for the uplink switch, §8.3). When the
  pod's OpenSync starts again, its database comes back from the template without
  it: that is not another manager's change, so it is no ownership conflict, and a
  conflict seen on an earlier start no longer blocks the pod (a new ownership
  period). The agent configures the pod again from the controller's next M2
  (§2.5). The operation's evidence names the start (`instance`).
- An `INDETERMINATE` operation becomes `TIMED_OUT` once its deadline has passed
  and the pod does not show it applied: its current Config lacks the write (it
  never landed, or a restart dropped it), or has it while a fresh State does not
  show it, as a `CONFIG_COMMITTED` one times out. It MUST NOT block the pod
  forever.
- After a restart, a `SUBMITTED` operation becomes `INDETERMINATE`, and an
  unsent one is cancelled.
- Legal transitions are listed in `src/emosa/operations.py` and MUST be kept.
- The uplink switch (§8.3) is a second scope with its own journal and the same
  states and transitions. Its deadline is 90 s. At most one operation per scope
  is active.

## 6. Management and state

| Item | Format | Where |
| --- | --- | --- |
| Fleet configuration | [`fleet-config.schema.json`](../schemas/fleet-config.schema.json) | e.g. `/etc/emosa-fleet.json` |
| Agent configuration | [`agent-config.schema.json`](../schemas/agent-config.schema.json) | `<config_dir>/<pod_id>.json` |
| Fleet registry | [`fleet-registry.schema.json`](../schemas/fleet-registry.schema.json) | `<state_root>/fleet.json` |
| Agent status | [`agent-status.schema.json`](../schemas/agent-status.schema.json) | `<state_dir>/status.json`, rewritten on change, at most once a second; with a `run_dir` (a RAM disk on a gateway) the file is `<run_dir>/status.json` and `<state_dir>/status.json` a link to it, so the state directory is not written once a second |
| Pod profile | [`pod-profile.schema.json`](../schemas/pod-profile.schema.json) | bundled, or a path |
| Operation journal, secrets | implementation-private | `<state_dir>/journal`, `<state_dir>/uplink` (the uplink scope), `<state_dir>/secrets` |

Real examples from the lab are in [`examples/`](examples).

Commands (the C names end in `-c`: `emosa-fleet-c`, `emosa-agent-c`, `emosa-gtp-c`; each
implementation's take the same arguments, read and write the same files, and either takes
over from the other):
- `emosa-fleet serve|list|forget CONFIG [SERIAL]`
- `emosa-agent CONFIG`
- `emosa-gtp setup|lease|reconcile|list CONFIG [ACTION MAC IP [HOST]]` (§8.2)

Each MUST validate its configuration and refuse an invalid one.

Secrets:
- The secret store is an interface with four operations: read a reference's value,
  create a reference (never replacing one, durable when it returns), remove one, and
  the fingerprint key (created once). The files backend keeps one private file per
  reference (mode 0600, owned by the agent) in `<state_dir>/secrets` (mode 0700); a
  platform's secure storage MAY implement the interface instead. The policy is the
  store's, the same for every backend: references match
  `[A-Za-z0-9][A-Za-z0-9_.-]{0,95}` (never a path), a usable passphrase is 8 to 63
  printable ASCII bytes, and fingerprints are HMAC-SHA256 under the store's key.
- Passphrases received in M2 live only in the secret store, one reference each
  (`wsc-<32 hex>` plus `-1`…`-7` for extra BSSes).
- They MUST NOT appear in logs, status, the journal or evidence. The journal
  holds keyed fingerprints only.

Journal:
- Each journal (the AP scope's and every other scope's) keeps every active operation
  (`REQUESTED`, `VALIDATED`, `SUBMITTED`, `CONFIG_COMMITTED`, `INDETERMINATE`), each
  pod's latest operation in `SUBMITTED`, `CONFIG_COMMITTED`, `OBSERVED_APPLIED`,
  `INDETERMINATE`, `TIMED_OUT` or `OWNERSHIP_CONFLICT` (the one reconciliation follows,
  §5), every operation whose window's rows are still in the pod (`commit_evidence.created`
  and not `released`, §3.7: the sweep's only record of them), and the 16 most recent. When an operation is added, every other operation is
  removed, with its WSC receipt, in the same transaction. Events are kept per run up to
  the 10 000 most recent. A journal so stays bounded, and a new operation is never
  refused for the journal's size.

Timers:

| Timer | Value |
| --- | --- |
| Topology Discovery interval | 60 s |
| Controller silence before onboarding again | 130 s |
| M1 without M2 before onboarding again | 30 s |
| Provisioned but serving no BSS before onboarding again | 60 s |
| Discovery window (three Searches, 1 s apart) | 5 s |
| Early AP Capability Report: retransmission, window | 250 ms, 1 s |
| Pod State re-read and reconcile | 0.5 s |
| Published report lifetime | 1.5 s |
| Operation deadline | 120 s |
| Uplink switch deadline | 90 s |
| Fleet exchange | 5 s |
| Idle wakeup (frames are handled at once) | 0.2 s |

## 7. Dependencies

- **Runtime:** OVSDB JSON-RPC (RFC 7047); Diffie-Hellman group 5 (MODP-1536),
  AES-CBC and HMAC-SHA256 for WSC; JSON Schema draft 2020-12 for contracts;
  with telemetry (§3.6), an MQTT broker the pods reach with their device
  certificates, and protobuf for OpenSync's pinned statistics schema.
- **Operating system:** raw packet sockets and one macvlan (or other L2
  interface) per agent. A supervisor, systemd in the reference, starts one
  agent process per pod.
- **Not needed:** nothing on the pod, and nothing from the controller's
  software (prplMesh, RDK) at build time or run time.

## 8. Data plane (RDK and prpl integration)

EMOSA carries a pod's control. A pod's clients also need a data path to the
gateway LAN. The design and its reasoning are in
[`docs/concepts/data-plane.md`](../docs/concepts/data-plane.md). Any
integration MUST meet the following.

### 8.1 Requirements

| # | Requirement |
| --- | --- |
| D1 | A pod's clients are on the gateway LAN at L2, getting its DHCP, IPv6 RAs, DNS, broadcast and multicast. |
| D2 | The pod reaches EMOSA's front port and agent port over its data path. |
| D3 | Pods are not modified. Only OVSDB configuration, and only as specified. |
| D4 | Client MTU is 1500, without fragmentation in normal operation. |
| D5 | The path rebuilds by itself after backhaul loss or reboot. A failed change never strands a pod. |
| D6 | The pods' underlay has its own L2 segment and subnet, separate from the client LAN. |
| D7 | What the controller is told about the uplink is true, or declared as a simplification. |

### 8.2 GRE termination point (GTP), the baseline

Always provided. The pod keeps OpenSync's 3-address backhaul station and
gretap. The GTP:
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
- the steering-disallowed entries for the pods' backhaul stations, in its
  controller's Multi-AP Policy for every agent (local and BTM). EMOSA lists
  each pod's station in its agent's status (`pod.backhaul.mac`). A gateway
  whose agent keeps the pod-backhaul SSID out of EasyMesh (RDK-B with
  OneWifi's libwebconfig 0014) shows its controller no station there at all;
  the entries still cover the pods' stations on any backhaul BSS.

The agent keeps reporting a declared Ethernet attachment.

### 8.3 EasyMesh backhaul, optional

Only for pods whose platform qualifies (data plane document §7): the platform
MUST report `multi_ap=backhaul_sta` in `Wifi_VIF_State` for a Multi-AP link.
OpenSync 6.6's cfg80211 platform does so for MediaTek drivers only;
opensync-lab's pod image patches it for every driver. The pod then
joins as a 4-address Multi-AP backhaul station bridged into `br-home`, without
GRE.

The agent makes the switch itself when its configuration has
`uplink.mode = multi-ap`:
- **Credentials:** the backhaul BSS (role backhaul) of the controller's
  applied M2 set, with `multi_bss`; or `uplink.ssid` and `uplink.secret_ref`
  from the configuration.
- **Upstream:** the one BSSID the station may join, `uplink.bssid`, required
  whatever the credential source. The credential carries it
  (`Wifi_Credential_Config.bssid`), so the station never picks a BSS by SSID
  alone. EMOSA MUST refuse a switch to any of the pod's own BSSIDs. A pod given
  the backhaul BSS in its M2 set serves the backhaul SSID itself, and a station
  on its own backhaul BSS puts a loop into `br-home` (data plane document §5.6).
- **Station:** the profile's uplink station (`uplink.station` overrides it), the one the
  pod bootstraps on. The pod's bootstrap MUST create it. The profile also names the pod's
  backhaul station on each band (`uplink.stations`). Stock OpenSync's bootstrap creates a
  station for every band its target lists, all enabled; opensync-lab's hwsim pods create
  them with only the bootstrap band's enabled (hwsim radios are multi-band). Either way the
  switch makes its station the only enabled one: two stations up bridge `br-home` into the
  network twice, with no STP to stop the loop.
- **When:** the pod is bound, `cm` reports a working uplink, and the station is
  not already on that backhaul. One switch per start of the pod's OpenSync. A
  start is identified by the UUIDs of its `Wifi_Radio_Config` rows: OpenSync's
  start scripts create them anew, while `AWLAN_Node` comes from the database
  template and keeps its UUID. The switch is therefore made again after every
  re-onboarding, because every OpenSync restart returns the pod to its
  bootstrap uplink.
- **Write:** one guarded transaction (§3.2): the station in credential-list
  mode with one `multi_ap` credential, pinned to the upstream BSSID, and each other
  backhaul station of the pod that is enabled disabled. Updates only: EMOSA creates no
  station row. EMOSA MUST NOT keep a lower-priority
  `gre` credential as the fallback, because osw aborts `owm` when it stays on a
  lower-priority network. The fallback is the pod's restart to its bootstrap
  (GTP) path.
- **Applied** only when, on the same start of the pod, its State shows the
  station with `multi_ap=backhaul_sta` and `wds=true` on the configured SSID
  and upstream BSSID (its `parent`), and it is `cm`'s only uplink in use.
- **Held:** a switch not applied within 90 s becomes `TIMED_OUT`. EMOSA then
  holds the pod on option 2. So does a switch that fails or is rejected by
  OVSDB, and one whose station configuration another manager changes on the
  same start. A failed switch's hold is bounded: after a backoff
  (`uplink.hold_backoff`, 600 s) EMOSA switches the pod again, once, on the same
  start or a later one; held again, the backoff doubles, up to
  `uplink.hold_backoff_cap` (9600 s). An applied switch clears the backoff. The
  hold, its attempt and when it ends are in the agent's status. Another
  manager's change holds until a new admission (§4 `forget`), which clears any
  hold. A kept target is the exception: see Moved.
- **Reported:** while the switch is applied, the backhaul is reported as in
  §3.3.
- **Moved:** a Backhaul Steering Request (§2.4) for the pod's backhaul station
  pins the station to the target BSSID instead, by the same switch, while the
  pod is on its EasyMesh backhaul and no switch is under way; the answer
  (`0x801A`) follows once the pod's State shows the station on the target. A
  target on another band (from the request's operating class: 81-84 2.4 GHz,
  115-130 5 GHz, 131-137 6 GHz) moves the uplink to the pod's backhaul station
  on that band, by the same switch, which disables the station in use; the
  answer names that station (EasyMesh 6.1, 17.2.33). A band the pod has no
  station row on takes no move: refused at once, reason `0x04`. So does a target
  on another channel than a station's radio when that radio carries the pod's
  BSSes: the station cannot leave the channel the controller set for them
  (§3.4). The target and its station are kept for the pod's later starts while
  the configured upstream is the one they replaced. A move not applied within
  the switch's deadline, or rejected, returns to the previous upstream, with its
  station, without a hold, and is answered with a failure. On a later start, a switch to the kept target that is not
  applied within the deadline, or is rejected (its BSS gone or out of reach),
  drops the target without a hold. The agent then switches to the configured
  upstream once the station no longer carries the failed switch's credential
  (`cm` reverts it on the same start, or OpenSync restarts): a switch written
  before then is reverted with it. Only that switch holds. Both agents.

### 8.4 Ethernet uplink, for a wired pod

A pod whose uplink is an Ethernet port (a wired pod: no backhaul station, `cm` takes the
port once its DHCP and router checks pass) carries its clients over that port: the
operator's cloud bridges the port into the pod's home bridge. Without the cloud, the agent
does it when its configuration has `uplink.mode = ethernet`:
- **Port:** `uplink.port`, `eth1` when absent. **Bridge:** the bridge of the profile's
  fronthaul VIF (`br-home`).
- **When:** the pod is bound and `cm` uses the port as its uplink: its
  `Connection_Manager_Uplink` row has `if_type=eth` and `is_used=true`, and its `bridge` is
  not already the fronthaul's. OpenSync forgets the bridge at every start, so the write is
  made once per start of the pod's OpenSync (identified as in §8.3).
- **Write:** one guarded transaction (§3.2): `update Connection_Manager_Uplink bridge`.
  `cm` then adds the port to the bridge.
- **Applied** when, on the same start, the row shows the bridge. A write not applied within
  30 s, or rejected, is not repeated on that start; the next start tries again.
- **Another manager:** a row that already names another bridge is left alone, and the write
  refused.
- **Reported:** the pod's backhaul is the Ethernet attachment the agent declares in any
  case (§8.2), which here it is (D7). Its 1905 neighbor is on that interface.

Both agents.

### 8.5 Pods as parents of other pods

A pod on its EasyMesh backhaul (§8.3) may have another pod's backhaul BSS as its
upstream (`uplink.bssid`, or a Backhaul Steering target): the child's 4-address
station joins the parent's backhaul BSS, and its `br-home` is bridged into the
parent's, whatever the parent's own uplink is. EMOSA writes nothing new to either
pod for it. The two agents, in one fleet, describe the hop to the controller:

- **Peer directory.** Each agent's status (§6) already gives its AL MAC
  (`agent_al`), its BSSes with their roles, its backhaul station (MAC, band,
  channel, parent BSSID) and its stations' measurements (§3.6). An agent reads
  the other agents' statuses in the fleet's run root, read-only, at most once a
  second; the status of an agent whose process (`worker_pid`) is gone is left out.
  It is the agent's only knowledge of other pods: no 1905 frame carries it.
- **The child** whose backhaul's parent BSSID is a backhaul BSS of another agent
  in the directory reports that agent's AL MAC as a 1905 neighbor on its backhaul
  interface (the station's MAC; 1905 Neighbor Device TLV, bridge flag 0), besides
  the controller on Ethernet, where EMOSA's frames go.
- **The parent** reports, on each of its backhaul BSSes, every agent in the
  directory whose backhaul's parent is that BSS and whose station is among the
  BSS's associated clients (§3.3), as a 1905 neighbor on the BSS's interface.
- **No loops.** EMOSA MUST refuse a switch or move (§8.3) to a BSS of a pod whose
  own upstream chain, through the directory, reaches this pod. Like a switch to
  the pod's own BSS, it would put a loop into `br-home`.
- **Link metrics.** A Link Metric Query for that neighbor (or for all) is
  answered for the pair from the parent's measurement of the child's station
  (§3.6), current as there:
  - the parent's Transmitter Link Metric (its BSS to the child's station) from
    the station's transmit counters and rate, its Receiver Link Metric from the
    receive counters and the SNR;
  - the child's from the same measurement, the directions swapped;
  - packet errors are the errors' sum in the counter epoch, packets the frames';
    the MAC throughput capacity and the PHY rate are the last rate (Mbit/s); link
    availability is 100; the RSSI field carries the SNR (a hwsim pod reports no
    noise floor, §9);
  - a counter that is unknown (§3.6), or a measurement that is not current,
    leaves that direction out; with no direction known for any neighbor asked
    about, nothing is sent.
- **The controller** places the child under the parent by its backhaul's parent
  BSSID, as before. It chooses or moves a pod between parents only if it knows a
  pod's backhaul BSS for a backhaul one: the BSS Configuration Report says so
  (§3.3) in `6.1`; in `r1` the report is profile-gated and not sent (RDK's
  controller drops a Topology Response below Profile 3 that carries it). RDK's
  controller takes a BSS's role from its own vendor TLV; the RDK lab's series
  adds the BSS Configuration Report (unified-wifi-mesh 0246) and, for an agent
  with neither, the role its own network SSID has (0247). A move to another of
  the pod's parents is the controller's Backhaul Steering Request (§8.3), also
  across bands.

Both agents.

### 8.6 A pod as the GRE parent of other pods (draft)

*Draft for review (10 October): approved in principle on 9 October (plan 9.5, layer B); not
implemented.*

§8.5 needs a 4-address Multi-AP link between the pods, which some radios cannot carry (a USB
adapter whose driver has no 4-address station or AP). Layer B gives a pod on such a radio the role
the gateway's GTP has (§8.2): its children join it as 3-address stations, as they join the
gateway, and their `cm` builds its gretap to it. The recipe is what the OpenSync cloud writes for
a parent pod, as opensync-lab's local-noc does (`mesh.py`, `parent_step` and `gre_step`).

- **Which backhaul.** The pod profile (§3.5) says how the pod's radios carry a backhaul BSS:
  `backhaul.mode` `multi-ap` (§8.5, the default) or `gre-parent` (this section). The controller
  decides where backhaul BSSes are, as before: a backhaul BSS in its applied M2 set for a radio
  of a `gre-parent` pod is created as the pod's **parent AP** on that radio, not as a Multi-AP
  BSS. The agent reports it as a backhaul BSS (§3.3), so the controller can steer a child onto it
  (RDK's controller takes the role from its own backhaul SSID, unified-wifi-mesh 0247).
- **The parent AP** (profile `backhaul.gre_parent.vif` over the backhaul slot's row): `mode=ap`,
  the M2's backhaul SSID and passphrase (`wpa_*` columns), `multi_ap=none`, no `bridge`,
  `ap_bridge=false`, SSID broadcast on. The pod's own backhaul station on that radio MUST be
  disabled: it would find only its own AP.
- **The underlay** (§8.2's rules, on the pod): `169.254.N.0/24`, the AP at `.1`
  (`Wifi_Inet_Config`: `ip_assign_scheme=static`, `netmask=255.255.255.0`, `NAT=false`,
  `mtu=1600`, `dhcpd` `start .10`, `stop .250`, `lease_time 12h`). `N` is the parent's, assigned
  by the fleet registry (§4): unique in the fleet, never `1` (the gateway's GTP), and kept across
  the pod's restarts. OpenSync 6.6 `cm` on a child takes the `.1` as its tunnel remote and
  pings it; the pod answers ICMP itself.
- **A tunnel per child:** for each lease on the AP (`DHCP_leased_IP`) whose MAC is associated to
  it (`Wifi_Associated_Clients` of the AP's `Wifi_VIF_State`): a `Wifi_Inet_Config` row
  `pgd<b3>_<b4>` (the lease's last two octets) with `if_type=gre`, `gre_ifname` the AP,
  `gre_local_inet_addr` the AP's address (`Wifi_Inet_State`), `gre_remote_inet_addr` the lease,
  `mtu=1562`, `ip_assign_scheme=none`, `network=true`; and its `Interface` and `Port` as a port of
  `br-home`. Removed when the lease or the association ends. Each hop carries one GRE: a
  child's frames leave its tunnel on the parent, in `br-home`, and ride the parent's own uplink.
  The underlay itself MUST NOT be bridged into `br-home`.
- **The child** of a GRE parent moves by the uplink switch (§8.3) with one difference: its
  credential is the pod-backhaul SSID and passphrase of a 3-address join (`onboard_type=gre`),
  pinned to the parent AP's BSSID, not a Multi-AP credential; `wds` stays unset. **Applied** when
  its State shows the station on that BSSID and `cm`'s uplink in use is the station with its
  gretap. Held, moved and reverted as in §8.3; the fallback is still the pod's restart to its
  bootstrap (GTP) path.
- **No loops**, as §8.5: EMOSA MUST refuse a move to a parent AP of a pod whose own upstream
  chain reaches this pod.
- **Reported** as in §8.5: the child names the parent's agent as its 1905 neighbor on its
  backhaul, the parent names the child on the AP, and the pair's link metrics come from the
  parent's client report of the child's station (§3.6; with telemetry off, no measurement).
- **Withdrawn:** a backhaul BSS no longer in the M2 set removes the parent AP, its
  `Wifi_Inet_Config` and every child's tunnel row, port and interface, in one guarded
  transaction; the children's `cm` then falls back to their bootstrap path.
- **Written** (§3.2), each change one guarded transaction: the parent AP (`insert` or `update
  Wifi_VIF_Config`, `mutate Wifi_Radio_Config vif_configs`, `update` the pod's station on that
  radio `enabled=false`, `insert` or `update Wifi_Inet_Config` of the AP); a child's tunnel
  (`insert Wifi_Inet_Config` gre row, `insert Interface`, `insert Port`, `mutate Bridge br-home
  ports`); the withdrawal and a child's departure as the matching deletes. Read besides §3.2's:
  `DHCP_leased_IP` (`hwaddr`, `inet_addr`), `Wifi_Inet_State` (`if_name`, `inet_addr`), `Bridge`
  (`name`, `ports`), `Port` and `Interface` (`name`).

Both agents. Proven first on `rdk-1010`'s virtual pods with a `gre-parent` profile, then on the
physical pods (a Pi through a Pi to the router).

## 9. Not covered yet

- 5 and 6 GHz radios, and WPA3;
- more than one radio per agent;
- backhaul link metrics and a 1905 neighbor on the backhaul interface towards an upstream
  that is not a pod of the fleet (an RDK or prpl node: EMOSA does not know its AL MAC);
- EasyMesh AP and station metrics. The pod's station measurements are
  collected (§3.6), but a complete metric needs more than a hwsim pod reports:
  RCPI needs the noise floor, traffic statistics need retries and errors, and
  AP metrics need channel utilization, which needs a radio model (wmediumd);
- TLS on the front port and agent ports (a physical pod requires it);
- DPP onboarding.
