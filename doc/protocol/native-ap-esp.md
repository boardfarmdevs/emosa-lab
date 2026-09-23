# Verify native reception of sparse AP service parameters

This exercise fixes a native-controller parser defect that would prevent later
AP reporting from working reliably. EMOSA is the Python EasyMesh-to-OpenSync
adapter. The affected C++ code belongs to the optional prplMesh controller
candidate used to evaluate it.

Read [AP metric reports](ap-metric-reports.md) and
[native lifecycle](native-lifecycle.md) first. The
[retained evidence](../evidence/ap-esp/README.md) includes a real baseline crash,
component checks, the corrected live trial and restoration observations.
**This is a synthetic parser diagnostic. AP measurement qualification and full
sustained acceptance remain pending.**

## Why optional fields change offsets

EasyMesh 6.1 §17.2.22/Table 45 requires BE service information and permits BK, VO
and VI entries. Each present entry occupies three octets. Absent entries occupy
no space. Their list order is BE, BK, VO, VI; this is distinct from the IEEE
numeric access-category order.

| Presence | ESP array | VI starting offset |
| --- | --- | ---: |
| BE, BK, VO, VI | BE → BK → VO → VI | 9 |
| BE, VI | BE → VI | 3 |
| BE, BK, VI | BE → BK → VI | 6 |

The baseline controller always asked its TLVF parser for VI at offset 9. For
BE+VI, the six-octet array has no byte at that offset. TLVF returned a null
pointer, and the controller dereferenced it. The live baseline trial records
SIGSEGV after this report; its data-model read then fails because the controller
has exited.

The optional patch walks only present entries, requires BE, and checks the exact
array length before changing this BSS's ESP or utilization fields. It preserves
the three bytes of each entry and the selected native model's existing integer
representation. It does not decide IEEE-to-EasyMesh ESP subfield reordering or
claim that those bytes came from an estimator.

## 1. Review the evidence on HOST

No VM or root access is needed for these retained-capture checks:

```bash
python3 scripts/check-native-ap-esp.py --baseline-failure \
  doc/evidence/ap-esp/native-ap-esp-old-01
python3 scripts/check-native-ap-esp.py \
  doc/evidence/ap-esp/native-ap-esp-new-01
uv run pytest -q tests/test_native_ap_esp_audit.py
```

The first command verifies an expected failed trial, not successful onboarding
through the entire diagnostic. The second checks each captured message against
later controller inventory, the eight rejected malformed inputs, clean shutdown
and exact restoration. Neither treats a successful socket send as receipt.

## 2. Build the optional candidate on HOST

Prepare the pinned native archives using the
[controller build guide](../guides/controller-counter-candidate.md). Substitute
the directory where those three archives actually reside. Use a new build path:

```bash
uv run --with cmake==3.31.6 python \
  deploy/peer-baseline/compatibility/build-controller.py \
  --build "$PWD/.lab/controller-ap-esp-learning-01" \
  --artifacts .cache/native-controller-inputs \
  --onboarding --lifecycle --ap-esp
```

The `--ap-esp` option adds `0007-controller-ap-esp-presence.patch` and a compiled
regression using the pinned TLVF library and the same helper called by the
controller. It exercises all eight valid combinations and 232 invalid
presence/length combinations. Invalid inputs must leave its output unchanged.
The baseline's invalid fixed-offset lookup is also reproduced through TLVF's
actual bounds check. Existing counter and library-lifetime checks still run.

Inspect `candidate.json`, `ap-esp-regression.json`, `regression.json`,
`lifetime-regression.json` and `build.log`. An unsuccessful build is never an
installation candidate. Preserve its directory and use another name after a fix.

## 3. Stage only while the owned lab is idle

