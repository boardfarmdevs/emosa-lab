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

## Remaining, in order (do P3b first — 7f)
Work P3b on a **WIP branch off layer-b with frequent pushes**, merging to layer-b only when green,
so a compaction never loses it (7f). It is the central WSC onboarding — a half-done one breaks every
box scenario.

1. **P3b — per-radio M1/WSC (the central piece).** `make_bridge` and the exchange are per-radio
   (P3a) but the onboarding (`src/emosa/wire/onboarding.py`, class `OnboardingSession`, ~803 lines)
   still runs ONE exchange for `radios[0]`: it builds the bridge at line ~464 (via the factory =
   `make_bridge` lambda in `pod.py` ~835), one `ComponentProvisioningSession` (~476-491), one
   `awaiting_m2` state, and `receive()` (~321-365) routes the first M2 (msg_type 9) to that one
   provisioning (→ state "provisioning", ~346-350). Add, **additively** (dormant when
   `capabilities.radios` has one entry, so single-radio is unchanged = green): after the fronthaul is
   provisioned, if there is a second bound radio, start a SECOND `ComponentProvisioningSession` with
   `make_bridge(radio_index=1)` and a per-radio `ComponentTarget` (ruid =
   `capabilities.radios[1].basic.ruid`, bssid = `b-ap-50`'s from the view; the factory at
   `pod.py` ~820-845 builds one target for `radios[0]` today). **Do the exchanges SEQUENTIALLY**
   (fronthaul M1→M2→provisioned, then the 5 GHz M1→M2): only one provisioning awaits an M2 at a time,
   so the next msg_type-9 frame goes to it — no concurrent M2-routing-by-RUID needed, which is the
   simplification that makes this tractable. Each radio's M2 applies to its BSSes (the M2-set
   application already maps the backhaul BSS to `b-ap-50` via P2). Why it matters (7f): §8.6 rests on
   "the controller's M2 backhaul BSS makes a pod a GRE parent"; without a 5 GHz M2 the controller
   never decides for that radio and withdrawal-by-band (the absent-BSS path through the Band list)
   acts on the wrong radio — the shortcut the owner ruled out. Keep P4's operational-report
   modelling; it stays correct once each radio has its own exchange. Test with a multi-radio box
   scenario (none exists yet; the box fixtures already present a 5 GHz radio).
2. **item-9 channel rule.** `src/emosa/wire/channel.py` already declines a Channel Selection that
   excludes the sole operable channel, but is 2.4G-specific (line ~256 `set(range(1,14)) - {6}`).
   Extend per-radio so the 5 GHz radio is operable only on the station's channel; a selection off it
   is declined (EMOSA does not move a radio whose channel follows its station).
3. **Finding A (gre-parent live promotion).** Flipping a live pod's profile to `gre-parent` doesn't
   convert its existing `backhaul_bss` to a parent AP; only a fresh onboard does, because
   `pod_profile.role()` (`src/emosa/opensync/pod_profile.py` ~261) maps both a rendered parent AP
   (`multi_ap` none) and a plain `backhaul_bss` to `backhaul`, so reconcile's
   `snap.observed.satisfies(target)` (`src/emosa/agent/gre_parent.py` ~56) is already true and
   `_extra_ops`'s conversion never submits. Fix: the applied-predicate for a parent must expect
   `multi_ap` none plus the underlay `Wifi_Inet_Config`; add a live-promotion box scenario and re-run
   the fresh-provision path. (Recorded in spec §8.6 as "Finding A".)
4. **C mirror.** Mirror all of the above in `c/src` (the lab runs the C agent — on the critical path
   to the lab proof): the profile model (`radios`/`slot_bands`), the 5 GHz operating class, per-radio
   capabilities, the per-radio bind/render/topology, and per-radio WSC. Then `emosa-units`,
   `emosa-vectors`, `scan-build`, and the STRICT/c-cert CI gates.
5. **The (b) lab proof on rdk-1010.** Build + deploy the C agent at the new SRCREV, bring up pod-1 as
   the 5 GHz parent and re-stage pod-2 on `lnf_radius_5g`, extenders off, then steer pod-2 onto
   `b-ap-50` and back + the right-device steer + the layer-A regression, with times. Relaunch the
   pods from **`rpi-pod-amd64-20261012084755`** (yocto-53's credential fix: pod-bootstrap takes only
   bssid-less credentials) so the virtual proof runs V54's exact Pi pod code. Fewer soaks during
   dev: box + units + the lab proof are the evidence; full soaks at the end. Before each
   `SteerWiFiBackhaul`, show that the controller's model holds the child's 5 GHz backhaul station:
   0216's "Backhaul STA learned ... sta=<bhaul-sta-50's MAC>" log line and the station-mode row on
   its Device.N (7f, 12 October: the MV3 shows pods unrooted with no backhaul MACs today; this
   proof is the evidence V54 fixes it). The Pis' adapter-B role is its own profile,
   `rpi-pod-mt7921u-6.6.1-gre-5g-v1` (done; 7f: profile ids are contracts, an Ethernet-only Pi
   keeps `rpi-pod-mt7921u-6.6.1-v1`), with `b-ap-50` at 52_owm_prep's index 1 (yocto-53, read
   on pi1).
6. **Deferred: the layer-A pods-only check.** A pod onto a POD serving a Multi-AP backhaul BSS (not
   an extender) and back, pods-only, before the end soaks (the box already proves the move+revert
   [c]). `b-ap-24`/02:00:00:01:ca:c0 were only example targets.

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
