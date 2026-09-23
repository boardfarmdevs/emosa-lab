# Native neighbor measurements and controller receipt

The optional owned Ethernet profile now completes **measured pod observations
→ OpenSync-schema OVSDB → EMOSA → actual IEEE 1905 reply → native controller
interface statistics**. The [guide](../../protocol/native-peer-metrics.md) and
[manual §13.28](../../guides/team-manual.md#1328-deliver-the-measured-peer-report-to-the-controller)
explain the profile, field meanings and reproduction commands.

This is a short reporting/recovery regression. It does not complete the full
15-minute sustained acceptance or qualify an unchanged physical OpenSync pod.

## Successful combined regression

`native-peer-metrics-04` runs for **210.124 active seconds** and passes the
[independent peer audit](native-peer-metrics-04/independent-peer-metric-check.json),
[operational recovery audit](native-peer-metrics-04/independent-recovery-check.json),
[policy receipt audit](native-peer-metrics-04/independent-policy-check.json) and
[telemetry-gap audit](native-peer-metrics-04/independent-telemetry-gap-check.json).

The peer audit checks 255 published observations against 278 common counter
intervals through three connection generations and two worker processes. The
VM bridge isolates the pod and adapter control ports from each other while
preserving their access to the controller. Both sides of the measured veth have
aggregation and VLAN offloads disabled. No adapter-source frame occurs in a
published interval in the independent backhaul capture.

| Native request/reply frames | MID | Reply latency | Controller TX/RX packets | Controller TX/RX errors |
| --- | ---: | ---: | --- | --- |
| 64 → 65 | 48 | 0.361 ms | 9 / 7 | 0 / 0 |
| 98 → 99 | 64 | 6.685 ms | 16 / 16 | 0 / 0 |

Each reply matches a fresh OVSDB-backed measurement interval. Controller
inventories collected afterward expose the same four statistics under the
represented pod interface. The reply's media, capacity, availability, bridge,
unknown PHY and unspecified RSSI fields are independently decoded and checked;
the controller inventory comparison claims only its exposed packet/error fields.
Capacity is 97 Mb/s after framing on the declared 100 Mb/s software service.
Availability is the estimated unused service percentage over the same interval.
Neither value is a physical radio or physical Ethernet PHY measurement.

Two other queries have explicit evidence-based classifications:

- Frame 29 arrives during the new counter baseline after the deliberate
  discovery-observer pause. The first complete interval cannot be read until
  after the query. The checker derives this interval from raw timestamps with
  the unchanged 1 ms allowance; it does not excuse delays once measurements
  exist. The initial coarse query-count audit rejected this case; its
  [finding is retained](native-peer-metrics-04/initial-audit-failure.json).
- Frame 112 arrives after the worker's recorded successful exit. The checker
  uses `worker-stop.json`, rejects ambiguous exit-boundary timing and lists
  post-exit queries separately from active operation.

## Actual faults and restoration

The runner actually disables pod-port isolation and observes metric withdrawal
while control authority and independent client traffic remain healthy. It then
restores isolation and verifies a fresh path baseline without additional
configuration operations. Pausing station telemetry leaves the independent peer
metric source available; pausing neighbor discovery withdraws its binding.

The actual OVSDB disconnect recovers in **7.329 seconds**. Actual adapter SIGKILL
and restart recover in **1.223 seconds**. Three authenticated operations retain
only one total Config-write attempt. All three channel preference queries,
three selections and three measured operating reports/Acks pass the strict
existing channel audit. Policy receipt passes, but three due reporting periods
still have no AP/STA report: receipt is not fulfilled reporting.

All **823 wired probes** pass. Wi-Fi receives **689 of 802** replies, with eight
gaps corresponding to deliberate client disconnects. Both clients pass through
management recovery. All 26 connected controller observations contain the
client under the represented BSS; eight disconnected observations remove it.
Sampled RSS is **56,032–59,564 KiB**, with **23 file descriptors**.

Control and radio captures contain 112 and 11,454 complete records respectively,
with matching capture totals and zero reported kernel drops. The source check
matches [103 runtime inputs](native-peer-metrics-04/source-match.json), including
the trial-time native candidate reference. Queue, isolation and exact original
offload features are restored; cleanup errors are empty and the owned idle
check passes. Native controller and helper agent shutdown still report
SIGABRT/core-dump. Restoration success does not close that lifecycle defect.

## Earlier attempts remain available

| Attempt | What it established | Why it is not the final combined pass |
| --- | --- | --- |
| `native-peer-metrics-01-limited` | Actual peer replies and controller values | No explicit worker-exit receipt to classify a final unanswered query; no injected path fault |
| `native-peer-metrics-02-limited` | Actual path withdrawal, peer replies and recovery | Telemetry pause began before the initial service baseline; it could not prove survival of an already available source |
| `native-peer-metrics-03-limited` | Peer audit passes: 283 published observations, 303 common intervals and two received reports | Two live startup channel requests remained unanswered before fresh radio telemetry; the strict recovery audit fails |

The third attempt also revealed two independent-checker omissions: the raw
capture contained one IPv6 frame from the separately inventoried pod bridge MAC,
and the native inventory envelope included a scalar status member. The checker
now admits that observed local bridge alias, still rejects unclassified sources,
and reads only inventory objects. No packet bounds or response deadlines were
widened. Its earlier failure and exact pre-fix channel source are retained.

The channel coordinator now holds at most four early requests until measurements
arrive, under the original one-second response deadline and control context.
Deterministic tests exercise the failed startup sequence, duplicate deadlines,
context loss, expiration and invalid requests. The fourth native run passes the
unchanged channel-response audit; it need not happen to reproduce that race.
CI also requires the retained third attempt to keep failing for the original
channel-response reason.

## Verification and remaining acceptance

The retained suites contain **1,365 passing unit tests and 60 passing real-OVSDB
tests**. CI independently replays the final native evidence. The earlier
combined-source audit remains byte-for-byte unchanged. The first two runs retain
their exact earlier harness source; the third retains the pre-fix channel source.
No historical evidence manifest entry is rewritten.

Next complete qualified AP/STA reporting and the online final-session publisher,
resolve native shutdown, and repeat the integrated 15-minute acceptance with
all required sources enabled. Multiple peers and actual physical media require
their own qualification. Physical acceptance remains **real EasyMesh messages
→ EMOSA → unchanged physical OpenSync pod → independently observed behavior**.
