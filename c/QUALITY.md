# The production bar for EMOSA in C

**Reference.** What the C implementation must meet to be production code, and
where it stands. It is step 8.1 of the easymesh-labs
alignment plan (in [easymesh-labs](https://mesh.vcpe.dev/))
(phase 8: the C to production quality, owned afterwards by a team without
access to the labs). The C is production code when every gate in §3 is met.

## 1. The contract

- **What it must do** is fixed by the specification (`spec/README.md`), the
  component design (`spec/design.md`), the schemas (`schemas/`) and the
  conformance vectors (`spec/conformance`). Where they and the code disagree,
  they win.
- **The Python reference is the vectors' oracle.** A change of behaviour starts
  in the specification and the reference; the vectors are regenerated from the
  reference (`lab/src/emosa_lab/conformance.py`); then the C follows until its
  harness reproduces them again.
- **Acceptance** is the design's §13: every vector, the live acceptance in the
  RDK lab with its room suite, and the reference workload.
- **Target.** Linux in the RDK lab's containers (32-bit x86, `bpibroadband`, now and
  long term; meta-cmf-bananapi-vcpe's recipe `emosa`, opt-in), physical BPI or mv3 routers (ARM) later. C11. Dependencies:
  cJSON, OpenSSL `libcrypto` and SQLite 3, all in the RDK-B images; another
  one needs a recorded decision.

## 2. The coding standard

The [SEI CERT C Coding Standard](https://wiki.sei.cmu.edu/confluence/display/c)
(2016 edition): its rules are required, its recommendations are guidance. A
deviation from a rule is recorded in §5 with the rule, the place and the
reason. On top of CERT:

- **Errors are values.** A function that can fail returns `bool` or an
  `em_reason` (the specification's reason codes, `common.h`), and its caller
  checks it. No module reports failure through `errno` to another.
- **Allocation failure ends the process.** Every allocation goes through
  `em_malloc`, `em_calloc`, `em_realloc` or `em_strdup` (`common.c`), and
  `em_init()` installs the same allocator in cJSON; on failure they print a
  message and `abort()`. The agent is supervised (`emosa-agent@POD`) and its
  journal makes a restart safe (design §6), so no caller handles a failed
  allocation, and neither these functions nor a cJSON constructor returns
  NULL for want of memory. This handles the failure in the sense of CERT MEM32-C. It is no
  licence to leak: what is allocated is freed (LeakSanitizer, §3).
- **Untrusted input is parsed only by fuzzed code.** The adapter's untrusted
  inputs and their parsers:

  | Input | From | Parsers |
  | --- | --- | --- |
  | 1905 CMDUs and TLVs | the LAN (any host on it) | `cmdu.c`, `autoconf.c`, `wsc.c` (M1/M2), `control.c`, `reporting.c`, `bhsteer.c`, `early.c` |
  | OVSDB JSON-RPC | the pod | `ovsdb.c` (framing), `ovs.c`, `view.c`, `southbound.c` |
  | MQTT and the pod's `sts.Report` protobuf | the broker | `mqtt.c`, `stats.c` |
  | The agent configuration | the fleet | `jschema.c` against `schemas/agent-config.schema.json` |
  | The state directory | the disk | `journal.c`, `vault.c`, `channel_store.c` (SQLite and files the agent wrote) |
  | What a pod sends the fleet's front port | the pod | `jsonrpc.c` (framing), `fleet.c` (its identity) |
  | The fleet's registry | the disk (also written by another fleet process: `forget`) | `fleet.c` |
  | A lease event, dnsmasq's lease file, iproute2's output | DHCP clients through dnsmasq; the host | `gtp.c` |

- **No truncation goes unnoticed** (ERR33-C). Text into a fixed buffer goes through
  `em_copy` or `em_format`, which return false when it did not fit, and the caller
  refuses or skips the value; only text bounded by construction (numbers, MACs,
  the program's own names, validated values) uses `EM_FORMAT_FIXED`, which aborts,
  and never text from the pod, the LAN or a file. A cut value is allowed only for
  what is shown (messages, the pod's model name), and the call says so with a
  `(void)` cast. Writes of log lines to stderr are not checked: nobody could be
  told that they failed (ERR33-C, EX1).
- **One owner thread** (design §5.1): no locks, no shared mutable state
  between threads.
- **Secrets never reach a log**, a status file or an error message (spec).

## 3. Gates

| Gate | How | State (30 September 2026) |
| --- | --- | --- |
| Warnings | `-Wall -Wextra -Wpedantic -Wshadow -Wformat=2 -Wstrict-prototypes -Wmissing-prototypes -Wvla -Wnull-dereference`, as errors, on gcc and clang (`-DEMOSA_STRICT=ON`); CI `c (gcc)`, `c (clang)` | met on gcc 11 and 13, clang 14 and 18 (CI: gcc 13, clang 18) |
| Conformance and units | `ctest`: `emosa-vectors` (every vector, 984 checks), `emosa-units` (81 checks) and the fuzz targets over their seed corpora | met (3 October 2026, with the fleet, the GTP and the secret store's interface (8.3), and the features' rejection paths (8.4): every refusal of the control, policy and query parsers and of the steering plan, as the reference gives it; a steering window's faults: a lost reply to its open, its kick or its close, and a restart before it was sent) |
| Sanitizers | the same tests under AddressSanitizer, LeakSanitizer and UndefinedBehaviorSanitizer (`-DEMOSA_SANITIZE=ON`), no suppression but the harness's simulated crash (`tests/lsan.supp`); CI `c (sanitizers)` | met. Found undefined behaviour: `qsort` with a null array (now `em_sort`) |
| Static analysis | the clang static analyzer, `scan-build --status-bugs`, with no findings; CI `c-analyzer` | met. Its findings (unchecked allocations, dead stores) are fixed |
| CERT C review | every rule checked, with a tool where one exists (clang-tidy `cert-*`, cppcheck's CERT addon) and by review where none does; deviations in §5 | the tool's part done: clang-tidy `cert-*` (18) from 247 findings to 6, the two deviations in §5 (4 `sscanf`, 2 `_GNU_SOURCE`, marked `NOLINT` at their lines); CI job `c-cert` fails on any other. Every string written into a buffer was reviewed by its source (`em_copy`, `em_format`, `EM_FORMAT_FIXED`); the defects it found, each fixed: three whole-file reads that trusted `ftell` (a heap overflow on failure); status and state files left truncated by a full disk; a telemetry topic or broker cut to fit and then accepted where the reference refuses it; a survey row that crashed the agent (NULL + 1); a probe report whose interface name could make `printf` read past it; a configured uplink SSID cut at 32 bytes; a secret removal that followed a path out of the vault; a list builder that pointed past its buffer; NULL passed to `%s` from records (four places); a state directory cut into another. The review of the rules no tool checks (memory, integers, floating point, concurrency): done for the features' modules (plan 8.4, 3 October: `scope_uplink.c`, `bhsteer.c`, `scope_telemetry.c`, `mqtt.c`, `stats.c`, `reporting.c`, `scope_steering.c`, `control.c`, `scope_watch.c`), each allocation, string write, copy and index from a TLV, a protobuf or a file, float-to-integer conversion, division, shift and narrowing into a TLV length. Its defects, each fixed: a Channel Scan Request naming more than 256 results overflowed a stack array (ARR30-C; any host on the LAN could send one; vector `channel-scan-many`); a pod's rate that was not finite or out of range converted to an integer (FLP34-C), and an SNR beyond 2^31 overflowed the RCPI arithmetic (INT32-C), both from the pod's statistics (spec 3.6 now: such a rate is absent; the reference's answer failed on them too); an MQTT topic copied with `strndup`, outside the allocator, and a topic with a NUL accepted cut short. Two differences from the reference, fixed: steering requests naming more than 32 stations or targets were refused, and scan results capped at 256. Not yet: the other modules (the agent's core, the basic adapter's), and the SQLite and OpenSSL calls |
| Fuzzing | a libFuzzer target per parser in §2 (`c/fuzz`, `-DEMOSA_FUZZ=ON` with clang), run in CI for a bounded time (CI `c-fuzz`: 60 s each) under ASan and UBSan, seeded from the vectors (`c/fuzz/seeds.py`) and recorded captures; no crash, no sanitizer report | partly: 1905 frames through reassembly and Response parsing (`cmdu`), the pod's statistics (`stats`), the pod's OVSDB rows into the view and the uplink (`rows`), a controller's WSC M2 (`wsc`). Found one undefined behaviour (`memcpy` from a report without an SSID, fixed); then 4 minutes each without a finding (1.9 million, 1.6 million, 317 000 and 959 000 inputs). With the basic adapter (plan 8.3, 3 October): a pod's byte stream at the fleet's front port through the framing, echoes, replies and its identity, and the registry file (`fleet`); a lease event, the lease file and iproute2's output through the GTP's commands (`gtp`): 2 minutes each without a finding (709 000 and 21 000 inputs). With the features (plan 8.4, 3 October): a controller's requests after onboarding through the control plane, the reporting policy, AP Metrics Query and Backhaul Steering as the agent dispatches them, with their ticks (`control`), and the broker's byte stream through the MQTT framing in each phase, whole and split (`mqtt`, the parser now apart from the socket code): 2 minutes each without a finding (93 000 and 1.9 million inputs). Not yet: the OVSDB framing (inside its socket code) and the configuration |
| Coverage | line coverage of the library by the vectors, the units and the lab in a box: 85 %, and every parser's rejection paths | met for the library and the features (3 October 2026, plan 8.4): 88.0 % of 9045 lines with the lab in a box (71.8 % by the vectors and units alone). The features' modules: `scope_steering.c` 91.0 %, `reporting.c` 91.0 %, `control.c` 90.3 %, `bhsteer.c` 95.2 %, `mqtt.c` 88.8 %, `scope_watch.c` 87.4 %, `scope_telemetry.c` 86.3 %, `scope_uplink.c` 86.3 %, `stats.c` 86.0 %; the basic adapter's: `fleet.c` 95.5 %, `jsonrpc.c` 96.1 %, `vault.c` 92.0 %, `gtp.c` 86.5 %, `proc.c` 86.3 %. The AP scope's multi-BSS path is covered now that the box's registrar answers an M2 set of two BSSes (`scope_ap.c` 60.5 % to 82.1 %). Not yet: the agent core's modules below 85 %, `channel_store.c` 63.6 %, `ethernet.c` 77.8 %, `cmdu.c` 79.1 %, `jschema.c` 81.5 %, `scope_ap.c` 82.1 %, `common.c` 83.8 %, and so every parser's rejection paths in `cmdu.c` and `jschema.c` |
| Lab in a box (plan 8.2) | the real binary in a network namespace, with a fake pod (an OVSDB server from recorded rows that answers writes), a scripted controller and a broker; a scenario for every behaviour the rooms rely on and every suite finding | met (3 October 2026): 27 scenarios of the agent, 6 of the fleet and the GTP (plan 8.3) and 8 of the features' faults and refusals (plan 8.4: an M2 set of two BSSes, the uplink held, a moved upstream kept, the broker gone and back, steering windows refused, expired and left open) ([spec/box-scenarios.md](../spec/box-scenarios.md)), every one passed by both implementations, in CI (`box (python)`, `box (c)`): either implementation's real binary in a private user and network namespace, the recorded pod in a real ovsdb-server dialing it, the controller's end of a veth with hostap's registrar, and mosquitto (`lab/src/emosa_lab/box.py`, `tests/test_box.py`). Its findings, fixed in the specification, the reference, the vectors and the C: the C admitted controllers the reference refuses (spec §2.5); a pod whose OpenSync started again was taken for another manager's change and never configured again (spec §5); a refused Backhaul Steering without option 1 lacked its Error Code TLV; the refresh cadence counted from a refresh's end while the report lease counts from its start, so one slow refresh ended the session just after provisioning (design §4.3); a `forget` run while the fleet served was undone at the pod's next handover, the serving fleet keeping its registry in memory (spec §4: the registry file is the state, read for each pod). Seen there: the two record different counters for the same replies (the reference `policy_receipt_ack_sent`, the C `topology_response_sent`) |
| Packaging (plan 8.5) | the programs built as a distribution's package and in the gateway image: logging through RDK's logger, the version, units, the bill of materials, the Yocto recipe opt-in with the default image unchanged | met (3 October 2026): `log.c` with an rdk-logger backend (`-DEMOSA_RDK_LOGGER`, tested against a stub of its interface, `emosa-log-rdk`); `--version` with the source revision; the package's layout (`-DEMOSA_INSTALL_DATA`: units, helper, data, `/etc/default/emosa`) and its bill of materials (`packaging/sbom.py`, SPDX 2.3), both checked in CI (`c-package`); the whole bar on 32-bit x86, the images' target (`c-i686`). meta-cmf-bananapi-vcpe's recipe `emosa` built the controller image with `EMOSA_ADAPTER = "1"` (rev140, 3 October): the image differs from the default only by the package `emosa` (520 KiB), the programs run in its root file system and log into `/rdklogs/logs/` (`EMOSA_<pod>.txt`, `EMOSAFleetLog.txt`), its bill of materials names the image's cJSON 1.7.15, OpenSSL 3.0.5 and SQLite 3.31.1; without the setting the image's packages are the default's. Open: EMOSA's own license (`LICENSE = "CLOSED"` in the recipe until its owner grants one) |
| Lab acceptance | design §13 | met by the C of 29 September (the reference workload, the RDK room suite with the pods on C and mixed); and on 3 October by the whole adapter in C, its fleet, GTP and agents with no Python (plan 8.3): the OpenSync lab's reference workload (run `c-adapter-1003`, every check passed) and, in the RDK lab with the pods on Wi-Fi backhaul and five BSSes each, readiness, five rooms, `backhaul-wired-parent` and `fifty-client-counter-roam`; and on 3 October after plan 8.4, the features at the bar: the OpenSync lab's reference workload (run `c-features-1003`, every check passed) and the RDK lab's full room suite with the adapter in C (the catalog's 27 rooms, one of them a timing edge on no pod that passed in 4 of 4 runs after, the four geometry rooms, the RF stages, the switch through every world; [the record](../docs/records/evidence/rdk-lab/README.md)) |

## 4. Running the gates

```sh
cmake -S c -B c/build -DEMOSA_STRICT=ON && cmake --build c/build -j && ctest --test-dir c/build
cmake -S c -B c/build-san -DEMOSA_STRICT=ON -DEMOSA_SANITIZE=ON -DCMAKE_BUILD_TYPE=Debug \
    && cmake --build c/build-san -j && ctest --test-dir c/build-san --output-on-failure
scan-build cmake -S c -B c/build-scan && scan-build --status-bugs cmake --build c/build-scan -j
```

Fuzzing (clang with its fuzzer runtime, e.g. Ubuntu's `libclang-rt-18-dev`):

```sh
cmake -S c -B c/build-fuzz -DCMAKE_C_COMPILER=clang -DEMOSA_FUZZ=ON -DEMOSA_SANITIZE=ON \
    && cmake --build c/build-fuzz -j --target emosa-fuzz-cmdu
mkdir -p /tmp/cmdu && cp c/fuzz/corpus/cmdu/* /tmp/cmdu/ && c/build-fuzz/emosa-fuzz-cmdu -max_total_time=600 /tmp/cmdu
```

Coverage: build with `-DCMAKE_C_FLAGS="--coverage -O0"`, run the tests, then
`gcov` over `CMakeFiles/emosa.dir/src/*.gcno`.

## 5. Deviations

| Rule | Where | Why |
| --- | --- | --- |
| ERR34-C (`sscanf` for integers) | `canon.c`, `jschema.c`, `ovsdb.c` | every conversion has a field width of at most five digits, so it cannot overflow an `int`, and the values are range-checked afterwards |
| DCL37-C (reserved identifier) | `_GNU_SOURCE` in `mqtt.c` and `ovsdb.c` | the C library's feature-test macro, which the program must define to get the POSIX and GNU interfaces it uses; it declares nothing of the program's own |
