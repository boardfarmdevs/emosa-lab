# Capability and topology report evidence — 2026-09-22

[Learning exercise](../../protocol/reports.md) · [Summary](summary.json)

This collection tests restricted complete Early AP Capability Reports and
Topology Responses, with new IEEE topology/security/BSS/client value codecs.
It does **not** establish native controller onboarding or physical-pod proof.
The implementation uses synthetic complete facts; a future coordinator must
supply qualified actual facts and bind them to the authorized controller/pod.

| Check | Result / artifact |
| --- | --- |
| Unit suite | [824 passed](unit-results.xml), including 62 new report checks |
| Real disposable OVSDB regression suite | [46 passed](ovsdb-results.xml) |
| Offline synthetic exchange | [Three frames and zero operations](offline/result.json), [PCAP](offline/synthetic-reports.pcap) |
| Isolated AF_PACKET run 1 | [Driver and cleanup](run-01/driver.json), [sender](run-01/left.json), [receiver](run-01/right.json) |
| Isolated AF_PACKET run 2 | [Driver and cleanup](run-02/driver.json), [sender](run-02/left.json), [receiver](run-02/right.json) |
| Independent fields and generated messages | [tshark 3.6.2](reference-all.json), [tshark 4.2.2](reference-all-vm.json) |
| Installed package | [62 source files match and offline exercise passes outside checkout](wheel-check.json) |
| Full wire scenario | [Exit 5, blocked, zero operations](wire-gate.json) |
| Selected native compatibility diagnostics | [Missing required fields and media-length mismatch](native-findings.json) |

Each VM receiver PCAP (`run-01/right.pcap`, `run-02/right.pcap`) contains an Early
Report and a Topology Response. Each sender-side PCAP contains the received
Query. These are actual socket receive bytes with **synthetic timestamps**;
they are not independent latency measurements. The receiver decodes the fixture
radio/BSS and explicitly reports `native_controller_inventory: false`. Both
runs cleaned their owned namespaces and touched no physical interface or pod.
The two receive traces match because the fixture exchange is deterministic.

Reproduce the unit suite with `uv run pytest -m unit`, OVSDB with
`uv run pytest -m ovsdb` after building the pinned tools, and independent fields
with `python3 scripts/check-report-reference.py`. Add `--report-capture` for each
retained right-side PCAP to repeat the generated-message comparisons. Follow the
[VM runbook](../../../deploy/wire/README.md) for a new live socket run.

The [summary](summary.json) records source hashes, staged VM hashes and two
resolved development findings: updating an old unsupported-type test and checking
the clock after a potentially slow source callback. Long JUnit parameter names
are compacted to function name plus SHA-256; counts, times and outcomes remain.
All 392 earlier evidence artifacts retain their original contents.

The new compatibility findings supplement earlier native observations: absent
Supported Cipher Suites in Early Report; absent Bridging Capability despite five
local interfaces; Wi-Fi 6 media-specific length ten instead of selected-edition
zero. They are not silently repaired in the captured peer. Also, the pinned
OpenSync client schema has no association age. Actual qualification must find a
trustworthy existing source before that field can be advertised for a client.

The next boundary is a running trusted-link coordinator, full AP Capability and
profile/peer reconciliation, then native controller inventory and durable
WSC-to-operation integration. Simulator results cannot complete the target:
**real EasyMesh messages → EMOSA → unchanged physical pod → independent client**.
