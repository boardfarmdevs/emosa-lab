# The production bar for EMOSA in C

**Reference.** What the C implementation must meet to be production code, and
where it stands. It is step 8.1 of the easymesh-labs
[alignment plan](https://github.com/boardfarmdevs/easymesh-labs/blob/main/docs/alignment-plan.md)
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
- **Target.** Linux in the RDK lab's containers (x86, `bpibroadband`, now and
  long term), physical BPI or mv3 routers (ARM) later. C11. Dependencies:
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
- **Untrusted input is parsed only by fuzzed code.** The agent's untrusted
  inputs and their parsers:

  | Input | From | Parsers |
  | --- | --- | --- |
  | 1905 CMDUs and TLVs | the LAN (any host on it) | `cmdu.c`, `autoconf.c`, `wsc.c` (M1/M2), `control.c`, `reporting.c`, `bhsteer.c`, `early.c` |
  | OVSDB JSON-RPC | the pod | `ovsdb.c` (framing), `ovs.c`, `view.c`, `southbound.c` |
  | MQTT and the pod's `sts.Report` protobuf | the broker | `mqtt.c`, `stats.c` |
  | The agent configuration | the fleet | `jschema.c` against `schemas/agent-config.schema.json` |
  | The state directory | the disk | `journal.c`, `vault.c`, `channel_store.c` (SQLite and files the agent wrote) |

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
| Conformance and units | `ctest`: `emosa-vectors` (every vector, 677 checks), `emosa-units` (39 checks) and the fuzz targets over their seed corpora | met |
| Sanitizers | the same tests under AddressSanitizer, LeakSanitizer and UndefinedBehaviorSanitizer (`-DEMOSA_SANITIZE=ON`), no suppression but the harness's simulated crash (`tests/lsan.supp`); CI `c (sanitizers)` | met. Found undefined behaviour: `qsort` with a null array (now `em_sort`) |
| Static analysis | the clang static analyzer, `scan-build --status-bugs`, with no findings; CI `c-analyzer` | met. Its findings (unchecked allocations, dead stores) are fixed |
| CERT C review | every rule checked, with a tool where one exists (clang-tidy `cert-*`, cppcheck's CERT addon) and by review where none does; deviations in §5 | the tool's part done: clang-tidy `cert-*` (18) from 247 findings to 6, the two deviations in §5 (4 `sscanf`, 2 `_GNU_SOURCE`, marked `NOLINT` at their lines); CI job `c-cert` fails on any other. Every string written into a buffer was reviewed by its source (`em_copy`, `em_format`, `EM_FORMAT_FIXED`); the defects it found, each fixed: three whole-file reads that trusted `ftell` (a heap overflow on failure); status and state files left truncated by a full disk; a telemetry topic or broker cut to fit and then accepted where the reference refuses it; a survey row that crashed the agent (NULL + 1); a probe report whose interface name could make `printf` read past it; a configured uplink SSID cut at 32 bytes; a secret removal that followed a path out of the vault; a list builder that pointed past its buffer; NULL passed to `%s` from records (four places); a state directory cut into another. Not yet: the review of the rules no tool checks (memory, integers, concurrency, the SQLite and OpenSSL calls) |
| Fuzzing | a libFuzzer target per parser in §2 (`c/fuzz`, `-DEMOSA_FUZZ=ON` with clang), run in CI for a bounded time (CI `c-fuzz`: 60 s each) under ASan and UBSan, seeded from the vectors (`c/fuzz/seeds.py`) and recorded captures; no crash, no sanitizer report | partly: 1905 frames through reassembly and Response parsing (`cmdu`), the pod's statistics (`stats`), the pod's OVSDB rows into the view and the uplink (`rows`), a controller's WSC M2 (`wsc`). Found one undefined behaviour (`memcpy` from a report without an SSID, fixed); then 4 minutes each without a finding (1.9 million, 1.6 million, 317 000 and 959 000 inputs). Not yet: the OVSDB and MQTT framing (inside their socket code), the configuration, the control and reporting handlers |
| Coverage | line coverage of the library by the vectors, the units and the lab in a box: 85 %, and every parser's rejection paths | 66 % of 8093 lines by the vectors and units. `scope_ap.c`, `scope_uplink.c`, `mqtt.c`, `ethernet.c` and `channel_store.c` are 0 % and `ovsdb.c` 7 %: they run only live, in the labs, until the lab in a box |
| Lab in a box (plan 8.2) | the real binary in a network namespace, with a fake pod (an OVSDB server from recorded rows that answers writes), a scripted controller and a broker; a scenario for every behaviour the rooms rely on and every suite finding | started (`lab/src/emosa_lab/box.py`, `tests/test_box.py`, CI job `ovsdb`): either implementation's real binary in a private user and network namespace, the recorded pod in a real ovsdb-server dialing it, the controller's end of a veth. Scenarios, both implementations pass each: the agent takes the pod and searches (`boot`); an EasyMesh 6.1 controller onboards the pod, with a real M2 from hostap's registrar for the agent's own M1, the credentials applied by the pod's managers and observed applied (`onboard`); a controller without Controller Capability is refused (`refuse`). Its first finding: the C admitted controllers the reference refuses (fixed, specified in spec §2.5 and vectored). The controller's requests after onboarding (`answers`, 1 October): topology, AP capability, channel preference, client capability, backhaul STA capability, unassociated STA metrics and policy configuration each get their reply with the request's MID; the Link Metric and AP Metrics Responses are withheld, their reason recorded, while the box has no telemetry (spec §3.8); both implementations alike. Seen there: the two record different counters for the same replies (the reference `policy_receipt_ack_sent`, the C `topology_response_sent`). Next: steering, telemetry with a broker, restarts, the suite findings |
| Lab acceptance | design §13 | met by the C of 29 September (the reference workload, the RDK room suite with the pods on C and mixed) |

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
