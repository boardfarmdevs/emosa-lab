# Next experiments toward a bounded viability result

The critical milestone is **real EasyMesh messages → EMOSA adapter → unchanged
physical pod → independently observed behavior**. Neither an OVSDB component pass
nor a successful hwsim association completes that objective.

The [service integration walkthrough](../guides/service-integration.md) now
provides a two-pod service exercise, actual process-crash recovery with hwsim
clients, and live native-controller preparation. The
[retained results](../evidence/service-integration/summary.json) keep semantic
initiation separate from the absent EMOSA wire exchange. The
[available WFA audit](../protocol/procedure-audit.md) advances the contract while
the exact IEEE base/amendment inputs remain pending.

The user-facing proof is specifically **an EasyMesh controller discovers and
onboards an OpenSync extender as another EasyMesh agent, represented by the
adapter around the controller**. The extender remains an OpenSync device;
EMOSA owns the protocol endpoint and maps its agent/radio/BSS identity to the
actual pod. Provisioning is the first management action after that discovery and
onboarding proof, not a substitute for controller-visible agent membership.

Select an actual controller/build early. Capture its topology/agent inventory
and protocol trace before discovery, after onboarding, after a supported BSS
change, and after reconnect. Advertise only capabilities supported by the
qualified pod mapping. A passing internal semantic API call is insufficient.

The [prplMesh 6.0.0 candidate](../../deploy/peer/README.md) now has verified startup,
an empty agent inventory and independently captured discovery traffic delivered
to the EMOSA container. This establishes the peer and transport setup for the
next implementation. No EMOSA exchange has occurred; the candidate's native
controller/helper shutdown aborts are a recorded qualification gap.

A separate [controller–standard-agent baseline](../../deploy/peer-baseline/README.md)
now exercises that controller with a normal native prplMesh agent and independent
wired/wireless client containers. Its Ethernet and WPS/hwsim paths establish the
existing peer's behavior before inserting EMOSA. This is a separate experiment
from the empty EMOSA-facing inventory above. Its observed native exchanges do
not remove EMOSA's specification or physical-pod gates.

## Priority and exit evidence

| Order | Work | Exit evidence / decision |
| --- | --- | --- |
| 1 — P0 | Freeze proposed editions and procedure subset; obtain missing lawful IEEE text; complete normative rules and independent packet/crypto vectors | Reproducible encodings, authentication rules, timers and radio-wide BSS semantics with exact references |
| 2 — I3/I4 | Implement actual packet endpoints, discovery/capabilities and genuine WSC provisioning; bind to the operation engine | Wire-driven OVSDB simulation and captures; valid/invalid authentication, duplicate/retry and lost-reply cases; no semantic fallback |
| In parallel — M0 | Run read-only qualification when trusted local inputs arrive; inspect pod/build, schema, managed radio/VIF, writers, recovery and client | Reviewed profile; sole-BSS radio or full radio-scope mapping; actual wired management and independent observer |
| 3 — I5 | Execute one SSID/PSK change over wired management on an unchanged physical pod | Correlated EasyMesh exchange, Config delta, fresh State, observed BSSID/SSID, station authentication and usable traffic |
| 4 — recovery | Repeat with lost acknowledgement and adapter restart; then qualify wireless management independently | Truthful unknown attribution; bounded recovery; no duplicate effects or unsupported rollback; outage/intervention duration retained |
| 5 — I6/I7 | Reproduce on retained Ubuntu/LXD images and a named independent EasyMesh controller | Same procedure from a separate peer, exact versions and a clean-environment rerun |

M0 collection can progress while P0/I3/I4 proceed. hwsim can independently qualify
the observer and a future lab manager; it must not postpone a ready physical-pod
experiment. Optional native OpenSync R0 and an expanded UI are not acceptance gates.

## What would demonstrate viability?

Declare the procedure and target tuple before the run: controller/version,
EMOSA revision, pod model/firmware, actual schema fingerprint, radio/BSS scope,
security representation, management direction/trust and deadline budget.

For that tuple, retain evidence that:

1. The controller discovered and onboarded the extender's EMOSA representation
   as an agent, with the expected stable identity, topology and qualified radio
   capabilities. It then sent a genuine selected provisioning procedure and
   EMOSA processed its complete supported scope, including radio-wide BSS rules.
2. Existing pod managers accepted and applied the qualified change, with
   unrelated fields and management/recovery behavior checked.
3. A separate physical station observed the intended BSSID/SSID, authenticated
   with the new credentials, and exchanged traffic over its Wi-Fi interface.
4. Invalid requests and unsupported scope caused visible, correct outcomes.
   Lost replies/restarts did not produce invented success or untracked effects.
5. Another run from the same inputs reproduced the result. A named independent
   peer repeated the wire path before any third-party interoperability claim.

A useful negative result identifies a concrete mismatch: a required BSS teardown
that cannot be represented, unavailable security encoding, cloud writer
interference, an unrecoverable management outage, or a controller expectation the
unchanged pod cannot meet. Record where it failed and whether adaptation is
possible within the no-new-pod-software constraint. One successful tuple does not
establish full EasyMesh conformance or support for all OpenSync pods.

## Radio/client work

The [optional hwsim harness](../../deploy/hwsim/README.md) uses hostapd and
wpa_supplicant in separate LXD containers and interface-bound traffic after
removing setup Ethernet. Its standalone scope must remain explicit.

The [OVSDB/hwsim integration](../evaluation/radio-manager.md) now connects semantic EMOSA
Config changes to a separate hostapd manager and derives State from
hostapd/nl80211. Three selected runs passed 13 cases with independent clients,
including wrong-key rejection, SSID/key change, lost reply, withholding, restart
and data-path failures. The next integration is genuine controller discovery/WSC
through EMOSA into this boundary, once the missing normative inputs are available.

For real pods use a physical Wi-Fi NIC in the observer, with an isolated client
data path and a separately verified management/recovery path. Archive captures
privately, review them for publication, and retain package/kernel/image details
and hashes. RF performance, roaming and interference modeling are later
experiments, not initial viability criteria.
