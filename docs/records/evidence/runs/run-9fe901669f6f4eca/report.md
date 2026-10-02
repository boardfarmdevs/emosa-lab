# EMOSA run run-9fe901669f6f4eca

Experiment execution: **blocked**; component verdict: **blocked**.
Interoperability: **blocked**.

Interface: `easymesh-wire`; backend: `ovsdb-sim`.

## Checks

| Check | Verdict | Expected | Observed |
| --- | --- | --- | --- |

## Timeline

| Sequence | UTC | Pod | Phase | Reason |
| --- | --- | --- | --- | --- |
| 1 | 2026-09-16T01:51:16.125Z | — | BLOCKED | MISSING_PREREQUISITE |

## Limits

- Semantic component testing does not evaluate EasyMesh provisioning or interoperability.
- No physical radio or independent Wi-Fi client evidence.
- Upstream reference schema and synthetic identities do not qualify a pod build.
- Lost-reply injection discards a real response at adapter ingress before interpretation.
- Simulation event triggers are controlled; OS scheduling is not deterministic.

## Evidence

- `inputs.json` — SHA-256 `ffb3c3fb47a262ebcef1493bfa791bc47bcd24288acd62cfc796b534eb1adb43`
- `events.json` — SHA-256 `b753505afe8d222662d32e848d2811b6f272e03d9aff3efe15fe4655c45ec510`
- `observations.json` — SHA-256 `37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570`
