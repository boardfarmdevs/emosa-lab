# Discovery and WSC exchange component evidence

Read the [beginner exercise and normative contract](../../protocol/autoconfiguration.md)
and [summary](summary.json) for the exact boundary. These checks run on HOST;
no new live controller session, LXD/hwsim run or physical-pod connection occurred.

| Evidence | Result |
| --- | --- |
| [Unit suite](unit-results.xml) | 762 pass, including 56 new discovery/exchange cases |
| [Real OVSDB regression](ovsdb-results.xml) | 46 pass; existing semantic service remains separate from the wire component |
| [Native discovery](native-discovery.json) | Captured Search Profile 2 / Response Profile 1; capability `0x40` lacks selected-edition KiB/MiB bit 7 |
| [Independent envelope check](ieee-reference.json) | tshark agrees on 59 native frames and completed TLV boundaries |
| [Independent WSC check](wsc-reference.txt) | Pinned hostap 2.11 reproduces the retained M1/M2 payload fixture |
| [Installed wheel](wheel-smoke.json) | Isolated interpreter outside checkout; all 59 Python source files match the installed package; native capture inspection passes |
| [Full wire gate](wire-gate.json) | Exit 5, blocked and zero operations; no semantic fallback |

Tests cover exact and re-encrypted duplicate requests, changed/invalid M2s,
fresh-transcript replay rejection, peer/radio/generation mismatch, fixed lifetime,
bounded retries and unsupported complete configuration. The accepted result is a
secret candidate, not a write capability. A real controller has not yet discovered
or provisioned EMOSA through this code.

Five long synthetic JUnit parameter names are shortened to their function name
and SHA-256; test outcomes/counts/times are unchanged. The summary retains the
initial incorrect capability expectation, tightened re-encryption handling and
corrected Mermaid punctuation. Existing 380 evidence artifacts retain their
original bytes and historical scope. Licensed PDFs, extracted pages and private
runtime material remain outside Git.

Next implement the complete capability/topology/coordinator obligations and
durable operation integration, then run the causal controller → EMOSA → simulated
pod → independent hwsim client path. Physical acceptance still requires the
unchanged qualified pod and independent physical observation.
