# Authenticated fleet and clean runtime evidence

These are synthetic OVSDB/service observations. They do **not** prove real
EasyMesh controller onboarding, physical-pod compatibility, radio behavior or
production capacity. Follow the [learning sequence](../../guides/learning-path.md),
[secure-fleet guide](../../guides/secure-fleet.md) and
[clean reproduction workflow](../../../deploy/reliability/README.md) to repeat them.

All final workloads passed, including twelve-cycle runs on HOST and in the
fresh installed-image container as UID/GID 1001. Local suites: **674 unit and 46
OVSDB tests passed**; the container independently passed four TLS transport tests.

| Environment | Pods | Cycles | Elapsed s | p95 operation s | Peak adapter RSS MiB |
| --- | --- | --- | --- | --- | --- |
| host | 4 | 1 | 11.6 | 0.172 | 52.5 |
| host | 8 | 1 | 14.3 | 0.607 | 67.5 |
| host | 16 | 1 | 19.4 | 1.353 | 97.0 |
| host | 32 | 1 | 29.4 | 1.784 | 152.0 |
| host | 4 | 12 | 156.4 | 0.164 | 53.5 |
| lxd | 4 | 1 | 14.2 | 0.307 | 59.3 |
| lxd | 8 | 1 | 17.1 | 0.405 | 74.1 |
| lxd | 16 | 1 | 24.1 | 0.809 | 103.0 |
| lxd | 32 | 1 | 38.5 | 3.759 | 150.3 |
| lxd | 4 | 12 | 159.6 | 0.211 | 61.8 |

Host and VM share hardware and the final workloads overlapped. These are scoped
observations, not controlled comparative performance measurements. The full
reports are linked by the summary; each has its own exact duration and samples.

The [summary](summary.json) records tested source hashes, suite counts, exact
workloads, resource/latency observations and runtime provenance. Each fleet report
contains a measured duration, per-cycle checks, sampled adapter resources and
the actual child-service lifecycle. Resource samples exclude the separate pod
database/manager processes. Percentiles describe this mixed lab workload and
polling interval, not a production SLO.

| Evidence | Meaning |
| --- | --- |
| [Unit suite](unit-results.xml) / [OVSDB suite](ovsdb-results.xml) | Current regression results; long generated case names are compacted with their SHA-256, preserving counts/outcomes |
| [Host recovery run](host-soak.json) | Twelve four-pod fault cycles with five-second intervals; final observer-lifecycle correction included |
| [Initial observer failure](initial-observer-failure.json) | Adapter had recovered; the idle test observer first encountered its stale old monitor during a read |
| [Longer-run observer backoff failure](soak-observer-backoff-failure.json) | Three cycles completed; forcing reconnect on an idle test observer retained backoff that exceeded one read budget |
| [Runtime image](runtime-image.json) | Private local LXD image, published before experiments, with retained export SHA-256 and byte size |
| [Debian packages](packages.tsv) / [Python packages](python-packages.txt) | Actual installed runtime tuple; apt dependencies are observed versions, not a claim of reproducible future apt resolution |
| [Bundle hashes](SHA256SUMS) / [OVS tool hashes](ovs-sha256.txt) | Wheel, locked dependency input, TLS test source, build script and compiled clean-runtime tools |
| [Mermaid validation](mermaid.json) | Repository diagrams rendered with the recorded renderer version |

A later fresh-container run exposed another fixture readiness race:
[the database's administrative socket was not yet accepting](lxd-startup-failure.json)
when its database socket appeared. `SimDatabase.start()` now waits until both
endpoints accept connections, including after database restart.

Review also found that the original Python TLS-negative client could reject
its synthetic server CA under strict X.509 verification before reaching the
intended peer-pin check. Test certificates now have the required key-usage and
key-identifier extensions; server-certificate verification errors fail the test,
and an explicit `pin_rejected == 1` assertion proves the pin path was reached.
Earlier TLS-negative verdicts are preliminary; use the final retained tests.

The idle fixture sessions now restart after the continuously running adapter's
fresh generation has been independently observed. No adapter deadline or success
condition was relaxed to fix those failures. Database loss, peer progress and
post-restart operation recovery remain real service assertions.

The first clean fleet/recovery run passed, then the deployment driver was tightened
to set both non-root UID and GID explicitly and disable pytest's attempt to write
a cache beside read-only installed test configuration. A fresh-image repetition
records that identity. A startup-only retry also corrected the interval between
`lxc start` returning and the VM agent/nested daemon becoming ready; only the
read-only readiness check is retried, never a possibly completed mutation.

The final image export is retained on the reference host under
`.cache/reliability/final-runtime-r3/`; its exact filename, fingerprint, digest
and size are in [runtime-image.json](runtime-image.json). It is not stored in Git
or publicly downloadable from this repository. Obtain it from the lab operator
and verify the manifest before reuse. Generated TLS keys, PSKs, private journals
and physical credentials are absent from this public evidence collection.

Remaining acceptance work is unchanged: selected IEEE 1905 wire validation and
implementation, reviewed capability/profile intersection, an actual private pod
connection/profile, and real controller messages through EMOSA into an unchanged
physical pod with independent client observations.
