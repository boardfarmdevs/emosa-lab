# EMOSA in C: a lab prototype

A second implementation of the EMOSA agent, in C, for the RDK EasyMesh lab
(`doc/architecture/rdk-lab.md`). It is a **lab prototype**, written with an AI
assistant: it is not production code, and the board proposal's rule (no
AI-written production code) applies to it.

What makes it interchangeable with the Python reference (`src/emosa`):
- the **same contract**: the specification (`spec/README.md`) and its
  conformance vectors (`spec/conformance`), which `emosa-vectors` replays;
- the **same interfaces**: the agent configuration
  (`schemas/agent-config.schema.json`), the status file it writes, the pod's
  OVSDB and the controller's 1905 LAN. The fleet stays in Python and starts
  either agent for a pod.

## Scope and order

| Part | Reference | Vectors | State |
| --- | --- | --- | --- |
| 1905 envelope | `emosa.wire.cmdu` | `cmdu.json` | done |
| WSC M1/M2, onboarding | `emosa.wsc`, `emosa.wsc_messages`, `emosa.wire.autoconfiguration` | `onboarding.json` | done |
| Pod view and 1905 TLVs | `emosa.opensync.easymesh_view`, `emosa.wire.reports` | `translation-northbound.json` | done |
| Control plane (channel, policy, steering) | `emosa.wire.channel`, `.reporting_policy`, `.steering` | `control.json` | done |
| OVSDB writes (M2, steering, uplink) | `emosa.opensync.pod_profile`, `.steering`, `.uplink` | `translation-southbound.json`, `steering.json`, `uplink.json` | done |
| Pod statistics | `emosa.opensync.stats` | `telemetry.json` | done |
| Agent runtime (config, OVSDB session, Ethernet, status) | `emosa.agent.pod` | live in `rdk-emosa` | done, gaps below |
| Yocto recipe for the RDK lab image | | | |

## The agent runtime

`emosa-agent-c CONFIG.json [--profiles DIR]` takes the Python agent's
configuration and writes the same status file, marked
`"implementation": "c-lab-prototype"`. Profiles come from `--profiles`, else
`EMOSA_PROFILES`, else `/usr/share/emosa/profiles`. One owner thread runs a poll
loop over the pod's OVSDB connection (`ovsdb.c`: the pod dials in, as it does to
the Python agent) and the 1905 socket (`ethernet.c`: AF_PACKET, ethertype
`0x893A`); the pod state is refreshed every 0.5 s under a 1.5 s lease.

Checked live in `rdk-emosa` against RDK's controller, in place of the Python
agent for `pod-1`: search, M1, the five-BSS M2 set applied to the pod, the
controller configuring the pod's radio, a steering mandate from `steer.sh`
carried out by the pod (the station left the source BSS).

Not in the C runtime yet (the Python agent has them):
- the durable operation journal: a restart forgets what was submitted, and
  steering windows a previous process left open are not closed;
- the uplink (EasyMesh backhaul STA) and telemetry scopes, and with them the AP
  metrics from the pod's statistics (spec §3.8; the statistics decoder, survey
  included, is here and checked by the vectors);
- Link Metric and AP Metrics answers (the queries are counted, not answered);
- probe requests from the band-steering report: an Unassociated STA Link
  Metrics Query is answered, but every station is refused (spec §3.9);
- retries of the Early AP Capability Report;
- schema validation of the configuration and status.

## Build and check

```sh
cmake -S c -B c/build && cmake --build c/build
c/build/emosa-vectors spec/conformance
```

Needs cJSON and, from the WSC part on, OpenSSL (libcrypto). Both are in the
RDK-B images.
