# Native counters under simulated medium loss

This is a bounded measurement experiment, not complete sustained-operation or
physical-pod acceptance. The selected unmodified wmediumd build applies 20%
AP-to-client frame loss in the owned hwsim lab while the native controller,
EMOSA, simulated pod manager and independent clients run normally.

`native-medium-loss-05` completed **216.08 active seconds**, 26 connected-phase
samples and eight complete removal-time station records. The original native
controller binary was restored; cleanup and the separate idle/process/interface
checks pass. The retained native shutdown behavior remains visible in the result.

| Observation across eight lifetimes | Result |
| --- | ---: |
| AP unicast TX submissions | 983 |
| TX bytes before the observed CCMP overhead | 115,994 |
| Medium status marked ACKed | 788 |
| Medium failed status and kernel `TX_FAILED` | 195, exact agreement |
| Retries represented in medium status chains | 2,405 |
| Kernel `TX_RETRIES` | 185 — **not reconciled** |

Packet, byte and failure bookkeeping reconciles in every checked lifetime. The
retry discrepancy remains explicit in
[the independent result](native-medium-loss-05/independent-accounting-check.json).
This is not a qualified final-counter source or a successful online conversion.
All 9,961 captured radio submissions have captured medium status; the per-session
check also requires the kernel to accept each selected status message. The kernel
rejected 2,217 offered RX copies; those are retained as rejections, not successful
delivery. This alone does not identify the cause of each rejection.

All capture counts equal their filter counts with **zero reported kernel drops**:
114 Ethernet packets, 11,916 radio packets and 63,975 netlink packets.
The runner records pod-connection recovery in **7.32 s**, including its deliberate
outage, and adapter SIGKILL recovery in **1.22 s**. Three fresh operations have
one total configuration-write attempt. The initial onboarding check and the
independent policy check pass. The 60-second policy survives both faults;
**three reporting periods remain unfulfilled**. These focused checks do not
certify continuous zero-loss traffic under an intentionally lossy medium.

Reproduce on HOST:

```bash
python3 scripts/check-medium-accounting.py \
  doc/evidence/medium-loss/native-medium-loss-05
python3 scripts/check-native-policy.py \
  doc/evidence/medium-loss/native-medium-loss-05
uv run pytest -q tests/test_medium_accounting.py
```

[The complete unit suite](unit-results.xml) passes **1,111 tests**. Its new tests
recheck the retained corpus, reject altered final byte/packet/failure
counters and capture loss, and check malformed netlink/rate-chain handling.
They also cover a corrected older ping gate: searching for the substring `0%`
had accidentally accepted `50%` and `100%`. The gate now parses the complete loss
percentage. The original 908-second recovery evidence is rechecked unchanged.

The [operator guide](../../protocol/medium-loss-accounting.md) explains setup,
interpretation and limitations. The [failed-attempt index](failed-attempts.json)
retains outcomes and artifact hashes for RF-kill, address-profile and capture-loss
attempts. Their full evidence remains in the named private owned-VM directories.
None is counted as successful accounting evidence.

The source review now includes ten exact Ubuntu 6.8.0-139.139 files. Reproduce it
with `scripts/review-station-kernel.py`; compare the
[source provenance](source-provenance.json). This reconstructs selected source,
not a reproducible kernel binary. The original seven counter-audit hashes are
unchanged.

The medium's rate and duration model remains unsuitable for qualified HT airtime
or PHY reporting. A medium TX status is not itself an independent RF observation.
Required AP/radio/STA/neighbor reports and the online final-session source remain
unqualified; all full sustained and physical-pod acceptance flags stay false.
