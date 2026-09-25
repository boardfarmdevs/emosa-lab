# Radio capability inputs: what the virtual agent can truthfully describe

This exercise adds explicit capability facts to the adapter's stable radio
identities. Run it on **HOST**, in your development or learning checkout. It uses
two disposable OVSDB servers and one actual `emosa serve` process. No LXD, hwsim,
wireless client or physical pod is needed: this experiment tests how EMOSA checks
and represents capability inputs, not whether a radio can transmit.

The output is one EasyMesh **AP Radio Basic Capabilities value (`0x85`)** per
represented radio. It contains no outer TLV header or IEEE message and is not
sent to a controller. A valid result does not complete an AP Capability Report,
qualify an EasyMesh profile or prove onboarding. See the
[retained evidence](../evidence/radio-capabilities/summary.json).

## 1. Separate capability, configuration and observation

A **capability** is something the device supports. **Configuration** is what a
manager requests. **Observation** is what the device currently reports doing.
For example, observing two active BSSs proves neither that two is the maximum nor
that four are supported. Observing channel 36 says nothing about support for
channel 48. A current transmit-power setting is not the maximum EIRP allowed for
the radio in its current regulatory domain.

The ordinary `pod pod-1 capabilities` command describes EMOSA's available
**semantic operations**, such as changing an existing SSID. This new
`pod pod-1 radio-capabilities` command describes explicit **radio capability
inputs**. Keep those two questions separate when explaining a result.

The information flows through these existing building blocks:

1. A pod initiates its simulated OpenSync management connection to EMOSA.
2. The adapter reads a fresh monitored snapshot and validates the complete
   [bound radio/VIF topology](observed-topology.md).
3. A local input profile supplies reviewed capability claims and references to
   local evidence files. The adapter verifies their hashes and validity window.
4. The mapper checks the input against the same snapshot's identities, model,
   firmware, schema, country, active BSS count and operational channel.
5. The value codec produces diagnostic bytes for a later EasyMesh endpoint.

The mapper does not copy capabilities from another pod or derive limits from
the upstream schema. **Hash matching proves which input bytes were used; it does
not prove the claims in those bytes.** Our executable example supplies explicitly
invented simulation capacities. Physical claims still require qualification.

## 2. Run the complete demonstration

