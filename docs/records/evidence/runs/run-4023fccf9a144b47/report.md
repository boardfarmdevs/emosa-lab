# EMOSA run run-4023fccf9a144b47

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
| 1 | 2026-09-16T01:39:12.657Z | pod-1 | READY | — |
| 2 | 2026-09-16T01:39:12.659Z | pod-1 | REQUESTED | — |
| 3 | 2026-09-16T01:39:12.662Z | pod-1 | VALIDATED | — |
| 4 | 2026-09-16T01:39:12.664Z | pod-1 | SUBMITTED | — |
| 5 | 2026-09-16T01:39:12.670Z | pod-1 | CONFIG_COMMITTED | — |
| 6 | 2026-09-16T01:39:12.671Z | — | BEFORE_DEVICE_APPLICATION | — |
| 7 | 2026-09-16T01:39:17.825Z | pod-1 | SIMULATED_MANAGER_FEEDBACK | — |
| 8 | 2026-09-16T01:39:20.026Z | pod-1 | OBSERVATION | — |
| 9 | 2026-09-16T01:39:20.032Z | pod-1 | OBSERVED_APPLIED | — |

## Limits

- Semantic component testing does not evaluate EasyMesh provisioning or interoperability.
- No physical radio or independent Wi-Fi client evidence.
- Upstream reference schema and synthetic identities do not qualify a pod build.
- Lost-reply injection discards a real response at adapter ingress before interpretation.
- Simulation event triggers are controlled; OS scheduling is not deterministic.

## Evidence

- `inputs.json` — SHA-256 `94dde4cd0f6ae15455684860984c5dd2f41090518dccd8eb3664212fa730a9d7`
- `events.json` — SHA-256 `1d2eb95b00218155bbce1109b52b133d5ff8f033db28a9805ea0e5ec66578721`
- `observations.json` — SHA-256 `3f74784ad80c6e0f010e1b10531c0d547091f623d9cfe7488779b827b4cbbf8c`
