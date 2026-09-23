# Native controller counter-capability candidate

This evidence removes one measured native discovery defect. It does not establish
controller onboarding, normative profile conformance or physical pod behavior.
Use the [step-by-step guide](../../guides/controller-counter-candidate.md).

| Measurement | Result |
| --- | --- |
| Pinned baseline, earlier independently captured run 07 | Controller Capability `0x40` |
| Candidate, two fresh runs | Controller Capability `0xC0`; correlated Profile-1 Responses |
| Restored pinned baseline, fresh run | Controller Capability `0x40` again |
| Native C++ counter-conversion function | 12 cases passed: bytes/KiB/MiB, zero/one/typical/maximum 32-bit inputs |
| Deliberate exception immediately after candidate installation | Expected failure; executable and both references restored |
| Controller inventory after Search | Probe device present, zero radios and zero BSSs |
| WSC / configuration operations / physical pod access | None |

`candidate.json` records the three verified source/runtime archives, seven exact
header commits, patch and source hashes, build recipe hashes, compiler versions,
debug/stripped executable hashes and native regression result. Its
`installed_in_peer: false` describes the build output stage. The separate
`candidate-trial.json` files record temporary installation and restoration.
The patch rebuilds only the native controller; shared libraries and EMOSA's
implementation retain their existing bytes. The retained binary is an evaluation
candidate, not the repository's selected runtime baseline.

`candidate-01/` and `candidate-02/` contain independently captured Ethernet,
before/after controller inventory, the unchanged EMOSA probe result, the marked
candidate reference and wrapper result. `restored-baseline/` repeats the original
probe after restoration. `failure-restoration.json` retains the intentional
failure. The comparison checker imports no EMOSA code:

```bash
python3 scripts/check-controller-candidate.py
```

The earlier development attempt successfully captured the corrected flag but
failed restoration because two collection steps selected the same archive name.
The failure was preserved, the baseline manually restored and hash-verified,
and the wrapper's restoration archive name corrected. Only subsequent clean
trials are selected here. Existing native shutdown aborts remain visible in the
discovery results and are not reclassified as clean process exits.

Publication is an explicit file allowlist. Native logs, baseline executable
backups, compiled candidates, full source archives, local specification PDFs and
private configurations remain outside Git. See `summary.json` for the run labels
and remaining work. The absent security TLV is still reported by the probe;
§18's unsupported-feature omission rule must be applied to an explicit non-DPP
contract before changing admission. Automatic Early/AP reporting, WSC integration
and complete native radio/BSS inventory remain required, followed by the unchanged
qualified physical pod and an independent client.
