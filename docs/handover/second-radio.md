<!-- SPDX-License-Identifier: Apache-2.0 -->
# Second radio (spec 9) and the pods-only (b) onto-and-back — handover

Branch `layer-b`. Goal: EMOSA binds and reports a pod's **second radio** (the GRE parent's/child's
5 GHz radio of spec 8.6) so the controller models it, then prove the faithful all-5 GHz
onto-and-back on rdk-1010. This is **scenario (b) pods-only** — V54's physical shape (see spec §8
"Scenarios": (a) extenders-only, (b) pods-only, (c) mixed; design and prove (b) first, extenders
off; layer A is a (b) mechanism too). Times are PDT.

## Faithful (b) proof shape
- pod-2 (child, serial `RPIPOD02D7777EF0D9`, controller Device.2, AL `02:c2:b8:31:3a:f8`):
  `bhaul-sta-50` bootstraps on the gateway's 5 GHz onboarding BSS `lnf_radius_5g`
  `02:00:00:8a:29:1d` (SSID `opensync-lab-bhaul`, PSK `opensync-lab-bhaul-psk`, ch 36) → steered onto
  pod-1's `b-ap-50` `72:00:00:00:6b:00` (GRE parent) → back.
- pod-1 (parent, `RPIPOD023F87E628DD`): its 5 GHz radio carries `bhaul-sta-50` (uplink, on
  `lnf_radius_5g`) **and** `b-ap-50` (the parent AP EMOSA owns). Needs a pin
  `POD_BACKHAUL_BSSID_50=02:00:00:8a:29:1d` in pod-1's inside pod.env when it becomes the parent
  (yocto-53).
- Station MAC == radio RUID is NOT a controller filter (0216 learns it on 2.4 GHz where it also
  equals the RUID); the missing 5 GHz row was just the agent not reporting the 5 GHz radio — fixed
  below. The empty `BackhaulSta.MACAddress` display is a separate post-layer-B fix.

