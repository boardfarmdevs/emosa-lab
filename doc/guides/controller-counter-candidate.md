# Test a native controller capability fix without replacing the baseline

**Scope note:** this is the counter-advertisement component exercise. Current
native onboarding also uses configuration-scope/lifetime fixes, and the newer
candidate adds sparse-ESP handling. See [current status](../project/current-status.md)
and the [frozen candidate identities](../project/integration-baseline.md).
Current Profile-1 agent runs advertise bytes; controller KiB/MiB support is a
separate capability.

This developer exercise fixes one measured interoperability defect: the pinned
controller handles KiB/MiB counters but omits the corresponding flag from its
discovery Response. EMOSA observes `0x40`; the candidate advertises `0xC0`.
The change is in the **native C++ controller**, not in EMOSA's parser or a packet
rewriter. [Retained results](../evidence/native-controller-counter/README.md)
separate the corrected advertisement from still-pending onboarding.

Read [native discovery](native-discovery.md) first. A discovered device with no
radios/BSSs remains incomplete, even after this flag is fixed. No pod, WSC
configuration, OVSDB operation or client test participates in this exercise.

## 1. Understand what the patch promises

EasyMesh 6.1 §6.1 requires the KiB/MiB indication; Table 117 assigns bit 7.
`0004-controller-counter-capability.patch` sets the existing native field. The
controller already records an agent's byte-counter units and converts received
metrics through `db::recalculate_attr_to_byte_units`. The build executes this
unchanged C++ function for bytes, KiB and MiB with zero, one, a typical count and
the maximum 32-bit wire count: **12 checks**. This is a function-level check,
not a complete traffic-metrics or ODH/data-lake experiment.

The candidate rebuilds the controller executable with NBAPI enabled against the
pinned native libraries. Transport, agent, HAL and Ambiorix libraries retain
their baseline bytes. Header dependencies have exact commit pins in
`deploy/peer-baseline/compatibility/controller-headers.json`. They are build
inputs, not replacements for specifications. The candidate is not promoted to
the standard baseline by this exercise.

## 2. Prepare inputs and build — HOST

Use the existing baseline and discovery setup, including its owned VM and
controller container. Build on HOST in a fresh private directory. The reproduced
build host is Ubuntu 22.04 x86_64; allow several GiB for sources, object files and
debug symbols. The runtime VM does not need a second full build tree.

Install build tools and the runtime libraries needed to link and run the native
regression. These commands are for the Ubuntu 22.04 HOST:

```bash
sudo apt-get install -y build-essential git patch binutils \
  libnl-3-200 libnl-route-3-200 libjson-c5 libssl-dev \
  libyajl2 liburiparser1 libevent-2.1-7 zlib1g
uv sync --frozen
mkdir -p .cache/native-controller-inputs
lxc file pull emosa-lab/opt/peer-artifacts/prplmesh-patched-source.tar.gz \
  .cache/native-controller-inputs/
lxc file pull emosa-lab/opt/peer-artifacts/prpl-install-nl80211-6.0.0.tar.gz \
  .cache/native-controller-inputs/
lxc file pull emosa-lab/opt/peer-artifacts/prpl-runtime-deps-6.0.0.tar.gz \
  .cache/native-controller-inputs/
uv run --with cmake==3.31.6 python \
  deploy/peer-baseline/compatibility/build-controller.py \
  --build "$PWD/.lab/controller-counter-learning-01" \
  --artifacts .cache/native-controller-inputs
```

The builder verifies all three archive hashes before extraction, fetches exact
header commits, applies the patch with no fuzz, builds and runs the C++ checks.
It leaves `candidate.json`, `regression.json`, `build.log` and
`stage/bin/beerocks_controller` in that new directory. The executable has debug
symbols stripped to reduce staging space; both executable hashes are recorded.
On failure, keep the directory and log; correct the cause and use a new directory.
Do not overwrite an earlier build to make a failed attempt disappear.

## 3. Stage the candidate — HOST commands, VM destination

Complete the unchanged discovery guide's staging first. Then copy only the
candidate and its provenance to a separate VM directory:

