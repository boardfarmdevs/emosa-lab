# Raw pod forwarding observations through OVSDB

The owned simulator now publishes independently read Linux bridge-port
identities and raw interface counters through the pinned OpenSync OVSDB schema.
EMOSA checks the graph, freshness and counter lifetime before computing an
interval. **Per-neighbor measurements and native metric delivery remain pending.**

The retained `native-forwarding-01` run completed **213.87 active seconds** with
29 connected-phase client/controller samples. The new independent checker finds:

- **367** successful manager publications, **308** received samples and **305**
  checked counter intervals. Both worker processes and all three connection
  generations are represented; reconnect and restart begin new baselines.
- An actual OVSDB loss withdraws the observation. Raw forwarding observations
  remain available during the separate **6.40-second** MQTT telemetry gap.
- The simulated pod's backhaul MAC is `00:16:3e:b2:d2:c4`, distinct from EMOSA's
  `02:00:00:00:30:01` control interface. Native controller Topology Discovery
  frames **1037, 2155 and 3346** on that backhaul advertise controller interface
  `00:16:3e:b8:c4:a7`, separately from its AL MAC.
- The forwarding capture contains **903** ICMP packets in each direction for
  the wired client and **770** in each direction for the Wi-Fi client. These
  include the independent continuous probes and sampled client probes.

All three captures have matching file/filter/captured counts and zero reported
kernel drops: **4,241 forwarding**, **113 controller-facing Ethernet** and
**11,623 radio** packets. Capture completeness does not by itself reconcile
interface counters with per-neighbor traffic. The accumulated interval deltas
are raw counts over the accepted intervals, not over the entire capture lifetime.

The independent recovery, freshness, policy-receipt and unavailable-neighbor
checks pass. Pod reconnect completes in **7.32 seconds**, including the deliberate
four-second interruption; adapter SIGKILL recovery takes **1.22 seconds**.
Three authenticated operations retain **one total Config-write attempt**.
Four native neighbor queries remain explicitly unanswered for lack of qualified
measurements; three due policy-reporting periods remain unfulfilled.

The native process exits and restoration are recorded without changing their
meaning: the controller/helper-agent shutdown SIGABRT remains in the evidence.
The original candidate is restored and a separate idle check passes. All 87
checked executed Python source hashes match the reviewed implementation.

```bash
python3 scripts/check-forwarding-observations.py doc/evidence/forwarding/native-forwarding-01
python3 scripts/check-native-recovery.py \
  doc/evidence/forwarding/native-forwarding-01 --minimum-seconds 210
python3 scripts/check-telemetry-gap.py doc/evidence/forwarding/native-forwarding-01
python3 scripts/check-native-policy.py doc/evidence/forwarding/native-forwarding-01
python3 scripts/check-neighbor-metrics.py --native-directory \
  doc/evidence/forwarding/native-forwarding-01
```

The [unit suite](unit-results.xml) passes **1,228 tests** and the
[real-OVSDB suite](ovsdb-results.xml) passes **55 tests**. New tests cover raced
bridge membership, incomplete/malformed source fields, stale/future/replayed
observations, changed schema/graph, counter decreases, interface lifetime changes,
connection generations, guarded transaction conflicts and explicit withdrawal.

The [learning guide](../../protocol/forwarding-observations.md) explains every
field and how to reproduce the experiment. It keeps runtime topology/media
binding, per-neighbor attribution and capacity/availability qualification open.
The original 15-minute operational result remains separate; full sustained
acceptance, AP/STA reporting, final-session metrics and unchanged physical-pod
acceptance remain incomplete.
