# Test guide

Every test that decides whether EMOSA is right runs without the labs: on a Linux host
or in CI (`.github/workflows/checks.yml`, workflow `component-checks`). The labs add
acceptance on real controllers and pods; their results are recorded in the repository
(`docs/records/evidence`), so a team without them still has the evidence.

## The tiers

| Tier | What it checks | Command | CI job |
| --- | --- | --- | --- |
| Lint | Python style and correctness rules (ruff, Python 3.13) | `uv run ruff check . && uv run ruff format --check .` | `unit` |
| Python units | the reference's modules, the lab tooling, the handover's maps (`tests/`, marker `unit`) | `uv run pytest -m unit` | `unit` |
| Conformance (reference) | the reference reproduces every vector in `spec/conformance` | in `pytest -m unit` (`tests/test_conformance.py`) | `unit` |
| OVSDB | the reference against a real disposable `ovsdb-server` | `bash scripts/build-ovsdb.sh && uv run pytest -m ovsdb` | `ovsdb` |
| C build and units | the C under the bar's warnings as errors, gcc and clang; the vectors (`emosa-vectors`), units (`emosa-units`), the RDK logger backend (`emosa-log-rdk`), the fuzz targets over their seeds | `cmake -S c -B c/build -DEMOSA_STRICT=ON && cmake --build c/build -j && ctest --test-dir c/build` | `c (gcc)`, `c (clang)` |
| C sanitizers | the same under ASan, LSan and UBSan | add `-DEMOSA_SANITIZE=ON -DCMAKE_BUILD_TYPE=Debug` | `c (sanitizers)` |
| 32-bit x86 | the same on the gateway images' target | `-DCMAKE_C_FLAGS=-m32` with i386 libraries (the job's steps) | `c-i686` |
| Static analysis | the clang analyzer, no findings | `scan-build cmake -S c -B b && scan-build --status-bugs cmake --build b` | `c-analyzer` |
| CERT C | clang-tidy's `cert-*` checks, no findings but QUALITY §5's deviations | `run-clang-tidy -p b -checks='-*,cert-*' -warnings-as-errors='cert-*' "$PWD/c/src/.*"` | `c-cert` |
| Fuzzing | each parser of untrusted input under libFuzzer, ASan and UBSan, 60 s each | `cmake -S c -B b -DCMAKE_C_COMPILER=clang -DEMOSA_FUZZ=ON -DEMOSA_SANITIZE=ON`, then each `emosa-fuzz-TARGET` on a copy of `c/fuzz/corpus/TARGET` | `c-fuzz` |
| Package | the distribution's layout, units and bill of materials | `-DEMOSA_INSTALL_DATA=ON`, `cmake --install` into a scratch root | `c-package` |
| Lab in a box | either implementation's real binaries in a private user and network namespace with a real `ovsdb-server` serving recorded pod rows, a scripted controller with hostap's registrar, mosquitto; every scenario of `spec/box-scenarios.md` | `EMOSA_BOX_AGENTS=c uv run pytest -m box` (or `python`); on an older host `scripts/run-box-in-container.sh` | `box (python)`, `box (c)` |
| Recorded references | captures of real controllers and pods checked against the reference's decoders | `python3 scripts/check-*.py` (the job's steps) | `easymesh-reference`, `wsc-reference` |
| Coverage | line coverage of the C library by the vectors, units and box (target 85 %) | build with `-DCMAKE_C_FLAGS="--coverage -O0"`, run ctest and the box with `EMOSA_C_AGENT` set to that build, then `gcov` over `CMakeFiles/emosa.dir/src/*.gcno` | not in CI (QUALITY §3 records it) |

The box needs unprivileged user namespaces (on Ubuntu 24.04:
`sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`), `ip_gre`, and the binaries
`scripts/build-ovsdb.sh` and `scripts/build-wsc-registrar.py` build; the large evidence
files some tests read come from `python3 scripts/fetch-evidence.py`.

## Changing behaviour

1. **The specification first** (`spec/README.md`, `spec/design.md`), then the reference
   (`src/emosa`) and its tests.
2. **The vectors from the reference**: the generator is
   `lab/src/emosa_lab/conformance.py`, `uv run python -m emosa_lab.conformance generate`
   rewrites `spec/conformance`; `pytest tests/test_conformance.py` fails while they are
   out of date.
3. **The C follows** until `emosa-vectors` reproduces them; its harness is
   `c/tests/vectors.c` (one function per vector set).
4. **A behaviour the rooms rely on, or a finding, gets a box scenario**
   (`lab/src/emosa_lab/box.py`, `box_adapter.py`, `box_features.py`, listed in
   `tests/test_box.py` and `spec/box-scenarios.md`), passed by both implementations.
5. **The traceability map** (`docs/handover/traceability.json`) names the new tests
   under the sections they check; `scripts/handover-traceability.py` regenerates
   [traceability.md](traceability.md), and `tests/test_handover.py` fails until both
   agree with the repository.
6. **A new parser of untrusted input gets a fuzz target** (`c/fuzz`, seeded by
   `c/fuzz/seeds.py`) and is listed in QUALITY §2.

## What the labs add

| Lab | Where | What it proves | Record |
| --- | --- | --- | --- |
| The OpenSync lab | an LXD VM with an RDK-free prplMesh controller and three OpenSync pods (`deploy/opensync-lab`) | the 900 s reference workload's faults: recovery, no unresolved operation, bounded memory | [opensync-lab-proof](../records/evidence/opensync-lab-proof/README.md) |
| The RDK lab | meta-cmf-bananapi-vcpe's RDK-B EasyMesh lab with the EMOSA option (`deploy/rdk-lab`) | onboarding by RDK's controller, the room suite with the pods as APs, EMOSA in the gateway (`gateway.sh`) | [rdk-lab](../records/evidence/rdk-lab/README.md) |
