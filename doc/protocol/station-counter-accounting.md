# Explain removal-time counters with an independent packet trace

**Status: six normal-traffic sessions reconcile with the selected Ubuntu kernel
accounting. EasyMesh conversion and live final-statistics delivery remain pending.**
This follows the [station-removal observation](station-removal-observations.md).
It answers what the collected integers count before connecting them to the
[final-session sender](final-session-statistics.md).

## Why another check is necessary

A counter name is not a definition. A transmit counter can increase before a
packet succeeds, or before encryption adds bytes. A receive counter can include
management frames differently from data frames. Copying these integers into
EasyMesh would produce plausible messages with the wrong meaning.

There is a second trap: an incomplete packet capture can make correct kernel
bookkeeping look wrong. The earlier `native-final-source-01` radio capture lost
120 packets at the capture socket. All six disconnect frames needed for its
event-correlation result were present, but that capture cannot establish complete
packet/byte accounting. Its original evidence and scope are preserved.

The harness now gives each tcpdump process an 8 MiB capture buffer and retains
its statistics. The independent health check rejects reported loss, unread or
unreconciled filter counts, truncated records, and inconsistent packet totals.
This check targets our Linux capture setup; tcpdump statistics have different
meanings on other platforms. Passing it establishes capture-point accounting,
not universal visibility into the network.

## What the new native experiment establishes

The [retained run](../evidence/counter-accounting/README.md) has **8,921 radio
records and 101 Ethernet records**, with matching filter totals and zero reported
kernel drops. It uses one HT20/CCMP station, actual native-controller onboarding,
six station removals, and both management recovery faults over 159.68 seconds.
It is a focused measurement experiment, not a replacement 15-minute acceptance.

An MPDU is the 802.11 MAC frame carried by the radio. Here its captured length
excludes the capture's radiotap metadata; this hwsim trace does not supply a radio
FCS. Neither this length nor a database byte counter should be assumed to mean
application payload bytes.

| Kernel field | Exact observation in all six sessions | Implication |
| --- | --- | --- |
| `TX_BYTES64` | Captured AP-to-station MPDU bytes minus 16 bytes for each protected transmission | Bookkeeping precedes the 8-byte CCMP header and 8-byte MIC; raw TX/RX byte counts have different boundaries |
| `TX_PACKETS` | Captured unicast transmissions after station creation; each has a separately captured ACK | Matches successful transmissions in this zero-failure corpus only; the source increments before success is known |
| `RX_BYTES64` | Sum of captured station-to-AP MPDU bytes after station creation | Includes the observed encryption framing at this receive observation point |
| `RX_PACKETS` | Captured frames plus four additional management-frame counts per session | Association request, two action frames and deauthentication each pass two packet-accounting sites |
| `TX_FAILED`, `TX_RETRIES`, `RX_DROP_MISC` | Explicit zero values in every final record | Nonzero failure/retry/drop semantics have not been exercised |

For example, the first session has 179 transmitted frames and 179 captured ACKs.
Its MPDU lengths total 23,839 bytes. Of those frames, 173 are protected:
`23,839 − 173 × 16 = 21,071`, exactly the kernel TX byte value. The receive trace
has 184 frames, while the kernel reports 188 packets and 23,342 bytes.

These are explanations of this kernel and workload, **not an enabled conversion
formula**. Adding a guessed overhead or subtracting four packets from every
future session would be wrong for other ciphers, frame forms and failure paths.
No raw-counter passthrough or measured final-statistics message is enabled.

## Reproduce the evidence checks — HOST

Start in the checkout with Python and tshark installed as in manual chapter 3.
No containers, radio changes or privileged access are needed to inspect retained
evidence. Run each command; a failed assertion means that scoped check failed.

```bash
python3 scripts/check-capture-health.py \
  doc/evidence/counter-accounting/native-counter-audit-01
python3 scripts/check-native-recovery.py \
  doc/evidence/counter-accounting/native-counter-audit-01 --minimum-seconds 150
python3 scripts/check-station-removal.py \
  doc/evidence/counter-accounting/native-counter-audit-01
python3 scripts/check-station-accounting.py \
  doc/evidence/counter-accounting/native-counter-audit-01
```

Read them in this order: capture integrity → actual onboarding and recovery →
association/reason/leave correlation → counter arithmetic. The accounting checker
uses transmit/receive MAC addresses, not bridged source/destination addresses.
It explicitly excludes the probe response and initial authentication request
that precede the corresponding kernel station bookkeeping. It rejects fragments,
A-MSDUs and retries, requires the observed management-frame pattern, and checks
every transmission against its own following ACK in capture record order.

