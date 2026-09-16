# EMOSA run run-aaef446cd0e74cb3

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
| 1 | 2026-09-16T03:40:08.980Z | pod-1 | READY | — |
| 2 | 2026-09-16T03:40:08.988Z | pod-1 | REQUESTED | — |
| 3 | 2026-09-16T03:40:09.000Z | pod-1 | VALIDATED | — |
| 4 | 2026-09-16T03:40:09.005Z | pod-1 | SUBMITTED | — |
| 5 | 2026-09-16T03:40:09.015Z | pod-1 | INDETERMINATE | OUTCOME_UNKNOWN |
| 6 | 2026-09-16T03:40:09.021Z | — | BEFORE_DEVICE_APPLICATION | — |
| 7 | 2026-09-16T03:40:09.030Z | pod-1 | SIMULATED_MANAGER_FEEDBACK | — |
| 8 | 2026-09-16T03:40:10.178Z | pod-1 | OBSERVATION | — |
| 9 | 2026-09-16T03:40:10.184Z | pod-1 | OBSERVED_APPLIED | OUTCOME_UNKNOWN |

## Limits

- Semantic component testing does not evaluate EasyMesh provisioning or interoperability.
- No physical radio or independent Wi-Fi client evidence.
- Upstream reference schema and synthetic identities do not qualify a pod build.
- Lost-reply injection discards a real response at adapter ingress before interpretation.
- Simulation event triggers are controlled; OS scheduling is not deterministic.

## Evidence

- `inputs.json` — SHA-256 `9644169c636573001758b169246d339c057b521b32befa3e0ec761bd8f726c37`
- `events.json` — SHA-256 `f67654789d5d8adb96fce8fec711d238ecc52fab83048ef430cecf91b5d51f6c`
- `observations.json` — SHA-256 `12e29548d7d5caa73832e39f1e0a172de217f7af125dda54475789b320f20ce8`
