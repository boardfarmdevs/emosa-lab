# Restricted discovery-session evidence — 2026-09-22

This collection establishes selected Search/Response correlation before
read-only topology reporting from an owned real OpenSync-schema database.
It does not establish native controller onboarding, automatic Early Report/M1
admission, an operation-engine connection or physical-pod behavior.

- [Summary](summary.json): source hashes, scope, suite counts and development findings.
- [Unit](unit-results.xml) and [OVSDB](ovsdb-results.xml): **898 / 49** passing tests;
  32 new lifecycle units and one new real-database exercise.
- [Installed package](wheel-check.json): 69 Python files match the wheel; locked
  dependencies; the ten-stage database exercise ran outside the checkout.
- [Database result](database/result.json) and [trace](database/messages.pcap):
  incompatible Response, explicit retry, observed State and fresh discovery after
  actual database restart. Ethernet delivery is in memory; OVSDB uses real sockets.
- [Run 1](run-01/driver.json) and [run 2](run-02/driver.json): actual AF_PACKET over
  owned isolated namespace/veth pairs. Both workers retain JSON and receive-byte
  PCAPs in their corresponding directory. Each run receives seven/six frames.
- [Host](reference-host.json) and [VM](reference-vm.json): independent tshark
  3.6.2/4.2.2 checks of Search/Response fields, MID relationships and old/old/new
  SSIDs. The deficient and disconnected Queries receive no captured answer;
  worker JSON records at least 1.1 seconds of active observation for each.
- [VM provenance](vm-provenance.json): exact staged core-source and retained
  OVSDB tool hashes; owned namespaces removed. No native controller was started.
- [Wire gate](wire-gate.json): full wire scenario stays blocked with zero operations.

PCAP timestamps are synthetic; received bytes are real in the VM runs. The
fixture administrator and separate simulated manager change their own database.
The adapter monitor reads only selected noncredential columns and creates no
Config writes. Capabilities and adjacency remain explicit fixture declarations.

The new native-field unit cross-check reads the existing retained native capture;
it does not generate a new live native-controller result. Known missing fields
and the Table 117 conflict remain visible. See the
[walkthrough](../../protocol/discovery-session.md) and
[manual §13.11](../../guides/team-manual.md#1311-discover-the-controller-before-reporting-the-simulated-pod).
