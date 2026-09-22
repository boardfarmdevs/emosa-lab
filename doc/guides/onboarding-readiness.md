# Check radio scope and native peer compatibility

These exercises close two preparation gaps for **EasyMesh to OpenSync
adaptation**. They determine whether the existing radio configuration fits our
narrow simulator mapping, and show what the selected native controller actually
sent. They do not yet join a controller message to an EMOSA operation.

The acceptance path remains **real EasyMesh messages → EMOSA adapter → unchanged
physical pod → independently observed behavior**. The exact IEEE 1905.1-2013 and
1905.1a-2014 texts and the physical-pod connection are still pending.

## 1. Understand why a radio needs a scope check

A BSS is one AP network/interface, with its own SSID and security configuration.
A radio can host several BSSs. OpenSync represents their membership using
`Wifi_Radio_Config.vif_configs`, with corresponding State references describing
the observed interfaces. A row existing in Config does not establish that its AP
is running.

Our original semantic exercise patches the SSID and one PSK of one existing
BSS. EasyMesh WSC provisioning can describe the **complete BSS set on a radio**.
If a controller asks for one BSS while a second old BSS remains, changing only
the selected SSID would leave an incomplete result. Removing the extra BSS also
cannot be assumed safe: it may carry backhaul or management traffic.

The optional `sole-fronthaul-radio` scope is an additional admission check for
`ovsdb-sim`. It requires an explicitly bound simulated pod, exactly one enabled
AP BSS on the selected radio, matching Config/State references, and the supported
single WPA2-PSK/CCMP credential representation in both Config and State. It rejects
orphaned or shared references anywhere in the monitored database. A fully separate
second radio is allowed and remains untouched.

The pinned OpenSync schema represents `multi_ap` as a **string enum**. This
fixture uses explicit `"none"`: an ordinary AP without native Multi-AP roles.
EMOSA supplies the virtual-agent representation externally. An absent value
means the role is unestablished for this check; values such as `backhaul_bss`
also fail this narrow scope. Do not change a physical pod's role to make the
check pass. The fixture is not a qualified physical profile.

The checker first assesses a fresh snapshot. At commit, OVSDB `wait` operations
guard the complete radio/VIF graph, including row UUIDs and membership. An extra
BSS inserted after planning, a changed role, an extra State credential, or a row
replaced with an identical-looking row aborts the whole transaction. This guards
against an intervening database change; it does not establish exclusive physical
manager ownership or protect against a writer changing things after the commit.

## 2. Inspect a running simulated pod without changing it