## Done on layer-b (all green on the fast CI gates)
Child side (the controller models pod-2's 5 GHz radio + backhaul STA):
- `radios` array + `slot_bands` in the pod-profile schema/model (which radio a slot's VIF is on).
- spec §8 three-scenarios note.
- `OPERATING_CLASSES += 5G` (UNII-1, opclass 115, ch 36-48).
- `radios_capabilities(specs)` multi-radio AP Capability Report builder (single-radio
  `radio_capabilities` is now a wrapper).
- `pod.refresh()` reports the fronthaul **and** each profile radio via `bound_radios()`
  (per-radio max_bss by `slot_bands`); `facts["radios"]` exposes them in the agent status.
Parent side:
- P1: `_binding` records `self.radio_uuid_by_band` {band → Wifi_Radio_Config uuid}.
- P2: the slot rendering places each slot on its radio (`_extras` + the Wifi_Radio_Config mutates
  use the slot's radio via `slot_bands`); `b-ap-50` banded 5G is created on the 5 GHz radio.
- P4: `topology()` reports an Operational/Configured radio per bound radio (`extra_radios`), so the
  controller models `b-ap-50` under the 5 GHz radio on its own channel.
- P3a: `make_bridge(radio_index=…)` builds a WSC exchange for any radio (rf_band from the opclass).
Spec: Finding A recorded in §8.6. Behaviour is identical for single-radio profiles throughout, so
existing box/units are unaffected.

## Done (12 October, branch `wip/second-radio-p3b`, fast-forwarded to layer-b when green)
Both agents, vectors and box for each; spec 8.6 and 9 record the rules.
- P3b: one M1 and one M2 per radio, sequential, each radio its own write scope and journal
  (`radio-<band>`): 680162d, 21ddceb (C).
- Item 9: policy radio lists (ddeeefa), channel procedures per radio (40a878c), UNII-3 classes
  124/125 and a station's channel move as one fresh session that leaves `b-ap-50` alone (e5fea15),
  AP metrics and unassociated queries per radio (a48d362).
- Finding A, live promotion (d2652db), and its mirror, demotion (9332f97).
- The Pis' profile `rpi-pod-mt7921u-6.6.1-gre-5g-v1` (1de67db).
- Found by the lab proof: a GRE child's backhaul credentials from its 5 GHz radio's set (1c1e7ae);
  channel procedures for a radio whose power the pod does not report (c292760).
- Found by the box (12 October, the GRE child's kept target for V54's fault rows): a pod's
  OpenSync restart between a write and its observation held the new start's M2 `BUSY` until the
  old write's deadline, and nothing onboarded again, so the pod stayed unconfigured until a
  Renew. This was in both agents, older than layer B. Now the old write fails at once (spec §5;
  vectors `engine.json` `restarted-before-observed`, box `restart-during-write`).
- The advertised maximum EIRP (spec 2.5), found on the physical Pis (12 October, after they took
  the mt76 transmit-power modules):
  - **Cause:** a pod's source begins before OpenSync fills `Wifi_Radio_State.tx_power`, so the
    agent advertised 20 dBm. Every new connection from the pod is a new source and repeats the
    race, an OpenSync restart included: on the bench, restarts at 19:08 and 19:14 PDT took 20
    again. The Pis then reported 30. The channel procedures refuse a power
    above the advertised maximum, so the controller timed out and renewed the agent every
    ~40 s. 9d930bc even refused the Channel Preference Query.
  - **Now (spec 2.5):** a reported power above a radio's advertised maximum is taken as new
    capabilities, with one fresh session, writing nothing. The maximum only rises (vectors
    `translation-northbound.json` `advertised_eirp`, box `power-rises`, red before on both
    agents).
  - **Not done, on purpose:** the agent does not wait for the power before its first M1, as
    7f offered. A radio whose VIFs disagree is never given a power by OpenSync (rdk-1010's
    5 GHz radio), so every start of such a pod would wait out the bound. Each boot of a Pi
    costs one fresh session instead, which writes nothing and leaves the uplink alone.

## The (b) proof on rdk-1010 (12 October, PDT; UTC in brackets)
EMOSA 1c1e7ae on the gateway from 15:30:41 (22:30:41Z); pods relaunched from
`rpi-pod-amd64-20261012084755` by yocto-53 (pod-1 14:40:43, pod-2 14:41:13), both on
`opensync-lab-hwsim-6.6.1-gre-5g-v1`; extenders stopped 14:44:22-40.
- Both pods report two radios (2.4 GHz ch 6, 5 GHz ch 36); each radio's own M2 makes its
  `b-ap-50` (pod-1 72:00:00:00:6b:00, pod-2 72:00:00:00:6d:00); every operation applied.
- The controller holds pod-2's 5 GHz station: 0216's "Backhaul STA learned from Device Information:
  sta=02:00:00:00:6d:00 parent=02:00:00:8a:29:1d", and a station row on its Device.N Radio.2.
- The first steer (14:45:07) was sent but stalled on the child's credentials (fixed in 1c1e7ae); it
  completed after the fix at 15:30:58 (22:30:58Z): "Backhaul parent update: sta=02:00:00:00:6d:00
  ... new_bssid=72:00:00:00:6b:00", pod-1's tunnel pgd1_57 to 169.254.1.57, a 1500-byte DF ping
  from pod-2 to 10.0.0.1 across both hops 3/3.
- Six measured SteerWiFiBackhaul moves, alternately onto pod-1's `b-ap-50` and back to
  `lnf_radius_5g` (15:31:35 to 15:35:00): all Status Success, each moved in 15.6 to 19.4 s, every
  onto-move's ping 3/3; every request named pod-2's AL with the right previous parent; pod-1 never
  moved. The controller's topology hangs pod-2 under pod-1's AL.
- A gateway 5 GHz channel change with pod-2 on pod-1 (36 to 44 at 15:40:53, back at 15:41:53):
  pod-1's station and `b-ap-50` followed within 4 s, pod-2 rejoined within 6 s (30 s on the way
  back, with an extra radio reset of ours), the tunnel and the DF ping came back; each pod's EMOSA
  took one session per change for its radio's new capabilities (more only when its OVSDB link,
  carried by the moving backhaul, dropped); `b-ap-50`'s Config `_version` never changed.
- Found: the pods were renewed by the controller every minute (EMOSA refused the channel
  procedures for a radio with no reported power; fixed in c292760). The controller's data model of
  a pod's radio B (station MAC == RUID) is an open-work row for after multi-hop: it does not affect
  steering (0 refusals in 7 steers).
- The fix's lab check: 42eb4a4 deployed at 16:30:23 (23:30:23Z); no pod renew from 16:31:10 to
  16:42:26 (pod-2 had one, at its agent's restart). Each pod answered one preference query. pod-1's
  radio B, with no power in its State, was reported at the capability's maximum EIRP
  (`operating_power_bound_reported` 2); pod-2's radio B read 30 dBm. Both pods declined every
  Channel Selection Request with code 0x02. That is the known answer to RDK's 0 dBm Transmit Power
  Limit and needs no change. The periodic AP metrics found no source, because pod telemetry
  (MQTT over TLS) is not wired in this lab.
- Seen, not EMOSA's: em_ctrl restarted at about 15:01 (22:01Z). After that it renewed the
  gateway's own agent's three radios every ~42 s until the end of the session. The restored
  baseline shows no such loop (0 renews from 16:52 to 17:08). Watch for it in the pods-only check.
- rdk-1010 restored to `pre-layerb` at 16:49 (23:49Z); the lab came up at 17:05 (cold-boot
  reconstruction PASS, model 6/18/60, 100/100 clients, 0 restarts) and its health audit passed at
  17:07 (00:07Z 13 October; 100/100 traffic, no radio drops, no cores).
- Evidence: rev140 `~/easymesh-builds/evidence-second-radio-20261012/` (the controller's backhaul
  and post-fix lines, the pods' status, the steers, the channel-change samples, the audit log).
  The gateway's copy went with the restore.

## V54 on the Pis (EMOSA's side)
The physical run of the (b) proof: the router, pi1 and pi2, extenders off; the Pi side, the run's
checks and its faults are in opensync-rpi `docs/multihop.md` ("V54 on the Pis"). The router's
own settings and steps stay with the router's project.
- **EMOSA:** the router's build pinned to an emosa-lab main commit.
- **Fleet configuration:** the router's own, plus `gtp_underlay` set to its GTP's underlay and
  for pi1 `RPIPOD02A632CA11B6` and pi2 `RPIPOD02A632CA11D8`: profile
  `rpi-pod-mt7921u-6.6.1-gre-5g-v1`, uplink `{mode gre, station bhaul-sta-50}`. A fleet restart
  applies it. The fleet rewrites each agent's configuration and restarts the agent, keeping the
  registry (AL MACs, ports), and gives each Pi a parent underlay that does not overlap the GTP
  (the lowest free `169.254.N.0/24`).
- **Before the first steer:** both agents onboarded with one M1 per radio. pi1's status shows
  `gre_parent.ap_bssid` as its `b-ap-50` BSSID (not withdrawn), and its `b-ap-50` is up on its
  station's channel with the underlay's `.1`.
- **Before each steer** (as on rdk-1010): the controller's 0216 line "Backhaul STA learned
  from Device Information: sta=<pi2's bhaul-sta-50> parent=<its current parent>" and pi2's
  station row in its `Device.N`.
- **The steers:** the operator's `SteerWiFiBackhaul` onto pi1's `b-ap-50` BSSID, and back onto
  the router's 5 GHz pod-backhaul BSSID (pi2's bootstrap). After each move onto pi1: pi2's uplink
  `gre`, applied on that
  BSSID; a `pgd` tunnel on pi1; a 1500-byte DF ping across both hops. Only the controller
  chooses a parent. The target of its last move is kept for the child's later starts (spec 8.3,
  "Moved"). After the child's OpenSync restart, which lands it on its bootstrap, EMOSA re-applies
  that move on the new start. When pi1 goes away, pi2 returns by its own fallback (about 3
  minutes). On pi2's next start EMOSA tries pi1 once more and drops the target if pi1 is not
  reachable within the switch's 90 s, unless the operator steers pi2 back sooner. The box proves
  the kept target for the Multi-AP uplink (`backhaul-steering-kept`, `backhaul-kept-gone`) and
  for a GRE child (`gre-child-kept`, `gre-child-kept-gone`), in both agents.
- **Expected, not failures:**
  - radio B is reported in the 20 MHz classes (149 as class 125) while it runs 149/80 (the known
    gap below);
  - a radio with no power in its State is reported at its capability's maximum EIRP (counted);
  - the Pis report their channel's regulatory cap as the radio's power: 30 dBm on 149/80 and on
    2.4 GHz channel 6 (23 on 36). Their kernels have had the mt76 transmit-power series
    (317bc1a0590e, 8286bbf62dcc, 879d754e48f6) since 12 October, 18:40 PDT; before it they read
    3 dBm. Both VIFs of a radio agree, so OpenSync fills `Wifi_Radio_State.tx_power` and EMOSA
    reports the pod's value, not the capability bound;
  - a Channel Selection Request with a power limit below the reported power is declined with
    code 0x02.
- **Watch:** the controller renewing the router's own agent every ~42 s after an em_ctrl restart
  (seen on rdk-1010, not in its baseline).

## Remaining
1. Deferred: the layer-A pods-only check (a pod onto a POD serving a Multi-AP backhaul BSS, not an
   extender, and back) before the end soaks; the box proves the move and revert.
2. Known gaps to close before V54's end soaks (spec 9): radio B's 80 MHz (class 128, centre 155, the
   HT/VHT capability TLVs, a steer naming 128/155) and radio B's own telemetry.
3. Known gap, after multi-hop: both agents answer a Backhaul STA Capability Query (0x8027) only
   within an onboarding session.
   - **C:** drops it from a session's end until the next session's first M1 (counted as
     `unsupported_message_8027`).
   - **Reference:** answers only while provisioning.
   - **Should:** an agent answers it whenever it has the pod's view and its controller, since the
     report describes the pod's backhaul station radios, not the autoconfiguration.
   - **V54:** not hit on the physical Pis (12 October): the counter stayed at 0. Radio B's
     7-minute renew loop after pi1's boot was the controller's (a bsta_cap queued behind the
     radio's em_config, its query never sent). That fix is a controller patch (meta-cmf 0266,
     approved 12 October); EMOSA's sending of the Backhaul STA Radio Capabilities TLV in every
     Topology Response is standard and stays.
4. The parent AP broadcasts its SSID (13 October; drafted on `wip/second-radio-p3b`, not pinned,
   for the owner's decision). After pi1's reboot, its hidden `b-ap-50` answered pi2's directed
   Probe Requests with the hidden beacon's empty SSID, from below hostapd (a receive-only capture
   on 149, 13 October 00:44 PDT), so pi2's station, pinned to its BSSID, never matched it:
   `CTRL-EVENT-NETWORK-NOT-FOUND` every 6 s. The same pin had joined in 5 s before the reboot.
   - **Fix:** the gre-5g profiles' `gre_parent.vif` sets `ssid_broadcast` enabled (spec 8.6). A
     station matches by SSID, a BSSID pin only narrows it, so a broadcast SSID needs no Probe
     Response; the gateway broadcasts its own backhaul BSSes too.
   - **Not chosen:** turning the MT7921's probe answering off on the Pis, which fixes one hardware.
5. To draft: after a failed switch to a kept target, or a failed move, of a GRE child (no
   configured upstream), the agent leaves its credential and BSSID pin on the station
   (`_kept_failed`, `_move_failed`, then "no upstream"). Spec 8.3 relies on the pod returning to
   its bootstrap by itself, but the Pis' 2.4 GHz fallback station keeps `cm`'s uplink working,
   so OpenSync never restarts and the 5 GHz station stays pinned to a parent it cannot join.
   The agent should undo its own write, as the steer-back revert does (both agents, and a box
   variant whose fallback station keeps the pod up).
6. Seen with two-radio pods, the controller's: after a Channel Selection Request for radio B, the
   agent's Operating Channel Report (radio A's TLV first, as EMOSA sends it) is handled on radio
   A's em thread, possibly before radio B's Channel Selection Response on radio B's. Radio B is
   then left in channel_selected until em_config's time limit renews the agent (three rounds after
   a boot, 13 October). A controller patch (meta-cmf 0268, draft) lets a report complete a radio
   still pending. EMOSA keeps its TLV order: reordering would hide the controller's race from
   every other agent that reports all its radios.
