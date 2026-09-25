"""Conformance vectors for the EMOSA adapter specification (spec/conformance).

Every vector is computed by the reference implementation (package ``emosa``)
from recorded inputs. ``generate`` writes them; ``check`` recomputes and
compares. Another implementation runs the same JSON files through its own
harness: the files, not this module, are the contract.

    python -m emosa_lab.conformance generate [DIR]
    python -m emosa_lab.conformance check [DIR]
"""

import argparse
import asyncio
import copy
import dataclasses
import json
import sys
import tempfile
from pathlib import Path

from emosa.agent.fleet import Fleet, agent_config, derive_al
from emosa.easymesh_payloads import encode_value
from emosa.errors import EmosaError
from emosa.model import ACTIVE, Intent
from emosa.opensync.easymesh_view import (
    backhaul,
    device_view,
    inventory,
    radio_capabilities,
    topology,
)
from emosa.opensync.pod_profile import PodBackend
from emosa.opensync.profiles import DEFAULT
from emosa.opensync.schema import Schema, reference_path
from emosa.opensync.uplink import UplinkBackend, UplinkIntent, uplink_state
from emosa.operations import TRANSITIONS
from emosa.secrets import SecretStore
from emosa.wire.autoconfiguration import EASYMESH_61, R1, PeerBinding
from emosa.wire.reports import _topology, capability_tlvs

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DIR = ROOT / "spec" / "conformance"
POD_ROWS = ROOT / "tests" / "fixtures" / "opensync" / "pod-6.6.1-hwsim-tables.json"
UPLINK_ROWS = {  # recorded: a pod on its bootstrap GRE uplink, and one on a Multi-AP backhaul
    kind: ROOT / "tests" / "fixtures" / "opensync" / f"pod-6.6.1-hwsim-uplink-{kind}.json"
    for kind in ("gre", "multi-ap")
}
SERIAL = "MVXPOD023F87E628DD"
RUID = "02:00:00:00:01:00"
AGENT = "02:72:f9:7f:07:85"
CONTROLLER = "02:00:00:e0:00:01"
PASSPHRASES = {  # public test values; a real passphrase never appears in a vector
    "ref-primary": "conformance-psk-1",
    "ref-extra-1": "conformance-psk-2",
    "ref-extra-2": "conformance-psk-3",
    "ref-extra-3": "conformance-psk-4",
}


def mac(text):
    return bytes.fromhex(text.replace(":", ""))


def plain(value):
    """JSON form of the reference's values: bytes as hex, dataclasses as objects."""
    if isinstance(value, bytes):
        return value.hex(":") if len(value) == 6 else value.hex()
    if dataclasses.is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value) if f.repr}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    return value


def tlvs(items):
    return [{"type": f"0x{t.kind:02x}", "value": t.value.hex()} for t in items]


# -- pod rows ---------------------------------------------------------------------


def pod_rows():
    return json.loads(POD_ROWS.read_text())["tables"]


def with_backhaul_slot(raw):
    """The recorded pod plus a b-ap-24 backhaul BSS, as a multi-BSS set creates it."""
    raw = copy.deepcopy(raw)
    config_uuid = "00000000-0000-4000-8000-0000000000b1"
    state_uuid = "00000000-0000-4000-8000-0000000000b2"
    home = next(r for r in raw["Wifi_VIF_Config"].values() if r["if_name"] == "home-ap-24")
    raw["Wifi_VIF_Config"][config_uuid] = {**home, "if_name": "b-ap-24", "multi_ap": "backhaul_bss"}
    state = next(r for r in raw["Wifi_VIF_State"].values() if r["if_name"] == "home-ap-24")
    raw["Wifi_VIF_State"][state_uuid] = {
        **state,
        "if_name": "b-ap-24",
        "vif_config": ["uuid", config_uuid],
        "mac": "82:00:00:00:01:01",
        "ssid": "emosa-mesh-bh",
        "multi_ap": "backhaul_bss",
        "associated_clients": ["set", []],
    }
    for table, uuid, column in (
        ("Wifi_Radio_Config", config_uuid, "vif_configs"),
        ("Wifi_Radio_State", state_uuid, "vif_states"),
    ):
        radio = next(r for r in raw[table].values() if r["freq_band"] == "2.4G")
        present = radio[column][1] if radio[column][0] == "set" else [radio[column]]
        radio[column] = ["set", [*present, ["uuid", uuid]]]
    return raw