Synthetic hwsim ACK timestamps can precede their corresponding MPDU timestamps
by microseconds even though their capture records follow them. The checker
therefore uses unique record ordering and a bounded absolute timestamp distance.
This establishes association/accounting for this sole-client trace; it is not an
RF interframe-timing measurement. No packet capture becomes an online production
telemetry source by passing these offline checks.

For a fresh owned-lab experiment, follow the staging and fresh-label commands in
the [observation guide](station-removal-observations.md), retaining
`--observe-station-removal`, `--recovery-checks` and at least 150 active seconds.
Collect with `scripts/collect-native-review.py`, then run the four checks above
against that new private review directory. New native runs require capture health;
historical results retain their original, narrower checks.

## Reproduce the exact runtime source review — HOST

The executed kernel is Ubuntu `6.8.0-139-generic`, package version
`6.8.0-139.139`. The evidence records installed package versions, module hashes,
module source versions and build identity. The source review reconstructs the
selected files from Ubuntu's base archive **and its distribution patch**, rather
than assuming Linux v6.8 is byte-for-byte the running distribution source.

```bash
python3 scripts/review-station-kernel.py .lab/kernel-accounting-learning-01
```

Choose a new output directory. This downloads about 239 MB into the reusable
ignored `.cache/upstream/linux-6.8.0-139.139` cache; provide `--cache PATH` to reuse
another copy. It requires the `patch` program, verifies pinned archive hashes,
extracts only seven selected files, applies their patch without fuzz, and checks
the resulting file hashes. It does not compile or install a kernel. The output
`source-provenance.json` must match the retained provenance. Sources come from
the official [Ubuntu source package description](https://archive.ubuntu.com/ubuntu/pool/main/l/linux/linux_6.8.0-139.139.dsc).

Read these locations in the reconstructed `source/` directory:

| Source | Relevant mechanism |
| --- | --- |
| `net/mac80211/sta_info.c`, `sta_set_sinfo()` and removal handling near lines 1471–1481 | Gather statistics before the final station-removal notification |
| `net/mac80211/tx.c`, statistics near line 1045 and handler order near line 1863 | Increment byte/packet counts before encryption and later transmission status |
| `net/mac80211/rx.c`, byte accounting near line 1768, defragment accounting near line 2385, management handling near lines 3790–3872 | Explain received MPDU bytes and the extra management packet increments |
| `net/mac80211/status.c` | Follow retry/failure status separately from transmit submission |
| `net/wireless/nl80211.c` and `include/uapi/linux/nl80211.h` | Check attribute presence, widths and removal-event encoding |
| `drivers/net/wireless/virtual/mac80211_hwsim.c` | Review simulated delivery/status and distinguish its survey values from measured airtime |

This verifies selected source provenance and mechanisms. It does not prove a
reproducible kernel binary or qualify all execution paths. Per-TID counters also
need review: the non-QoS bucket can include management frames, so selecting a
different kernel field is not automatically a correction.

## Remaining qualification and reporting work

EasyMesh 6.1 Table 58 and its selected Wi-Fi Data Elements reference govern the
wire quantities. The Wi-Fi Data Elements 3.0 package, including
`TR-181-2-17_DEr3.xlsx`, remains in the
[acquisition checklist](specification-acquisition.md). A useful public cross-check
is BBF's [TR-181 2.17 RetransCount definition](https://cwmp-data-models.broadband-forum.org/tr-181-2-17-0-cwmp.html#D.Device:2.Device.WiFi.DataElements.Network.Device.Radio.BSS.STA.RetransCount):
retransmitting the same packet twice adds two, not one. This corrects the idea
that the requested retry quantity must count distinct original packets. It does
not replace the pending selected Wi-Fi Alliance package or qualify Linux retry
accounting under failed/aggregated traffic.

Next exercise known failed, retried and queued traffic; establish the requested
error and byte boundaries; and implement an online source tied to an association
and restart epoch. Join the actual reason before submitting the complete final
record. Require the native controller's acknowledgment and independently decoded
values. AP airtime/ESP, STA policy reporting and IEEE 1905 neighbor measurements
remain separate gaps before the complete 15-minute acceptance can pass.

**Learning checkpoint:** explain the first-session arithmetic, identify the
capture-loss rejection, and explain why an exact accounting match still does
not qualify a wire conversion or an unchanged physical OpenSync pod.
