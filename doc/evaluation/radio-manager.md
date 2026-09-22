# EMOSA, OVSDB and real hwsim observations

The additional [service integration walkthrough](../guides/service-integration.md)
now tests an actual `emosa serve` process with a pod-initiated OVSDB connection,
independent hwsim clients and recovery after `SIGKILL`. Its separate
[evidence summary](../evidence/service-integration/summary.json) complements the
historical thirteen-case engine-in-runner results below. It does not turn either
experiment into a controller-originated or physical-pod proof.

The semantic EMOSA operation engine now changes an actual hostapd AP through
OVSDB and an independent manager. Three selected repeat runs passed all 13 cases
with separate wired and wpa_supplicant client containers. The experiment uses
the pinned OpenSync schema, one synthetic AP and wired backhaul. **No EasyMesh
controller exchange or unchanged physical pod was exercised.**

The [deployment manual and architecture](../../deploy/radio-manager/README.md)
describe the process boundaries and reproduction commands. The
[execution summary](../evidence/radio-manager/summary.json) retains all eight runs,
including the earlier development and simpler-key runs, and identifies the three final
selections. The [sample index](../evidence/radio-manager/sample/index.json) links
the reviewed synthetic capture, manager observations and independent client
records from the final run.

| Case | Required observation | Final repetitions |
| --- | --- | --- |
| Initial state | Seed starts disabled; manager observes live AP before positive State | 3/3 |
| SSID/key change | EMOSA guarded transaction, actual radio change, both clients pass; duplicate request keeps one operation/write | 3/3 |
| Withheld application | New Config, old live AP and State; operation times out; old clients still work | 3/3 |
| Late application | New radio/client behavior, original timeout retained with late resolution | 3/3 |
| Lost reply | Real reply discarded, observed application with unknown commit attribution and one attempt | 3/3 |
| Wrong key | Fresh supplicant WRONG_KEY; correct-key reconnection then passes | 3/3 |
| Unsupported channel | Requested channel 11 rejected by manager; live channel 6 and clients preserved | 3/3 |
| Adapter restart | Reconstruct backend/engine from journal; attribution and traffic preserved | 3/3 |
| Database/manager restart | New OVSDB session generation, fresh manager observation and clients | 3/3 |
| Backhaul loss | Both clients detect failure although radio State remains enabled | 3/3 |
| Backhaul recovery | Both interface-bound application probes succeed again | 3/3 |
| AP unavailable | State becomes disabled and Wi-Fi traffic fails | 3/3 |
| AP recovery | Manager restart applies current Config; clients recover | 3/3 |

Final review found that wpa_supplicant does not decode JSON escapes in its
quoted PSK field. The client helper now derives the supported hexadecimal PSK
with the standard library. Final runs include a literal quote and backslash in
the changed passphrase; earlier simple-key runs remain in the history.

The manager obtains SSID/security from live hostapd control reads and compares
SSID/BSSID/channel against nl80211 interface observations. It never constructs
positive State from the requested Config. Unit/OVSDB tests also force disagreement,
missing AP reads, unsupported extra credentials, and a Config race during
observation. Publication guards reject the raced snapshot.

Every client result has a fresh nonce, the observed originating IP, the bound
interface, and a case identifier linking it to the operation in the run report.
The wireless client is pinned to the AP BSSID. Radio captures independently show
beacons for each configured SSID and the client WPA handshake. They do not
establish an EasyMesh WSC exchange. Operation completion and client success remain
separate fields; the backhaul-loss case demonstrates why both are needed.

This manager is lab code, not native OpenSync. It restarts hostapd when applying
changes, has a deliberately narrow single-BSS/security profile, and does not
provide a production freshness lease when the manager itself disappears. The
adapter-restart case reconstructs the engine/backend/journal in the runner; it
does not claim an external service or whole-VM reboot test. All raw records,
private synthetic configuration and sources are retained in `.lab/radio-manager/`.

Next, connect the pinned controller to EMOSA's virtual-agent packet endpoint,
complete discovery/WSC and bind the full validated radio request to this boundary.
The exact missing IEEE editions and related materials remain in the
[acquisition checklist](../protocol/specification-acquisition.md). No specification-dependent
wire gate was removed. Physical qualification remains pending a populated private
connection file; the [read-only collector](../guides/pod-qualification.md) is ready for that
input. The final viability criterion is still real EasyMesh → EMOSA → unchanged
OpenSync pod → independently observed behavior.
