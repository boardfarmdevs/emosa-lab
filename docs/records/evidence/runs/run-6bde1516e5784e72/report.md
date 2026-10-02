# EMOSA run run-6bde1516e5784e72

Experiment execution: **failed**; component verdict: **fail**.
Interoperability: **not_evaluated**.

Interface: `semantic`; backend: `ovsdb-sim`.

## Checks

| Check | Verdict | Expected | Observed |
| --- | --- | --- | --- |

## Timeline

| Sequence | UTC | Pod | Phase | Reason |
| --- | --- | --- | --- | --- |
| 1 | 2026-09-16T01:33:53.956Z | pod-1 | READY | — |
| 2 | 2026-09-16T01:33:53.959Z | pod-1 | REQUESTED | — |
| 3 | 2026-09-16T01:33:53.963Z | pod-1 | VALIDATED | — |
| 4 | 2026-09-16T01:33:53.965Z | pod-1 | SUBMITTED | — |
| 5 | 2026-09-16T01:33:53.970Z | pod-1 | CONFIG_COMMITTED | — |
| 6 | 2026-09-16T01:33:53.972Z | — | BEFORE_DEVICE_APPLICATION | — |
| 7 | 2026-09-16T01:33:58.989Z | — | ERROR | {'code': 'NOT_READY', 'message': 'independent manager command failed', 'details': {}} |

## Limits

- Semantic component testing does not evaluate EasyMesh provisioning or interoperability.
- No physical radio or independent Wi-Fi client evidence.
- Upstream reference schema and synthetic identities do not qualify a pod build.
- Lost-reply injection discards a real response at adapter ingress before interpretation.
- Simulation event triggers are controlled; OS scheduling is not deterministic.

## Evidence

- `inputs.json` — SHA-256 `94dde4cd0f6ae15455684860984c5dd2f41090518dccd8eb3664212fa730a9d7`
- `events.json` — SHA-256 `fb59945045718cbca110000bedc8dfc300dbc7468248c1d8806a22dbc268d80b`
- `observations.json` — SHA-256 `f0a70f589ee25e25e60795e8425c85c59340e59d4ef64fdd78d26ca745f0bf83`