Run on **HOST**, with the development installation from manual chapter 3 and the
database prerequisites from chapter 5. Start the three-terminal
[connecting-pod exercise](connecting-pod.md#3-operate-the-demonstration-interactively)
first. Keep the fixture and `emosa serve` running. In its command terminal use
the socket path printed by that exercise; for its documented directory:

```bash
uv run emosa --socket .lab/connecting-pod-live-01/control.sock \
  pod pod-1 radio-scope --json
```

This issues the read-only local API method `radio.scope`. It retrieves fresh
OVSDB observations and returns bindings and blocker codes, without returning
SSIDs, keys or credential fingerprints. It does not submit an operation.

| Field | How to interpret it |
| --- | --- |
| `topology_candidate` | The observed membership, AP role and State relationships satisfy these narrow structural checks. |
| `credential_layout` | Whether a single supported WPA2-PSK layout was observed, was unsupported, or was not collected. It reveals no key. |
| `synthetic_mapping_candidate` | All these checks, including the configured synthetic serial, passed. This is eligibility for the simulation scope only. |
| `blockers` | Concrete reasons the scope is not established. Investigate them rather than deleting rows to obtain a positive flag. |
| `mapping_scope` | The configured backend policy: default `existing-bss`, or optional `sole-fronthaul-radio`. Inspection alone does not switch it. |
| `physical_qualification`, `wire_admission` | Both remain false, including for a positive simulation result. |

The default connecting-pod fixture intentionally has an extra synthetic guest
credential and no explicit `multi_ap` role. Expect blockers for those conditions.
Its original one-BSS semantic patch remains usable under `existing-bss`; that
exercise tests preserving unrelated credentials. A blocked scope result is an
expected teaching example, not a broken database connection.

For a custom **simulation** configuration, the implemented loader accepts
`"mapping_scope": "sole-fronthaul-radio"` inside the relevant `pods` entry. It
requires `backend_mode: "ovsdb-sim"` and the existing `virtual_agent` binding
with explicit `al_mac` and `expected_serial`. Selecting the policy does not repair
or populate the database. A fixture must independently supply compatible rows.
The automated radio exercise below generates both inputs consistently.

## 3. Exercise the stricter scope with independent clients

Use [service integration §2–3](service-integration.md#2-restore-owned-radios-only-if-the-vm-rebooted).
That guide explains the owned LXD VM, containers, hwsim radios, staging and
preflight. Run `stage.py` from HOST before executing the VM's `run-service.py`;
an old staged copy will not include this check.

The generated adapter configuration now selects `sole-fronthaul-radio`. In the
new run's `result.json`, inspect `cases.radio_scope` after `pod_connected`.
Expect no blockers, a positive synthetic candidate, and false wire/physical flags.
The harness then performs the semantic change, independently observes the AP
and wired/Wi-Fi clients, withholds the next Config application, kills EMOSA,
and verifies recovery with one write attempt. There are now **15 checkpoints**.

The [retained scope result](../evidence/onboarding-readiness/summary.json) passed
this exercise. Its `radio_scope`, operation IDs, client checks, lifecycle and
private artifact hashes connect the scope check to that actual service run.
The initiating interface is still the local semantic API. The native capture
review below is a separate experiment, even though both appear in this guide.

For a development regression without LXD or radios, run on HOST:

```bash
uv run pytest tests/test_radio_scope.py -q
```

This requires the real disposable OVSDB prerequisites from chapter 5. It covers
preflight rejection, concurrent scope changes, successful manager application,
and preservation of a separate radio. It never connects to a physical pod.

## 4. Review the native controller's retained messages offline

This command reads a completed packet capture using an installed `tshark`.
It generates no traffic, forwards no packets and decrypts no credentials.
It applies selected WFA inclusion/profile checks from the
[procedure audit](../protocol/procedure-audit.md) to the dissector's observations.
The dissector is an independent implementation, not the normative specification.

Run on HOST from the repository root. The tested tshark versions are 3.6.2 and
4.2.2; both must provide the listed fields. This exercise needs no LXD or running
controller. Check the executable, then choose a new private result directory:

```bash
tshark --version
emosa_review="$HOME/.local/state/emosa/peer-review/wired-$(date -u +%Y%m%d-%H%M%S)"
uv run python -m emosa.evaluation.peer_capture \
  --capture doc/evidence/peer-baseline/samples/wired/ethernet.pcap \
  --output "$emosa_review" \
  --controller-al 02:00:00:e0:00:01 \
  --agent-al 02:00:00:e0:00:02
cat "$emosa_review/review.json"
```

For wireless-backhaul evidence, repeat with the `samples/wireless/ethernet.pcap`
path and a new output directory. These are previously reviewed synthetic native
peer captures, not physical OpenSync traffic. The selected lab peers use their
AL addresses as Ethernet source addresses on this link. For another link or a
relayed capture, that identity relationship needs separate qualification.

Output contains `review.json` and `frames.tsv`. The latter contains only an
explicit list of message types, TLV types, WPS message types, profile values,
addresses, lengths and timestamps. The report records the capture hash, selected
fields, projection hash, tshark version and executable hash. Keep even these
metadata private for real devices; they can identify a network. The temporary
raw capture snapshot is removed after analysis. The input file is unchanged.

The local analysis budgets are 64 MiB per capture, 20,000 projected rows, 16 MiB
per tool invocation's combined output, and 30 seconds per tool invocation.
Oversize, changed input, unsupported fields or invalid projection produce an
analysis failure, not an empty successful result. `failure.json` retains a safe
error category after output-directory creation. Exit code 2 means analysis failed;
exit code 0 means a report was produced, **including when it contains findings**.

Search/Response pairing uses matching MID and selected peers within five seconds,
with ambiguous matches unassessed. Five seconds is a local review window, not an
IEEE timer. Partial/truncated/malformed messages are unassessed; reassembly relies
on the recorded tshark version. The tool does not claim to validate complete
messages, profile conformance, authentication, replay protection, radio binding
or controller-to-operation causality.

## 5. Interpret the actual findings and act on them

Both retained native captures produce `review_required`, with the same results
under the two tested dissectors:

| Observation | Meaning for the next onboarding trial |
| --- | --- |
| Frame 1 Search advertises Profile 2; frame 2 Response advertises Profile 1. | EasyMesh 6.1 §6.1 p.63 calls for the corresponding response profile. Resolve the selected peer/build/profile intersection; its earlier successful native baseline is not proof of 6.1 compatibility. This observation does not establish that a future Profile-1 EMOSA trial would fail. |
| The controller's reassembled WSC message contains two M2 payloads plus a WPS message of type 12 (M8). | This is larger than the single-M2 request our proposed one-BSS mapping could consider. The analyzer does not interpret encrypted roles/credentials or companion configuration. Never pick one M2 and silently discard the rest. |
| M1 advertises Max_BSS 2. | This is an advertisement from the native peer, not measured capacity of an OpenSync pod. Counting one current BSS does not establish a pod's maximum capability. |

The M1/M2 observations are frames 5/7 in the wired sample and 6/8 in the wireless
sample; the preceding WSC fragment is unassessed on its own. Read both the
finding and the report's limitations before presenting a verdict.

The next wire trial needs a named compatible controller policy/build and an
authenticated **complete** request matching the qualified radio scope. It also
needs the complete reviewed procedure, exchange lifecycle and independent
validation. The [IEEE envelope component](../protocol/ieee1905-envelope.md) now
uses both acquired IEEE documents and has native-capture boundary vectors. A scope candidate or a payload count cannot enable that path.

## 6. Apply the same questions to a physical pod without writing it

The existing [read-only qualification command](pod-qualification.md) now adds
`radio_scope_candidates` to its draft profile. It collects structural candidates
from the actual radio/VIF inventory. It deliberately does not collect credential
maps, so `credential_layout` is `not_collected` and synthetic eligibility remains
false, even if structural checks pass. The draft stays `writable: false`.

The operator still needs to populate the private connection file outside the
repository and provide only its absolute path. No such connection has been
supplied. Manager behavior, writer controls, management/backhaul dependencies,
physical radio capabilities, recovery and independent physical client checks
remain qualification work. No physical pod was changed by this increment.
