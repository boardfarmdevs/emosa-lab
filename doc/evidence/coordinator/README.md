# Read-only report coordinator evidence — 2026-09-22

[Walkthrough](../../protocol/report-coordinator.md) · [Summary](summary.json)

This increment joins real database observations to a restricted report
coordinator. It handles Topology Query dispatch and Early Report Ack/retry,
without discovery/profile admission, WSC or an operation-engine callback.
Its controller peer and radio capability/virtual-adjacency inputs are synthetic.

| Check | Retained outcome |
| --- | --- |
| Unit suite | [866 passed](unit-results.xml), including 42 new coordinator checks |
| Real OVSDB suite | [48 passed](ovsdb-results.xml), including two new database-backed checks |
| Database exercise | [Eight stages](database/result.json) and [serialized frames](database/messages.pcap) |
| Final Ethernet run 1 | [Driver cleanup](run-01/driver.json), [coordinator](run-01/left.json), [peer](run-01/right.json) |
| Final Ethernet run 2 | [Driver cleanup](run-02/driver.json), [coordinator](run-02/left.json), [peer](run-02/right.json) |
| Independent packet fields | [tshark 3.6.2](reference-host.json), [tshark 4.2.2](reference-vm.json) |
| Installed wheel | [66 source files match; real database exercise passes outside checkout](wheel-check.json) |
| VM runtime | [Source and retained OVSDB binary hashes](vm-provenance.json) |
| Full wire-operation gate | [Exit 5, blocked, zero operations](wire-gate.json) |

The final VM executions were `run-05` and `run-06` under the recorded staging
directory; they are curated here as `run-01` and `run-02`. Four earlier successful
development runs preceded the final full response-window observation. The final
runs also include the unknown-channel guard. Their private output directories were preserved. The final staged
sources match the recorded checkout hashes. No upstream runtime was rebuilt.

Each worker PCAP contains five actual received Ethernet frames. The peer sees
Early Reports with new MIDs 1 and 2 and acknowledges 2. It sends Queries
600/601/602/603 and receives Responses only for the first three. Their observed
SSID sequence is old/old/new: a fixture Config change alone does not become
operational state, while the separate manager's State update does. After database
loss, the peer observes at least 1.1 seconds without a reply, with the coordinator
still running. PCAP timestamps are synthetic and cannot measure that interval;
worker monotonic timing and source-withdrawal events establish the bounded check.

The database initiates a local JSON-RPC connection to a noncredential read-only
monitor. The coordinator makes **zero Config writes**. The fixture administrator
and separate simulated manager explicitly perform changes. The HOST/installed
exercise also tests missing association age, inventory restoration and database
reconnect. Regression checks cover incompatible security and an unknown channel.

The [summary](summary.json) retains the empty-client-table handling, unknown-channel
guard and observation-window refinement. Five long JUnit parameter names are
compacted to function name plus SHA-256; test counts, times and outcomes remain.
All 415 earlier allowlisted evidence artifacts keep their original bytes.

Reproduce using the [learning guide](../../protocol/report-coordinator.md) and
[VM runbook](../../../deploy/wire/README.md). No physical pod was contacted, no
radio was needed, and no native controller accepted this agent. An Ack remains a
receipt, not permission to start M1. The next boundary is full discovery/profile
and capability admission, native controller inventory, then durable WSC-to-operation
integration. The target remains **real EasyMesh messages → EMOSA → unchanged
physical pod → independent client behavior**.
