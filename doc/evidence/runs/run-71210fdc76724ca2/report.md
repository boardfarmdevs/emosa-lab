# EMOSA run run-71210fdc76724ca2

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
| 1 | 2026-09-16T01:51:16.402Z | pod-1 | READY | — |
| 2 | 2026-09-16T01:51:16.407Z | pod-1 | REQUESTED | — |
| 3 | 2026-09-16T01:51:16.422Z | pod-1 | VALIDATED | — |
| 4 | 2026-09-16T01:51:16.424Z | pod-1 | SUBMITTED | — |
| 5 | 2026-09-16T01:51:16.431Z | pod-1 | INDETERMINATE | OUTCOME_UNKNOWN |
| 6 | 2026-09-16T01:51:16.434Z | — | BEFORE_DEVICE_APPLICATION | — |
| 7 | 2026-09-16T01:51:16.440Z | pod-1 | SIMULATED_MANAGER_FEEDBACK | — |
| 8 | 2026-09-16T01:51:17.523Z | pod-1 | OBSERVATION | — |
| 9 | 2026-09-16T01:51:17.525Z | pod-1 | OBSERVED_APPLIED | OUTCOME_UNKNOWN |

## Limits

- Semantic component testing does not evaluate EasyMesh provisioning or interoperability.
- No physical radio or independent Wi-Fi client evidence.
- Upstream reference schema and synthetic identities do not qualify a pod build.
- Lost-reply injection discards a real response at adapter ingress before interpretation.
- Simulation event triggers are controlled; OS scheduling is not deterministic.

## Evidence

- `inputs.json` — SHA-256 `9644169c636573001758b169246d339c057b521b32befa3e0ec761bd8f726c37`
- `events.json` — SHA-256 `7f54846c06d0a6d6d1e73364fb6976064a2efacafd00d62b40b4dabfcd41e73b`
- `observations.json` — SHA-256 `6ae183dc4605d424429713148d0f2003f4e8d0a1558734654f2b3cf6aad48680`