def cold(raw):
    """The recorded pod after a restart: radios only, no fronthaul VIF yet."""
    raw = copy.deepcopy(raw)
    home = [u for u, r in raw["Wifi_VIF_Config"].items() if r["if_name"] == "home-ap-24"]
    states = [u for u, r in raw["Wifi_VIF_State"].items() if r["if_name"] == "home-ap-24"]
    for u in home:
        del raw["Wifi_VIF_Config"][u]
    for u in states:
        del raw["Wifi_VIF_State"][u]
    for table, column, gone in (
        ("Wifi_Radio_Config", "vif_configs", home),
        ("Wifi_Radio_State", "vif_states", states),
    ):
        for radio in raw[table].values():
            refs = radio[column][1] if radio[column][0] == "set" else [radio[column]]
            radio[column] = ["set", [r for r in refs if r[1] not in gone]]
    return raw


POD_CASES = {
    "one-fronthaul": pod_rows,
    "fronthaul-and-backhaul": lambda: with_backhaul_slot(pod_rows()),
    "cold-pod": lambda: cold(pod_rows()),
}


def decode(raw):
    schema = Schema(json.loads(reference_path().read_text()))
    return {t: {u: schema.row(t, r) for u, r in rows.items()} for t, rows in raw.items()}


# -- vector sets --------------------------------------------------------------------


def al_mac_vectors():
    cases = []
    for serial, taken in (
        ("MVXPOD023F87E628DD", []),
        ("MVXPOD02D7777EF0D9", []),
        ("MVXPOD02288DCB5DCC", []),
        ("A", []),
        ("serial.with-punctuation_1", []),
        ("MVXPOD023F87E628DD", [derive_al("MVXPOD023F87E628DD")]),
    ):
        cases.append({"serial": serial, "taken": taken, "expected": derive_al(serial, set(taken))})
    return {"description": "spec §2.2: AL MAC from the pod serial", "cases": cases}


def transition_vectors():
    return {
        "description": "spec §5: legal operation state transitions and the active set",
        "active": sorted(str(s) for s in ACTIVE),
        "transitions": {
            str(state): sorted(str(t) for t in targets) for state, targets in TRANSITIONS.items()
        },
    }


def northbound_case(name, raw, ages):
    view = device_view(decode(raw))
    radio = view.radio(mac(RUID))
    channel = radio.channel if radio.bsses else 6
    caps = radio_capabilities(radio, channel=channel, max_bss=5, max_eirp=30)
    facts = topology(
        agent_al=mac(AGENT),
        controller_al=mac(CONTROLLER),
        radio=radio,
        channel=channel,
        bsses=radio.bsses,
        ages={mac(m): s for m, s in ages.items()},
    )
    binding = PeerBinding("conformance", 1, mac(AGENT), mac(CONTROLLER), (mac(CONTROLLER),))
    inv = inventory(view, radio, b"mac80211_hwsim")
    return {
        "name": name,
        "ovsdb_tables": raw,
        "agent": {
            "al_mac": AGENT,
            "controller_al": CONTROLLER,
            "radio": RUID,
            "channel": channel,
            "max_bss": 5,
            "max_eirp": 30,
            "station_ages": ages,
            "chipset": "mac80211_hwsim",
        },
        "expected": {
            "device_view": plain(view),
            "capability_tlvs": tlvs(capability_tlvs(caps)),
            "inventory_tlv": {"type": f"0x{inv.kind:02x}", "value": encode_value(inv).hex()},
            "topology_tlvs": {
                EASYMESH_61: tlvs(_topology(facts, binding, EASYMESH_61)),
                R1: tlvs(_topology(facts, binding, R1)),
            },
        },
    }


def northbound_vectors():
    ages = {"02:00:00:00:0a:00": 12, "02:00:00:00:10:00": 70000}
    return {
        "description": "spec §2.6, §3.3: pod OVSDB rows (raw RFC 7047 JSON) to the device view, "
        "and the view to 1905 TLVs. 'value' is the TLV value in hex, without type and length.",
        "cases": [northbound_case(name, make(), ages) for name, make in POD_CASES.items()],
    }


class Recorder:
    """An OVSDB session over fixed rows that records every transaction."""

    def __init__(self, raw):
        self.schema = Schema(json.loads(reference_path().read_text()))
        self.tables, self.sent = raw, []

    async def snapshot(self):
        return {
            "tables": copy.deepcopy(self.tables),
            "generation": 1,
            "revision": 1,
            "ready": True,
            "schema": self.schema,
        }

    async def transact(self, operations, transaction_id=None, generation=None, **_):
        self.sent.append(copy.deepcopy(operations))
        replies = {
            "wait": {},
            "select": {"rows": []},
            "insert": {"uuid": ["uuid", "00000000-0000-4000-8000-0000000000ff"]},
        }
        return [replies.get(op["op"], {"count": 1}) for op in operations]


