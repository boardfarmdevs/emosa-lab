# Native telemetry-gap and recovery regression

`native-telemetry-gap-01` completed **213.42 active seconds**, 29 connected-phase
samples and both recovery faults. It deliberately withheld telemetry for
**6.37 seconds**, including the time needed for independent client probes,
while OVSDB observations and ordinary hwsim traffic continued.

The independent gap, recovery and reporting-policy receipt checks pass:

- Stale client inventory and operating-radio observations became unavailable.
- The control context and worker remained unchanged during the telemetry gap.
- Fresh telemetry restored observations without false departure notifications,
  another discovery/WSC exchange or another configuration operation.
- Pod disconnection recovered in **7.32 seconds**, including the four-second
  intentional outage. Adapter SIGKILL/restart recovered in **1.22 seconds**.
- Three fresh authenticated operations used **one total Config-write attempt**;
  both recovery operations completed as observed no-ops.
- Both clients passed nonce/traffic probes during the telemetry gap and continued
  traffic through management faults. Intentional client outages remain recorded.
- Radio and Ethernet capture counts match their filter/record counts with zero
  reported drops: **11,586** and **114** respectively.

The controller candidate was restored, cleanup errors are empty and a separate
idle check passed. The previously observed native shutdown SIGABRT behavior
remains visible in `result.json`; it is not represented as clean native shutdown.
Executed source hashes include the freshness change and the fault-injection
harness. No physical pod was connected or changed.

```bash
python3 scripts/check-telemetry-gap.py \
  doc/evidence/telemetry-freshness/native-telemetry-gap-01
python3 scripts/check-native-recovery.py \
  doc/evidence/telemetry-freshness/native-telemetry-gap-01 --minimum-seconds 210
python3 scripts/check-native-policy.py \
  doc/evidence/telemetry-freshness/native-telemetry-gap-01
```

The [full unit suite](unit-results.xml) passes **1,137 tests** and the
[real OVSDB suite](ovsdb-results.xml) passes **54 tests**. Added tests reject
stale inventory, changed control authority, extra operations, altered completion
flags, missing trace records and trace loss. Source-level tests ensure that
prepared stale reports cannot be emitted and that actual source expiry still
revokes authority.

See the [step-by-step guide](../../protocol/telemetry-freshness.md) for setup,
fault meanings and interpretation. This focused run complements the original
908-second operational soak. It preserves the controller's requested policy
but still has **three unfulfilled reporting periods**. Required measured reports
and final-session publication remain incomplete; complete sustained and physical
acceptance flags remain false.
