# Conformance vectors

Recorded inputs and the exact outputs the [specification](../README.md)
requires. The Python reference implementation reproduces all of them
(`tests/test_conformance.py`). An implementation in another language
conforms when its own harness reproduces them too.

| File | Spec | Input | Expected |
| --- | --- | --- | --- |
| `al-mac.json` | §2.2 | a serial number and the AL MACs already taken | the agent's AL MAC |
| `operation-transitions.json` | §5 | none | the legal operation state transitions and the active states |
| `fleet.json` | §4 | a fleet configuration and pods arriving in order (their `AWLAN_Node` rows) | per arrival: the registry entry (without timestamps), the agent configuration, and the `manager_addr` update |
| `translation-northbound.json` | §2.6, §3.3 | the pod's OVSDB rows, plus the agent's parameters | the device view, the capability TLVs, the Device Inventory TLV, and the Topology Response TLVs for each message set; with stations' association times, the Topology Response TLVs at several response times (ages as of each response) |
| `translation-southbound.json` | §3.2, §3.4 | the pod's OVSDB rows, a pod profile, and an accepted M2 intent | the result status and every OVSDB transaction sent, operation by operation |
| `cmdu.json` | §2.1 | a message (addresses, type, MID, TLVs, relay flag, MTU); received frames | the Ethernet frames the agent transmits; the reassembled message, none while incomplete, or the rejection's reason code |
| `control.json` | §2.4, §3.4, §3.7 | the recorded pod rows under a provisioned agent, and controller request frames in order | per request: the agent's result and the exact frames it sends (its own messages take MIDs from 500); the steering mandates it hands to the pod |
| `steering.json` | §3.7 | the recorded pod rows (with and without an existing steering group and neighbor) and one steering intent | the open, kick and close OVSDB transactions |
| `onboarding.json` | §2.5 | the agent's identity, radio and M1 device facts, the fixed entropy of the independent WSC fixture, and controller Response and M2 frames, per message set | the Search and M1 frames; the Response's admission; the M2's BSS settings; the rejection of an M2 with a changed Authenticator |
| `telemetry.json` | §3.6 | the pod's statistics topic and interval, a fixed clock, and MQTT publishes: three recorded `sts.Report` payloads, then a repeated, a retained and a foreign one | per publish: whether the report is used, and the statistics status after it |
| `uplink.json` | §8.3 | the pod's OVSDB rows (one pod on its bootstrap GRE uplink, one on a Multi-AP backhaul), a backhaul station, and an uplink intent | the uplink as `cm` and `owm` report it; for an EasyMesh backhaul, its view, the Topology Response TLVs and the Backhaul STA Radio Capabilities TLV; the switch's result status and OVSDB transaction |
| `metrics.json` | §3.8, §10 | the recorded pod rows, the pod's statistics publishes, and controller frames and ticks in time | per step: the result and the frames sent (Multi-AP Policy, Link Metric and AP Metrics answers, periodic AP metrics); the kept policy record |
| `backhaul-steering.json` | §9 | the pod rows with a Multi-AP backhaul station, Backhaul Steering Requests, the uplink scope's answers and the move's outcomes, across a session renewal | per step: the result and the frames sent; the moves handed over; the last session's status |
| `scope-writes.json` | §3.6, §3.7, §3.9 | the pod rows and one intent per scope (statistics publishing, probe watch, a steering window over a watch row) | the plan's refusal, or the submission's status and every transaction sent |
| `engine.json` | §5 | a scripted pod and requests, executions, clock steps, reconciles, recoveries and cancels | per step: the request's error, every operation's lifecycle fields, the pod's ownership and the journal events added |
| `early-report.json` | §2.5 | the pod's capability TLVs, notifications, ticks, Acks and State changes in time | per step: the Ack's result and the frames sent; the delivery counts |
| `steering-queue.json` | §3.7 | the pod rows and client steering mandates and ticks in time | per step: the refusal, the transactions sent and the steering status |
| `probe-watch.json` | §3.9 | the pod rows and the stations asked about, in time | per step: the transactions sent and the watch's status |
| `session-timing.json` | §2.5 | the pod's State (available, its database generation) and Renews in time; controller contact and the unserved and awaiting-M2 conditions in time; M2 acceptance and the Topology Responses sent | per step: whether an attempt started, the Searches sent, how it ended, and the back-off; the reasons to renew; whether the clients are announced again |

Formats:
- **OVSDB rows** use raw RFC 7047 JSON, `{table: {uuid: row}}`, exactly as a
  monitor or select returns them. They are real rows from an opensync-lab
  pod (OpenSync 6.6.1.0), with keys replaced by placeholders, plus two
  derived cases:
  - `fronthaul-and-backhaul`: a backhaul slot VIF is added;
  - `cold-pod`: the fronthaul VIF is removed.
- **TLVs** are `{"type": "0x..", "value": "<hex of the value only>"}`, in
  message order.
- **Frames** are whole Ethernet frames in hex (destination, source, EtherType
  `893a`, the 1905 header, TLVs, end of message), as sent or received.
- **MAC addresses** are lowercase `aa:bb:cc:dd:ee:ff`. Other byte strings are
  plain hex.
- **The device view** lists radios by RUID and BSSes by interface name. The
  topology vectors report every BSS of the view in that order.
- **Passphrases** are public test values, given by reference in `passphrases`.
  A vector never contains a real one.
- **Station ages** are seconds since association. Values above 65535 are
  reported as 65535.

To regenerate the vectors after an intended change of behaviour, which must
start with a change to the specification:

```sh
uv run python -m emosa_lab.conformance generate
uv run python -m emosa_lab.conformance check
```

Not covered by vectors yet, but by the reference implementation's tests:
- the WSC M1/M2 cryptography beyond `onboarding.json`, which has independent
  hostap vectors in `tests/fixtures/protocol/wsc-messages`;
- the Topology, AP Capability and Client Capability answers as whole
  messages, whose TLVs `translation-northbound.json` covers;
- the AP scope's M2 writes over a live pod (a Wifi_VIF_Config row changing
  under a write), which the RDK lab's room suite exercises (`docs/concepts/rdk-lab.md` §5);
- the fleet's supervision of agent processes.
