# Test an isolated native peer fix and a one-BSS policy

This exercise addresses two concrete problems found in the selected prplMesh
baseline: an invalid Wi-Fi 6 MCS length and provisioning broader than EMOSA's
initial one-BSS mapping. It runs **native controller → native agent**, without
EMOSA or an OpenSync pod. Read the [retained summary](../evidence/native-compatibility/summary.json)
alongside this guide; a native functional pass is not EMOSA onboarding proof.

## 1. Understand what changed

The upstream nl80211 parser initialized an HE MCS byte counter to zero and added
the mandatory first four bytes only when it encountered VHT capabilities. An HE
radio without VHT therefore advertised length zero. The counter also lived across
bands and repeated netlink messages. The retained 2.4 GHz native capture exposed
the first defect even though the native peer accepted the message.

The [candidate patch](../../deploy/peer-baseline/patches/0003-prplmesh-he-mcs-length.patch)
derives length from the HE width flags that the existing parser reports: four
bytes for the base Rx/Tx pair, plus four for each advertised wider pair. It
replaces the length nibble rather than accumulating it. It does not change
upstream width interpretation, aggregation of roles, capability semantics, MCS
ordering or controller profile behavior.

Only `libbwl` is rebuilt. The baseline's primary-BSS fix is also applied. The
controller, agent, transport, other libraries and hostap binaries retain their
pinned versions. The baseline reference and existing build are preserved; the
candidate has its own build directory, digest and run labels. No candidate
archive is substituted for the published baseline artifact.

The new `--policy sole-fronthaul` option prepares one AP BSS on the external
native agent and submits one fronthaul credential to the controller for that
agent. The controller's own radio retains its fronthaul/backhaul BSSs. This
experiment uses wired management and supports a single clean run per invocation.
The ordinary native baseline remains `front-and-backhaul` by default.

## 2. Review the result before rerunning

The [C++ regression executable](../../deploy/peer-baseline/compatibility/he-length.cpp)
injects libnl attribute messages into the **actual upstream parser in the loaded
library**. It does not test a reimplementation of the formula. It covers HE
with/without VHT, wider pairs, repeated iftypes, split replies and independent
bands. The retained baseline fails six of nine cases; the candidate passes all
nine. These synthetic parser inputs require no radio or network connection.

The live trial uses the existing owned hwsim radio lab, a native controller and
agent, and separate wired and wpa_supplicant client containers. Selected capture
observations are:

| Observation | Meaning |
| --- | --- |
| Search profile 2; Response profile 1 | The profile mismatch remains open under the selected EasyMesh 6.1 rule |
| One M2 and no M8 in the selected response | The new controller policy narrows the observed provisioning payload set; encrypted contents and complete EMOSA admission are not thereby validated |
| One agent radio/BSS in controller inventory | The native controller observed the selected native agent's provisioned topology |
| Wi-Fi 6 AP/STA length nibbles are both 4 | The live capability report reflects the rebuilt HAL fix |
| Independent client association and fresh traffic | The selected native provisioning produced a usable BSS in this lab |
| Shutdown abort recorded separately | Functional onboarding does not establish clean shutdown or production reliability |

The 0xAA bytes still inherit the upstream MCS ordering behavior. A length fix does
not validate their Rx/Tx meaning. The ambiguous `0x88` conversion also remains
pending. The candidate is deliberately not promoted to a qualified peer profile.

For an offline reproduction on HOST, install the manual's tshark prerequisite:

```bash
python3 deploy/peer-baseline/compatibility/check-capture.py
```

This checks only a hash-pinned, reviewed synthetic capture. It imports no EMOSA
code, sends no traffic and compares raw length bytes because the older dissector
labels that nibble as reserved. Expect `passed: true`, with false conformance,
MCS-ordering qualification and EMOSA-onboarding flags. CI repeats this check.

## 3. Prepare the existing native lab

