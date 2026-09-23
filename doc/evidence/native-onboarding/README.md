# Bounded native controller → EMOSA → simulated OpenSync onboarding

The selected `native-radio-05` run completes the initial causal path using an
actual native prplMesh controller, EMOSA's packet endpoint, real OpenSync-schema
OVSDB, a separate hostapd/nl80211 manager, and independent wired/Wi-Fi clients.
Use the [walkthrough](../../protocol/native-onboarding.md) to reproduce it.

| Boundary | Retained observation |
| --- | --- |
| Discovery | Profile-1 Search/Response, corrected native `0xc0` flags |
| Early report and WSC | Early before M1; actual native M2; captured M1 hash equals durable receipt |
| Configuration | One operation and one Config transaction; old observed SSID while application is withheld |
| Application | Separate manager observes the new live AP; operation becomes `OBSERVED_APPLIED` |
| Capabilities | Native AP Capability query receives the selected capability and Device Inventory report |
| Native inventory | AL `02:00:00:00:30:01`, radio/BSSID `02:00:00:ec:02:00`, enabled fronthaul SSID `emosa-controller-trial` |
| Independent clients | Wired `eth1` and wpa_supplicant `wlan0` pass nonce-bearing HTTP and ping; new SSID beacons and EAPOL messages 1–4 captured |
| Restoration | Original native executable and references restored and hash-checked |

Run the independent check (Python standard library plus tshark; no EMOSA import):

```bash
python3 scripts/check-native-onboarding.py doc/evidence/native-onboarding/run-05
```

The immutable runner result says `observed_pending_capture_review`; the separate
`independent-check.json` records the subsequent bounded success verdict. The
original artifacts are not rewritten to turn a pre-review status into success.
`candidate.json` pins source/runtime/header inputs, both patches, compiler/build
recipe and binary digests. `source-hashes.json` records the staged Python/native
reference inputs. Both source patches are explicit evaluation changes; the
unmodified baseline is restored, not promoted silently.

`rejected-03/` retains the first protocol failure: real M2 contained default VLAN
settings and an empty advanced-BSS record, so EMOSA created no operation. Topology
queries also exposed EMOSA's incorrect same-profile restriction. The later fixes
respect the advertised feature scope and §6.2's sender-profile rule. Setup trials
01 (missing OVS binary environment) and 02 (post-reboot radio preflight without
restoration) remain privately retained; both restored the controller. Trial 04
also passed the causal radio/client path, but the selected 05 additionally waits
for the native AP Capability query and captures final source hashes.

This proves **one bounded simulated onboarding**, not a complete Profile-1 agent.
Policy configuration and channel requests are recorded as unsupported. The native
controller does not acknowledge Early Report, and its controller/helper still
abort during shutdown; the original exit records remain in `result.json`.
The report worker ends before client association because this radio fixture lacks
complete station inventory/age publication. The controller's zero metric fields
are defaults, not measured telemetry. Continuous management, recovery in this
native path, another controller and physical qualification remain further work.

Publication uses an explicit allowlist. PCAPs contain only owned synthetic lab
identities/configuration. Private journals, secret files, full native logs,
backups, binaries and standards PDFs are not published. The physical acceptance
objective still requires an unchanged OpenSync extender and independent behavior.
