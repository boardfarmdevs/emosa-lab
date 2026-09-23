# Native reporting-policy receipt and schedule recovery

The **212.10-second** `native-policy-receipt-01` experiment checks policy receipt
with the actual native controller and both management recovery faults. It closes
the unanswered policy Ack gap. **It does not fulfill the requested metric reports
or complete the 15-minute sustained acceptance.**

- All three native Policy Config Requests retain the 60-second interval and all
  three enabled STA inclusion bits. The controller's policy was not weakened.
- EMOSA records each selected request durably before its same-MID Ack. Captured
  response delays are **8.07–9.00 ms**, within the one-second requirement.
- Pod reconnect and adapter SIGKILL preserve the policy and original reporting
  schedule. Identical policy re-delivery does not postpone the next deadline.
- **Three reporting periods remain explicitly unfulfilled.** No AP Metrics
  Response is present. Receipt/application/reporting are separate status fields;
  the last two remain unproven.
- Both recoveries pass: **7.32 s** for the pod connection including its deliberate
  outage, **1.21 s** after SIGKILL. Three fresh authenticated operations, one total
  Config write; independent client traffic survives both management faults.
- All 30 connected-phase samples place the client under the represented BSS.
  The captures retain 113 Ethernet and 11,497 radio packets with matching filter
  totals and zero reported drops.
- Cleanup has no errors, the original controller is restored, and the owned lab
  was separately verified idle. Existing native-controller shutdown SIGABRT
  behavior remains recorded.
- [Unit tests](unit-results.xml): **1,087 passed**, including 25 policy tests and
  one additional onboarding integration test. This covers persistence, cadence,
  re-delivery, malformed/unsupported input, resource bounds, deadlines, source
  loss and explicit boot-change rebasing. An actual VM reboot was not exercised.

Recheck on HOST with Python and tshark:

```bash
python3 scripts/check-native-recovery.py \
  doc/evidence/reporting-policy/native-policy-receipt-01 --minimum-seconds 210
python3 scripts/check-native-policy.py \
  doc/evidence/reporting-policy/native-policy-receipt-01
```

The [independent policy result](native-policy-receipt-01/independent-policy-check.json)
joins actual request bytes, Acks, stored intent and recovery snapshots. The
[recovery result](native-policy-receipt-01/independent-recovery-check.json) also
verifies traffic, fresh onboarding, measured channel reports and no duplicate
Config effects. Neither checker imports the adapter implementation.

`unanswered_controller_requests["0x8003"]` is now empty, but the actual metric
reports are still missing. Three IEEE 1905 neighbor queries remain unanswered;
final disassociation measurements/reports also remain incomplete. Full sustained
and physical-pod acceptance stay false. Follow the
[policy receipt guide](../../protocol/reporting-policy.md) and its learning
sequence before interpreting this result.

Only reviewed synthetic-run artifacts from the collector's allowlist, independent
checks and unit results are retained here. Private policy/operation databases,
secrets, native configuration/full logs, binaries and standards PDFs are excluded.
All changed runtime source hashes match the executed VM files. No physical pod
was connected or changed.
