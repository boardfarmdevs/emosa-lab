# Recovery and ownership

Use `emosa quiesce` to stop new submissions. Existing operations continue to be
observed. Stopping the service never restores a stale configuration snapshot.
The SQLite journal uses FULL synchronous commits and WAL; one process lock
protects each state directory. Independent controllers on other hosts require
external fencing and remain outside this foundation's scope.

After restart, a durably `SUBMITTED` operation becomes `INDETERMINATE` and is
reconciled against a fresh monitor snapshot. Matching Config/State proves the
current target condition, not which writer caused it. A missing secret file
prevents resubmission/equality evaluation but does not stop read-only observation.
Retain the private fingerprint key and secret files along with the journal.

Conflict evidence is durable. This release does not automatically clear it or
retry against a competing writer. Establish actual ownership and document the
handover before using a new qualified scope. No hardware profile is writable in
this delivery; simulator booleans cannot enable one.

Simulation cleanup terminates only the processes created for the run and removes
their disposable database. Run reports, observations and journals are retained.
An interrupted run remains inspectable and is not a passing compatibility result.
For hardware, guarded compensation, real cloud/local writer controls, wired or
wireless management and recovery procedures must be qualified through M0. These
cannot be inferred from the simulation or a live OVSDB connection.
