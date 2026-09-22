# IEEE 1905 envelope and packet evidence

This collection follows receipt of both IEEE PDFs on 2026-09-22. Read the
[learning exercise and clause review](../../protocol/ieee1905-envelope.md) and
[summary](summary.json) before interpreting a pass.

| Evidence | Result and scope |
| --- | --- |
| [Native inspection](native-inspection.json) | 59 frames → 58 complete messages, including one two-fragment WSC; no rejected/incomplete input; zero operations |
| [HOST reference](reference-host.json), [VM reference](reference-vm.json) | Independent tshark 3.6.2/4.2.2 agree on all headers and completed TLV boundaries; no EMOSA imports in extractor |
| [First left endpoint](run-01-left.json), [right endpoint](run-01-right.json) | Actual isolated AF_PACKET delivery of two empty Topology Queries each direction |
| [Repeated left endpoint](run-02-left.json), [right endpoint](run-02-right.json), [cleanup result](endpoint-02.json) | Repeated in fresh namespaces; only owned namespaces removed |
| [Unit results](unit-results.xml), [OVSDB results](ovsdb-results.xml) | 706 unit tests, including 32 new envelope tests; 46 real OVSDB regressions pass |
| [Installed-wheel smoke](wheel-smoke.txt) | Isolated Python loads installed envelope module and inspects 58 messages without an editable source import |
| [Full procedure gate](wire-gate.json) | Provisioning scenario remains blocked, exit 5; envelope implementation does not enable the unfinished procedure |

The four `run-*-*.pcap` files contain bytes from actual socket receives. Their
timestamps are synthetic; no independent sniffer timing or physical Ethernet/FCS
claim is made. The reverse-direction messages are new transport probes, not
Topology Responses. Existing radio/native services and physical pods were not
used. The native research container was briefly started for an earlier read-only
investigation, then stopped before this IEEE work; its files were not changed.

The summary retains two corrected development failures: a mistyped expected AL
address, and a source bundle missing the package initializer. Five very long
synthetic parameterized JUnit names were shortened to their function name plus
SHA-256; outcomes, counts and durations are unchanged. Existing evidence
collections are immutable and keep their historical “IEEE inputs missing” status.
Current acquisition and implementation status is in the linked protocol guide.

This is **envelope and transport component evidence**. No controller discovered
or onboarded EMOSA in these tests. No WSC message created a pod operation, and no
physical device was connected. The next integration must bind real controller
discovery/WSC to the guarded engine and independent radio/client evidence.
