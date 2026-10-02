# Existing BSS mapping: synthetic-existing-bss-v1

Qualification is limited to the pinned upstream schema plus EMOSA's independent
simulated manager. No actual pod/build appears in `../evaluation/supported-pods.json`.

| Contract | Implementation |
| --- | --- |
| Resource | Stable pod/radio/BSS IDs; explicit `lab-radio` / `lab-ap` binding |
| Inventory | Follow radio `vif_configs`, state `radio_config`, `vif_states`, VIF `vif_config` and client references; ambiguity disables writes |
| Initial state | Existing enabled AP using modern WPA2-PSK, CCMP, no legacy security map, designated existing `key` entry |
| Input | UTF-8 SSID of 1–32 bytes without NUL; printable ASCII PSK of 8–63 bytes read from a private file |
| Write | `Wifi_VIF_Config.ssid` update and `wpa_psks[key]` delete/insert mutation in one transaction |
| Guards | Zero-timeout `wait` on radio identity/VIF references and existing VIF identity, SSID, mode, enabled/security fields and complete PSK map |
| Preserve | All unrelated columns, other map keys/tags, bridges, VIF lifecycle, radio settings and `multi_ap` |
| Observe | Fresh complete monitor generation; state linked through both VIF and radio references; SSID/enabled/AP/security/key fingerprint match |
| Evidence | Config acceptance and State application are separate; a real independent client is still required for physical security behavior |
| Recovery | Resnapshot and rebind after disconnect; unknown transaction attribution stays unknown; no automatic replay |
| Deadline | Scenario-specific, with CLI wait independent; late application does not erase missed deadline |
| Conflict | Persist scope conflict and refuse more writes; requalification requires explicit administrative work |

Only the simulator manager writes State. Its commands are distinct test-device
actions, not application acknowledgments manufactured by the adapter. A rejected
or withheld simulated application leaves Config committed and State unchanged;
the operation reaches its application deadline.
