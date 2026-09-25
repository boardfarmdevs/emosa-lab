# Map declared Wi-Fi 6 roles through the adapter service

This exercise connects two simulated OpenSync pods to the actual EMOSA service
and obtains a Wi-Fi 6 capability value for each declared HE radio. It runs on
**HOST**, in your learning or development checkout. It needs the Python/uv and
disposable OVSDB installation from [manual chapter 3](team-manual.md#3-set-up-a-developer-checkout).
It needs no LXD VM, hwsim radio, wireless client or physical pod.

The adapter is read-only. The simulation harness owns the databases, supplies
invented capabilities and changes its own fixtures to test failure handling.
Passing this exercise establishes a bounded data-mapping and recovery path.
The controller still cannot discover this local diagnostic representation over
EasyMesh, and no physical device's capabilities are established.

## 1. Understand what the input supplies

An AP role serves wireless clients; a non-AP STA role can provide a wireless
backhaul. They can have different receive/transmit MCS maps and feature limits.
The [field walkthrough](he-wifi6.md) explains those terms and the standalone
`0xAA` codec. This exercise adds the connection from explicit role declarations
to that codec through the service's existing capability diagnostic.

Fresh OVSDB observations establish the bound device identity, radio inventory,
interfaces and modes. They do not establish the supported MCS ranges, OFDMA
user limits or every possible role. The input file supplies those separate
claims. Its digest and evidence-file hashes identify the exact declarations
used; hashes cannot prove that hardware supports them.

The new `extensions.wifi6` input contains exactly the radios explicitly marked
`he: true` in `extensions.technology`. Every entry supplies:

| Input | Purpose |
| --- | --- |
| `radio_id` | Select the existing bound radio; its RUID comes from the verified binding |
| `evidence_id` | Identify a hash-checked evidence entry whose `covers` includes `wifi6` |
| `complete_role_inventory` | Explicitly declare that the supported role list is complete |
| `roles` | One or two distinct roles: `ap` and/or `non_ap_sta` |
| Per-role `mcs` | Separate eight-code Rx/Tx maps for the up-to-80 MHz field; wider pairs are explicit `null` in this service subset |
| Per-role `features` | All sixteen booleans required by the selected capability fields; omitted/unknown flags cannot become false |
| Per-role `ap_user_limits` | Explicit DL/UL MU-MIMO and OFDMA limits; the STA role uses zeros under the local input policy |

A role observed on a bound interface must appear in the declaration. An extra
declared capability role does not prove that a corresponding interface is
currently connected. The fixture has an AP role on radio 1, and AP plus STA roles
on radio 2. It deliberately declares HE support on radio 2 without VHT support;
HE capability mapping must not depend on an unrelated VHT branch.

Only synthetic profiles are admitted. `source_kind: physical_pod_draft` remains
blocked by the parent loader, even if every role and hash is supplied.

## 2. Run the complete exercise

From the checkout root, choose a **new** output directory:

```bash
uv run python -m emosa_lab.simulation.radio_capabilities \
  --with-wifi6 --output .lab/wifi6-role-first
```

`--with-wifi6` includes the existing technology and Device Inventory inputs, so
you do not also need `--with-extensions`. The command starts two disposable
OVSDB servers and the EMOSA service. Each simulated pod initiates its management
connection to EMOSA. The harness requests the real `radio.capabilities` API and
runs the user CLI, then checks failure/recovery behavior. It stops all of its
processes before returning. Expected exit code: **0**, with `passed: true` and
the report path printed on stdout.

The result directory also contains private local configuration, database and log
artifacts. Treat it as a local lab run, not a publication bundle. Use a fresh
directory for each run; the command refuses to replace an existing one.

## 3. Read readiness at the right level

Inspect the saved report without starting another service:

```bash
uv run python - <<'PY'
import json
from pathlib import Path

report = json.loads(Path(".lab/wifi6-role-first/report.json").read_text())
for pod in report["stages"]["connected"]:
    print(pod["pod_id"], "basic ready:", pod["ready"])
    for name, result in pod["extensions"].items():
        print(" ", name, result["ready"], result["blockers"])
    for value in pod["extensions"]["wifi6"]["values"]:
        radio = value["decoded"]
        print(" ", radio["ruid"], [role["known_role"] for role in radio["roles"]])
    print(" complete report:", pod["complete_ap_capability_report"])
PY
```

Expected for **each** pod:

| Result | Expected | Meaning |
| --- | --- | --- |
| Top-level `ready` | `true` | The Basic radio-capability context passed its checks |
| `extensions.device_inventory.ready` | `true` | The synthetic identity/inventory values can be produced |
| `extensions.wifi6.ready` | `true` | Two `0xAA` values can be produced from the declared roles |
| `extensions.technology.ready` | `false` | The complete HE technology set still cannot be produced |
| `complete_ap_capability_report` | `false` | Required report fields/procedures remain incomplete |

The technology blocker is `he_and_wifi6_mapping_pending`: the required **pair**
of HE (`0x88`) and Wi-Fi 6 (`0xAA`) values is incomplete because `0x88` conversion
remains unresolved. A standalone companion must not bypass that requirement.
The CLI exit code is based on Basic readiness, so its initial exit **0** does
not imply every extension passed. Read each section and the complete-report flag.

Each Wi-Fi 6 value includes its raw hex, SHA-256 and decoded role fields. The
AP maps intentionally differ by direction, and the STA maps differ from the AP
maps. Pod identities also differ. This makes accidental field swaps or use of
another pod's values detectable. A successful role declaration is not evidence
that a controller has received it or that wireless clients can use the feature.

With an already running configured service, the same diagnostic is available
as `emosa --socket SOCKET pod POD radio-capabilities --json`; replace `SOCKET`
and `POD` with that service's values. The completed exercise above has already
stopped its service, so use the saved report for its results.

## 4. Inspect the input and understand the failure rules

Open `.lab/wifi6-role-first/pod-1/capabilities.json` in your editor and find
`extensions.wifi6`. The adjacent `synthetic-radio-contract.json` is the retained
declaration material. Its hash is referenced by the profile, and the adapter's
local configuration pins the profile hash. The schema is
[radio-capabilities.schema.json](../../schemas/radio-capabilities.schema.json).

Do not merely edit a pinned file and expect the service to accept the change.
A digest mismatch intentionally withdraws **all** capability values for that
pod. A reviewed new profile/evidence pair needs new hashes in the local service
configuration. Future physical inputs require qualification in addition to these
mechanical checks; populating a JSON file does not provide that qualification.

Within a valid parent context, any bad Wi-Fi 6 radio or role withdraws the entire
Wi-Fi 6 section. No earlier radio's value leaks through. Device Inventory can
remain ready because its checks are separate. The mapper rejects missing or
duplicate HE radios, incomplete/duplicate roles, absent observed AP/STA roles,
wrong evidence scope, invalid MCS codes and contradictory user limits/features.
Unknown HE support remains unknown. Only explicitly unsupported HE on every
radio permits a ready Wi-Fi 6 section with no values.

The following are **local service scope policies**, not claims that the standard
forbids other devices from supporting them:

- The initial service mapping accepts only the up-to-80 MHz pair. Standalone
  codecs support wider fields, but their complete target context is not reviewed.
- Each direction must declare at least one supported stream count.
- AP user limits must agree with the corresponding MU-MIMO/OFDMA feature flags.
  Non-AP STA inputs must leave those AP-specific limits zero.
- Spatial reuse and anticipated channel usage must be false: EMOSA does not yet
  implement their complete configuration/reporting behavior. Hardware support
  alone cannot justify those advertised agent capabilities.

## 5. Verify withdrawal, isolation and recovery

The report has twelve stages. `profile_changed`, `evidence_changed`,
`country_changed`, `firmware_changed` and `disconnected` must show unavailable
capabilities with no emitted values. `other_pod_unchanged` and
`other_pod_while_disconnected` retain the other pod's values. `reconnected` and
`restarted` reproduce the original declarations and value bytes; the latter
follows an actual adapter process crash and restart.

At the end, `observed_tables_unchanged_after_restoring_fixture_faults: true`
means the harness restored its own mutations and compared all monitored rows
with the initial fixture. The adapter ran in read-only mode throughout.

Reproduce the focused checks with:

```bash
uv run pytest tests/test_wifi6_projection.py
uv run pytest tests/test_radio_capability_service.py
```

The second command requires the disposable OVSDB binaries. The
[retained evidence](../evidence/wifi6-inputs/summary.json) also records a run from
an installed wheel outside the checkout, so the demonstration does not depend
on importing source files from the development directory.

Next, resolve the exact `0x88` conversion and independent positive peer vectors,
then combine the truthful HE and Wi-Fi 6 values into the complete technology
set. Native peer/profile compatibility, remaining report dependencies and the
exact IEEE 1905 base/amendment review are still needed before wire onboarding.
The acceptance path remains **real EasyMesh messages → EMOSA adapter → unchanged
physical pod → independently observed behavior**.
