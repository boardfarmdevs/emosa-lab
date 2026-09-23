# Native lifetime fix and 15-minute recovery workload

The optional Linux BPL candidate passes a **901.473-second active run** with
automatic recovery, measured neighbor replies and clean native main-process
shutdown. This resolves the selected candidate's recorded shutdown defect.
**Complete sustained acceptance remains pending** because qualified AP/STA and
final-session reporting are absent. No physical OpenSync pod was contacted.

Use the [step-by-step guide](../../protocol/native-lifecycle.md),
[manual §13.30](../../guides/team-manual.md#1330-prove-clean-native-shutdown-after-the-full-workload)
and learning-path step 20 to reproduce it. EMOSA remains the Python
EasyMesh-to-OpenSync adapter; this optional native patch affects the evaluation
controller and its colocated helper's selected C++ BPL library.

## What changed and why

Earlier native runs aborted during exit after otherwise useful operation.
The retained [controller](diagnosis/controller-backtrace-resolved.txt) and
[helper](diagnosis/agent-backtrace-resolved.txt) backtraces point through
`amxb_free`, `AmbiorixImpl::~AmbiorixImpl` and the BPL shared-library global
destructor. The old BPL shared owner survived the executable's Ambiorix runtime
guarantee, so model destruction attempted to free an already freed connection.
The patch constructs the owner on first application use, which reverses that
bad destruction order while preserving normal model cleanup.

The backtraces come from historical owned-container Apport records. GDB warns
about XSAVE register size and missing matching `libthread_db`; the helper also
has a historical HAL-library address mismatch. Those limitations are retained.
These traces are a diagnostic lead, not a fully reconstructed original runtime.
No core dump, process memory, private configuration or complete crash report is
published. The subsequent actual-library probe and live process observations
provide fresh verification of the fix.

An undefined-symbol scan of 14 collected native ELF inputs found no external
consumer of the removed internal `amb_ptr` symbol. The public setter retains its
interface. Other platform builds and arbitrary external consumers are outside
this selected Linux/no-WHM profile.

## Build comparison and preserved failures

[Build 04 provenance](build-04/candidate.json) records the three pinned input
archives, header commits, tools, patches, source hashes and recipe hashes. The
[independent provenance check](build-04/independent-provenance-check.json)
matches the executed recipes and seven patched source files. The native counter
regression still passes all 12 checks.

| Build | Outcome |
| --- | --- |
| `build-01` | Missing generated `mapf/common/config.h`; recipe/log retained |
| `build-02` | BPL built as static library; expected shared output absent |
| `build-03` | Shared BPL link lacked its TLVF dependency |
| `build-04` | Shared library and controller built; both native regressions pass |

Failed builds were never installed. Their exact intermediate recipes and logs
are retained; later builds used new directories.

The [C++ lifetime probe](build-04/lifetime-regression.json) loads each real BPL
library, with a derived dummy model and runtime object:

| Library/case | Actual event order |
| --- | --- |
| Baseline implicit exit | Main returns → runtime destroyed → model destroyed |
| Candidate implicit exit | Main returns → model destroyed → runtime destroyed |
| Both, explicit model clear | Model destroyed → main returns → runtime destroyed |

The probe verifies destruction order, not a real bus connection. The native run
below tests actual connected processes. Selected SHA-256 digests are:

| Input | SHA-256 |
| --- | --- |
| Baseline BPL | `2e4d0b2ef7dc68e11ad8e6686418555100768634f7462a24f6bb454c77b42cf6` |
| Candidate BPL | `7bc9b040738b30759c73da2a9b3907328e1c468141a332a55e56fd71adf68a0d` |
| Candidate controller | `dd5784852f384069d40274a322b6a7530931bb8b3760d40085a3e02f7db2d55e` |

## Restoration fault before the live run

The actual [post-install fault](restoration-fault/result.json) exits nonzero as
intended after installing the candidate. Its result records `status: failed`,
zero operations, the caught `RuntimeError`, and exact baseline restoration of
both controller and BPL. The original BPL hash was also read from the container
before starting the subsequent run. A [later independent observation](restoration-fault/later-baseline-observation.json)
also checks the restored bytes against this trial’s original reference; its
timestamp is after the soak and does not stand in for a pre-soak observation.
The wrapper retains backups and refuses
reuse while a stop/restoration error remains unresolved.

## Native run: `native-lifecycle-soak-01`

The five independent audits pass:
[recovery](native-lifecycle-soak-01/independent-recovery-check.json),
[lifecycle](native-lifecycle-soak-01/independent-lifecycle-check.json),
[peer metrics](native-lifecycle-soak-01/independent-peer-check.json),
[AP withholding](native-lifecycle-soak-01/independent-ap-withholding-check.json)
and [telemetry gap](native-lifecycle-soak-01/independent-telemetry-check.json).

| Observation | Result |
| --- | --- |
| Active duration | 901.472981619 seconds; 130 connected native inventory samples |
| Actual OVSDB interruption | Automatic fresh authenticated recovery in 7.332 seconds |
| Actual adapter SIGKILL/restart | Automatic fresh authenticated recovery in 1.217 seconds |
| Config effects | Three operations, one total Config-write attempt; zero extra writes on recovery |
| Independent wired traffic | 3,525/3,525 replies; no traffic gaps |
| Independent Wi-Fi traffic | 2,940/3,426 replies; 35 gaps, all within deliberate client outages |
| Native channel procedures | Three preference queries, three accepted selections, three acknowledged measured operating reports |
| Adapter resources | RSS 56,108–60,660 KiB; 23–24 descriptors |
| Peer publisher | 1,194 checked publications; 1,222 counter windows; three control generations and two workers |
| Native peer replies | All 15 live queries answered in 0.404–19.029 ms; matching later native controller interface statistics |
| Missing AP reporting | Fifteen due periods explicitly recorded as unavailable; no fabricated AP Metrics Response |
| Cleanup | No errors; original queue, offload/isolation, binaries, library and references restored |

The peer audit checks every reply against fresh measured source values and its
original one-second deadline. Queries arriving during an input outage may wait
within that deadline and are checked normally if fresh input returns. The old
unanswered-query failure remains rejected for its original reason. The auditor
also caches the invariant inventory timing input to avoid repeatedly parsing
the full 15-minute sample file; acceptance criteria are unchanged.

The initial recovery audit rejected this candidate because its historical guard
required exactly one extra patch. The [retained failure](native-lifecycle-soak-01/initial-audit-failure.json)
records the correction: admit only the exact known one-patch or two-patch recipe,
with explicit library/restoration checks. The live run was not restarted and no
wire, timing, resource or traffic limit was relaxed.

Both start/stop observations show controller PID 15447 and helper PID 15454
mapping the candidate BPL bytes. They retain SIGTERM, a 20-second stop timeout,
`Restart=no` and an empty `SuccessExitStatus`. Both main processes exit with
`Result=success`, `ExecMainStatus=0`; transport, bus and hostap services do too.
No signal is reclassified as success and no destructor is skipped.

The [post-restoration observation](native-lifecycle-soak-01/post-restoration-observation.json)
independently reads the actual baseline controller, helper, BPL and reference
hashes and checks the lab is idle. The original restored baseline still has its
historical lifetime defect; select the optional candidate for new lifecycle
experiments.

The [source audit](native-lifecycle-soak-01/independent-source-check.json)
resolves all **109 runtime inputs**: 108 match the current repository/reference,
and the exact older VM `setup.py` is retained under `executed-source/`. Its only
source difference is the initial package list's later `ethtool` addition; the
already provisioned VM has the required tool. Setup is imported for constants,
not rerun during this experiment. Captures and logger totals match with zero
reported kernel drops.

## Limits and next work

The new audit tests reject hidden abort-success settings, changed PIDs, wrong
mapped/restored libraries, nonzero exit status, shortened duration and unknown
candidate recipes. Retained suites contain **1,419 passing unit tests and 60 passing real-OVSDB
tests**. The XML records the exact executions.
CI rechecks this evidence and preserves previous negative cases.

Next qualify the AP/STA measurement definitions and conversions, connect their
publisher, verify native controller receipt, and complete final disassociation
statistics. Then repeat the complete integrated acceptance with every required
report enabled. The missing Wi-Fi Data Elements package and physical-pod
connection/profile remain separate external inputs. The eventual acceptance
path remains **real EasyMesh messages → EMOSA → unchanged physical OpenSync pod
→ independently observed behavior**.
