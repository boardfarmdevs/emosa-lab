# Kernel station-removal observation evidence

`native-final-source-01` is a **158.51-second observation/recovery experiment**,
not another complete 15-minute acceptance claim. It uses the same native
controller candidate and unchanged EMOSA packet worker as the previous run.
The additional owned-AP observer only reads nl80211 notifications.

Results:

- Seven observed station lifetimes, six removal records and no reported netlink
  loss/truncation or collector error.
- All six removals correlate with independently captured client deauthentication
  reason 3 and EasyMesh leave notifications. Kernel delivery delay is 17.2–23.3 ms.
- Raw byte/packet/failure/retry attributes are present; each removed association
  has a distinct kernel association timestamp. Reason is absent from netlink.
- Pod connection recovery: 6.32 seconds, including the deliberate outage.
  Adapter SIGKILL recovery: 1.22 seconds. Three fresh authenticated operations,
  one total Config write; client traffic survives both management faults.
- Cleanup has no errors, the original controller is restored, and the owned
  lab was separately verified idle. Existing native shutdown SIGABRT behavior
  remains recorded.
- [Unit tests](unit-results.xml): **1,046 passed**, including 18 new observer
  framing, field-presence, truncation, sender and read-only-request tests.

The source **does not yet qualify EasyMesh counter semantics**. Linux's attempted
transmit counts, byte accounting, miscellaneous drops and retry accumulation
must not be renamed as EasyMesh's required quantities without further review.
No measured final-statistics CMDU was sent; full sustained and physical-pod
acceptance remain false. See the
[observation guide](../../protocol/station-removal-observations.md) for the exact
limits, source review, pending Data Elements input and learning sequence.

Recheck on HOST with standard-library Python and tshark:

```bash
python3 scripts/check-native-recovery.py \
  doc/evidence/station-removal/native-final-source-01 --minimum-seconds 150
python3 scripts/check-station-removal.py \
  doc/evidence/station-removal/native-final-source-01
```

The [independent observation result](native-final-source-01/independent-observation-check.json)
joins raw kernel integers, association lifetimes, radio reasons and leave frames.
The separate [recovery result](native-final-source-01/independent-recovery-check.json)
checks native onboarding, controller inventory, continuous traffic and fresh
operations. Neither checker imports EMOSA or the observer.

Publication contains only reviewed synthetic-run artifacts from the collector's
explicit allowlist. Private journals, secrets, native configuration/logs,
binaries and standards PDFs are excluded. The original run remains on the VM.
