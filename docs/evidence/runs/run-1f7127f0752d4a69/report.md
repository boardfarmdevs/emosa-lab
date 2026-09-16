# EMOSA run run-1f7127f0752d4a69

Experiment execution: **completed**; component verdict: **fail**.
Interoperability: **not_evaluated**.

Interface: `semantic`; backend: `ovsdb-sim`.

## Checks

| Check | Verdict | Expected | Observed |
| --- | --- | --- | --- |
| pod-1:operation | fail | OBSERVED_APPLIED | OBSERVED_APPLIED |

## Timeline

| Sequence | UTC | Pod | Phase | Reason |
| --- | --- | --- | --- | --- |
| 1 | 2026-09-16T01:51:16.410Z | pod-1 | READY | — |
| 2 | 2026-09-16T01:51:16.414Z | pod-1 | REQUESTED | — |
| 3 | 2026-09-16T01:51:16.424Z | pod-1 | VALIDATED | — |
| 4 | 2026-09-16T01:51:16.426Z | pod-1 | SUBMITTED | — |
| 5 | 2026-09-16T01:51:16.435Z | pod-1 | INDETERMINATE | OUTCOME_UNKNOWN |
| 6 | 2026-09-16T01:51:16.440Z | — | BEFORE_DEVICE_APPLICATION | — |
| 7 | 2026-09-16T01:51:16.447Z | pod-1 | SIMULATED_MANAGER_FEEDBACK | — |
| 8 | 2026-09-16T01:51:17.547Z | pod-1 | OBSERVATION | — |
| 9 | 2026-09-16T01:51:17.549Z | pod-1 | OBSERVED_APPLIED | OUTCOME_UNKNOWN |

## Limits

- Semantic component testing does not evaluate EasyMesh provisioning or interoperability.
- No physical radio or independent Wi-Fi client evidence.
- Upstream reference schema and synthetic identities do not qualify a pod build.
- Lost-reply injection discards a real response at adapter ingress before interpretation.
- Simulation event triggers are controlled; OS scheduling is not deterministic.

## Evidence

- `inputs.json` — SHA-256 `9e432c01fcedc1889d5825b75875420350f4b98b2020002a02ca2df83c279b24`
- `events.json` — SHA-256 `7f46b4ad004f1607c2e69d3778de7450662a097f230889fcbc3aaf470df257d9`
- `observations.json` — SHA-256 `a0466bbde66dab76bc901c6b3d36bfe1cac20136fe2ac87e148f2e3e2ebcf871`
