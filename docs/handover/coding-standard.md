# Coding standard

The standard for the C is normative in [c/QUALITY.md](../../c/QUALITY.md) §2: the SEI
CERT C Coding Standard (2016), its rules required and its recommendations guidance,
with the project's rules on top and the deviations in §5. This page is how the code
follows it in practice, and the conventions for the Python and the scripts.

## C

- **C11**, built with `-Wall -Wextra -Wpedantic -Wshadow -Wformat=2
  -Wstrict-prototypes -Wmissing-prototypes -Wvla -Wnull-dereference` as errors
  (`-DEMOSA_STRICT=ON`) on gcc and clang, 64-bit and 32-bit x86.
- **A module is a `.c` with its `.h`**: the header states what the module does and its
  contract; the `.c` opens with what it mirrors in the reference (`Mirrors
  emosa.wire.cmdu`) and the specification section it implements. Public names start
  with `em_`; everything else is `static`.
- **Errors are values.** A function that can fail returns `bool` or an `em_reason`
  (the reference's reason codes, `common.h`), and its caller checks it. Nothing reports
  failure through `errno` across modules.
- **Allocation** goes through `em_malloc`, `em_calloc`, `em_realloc` and `em_strdup`,
  which end the process when memory runs out (`em_init()` gives cJSON the same
  allocator). No caller handles a failed allocation, and none leaks: LeakSanitizer
  runs on every test.
- **Strings into fixed buffers** go through `em_copy` and `em_format`, which return false
  when the text did not fit; the caller refuses or skips the value. `EM_FORMAT_FIXED`
  (which aborts) is only for text bounded by construction (numbers, MACs, the program's
  own names). A value cut on purpose is only ever shown text, and the call says so with
  a `(void)` cast.
- **Untrusted input** (the LAN's frames, the pod's OVSDB and statistics, the broker, the
  disk, configurations) is parsed only by code with a fuzz target (QUALITY §2's table);
  a new parser gets one.
- **One owner thread** per program: a `poll` loop, no locks, no shared mutable state.
- **The log** is `em_log(level, component, ...)` (`log.h`), the component the
  reference's logger name; never a secret, a passphrase or a key.
- **Files the reference also writes** (status, registry, configurations) are written
  through `canon.c`, so the bytes match.
- **Comments** say what and why, in the specification's terms; a reference to the
  specification is a section (`spec §3.7`), to the reference a module name.

## Python

- Python 3.13 (`.python-version`, `uv.lock` pins every dependency); `ruff` with rules
  `E, F, I, UP, B, SIM` and lines of 100 (`pyproject.toml`); `ruff format`.
- The adapter (`src/emosa`) stands alone: no lab code, only its declared dependencies
  (`tests/test_adapter_boundary.py`). Lab and tooling code is in `lab/src/emosa_lab` and
  `scripts`.
- Tests carry a marker (`unit`, `ovsdb`, `box`, ...); `unit` tests are unprivileged and
  deterministic.

## Shell

- `#!/bin/bash` (or `/usr/bin/env bash`) with `set -euo pipefail`; each entry script does
  one stage and says so in its header, with its usage.
- Check every change with `bash -n` (and `shellcheck` where installed).
- A waiter polls a PID, a sentinel or a log, never `pgrep -f` with a pattern its own
  command line contains.

## Commits

Plain messages: a summary line, then what changed and why. No attribution lines.