Use the [lifecycle staging procedure](native-lifecycle.md#3-stage-the-candidate-and-current-harness--host--vm)
with the new build name. Stage its executable, BPL library, candidate metadata
and regression files. Also stage the current wrapper/harness and new probe:

```bash
lxc exec emosa-lab -- mkdir -p \
  /opt/emosa-baseline/candidate-ap-esp-learning-01/stage/bin \
  /opt/emosa-baseline/candidate-ap-esp-learning-01/stage/lib
lxc file push .lab/controller-ap-esp-learning-01/stage/bin/beerocks_controller \
  emosa-lab/opt/emosa-baseline/candidate-ap-esp-learning-01/stage/bin/
lxc file push .lab/controller-ap-esp-learning-01/stage/lib/libbpl.so.6.0.0 \
  emosa-lab/opt/emosa-baseline/candidate-ap-esp-learning-01/stage/lib/
lxc file push .lab/controller-ap-esp-learning-01/candidate.json \
  .lab/controller-ap-esp-learning-01/regression.json \
  .lab/controller-ap-esp-learning-01/lifetime-regression.json \
  .lab/controller-ap-esp-learning-01/ap-esp-regression.json \
  emosa-lab/opt/emosa-baseline/candidate-ap-esp-learning-01/
lxc file push deploy/peer-baseline/compatibility/controller-candidate.py \
  emosa-lab/opt/emosa-baseline/compatibility/
lxc file push deploy/peer-baseline/native-onboarding.py \
  deploy/peer-baseline/ap-esp-probe.py \
  deploy/peer-baseline/patches/0007-controller-ap-esp-presence.patch \
  emosa-lab/opt/emosa-baseline/
```

Here `candidate-ap-esp-learning-01` refers to your newly staged candidate, not the
baseline. The wrapper validates the extra patch and its native regression before
installation, keeps the existing ownership lock and restores every replaced file.
No software is installed on a physical OpenSync pod.

## 4. Run the diagnostic through the real controller

```bash
lxc exec emosa-lab -- env \
  PYTHONPATH=/opt/emosa-radio-manager/source \
  EMOSA_OVS_BIN=/opt/emosa-radio-manager/ovsdb \
  /opt/emosa/.venv/bin/python /opt/emosa-baseline/native-onboarding.py \
  --build /opt/emosa-baseline/candidate-ap-esp-learning-01 \
  --label ap-esp-learning-01 --ap-esp-probe
```

Normal EMOSA onboarding creates the represented radio/BSS and the independent
clients pass their traffic checks. The EMOSA worker stops before the diagnostic.
The separate probe then sends deliberately synthetic `0x800C` frames from the
owned virtual-agent interface. Each contains a single AP Metrics TLV so the test
isolates that parser; it is not a complete AP Metrics Response/policy fulfillment
exercise. Malformed cases deliberately violate the TLV's presence/length rules.

The probe tests eight valid presence sets, then eight malformed arrays. Each
valid case must update the present categories with distinctive values. Missing
categories preserve their prior native model values; the diagnostic does not
claim those older values remain fresh measurements. A malformed case must leave
ESP and utilization unchanged. Actual observations are saved after each send.

`--ap-esp-probe` cannot be combined with an active soak. Its synthetic values
must never be counted as measurements in sustained acceptance. Reproducing the
baseline comparison with the older lifecycle candidate intentionally crashes the
owned controller; use a separate label and collect its failed result before
starting the patched trial.

## 5. Collect, audit and understand the remaining boundary

```bash
python3 scripts/collect-native-review.py ap-esp-learning-01 \
  .lab/ap-esp-learning-review --candidate candidate-ap-esp-learning-01
python3 scripts/observe-native-restoration.py ap-esp-learning-01 \
  .lab/ap-esp-learning-review/post-restoration-observation.json
```

The lifecycle/onboarding audits also need the exact trial reference. Collect it:

```bash
lxc file pull \
  emosa-lab/opt/emosa-baseline/controller-candidates/ap-esp-learning-01/reference-candidate.json \
  .lab/ap-esp-learning-review/reference-candidate.json
```

Then run `python3 scripts/check-native-ap-esp.py .lab/ap-esp-learning-review`.
Its positive result establishes
parser safety and correct placement of opaque ESP bytes in the selected native
model. It does not establish their semantic conversion, estimator accuracy,
complete AP/STA report delivery, final-session reporting or physical-pod viability.
Those remain required before the full integrated 15-minute acceptance can pass.