7. The agents' log lines on the router carry no timestamps, so their order against the
   controller's log has to be inferred.

## Lab access (rdk-1010 on rev120)
`ssh rev120` → `lxc exec rdk-1010` → `lxc exec bpibroadband` (the gateway: em_ctrl + emosa). Feed
scripts via `sh -s` on stdin to avoid nested quoting; binary via plain tar (busybox tar, no xz —
decompress on rev140). `pod-launch.sh launch` runs INSIDE rdk-1010 at `/root/handoff-rdk-1010`
(pod-1/, pod-2/), where the hwsim radios are — NOT on rev120. EMOSA config/state in bpibroadband
`/nvram/emosa` (fleet-config.json, agents/<serial>.json, state/<serial>). The agent's uplink station
in both `/nvram/emosa/agents/<serial>.json` and the fleet-config pods entry must match the pod's
`POD_STA_BANDS`. Controller REST 127.0.0.1:8888 (/api/v1/bsses, /devices, /topology); DataElements
via rbuscli. Controller patch 0216 creates the sta-mode BSS row from Device Information. The agent
overlay deploy (no opkg): old binaries backed up at `/nvram/emosa/rollback-149444a`.

## References
- Memory: `second-radio-and-onto-back` (and `rdk-1010-proof-traps`, `alignment-plan`).
- Spec: `spec/README.md` §8 (Scenarios), §8.6 (GRE parent + Finding A), §9 (second radio).
- Commits: `git log --oneline` on layer-b (search "second radio").
