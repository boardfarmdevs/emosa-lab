# EMOSA run run-65264f5d645e4bb7

Experiment execution: **completed**; component verdict: **pass**.
Interoperability: **not_evaluated**.

Interface: `semantic`; backend: `ovsdb-sim`.

## Checks

| Check | Verdict | Expected | Observed |
| --- | --- | --- | --- |
| pod-1:operation | pass | OBSERVED_APPLIED | OBSERVED_APPLIED |

## Timeline

| Sequence | UTC | Pod | Phase | Reason |
| --- | --- | --- | --- | --- |
| 1 | 2026-09-16T01:51:16.418Z | pod-1 | READY | — |
| 2 | 2026-09-16T01:51:16.422Z | pod-1 | REQUESTED | — |
| 3 | 2026-09-16T01:51:16.432Z | pod-1 | VALIDATED | — |
| 4 | 2026-09-16T01:51:16.435Z | pod-1 | SUBMITTED | — |
| 5 | 2026-09-16T01:51:16.442Z | pod-1 | CONFIG_COMMITTED | — |
| 6 | 2026-09-16T01:51:16.444Z | — | BEFORE_DEVICE_APPLICATION | — |
| 7 | 2026-09-16T01:51:16.450Z | pod-1 | SIMULATED_MANAGER_FEEDBACK | — |
| 8 | 2026-09-16T01:51:16.453Z | pod-1 | OBSERVATION | — |
| 9 | 2026-09-16T01:51:16.455Z | pod-1 | OBSERVED_APPLIED | — |

## Limits

- Semantic component testing does not evaluate EasyMesh provisioning or interoperability.
- No physical radio or independent Wi-Fi client evidence.
- Upstream reference schema and synthetic identities do not qualify a pod build.
- Lost-reply injection discards a real response at adapter ingress before interpretation.
- Simulation event triggers are controlled; OS scheduling is not deterministic.

## Evidence

- `inputs.json` — SHA-256 `8f5e3e488b31ecf222b68050b9c2633fb37958d7983a7ec16a3d5254c1cfe80c`
- `events.json` — SHA-256 `239f9849b09d69022021b9acfddde6366307e30a11bc6c54324a2d247fcb4093`
- `observations.json` — SHA-256 `b56b0c75362287d7a591ef170ac1f965b618d4db4b3ec5e8bc29f263c331d254`