Use [native baseline setup](../../deploy/peer-baseline/README.md) first. This guide
does not create a second lab. The outer VM must be `emosa-lab`; its four owned
baseline containers must be running, with their hwsim PHYs assigned and setup
Ethernet removed. Stop and collect owned radio/native services before installing
a candidate. Do not run the radio-manager and native harnesses concurrently.

On HOST, inspect capacity and ownership before staging:

```bash
lxc exec emosa-lab -- lxc list -c ns
lxc exec emosa-lab -- df -h /opt
```

Allow space for a new build, backup libraries and all run evidence; approximately
700 MiB free is a useful minimum for this bounded exercise. Existing evidence is
not disposable scratch space. The source/runtime archives and hostap build from
the baseline guide must already be present under `/opt`.

Stage the current baseline Python files and references using that guide. In
particular, the VM and both peer containers must have identical `reference.json`
bytes. The candidate runner intentionally refuses mismatched references. Then,
from the repository root on HOST, stage the additional files:

```bash
lxc exec emosa-lab -- mkdir -p /opt/emosa-baseline/compatibility
lxc file push deploy/peer-baseline/compatibility/build.sh \
  deploy/peer-baseline/compatibility/he-length.cpp \
  deploy/peer-baseline/compatibility/run.py \
  emosa-lab/opt/emosa-baseline/compatibility/
lxc file push deploy/peer-baseline/patches/0003-prplmesh-he-mcs-length.patch \
  emosa-lab/opt/emosa-baseline/
lxc file push deploy/peer-baseline/bwl-overlay/CMakeLists.txt \
  emosa-lab/opt/emosa-baseline/bwl-overlay/
```

The existing `0002-prplmesh-primary-bss-identity.patch` must also be staged as in
the baseline guide. All new source is off the pods; these are lab containers.

## 4. Build and run a new candidate

Run from HOST. Choose an unused build directory and run label; the examples
intentionally do not overwrite a prior execution:

```bash
lxc exec emosa-lab -- bash /opt/emosa-baseline/compatibility/build.sh \
  /opt/emosa-baseline/candidate-my-he-length-01
lxc exec emosa-lab -- python3 /opt/emosa-baseline/compatibility/run.py \
  --build /opt/emosa-baseline/candidate-my-he-length-01 \
  --label my-sole-fronthaul-01
```

The builder checks the pinned source/install archive hashes, applies both patches
with no fuzz, compiles the real NL80211 backend, runs the regression and records
the resulting library hash. It refuses an existing build directory. Build logs,
package versions, patch hashes and regression output remain in that directory.

The runner checks ownership, idle services and current library/reference hashes.
It backs up the two peer libraries and references, temporarily installs the
candidate, invokes a clean wired native trial, then collects shutdown results and
restores the original bytes. `baseline_restored: true` is supported by library
digest checks. A native shutdown abort remains `abnormal_shutdown: true`, even
when the functional run exits zero.

Inspect both output locations inside the VM:

- `/opt/emosa-baseline/runs/my-sole-fronthaul-01/`: functional observations,
  copied harness/hashes, runtime reference, inventory, captures and clients.
- `/opt/emosa-baseline/compatibility-runs/my-sole-fronthaul-01/`: original library
  and reference backups, candidate reference, shutdown records and restoration
  result. Keep these private until reviewed.

If collection or restoration raises an error, do not launch another candidate.
Inspect the retained backups and service state; collect/stop owned services before
restoring the recorded originals. The runner intentionally avoids replacing a
library when it cannot complete shutdown collection. A failed run is retained,
not retried under the same label.

## 5. Keep the remaining decisions explicit

This closes the concrete length defect for the candidate and demonstrates a
narrower controller policy. Next, resolve the peer's profile behavior against its
implemented mandatory functions, validate capability ordering with authoritative
inputs and independent asymmetric examples, and finish the IEEE/WFA procedure
contract. The [first complete experiment](first-wire-experiment.md) specifies how
to join genuine controller messages to EMOSA and then qualify an unchanged pod.