def southbound_case(name, raw, intent, *, multi_bss):
    with tempfile.TemporaryDirectory() as directory:
        vault = SecretStore(Path(directory) / "secrets")
        for ref, passphrase in PASSPHRASES.items():
            vault.write_simulated(ref, passphrase)
        session = Recorder(copy.deepcopy(raw))
        backend = PodBackend("pod-1", session, vault, serial=SERIAL, multi_bss=multi_bss)

        async def run():
            await backend.snapshot()
            value = Intent(
                "pod-1",
                "radio-1",
                "bss-1",
                intent["ssid"],
                intent["secret_ref"],
                additional=tuple(intent["additional"]) if intent.get("additional") else None,
            )
            await backend.plan(value)
            return await backend.submit(
                value,
                {
                    "attempt_id": "a",
                    "transaction_id": "t",
                    "session_generation": 1,
                    "prepared_at": "",
                },
            )

        result = asyncio.run(run())
    return {
        "name": name,
        "ovsdb_tables": raw,
        "profile": DEFAULT,
        "multi_bss": multi_bss,
        "intent": intent,
        "passphrases": PASSPHRASES,
        "expected": {"status": result.status, "transactions": session.sent},
    }


def southbound_vectors():
    rdk_set = [
        {"role": "fronthaul", "ssid": "emosa-iot", "secret_ref": "ref-extra-1"},
        {"role": "fronthaul", "ssid": "emosa-guest", "secret_ref": "ref-extra-2"},
        {"role": "backhaul", "ssid": "emosa-bh", "secret_ref": "ref-extra-3"},
    ]
    primary = {"ssid": "emosa-mesh-2", "secret_ref": "ref-primary"}
    return {
        "description": "spec §3.2, §3.4: an accepted M2 intent against pod rows, and the "
        "exact OVSDB transaction(s) sent. Passphrases are public test values by reference.",
        "cases": [
            southbound_case("update-fronthaul", pod_rows(), primary, multi_bss=False),
            southbound_case("cold-pod-create", cold(pod_rows()), primary, multi_bss=False),
            southbound_case(
                "multi-bss-set",
                pod_rows(),
                {**primary, "additional": rdk_set},
                multi_bss=True,
            ),
        ],
    }


def uplink_state_case(name, raw, station):
    rows = decode(raw)
    state = uplink_state(rows, station)
    view = device_view(rows)
    case = {
        "name": name,
        "ovsdb_tables": raw,
        "station": station,
        "expected": {
            "uplink": {k: v for k, v in state.items() if k != "state"},
            "backhaul": None,
        },
    }
    link = backhaul(view, station) if state["kind"] == "multi-ap" else None
    if link is not None:
        radio = view.radio(link.ruid)
        facts = topology(
            agent_al=mac(AGENT),
            controller_al=mac(CONTROLLER),
            radio=radio,
            channel=radio.channel,
            bsses=radio.bsses,
            ages={m: 0 for b in radio.bsses for m in b.stations},
            uplink=link,
        )
        binding = PeerBinding("conformance", 1, mac(AGENT), mac(CONTROLLER), (mac(CONTROLLER),))
        case["expected"]["backhaul"] = {
            "view": plain(link),
            "topology_tlvs": tlvs(_topology(facts, binding, EASYMESH_61)),
            "backhaul_sta_capability_tlvs": [
                {"type": "0xcb", "value": (ruid + b"\x80" + sta).hex()}
                for ruid, sta in facts.backhaul_stations
            ],
        }
    return case


def uplink_switch_case(name, raw, station, ssid, bssid):
    serial = next(iter(raw["AWLAN_Node"].values()))["serial_number"]
    with tempfile.TemporaryDirectory() as directory:
        vault = SecretStore(Path(directory) / "secrets")
        for ref, passphrase in PASSPHRASES.items():
            vault.write_simulated(ref, passphrase)
        session = Recorder(copy.deepcopy(raw))
        backend = UplinkBackend("pod-1", session, vault, serial=serial, station=station)
        intent = UplinkIntent("pod-1", station, ssid, "ref-primary", bssid=bssid)

        async def run():
            await backend.snapshot()
            try:
                await backend.plan(intent)
            except EmosaError as exc:
                return exc.code.value, str(exc)
            attempt = {"attempt_id": "a", "transaction_id": "t", "session_generation": 1}
            return (await backend.submit(intent, attempt)).status, None

        status, refusal = asyncio.run(run())
    expected = {"status": status, "transactions": session.sent}
    if refusal:
        expected["refusal"] = refusal
    return {
        "name": name,
        "ovsdb_tables": raw,
        "intent": intent.record(),
        "passphrases": PASSPHRASES,
        "expected": expected,
    }