```bash
lxc exec emosa-lab -- mkdir -p \
  /opt/emosa-baseline/candidate-counter-learning-01/stage/bin \
  /opt/emosa-baseline/compatibility
lxc file push .lab/controller-counter-learning-01/stage/bin/beerocks_controller \
  emosa-lab/opt/emosa-baseline/candidate-counter-learning-01/stage/bin/
lxc file push .lab/controller-counter-learning-01/candidate.json \
  .lab/controller-counter-learning-01/regression.json \
  emosa-lab/opt/emosa-baseline/candidate-counter-learning-01/
lxc file push deploy/peer-baseline/patches/0004-controller-counter-capability.patch \
  emosa-lab/opt/emosa-baseline/
lxc file push deploy/peer-baseline/compatibility/controller-candidate.py \
  emosa-lab/opt/emosa-baseline/compatibility/
```

The runner requires all owned native/radio experiments to be idle and takes the
shared experiment lock. It verifies the existing controller and references,
retains a compressed byte-exact backup, installs the candidate with a clearly
marked experimental reference, then calls the same discovery trial. Native
binary checks remain active; EMOSA's code and admission gate remain unchanged.

## 4. Exercise failure recovery before the real comparison

Use a fresh lowercase label of at most 24 characters for every run:

```bash
lxc exec emosa-lab -- env PYTHONPATH=/opt/emosa-radio-manager/source \
  /opt/emosa/.venv/bin/python \
  /opt/emosa-baseline/compatibility/controller-candidate.py \
  --build /opt/emosa-baseline/candidate-counter-learning-01 \
  --label counter-failure-01 --fail-after-install
```

This command **intentionally exits unsuccessfully** after installing the
candidate. Inspect
`/opt/emosa-baseline/controller-candidates/counter-failure-01/result.json`:
`failure_injection` and `baseline_restored` must be true, with `status: failed`.
No discovery run should exist for that label. This proves the ordinary exception
path restores the executable and both references. It does not prove recovery
from power loss or SIGKILL.

Repeat the command with a new label and without `--fail-after-install`. Expect
`status: counter_flag_observed_admission_pending` and `baseline_restored: true`.
The discovery evidence is under `discovery-trials/LABEL`; the replacement,
backup and restoration records are under `controller-candidates/LABEL`.

The runner always stops and collects native services before restoration. Native
shutdown aborts remain visible in the discovery result. If stopping or restoring
fails, the command fails and retains the candidate, references and backup for
inspection. Do not start another experiment while restoration is unresolved.
After an uncatchable interruption, stop/collect owned services, verify the saved
baseline hash, decompress `baseline-controller.gz`, restore that executable and
`reference-before.json` to the controller container, restore the VM reference,
and verify both hashes before resuming. Retain the incident evidence.

## 5. Compare independent evidence and understand the remaining work

Compare a baseline capture, two fresh candidate captures, and a final baseline
capture after restoration. The expected flag sequence is `40 → c0 → c0 → 40`.
Both versions create only a device entry; represented radios/BSSs remain zero.

Recheck the published comparison on HOST (requires tshark):

```bash
python3 scripts/check-controller-candidate.py
```

This independently decodes the PCAPs and checks candidate hashes, restoration
results, the injected failure and the native inventory. It imports no EMOSA
implementation. The remaining `security_capability_absent` diagnostic describes
an absent field; it does **not** mean “insert three zero bytes.” In Table 90,
those bytes advertise actual DPP/HMAC-SHA256/AES-SIV capabilities. §13.1 binds
those capabilities to DPP, and §18 provides an omission rule for unsupported
features. The [updated procedure review](../protocol/procedure-audit.md)
records why the non-DPP profile decision still needs to be made explicitly.

**Learning checkpoint:** explain why the candidate has removed one observed
defect, why its baseline must be restored, and why neither result authorizes M1.
Next resolve the selected feature/admission contract and Early/AP capability
sequence, join the existing authenticated WSC-to-radio path, and require exact
controller radio/BSS inventory plus independent client behavior in the same run.
Physical acceptance still requires an unchanged qualified OpenSync pod.

## Optional lifecycle follow-up

For a full-duration recovery workload, follow the
[native lifecycle guide](../protocol/native-lifecycle.md). Its explicit
`--lifecycle` option additionally rebuilds the selected Linux BPL library and
compares actual baseline/candidate model destruction order. The guarded runner
backs up and restores that library as well as the controller. The default
counter/onboarding candidate described above retains the historical shared
libraries and their recorded shutdown defect.
