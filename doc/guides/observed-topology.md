# Stable identities and complete observed radio/BSS topology

This exercise connects the EasyMesh Operational BSS **value component** to a real
OVSDB observation through the running adapter. It adds stable explicit radio and
interface bindings, a complete radio/VIF graph check and a read-only diagnostic
command. It runs on **HOST**, without a VM, wireless interface or physical pod.

The evidence remains **OVSDB simulation**. The value is not sent to a controller.
No IEEE 1905 message endpoint, full Topology Response, physical neighbor discovery
or device capability advertisement is enabled. The acceptance objective remains
real EasyMesh messages → EMOSA adapter → unchanged physical pod → independently
observed behavior.

## 1. Understand what is being bound

Several identifiers coexist, and they have different lifetimes:

| Identifier | Meaning and source | Behavior here |
| --- | --- | --- |
| `pod_id` | Operator's logical pod name | Separates pods even when their radio/VIF names and database names are identical |
| AL MAC | Configured synthetic virtual-agent address | Identifies the represented agent; not automatically the pod's hardware address |
| `radio_id` | Stable logical radio name | Independent of the database's row UUID |
| RUID | Explicit six-octet identifier for that represented radio | Configured locally administered unicast address; the mapping to observed radio MAC is retained |
| `interface_id` | Stable logical VIF name; also `bss_id` for an AP | Identifies the resource across row recreation; a station interface is not called a BSS |
| `expected_mac` | Explicit expected radio or VIF MAC in the synthetic fixture | Must match observed State; changes block projection rather than silently rebinding |
| OVSDB UUID | Database row identifier used in references | Rebuilt from each snapshot; never used as a stable logical ID or derived RUID |
| `if_name` | Explicit interface name used to locate the configured resource | Cross-checked with State and UUID relationships; naming order/suffix does not imply ownership |

The RUID-to-radio and BSS-to-interface mappings are visible in the report. Reverse
lookup is unambiguous because bindings require unique radio IDs, interface IDs,
names and corresponding addresses in their scopes. A radio's actual MAC may
equal its AP's MAC; those represent different roles. RUIDs and AL addresses
cannot collide. Expected radio MACs and expected interface MACs each must be
unique across represented pods. Addresses are supplied explicitly, not allocated
from an IP, serial hash or transient database UUID.

The adapter pins the configured binding in its local SQLite journal on startup,
before opening pod sessions. The record includes the pod ID, AL MAC, expected
serial, radio IDs/RUIDs, interface IDs/names/modes and expected MACs. The serial
is a simulation cross-check, **not authentication or physical attestation**.
The existing hardware qualification/trust gate still applies to real pods.

## 2. Run the automated two-pod experiment

