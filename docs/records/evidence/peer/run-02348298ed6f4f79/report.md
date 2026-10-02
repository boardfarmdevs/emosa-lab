# EMOSA run run-02348298ed6f4f79

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
| 1 | 2026-09-16T03:39:19.942Z | pod-1 | READY | — |
| 2 | 2026-09-16T03:39:19.950Z | pod-1 | REQUESTED | — |
| 3 | 2026-09-16T03:39:19.962Z | pod-1 | VALIDATED | — |
| 4 | 2026-09-16T03:39:19.974Z | pod-1 | SUBMITTED | — |
| 5 | 2026-09-16T03:39:19.985Z | pod-1 | CONFIG_COMMITTED | — |
| 6 | 2026-09-16T03:39:19.990Z | — | BEFORE_DEVICE_APPLICATION | — |
| 7 | 2026-09-16T03:39:19.999Z | pod-1 | SIMULATED_MANAGER_FEEDBACK | — |
| 8 | 2026-09-16T03:39:20.004Z | pod-1 | OBSERVATION | — |
| 9 | 2026-09-16T03:39:20.013Z | pod-1 | OBSERVED_APPLIED | — |

## Limits

- Semantic component testing does not evaluate EasyMesh provisioning or interoperability.
- No physical radio or independent Wi-Fi client evidence.
- Upstream reference schema and synthetic identities do not qualify a pod build.
- Lost-reply injection discards a real response at adapter ingress before interpretation.
- Simulation event triggers are controlled; OS scheduling is not deterministic.

## Evidence

- `inputs.json` — SHA-256 `8f5e3e488b31ecf222b68050b9c2633fb37958d7983a7ec16a3d5254c1cfe80c`
- `events.json` — SHA-256 `95d5be4d9cf557d715536eaec4bc5f2ca25bab97fd624fe889b100e41e6f4869`
- `observations.json` — SHA-256 `7aa011ac4a45bca220aeeb642a478e013c5485ae2c9b81db6c1a1dd4bd7f5e16`