Complete manual chapters [3](team-manual.md#3-set-up-a-developer-checkout)
and [5](team-manual.md#5-build-and-exercise-the-real-ovsdb-simulator) first:
install the pinned Python environment and disposable OVSDB binaries.

On HOST, from the checkout root:

```bash
uv sync --locked
uv run python -m emosa_lab.simulation.radio_capabilities \
  --output .lab/radio-capabilities-demo
```

Choose a new output directory on every run. The program refuses to reuse one,
so a previous result cannot be overwritten accidentally. Expect `passed: true`
and `controller_onboarding_proven: false`. The adapter and databases stop on exit;
the retained configuration, profiles, evidence, journal and log remain private
inside your ignored output directory.

Inspect selected results:

```bash
uv run python - <<'PY'
import json
from pathlib import Path

root = Path('.lab/radio-capabilities-demo')
report = json.loads((root / 'report.json').read_text())
for pod in report['stages']['connected']:
    print(pod['pod_id'], 'ready:', pod['ready'])
    for radio in pod['radios']:
        print(radio['radio_id'], radio['decoded'], radio['value'])
for stage in ('profile_changed', 'evidence_changed', 'country_changed',
              'firmware_changed', 'disconnected'):
    value = report['stages'][stage]
    print(stage, 'ready:', value['ready'], 'blockers:', value['blockers'])
PY
```

Each simulated pod has two radios, three enabled AP BSSs and one station
interface. The first radio's declared capacity is **four** BSSs, although only
two are active; the second's capacity is **two**, although only one is active.
This deliberate difference demonstrates that the mapper is using explicit
capacity inputs. The first radio supports global class 115 in the fixture;
the second declares classes 81, 83 and 84. These are synthetic facts, not a
discovered hardware profile or evidence of RF behavior.

The demonstration checks:

| Stage | Expected observation and reason |
| --- | --- |
| `connected`, `cli_ready` | Both pods produce independently bound values; the real CLI exits 0 |
| `profile_changed`, `cli_blocked` | Even an extra space changes the pinned file digest; no radio values, CLI exit 5 |
| `evidence_changed` | The referenced artifact no longer matches its digest; the input is withdrawn |
| `country_changed` | An observed country change invalidates the regulatory context of the claims |
| `firmware_changed` | A capability claim for the previous firmware is no longer accepted |
| `other_pod_unchanged`, `other_pod_while_disconnected` | A fault or disconnect on pod 1 leaves pod 2 usable |
| `disconnected`, `reconnected` | No current values while disconnected; fresh observations restore the same mapped facts |
| `restarted` | A new adapter process verifies the same pinned inputs and stable identities again |

Only the fixture administrator introduces and restores these faults, against
databases it owns. The adapter's configuration is read-only throughout. The
final comparison verifies that monitored tables match their initial contents
after the fixture restores its own changes. It does not establish anything about
an unobserved physical database or all possible intermediate writes.

## 3. Understand the generated input and use the diagnostic

Open `.lab/radio-capabilities-demo/pod-1/capabilities.json`. It matches the
implemented [input schema](../../schemas/radio-capabilities.schema.json). Its
neighbor `synthetic-radio-contract.json` is the referenced evidence artifact.
The second pod has its own input and evidence, with different identity bindings.

| Input | Why it is required |
| --- | --- |
| `source_kind` | Only `synthetic_fixture` can produce values today; `physical_pod_draft` remains blocked |
| `pod_id`, `binding_sha256` | Bind claims to the complete persisted pod/radio/VIF identity allocation |
| `schema_fingerprint`, `device` | Prevent silent reuse across a different schema, model or firmware |
| `issued_at`, `expires_at` | Explicit UTC validity window; a future or expired input cannot be current evidence |
| `complete_operating_class_inventory` | Assert that all supported classes have been accounted for, not just a convenient subset |
| `evidence` | Named regular files, SHA-256 hashes, review notes and explicit coverage of device, regulatory domain, capacity and class inventory |
| Per-radio `radio_id`, `ruid`, `country` | Match stable identity and the fresh synthetic State country representation |
| `max_bss` | Explicit nonzero capacity, independent of active BSS count |
| `operating_classes` | Each class's maximum EIRP in signed integer dBm and every statically non-operable channel |
| `evidence_id` | Identify the artifact reviewed to support all of that radio's claims |

EasyMesh's non-operable list is an **exclusion list**: other channels in the
class are claimed supported. An empty list means all channels in that class,
not “unknown.” Temporary interference or a DFS waiting condition is not proof
that a channel is permanently impossible. Unknown values must remain unresolved;
do not enter zero, an empty list or a guessed capacity to make validation pass.

The per-pod adapter configuration references the profile using an **absolute
path** and its exact file SHA-256 under `radio_capabilities`. The generated
`adapter.json` demonstrates this without credentials. Evidence filenames are
plain basenames relative to the profile directory; path traversal, leaf symlinks,
FIFOs, directories and oversized files are rejected. The local limits are a
64 KiB profile, eight evidence files of at most 1 MiB each, eight radios and
32 declared classes per radio. These budgets are implementation policies.

For an already running, appropriately configured simulation service:

```bash
uv run emosa --socket /absolute/path/to/control.sock \
  pod pod-1 radio-capabilities --json
```

This command is read-only. It rereads the pinned files and obtains a fresh
snapshot on each request. Exit **0** means the selected diagnostic is ready;
exit **5** means its input/observation checks blocked it. An existing service
without `radio_capabilities` returns `capability_input_not_configured` and no
values. Merely adding the profile field to a JSON file does not reconfigure an
already running process: review the new profile and digest, update that pod's
configuration, then restart the adapter. Never change an identity binding to
silence a mismatch; the journal deliberately rejects implicit identity migration.

The automated demonstration has stopped its servers when it returns. Its
retained socket path is therefore not a live interactive service. To inspect
bytes afterwards, use the offline value command:

```bash
uv run emosa-lab payload --type 0x85 --value-hex 0200000140010401731400
```

This known synthetic example decodes to RUID `02:00:00:01:40:01`, capacity four,
class 115, EIRP 20 dBm and no excluded channels. The command does not connect to
a pod or validate the truth of that claim.

## 4. Know what has been implemented from the specifications

The sources are the accessible EasyMesh 6.1 and IEEE 802.11-2024 editions,
identified by content hashes in the [protocol matrix](../protocol/protocol-matrix.json).
The exact selected definitions are EasyMesh **§17.2.7, Table 30, pp.127–128** and
IEEE **Table E-4, pp.5658–5659**, together with EasyMesh §3.1.2 reserved-value rules.
No licensed text or page image is redistributed.

The [codec](../../src/emosa/easymesh_payloads.py) validates counts, lengths,
nonzero Max_BSS and signed power encoding. Its decoder retains unfamiliar class
identifiers for diagnostics. Its encoder and the mapper accept only reviewed
global classes **81, 82, 83, 84, 115, 116, 117 and 128**. An input containing another class is
blocked in full; the mapper never silently drops it. Extending this catalogue
requires source review and independent tests. Channel-set validation does not
establish regulatory permission, bandwidth support or HT/VHT/HE feature support.
In particular, Table E-4's class-81 channel-spacing entry is 25 MHz; this code does
not reinterpret that column as a 20 MHz bandwidth assertion.

The raw codec preserves structural order and duplicates; the input mapper adds
identity, completeness, unique-class/channel and supported-channel checks.
Current band/channel is checked for consistency when a radio is enabled; this
does not infer its current operating class or verify every claimed channel.

Reproduce the component and service checks:

```bash
uv run pytest tests/test_easymesh_payloads.py tests/test_radio_capabilities.py
uv run pytest tests/test_radio_capability_service.py
python3 scripts/check-easymesh-reference.py
```

The last command requires `tshark` and now checks twenty independently dissected values
from the existing synthetic native-peer capture. Frame 5 supplies a real native
Radio Basic Capabilities value. It is prplMesh traffic, not an EMOSA onboarding
exchange. The fixture expectations are extracted without importing EMOSA;
cross-checks supplement the specifications rather than replacing them.

## 5. Move from synthetic inputs toward an unchanged pod

Keep actual device inputs and evidence outside the repository, for example under
`~/.local/state/emosa/pod-qualification/`. Connection credentials remain in the
private mechanism described by the [qualification guide](pod-qualification.md).
The existing collector produces a draft, not a qualified capability profile.

For a named physical model/firmware, obtain authoritative or independently
verified capacity, complete operating-class/channel support and maximum EIRP for
the actual regulatory context. Record how each fact was established, including
antenna/EIRP interpretation and concurrent interface constraints. Verify the
actual pod's State/configuration representation rather than assuming that its
schema or country field equals this simulation reference. A non-TLS management
capture can help characterize that interface, but does not by itself establish
maximum radio limits or trusted identity.

The [profile-readiness audit](../protocol/profile-readiness.md) now inventories
mandatory-function gaps and selected feature conditions. Next, extend technology
capability inputs, complete that clause review, and resolve the native controller's recorded
Profile-2 Search/Profile-1 Response mismatch. Complete IEEE transport and
procedure validation still requires **IEEE 1905.1-2013 and IEEE 1905.1a-2014**.
The acceptance path remains **real EasyMesh messages → EMOSA adapter → unchanged
physical pod → independently observed behavior**. These simulation results are
preparation for that proof.

The optional [technology and Device Inventory extension](technology-inventory.md)
uses the same validated context. It has separate readiness results; Basic readiness
is unchanged. Class 128 uses center channels, so it cannot validate the observed
primary channel. The extension demo includes explicit 40/80 MHz class inputs.
