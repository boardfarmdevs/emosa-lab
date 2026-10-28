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
| `0x0000` | Topology Discovery | once the pod is there, then every 60 s while it is (its source current, or its session held through its reconnection, §2.5): none for a pod away past the reconnection window, one at once when it is back (box `away-silent`) |
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
| `source_lost` | the pod became unavailable (a provisioned session: past the reconnection window), or another pod source |
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
- The radio's capabilities are fixed for one source. If the fronthaul radio's channel moves
  under the same source while the session is not provisioned yet, or another radio is in its
  place, that is another pod source (`fronthaul_moved`, `capabilities_changed`): a fresh session
  with the new capabilities, never a refusal, which would turn a channel move into an outage
  (box `fronthaul-moves-not-provisioned`). A provisioned session keeps a fronthaul's move, as a
  further radio's, with its reports (below). Every new connection from
  the pod to its agent is a new database generation: the pod's OpenSync restarted, the pod
  rebooted or was recreated (maybe with another radio), or its link to the agent dropped and
  came back. Its capabilities are taken again from its first refresh.
- A provisioned session is kept across its pod's reconnection (owner's decision, 13 October).
  Every backhaul move drops the pod's link to its agent, as the pod's station leaves its
  parent: OpenSync's CM opens it again 15 to 18 s later, a new generation of the same database
  (seen on rdk-1010 and on the Pis). Before this rule each move ended the session, a Search and
  new M1s followed, and the controller's model lacked the pod's BSSes for about 20 s. Now, while
  the link is away, a provisioned session is held, answering nothing that needs the pod, for
  at most the reconnection window, 60 s. It is kept when the new generation continues the pod
  source: the same pod (its serial and node ID), the same start of its OpenSync (its radio
  rows, which every start creates anew), and the same capabilities apart from their operable
  channels (every radio's RUID, maximum BSS, operating classes and their maximum EIRP, its HT and
  advanced capabilities, and the device's own; the maxima advertised so far are kept for the
  comparison). The agent then sends one Topology Notification, so that the controller's next
  Topology Query sees the pod's new uplink, and no Search or M1; its write scopes see the new
  generation, its configuration still in the pod. The window covers CM's reconnection with room
  for a slow move and stays well inside a controller's own silence windows (RDK queries each
  agent's topology every 15 s; EMOSA's `topology_query_window` in the RDK lab is 120 s; the
  controller-silence rule below is 130 s), so a held agent never outlasts what its controller
  tolerates. Any other new generation is another source, with a fresh session, counted by
  reason in the status's `reconnections`: another pod (`another_pod`); a restart of its OpenSync
  (`pod_restarted`: its database comes from its template, and the agent writes a configuration
  only for an M2, so a restarted pod needs the fresh session's); the window passed
  (`window_passed`); other capabilities (`capabilities_changed`, a power rise among them, the
  agent's log naming the first radio field or TLV that differs, e.g. `radio 02:..:00 class 81
  max EIRP 20 -> 23 dBm`). A
  session not yet provisioned (discovering, or an M1 awaiting its M2) is not held either: its
  WSC exchange belongs to its source. Both agents (box `transport-cut`, `transport-cut-long`,
  `reconnect-moved-uplink`, `reconnect-not-provisioned`, and the pod restarts `new-source`,
  `restart-during-move`, `restart-during-write`, `gre-child-kept`). A kept reconnection whose pod no longer
  has the agent's configuration (its rows missing or changed under the same start: a path not
  seen yet) ends the session at once, counted as `config_lost`, rather than after the unserved
  rule's 60 s; the fresh session's M2 is written as §5 allows: a configuration changed under the
  same start is another manager's, and its operation is refused (`OWNERSHIP_CONFLICT`), not forced
  (box `reconnect-config-lost`).
- A radio's operable channel follows its pod's uplink (§9: a further radio's channel is its
  station's; the fronthaul's is the channel of the station it shares the radio with, as a
  legacy pod's GTP station or the Pis' 2.4 GHz fallback, wherever a gateway's automatic channel
  selection puts its 2.4 GHz). When only the radios' operable channels changed, on a kept
  reconnection or on the same source, a provisioned session is kept, as an EasyMesh agent
  reports a channel change without onboarding again: the agent takes the new capabilities and,
  for the radios that moved, sends an unsolicited Channel Preference Report, then an Operating
  Channel Report. The Channel Preference Report names a moved radio's new channel its only
  operable one (every other channel of its band's classes at preference 0, reason 8: operation
  would prevent backhaul operation using a shared radio) only while a station on the radio holds
  its channel (connected to a parent: the pod's Wi-Fi backhaul or its GTP station); a radio with
  no station on it has its normal preferences, none, as a Channel Preference Query is answered.
  Its M1 had named the old channel; the controller learns the new
  one from these. Nothing is written: the pod moved the radio and its BSSes (box
  `fronthaul-channel-moves`, `fronthaul-channel-moves-unheld`, `second-radio-moves-channel`,
  `reconnect-channel-follows`).
- One exception: a radio's maximum EIRP. A source starts with the radio's reported
  `Wifi_Radio_State.tx_power`, or 20 dBm when the pod reports none yet. A reported power above
  the maximum a radio advertises means new capabilities, since a radio cannot transmit above
  its maximum: they are taken again with that power as the maximum, and the session that has
  the old ones ends, the next starting afresh with the new M1s. Its unchanged M2 sets are
  observed applied without writing. A fall changes nothing, so the maximum only rises and a
  pod gets one fresh session per rise, not per change. Before this rule, a Pi whose source
  began before it reported its power advertised 20 dBm and then reported 30, and the channel
  procedures, which refuse a power above the advertised maximum, were refused; RDK renewed the
  agent every 40 s (12 October). Both agents (vectors `translation-northbound.json`
  `advertised_eirp`, box `power-rises`). The agent does not wait for the power before its
  first M1: a radio whose VIFs disagree is never given one (§9).
- A session that fails, loses its source (the pod's State lost, past the reconnection window
  for a provisioned one, or another pod source), or receives a Renew is replaced by a new one.
  Restarts
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
  channel is operable (any of 1 to 13: the channel the station on the radio
  sets, §9);
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
  5 s: a connection that has received nothing for 5 s gets an echo request, and one
  that has still received nothing 5 s later is dropped, its pod source lost (a pod
  gone without a FIN, powered off or rebooted behind a switch, is not held for
  hours as its source; 14 October). Both agents (box `pod-vanished`).
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
| `Wifi_Stats_Config` | `stats_type`, `radio_type`, `survey_type`, `report_type`, `reporting_interval`, `sampling_interval`, `survey_interval_ms` |

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

A transaction's results are read as RFC 7047 (4.1.3) gives them: one per
operation. An operation's error refuses the whole transaction (a failed `wait`:
the guarded rows changed, an ownership conflict; any other: `FAILED`,
`INVALID_INPUT`). Every operation done but the commit refused (a full table, a
broken reference) is one result more, an error: nothing of it is applied, so it is
`FAILED`, `INVALID_INPUT`, as definite as an operation's error and not an unknown
outcome. Either way the operation's commit evidence keeps the database's error
(`database_error`, its RFC 7047 error string, at most 64 characters; never its
`details`, which can quote a value written, a passphrase among them). Results of
any other shape, or none, are an unknown outcome (`INDETERMINATE`).

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
  the pod operates on, whichever it is (finding 24). Preference groups for operating classes the radio does
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
  creates;
- the pod's database generation, `schema`: `opensync-6.6`, or `opensync-2.0` for a
  legacy pod (§3.10).

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

The agent's status also lists the pod's `Wifi_Stats_Config` rows, whoever wrote
them and whatever its telemetry mode (`stats_config`): their number and, per
row, its radio type, statistics type and survey type, its sampling interval when
it takes the radio off its channel (survey type `off-chan` or `full`) and its
`survey_interval_ms`, the time on each other channel. A radio away from its
channel cannot answer its stations, so such a row can delay their associations.

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

### 3.10 Legacy pods: the OpenSync 2.0 schema

Some pods run an OpenSync whose database predates the `wpa_*` columns and
`multi_ap`, whatever version their firmware names. A pod is served as one
when its profile says `"schema": "opensync-2.0"`; the default, `opensync-6.6`,
is everything above. For a legacy pod, all of this specification applies, except:

- **Qualification.** The reference is the unmodified schema of OpenSync
  `osync_2.0.0` (`tests/fixtures/opensync/opensync-2.0.0.ovsschema`, pinned by
  hash) without `Wifi_VIF_Config.multi_ap` and `Wifi_VIF_State.multi_ap`. At
  connect, every column EMOSA uses (§3.2, without the `wpa_*` columns and
  `multi_ap`) MUST exist with the reference's type. An enum column qualifies
  when its values are a subset of the reference's that keeps every value EMOSA
  writes (a pod without `ap_vlan`, say). The generations do not mix: a pod with
  `wpa_psks` is not a legacy pod, and one without it is not a 6.6 pod. Either
  mismatch is refused, `SCHEMA_MISMATCH`, before anything is read or written.
- **Security.** WPA2-PSK is the `security` map: `encryption` `WPA-PSK` and
  `key` the passphrase, after the profile row's own entries (an `oftag`, say).
  EMOSA writes the whole map in one `update` (or in the `insert` of a created
  VIF). A BSS is WPA2-PSK in this encoding while its map's `encryption` is
  `WPA-PSK` and it has a `key`. Guarded AP VIF fields: `if_name`, `mode`,
  `enabled`, `ssid` and `security`.
- **The AP's bridge port.** A legacy pod's wm and its authenticator (Broadcom's
  nas and eapd) hear an AP's stations on the AP's bridge, whose members are
  that bridge's Open vSwitch ports. The pod never adds an AP there: its cloud
  writes the `Port`. `Wifi_VIF_State.bridge` shows the bridge either way, from
  the pod's NVRAM. An AP with no port associates its clients, which then get no
  EAPOL M1 (pods after a hooked boot, which starts with no VIF rows, 15
  October). So each AP of EMOSA's (the fronthaul, a slot) that is on, has a
  `bridge` naming the pod's one `Bridge` of that name, has a `Wifi_VIF_State`
  row, and has no `Port` of its name anywhere gets one guarded transaction:
  the `Bridge`'s `ports` unchanged, no such `Port`, and the AP's `if_name`,
  `enabled`, `bridge` and `security` unchanged; then an `Interface` and a
  `Port` of the AP's name, the `Port` into the `Bridge`'s `ports`, and the
  `oftag` marked (`~reload`, below) unless it already is. This needs no
  restart of the pod, and the agent counts it in its status (`bridge_ports`).
  A `Port` of that name elsewhere is another manager's and is left alone.
- **The bridge mode.** A converted pod's cloud runs it as a bridge. `br-home`'s
  Inet row is up (`enabled`, `network`: its interface, where wm and the
  authenticator listen, is up). `br-wan`'s NAT is off. OpenSync's patch pair
  (§8.4) joins the two, and the home APs have `group_rekey` 86400 and `ft_psk` 0.
  A hooked boot's seed has none of it: `br-home` down, `br-wan`'s NAT on, no
  pair. So clients associate and get no EAPOL M1, and no DHCP reaches the
  router's LAN (15 October). With `radio_plan: emosa`, while the pod has Inet
  rows for both bridges, the agent writes what the pod lacks in one guarded
  transaction, on a new start or a live pod alike:
  - `br-home`'s row set to `enabled`, `network` true and NAT false;
  - `br-wan`'s NAT set to false;
  - the patch pair, when neither of its `Port`s exists anywhere (half a pair is
    another manager's and is left alone);
  - the home APs' (the fronthaul and the fronthaul slots) `group_rekey` and
    `ft_psk`: the cloud's 86400 and 0, which the agents carry themselves (an
    agent deployed alone reads an older profile without them), unless the
    profile's fronthaul row sets others (the bundled ones set the same, so the
    rows EMOSA makes have them);
  - their `oftag`s marked (`~reload`, below) unless they already are.

  Its status counts the writes (`bridge_mode_writes`). The cloud's taps and its
  OpenFlow rules (its analytics and portal features) are not written.
- **The radios' own netdevs.** On a pod converted at boot, the netdev of a radio
  itself (`wl0` under the 5 GHz home AP `wl0.2`) stays down: OpenSync 1.4's wm
  raises a netdev only while applying a VIF on it, and the seed's stations are
  deleted before wm starts. Later OpenSync raises it with every VIF, as a
  Broadcom radio in DHD offload mode passes none of the host's frames otherwise:
  the authenticator's M1 never reached the air (16 October). For each radio
  carrying a home AP whose VIFs are all EMOSA's (the fronthaul, its slots, the
  profile's stations), with no VIF or Inet row on its own netdev, the agent
  writes one `Wifi_Inet_Config` row: `if_type` `tap`, enabled, `network` on,
  `ip_assign_scheme` none, NAT off. nm runs it as a plain interface (the link set
  up, no address, nothing created), and the connection manager, which takes `eth`
  rows for uplinks, leaves it alone. A radio that also carries a VIF EMOSA did
  not make (a cloud-made pod's, whose onboarding raised the netdev) gets none,
  and so does a netdev the profile runs a station on (`wl1`), also while the
  station has no row: wm raises it when it applies EMOSA's station, and the
  station's writer keeps an Inet row it finds there, so the station would come
  back on this row without its address.
  The row brings nm's accept rules for that netdev's own traffic, and one down
  and up of the netdev when it is inserted. The fleet's `phy_netdev_up` (on by
  default, settable per pod) false removes the `tap` rows on the pod's radios,
  and nm sets the link down again. Its status gives the setting, the writes and
  each home AP's radio's `port_state` as nm reports it in `Wifi_Master_State`
  (`phy_netdev_up`).
- **The authenticator's reload.** OpenSync 2.4.3's Broadcom wm restarts the
  pod's authenticator (nas and eapd) in full only for a new AP or a change of
  its `security` entries beyond the key (`encryption`, `mode`, `oftag`). An
  `ssid` or `key` alone is a fast reload. An EMOSA write that makes an AP or
  changes its `ssid` or key gives the map's `oftag` the suffix `~reload` (the
  profile's `home--1` becomes `home--1~reload`; a row without one gets
  `~reload`), a full reload, as do the bridge port's and the bridge mode's
  writes. The agent restores the `oftag` with one guarded `mutate` (the marker
  deleted, the profile's `oftag` inserted), a second full reload, once all of
  these hold: the pod's `Wifi_VIF_State` shows that write (its `ssid`, key and
  marked `oftag`), the AP needs no bridge port, the pod lacks none of the bridge
  mode, and no EMOSA write is in flight. It counts the restore in its status
  (`auth_reloads`). So the authenticator's last restart comes after the AP's
  port and the bridge mode are in place. The marker is the pod's own state: an
  agent restarted in between restores it. The AP stays up (beacons continue; its
  clients reassociate within seconds of each reload). A write that keeps the
  `ssid` and key, and the radio plan's neutral APs (hidden, their Inet off: they
  serve no client), carry no marker. A 6.6 pod has none.
- **Roles.** There is no `multi_ap`, so EMOSA writes none. A BSS's role is its
  slot's (§3.4): the fronthaul VIF and fronthaul slots are fronthaul,
  backhaul slots backhaul, and the BSS Configuration Report flags follow from it.
  With no Multi-AP backhaul BSS on such a pod, a legacy profile's
  `backhaul_mode` MUST be `gre-parent` (§8.6) when it has backhaul slots.
- **The Ethernet uplink** (§8.4) is bridged by OpenSync's patch pair, there being no
  `Connection_Manager_Uplink.bridge`.
- **A GRE child** (§8.6, uplink mode `gre`): its `Wifi_Credential_Config` has no `bssid`,
  `priority` or `enabled`, so a move writes the credential with `ssid`, `security` and
  `onboard_type=gre` only, and pins the parent in the station's own
  `Wifi_VIF_Config.parent`, beside `credential_configs` (credential-list mode, `ssid`
  empty). A linked credential counts as enabled. The station's `parent` is guarded with
  its other columns and snapshotted for the revert, which puts the pod's back.
- **The GTP station** (uplink `bootstrap`). A legacy pod's core makes no backhaul station: `cm`
  selects the `mode=sta` rows it finds, and `wm` gives a station only the credentials its own
  `credential_configs` links. Its database is its vendor's seed again at every start of Open
  vSwitch: the operator's onboarding credentials, unlinked, and no station. So its Wi-Fi path to
  the site's GTP (§8.2) is EMOSA's to write, as a 6.6 pod's bootstrap writes its own. The agent
  does it when its configuration has `uplink.bootstrap`, in any uplink mode a legacy pod serves:
  the GTP BSS's `ssid`, its WPA2-PSK passphrase by `secret_ref`, and an optional `bssid` pin. There
  is no pin by default: a pinned pod would stay on that BSS when the site's GTP BSS changes.
  - **When:** the pod is bound and its uplink station (the profile's `uplink.station`) has no
    `Wifi_VIF_Config` row. A row already there is left alone, whoever wrote it (the pod, an
    operator, a start-up hook). The write is made once per start of the pod's OpenSync (§8.3)
    and credential: the next start writes it again, and so does a changed credential on a start
    EMOSA continues (its start marker read back after an EMOSA restart), going back to an
    earlier one included: the start's latest write stands for its credential, not any earlier
    write.
  - **Write:** one guarded transaction:
    1. `wait` on the serial, and that the station has no `Wifi_VIF_Config` row;
    2. `insert Wifi_Credential_Config`: `ssid`, `security` (`encryption` `WPA-PSK` and `key`),
       `onboard_type=gre`;
    3. `insert Wifi_VIF_Config` of the station: `if_name`, `mode=sta`, `enabled=true`,
       `vif_radio_idx=0`, `ssid` and `security` the GTP BSS's (a bootstrap's vconf form, as a 6.6
       pod's), `credential_configs` the credential, and `parent` the pin if any;
    4. `mutate Wifi_Radio_Config vif_configs` of the station's radio (`insert`);
    5. `update Wifi_Inet_Config` of the station (`enabled=true`, `network=true`; its address
       scheme stays the seed's), or `insert` one (`if_type=vif`, `ip_assign_scheme=dhcp`) when
       the pod has none.

    This is the write that joined a 2.x pod to a GTP BSS on 13 October, made by hand.
  - **Applied** when, on the same start, the station's row is in that form and its
    `Wifi_VIF_State` row exists. A write that fails, or is not applied within 30 s, is not
    repeated on that start.
  - **While wired:** `cm` keeps its Ethernet port as the uplink in use, and builds no GRE until
    the port goes. The station joins the GTP's BSS beside it, then carries the uplink. The
    radio's channel is then the GTP BSS's, wherever the gateway puts it (its automatic channel
    selection): the radio follows the station, and the radio plan follows the radio (below).
  - **The operator's credentials** stay, unlinked: `wm` uses none of them.
  - **Not persisted.** A reboot brings back the vendor's seed, with the operator's redirector,
    and no write of EMOSA's survives it. Bringing the pod back to the fleet after a reboot is
    the pod's own start-up's job.
  - A GRE child's switch (§8.6) treats this station as the pod's bootstrap, in vconf mode: its
    snapshot and revert put it back.
  - **The guard.** The 2.4.3 pods' vendor healthcheck (read on 14 October) runs every minute
    over every `Wifi_VIF_Config` row, enabled or not: an AP must beacon (a BSSID other than
    zero), a station must be associated. Ten failing minutes in a row reboot the pod; one
    passing round resets the count. So a station that cannot associate (a wrong passphrase, the
    GTP BSS gone or out of reach) would reboot a wired pod every ten minutes. The station in the
    scope's form (the GTP's SSID) that is not associated (its `Wifi_VIF_State` has no `parent`)
    for `uplink.bootstrap.unassociated_seconds` (300) in a row is deleted, not disabled (a
    disabled row fails the check too): one guarded transaction deletes its row, its linked
    credentials and its radio's reference. It is written again after a back-off
    (`retry_seconds`, 900, 1800 then 3600, at most three, the last repeated; back to the first
    once a write associates); each attempt costs the healthcheck at most five failing rounds.
    Counted in the status (`gtp_station.removed_unassociated`, `associated`) and journalled
    (`GTP_STATION_REMOVED`). On a pod that is not converted, a station the scope did not write
    (another SSID) is not its to remove. On a converted pod (`radio_plan: emosa`) every other
    station row is guarded the same way (the seed's onboarding stations, a hook's: the vendor
    healthcheck reboots the pod for any of them) and never written again: deleted with what
    only it uses (its radio's reference, the credentials no other VIF links, its Inet row when
    no other VIF has its name), counted (`others_removed_unassociated`), journalled
    (`STATION_REMOVED`) and logged. While the pod is wired the radio plan deletes those at the
    start already (below); this covers a pod on its Wi-Fi uplink.
  - **Why it is written while wired, not on the Ethernet's loss:** the pod's OVSDB session to
    its agent runs over that Ethernet, so once it is gone nothing can be written; the station
    must be there before. **Accepted risk:** a pod cabled to the gateway that hosts its agent
    loses its Ethernet with the gateway, and `cm` restarts its managers from the seed (which has
    no station) after 120 s, two or three failing rounds; but a pod wired through another
    switch, its GTP BSS gone for more than ten minutes while its agent's host is down too (so
    nothing removes the station), reboots once. Its seed has no station, so it is one reboot,
    never a loop.

  Both agents. Box `legacy-gtp-station` (the seed, then the Ethernet lost),
  `legacy-gtp-station-present` (a station already there, left alone) and
  `legacy-gtp-station-unassociated` (the guard).
- **The radio plan** (profile `radio_plan: emosa`, a legacy profile's choice; `pod`, the
  default, leaves it alone). The vendor's seed brings back, at every start, the operator's APs
  on every radio, some with their own address and DHCP server, and the radios' channels as the
  operator left them. With `radio_plan: emosa`, the pod's radio plan is EMOSA's, as a
  controller's is:
  - **Unclaimed APs.** Every `mode=ap` `Wifi_VIF_Config` row that no VIF of the profile names
    (its fronthaul and its slots, on any radio) keeps its VIF on and is made neutral: hidden
    (`ssid_broadcast=disabled`), in no bridge (`bridge` empty, out of `br-home`), on EMOSA's own
    passphrase (WPA2-PSK in the security map: a per-pod secret `emosa-unclaimed`, 32 random
    letters and digits made once by the agent's secret store, never in a log, the status or the
    journal), so the operator's credentials admit no one; and its `Wifi_Inet_Config` row goes off
    (`enabled=false`): its address, its DHCP server, DNS and firewall rules go, and its link stays
    `wm`'s (OpenSync's `inet_vif`: an Inet row never takes a VIF down), so it keeps beaconing (a
    hidden BSS beacons with its BSSID). An AP is never disabled, nor a radio: the vendor healthcheck (see the GTP
    station's guard) reboots a pod whose configured VIF has no BSSID, and disabling `wl0.1` and
    `wl0.2` rebooted a converted pod ten minutes later, twice (13 and 14 October). A VIF the
    profile names is never touched.
  - **The seed's stations.** A converted pod's seed brings back the operator's onboarding
    stations (on `wl0` and `wl1`, the operator's credential linked): seen on 14 October, a
    looping pod's two radios joined another pod's operator backhaul APs. On a converted site
    they associate to nothing and fail the vendor healthcheck (a station must be associated),
    and a station on `wl1` keeps the GTP station from being written. So while the pod is wired
    and the site has a GTP (`uplink.bootstrap`), every `mode=sta` row not on the GTP's SSID (its
    own `ssid` or a linked credential's: the GTP station, or one a start-up hook wrote on the
    GTP) is deleted in the plan's write, with its radio's `vif_configs` reference, the
    credentials no kept station links and its Inet row (when no kept VIF has its name); each
    row `wait`ed on as it is, and logged. The GTP station is then written on the free `wl1`. A
    pod on its Wi-Fi uplink keeps its stations (one may be its uplink). The status shows the
    stations left to delete (`foreign_stations`).
  - **The fronthaul radio's channel.** A station on the radio connected to a parent (the GTP
    station, or the pod's Wi-Fi uplink) holds its channel: the radio follows the station (a
    gateway's channel switch, or its join), and the pod's OpenSync pulls a radio off its
    Config's channel back to that channel, dropping the station, which joins again on the
    gateway's, over and over (the bench, 20 October: ten minutes of it, 32 fresh sessions per
    pod). So the profile's `radio.channel` and `ht_mode` are written on the fronthaul's radio
    only while the pod is wired and no station has held the radio on that start of its
    OpenSync. Once one has, the plan follows the station: its channel, and its width from
    `Wifi_Radio_State` (`follows`), written alone, one write per move of the station, at most
    one in 20 s; a station moved again within those 20 s has its latest channel written when
    they end, when the Config does not have it already. Written, not cleared: on the 2.4.3
    seed a radio with no channel in its Config did not beacon until one was set (14 October),
    and the vendor healthcheck reboots a pod whose enabled AP does not beacon. The followed
    channel stays when the station is gone (a gateway's outage), never the profile's again on
    that start: a gateway under automatic selection may come back on another channel, and the
    station looks for it on every channel. Nothing else of the plan is written again (one
    write per start). The session is kept and the move reported (§2.5, §9). The status shows
    the radio's station channel and the plan's follow (`radio_plan.station_channel`,
    `follow`, `follows`). A fronthaul created cold on a radio a station holds leaves the
    radio's channel and `ht_mode` alone, on any pod. Box `legacy-radio-follows-moves`,
    `legacy-radio-follows-quick-moves`, `legacy-radio-follows-outage`.
  - **Every enabled AP beacons.** The vendor healthcheck fails an enabled AP that does not beacon
    (14 October: "wl0.2 is not beaconing" until the operator's cloud set the 5 GHz radio). A
    seed's radio has no channel in its Config until its cloud sets one; on a hooked boot no cloud
    comes, and the 2.4 profile neither binds nor places `wl0`, whose unclaimed APs stay enabled.
    So a profile names its idle radios (`idle_radios`: a band it neither binds nor places, each
    with a channel EMOSA maps and an `ht_mode`; the 2.4 profile's `wl0` at 36, HT20), and the
    plan turns such a radio on (`enabled`, the channel and `ht_mode`) when it carries an enabled
    AP and its `Wifi_Radio_State` shows it off or with no channel. A radio that beacons is never
    moved, whatever its Config says. The status shows the idle radios left dark (`dark_radios`).
  - **The operator's cloud.** A converted pod talks only to EMOSA (the owner, 14 October). The
    seed points `qm` at its operator's cloud: `AWLAN_Node`'s `mqtt_settings` (the broker, port
    and topic), `mqtt_headers` and `mqtt_topics`, and `Wifi_Stats_Config` rows asking the pod's
    managers for the statistics it publishes there. The plan empties the three maps and removes
    every statistics row, so `qm` has no broker and nothing is sampled for one. The agent's own
    telemetry (§3.6) is off on a converted pod, a fleet's `telemetry` ignored with a warning in
    the log: nothing of EMOSA's takes the cloud's place. What the vendor's own scripts send past
    OpenSync (a log upload, say) is not the agent's to change.
  - **Write:** once per start of the pod's OpenSync and plan (a start EMOSA continues gets one
    more when the plan changed, going back to an earlier plan included), in one guarded
    transaction: `wait` on the
    serial and on each changed row as it is (`if_name` and `mode` of a VIF; `if_name` and
    `enabled` of an Inet row; `if_name`, `channel` and `ht_mode` of the radio, `enabled` too for
    an idle radio; the node's three
    MQTT maps; `stats_type` and `radio_type` of a statistics row; `if_name`, `mode` and `ssid`
    of a station row), then each `update` (a statistics row's, a station's and its rows'
    `delete`, its radio's `mutate`). Nothing is written when nothing differs. **Applied** when, on the
    same start, every unclaimed AP is neutral (`unclaimed_exposed` empty), its Inet row off
    (`unclaimed_inet_on` empty), the operator's cloud cleared (`operator_cloud` empty: none of the
    three maps set, no statistics row), no seed station left (`foreign_stations` empty), no idle
    radio dark (`dark_radios` empty: `wm` shows each on a channel) and the radio's Config has the
    profile's channel. Not
    repeated on that start, even when a row is changed again: the next start writes it again. The
    status shows the unclaimed APs left on (`unclaimed_enabled`), those not neutral yet, their
    Inet rows still on and what is left of the cloud. The vendor's target may rebuild a VIF to
    hide it or change its key: a few seconds without beacons, at most one failing healthcheck
    round.

  Both agents. Box `legacy-radio-plan` (every VIF beaconing or associated, the box's terms for the
  vendor healthcheck: an AP's State on and its radio's State on a channel), `legacy-cloud-cleared`
  (the cloud cleared on the start and again after a re-seed, the fleet's telemetry ignored),
  `legacy-seed-stations` (the seed's stations deleted, only the GTP station left, on the start
  and after a re-seed) and `legacy-dark-radio` (`wl0` shown with no channel turned on at 36, on
  the start and after a re-seed).
- **Its starts, marked in its database.** A legacy pod's database is its vendor's seed
  again at every start of its OpenSync (a reboot, or its connection manager restarting the
  managers after more than 120 s without its uplink), every row with the UUID it had in the
  seed, its radio rows too: seen on a converted pod on 14 October, one start instance (§8.3)
  across 13 October's configuration and the next day's re-seed, so the operator's rows read as
  another manager changing EMOSA's configuration under the same start, every M2 refused
  (`OWNERSHIP_CONFLICT`), and the GTP station and the radio plan taken as written. Across that
  pod's reboot the same day only its `Wifi_Radio_Config` rows kept their UUIDs; its radios'
  State rows and its VIFs' Config and State rows were new. Those tell a reboot apart, but a
  re-seed within one boot (the managers restarted) has not been sampled, so the agent does not
  rely on them. The radio rows' UUIDs are the seed's, the same on every unit of that firmware
  (a second converted pod showed exactly the first one's), so their start instance is the same
  for every such pod: neither it nor those UUIDs may serve as a device's or a session's
  identity across pods. Nothing does: a journal is one agent's, for one pod. So on a legacy
  profile the agent marks each start of the pod's OpenSync in its database: the **start
  marker**, a `Node_Config` row of EMOSA's own module (`emosa`, key `start`) with a random
  16-hex token, never `persist`. No seed has it, and none of the pod's managers reads it
  (OpenSync 1.4's core reads `Node_Config` in its demo manager only, 2.4.3's readers each their
  own module, and the hook module's rows have been on the converted pods since 15 October with
  no reaction). A re-seed or a reboot loses it; an EMOSA restart reads it back. The start
  instance (§8.3) is the radio rows with the marker's token, and a legacy pod without a marker is
  not ready for any scope: the agent writes one at its next refresh of the pod (a guarded
  insert, only where the pod has none; again at most every 5 s in one database generation until
  it shows), and no operation, reconcile or scope reads the pod meanwhile. A new token (a
  re-seed's, a reboot's) is a new start for every scope: a new ownership period (§5, a conflict
  recorded on an earlier one released), the controller's M2 written again where the pod lacks
  it, and the GTP station and the radio plan written once more where they differ: the pod
  claimed again, as at its first admission, with no hands on it. An EMOSA restart, a fresh
  session (§2.5) and a kept reconnection that found its configuration lost (`config_lost`) stay
  on the start: a configuration changed under it is another manager's (§5), and that conflict
  outlives EMOSA's restarts. Before 18 October the start was one of the agent's own sessions, so
  each EMOSA restart released such a conflict and wrote over it, and each fresh session wrote
  the once-per-start configuration again. The module name `emosa` is refused for a configured
  module (§4, the pods' modules). The status's `session_starts` counts the starts the agent saw
  (a legacy profile's only, its own first), `start_marker` says whether the marker is in use and
  how many the agent wrote, and the agent's log says each ("its configuration claimed again",
  "the pod's start marked"). A pod whose database keeps refusing the marker stays not ready (it
  fails safe: nothing else is written to it), and `start_marker` says since when
  (`pending_since`, Unix time, null while the pod has its marker) and why (`last_error`: the
  last failed write's reason with the database's own error, as `INVALID_INPUT: constraint
  violation` for a full table, a refusal at commit (§3.2); null again once the marker is read
  back; each try also in the log). A legacy schema without `Node_Config` falls back to
  the agent's own token, renewed for its first session, every fresh session and a `config_lost` reconnection.
  A 6.6 pod's start is its radio rows alone, as before. Both agents. Box `legacy-reseed-reclaim`,
  `legacy-reseed-reclaim-5g` (wl0 back on 36 at HT80 with its home AP),
  `legacy-reseed-reclaim-late` (past the reconnection window), `legacy-start-marker` (the
  marker read back after an EMOSA restart: the same instance, the M2 found applied, an ownership
  conflict kept across the restart; a re-seed's new marker releasing it, the M2 written again),
  `legacy-start-marker-refused` (the marker refused: no other row written, the status's
  `pending_since` and `last_error`, the pod configured once the database takes it) and the
  upgrades from the release before the marker (`lab/upgrade-from`), the pod's OpenSync not
  restarted: `upgrade-legacy-parent` (a new start and a remap, no conflict, the marker the one
  row written, the child's tunnel untouched) and `upgrade-pi-parent` (a 6.6 parent: the same
  start, a remap only, nothing written).
- **Not served** on a legacy pod, refused at the agent's start
  (`INVALID_INPUT`) or turned off and said so in the status:
  the uplink switch onto a Multi-AP backhaul (§8.3: no Multi-AP station on such a
  pod); client steering (§3.7) and
  the station watch (§3.9), whose `Band_Steering_Clients` columns do not
  exist. Statistics (§3.6) are served: their columns are the same.

## 4. The fleet

For each connection on the front port, the fleet:
1. selects `AWLAN_Node` and requires exactly one row with a usable serial:
   letters, digits, `.`, `_` and `-`, at most 64 characters;
2. refuses the pod, leaving it unchanged, if it is not admitted or if no port is
   free. A pod is admitted when `admit` is `*` or lists its serial, or when the
   fleet's `admit_by_model` is on (it is off unless set) and one of its `models`
   recognises the pod (below);
3. finds or creates the pod's registry entry: the AL MAC (§2.2), the first free
   port (for a pod on an agent port, the port it dialled: re-admission, below), and
   interface `em<index>`;
4. writes the agent configuration; if it changed, restarts the agent,
   otherwise starts it;
5. writes `manager_addr` and ends the session.

The whole exchange MUST complete within 5 s. A pod returning later gets the
same entry.

**Re-admission on an agent port.** A pod may dial an agent port no registry entry holds: a
factory reset loses the registry with the agents' state while the configurations come back,
and each pod keeps the `manager_addr` its agent was given. A serving fleet MUST listen on each
agent port no entry holds (on loopback, as the agents do, §3.1: the forwarder or the labs'
proxy brings the pod there), at most 128 of them, and keep those listeners in line with the
registry, looking again at least once a second (a `forget` run as another process frees its
port). A pod that connects there goes through steps 1 and 2 as at the front port. In step 3 its
entry, when it has none, is made on the port it dialled instead of the first free one, with the
interface that port's place in the range gives and the AL MAC its serial gives (§2.2). So each
pod gets its port, interface and AL MAC back, whatever order the pods come back in, and the
same agent identity reaches the controller. A pod with an entry is handed over to its own port,
whichever it dialled. A pod on a port another pod's entry holds is refused and left unchanged:
that pod's agent is behind the port, and it acts on no other pod (§3.1). Before an agent starts
on a port (step 4, after either way in), the fleet stops listening on it. With the site's
GTP underlay (§8.6) the fleet's step 1 also selects the pod's `Wifi_Inet_Config` rows, and a
GRE parent seen for the first time keeps the underlay its parent AP's row already has (static,
the underlay's .1 with netmask 255.255.255.0, a 169.254.N.0/24), when the rows name one, no
entry uses it and it is clear of the GTP underlay: its children's tunnels stay up. One taken, or
over the GTP underlay, is allocated as before. Both fleets (conformance `fleet-sessions`:
`readmitted-on-the-ports-they-dial-after-a-lost-registry`,
`agent-ports-taken-or-dialled-by-a-registered-pod`, `agent-ports-admit-as-the-front-port`,
`gre-parents-keep-their-underlays-after-a-lost-registry`; box `fleet-readmit`,
`fleet-readmit-pods`).

A serving fleet keeps each valid configuration it starts on as the last valid one
(`fleet-config.valid.json` under its `state_root`). One that fails its checks (its
schema, a module's update without `files`, or not JSON at all, a partial write) is
rejected. If the `state_root` it names keeps a valid one (the platform's,
`EMOSA_STATE_ROOT` as `/etc/default/emosa` gives it, when it names none), the fleet logs
the rejection once, records it (`fleet-config.rejected.json`: the file, its error, when)
and serves the last valid configuration. So one bad key never takes the pods off EMOSA:
the fleet does not end, and its service manager does not start it again and again (15
October). With no valid one kept, it ends as before. A valid start removes the record.
`list` and `forget` read the configuration as given. A tool that writes it writes a
temporary file and renames it over the configuration (as the lab's deploy library and
the router's apply script do), so the fleet never reads half a file.

When it starts, the fleet MUST do step 4 for each admitted entry its
registry has, in the registry's order, without waiting for the pod: an image
upgrade keeps the registry and the configurations on persistent storage but not
the agents' enabled units. The registry file is the fleet's state: a serving fleet MUST read it
again for each pod, so a `forget` run as another process takes effect at once. With `run_root` each agent's configuration has `run_dir` `<run_root>/<pod_id>`. The agent configuration takes the fleet's settings (`message_set`,
`multi_bss`, `m2_session`, `profile`, `uplink`), overridden by the pod's model's
(`models`) and over that per pod by `pods.<serial>`, and the fleet's
`topology_query_window` (§2.5), a property of the
controller, when it has one: a different pod model needs its own profile, and the uplink
switch (§8.3) is enabled per pod.

**The pods' models.** A pod reports its model and firmware (`AWLAN_Node`'s `model` and
`firmware_version`, which the registry keeps with its entry). The fleet's `models` is a list,
in order of preference, of the models it knows: each names a `model` (equal to the pod's) and
optionally a `firmware_prefix` (the pod's firmware starts with it), with the settings a pod of
that model and firmware takes (the keys of `pods.<serial>`: a converted legacy pod's profile,
§3.10, say). The first that matches recognises the pod, and its settings apply to it unless
`pods.<serial>` sets them. A pod that reports no model is recognised by none, and one that
reports no firmware only by an entry without a `firmware_prefix`. So a site
names a model's profile once, and every pod of that model gets it, admitted by serial or by
its model. With `admit_by_model` on, the fleet admits every pod one of its `models`
recognises, whatever `admit` lists: a router whose owner turned it on takes any converted pod
of a model it knows, with no serial written down first, and a pod of another model is refused
as before. The fleet's log of a handover says how the pod was admitted (`serial`, `model` or
`all`) and, for one recognised, which `models` entry applies. At the fleet's start (above) the same rules
hold with the model and firmware its registry keeps. Both fleets. `forget SERIAL` stops the agent, deletes the entry and the
configuration, and archives the agent's state directory as
`<pod_id>.released-<stamp>`, without the agent's status (a running agent's: in its run
directory, its link would show the next agent's). A pod handed over
again starts a new ownership period, so conflicts recorded before its release
don't block it.

**The fleet's secrets.** With `secrets` (a directory: the fleet's own secret store, the files
backend of §6) a secret a site shares among its pods is kept once per gateway: the GTP BSS's
passphrase (a legacy pod's `uplink.bootstrap.secret_ref`, §3.10) or a backhaul credential
(`uplink.secret_ref`). Whenever the fleet writes an agent's configuration (step 4, and at its
start), each reference the configuration names that the agent's own store lacks is copied from
the fleet's: created (mode 0600, the agent's `secrets` directory 0700), never replacing one the
agent has, durable. A reference with no source, in neither store (the agent's alone when the
fleet has none, as after a factory reset that lost the agents' state), fails loudly without
taking any pod off EMOSA: it is logged as an error by reference, each time the fleet writes that
agent's configuration, and recorded under the state root (`fleet-secrets.missing.json`: each pod
and its references with no source, owner-only) until it has one; the agent starts all the same
and says what it lacks. The value is never logged. So a pod forgotten and admitted again, or a
new one, needs no secret placed by hand, and an image that keeps a shared secret elsewhere (the
gateway's own pod-backhaul key, say) fills the fleet's store from it at boot. Both fleets.

**The pods' modules.** A pod may run software of its own that EMOSA manages through OpenSync's
`Node_Config` and `Node_State` (a start-up hook kept on a converted pod's flash, say: its
redirector, its own updates). The fleet's `modules`, replaced per pod by `pods.<serial>.modules`,
names each module (the site's name, `[a-z0-9_-]`, at most 32, never `emosa`: EMOSA's own,
a legacy pod's start marker, §3.10) with its settings (`config`: key
to value, `[a-z0-9_]` keys, values of 1 to 1024 characters) and, optionally, a file to install
(`update`: its SHA-256). The agent's configuration gets each module's settings as a map
(`modules`), with `update_url` (`http://<advertise>:<files.port>/<sha256>`) and `update_sha256`
for an update; the fleet refuses an update without `files`.
- **The agent** writes each module's settings once per start of the pod's OpenSync (its start
  marker's on a legacy pod, §3.10) and configuration: a start EMOSA continues (the marker read
  back after an EMOSA restart) gets one more write when the settings changed meanwhile (a new
  operation; what already matches is not written again; 19 October, the bench), going back to
  earlier settings included (the start's latest write stands for its settings, not any earlier
  write: a rollback is written), in one guarded transaction, with its own journal
  (`node-config`): a `Node_Config` row per setting (`persist` true), a changed value updated, a
  key it no longer sets or a second row of one key removed, each `wait`ed on as it is, and a new
  key's insert guarded by its absence. Another module's rows are never touched. Applied when the
  module's rows are its settings and nothing else, on the same start; not repeated on that
  start. It reads each configured module's `Node_State`, never writes it, and puts in its status
  (`node_config.modules`): whether the settings are written (`settings_written`), the settings
  the module echoes with the value written (`settings_echoed`, a `Node_State` row with the
  setting's key), `state` (`ok` or `error:<reason>`), `boot_id`, `version` (the SHA-256 of the
  module's installed file), `running` (the one its running process started from), `slot`, and
  `update` (`ok` or `failed:<reason>`) with `update_sha256` (the request it answers), and
  `events_prev` (the module's events before the pod's last boot, as the module's own log on flash
  has them, when it reports them: a converted pod's hook since its r4, 18 October). The `flags`
  say what they mean: `state_error`; `not_started_at_boot` (`slot` `none`); `update_pending` (no
  answer to this update yet); `update_failed`; `restart_not_taken` (the update installed, `ok`,
  and still not running 60 s later). Its `events` (`<seq> <word>[:<detail>]` entries joined by
  `;`, newest last, `seq` rising within a boot) are logged once each, by boot and `seq`.
- **The file server.** With `files` the fleet serves plain HTTP on its advertise address only,
  port `files.port` (6650 by default): `GET` or `HEAD /` answers 200 with an empty body, and
  `/<sha256>` the file of `files.directory` with that name, when it is a regular file of at most
  1 MiB whose content has that SHA-256 (checked at each request); anything else is 404. Every
  reply carries a `Date` header in GMT, the gateway's clock. Each request is logged (the peer,
  the path, the answer; `/` only when it is not 200); the files are listed at the fleet's start,
  those not served with why. A gateway's LAN bridge may get the advertise address minutes after
  the fleet starts (seen 14 October): while it cannot listen, the fleet serves its pods and tries
  again every 10 s, logging the first failure and the listener's start. No file is pushed: the
  module fetches it, checks it and installs it, and reports in `Node_State`. The fleet's secret
  store (`secrets`) is only read: a secret is copied into an agent's store, never moved. Both
  fleets.
- **The pods' time.** `HEAD /` on the file server is the pods' time source: a converted pod has
  no clock that survives a reboot (no RTC) and gets none without its operator's cloud, and its
  vendor's healthcheck reboots a pod whose clock is not set (14 October). Its hook reads the
  `Date` of `HEAD http://<advertise>:<files.port>/` and sets the clock from it, refusing a time
  earlier than the one it saved or its own build's (a gateway whose clock is not set yet). The
  fleet serves only a time it trusts: while the gateway's clock is not synchronized no reply
  carries a `Date` (RFC 7231: a server without a reliable clock sends none) and `GET` or `HEAD /`
  answers 503 with `Retry-After: 60`, so the hook keeps its saved clock; files are still served.
  Synchronized is the kernel's clock status, as a time daemon keeps it (`adjtimex(2)` without a
  change, not `TIME_ERROR`: what `ntpq -c kerninfo` shows; no daemon running leaves it
  unsynchronized), or, when `files.time_synced_command` is set (a platform whose daemon does not
  keep the kernel's status), that command's exit status 0. Checked at most once a minute, each
  change logged. So the gateway's own clock must be set by NTP (on its WAN, or a lab's server).

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
  written on (the start's radio rows, as for the uplink switch, §8.3; a legacy pod's,
  whose radio rows do not change, with its start marker, §3.10). When the
  pod's OpenSync starts again, its database comes back from the template without
  it: that is not another manager's change, so it is no ownership conflict, and a
  conflict seen on an earlier start no longer blocks the pod (a new ownership
  period). The agent configures the pod again from the controller's next M2
  (§2.5). The operation's evidence names the start (`instance`). EMOSA's own restart
  is not a start of the pod's OpenSync: a conflict recorded before it still blocks the
  pod after it, on a legacy pod too (its start marker, §3.10).
- So an active operation whose write belongs to an earlier start than the one the
  pod runs now can never be observed applied: it ends `FAILED` (`NOT_READY`, as a
  write whose database generation changed) as soon as the agent sees the new
  start, and does not hold the new start's M2 `BUSY` until its deadline. Before
  this rule, a restart in the second between a write and its observation left the
  pod unconfigured until a Renew (the box, 12 October). Both agents (vectors
  `engine.json` `restarted-before-observed`, box `restart-during-write`).
- A written configuration also belongs to the mapping it was planned under (its
  plan's `mapping`: the pod's profile, by its ID and its file's content,
  `<id>@<the first 12 hex of the file's SHA-256>`). When the pod's profile changes (a pod
  promoted to GRE parent while onboarded, §8.6, or an image whose profile changed, the
  same pod's OpenSync running on: §3.10's start marker keeps its start across EMOSA's
  restart, 18 October), what an earlier operation wrote is
  read under the new mapping, where it may no longer hold: that is not another
  manager's change either, so no ownership conflict, and the next operation begins
  a new ownership period. An earlier operation's deadline still runs. Any change to a
  profile file's bytes is such a remap, a layout change too: one new ownership period
  per pod on that profile, at the first operation the agent plans under the new file
  (an image that changes a profile, or the first image with this mapping, whose earlier
  plans had the ID alone). A configuration the pod already runs is still not written
  again.
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
| Reconnection window (a provisioned session held for its pod's link) | 60 s |
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

**Scenarios.** A production deployment runs either real EasyMesh extenders only (**(a)
extenders-only**) or OpenSync pods only (**(b) pods-only**, EMOSA's own case and V54's physical
shape), never both. A lab may run both (**(c) mixed**); that is not a production case and may take
shortcuts. The design proves **(b)** first. **(a)** MUST NOT regress, since EMOSA's controller patches touch
the controller that also serves the native extender mesh. **(c)** only needs to work, and needs no
per-pod backhaul withdrawal that spares the extenders: §8.6's network-wide withdrawal stands. A rule
that serves one scenario rather than all three names it as `(a)`, `(b)` or `(c)`. Both pod data-plane
mechanisms are **(b)**: the EasyMesh backhaul move of §8.3 (layer A, pod onto pod as a 4-address
Multi-AP station, for radios that can do it, such as hwsim) and the GRE parent/child of §8.6
(layer B, for radios that cannot, such as the Pis' MT7921U, and for multi-hop).

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
- MUST NOT end a tunnel on a step of the gateway's clock: dnsmasq ends leases by the
  wall clock, so a forward step (an NTP sync after hours unsynchronized; 14
  October: 43 min ended the Pis' 1 h leases while they held them) ends them early. The
  reference keeps each lease's last start (`add`, `old`) on the monotonic clock
  (CLOCK_BOOTTIME, in `<state_dir>/lease-times.json`, this boot's only). A `del` that comes
  before the lease ran its length there, beyond a margin (60 s, at most a quarter of the
  length), keeps the tunnel and is logged; a reconcile keeps it too. A kept tunnel goes
  once a whole lease passes without a renewal (the pod left), at the next lease event or
  reconcile. dnsmasq does not tell a release from an expiry, so a released lease's tunnel
  stays that long, harmless: a later lease of the address replaces it;
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
  before then is reverted with it. Only that switch holds. A GRE child (§8.6) has no
  configured upstream: after such a failed switch, or a failed move from its bootstrap, the
  agent undoes its own write while the station still carries the failed credential on that
  start, as the revert to the bootstrap does (the station's snapshot back, its credential
  deleted). The undo is confirmed once the credential is off the station, whichever uplink
  `cm` then uses, and made once per failed switch. A pod whose fallback station keeps its
  uplink working never restarts to drop the credential, and the station would stay pinned to a
  parent it cannot join (the Pis, 13 October). Both agents.
- **A kept target on another fleet pod waits for that pod's AP** (19 October: a converted
  pod rebooting in a gateway outage brought its parent AP back four minutes after its child's
  kept switch had timed out, and the child never tried again). Each agent's status says which of
  its pod's APs are up (`ap_up`: every AP it has seen, kept across its restarts, and its GRE
  parent APs, true while enabled in the pod's `Wifi_VIF_State` on its current start with its
  report source live), and a child reads its fleet's as it reads the parent set (§8.5). While
  the pod that has the kept target's own BSS has it down, the switch is not made and no deadline
  runs; the status says `kept target waiting for its parent`. Another of that pod's APs up is no
  reason to switch. Once it is up, the switch is made with its deadline. A switch made with the
  AP up that is not applied is undone as above (the pod on its bootstrap meanwhile, no switch to
  the configured upstream) and made again after `kept_backoff` (2 minutes, doubled after each);
  the target is given up only after `kept_tries` (3) such failures. A switch the AP went down
  during counts none, and the target waits again. The wait is bounded by `parent_wait_cap`
  (30 minutes). A kept target is a fleet pod's once a fleet agent has named its AP in `ap_up`
  (when kept, while kept, or when a switch to it fails), or it is a GRE parent's, or the move
  onto it is a GRE one; `target.json` remembers it (`fleet`). No agent naming such a target's AP
  is no proof it left (19 October: on a gateway reboot every agent starts again at once, and a
  parent whose `aps.json` was not written yet, re-seeding, named its AP only after its M2): it
  counts as the AP down, so the switch waits and a failed one counts none. Only a target no fleet
  agent has ever named keeps the rule above: one switch, forgotten when it fails. A fleet pod's
  target given up (its tries used, or the wait capped) is parked, not forgotten: the uplink back
  on its configured upstream, `target.json` keeping the target, and it is taken up again, its
  tries anew, once its parent's AP is reported up after it was reported down (an AP up
  throughout is no reason, nor one no fleet agent names for a while, as when the agents start
  again), at most `kept_rearms` (3) times; then it is forgotten. The parked state and the count
  are kept across EMOSA's restarts, which take a parked target up no sooner. Each give-up's log
  says which case it was (no fleet agent names it, its tries failed, its parent not back) and
  whether the target is parked or forgotten. A new move of the controller's, or the kept target
  applied, clears the tries and the count. Both agents.
- **A GRE move onto another station: make-before-break.** A GRE move's switch (§8.6) writes only
  its own station; `cm` keeps a working uplink in use and moves among equal ones only at its
  periodic pass (two minutes on the Pis' `cm2`, 17 October), so with the old station still up the
  move would wait for that pass. Once the move's station is on the target (its State's `parent`)
  and its own gretap (`g-<station>`) has L2 and L3, the agent takes the station `cm` uses and any
  other station EMOSA's switch wrote (its `ssid` "", the move's marker) off in one guarded
  transaction: disabled, and, one EMOSA wrote, its snapshot put back and EMOSA's credential
  deleted, as the revert does. The pod's own other stations (a fallback station, §8.6) stay as the
  pod set them. `cm` then loses its uplink and takes the new gretap at once, within the switch's
  90 s; also when `cm` took it by itself, the old station is taken off, once per switch the agent
  wrote. The move is applied only once `cm` uses the move station's own gretap. Losing the parent
  then means the pod's own fallback, or its restart to its bootstrap (GTP) path. A legacy pod
  (§3.10) has one backhaul station (`wl1`), and its vendor healthcheck reboots it for a disabled
  station row: nothing is taken off there.
- **A failed GRE move onto another station** is answered with a failure and returns to the
  previous upstream with its station (re-pinned without a write when it still carries its pin).
  The switch back does not disable the move's station, which would otherwise join its target
  later and take `cm` (the V54 bench, 17 October): the agent undoes its own write there first,
  the station's snapshot back (its `enabled` with it: a station the pod kept off is off again),
  EMOSA's credential deleted. The snapshot is each station's own, taken when EMOSA's switch first
  wrote it from the pod's configuration. A failed switch whose make-before-break took stations off
  enables them again (one guarded write, on that start): the pod's own as the pod had them, EMOSA's
  with their snapshot back, on the pod's own network until the re-pin (which waits for a working
  uplink). So a GRE child moved off its bootstrap station is not left without one when there is
  no upstream to go back to; box `pi-gre-move-fails-after-take-off` (from the GTP and from a 6.6
  pod's parent). A pod left without any uplink is unreachable for the agent too: its OpenSync's
  own restart to the bootstrap stays the fallback.
- **The uplink record follows `cm`.** The station the uplink is on is the one `cm` uses (the
  station itself, or its own gretap); only with none of the bound stations in use does the one in
  credential-list mode, then the bootstrap one, stand in. A GRE child whose `cm` uses another bound
  station's own gretap than the record's (the pod's own fallback, or `cm`'s pass) has its record
  follow it, with no write: onto EMOSA's pin when that station carries one, else, on the same
  start, onto its bootstrap (no EMOSA upstream, the kept target dropped). The reported backhaul
  (§8.6, "How the controller reaches a pod") already names that station, so the controller learns
  it from the pod's next topology. A GRE switch whose station already carries the same pin and
  credential, enabled, writes nothing: a steer to where `cm` already is moves nothing on the air. A
  finished move's record is the last start's, cleared from the status on the pod's next start.
- **Refusals in the log.** Each refused Backhaul Steering Request is logged with its code, reason,
  station, target, class and channel, not only counted in the status. Both agents; box
  `pi-gre-move-at-cms-pass`, `pi-gre-move-fails-undone`, `pi-gre-record-follows-cm` (a GRE child of
  the Pis' shape: its bootstrap on its 5 GHz station, the 2.4 GHz one off) and `legacy-gre-move`.

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
- **A legacy pod (§3.10)** has no `Connection_Manager_Uplink.bridge`: its `cm` adds the
  uplink port to its WAN bridge (the profile's `wan_bridge`, `br-wan`) and takes the pod as
  a bridge when OpenSync's patch pair joins that bridge to the home bridge. `cm` neither
  makes nor removes the pair. The agent writes it, in one guarded transaction: `wait` on
  the serial, on the port's row (`if_name`, `if_type=eth`, `is_used=true`) and that no Port
  `patch-w2h` or `patch-h2w` exists → `insert Interface` `patch-w2h` and `patch-h2w`
  (`type=patch`, `options` `peer` the other) → `insert Port` of each → `mutate Bridge`
  `ports` of the WAN bridge (`patch-w2h`) and of the fronthaul's bridge (`patch-h2w`).
  **Applied** when both Ports are in their bridges. A pair already in place is the same
  bridging, whoever made it; half a pair (one Port without the other) is another
  manager's, left alone, and the write refused. The pair stays across the pod's starts
  (its Open vSwitch database keeps it), so nothing is written again while it is there.

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

### 8.6 A pod as the GRE parent of other pods

*Approved on 9 October (plan 9.5, layer B). Implemented in both agents and proven pods-only on
rdk-1010 (12 October, below); the physical run on the Pis is V54.*

§8.5 needs a 4-address Multi-AP link between the pods, which some radios cannot carry (a USB
adapter whose driver has no 4-address station or AP). Layer B gives a pod on such a radio the role
the gateway's GTP has (§8.2): its children join it as 3-address stations, as they join the
gateway, and their `cm` builds its gretap to it. The recipe is what the OpenSync cloud writes for
a parent pod, as opensync-lab's local-noc does (`mesh.py`, `parent_step` and `gre_step`).

- **Which backhaul.** The pod profile (§3.5) says how the pod's radios carry a backhaul BSS:
  `backhaul_mode` `multi-ap` (§8.5, the default) or `gre-parent` (this section). The controller
  decides where backhaul BSSes are, as before: a backhaul BSS in its applied M2 set for a radio
  of a `gre-parent` pod is created as the pod's **parent AP** on that radio, not as a Multi-AP
  BSS. The agent reports it as a backhaul BSS (§3.3), so the controller can steer a child onto it
  (RDK's controller takes the role from its own backhaul SSID, unified-wifi-mesh 0247). This is
  standard EasyMesh, the controller configuring backhaul BSSes through M2, as in §8.5. A child
  moves only when the operator directs it, by the controller's Backhaul Steering Request
  (`SteerWiFiBackhaul`): RDK's controller does not choose a pod's parent by itself (the owner's
  ruling of 8 October holds).
- **The parent AP** (profile `gre_parent.vif` over the backhaul slot's row): `mode=ap`,
  the M2's backhaul SSID and passphrase (`wpa_*` columns), `multi_ap=none`, no `bridge`,
  `ap_bridge=false`. The SSID is hidden by default, as the OpenSync cloud keeps a backhaul;
  broadcasting it is the profile's choice (`ssid_broadcast`). A child's station still matches
  its parent by SSID, the BSSID pin only narrowing the match, so a hidden parent depends on its
  Probe Responses carrying the SSID. Every parent AP of a site carries the same backhaul SSID and
  passphrase, whatever its pod's generation, so a station holding them without a pin joins
  whichever parent it hears best, and its lease and tunnel go with it: with mixed parents, a
  station's parent is the one it hears best unless pinned (14 October, a station renewing one
  converted pod's underlay lease on another's parent AP). The Pis' MT7921 answered a child's directed probe with the
  hidden beacon's empty SSID, below hostapd (13 October), and the child never joined, so the
  gre-5g profiles broadcast it, as a gateway broadcasts its own backhaul BSSes; hiding protects
  nothing, as every association request carries the SSID. The legacy GRE profiles (§3.10) broadcast
  their parent AP `wl1.1` too: a station finds a hidden parent only by probing for it, and a Pi's
  moves onto a converted pod's hidden `wl1.1` spent 31 and 48 s of the 90 s deadline scanning (two
  and four scans finding nothing, 17 October), its move back to a broadcasting parent 12 s in
  all. Only the parent AP changes: a legacy pod's fronthaul broadcasts as before, its unclaimed APs
  stay hidden, and an AP beacons either way, so the vendor healthcheck (§3.10) sees no change.
  When the parent AP is made from the seed's row, the flag rides on that write. A pod converted
  before (its `wl1.1` written hidden by an earlier image, with the M2's SSID and key) gets it
  too: the slot's Config row is the parent AP only with the profile's `ssid_broadcast` as well
  (`gre_parent.vif`'s, hidden when it sets none), so the hidden row keeps a role of its own, the
  intent is not satisfied and the first M2 after the new image (the agent's start) writes the
  same row once; after that the set is applied (`observed_noop`) and nothing is written or
  taken as drifted. The row's State is judged without the flag, which a pod's `wm` need not
  echo, else the write would never be seen applied. That one write may rebuild the VIF (a few
  seconds without beacons, at most one failing healthcheck round, as for a key change). Both
  agents (box `legacy-parent-unhide`).
  The parent AP is named `b-ap-<band>` (`b-ap-24` on the 2.4 GHz radio,
  `b-ap-50` on 5 GHz), as the OpenSync cloud and `local-noc` name it, so the pod's own
  fallback can find it. The slot is the parent AP only as a parent renders it: a Multi-AP
  backhaul BSS left in it (a pod promoted to GRE parent while onboarded, its profile changed and
  its agent restarted) is not the applied configuration, so the controller's unchanged M2 set
  converts that same row and writes its underlay Inet row in one transaction (§5: a new mapping,
  no ownership conflict). Both agents (box `gre-parent-promoted`). A parent demoted (its profile
  back to the plain one) is the mirror: the same row is made a Multi-AP backhaul BSS again and its
  Inet row the profile's slot row, the underlay's address and DHCP server cleared, since a DHCP
  server left on a Multi-AP backhaul would hand underlay addresses to whatever associates there.
  The agent of a former parent (its tunnels' journal kept) removes the tunnels left on its former
  parent AP, with their `br-home` ports, and advertises no parent AP (status `gre_parent.withdrawn`,
  no `ap_bssid`). The pod's own rows are not touched. Both agents (box `gre-parent-demoted`).
  Proven pods-only on rdk-1010 (12 October, the Pis' shape on hwsim, each pod's `b-ap-50` on its
  5 GHz radio): seven SteerWiFiBackhaul moves of a child onto a parent's `b-ap-50` and back, none
  refused, each in 16 to 19 s, a 1500-byte DF ping across both hops after each move onto the
  parent, and a gateway 5 GHz channel change that the parent's station, its `b-ap-50` and the
  child followed without EMOSA writing `b-ap-50` (docs/handover/second-radio.md).
- **The parent AP's radio.** The controller decides where backhaul BSSes are, and can only do
  that for a radio it knows, so the parent AP's radio is one EMOSA **binds and reports** (its own
  M1 and Radio Basic Capabilities, the controller's M2 for it). Where the only radio that can
  carry the parent AP is not the fronthaul's (a pod whose one adapter per radio allows a single
  AP, and whose uplink station is on another band's adapter, the Pis' MT7921U), EMOSA binds that
  second radio too (§9, more than one radio per agent): it owns `b-ap-<band>` and the tunnels on
  it, and leaves the pod's uplink station, that radio's channel and `ht_mode`, and the pod's own
  rows alone. A hidden parent radio the controller does not see is refused: the controller would
  place the backhaul BSS, steer children (`SteerWiFiBackhaul`'s class and channel) and read link
  metrics on the wrong radio.
- **The parent's own uplink.** When the pod's backhaul station on that radio is its uplink in
  use (a parent that is itself a child: a radio carrying both its uplink station and the parent
  AP), the station stays: the parent AP takes the station's channel, and the station stays
  pinned to its upstream BSSID (§8.3), so it never joins its own AP. On a wired parent (the
  uplink in use, `Connection_Manager_Uplink.is_used`, is wired) the station on the parent AP's
  radio is kept off, and that is the pod's: its fallback (`pod-bh-fallback`, which owns the
  `bhaul-sta-*` rows that its bootstrap creates) holds the station off while the wired uplink is
  in use, and lifts the hold the moment it is not, when EMOSA may not reach the pod at all: the
  station comes up at once, pinned to its upstream, a Wi-Fi uplink within about 10 to 20 s. On a
  one-channel radio a linked station would drag the AP onto the upstream's channel, and its scans
  pause the AP's beacons. EMOSA writes no `bhaul-sta-*` column but those the uplink switch moves
  on the uplink station (§8.3: `credential_configs`, `multi_ap`); `enabled` is the pod's.
- **Fallback and return.** When the uplink moves off that radio (a fallback to another band),
  the parent AP stays up at its channel and keeps its children, while the parent's own traffic
  takes the fallback. When the uplink returns on the same channel, nothing moves. On another
  channel, a radio that runs an AP and a station on one channel only (the Pis' MT7921U) cannot
  link its station beside the AP, and the AP cannot move before the station links: the pod
  **releases** the AP. The trigger is the deadlock itself, not the fallback: while the station on
  the AP's radio is enabled, pinned to its upstream BSSID (§8.3), unlinked for 30 s, and that
  BSSID is heard on another control channel than the AP's, the pod holds the AP down to let the
  station rejoin there (whether or not a fallback carries the pod meanwhile; it also covers a
  2.4 GHz station beside a home AP on another channel). The AP then starts on the station's
  channel and the children, pinned to its BSSID, re-associate (about 15 to 30 s, under a child's
  own fallback delay). If the station has not linked within the 30 s, the AP comes back on its
  configured channel (§8.6 writes no radio channel, so where it was), and the pod tries again no
  sooner than 5 minutes later. One owner per column: EMOSA owns the parent AP's
  `Wifi_VIF_Config` row and never sees the pod's scan, so the release acts below EMOSA's
  columns, in the pod's own configuration layer (a conf mutator in the pods' OpenSync core,
  beside their channel override), never by `Wifi_VIF_Config.enabled`: the row stays as EMOSA
  wrote it, and only `Wifi_VIF_State` shows the AP down. EMOSA reconciles its Config rows
  against its M2 set, not against State, so it treats State and Config differing for that AP
  during a hold as expected and writes nothing. The pod's fallback stations (`bhaul-sta-*`) stay
  the pod's.
  A fallback that restarts the parent's OpenSync restarts the AP too, on its configured channel,
  and its children may fall back themselves: keeping the parent's fallback AP on the upstream's
  channel of that band avoids it. Withdrawing the AP on every fallback instead needs no release,
  at the cost of each child's fallback and rejoin (about 2 minutes) per loss of the parent's
  uplink.
- **The underlay** (§8.2's rules, on the pod): `169.254.N.0/24`, the AP at `.1`
  (`Wifi_Inet_Config`: `ip_assign_scheme=static`, `netmask=255.255.255.0`, `NAT=false`,
  `mtu=1600`, `dhcpd` `start .10`, `stop .250`, `lease_time 12h`; the profile's
  `gre_parent.inet` over the columns that are not the underlay's). `N` is the parent's, assigned
  by the fleet registry (§4): unique in the fleet, kept across the pod's restarts (and on first
  sight the one the pod's AP already has, when free: §4, re-admission), and never one
  whose `/24` overlaps the site's own GTP underlay, which the fleet's configuration gives per
  site (`169.254.1.0/24` in the RDK lab; a router's GTP may use another, such as
  `169.254.2.0/25`). The registry refuses a value that overlaps it. OpenSync 6.6 `cm` on a child takes the `.1` as its tunnel remote and
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
  credential, the backhaul SSID and passphrase of its applied M2 set (the parent AP's, above; not
  the gateway's pod-backhaul SSID of §8.2), is a 3-address join (`onboard_type=gre`), pinned to the
  parent AP's BSSID, not a Multi-AP credential; `wds` stays unset. **Applied** when
  its State shows the station on that BSSID and `cm`'s uplink in use is the station with its
  gretap. Held, moved and reverted as in §8.3; the fallback is still the pod's restart to its
  bootstrap (GTP) path. A pure GRE child has no configured Multi-AP upstream: it stays on its
  bootstrap (GTP) path, writing nothing, until the controller steers it onto a parent (uplink
  mode `gre`, the pinned BSSID set only by a steer).
- **Which target is a GRE join.** A child learns the fleet's GRE parent APs only from the peers'
  statuses: each GRE parent advertises its parent AP's own BSSID (`gre_parent.ap_bssid`), and a
  child reads that set (no 1905 frame names another pod's AP). A Backhaul Steering target in that
  set is the 3-address GRE join above; any other target is the Multi-AP (4-address) switch of
  §8.3. A child MUST NOT try GRE onto a BSS that is not a known fleet parent.
- **The decision, before the child leaves its uplink.** For a target that is not a fleet parent,
  EMOSA decides from what it holds, not from the controller's word, and refuses only what is
  positively foreign or absent (so an operator's move onto a hidden backhaul BSS, as in layer A,
  is not refused): a target seen on air advertising the child's own backhaul SSID (from its M2
  bSTA profile) is the Multi-AP switch; a target seen hidden (an empty SSID) and not a parent is
  still the Multi-AP switch, with the attempt-and-revert of §8.3 (it cannot be verified, but it is
  not foreign); a target seen with a different, non-empty SSID is refused at once, the uplink kept
  up; a target not on air after one on-channel scan is refused ("target not found"), the uplink
  kept up. The source of "a Multi-AP backhaul BSS" is the backhaul SSID of the child's own M2, not
  any received flag: a child holds only its own M2, never another agent's AP Operational BSS or
  BSS Configuration Report, so there is no received TLV that marks a peer's BSS as backhaul. A
  hidden backhaul BSS therefore cannot be positively confirmed by a passive scan; confirming it by
  a directed probe for the backhaul SSID (a hidden BSS answers one) is a future refinement, for
  when the pod code exposes beacon or probe-response IEs.
- **The on-channel scan.** When the target is not already in the scan results, EMOSA runs one
  on-channel scan before the child leaves its uplink: it asks the pod's stats manager for a
  neighbour scan on the target's channel and reads the result from `Wifi_Scan_State` (which
  carries each BSS's BSSID, SSID and channel; a hidden BSS shows an empty SSID). If the target's
  channel is not the child's current one, that scan is off channel for the uplink, a brief absence
  before the decision; the uplink is not moved until the decision is made. *Ownership* (§3.2's one
  writer per column): `Wifi_Stats_Config` is the cloud's table, so EMOSA's scan request is its own
  row, identified as EMOSA's and removed by EMOSA alone; EMOSA never edits or deletes another
  writer's row, and a cloud reconcile of the table while EMOSA's row exists does not disturb the
  scan (the row is re-added if gone, removed when the scan is read).
- **Refused.** A refusal is a Backhaul Steering Response with a failure result and an Error Code
  TLV. "No station on the band" and "channel not operable" are reason `0x04` (the STA cannot
  operate on the channel specified). A foreign or not-found target is reason `0x06`: the nearest
  code (`0x05`–`0x07` all describe the target end refusing), read as "the association with the
  target failed" — EMOSA declined to associate because the target could not be confirmed a
  backhaul BSS or was not on air, which is what the RDK agents send when a backhaul steering
  target cannot be joined. The uplink is never left for a refused target, so the refusal costs no
  outage.
- **How the controller reaches a pod at all.** The controller sends a Backhaul Steering Request
  only to a device it models with a station-mode backhaul BSS, so two things are needed, and both
  are **standard** — no RDK vendor-specific TLV. (1) *The medium.* EMOSA reports the pod's backhaul
  station, whatever its uplink's kind (Multi-AP, or a 3-address station under a GRE tunnel or the
  bootstrap/GTP path), as an 802.11 backhaul neighbour with its MAC, band and parent BSSID, never
  claimed as a Multi-AP (4-address) bSTA (§8.5's reporting, now for a GRE child too); the controller
  then models the device's backhaul link as Wireless LAN. Under a GRE tunnel the station reported is
  the one whose own gretap cm uses (cm names it `g-<station>`): until a GRE move's make-before-break
  takes it off (§8.3), the station it moved from may still be associated to its old parent, and reporting that one
  would give the controller the old parent and make it name the old station in its next steer, which
  EMOSA refuses (box `mixed-gre-child-onto-legacy-parent`, 16 October). Only when no station's own
  gretap is in use is the first station under a GRE tunnel reported, as before. Both agents.
  (2) *The bSTA model.* EMOSA reports the
  station as a **non-AP (STA-role) interface in its Device Information**, on its parent's BSSID, plus
  the standard R2 Backhaul STA Radio Capabilities TLV; the controller's `handle_topology_response`
  builds the station-mode backhaul BSS from that interface (unified-wifi-mesh patch
  `0216-ctrl-learn-backhaul-sta-from-device-information`, already in the lab and MV3 controller). No
  vendor operational-BSS TLV, and no precondition relax, is required. **Proven on `rdk-1010`
  (11 Oct):** once the agent onboards, the controller logs *"Backhaul STA learned from Device
  Information: sta=… parent=…"* and records the association; `SteerWiFiBackhaul()` then passes its
  precondition (*"… sta … to … queued"*), sends the Backhaul Steering Request, and the EMOSA agent
  answers with a success result. There is one operator path, not two. *Data-model cosmetic:* when the
  bSTA's MAC equals the pod's AP-radio id (the hwsim 2.4 GHz case, where a station shares its radio's
  MAC), `Device.{i}.Radio.1.BackhaulSta.MACAddress` reads empty — that `Radio` row is the AP radio,
  not the station — but `SteerWiFiBackhaul`'s precondition scans the BSS table directly, so it is
  unaffected. The precondition scans the invoked `Device.{i}`'s own BSS table (its per-device data
  model, `get_dm_easy_mesh(instance)`), so it names that pod's station; a fleet of several backhaul
  children — each its own device — stays correct, each steer acting on its own device's station.
- **Moved back (to the bootstrap), one operator path.** The same `SteerWiFiBackhaul`, with the
  target set to the pod's own bootstrap BSS (the GTP gateway BSS on `rdk-1010`, `podbh` on the MV3),
  returns the pod to its bootstrap. EMOSA records that BSSID as the station's bootstrap upstream —
  the BSS its station was on before any move, cm's own association, read on first observation and
  persisted with the uplink state — so a steer to it is recognised as a revert (neither a parent nor
  a foreign AP, but home), and a steer to `podbh`, whose SSID is not a backhaul SSID, is not mistaken
  for a foreign target. The revert does **not** re-link the bootstrap credential: in credential-list
  mode core builds its networks from the linked credentials' own BSSIDs and ignores
  `Wifi_VIF_Config.parent`, and the pod's bootstrap credential carries no BSSID, so a re-link would
  leave the station unpinned (a loop or the wrong BSS). Instead EMOSA restores the station to
  **vconf mode** — the `ssid`, `security` and `credential_configs` it snapshotted at the move —
  leaves `parent` (the pod's bootstrap pin) alone for the station to land on, and deletes the one
  credential it inserted. There is no second operator path and no "drop from parent" action: onto is
  a steer to the parent AP, back is a steer to the bootstrap BSS. With no record (EMOSA first bound
  while already on a parent) the revert is recognised instead by the target advertising the pod's own
  onboarding SSID in a scan.
- **A failed move, or a failed switch to a kept parent,** is undone by the same restore, without
  the operator (§8.3 "Moved"). A child has no configured upstream to switch to, and its pod may
  never restart to drop the failed credential: the Pis' fallback station keeps `cm`'s uplink
  working. The undo answers no controller, so it is confirmed once EMOSA's credential is off the
  station, not by the station's parent.
- **The parent AP on the second radio** (§9; the Pis' adapter B, one AP per adapter): `b-ap-50` is
  configured by the 5 GHz radio's own M2, through that radio's own write scope, never by the
  fronthaul radio's (whose set's backhaul BSS has no slot there and is left out, §3.4). One owner per
  column of that radio:

  | Row / column on the second radio | Owner |
  | --- | --- |
  | `Wifi_Radio_Config` `channel`, `ht_mode`, `enabled` and every other column but `vif_configs` | the pod (its channel override follows the station); EMOSA never writes |
  | `vif_configs`: the references to the pod's own rows (its station `bhaul-sta-50`) | the pod |
  | `vif_configs`: the reference to `b-ap-50` | the radio's EMOSA scope: insert and delete of its own reference only, never a replace; the transaction guards the radio's `if_name` only |
  | `b-ap-50`'s `Wifi_VIF_Config` and `Wifi_Inet_Config` (the underlay's `.1`) | the radio's EMOSA scope, from that radio's M2 set; removed when the set has no backhaul BSS (withdrawal by band) |
  | the fronthaul radio, `home-ap-24` and its slots | the fronthaul's EMOSA scope, from its own radio's M2 |
  | the children's tunnels (`pgd*`) | EMOSA's GRE parent scope, by the parent AP's name on whichever radio |

- **One owner per column of the backhaul station** `bhaul-sta-<band>`, agreed with the pod side
  (pod-bootstrap and pod-bh-fallback, OpenSync 6.6.1). The marker that a station is **under an EMOSA
  move** is `Wifi_VIF_Config.ssid == ""` (credential-list mode; 6.6.1 has no `other_config` column,
  and a non-empty `ssid`, vconf mode, is the pod's bootstrap). A GRE move disables **no** other
  station (cm bridges only the in-use uplink's GRE into `br-home`, so there is no double bridge);
  only a Multi-AP move disables the others.

  | Column / row of `bhaul-sta-<band>` | Owner | Under an EMOSA move | At the revert |
  | --- | --- | --- | --- |
  | row existence, `if_name`, `mode`, `vif_radio_idx`, `bridge`, the radio's `vif_configs` membership, its `Wifi_Inet_Config` | the pod (pod-bootstrap, at OpenSync start) | EMOSA never writes | unchanged |
  | `parent` (the bootstrap pin, `POD_BACKHAUL_BSSID_<band>`) | the pod only | EMOSA never writes; ignored in credential-list mode | left; the station lands on it |
  | `enabled` | the pod (true at start) | EMOSA sets true (a no-op) | left |
  | `ssid`, `security`, `credential_configs` | the pod at start | EMOSA sets `""`, `{}`, [its own credential]; `ssid==""` is the move marker | EMOSA restores the snapshot it took at the move |
  | `multi_ap`, `wds` | the pod (`none`/false) | a GRE move leaves them; only a Multi-AP move unsets them | restored if the move touched them |
  | the bootstrap `Wifi_Credential_Config` (no BSSID) | the pod | EMOSA never writes; stays present | left |
  | EMOSA's `Wifi_Credential_Config` (BSSID = target) | EMOSA | EMOSA inserts it | EMOSA deletes it |
  | the fallback station `bhaul-sta-<POD_STA_FALLBACK>` | pod-bh-fallback | EMOSA never writes, moves or disables it | unchanged |

- **A pod restart drops the move.** OpenSync rebuilds OVSDB from its template at every start
  (pod-bootstrap writes the bootstrap rows, including the pin, before `dm`), so a move, its marker and
  EMOSA's credential are gone after a restart and the pod returns to its pin by itself. EMOSA sees a
  new OpenSync instance and **re-applies the active move from its persisted target** once the pod is
  back, without the operator (as the uplink switch is re-applied after every re-onboarding, §8.3). A
  move therefore needs no persistent pod database. **Pod-side change** (yocto-53, with the next pod
  image after soak B; nothing on the Pis before): pod-bootstrap's `load_creds` selects only
  credentials without a BSSID, so a fallback station brought up **during** a move cannot link or take
  EMOSA's BSSID-pinned credential (today it takes all credentials and the first one's SSID/key).
- **RDK's own autonomous steering (a known property, not used).** The controller carries a
  native-backhaul module (`em_backhaul_ctrl.cpp`, its "Native backhaul state") that tracks each
  backhaul station and may, from its own topology, move one between parents without an operator. It
  does learn an EMOSA child's station from the child's Device Information and reported parent, but
  marks it **unrooted**: `em_backhaul::root_path(...).valid` is false, so the child is skipped,
  because EMOSA pods do not take part in the native rooted-admission probe that marks a station
  rooted (only the real gateway uplink is rooted). This does not affect the operator
  `SteerWiFiBackhaul` path, which models the bSTA from the standard reporting independently of
  rootedness, and the owner has not approved the controller choosing pod parents on its own; so
  EMOSA relies on operator-directed moves only and the unrooted state is benign. Observed on
  `rdk-1010`: the child's station appears in the controller's "Native backhaul state" log as
  `…/unrooted`.
- **No loops**, as §8.5: EMOSA MUST refuse a move to a parent AP of a pod whose own upstream
  chain reaches this pod.
- **Reported** as in §8.5: the child names the parent's agent as its 1905 neighbor on its
  backhaul, the parent names the child on the AP, and the pair's link metrics come from the
  parent's client report of the child's station (§3.6; with telemetry off, no measurement).
- **Withdrawn:** there are two distinct cases, and EMOSA treats them differently because the M2
  set differs. (a) *Absent BSS (a true delete).* A backhaul BSS no longer in the M2 set removes
  the parent AP and its `Wifi_Inet_Config` (the AP scope's guarded transaction, §3.2); the parent
  AP gone, its children are gone, so the tunnel reconcile then removes every child's tunnel row,
  port and interface (its own guarded transaction). The children's `cm` falls back to their
  bootstrap path. (b) *Disabled BSS.* A backhaul BSS still in the M2 set but with the operator's
  enable cleared is mirrored as a VIF disable (`enabled=false`), not a delete: the parent AP row
  stays, so the tunnels stay as rows but go down with the AP. EMOSA does not invent a delete the
  controller did not ask for; only an absent BSS is a delete. The nearest real-controller trigger
  for (a) is to drop the backhaul's band (so the controller omits that band's BSS element from
  the M2 of every device, not merely clears its enable); for (b), the controller's own
  enable toggle on the haul profile, which keeps the BSS in the set. Case (a) is the canonical
  spec path; case (b) is what an RDK operator UI that only toggles enable produces.
- **Written** (§3.2), each change one guarded transaction: the parent AP (`insert` or `update
  Wifi_VIF_Config`, `mutate Wifi_Radio_Config vif_configs`, `insert` or `update
  Wifi_Inet_Config` of the AP); a child's tunnel
  (`insert Wifi_Inet_Config` gre row, `insert Interface`, `insert Port`, `mutate Bridge br-home
  ports`); the withdrawal and a child's departure as the matching deletes. Read besides §3.2's:
  `DHCP_leased_IP` (`hwaddr`, `inet_addr`), `Wifi_Inet_State` (`if_name`, `inet_addr`), `Bridge`
  (`name`, `ports`), `Port` and `Interface` (`name`).

Both agents. Box scenarios: a child onto a parent and back; the parent's uplink falling back
and returning on another channel with a child attached (the release, and its 30 s bound); the
parent lost; a child's tunnel that drifts away is re-created while the child stays. Proven first
on `rdk-1010`'s virtual pods with a `gre-parent` profile, then on the physical pods (a Pi through
a Pi to the router).

The withdrawn case (a), the tunnel re-create, and the recovery were exercised against the real
onewifi_em_ctrl on `rdk-1010`'s virtual pods: dropping 2.4 GHz from the backhaul band deleted the
parent AP's `Wifi_VIF_Config` and `Wifi_Inet_Config` rows (not `enabled=false`) 2.6 s after the
POST and the child's tunnel 2.8 s after, the child's two-hop path went down, and the controller
omitted the BSS element network-wide (its DataElements BSS list compacted, and the real agents
that were backhauled on 2.4 GHz re-homed to Ethernet). Restoring the band re-created the parent AP
in 7.5 s, re-formed the child's tunnel in 12.2 s, and the child was back with a full-MTU two-hop
path 17.5 s after the POST. A drifted tunnel (its gre row, port and interface deleted underneath a
still-present child) was re-created by the same agent, at one process, in 1.2 s.

**Finding A (live promotion).** Flipping an already-onboarded pod's profile to `gre-parent` did
not convert its existing `backhaul_bss` into a parent AP; only a fresh onboard did.
`pod_profile.role()` maps both a rendered parent AP (`multi_ap` none) and a plain `backhaul_bss`
to `backhaul`, so reconcile's `observed.satisfies(intent.target())` was already true and the
conversion in `_extra_ops()` was never submitted. The fix makes the backend's rendering
expectation for a parent (`multi_ap` none plus the underlay `Wifi_Inet_Config`) reach the
applied-predicate, so a live promotion submits; a box scenario covers it and the fresh-provision
path is re-run. Carried with the second-radio work (§9).

## 9. Not covered yet

- 5 and 6 GHz radios, and WPA3 (layer B, draft, binds a 5 GHz radio as a GRE parent's AP);
- more than one radio per agent, except the GRE parent's second radio of §8.6 (layer B):
  EMOSA binds and reports it, owns `b-ap-<band>` and its tunnels on it, and leaves alone the
  pod's uplink station on it, its channel and `ht_mode` (the pod's channel override follows the
  station, so a written channel would fight it; EMOSA reports the channel from `Wifi_Radio_State`
  and declines a Channel Selection Request that moves the radio off the station's channel), and
  the pod's own rows (`insert`/`delete` of EMOSA's own VIFs only, never a replace of
  `vif_configs` that would drop the station). One M1 and one M2 per radio (EasyMesh 6.1 §7.1):
  the further radio's M1 (its own AP Radio Basic Capabilities, WSC RF Bands 2) goes once the
  fronthaul's M2 is taken, one exchange awaiting an M2 at a time, and an M2 goes to the exchange
  of the radio its AP Radio Identifier names. Each radio is its own write scope with its own
  journal (`radio-<band>` in the agent's state): its M2 set is fitted to that radio's slots only,
  with no primary BSS (the radio serves no fronthaul of its own), so a renew of one radio never
  rewrites the other's BSSes and a set without a backhaul BSS removes only that radio's parent AP.
  Every bound radio is an Operational and Configured radio of the Topology Response, with no BSS
  when it carries none of EMOSA's (a child's radio). A radio is reported in every 20 MHz class of
  its band (IEEE 802.11 Table E-4), its current channel the only operable one in each: 2.4 GHz
  class 81; 5 GHz the non-DFS UNII-1 class 115 (36 to 48) and UNII-3 classes 124 (149 to 161)
  and 125 (149 to 177). Its Operating Channel Report names the last class that has its channel
  (115/44 on rdk-1010, 125/149 on V54). A further radio on a channel not mapped (DFS) is not
  reported until it leaves it. **A radio the profile places** (its `radios` entry declares a
  `channel`, which EMOSA maps: a fronthaul with no station of its own, as a legacy pod's 5 GHz
  radio, §3.10) is the exception to the owner table above for `channel`, `ht_mode` and
  `enabled`: it is reported on the declared channel from the start, whatever channel the pod
  left it on, and the radio's own write scope sets the declared channel and width (and
  `enabled`) in the transaction of its slots, when the radio does not have them already, as a
  cold fronthaul's create does. The channel rule (item 9): the radio is operable only on its
  station's channel, so a Channel Selection Request that forbids that channel, in any class that
  has it, is declined for that radio alone. A station that moves the radio to another channel
  changes the radio's operable channel: a provisioned session is kept (§2.5), the agent takes the
  new capabilities and reports the new channel in a Channel Preference Report and an Operating
  Channel Report (a radio with no channel for a moment keeps the one it was reported on, so a
  move is one change); a session not yet provisioned starts afresh. Nothing is written:
  `b-ap-<band>` moves with its radio, the pod's doing, and EMOSA neither recreates nor rewrites
  it. The fronthaul radio moves the same way with the station it shares the radio with, and is
  kept and reported the same way (§2.5), on any channel of class 81 (1 to 13), its Operating
  Channel Report naming 81 and that channel; it too keeps the channel it was reported on while
  it has none for a moment (its BSS down while the radio changes channel). Finding 24: the Python
  agent had the fronthaul's operating parameters on channel 6 only, so off it a Channel
  Selection Request went unanswered and no Operating Channel Report was sent, while the C agent
  answered (box `channel-selection-off-6`).
  AP metrics (§3.8) per radio: each radio's BSSes carry the utilization of the pod's survey of
  that radio's own channel and their stations' metrics; a radio with no fresh survey is left out
  of the response, nothing filled in. Known gap, to close before V54's end soaks: the pod's
  telemetry surveys and reports clients on the fronthaul's band only, so a second radio's BSS
  (`b-ap-<band>`) is left out and the controller cannot see its child pods' link quality (their
  RCPI and rates). The move does not need it (an operator-directed backhaul steer), production
  (b) does: the follow-up is that radio's own telemetry (its survey and client reports, a write
  scope of its own). An Unassociated STA Link Metrics Query (§3.9) is heard on any
  of the agent's radios, on its channel in a class of its band (149 in 124 or 125), and a station
  on any radio's BSS is associated. A pod's uplink takes its backhaul credentials (spec 8.3, 8.6)
  from the backhaul BSS of an applied M2 set: the fronthaul radio's first, then each further
  radio's, since on the Pis' shape only the 5 GHz radio's set has one (box
  `second-radio-child-credentials`). A radio's transmit power is the pod's
  `Wifi_Radio_State.tx_power`, as reported. OpenSync leaves it unset when the radio's VIFs report
  different powers (on rdk-1010's hwsim pods the 5 GHz radio's station is at 20 dBm and its
  `b-ap-50` at 30). The channel procedures still answer such a radio: a Channel Preference Query
  at once, from the capabilities (it needs no power), and an Operating Channel Report giving the
  radio's advertised maximum EIRP, a bound and not a measurement, logged and counted
  (`operating_power_bound_reported`) each time. Refused, RDK re-sent its query every second until
  its configuration timed out and renewed the pod every minute (rdk-1010, 12 October; box
  `second-radio-no-power`); answered, it renewed neither pod in the 11 minutes measured after the
  deploy. On mt7921 (the Pis) the reported power is the channel's regulatory
  cap given to the firmware (30 dBm on 149 and on 2.4 GHz channel 6, 23 on 36), once the Pis'
  driver reports it (mt76's transmit-power series 317bc1a0590e, 8286bbf62dcc and 879d754e48f6
  backported to their kernel): before that it reads 3 dBm on
  every radio, which is not the radio's power.
  Both agents (box `second-radio-parent`, `second-radio-withdrawn`, `second-radio-moves-channel`).
  Known gap, to close before V54's end soaks: V54's radio B runs VHT 80 MHz on 149/80 (centre
  155), while EMOSA reports it in the 20 MHz classes with no HT, VHT or HE capability TLVs, so
  the controller models a 20 MHz radio. The follow-up: the AP HT and VHT (later HE) Capabilities
  TLVs, the 80 MHz class 128 (centre 155 for 149 to 161), and proof that a Backhaul Steering
  Request naming 128/155 lands the child on the right primary channel (149), not on "channel
  155";
- backhaul link metrics and a 1905 neighbor on the backhaul interface towards an upstream
  that is not a pod of the fleet (an RDK or prpl node: EMOSA does not know its AL MAC);
- EasyMesh AP and station metrics. The pod's station measurements are
  collected (§3.6), but a complete metric needs more than a hwsim pod reports:
  RCPI needs the noise floor, traffic statistics need retries and errors, and
  AP metrics need channel utilization, which needs a radio model (wmediumd);
- TLS on the front port and agent ports (a physical pod requires it);
- DPP onboarding.
