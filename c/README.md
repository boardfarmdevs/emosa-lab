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
| Agent runtime (config, OVSDB session, Ethernet, status) | `emosa.agent.pod` | live in `rdk-emosa` | next |
| Yocto recipe for the RDK lab image | | | |

## Build and check

```sh
cmake -S c -B c/build && cmake --build c/build
c/build/emosa-vectors spec/conformance
```

Needs cJSON and, from the WSC part on, OpenSSL (libcrypto). Both are in the
RDK-B images.