Complete [manual chapters 3–5](team-manual.md#3-set-up-a-developer-checkout) first:
the pinned Python/uv environment and disposable OVSDB binaries must be available.
Use a new output directory on each run:

```bash
uv run python -m emosa.simulation.topology --output .lab/topology-demo
```

The command starts two owned databases, separate manager simulators and one real
adapter child process. Each database initiates its connection to the adapter.
The adapter's `write_mode` is **read-only throughout**. The test fixture changes
its own disposable database to exercise observation behavior; these fixture
mutations are not adapter operations or physical-pod writes.

Expect a JSON result with `passed: true` and a path to `report.json`. Read a
compact selection of its stages:

```bash
uv run python - <<'PY'
import json
from pathlib import Path

result = json.loads(Path(".lab/topology-demo/report.json").read_text())
stages = result["stages"]
for pod in stages["connected"]:
    print(pod["pod_id"], pod["binding_sha256"], pod["ready"])
    for radio in pod["radios"]:
        print(" ", radio["radio_id"], radio["ruid"], radio["observed_mac"])
        for interface in radio["interfaces"]:
            print("   ", interface["interface_id"], interface["mode"], interface["enabled"])
print("Unbound interface:", stages["unexpected_interface"]["blockers"])
print("Recreated rows:", stages["uuid_recreation"])
print("CLI exit codes:", stages["cli_ready"]["exit_code"], stages["cli_blocked"]["exit_code"])
PY
```

Each pod has two radios and four VIFs: three AP BSSs and one station interface.
The AP value contains two BSSs on the first radio and one on the second. The
station stays in the normalized inventory but is excluded from that AP list.
Its `enabled` State is a synthetic observation, not evidence of Wi-Fi association
or a measured backhaul link. Both pods deliberately reuse logical names such as
`radio-1` and `bss-1`; their pod-scoped bindings and addresses remain distinct.

| Stage | Expected result | Why it matters |
| --- | --- | --- |
| `connected` / `cli_ready` | Complete reports; CLI exits 0 | The public API and CLI read the actual service's monitored snapshots |
| `config_only` | Operational BSS value unchanged | Desired configuration is not reported as applied State |
| `manager_applied` | Value changes after separate manager publishes State | Observation follows the device-side simulation boundary |
| `other_pod_unchanged` | Other pod's value stays the same | Resource names do not cause cross-pod mixing |
| `unexpected_interface` / `cli_blocked` | No payload; CLI exits 5 | An unbound interface cannot disappear from a purported complete report |
| `disconnected` | No cached payload returned as current | Unavailable observations cannot produce current advertisements |
| `uuid_recreation` | All 12 radio/VIF row UUIDs replaced | Logical IDs depend on bindings and revalidated references, not row persistence |
| `reconnected` | Same IDs and observed value; newer session generation | Fresh synchronization rebuilds references |
| `restarted` | Same binding digest/IDs/value after SIGKILL; new adapter instance ID | Local durable identity survives a real process restart |

On exit the fixture stops its child processes and removes its owned temporary
databases/sockets. The output directory retains the configuration, local journal,
private adapter log and report. Inspect those privately; do not publish a whole
run directory. Synthetic keys are excluded from the report.

## 3. Inspect topology interactively

For a live CLI session use the existing single-radio connecting-pod fixture.
Start a **new** exercise in terminal A:

```bash
uv run python -m emosa.simulation.connecting_pod --directory .lab/topology-live
```

Wait for `Fixture ready` and leave it running. Before starting the adapter, use
terminal B to add the [credentials-free binding example](../../examples/topology/single-radio.json)
to this generated configuration:

```bash
uv run python - <<'PY'
import json
from pathlib import Path
from emosa.config import load

path = Path(".lab/topology-live/adapter.json")
config = json.loads(path.read_text())
config["write_mode"] = "read-only"
config["pods"][0]["virtual_agent"]["topology"] = json.loads(
    Path("examples/topology/single-radio.json").read_text()
)
path.write_text(json.dumps(config, indent=2) + "\n")
load("config", path)
PY
uv run emosa serve --config .lab/topology-live/adapter.json
```

These MACs and names match this specific synthetic fixture. This file is not a
physical-pod profile. For other simulations, configure every radio and VIF with
explicit matching IDs, modes and expected MACs before starting the service.

In terminal C:

```bash
emosa_socket="$PWD/.lab/topology-live/control.sock"
uv run emosa --socket "$emosa_socket" pod pod-1 topology --json
```

Initial synchronization can yield `ready: false`; repeat once the pod has
connected. A successful report includes one radio, one enabled AP and the
`operational_bss_value` containing `type: 0x83`, hexadecimal value bytes and a hash.
The logical BSS ID stays `bss-1`; its observed MAC and SSID come from State.

Exercise availability using only this owned fixture's marker:

```bash
touch .lab/topology-live/disconnect
uv run emosa --socket "$emosa_socket" pod pod-1 topology --json
rm .lab/topology-live/disconnect
uv run emosa --socket "$emosa_socket" pod pod-1 topology --json
```

Allow the fixture's loop and the OVSDB session to observe each transition; repeat
the query if needed. Disconnect returns an unavailable report with no value and
exit code 5; reconnection eventually returns the same binding digest and IDs with
a new generation. `generation` is local to one process's session. Use
`adapter_instance_id` with it across process restarts.

Stop terminal B with Ctrl-C, then terminal A. Do not delete the journal as a way
to get past an unexpected identity error. Retain it to understand the mismatch.

## 4. Know the completeness and persistence rules

The checker considers **all rows** in Radio Config/State and VIF Config/State
from one monitored snapshot. Every configured radio/VIF must be bound. Every
Config resource must have exactly one corresponding State row; its State name,
mode and MAC must agree with the explicit binding. Radio Config and Radio State
memberships must both agree with the full expected graph. Extra, missing,
orphaned, shared or ambiguous resources block the entire projection.

Config is used only for current reference relationships and explicit resource
location. `enabled`, SSID, radio MAC, VIF MAC, band and channel come from State.
Unknown channel/band values remain null. An enabled VIF on an observed disabled
radio is inconsistent and blocks projection. Disabled APs remain visible in the
report but are omitted from the operational BSS list; a known radio with no
enabled APs has an explicit zero BSS count.

This synthetic mapping encodes an enabled AP's nonempty OVSDB SSID string as
UTF-8, bounded to 32 bytes. That is a stated local representation policy, not a
claim that every physical OpenSync build or IEEE SSID uses UTF-8. The standalone
codec continues to support opaque SSID bytes. Nonrepresentable/unknown State
blocks projection; Config never supplies a substitute SSID. MLD interpretation,
client association times, actual capabilities, physical links and protocol
adjacency remain unqualified or unknown. Observed BSS count is **not Max_BSS**.

The binding record is versioned local journal metadata and contains no credential
or SSID. Endpoint changes and row UUID recreation do not change identity.
Reordering equivalent configuration lists is harmless. Changing the pod's AL
address, serial, radio/interface identity, names, modes or expected MACs—or
removing a previously pinned topology from a still-configured pod—fails startup
with `PRECONDITION_FAILED`. The writer lock is released on that rejection.
Removed pods' assignments stay reserved against accidental reuse.

An identity migration/retirement command is **not implemented**. Review a mismatch
and restore the intended configuration; plan a deliberate migration when the
actual topology changes. Use a new private state directory for a genuinely new,
isolated experiment, preserving the old evidence. Never erase an active operation
journal or repin an unexpected physical identity to make a check pass.

Current local budgets are 8 radios and 16 interfaces per radio, at most 64
interfaces per pod, and 32 retained pod bindings per state directory. They bound
the experiment; they are not normative EasyMesh capacity limits or measured
production capacity. The usual local API response-size limit also applies; no
partial topology is returned to fit a smaller response budget.

`pod topology` is a diagnostic query and grants no write authority. The existing
semantic operation remains scoped to its designated BSS, even if this report
contains several APs. `pod radio-scope` separately checks the narrow full-radio
actuation candidate. Hardware mode and actual EasyMesh write admission remain
gated. Existing configurations without the optional topology binding still work;
their topology query explicitly reports `topology_binding_not_configured`.

## 5. Reproduce validation and move toward the proof

```bash
uv run pytest tests/test_topology.py tests/test_topology_service.py
```

The unit cases exercise complete and malformed graphs, State-only reporting,
identity drift, count/binding budgets, persistence and fail-closed startup.
The service case runs the same two-pod experiment through real OVSDB, the public
API and CLI, including disconnect, UUID recreation and process death. See the
[reviewed evidence](../evidence/observed-topology/summary.json).

The [radio-capability input exercise](radio-capabilities.md) now adds explicit
synthetic limits and field mapping, with unsupported/unknown data blocked and
without deriving capacity or classes from current channel or BSS count. Actual
capability qualification and the complete profile audit remain open.
Actual controller discovery/topology/WSC processing
still needs IEEE 1905.1-2013 and 1905.1a-2014 review, independent full-message
vectors and trusted peer/exchange/radio binding. Physical proof still needs the
operator's local connection configuration and pod qualification.
