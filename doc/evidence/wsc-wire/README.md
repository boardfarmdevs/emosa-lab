# Ethernet WSC, real OVSDB and independently observed Wi-Fi — 2026-09-22

The synthetic hostap payload peer sends M2 over an actual isolated Ethernet
pair. EMOSA authenticates it, creates one durable `wsc-component` operation,
and changes owned OVSDB Config. The radio runs then apply it through a separate
hostapd/hwsim manager and verify separate wired/wpa_supplicant clients. There is
no semantic submission supplying the change.

**This is component causality, not native-controller onboarding or physical-pod
acceptance.** No OpenSync firmware runs. The regular service's full-wire gate
stays blocked, and no physical pod is contacted or changed.

- [Summary](summary.json): scope, exact source hashes, selected runs, development
  findings and remaining work. [Runtime](runtime.json) records the VM/kernel and
  independent tool hashes. Radio runs retain their exact staged source hashes.
- [Unit](unit-results.xml) and [OVSDB](ovsdb-results.xml): **928 / 53** passes,
  including eight new packet-session units. Parameterized test names are replaced
  by stable hashes; counts/outcomes are preserved and original digests retained.
- [Packet normal](packet-normal/left.json) and
  [packet lost reply](packet-lost-reply/left.json): actual AF_PACKET receipt,
  real database and separate simulated State publisher, without a radio.
- [Radio normal](radio-normal/result.json) and
  [radio lost reply](radio-lost-reply/result.json): packet-driven Config through
  hostapd/nl80211 State and four independent client phases, including withheld
  application, changed SSID, wrong-key rejection and correct-key recovery.
- [Independent capture check](independent-reference.json): external tshark
  Ethernet/CMDU headers, separate TLV-fragment reader, M1/M2/receipt correlation,
  radio beacons and WPA handshake fields. The public client reports are checked
  for distinct phase nonces, intended interface/BSSID and application peer IP.
- [Hostap vectors](independent-wsc.txt): existing cryptographic/payload reference
  checks pass; each packet peer's `right.json` records its independent C helper's
  pinned source/build/binary provenance. The capture checker does not decrypt M2.
- [Interruption cleanup](cancel-result.json): SIGTERM to the driver while its
  owned peer is stopped removes its workers and namespaces. The
  [original transport mode](transport-regression.json) also passes.
- [Installed wheel](wheel-check.json): all Python source files match the installed
  package, new packet modules import with isolated Python outside the checkout,
  and the four earlier durable-handoff cases pass there. Privileged packet/radio
  runs use staged source, not that installed wheel.
- [Full-wire gate](wire-gate.json): expected exit 5, blocked, zero operations.

## Read a normal run

Open [the adapter result](radio-normal/ethernet/left.json) and
[the peer result](radio-normal/ethernet/right.json). The rejected first exchange
creates no operation/credential/transaction. Fresh M1 uses a new nonce, public
key and MID. The second exchange rejects a wrong RUID, waits for both fragments
of MID 503, then creates one operation. Identical MID 504 and freshly encrypted
MID 505 reuse it; changed configuration at 506 cannot create another operation.

The operation receipt's M1 digest equals the second captured M1. The selected
RUID is `02:00:00:00:50:10`; the radio fixture's BSSID is
`02:00:00:ec:02:00`. The fixture binds these explicitly; it does not take target
authority from the MAC inside encrypted settings.

While the manager withholds application, Config names `EMOSA-WSC-component`
but State and [withheld clients](radio-normal/clients-withheld.json) still use
`emosa-radio-initial`. After release,
[changed clients](radio-normal/clients-changed.json) observe the new SSID,
authenticate at the pinned BSSID, and exchange fresh interface-bound traffic.
A [fresh wrong-key event](radio-normal/wrong-key.log) is required before
[correct-key recovery](radio-normal/clients-restored.json).

## Read the lost reply without inventing attribution

The [fault adapter result](radio-lost-reply/ethernet/left.json) starts
`INDETERMINATE` because the commit reply was discarded. The reconnect invalidates
old exchange write authority; later retries reject. Fresh observed State allows
`OBSERVED_APPLIED`, with **one attempt**, commit attribution still `unknown`,
and application attribution `current_condition_only`. The independent client
and radio checks also pass. Current observed success does not reconstruct the
missing historical commit reply.

## Preservation and limits

Each Ethernet folder contains seven received M2 frames and two received M1
frames. The Ethernet PCAP timestamps are synthetic; the bytes and their order
are actual socket input. Radio PCAPs use actual tcpdump capture times. Use M1
digests, operation receipts and phase nonces for correlation; do not infer packet
latency from the Ethernet timestamps.

This collection uses an explicit reviewed allowlist. Raw database/journal,
credential files, private exchange material and hostapd/supplicant configurations
remain private. The fixture key is intentionally public in the C source; the
retained captures contain synthetic encrypted traffic. No physical captures or
standards PDF excerpts are included. Earlier evidence collections are unchanged.

Selected radio runs are development labels `wsc-wire-03` (lost reply) and
`wsc-wire-04` (normal). An earlier corrected normal run also passed. The first
radio run reached positive client success but failed its wrong-key check because
the checker opened the wrong log path; it is recorded as a failed development
attempt in the summary. The first packet development run reset the M1 MID
allocator; selected captures verify the corrected sequence 101/102.

Use the [step-by-step walkthrough](../../protocol/wsc-wire-radio.md) and
[manual §13.13](../../guides/team-manual.md#1313-drive-wi-fi-from-an-ethernet-wsc-exchange)
to reproduce these finite cases. The next boundary is compatible native
controller discovery/profile/capability admission and its own inventory, then a
qualified unchanged physical pod and independently observed physical client.
