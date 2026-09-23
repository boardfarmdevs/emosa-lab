# Fifteen-minute operational soak with recovery

Run `native-soak-01` passes the **scoped operational recovery checks** against
the patched native controller, EMOSA virtual agent, pod-initiated OpenSync-schema
OVSDB simulator, separate hwsim manager and independent client containers.
It does not yet pass complete sustained-operation acceptance or prove a physical
OpenSync extender. Recheck the reviewed artifacts with:

```bash
python3 scripts/check-native-recovery.py doc/evidence/native-soak/run-01
```

The checker uses standard Python and tshark without importing EMOSA. It rechecks
initial onboarding, then correlates fresh discovery/Early/M1/M2 with each recovery
receipt, native inventory, measured channel reports, process samples and
continuous traffic. See [the result](run-01/independent-check.json) and
[the reproduction guide](../../protocol/sustained-operation.md).

| Observation | Measured result |
| --- | --- |
| Active phase | 908.33 seconds; 135 process/client samples |
| Native client inventory | Client under the virtual agent's BSS in all 135 connected samples; absent in 36 deliberate disconnect inventories |
| Client messages | 39 join notifications, including the fresh-context announcements; 36 leaves; 39 explicit capability-unavailable reports |
| Pod OVSDB interruption | Fresh native onboarding in 7.32 seconds including the deliberate outage; same adapter PID |
| Adapter SIGKILL | New process and fresh native onboarding in 1.22 seconds |
| Configuration effects | Three distinct authenticated WSC operations; one total write attempt; both recovery operations are observed no-ops |
| Channel exchange | Three preference queries; four accepted selections and measured operating-channel reports, all acknowledged |
| Wired traffic | 3,550/3,550 continuous ICMP replies; no gap over 1.5 seconds |
| Wi-Fi traffic | 2,949/3,450 replies; 36 gaps over 1.5 seconds, all confined to recorded intentional client outages; traffic continues during both management faults |
| Resources | RSS 54,784–58,808 KiB; 20 descriptors in every sample across two processes |
| Cleanup | No harness cleanup errors; exact original controller executable restored; owned lab verified idle |

The initial configuration is withheld before independent radio application, as
in the bounded onboarding experiment. The actual controller supplies every WSC
configuration; no semantic submission substitutes for it. Continuous ICMP runs
alongside per-sample interface-bound ping, route, WPA and nonce/HTTP checks.
This is a connectivity and recovery test, not a throughput benchmark. The public
candidate remains SHA-256
`d6db692f97b5b1dd2bb20c7810bbe0a775351d6f5fd8e3c7e21310a230bf5436`.
The source hashes match 84 staged Python/helper files in the implementation used
for this run. The retained unit report records 995 passing unit tests.

Remaining wire gaps are visible in the capture: three unanswered Multi-AP policy
requests and 15 unanswered IEEE 1905 neighbor-link-metric queries. Qualified
AP/STA metrics and final disassociation counters/reasons remain incomplete.
The native controller/helper's pre-existing SIGABRT shutdown behavior is retained
in `result.json`; restoration does not qualify that shutdown behavior. Therefore
`sustained_operation_proven`, `full_profile_qualified` and `physical_pod_proven`
remain false. The final acceptance path remains real EasyMesh messages → EMOSA →
unchanged physical pod → independently observed behavior.

The shorter `native-recovery-01` attempt stopped before fault injection because
LXD failed to update the synthetic HTTP response file. `native-recovery-02`
completed both faults over 150 seconds but exposed a cleanup race: systemd had
already removed successfully exited transient ping units when the harness issued
`stop`. The harness now verifies inactive state and zero PID after SIGINT.
Those original failed attempts remain under their VM labels; neither was turned
into a passing result. The final run uses a new label and completes cleanup.

Collection uses [an explicit allowlist](../../../scripts/collect-native-review.py).
The published material contains reviewed synthetic captures, public receipts,
native inventory, independent observations and provenance. It excludes standards
PDFs, operation journals, credentials, secrets, backups, binaries and full native
logs. Checker-negative tests reject an extra write, reuse of an old M1 hash and
an unexplained wired traffic gap; their scope is recorded in
[tamper-checks.json](tamper-checks.json).