def uplink_vectors():
    gre = json.loads(UPLINK_ROWS["gre"].read_text())["tables"]
    multi_ap = json.loads(UPLINK_ROWS["multi-ap"].read_text())["tables"]
    return {
        "description": "spec §8.3: data plane option 1. The pod's uplink as cm and owm report it "
        "(recorded rows), the backhaul the agent then reports (1905 TLVs, 'value' in hex "
        "without type and length), and the exact OVSDB transaction of the switch.",
        "states": [
            uplink_state_case("bootstrap-gre", gre, "bhaul-sta-50"),
            uplink_state_case("multi-ap-backhaul", multi_ap, "bhaul-sta-24"),
            uplink_state_case("multi-ap-other-station", multi_ap, "bhaul-sta-50"),
        ],
        "switch": [
            uplink_switch_case(
                "switch-from-gre", gre, "bhaul-sta-50", "emosa-mesh-bh", "02:00:00:00:09:00"
            ),
            # one of the pod's own BSSes (the fixture's home-ap-24): a br-home loop
            uplink_switch_case(
                "refused-own-bssid", gre, "bhaul-sta-50", "emosa-mesh-bh", "82:00:00:00:01:00"
            ),
        ],
    }


def fleet_vectors():
    cases = []
    config = {
        "listen": "punix:/run/emosa-fleet.sock",
        "advertise": "10.101.0.1",
        "ports": [6651, 6690],
        "controller_al": CONTROLLER,
        "state_root": "/var/lib/emosa",
        "config_dir": "/etc/emosa",
        "message_set": EASYMESH_61,
    }
    arrivals = [
        {"id": "MVXPOD023F87E628DD", "serial_number": "MVXPOD023F87E628DD", "model": "HWSIM_POD"},
        {"id": "MVXPOD02D7777EF0D9", "serial_number": "MVXPOD02D7777EF0D9", "model": "HWSIM_POD"},
        {"id": "MVXPOD023F87E628DD", "serial_number": "MVXPOD023F87E628DD", "model": "HWSIM_POD"},
    ]
    with tempfile.TemporaryDirectory() as directory:
        live = {**config, "state_root": directory, "config_dir": directory}
        fleet = Fleet(live, starter=lambda *_: None, stopper=lambda *_: None)
        for row in arrivals:
            updates = []

            def call(conn, method, params, deadline, row=row, updates=updates):
                if params[1]["op"] == "select":
                    return [{"rows": [row]}]
                updates.append(params[1]["row"])
                return [{"count": 1}]

            fleet._call = call
            entry = fleet.handle(None, "conformance")
            stable = {k: entry[k] for k in ("pod_id", "al_mac", "port", "interface", "handovers")}
            cases.append(
                {
                    "awlan_node": row,
                    "expected": {
                        "registry_entry": stable,
                        "agent_config": agent_config(entry, config),
                        "manager_addr_update": updates,
                    },
                }
            )
    return {
        "description": "spec §4: pods arriving at the front port, in order, with this fleet "
        "configuration; timestamps are omitted.",
        "fleet_config": config,
        "cases": cases,
    }


VECTOR_SETS = {
    "al-mac.json": al_mac_vectors,
    "operation-transitions.json": transition_vectors,
    "translation-northbound.json": northbound_vectors,
    "translation-southbound.json": southbound_vectors,
    "fleet.json": fleet_vectors,
    "uplink.json": uplink_vectors,
}


def render(value):
    return json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def generate(directory=DEFAULT_DIR):
    directory.mkdir(parents=True, exist_ok=True)
    for name, build in VECTOR_SETS.items():
        (directory / name).write_text(render(build()))
    return sorted(VECTOR_SETS)


def check(directory=DEFAULT_DIR):
    """Names of the vector files the reference implementation no longer reproduces."""
    return [
        name
        for name, build in VECTOR_SETS.items()
        if not (directory / name).is_file() or (directory / name).read_text() != render(build())
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("generate", "check"))
    parser.add_argument("directory", nargs="?", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args()
    if args.command == "generate":
        print("\n".join(generate(args.directory)))
        return
    failed = check(args.directory)
    for name in failed:
        print(f"differs: {name}", file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
