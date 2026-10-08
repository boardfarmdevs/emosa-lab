# SPDX-License-Identifier: Apache-2.0
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
import contextlib
import copy
import dataclasses
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

from emosa.agent.fleet import Fleet, agent_config, derive_al
from emosa.easymesh_payloads import encode_value
from emosa.errors import EmosaError, Reason
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


def aged_at_response():
    """Station ages as of each Topology Response, from the association times (a suite
    finding: ages taken when membership changed went stale for the controller)."""
    raw = pod_rows()
    view = device_view(decode(raw))
    radio = view.radio(mac(RUID))
    associated_at = {"02:00:00:00:0a:00": 100.0, "02:00:00:00:10:00": 40.25}
    facts = topology(
        agent_al=mac(AGENT),
        controller_al=mac(CONTROLLER),
        radio=radio,
        channel=6,
        bsses=radio.bsses,
        ages={mac(m): 0 for m in associated_at},
        associated_at={mac(m): t for m, t in associated_at.items()},
    )
    binding = PeerBinding("conformance", 1, mac(AGENT), mac(CONTROLLER), (mac(CONTROLLER),))
    return {
        "ovsdb_tables": raw,
        "associated_at": associated_at,
        "responses": [
            {"now": now, "topology_tlvs": tlvs(_topology(facts, binding, EASYMESH_61, now=now))}
            for now in (100.0, 112.7, 65700.0)
        ],
    }


def northbound_vectors():
    ages = {"02:00:00:00:0a:00": 12, "02:00:00:00:10:00": 70000}
    return {
        "description": "spec §2.6, §3.3: pod OVSDB rows (raw RFC 7047 JSON) to the device view, "
        "and the view to 1905 TLVs. 'value' is the TLV value in hex, without type and length. "
        "aged_at_response: the recorded rows' stations associated at the given times "
        "(monotonic seconds); per response time, the EasyMesh 6.1 Topology Response TLVs, each "
        "station's age taken as of the response (whole seconds, at most 65535).",
        "cases": [northbound_case(name, make(), ages) for name, make in POD_CASES.items()],
        "aged_at_response": aged_at_response(),
    }


INSERTED = "00000000-0000-4000-8000-0000000000ff"


class Recorder:
    """An OVSDB session over fixed rows that records every transaction. With ``owm``, a
    steering client row a transaction inserts appears in the rows as owm steering it; the
    transactions numbered in ``lost`` (from 0) are carried out but their replies are lost."""

    def __init__(self, raw, *, owm=False, lost=()):
        self.schema = Schema(json.loads(reference_path().read_text()))
        self.tables, self.sent = raw, []
        self.owm, self.lost = owm, set(lost)

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
        for op in operations if self.owm else ():
            if op["op"] == "insert" and op["table"] == "Band_Steering_Clients":
                row = {**op["row"], "cs_state": "steering"}
                self.tables.setdefault("Band_Steering_Clients", {})[INSERTED] = row
        if len(self.sent) - 1 in self.lost:
            raise ConnectionError("the reply was lost")
        replies = {
            "wait": {},
            "select": {"rows": []},
            "insert": {"uuid": ["uuid", INSERTED]},
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


FLEET_ROOT = "{root}"  # a session's directory: each implementation substitutes its own
FLEET_STAMP = "20261003T120000"  # forget's archive suffix, as time.strftime would give it


def fleet_session_vectors():
    import emosa.agent.fleet as fleet_module

    def node(serial, **extra):
        return {"id": serial, "serial_number": serial, "model": "HWSIM_POD", **extra}

    base = {
        "listen": "ptcp:6650:127.0.0.1",
        "advertise": "10.101.0.1",
        "ports": [6651, 6653],
        "controller_al": CONTROLLER,
        "state_root": f"{FLEET_ROOT}/state",
        "config_dir": f"{FLEET_ROOT}/etc",
        "message_set": EASYMESH_61,
    }
    own = {
        SERIAL: {
            "profile": DEFAULT,
            # keys in sorted order: the vector file is written sorted, and the configuration
            # file's order is the order the agent configuration keeps
            "uplink": {"bssid": "02:00:00:00:19:01", "mode": "multi-ap"},
        }
    }
    sessions = {
        "pods-arrive-and-return": [
            {"fleet_config": base},
            {"arrival": [{"rows": [node(SERIAL)]}], "now": 1759500000.25},
            {
                "arrival": [{"rows": [node("MVXPOD02D7777EF0D9", firmware_version="6.6.1.0")]}],
                "now": 1759500001.5,
            },
            {"arrival": [{"rows": [node(SERIAL)]}], "now": 1759500002.75},
        ],
        # a run root (a RAM disk on a gateway): each agent's run_dir, where its status goes
        "run-root-gives-each-agent-a-run-directory": [
            {"fleet_config": {**base, "run_root": f"{FLEET_ROOT}/run"}},
            {"arrival": [{"rows": [node(SERIAL)]}], "now": 1759500000.25},
        ],
        # a fleet started again (a reboot, an image upgrade that kept its files): the
        # registry's agents at once, in the registry's order; only the admitted ones
        "a-started-fleet-starts-its-registrys-agents": [
            {"fleet_config": base},
            {"arrival": [{"rows": [node("MVXPOD02D7777EF0D9")]}], "now": 1759500000.25},
            {"arrival": [{"rows": [node(SERIAL)]}], "now": 1759500000.5},
            {"fleet_config": base},
            {"fleet_config": {**base, "admit": ["MVXPOD02D7777EF0D9"]}},
        ],
        # the controller's Topology Query cadence (spec 2.5): the fleet's to every agent;
        # set later, the agent's configuration changes, so it restarts
        "topology-query-window-goes-to-every-agent": [
            {"fleet_config": base},
            {"arrival": [{"rows": [node(SERIAL)]}], "now": 1759500000.25},
            {"fleet_config": {**base, "topology_query_window": 120}},
            {"arrival": [{"rows": [node(SERIAL)]}], "now": 1759500001.25},
        ],
        "own-settings-restart-the-agent": [
            {"fleet_config": base},
            {"arrival": [{"rows": [node(SERIAL)]}], "now": 1759500000.25},
            {"fleet_config": {**base, "pods": own}},
            {"arrival": [{"rows": [node(SERIAL)]}], "now": 1759500003.125},
        ],
        "unusable-or-unadmitted-pods-are-left-unchanged": [
            {"fleet_config": {**base, "admit": ["MVXPOD02D7777EF0D9"]}},
            {"arrival": [{"rows": [node(SERIAL)]}], "now": 1759500000.25},
            {"arrival": [{"rows": []}], "now": 1759500000.5},
            {
                "arrival": [{"rows": [node(SERIAL), node("MVXPOD02D7777EF0D9")]}],
                "now": 1759500000.75,
            },
            {"arrival": [{"rows": [node("bad serial")]}], "now": 1759500001.0},
            {"arrival": [{"rows": [node("../x")]}], "now": 1759500001.25},
            {"arrival": [{"rows": [{"id": "x", "model": "HWSIM_POD"}]}], "now": 1759500001.5},
            {"arrival": [{"error": "syntax error"}], "now": 1759500001.75},
            {"fleet_config": {**base, "ports": [6651, 6651]}},
            {"arrival": [{"rows": [{"serial_number": SERIAL}]}], "now": 1759500002.25},
            {"arrival": [{"rows": [node("MVXPOD02D7777EF0D9")]}], "now": 1759500002.5},
        ],
        "forget-releases-and-archives": [
            {"fleet_config": base},
            {"arrival": [{"rows": [node(SERIAL)]}], "now": 1759500000.25},
            {"mkdir": f"state/{SERIAL}"},
            {"forget": SERIAL},
            {"forget": SERIAL},
            {"arrival": [{"rows": [node(SERIAL)]}], "now": 1759500009.5},
        ],
        # box finding 14: emosa-fleet forget runs as its own process while the fleet serves
        "forget-while-the-fleet-serves": [
            {"fleet_config": base},
            {"arrival": [{"rows": [node(SERIAL)]}], "now": 1759500000.25},
            {"arrival": [{"rows": [node("MVXPOD02D7777EF0D9")]}], "now": 1759500000.5},
            {"forget_elsewhere": SERIAL},
            {"arrival": [{"rows": [node(SERIAL)]}], "now": 1759500009.5},
        ],
    }
    out = []
    for name, steps in sessions.items():
        with tempfile.TemporaryDirectory() as directory:
            root = str(Path(directory).resolve())

            def live(value, root=root):
                return json.loads(json.dumps(value).replace(FLEET_ROOT, root))

            def placeholder(text, root=root):
                return text.replace(root, FLEET_ROOT)

            fleet, recorded = None, []
            for step in steps:
                done = dict(step)
                if "fleet_config" in step:
                    config = live(step["fleet_config"])
                    for key in ("state_root", "config_dir"):
                        Path(config[key]).mkdir(parents=True, exist_ok=True)
                    starts, stops = [], []
                    fleet = Fleet(
                        config,
                        starter=lambda pod, changed, starts=starts: starts.append([pod, changed]),
                        stopper=lambda pod, stops=stops: stops.append(pod),
                    )
                    fleet.start_registered()  # serving: the registry's agents first
                    done["expected"] = {"started": list(starts)}
                elif "mkdir" in step:
                    (Path(root) / step["mkdir"]).mkdir(parents=True)
                elif "arrival" in step:
                    updates = []

                    def call(conn, method, params, deadline, step=step, updates=updates):
                        if params[1]["op"] == "select":
                            return step["arrival"]
                        updates.append(params)
                        return [{"count": 1}]

                    fleet._call = call
                    del starts[:]
                    try:
                        with mock.patch.object(fleet_module.time, "time", lambda s=step: s["now"]):
                            entry = fleet.handle(None, "conformance")
                        outcome = "handover" if entry else "refused"
                    except ValueError:
                        outcome = "invalid"
                    done["expected"] = {
                        "outcome": outcome,
                        "start": starts[0] if starts else None,
                        "update": updates[0] if updates else None,
                    }
                elif "forget_elsewhere" in step:
                    other = Fleet(config, starter=lambda *_: None, stopper=lambda *_: None)
                    with mock.patch.object(fleet_module.time, "strftime", lambda _f: FLEET_STAMP):
                        other.forget(step["forget_elsewhere"])
                elif "forget" in step:
                    del stops[:]
                    with mock.patch.object(fleet_module.time, "strftime", lambda _f: FLEET_STAMP):
                        entry = fleet.forget(step["forget"])
                    done["expected"] = {
                        "entry": live(entry)
                        if entry is None
                        else json.loads(placeholder(json.dumps(entry))),
                        "stopped": list(stops),
                    }
                recorded.append(done)
            state = Path(fleet.config["state_root"])
            configs = Path(fleet.config["config_dir"])
            out.append(
                {
                    "name": name,
                    "steps": recorded,
                    "files": {
                        "registry": placeholder((state / "fleet.json").read_text()),
                        "configs": {
                            p.stem: placeholder(p.read_text())
                            for p in sorted(configs.glob("*.json"))
                        },
                        "archives": sorted(p.name for p in state.glob("*.released-*")),
                    },
                }
            )
    return {
        "description": "spec §4: the fleet over whole sessions, as emosa.agent.fleet handles "
        "them. A step: fleet_config (the fleet (re)started with this configuration on the same "
        "files; it starts the agents its registry has, in the registry's order: each [pod, "
        "configuration changed]), arrival (a pod at the front port: the result of the fleet's "
        "AWLAN_Node "
        "select, and the wall clock), mkdir (a directory under the session's), forget (a "
        "serial, archived with suffix " + FLEET_STAMP + "), forget_elsewhere (the same, by "
        "another fleet process on the same files while this one serves). Per arrival: "
        "handover (the agent started or restarted, then the update sent), refused (not "
        "admitted, or no free port: nothing written to the pod) or invalid (an unusable "
        "identity: the same). files: the "
        "registry and the agent configurations as the fleet left them, byte for byte, and "
        "the archived state directories. " + FLEET_ROOT + " stands for the session's own "
        "directory.",
        "root": FLEET_ROOT,
        "stamp": FLEET_STAMP,
        "sessions": out,
    }


class RecordingIp:
    """iproute2 over an in-memory set of links (tests/test_gtp.py's FakeIp), recording
    every call with its check flag and output."""

    def __init__(self, links):
        self.links = copy.deepcopy(links)
        self.calls = []

    def __call__(self, *args, check=True):
        output = self._run(args)
        self.calls.append([list(args), check, output])
        return output

    def _run(self, args):
        if args[:3] == ("-br", "link", "show"):
            return f"{args[4]} UP\n" if args[4] in self.links else ""
        if args[:5] == ("-d", "-o", "link", "show", "type"):
            return "".join(
                f"7: {n}@NONE: <UP> mtu 1562 \\    gretap"
                f" remote {v['remote']} local {v['local']}"
                + (f" dev {v['dev']}" if v.get("dev") else "")
                + " ttl inherit\n"
                for n, v in self.links.items()
                if v.get("type") == "gretap"
            )
        if args[:4] == ("-o", "link", "show", "master"):
            return "".join(
                f"{i}: {n}: <UP> mtu 1500 master {args[4]}\n"
                for i, (n, v) in enumerate(self.links.items())
                if v.get("master") == args[4]
            )
        if args[:2] == ("link", "add"):
            self.links[args[2]] = dict(zip(args[3::2], args[4::2], strict=False))
        elif args[:2] == ("link", "del"):
            del self.links[args[2]]
        elif args[:2] == ("link", "set") and "master" in args:
            self.links[args[2]]["master"] = args[args.index("master") + 1]
        return ""


def gtp_vectors():
    from emosa.gtp import GTP, ConfigError, Links, check, render_dnsmasq, tunnel_name

    config = {
        "underlay": {
            "interface": "podbh",
            "address": "169.254.2.1/25",
            "mtu": 1600,
            "dhcp_range": ["169.254.2.10", "169.254.2.126"],
            "lease_time": "1h",
        },
        "lan": {"bridge": "br-gtp", "ports": ["eth1"]},
        "tunnel_mtu": 1562,
        "state_dir": "/var/lib/emosa-gtp",
    }

    def changed(**underlay):
        c = copy.deepcopy(config)
        tunnel = underlay.pop("tunnel_mtu", None)
        c["underlay"].update(underlay)
        if tunnel:
            c["tunnel_mtu"] = tunnel
        return c

    checks = []
    for case in (
        config,
        changed(address="169.254.2.2/25"),
        changed(address="169.254.2.129/25"),
        changed(address="169.254.0.1/16", dhcp_range=["169.254.0.10", "169.254.255.254"]),
        changed(dhcp_range=["169.254.2.1", "169.254.2.126"]),
        changed(dhcp_range=["169.254.2.10", "169.254.3.10"]),
        changed(dhcp_range=["169.254.2.100", "169.254.2.10"]),
        changed(tunnel_mtu=1580),
        changed(mtu=1600, tunnel_mtu=1562),
        changed(address="169.254.2.1/24", dhcp_range=["169.254.2.2", "169.254.2.254"]),
    ):
        try:
            check(copy.deepcopy(case))
            error = None
        except ConfigError as exc:
            error = str(exc)
        checks.append({"config": case, "error": error})

    leases_text = (
        "0 02:00:00:00:05:00 169.254.2.57 pod-1 *\n"
        "4102444800 02:00:00:00:06:00 169.254.2.60 pod-2 01:02:00:00:00:06:00\n"
        "1000 02:00:00:00:07:00 169.254.2.61 gone *\n"
    )
    sessions = {
        "setup-lease-renew-release": (
            {"podbh": {}, "eth1": {}, "wlan0": {"master": "podbh"}},
            "",
            [
                ["setup"],
                ["lease", "add", "02:00:00:00:05:00", "169.254.2.57", "pod-1"],
                ["lease", "old", "02:00:00:00:05:00", "169.254.2.57"],
                ["lease", "add", "02:00:00:00:06:00", "169.254.2.60"],
                ["lease", "del", "02:00:00:00:05:00", "169.254.2.57"],
                ["lease", "add", "02:00:00:00:05:00", "169.254.1.57"],
                ["lease", "add", "02-00-00-00-05-00", "169.254.2.57"],
                ["list"],
            ],
        ),
        "reconcile-to-the-leases": (
            {
                "podbh": {},
                "eth1": {},
                "gtp2_99": {"type": "gretap", "local": "169.254.2.1", "remote": "169.254.2.99"},
                "gtp2_57": {
                    "type": "gretap",
                    "local": "169.254.2.1",
                    "remote": "169.254.2.57",
                    "dev": "if31",
                },
            },
            leases_text,
            [["reconcile"], ["list"]],
        ),
        "setup-without-its-interfaces": (
            {"eth1": {}},
            "",
            [["setup"]],
        ),
        "setup-without-the-lan-port": (
            {"podbh": {}},
            "",
            [["setup"]],
        ),
    }
    out = []
    for name, (links, leases, steps) in sessions.items():
        with tempfile.TemporaryDirectory() as directory:
            live = copy.deepcopy(config)
            live["state_dir"] = directory
            (Path(directory) / "leases").write_text(leases)
            fake = RecordingIp(links)
            gtp = GTP(live, Links(fake))
            recorded = []
            for step in steps:
                del fake.calls[:]
                try:
                    if step[0] == "setup":
                        result = gtp.setup(Path(directory) / "gtp.json")
                    elif step[0] == "lease":
                        result = gtp.lease(*step[1:4])
                    elif step[0] == "reconcile":
                        result = gtp.reconcile()
                    else:
                        result = gtp.list()
                    error = None
                except ConfigError as exc:
                    result, error = None, str(exc)
                recorded.append(
                    {"command": step, "calls": list(fake.calls), "result": result, "error": error}
                )
            out.append({"name": name, "links": links, "leases": leases, "steps": recorded})
    conf, _hook = render_dnsmasq(config, "/etc/emosa-gtp.json")
    return {
        "description": "spec §8.2: the GRE termination point, as emosa.gtp. checks: the rules "
        "beyond the schema (error: the refusal, null: accepted). tunnel_names: an underlay "
        "address and its gretap. dnsmasq: the configuration setup writes for config (its "
        "dhcp-script runs the implementation's own lease command). sessions: commands on a "
        "host whose links and dnsmasq lease file are given, the session's state_dir its own "
        "directory: every iproute2 call in order (its arguments after ip, whether a failure "
        "stops the command, and the output it gave), and the command's result (the tunnels "
        "listed) or its refusal.",
        "config": config,
        "checks": checks,
        "tunnel_names": [
            [ip, tunnel_name(ip)] for ip in ("169.254.2.57", "169.254.2.126", "169.254.255.1")
        ],
        "dnsmasq": conf,
        "sessions": out,
    }


# -- the 1905 envelope and the provisioned agent's control plane ------------------


def cmdu_vectors():
    from emosa.wire.cmdu import Fragment, Reassembler, Tlv, fragment_message

    agent, controller = mac(AGENT), mac(CONTROLLER)
    encode = []
    for name, message_type, mid, items, relay, mtu in (
        ("topology-query", 0x0002, 1, (), False, 1500),
        (
            "ack-with-error-codes",
            0x8000,
            0x1234,
            (Tlv(0xA3, b"\x02" + bytes(range(6))),),
            False,
            1500,
        ),
        ("relayed-multicast", 0x0007, 7, (Tlv(0x01, agent), Tlv(0x0F, b"\x00")), True, 1500),
        (
            "fragmented",
            0x0009,
            42,
            tuple(Tlv(0x11, bytes([i]) * 600) for i in range(3)),
            False,
            1500,
        ),
        ("small-mtu", 0x8002, 9, (Tlv(0x80, b"\x05" * 40), Tlv(0x85, b"\x06" * 40)), False, 64),
    ):
        destination = bytes.fromhex("0180c2000013") if relay else controller
        frames = fragment_message(
            destination, agent, message_type, mid, items, relay=relay, mtu=mtu
        )
        encode.append(
            {
                "name": name,
                "input": {
                    "destination": destination.hex(":"),
                    "source": AGENT,
                    "message_type": f"0x{message_type:04x}",
                    "mid": mid,
                    "relay": relay,
                    "mtu": mtu,
                    "tlvs": tlvs(items),
                },
                "expected_frames": [f.hex() for f in frames],
            }
        )
    # what the agent refuses to send: an end marker given as a TLV, a TLV that no fragment
    # holds, more than 64 fragments or 256 TLVs, an MTU below the header
    for name, items, mtu in (
        ("end-marker-as-tlv", (Tlv(0x00, b""),), 1500),
        ("tlv-over-mtu", (Tlv(0x80, b"\x05" * 60),), 64),
        ("over-64-fragments", tuple(Tlv(0x80, b"\x05" * 40) for _ in range(65)), 64),
        ("over-256-tlvs", tuple(Tlv(0x80, b"") for _ in range(257)), 1500),
        ("mtu-below-header", (), 13),
    ):
        try:
            fragment_message(controller, agent, 0x8002, 9, items, mtu=mtu)
            error = None
        except EmosaError as exc:
            error = str(exc.code)
        encode.append(
            {
                "name": name,
                "input": {
                    "destination": CONTROLLER,
                    "source": AGENT,
                    "message_type": "0x8002",
                    "mid": 9,
                    "relay": False,
                    "mtu": mtu,
                    "tlvs": tlvs(items),
                },
                "expected_error": error,
            }
        )
    decode = []
    good = fragment_message(agent, controller, 0x8014, 900, (Tlv(0x9B, bytes(13)),))[0]
    header = 14 + 8  # Ethernet + 1905 header

    def case(name, frames, times=None):
        now = [0.0]
        parser = Reassembler(clock=lambda: now[0])
        for index, frame in enumerate(frames):
            now[0] = times[index] if times else 0.0
            result = error = None
            try:
                result = parser.feed(frame)
            except EmosaError as exc:
                error = str(exc.code)
        expected = (
            {"error": error}
            if error
            else {
                "message": None
                if result is None
                else {
                    "destination": result.destination.hex(":"),
                    "source": result.source.hex(":"),
                    "message_type": f"0x{result.message_type:04x}",
                    "mid": result.mid,
                    "relay": result.relay,
                    "tlvs": tlvs(result.tlvs),
                }
            }
        )
        entry = {"name": name, "frames": [f.hex() for f in frames], "expected": expected}
        if times:
            entry["times"] = times
        decode.append(entry)

    def fragment(mid, fid, last, payload, relay=False, message_type=0x0009):
        return Fragment(controller, agent, message_type, mid, fid, last, relay, payload).encode()

    case("single", [good])
    large = tuple(Tlv(0x11, bytes([i]) * 600) for i in range(3))
    case("fragments-in-order", list(fragment_message(agent, controller, 0x0009, 42, large)))
    case("first-fragment-only", [fragment_message(agent, controller, 0x0009, 43, large)[0]])
    case("reserved-version", [good[:14] + b"\x01" + good[15:]])
    case("truncated-tlv", [good[: header + 5]])
    case("missing-end-of-message", [good[: header + 16]])
    case("ethernet-padding-after-end", [good + bytes(20)])
    # the refusals: frames that are not 1905, fragments that conflict, budgets, the deadline
    f0, f1, f2 = fragment_message(agent, controller, 0x0009, 44, large, mtu=700)
    altered = f0[:-10] + bytes([f0[-10] ^ 1]) + f0[-9:]
    relayed = fragment_message(agent, controller, 0x0009, 44, large, relay=True, mtu=700)
    pair = fragment_message(agent, controller, 0x0009, 44, large[:2], mtu=700)
    single = fragment_message(agent, controller, 0x0009, 44, large[:1], mtu=700)
    case("wrong-ethertype", [good[:12] + b"\x88\x8e" + good[14:]])
    case("shorter-than-header", [good[: header - 1]])
    case("duplicate-fragment", [f0, f0, f1, f2])
    case("conflicting-duplicate", [f0, altered])
    case("quarantined-after-conflict", [f0, altered, f1])
    case("relay-mismatch", [f0, relayed[1]])
    case("fragment-index-over-budget", [fragment(45, 64, True, b"\x00\x00\x00")])
    case("fragment-after-final", [pair[1], f2])
    case("final-before-stored", [f1, single[0]])
    case("empty-non-final-fragment", [fragment(46, 0, False, b"")])
    case("non-final-end-marker", [fragment(47, 0, False, b"\x00\x00\x00")])
    many = Tlv(0x11, b"").encode()
    case(
        "tlv-count-over-budget",
        [fragment(48, 0, False, many * 200), fragment(48, 1, True, many * 60 + b"\x00\x00\x00")],
    )
    case("context-budget", [fragment(mid, 0, False, many) for mid in range(100, 165)])
    case("deadline-expired", [f0, f1, f2], times=[0.0, 5.0, 5.0])
    case("within-deadline", [f0, f1, f2], times=[0.0, 4.9, 4.9])
    return {
        "description": "spec §2.1: the IEEE 1905.1 envelope. 'encode': a message to the "
        "Ethernet frames the agent transmits (fragmented at TLV boundaries within the MTU), "
        "or the reason code it refuses with. 'decode': received frames, fed in order to one "
        "reassembler (timeout 5 s, 64 contexts, 64 fragments, 64 KiB a message, 2 MiB in "
        "all), each rejection dropping only its frame, 'times' the clock at each frame "
        "(0 when absent); expected is the last frame's outcome: the reassembled message, "
        "none while incomplete, or the reason code of the rejection.",
        "encode": encode,
        "decode": decode,
    }


def wsc_m2_vectors():
    """The M2's authentication and mapping (spec §2.5): what a controller's M2 for the
    fixture's M1 gives, or the reason it is refused, built as the reference's tests build
    them (an attribute changed or removed, the settings encrypted and the message signed
    again with the exchange's keys)."""
    from cryptography.hazmat.primitives.asymmetric import dh

    from emosa import wsc
    from emosa.wsc import (
        MODP_1536,
        KeyPair,
        SessionKeys,
        authenticate_message,
        decode_attributes,
        encode_attribute,
        encrypt_settings,
    )
    from emosa.wsc_messages import WFA_ID, M1Transcript
    from emosa.wsc_radio import decode_radio_payloads

    fixture = json.loads(WSC_FIXTURE.read_text())["cases"][0]

    def raw(name):
        return bytes.fromhex(fixture[name])

    def transcript():
        parameters = dh.DHParameterNumbers(MODP_1536, 2, (MODP_1536 - 1) // 2)
        public = dh.DHPublicNumbers(int.from_bytes(raw("enrollee_public"), "big"), parameters)
        private = int.from_bytes(raw("enrollee_private"), "big")
        return M1Transcript(raw("m1"), KeyPair(dh.DHPrivateNumbers(private, public).private_key()))

    keys = SessionKeys(*(raw(name) for name in ("auth_key", "key_wrap_key", "emsk")))
    body = raw("m2")[:-12]  # without its Authenticator
    settings = raw("ap_settings")

    def rewrite(data, kind, value):
        return b"".join(
            encode_attribute(a.kind, value if a.kind == kind else a.value)
            for a in decode_attributes(data)
            if a.kind != kind or value is not None
        )

    def signed(changed):
        return authenticate_message(keys, raw("m1"), changed)

    def with_settings(plain):
        # a fixed IV (00..0f), as the fixture's public entropy: the vectors reproduce
        with mock.patch.object(wsc.secrets, "token_bytes", lambda n: bytes(range(n))):
            return signed(rewrite(body, 0x1018, encrypt_settings(keys, plain)))

    def role(flags):
        return with_settings(rewrite(settings, 0x1049, WFA_ID + bytes([6, 1, flags])))

    altered = raw("m2")[:-1] + bytes([raw("m2")[-1] ^ 1])
    cases = [
        ("baseline", raw("m2")),
        ("teardown", raw("teardown_m2")),
        ("backhaul-role-alone", role(0x40)),
        ("combined-role", role(0x60)),
        ("authenticator-altered", altered),
        ("no-encrypted-settings", signed(rewrite(body, 0x1018, None))),
        ("another-enrollee-nonce", signed(rewrite(body, 0x101A, bytes(16)))),
        ("nonce-twice", signed(encode_attribute(0x101A, bytes(range(16))) + body)),
        (
            "bss-index-first",
            signed(encode_attribute(0x1BBC, b"\x01") + rewrite(body, 0x1BBC, None)),
        ),
        ("bss-index-two-octets", signed(rewrite(body, 0x1BBC, b"\x01\x02"))),
        ("version-twice", signed(rewrite(body, 0x1049, b"\x00\x37\x2a\x00\x01\x20\x00\x01\x20"))),
        ("version-two-octets", signed(rewrite(body, 0x1049, b"\x00\x37\x2a\x00\x02\x20"))),
        ("version-absent", signed(rewrite(body, 0x1049, None))),
        ("registrar-key-zero", signed(rewrite(body, 0x1032, bytes(192)))),
        ("settings-key-twice", with_settings(settings + encode_attribute(0x1027, b"another-key"))),
        ("ssid-33-octets", with_settings(rewrite(settings, 0x1045, bytes(33)))),
        ("key-65-octets", with_settings(rewrite(settings, 0x1027, bytes(65)))),
        ("network-key-index", with_settings(settings + encode_attribute(0x1028, bytes(2)))),
        (
            "wfa-subelement-overruns",
            with_settings(rewrite(settings, 0x1049, b"\x00\x37\x2a\x06\x02\x20")),
        ),
        (
            "password-change",
            with_settings(
                settings
                + encode_attribute(0x102A, b"new-public-password")
                + encode_attribute(0x1012, b"\x00\x00")
            ),
        ),
        (
            "legacy-passphrase-terminator",
            with_settings(rewrite(settings, 0x1027, b"public-vector-passphrase\0")),
        ),
        ("ssid-terminator", with_settings(rewrite(settings, 0x1045, b"private_ssid\0"))),
        ("ssid-two-terminators", with_settings(rewrite(settings, 0x1045, b"private_ssid\0\0"))),
        ("ssid-empty", with_settings(rewrite(settings, 0x1045, b""))),
        ("ssid-embedded-nul", with_settings(rewrite(settings, 0x1045, b"embedded\0ssid"))),
        ("ssid-not-utf8", with_settings(rewrite(settings, 0x1045, b"\xff"))),
        ("passphrase-short", with_settings(rewrite(settings, 0x1027, b"short"))),
        (
            "passphrase-control-character",
            with_settings(rewrite(settings, 0x1027, b"public\x01vector-pass")),
        ),
        ("authentication-open", with_settings(rewrite(settings, 0x1003, b"\x00\x01"))),
        ("encryption-tkip", with_settings(rewrite(settings, 0x100F, b"\x00\x04"))),
    ]
    cases += [
        (f"m2-without-{kind:04x}", signed(rewrite(body, kind, None)))
        for kind in (0x1022, 0x1039, 0x1048, 0x1032, 0x1004, 0x1010, 0x1011, 0x102D)
    ]
    cases += [
        (f"settings-without-{kind:04x}", with_settings(rewrite(settings, kind, None)))
        for kind in (0x1045, 0x1003, 0x100F, 0x1027, 0x1020)
    ]
    out = []
    for name, message in cases:
        try:
            radio = decode_radio_payloads(transcript(), (message,), max_bss=1)
            if radio.action == "teardown":
                expected = {"teardown": True, "bss": []}
            else:
                candidate = radio.existing_fronthaul_candidate()
                expected = {
                    "teardown": False,
                    "bss": [
                        {
                            "role": "fronthaul",
                            "ssid": candidate.ssid,
                            "passphrase": candidate.passphrase,
                            "bss_index": candidate.bss_index,
                        }
                    ],
                }
        except EmosaError as exc:
            expected = {"error": str(exc.code)}
        out.append({"name": name, "m2": [message.hex()], "expected": expected})
    return {
        "description": "spec §2.5: one radio's M2 for the M1 below (the WSC payload fixture's, "
        "its enrollee keys public test values), authenticated and mapped to the BSS it "
        "configures: one fronthaul BSS (max_bss 1, not multi-BSS), a teardown, or the reason "
        "it is refused. Passphrases are public test values.",
        "m1": fixture["m1"],
        "enrollee_private": fixture["enrollee_private"],
        "enrollee_public": fixture["enrollee_public"],
        "cases": out,
    }


def control_agent(raw, stations):
    """A provisioned agent's report source over recorded rows, at time 0."""
    from emosa.wire.channel import OperatingRadio
    from emosa.wire.coordinator import ReportSource

    view = device_view(decode(raw))
    radio = view.radio(mac(RUID))
    caps = radio_capabilities(radio, channel=6, max_bss=5, max_eirp=30)
    facts = topology(
        agent_al=mac(AGENT),
        controller_al=mac(CONTROLLER),
        radio=radio,
        channel=6,
        bsses=radio.bsses,
        ages={mac(m): 10 for m in stations},
    )
    binding = PeerBinding("conformance", 1, mac(AGENT), mac(CONTROLLER), (mac(CONTROLLER),))
    source = ReportSource(binding, "pod-1", "c" * 64, clock=lambda: 0.0)
    source.publish(
        (1, 1),
        caps,
        facts,
        observed_at=0.0,
        lifetime=1.5,
        operating_radios=(OperatingRadio(mac(RUID), 81, 6, radio.tx_power),),
    )
    return source, radio


SCAN_TIMESTAMP = "2026-09-25T00:00:00.000Z"  # the Channel Scan Report's time, fixed
PROBE_WALL = 1790313745.0  # the agent's wall clock for probe ages (epoch seconds), fixed
# The pod's last probe requests (spec §3.9) for the measured query: heard 30 s
# before, heard 200 s before (too old), a 5 GHz probe, and one on the station
# queried on another channel
PROBES = {
    "02:00:00:00:99:99": {
        "band": "2.4G",
        "ifname": "wl0.1",
        "snr_db": 34,
        "measured_at": PROBE_WALL - 30,
    },
    "02:00:00:00:96:96": {
        "band": "2.4G",
        "ifname": "wl0.1",
        "snr_db": 30,
        "measured_at": PROBE_WALL - 200,
    },
    "02:00:00:00:95:95": {
        "band": "5G",
        "ifname": "wl1.1",
        "snr_db": 40,
        "measured_at": PROBE_WALL - 5,
    },
    "02:00:00:00:98:98": {
        "band": "2.4G",
        "ifname": "wl0.1",
        "snr_db": 28,
        "measured_at": PROBE_WALL - 10,
    },
}


class RecordedProbes:
    """The pod's statistics as the query sees them: the last probe per station."""

    def __init__(self, samples):
        from emosa.opensync.stats import ProbeSample

        self.samples = {m: ProbeSample(**s) for m, s in samples.items()}

    def probe(self, mac):
        return self.samples.get(mac.lower())


def control_vectors():
    from emosa.wire.channel import ChannelCoordinator, ChannelPolicyStore
    from emosa.wire.cmdu import MidSequence, Reassembler, Tlv, fragment_message
    from emosa.wire.reporting_policy import ReportingPolicyCoordinator, ReportingPolicyStore
    from emosa.wire.steering import SteeringCoordinator
    from emosa.wire.unassociated import UnassociatedCoordinator

    raw = pod_rows()
    stations = ["02:00:00:00:0a:00", "02:00:00:00:10:00"]
    bssid = mac("82:00:00:00:01:00")
    ruid = mac(RUID)
    target = mac("02:00:00:12:75:2c")

    def request(message_type, mid, items):
        return fragment_message(mac(AGENT), mac(CONTROLLER), message_type, mid, items)[0]

    def steering_tlv(stas, targets, *, mandate=True, imminent=True, window=5):
        flags = (0x80 if mandate else 0) | (0x40 if imminent else 0) | 0x20
        value = bssid + bytes([flags]) + window.to_bytes(2, "big") + (5).to_bytes(2, "big")
        value += bytes([len(stas)]) + b"".join(stas)
        value += bytes([len(targets)]) + b"".join(b + bytes([c, n]) for b, c, n in targets)
        return Tlv(0x9B, value)

    rdk_preferences = bytes.fromhex(  # RDK's, captured: channel 6, 40 MHz classes 83/84
        "05510c01020304050708090a0b0c0d00510106105308010203040507080910"
        "530106e0540905060708090a0b0c0d10"
    )
    rdk_policy = (
        Tlv(0x89, bytes.fromhex("000001") + ruid + bytes.fromhex("023c78")),
        Tlv(0x8A, bytes.fromhex("0501") + ruid + bytes.fromhex("78053cc0")),
        Tlv(0xB5, bytes.fromhex("000140")),
        Tlv(
            0xB6,
            bytes.fromhex(
                "050c707269766174655f73736964000c08696f745f73736964000e0a6c6e665f7261"
                "64697573000f0d6d6573685f6261636b6861756c000d07686f7473706f740010"
            ),
        ),
        Tlv(0xA4, b"\x00"),
        Tlv(0xC4, bytes(5)),
        Tlv(0x0B, bytes.fromhex("d89c8e00")),
    )
    sequences = [
        (
            "channel-preference-query",
            [request(0x8004, 11, ())],
        ),
        (
            "channel-selection-accepted",
            [request(0x8006, 12, (Tlv(0x8B, ruid + bytes.fromhex("01510106e0")),))],
        ),
        (
            # class 81 channels 1 and 11 less preferred, a 40 MHz class ignored, the
            # pod's own 30 dBm as the power limit
            "channel-selection-accepted-with-limit",
            [
                request(
                    0x8006,
                    14,
                    (
                        Tlv(0x8B, ruid + bytes.fromhex("025102010b50530106e0")),
                        Tlv(0x8D, ruid + bytes([30])),
                    ),
                )
            ],
        ),
        (
            "channel-selection-rdk-declined",  # RDK's request: 40 MHz classes, 0 dBm power limit
            [request(0x8006, 13, (Tlv(0x8B, ruid + rdk_preferences), Tlv(0x8D, ruid + b"\x00")))],
        ),
        ("policy-rdk", [request(0x8003, 14, rdk_policy)]),  # RDK's TLV set, captured
        (
            "channel-scan",  # RDK's layout: no fresh scan, one radio, one class
            [request(0x801B, 19, (Tlv(0xA6, b"\x00\x01" + ruid + bytes.fromhex("0151020106")),))],
        ),
        (
            # 258 results, the last for the radio named again with no class: more than the
            # C's former fixed 256, which that last one overflowed (found in review, 8.4)
            "channel-scan-many",
            [
                request(
                    0x801B,
                    20,
                    (
                        Tlv(
                            0xA6,
                            b"\x00\x03"
                            + ruid
                            + b"\x01\x51\xff"
                            + bytes(range(1, 256))
                            + ruid
                            + bytes.fromhex("0151020106")
                            + ruid
                            + b"\x00",
                        ),
                    ),
                )
            ],
        ),
        (
            "steering-mandate",
            [request(0x8014, 15, (steering_tlv([mac(stations[0])], [(target, 81, 6)]),))] * 2,
        ),
        (
            "steering-station-not-on-source",
            [request(0x8014, 16, (steering_tlv([mac("02:00:00:00:99:99")], [(target, 81, 6)]),))],
        ),
        (
            # 40 stations and 33 targets: more than the C's former 32 (plan 8.4); the TLV's
            # one-byte counts are the only limit
            "steering-many-stations",
            [
                request(
                    0x8014,
                    21,
                    (
                        steering_tlv(
                            [mac(f"02:00:00:00:a0:{i:02x}") for i in range(40)],
                            [(target, 81, 6)] * 33,
                        ),
                    ),
                )
            ],
        ),
        (
            "steering-opportunity",
            [
                request(
                    0x8014,
                    17,
                    (steering_tlv([mac(stations[0])], [(target, 81, 6)], mandate=False),),
                )
            ],
        ),
        (
            "steering-agent-selected-target",
            [request(0x8014, 18, (steering_tlv([mac(stations[0])], [(b"\xff" * 6, 0, 0)]),))],
        ),
        (
            # without telemetry nothing was heard: an associated station is refused with
            # 0x01, the others with 0x02, and the Ack is the whole answer (spec §3.9)
            "unassociated-query-refused",
            [
                request(
                    0x800F,
                    20,
                    (
                        Tlv(
                            0x97,
                            bytes((81, 2, 6, 2))
                            + mac(stations[0])
                            + mac("02:00:00:00:99:99")
                            + bytes((1, 2))
                            + mac("02:00:00:00:98:98")
                            + mac(stations[0]),
                        ),
                    ),
                )
            ],
        ),
        (
            # the pod heard 02:00:00:00:99:99 on its channel 30 s ago: measured, in a
            # Response after the Ack; the others are refused (associated, never heard,
            # heard too long ago, heard on 5 GHz, asked on another channel)
            "unassociated-query-measured",
            [
                request(
                    0x800F,
                    21,
                    (
                        Tlv(
                            0x97,
                            bytes((81, 2, 6, 5))
                            + mac(stations[0])
                            + mac("02:00:00:00:99:99")
                            + mac("02:00:00:00:97:97")
                            + mac("02:00:00:00:96:96")
                            + mac("02:00:00:00:95:95")
                            + bytes((1, 1))
                            + mac("02:00:00:00:98:98"),
                        ),
                    ),
                )
            ],
        ),
    ]
    other = mac("02:00:00:00:99:00")  # another agent's radio
    good = ruid + bytes.fromhex("01510106e0")
    multicast = bytes.fromhex("01005e000001")

    def one(name, message_type, mid, *tlvs):
        return (name, [request(message_type, mid, tlvs)])

    # what each parser refuses, one case each (plan 8.4): the reason the reference gives
    # is the contract, and so is what was sent before the refusal
    sequences += [
        one("refused-selection-truncated", 0x8006, 30, Tlv(0x8B, ruid + b"\x01\x51")),
        one("refused-selection-other-radio", 0x8006, 31, Tlv(0x8B, other + good[6:])),
        one(
            "refused-selection-overlapping",
            0x8006,
            32,
            Tlv(0x8B, ruid + bytes.fromhex("02510106e0510106c0")),
        ),
        one(
            "declined-selection-forbids-channel",
            0x8006,
            33,
            Tlv(0x8B, ruid + bytes.fromhex("0151010600")),
        ),
        one("refused-selection-unknown-companion", 0x8006, 34, Tlv(0x8B, good), Tlv(0xD8, b"\x00")),
        one(
            "refused-selection-power-other-radio",
            0x8006,
            35,
            Tlv(0x8B, good),
            Tlv(0x8D, other + b"\x10"),
        ),
        ("selection-repeated", [request(0x8006, 36, (Tlv(0x8B, good),))] * 2),
        one(
            "refused-scan-two-requests",
            0x801B,
            37,
            Tlv(0xA6, b"\x00\x01" + ruid + b"\x00"),
            Tlv(0xA6, b"\x00\x01" + ruid + b"\x00"),
        ),
        one("refused-scan-none", 0x801B, 38),
        one("refused-scan-truncated-radio", 0x801B, 39, Tlv(0xA6, b"\x00\x01" + ruid[:3])),
        one(
            "refused-scan-truncated-class",
            0x801B,
            40,
            Tlv(0xA6, b"\x00\x01" + ruid + b"\x01\x51\x05\x01"),
        ),
        one("refused-scan-trailing", 0x801B, 41, Tlv(0xA6, b"\x00\x01" + ruid + b"\x00\xff")),
        one(
            "scan-class-81-no-channel", 0x801B, 42, Tlv(0xA6, b"\x00\x01" + ruid + b"\x01\x51\x00")
        ),
        ("scan-repeated", [request(0x801B, 43, (Tlv(0xA6, b"\x00\x01" + ruid + b"\x00"),))] * 2),
        one(
            "refused-policy-multicast-station",
            0x8003,
            44,
            Tlv(0x89, b"\x01" + multicast + b"\x00\x00"),
        ),
        one(
            "refused-policy-duplicate-station",
            0x8003,
            45,
            Tlv(0x89, b"\x02" + mac(stations[0]) * 2 + b"\x00\x00"),
        ),
        one(
            "refused-policy-two-radios",
            0x8003,
            46,
            Tlv(0x89, b"\x00\x00\x02" + ruid + b"\x00\x3c\x78" + other + b"\x00\x3c\x78"),
        ),
        one(
            "refused-policy-two-steering",
            0x8003,
            47,
            Tlv(0x89, b"\x00\x00\x00"),
            Tlv(0x89, b"\x00\x00\x00"),
        ),
        one("refused-policy-metrics-truncated", 0x8003, 48, Tlv(0x8A, b"\x05\x01" + ruid)),
        one(
            "refused-policy-metrics-two-radios",
            0x8003,
            49,
            Tlv(
                0x8A,
                b"\x05\x02" + ruid + bytes.fromhex("78053cc0") + other + bytes.fromhex("78053cc0"),
            ),
        ),
        one("refused-policy-qos-reserved", 0x8003, 50, Tlv(0xDB, b"\x00\x00\x00")),
        one("refused-steering-none", 0x8014, 51),
        one("refused-steering-truncated", 0x8014, 52, Tlv(0x9B, bssid + b"\xe0")),
        one(
            "refused-steering-multicast-station",
            0x8014,
            53,
            steering_tlv([multicast], [(target, 81, 6)]),
        ),
        one(
            "refused-steering-duplicate-station",
            0x8014,
            54,
            steering_tlv([mac(stations[0])] * 2, [(target, 81, 6)]),
        ),
        one(
            "refused-steering-trailing",
            0x8014,
            55,
            Tlv(0x9B, steering_tlv([mac(stations[0])], [(target, 81, 6)]).value + b"\x00"),
        ),
        one(
            "steering-two-targets",
            0x8014,
            56,
            steering_tlv([mac(stations[0])], [(target, 81, 6), (target, 81, 1)]),
        ),
        one("refused-unassociated-none", 0x800F, 57),
        one(
            "refused-unassociated-truncated",
            0x800F,
            58,
            Tlv(0x97, bytes((81, 1, 6, 2)) + mac(stations[0])),
        ),
        one(
            "refused-unassociated-too-many",
            0x800F,
            59,
            Tlv(
                0x97,
                bytes((81, 1, 6, 65)) + b"".join(mac(f"02:00:00:00:b0:{i:02x}") for i in range(65)),
            ),
        ),
        (
            "refused-unbound-controller",  # a request from an AL the agent is not bound to
            [fragment_message(mac(AGENT), mac("02:00:00:e0:00:09"), 0x8003, 60, rdk_policy)[0]],
        ),
    ]
    measured = {"unassociated-query-measured"}
    cases = []
    for name, frames in sequences:
        with tempfile.TemporaryDirectory() as directory:
            source, _ = control_agent(copy.deepcopy(raw), stations)
            sent, handed = [], []
            mids = MidSequence(499)
            handlers = (
                ChannelCoordinator(
                    source,
                    sent.append,
                    ChannelPolicyStore(Path(directory) / "channel.sqlite"),
                    mids,
                    clock=lambda: 0.0,
                    utc=lambda: SCAN_TIMESTAMP,
                ),
                ReportingPolicyCoordinator(
                    source,
                    sent.append,
                    ReportingPolicyStore(Path(directory) / "policy.sqlite", boot_id="conformance"),
                    clock=lambda: 0.0,
                ),
                SteeringCoordinator(
                    source,
                    sent.append,
                    lambda r, mid, handed=handed: handed.append({"mid": mid, **r.record()}),
                    mids,
                    clock=lambda: 0.0,
                ),
                UnassociatedCoordinator(
                    source,
                    sent.append,
                    mids,
                    probes=RecordedProbes(PROBES) if name in measured else None,
                    clock=lambda: 0.0,
                    wall=lambda: PROBE_WALL,
                ),
            )
            steps = []
            for frame in frames:
                message, before = Reassembler().feed(frame), len(sent)
                try:
                    result = next(
                        (r for r in (h.handle(message, 0.0) for h in handlers) if r is not None),
                        None,
                    )
                except EmosaError as exc:  # refused: its reason, and what was sent before
                    result = exc.code.value
                steps.append(
                    {
                        "request": frame.hex(),
                        "expected": {"result": result, "frames": [f.hex() for f in sent[before:]]},
                    }
                )
            kept = handlers[0].store.read()
            kept.pop("context", None)
            for handler in handlers:
                handler.close()
            case = {
                "name": name,
                "steps": steps,
                "handed_to_pod": handed,
                "channel_policy": kept if kept["status"] == "accepted_no_adjustment" else None,
            }
            if name in measured:
                case["probes"] = PROBES
            cases.append(case)
    return {
        "description": "spec §2.4, §3.4, §3.7, §3.9: a provisioned agent over the recorded "
        "pod rows "
        "(stations " + ", ".join(stations) + " on 82:00:00:00:01:00, radio " + RUID + " on "
        "class 81 channel 6 at the pod's 30 dBm, max EIRP 30). Each step is a controller "
        "request frame and the frames the agent sends in answer, in order; the agent's own "
        "messages take MIDs from 500 and a Channel Scan Report carries scan_timestamp. "
        "'handed_to_pod' lists the steering mandates the agent carries out (spec §3.7), "
        "as decoded from the request; 'channel_policy' is the accepted channel policy's "
        "durable record (spec §3.4) without its context, or null when none was accepted. "
        "A case with 'probes' gives the pod's last probe "
        "request per station (spec §3.9); probe_wall is the agent's clock for their ages.",
        "agent": {
            "al_mac": AGENT,
            "controller_al": CONTROLLER,
            "radio": RUID,
            "first_mid": 500,
            "scan_timestamp": SCAN_TIMESTAMP,
            "probe_wall": PROBE_WALL,
        },
        "ovsdb_tables": raw,
        "cases": cases,
    }


def steering_vectors():
    from emosa.opensync.steering import SteeringBackend, SteeringIntent

    intent = SteeringIntent(
        "pod-1",
        "02:00:00:00:0a:00",
        "82:00:00:00:01:00",
        "02:00:00:12:75:2c",
        81,
        6,
        True,
        15,
        900,
    )
    cases = []
    for name, extra in (
        ("new-group-and-neighbor", {}),
        (
            "existing-group-and-neighbor",
            {
                "Band_Steering_Config": {
                    "00000000-0000-4000-8000-0000000000b1": {"if_name_2g": "home-ap-24"}
                },
                "Wifi_VIF_Neighbors": {
                    "00000000-0000-4000-8000-0000000000b2": {
                        "bssid": "02:00:00:12:75:2c",
                        "if_name": "home-ap-24",
                        "channel": 6,
                    }
                },
            },
        ),
    ):
        raw = {**pod_rows(), **copy.deepcopy(extra)}
        session = Recorder(copy.deepcopy(raw))
        backend = SteeringBackend("pod-1", session, serial=SERIAL)

        async def run(backend):
            await backend.snapshot()
            attempt = {"transaction_id": "conformance", "session_generation": 1}
            result = await backend.submit(intent, attempt)
            created = result.evidence.get("created", {})
            await backend.kick(intent.station, created["client"])
            await backend.close(intent, created)
            return result

        result = asyncio.run(run(backend))
        opened, kicked, closed = session.sent
        cases.append(
            {
                "name": name,
                "ovsdb_tables": raw,
                "expected": {
                    "status": result.status,
                    "open": opened,
                    "kick": kicked,
                    "close": closed,
                },
            }
        )
    return {
        "description": "spec §3.7: the steering window on the pod for one mandate, as OVSDB "
        "transactions: open (guarded; group and neighbor reused when present, inserted when "
        "absent), the directed kick once owm steers, and the close deleting exactly what the "
        "open inserted. The server's reply to every insert is the UUID "
        "00000000-0000-4000-8000-0000000000ff.",
        "intent": dataclasses.asdict(intent),
        "cases": cases,
    }


WSC_FIXTURE = ROOT / "tests" / "fixtures" / "protocol" / "wsc-messages" / "vectors.json"


def onboarding_vectors():
    """Search, Response admission, M1 and M2 around the independent WSC payload fixture."""
    from contextlib import ExitStack
    from unittest import mock

    from cryptography.hazmat.primitives.asymmetric import dh

    from emosa import wsc_messages
    from emosa.easymesh_payloads import (
        APRadioAdvancedCapabilities,
        APRadioBasicCapabilities,
        BasicOperatingClass,
        Profile2APCapability,
    )
    from emosa.wire.autoconfiguration import DiscoveryExchange, WscExchange, parse_response
    from emosa.wire.cmdu import MidSequence, Reassembler, Tlv, fragment_message
    from emosa.wsc import MODP_1536, KeyPair
    from emosa.wsc_messages import M1Device

    fixture = json.loads(WSC_FIXTURE.read_text())["cases"][0]
    local, controller = bytes.fromhex("020000000001"), bytes.fromhex("020000000002")
    ruid = bytes.fromhex("020000001001")
    device = M1Device(
        uuid=bytes(range(0x40, 0x50)),
        al_mac=local,
        authentication_types=0x23,
        encryption_types=0x0D,
        connection_types=1,
        configuration_methods=0x0280,
        wps_state=2,
        manufacturer=b"EMOSA synthetic laboratory",
        model_name=b"Payload reference",
        model_number=b"1",
        serial_number=b"public-vector-1",
        primary_device_type=bytes.fromhex("00060050f2040001"),
        device_name=b"Synthetic represented AP",
        rf_band=1,
        association_state=0,
        device_password_id=4,
        configuration_error=0,
        os_version=1,
    )
    basic = APRadioBasicCapabilities(ruid, 2, (BasicOperatingClass(81, 20, ()),))
    profile2 = Profile2APCapability(0, 0, 0, 0)
    advanced = APRadioAdvancedCapabilities(ruid, 0)
    binding = PeerBinding("conformance", 1, local, controller, (controller,))

    def pair():
        parameters = dh.DHParameterNumbers(MODP_1536, 2, (MODP_1536 - 1) // 2)
        public = dh.DHPublicNumbers(
            int.from_bytes(bytes.fromhex(fixture["enrollee_public"]), "big"), parameters
        )
        private = int.from_bytes(bytes.fromhex(fixture["enrollee_private"]), "big")
        return KeyPair(dh.DHPrivateNumbers(private, public).private_key())

    def feed(frames):
        parser, result = Reassembler(), None
        for frame in frames:
            result = parser.feed(frame)
        return result

    cases = []
    for message_set in (EASYMESH_61, R1):
        with ExitStack() as stack:
            # The fixture's public synthetic entropy: its DH key, enrollee nonce 00..0f.
            stack.enter_context(mock.patch.object(KeyPair, "generate", pair))
            stack.enter_context(
                mock.patch.object(wsc_messages.secrets, "token_bytes", lambda n: bytes(range(n)))
            )
            mids = MidSequence(65534)
            discovery = DiscoveryExchange(
                binding, band=0, profile=1, profile2=profile2, mids=mids, message_set=message_set
            )
            search = discovery.request()
            response_tlvs = (
                Tlv(0x0F, b"\0"),
                Tlv(0x10, b"\0"),
                Tlv(0x80, b"\x01\0"),
                Tlv(0xB3, b"\x01"),
            )
            response = fragment_message(local, controller, 0x0008, 65535, response_tlvs)
            admitted = discovery.receive(feed(response), ingress="conformance", generation=1)
            exchange = WscExchange(
                binding, device, basic, profile2, advanced, mids=mids, message_set=message_set
            )
            m1 = exchange.request()
            m2 = fragment_message(
                local,
                controller,
                0x0009,
                321,
                (Tlv(0x82, ruid), Tlv(0x11, bytes.fromhex(fixture["m2"]))),
            )
            result = exchange.receive(feed(m2), ingress="conformance", generation=1)
            tampered_value = bytearray(bytes.fromhex(fixture["m2"]))
            tampered_value[-1] ^= 1  # the Authenticator's last octet
            tampered = fragment_message(
                local, controller, 0x0009, 322, (Tlv(0x82, ruid), Tlv(0x11, bytes(tampered_value)))
            )
            second = WscExchange(
                binding,
                device,
                basic,
                profile2,
                advanced,
                mids=MidSequence(99),
                message_set=message_set,
            )
            second.request()
            try:
                second.receive(feed(tampered), ingress="conformance", generation=1)
                rejected = None
            except EmosaError as exc:
                rejected = str(exc.code)
        candidate = result.candidate
        cases.append(
            {
                "message_set": message_set,
                "search_frames": [f.hex() for f in search],
                "response_frames": [f.hex() for f in response],
                "expected_admission": plain(parse_response(feed(response), message_set=message_set))
                | {"admitted": admitted is not None},
                "m1_frames": [f.hex() for f in m1],
                "m2_frames": [f.hex() for f in m2],
                "expected_m2": {
                    "ruid": result.ruid.hex(":"),
                    "ssid": candidate.ssid,
                    "passphrase": candidate.passphrase,
                    "bss_index": candidate.bss_index,
                },
                "tampered_m2_frames": [f.hex() for f in tampered],
                "expected_tampered": {"error": rejected},
            }
        )
    # The session's admission of a Response (non_dpp_admission): Responses that are
    # dropped (a band or profile that does not match the Search), and the issues that
    # make a session incompatible, for both message sets.
    from emosa.wire.autoconfiguration import EASYMESH_61 as SET_61
    from emosa.wire.onboarding import non_dpp_admission

    base = (Tlv(0x0F, b"\0"), Tlv(0x10, b"\0"), Tlv(0x80, b"\x01\0"))
    variants = {
        "no_controller_capability": (*base, Tlv(0xB3, b"\x01")),
        "controller_capability": (*base, Tlv(0xB3, b"\x01"), Tlv(0xDD, b"\xc0")),
        "kib_mib_only": (*base, Tlv(0xB3, b"\x01"), Tlv(0xDD, b"\x80")),
        "early_ap_capability_only": (*base, Tlv(0xB3, b"\x01"), Tlv(0xDD, b"\x40")),
        "security_capability_zero": (
            *base,
            Tlv(0xB3, b"\x01"),
            Tlv(0xDD, b"\xc0"),
            Tlv(0xA9, bytes(3)),
        ),
        "security_capability_reserved": (
            *base,
            Tlv(0xB3, b"\x01"),
            Tlv(0xDD, b"\xc0"),
            Tlv(0xA9, b"\0\0\x01"),
        ),
        "security_capability_short": (
            *base,
            Tlv(0xB3, b"\x01"),
            Tlv(0xDD, b"\xc0"),
            Tlv(0xA9, bytes(2)),
        ),
        "profile_2": (*base, Tlv(0xB3, b"\x02"), Tlv(0xDD, b"\xc0")),
        "profile_reserved_4": (*base, Tlv(0xB3, b"\x04"), Tlv(0xDD, b"\xc0")),
        "band_5ghz": (
            Tlv(0x0F, b"\0"),
            Tlv(0x10, b"\x01"),
            Tlv(0x80, b"\x01\0"),
            Tlv(0xB3, b"\x01"),
            Tlv(0xDD, b"\xc0"),
        ),
        "no_profile": base,
    }
    admission = []
    for message_set in (SET_61, R1):
        for name, tlvs in variants.items():
            discovery = DiscoveryExchange(
                binding,
                band=0,
                profile=1,
                profile2=profile2,
                mids=MidSequence(65534),
                message_set=message_set,
            )
            discovery.request()
            response = fragment_message(local, controller, 0x0008, 65535, tlvs)
            try:
                advertisement = discovery.receive(
                    feed(response), ingress="conformance", generation=1
                )
                issues = non_dpp_admission(advertisement, message_set=message_set)
                expected = {"dropped": False, "issues": list(issues)}
            except EmosaError:
                expected = {"dropped": True}
            admission.append(
                {
                    "name": name,
                    "message_set": message_set,
                    "response_frames": [f.hex() for f in response],
                    "expected": expected,
                }
            )
    return {
        "description": "spec §2.5: the agent's Search, a controller Response and its admission, "
        "the agent's M1, and a controller M2 decrypted to the BSS settings, for each message "
        "set. The WSC payloads are the independent synthetic fixture "
        "(tests/fixtures/protocol/wsc-messages): public values, including its DH key pair and "
        "the enrollee nonce 000102..0f the agent must use for these vectors. A real exchange "
        "never uses fixed entropy. The M2 with a changed Authenticator is rejected.",
        "agent": {
            "al_mac": local.hex(":"),
            "controller_al": controller.hex(":"),
            "first_mid": 65535,
            "radio": {
                "ruid": ruid.hex(":"),
                "max_bss": 2,
                "operating_classes": [[81, 20, []]],
                "advanced_flags": 0,
            },
            "profile2_ap_capability": encode_value(profile2).hex(),
            "search": {"band": 0, "profile": 1},
            "m1_device": plain(device),
            "enrollee_private": fixture["enrollee_private"],
        },
        "cases": cases,
        "admission": admission,
    }


STATS_RECORDED = ROOT / "tests" / "fixtures" / "opensync" / "pod-6.6.1-hwsim-client-stats.hex"


def survey_report(timestamp_ms, *, channel=6, busy=41):
    """A raw on-channel survey publish (spec §3.8), built from the pinned schema."""
    from emosa.opensync.stats import Report

    report = Report(nodeID="")
    survey = report.survey.add(band=0, survey_type=0, timestamp_ms=timestamp_ms)
    # sampled 1.5 s before the report's time (offset_ms = report time - sample time)
    sample = survey.survey_list.add(duration_ms=5000, busy=busy, offset_ms=1500)
    if channel is not None:
        sample.channel = channel
    return report.SerializePartialToString().hex()


def bs_report(timestamp_ms, clients, *, complete=True):
    """A band-steering publish (spec §3.9): clients as (mac, ((band, ifname, events), ...)),
    events as (type, offset_ms, rssi or None). ``complete=False`` drops a required
    event field (offset_ms)."""
    from emosa.opensync.stats import Report

    report = Report(nodeID="")
    bs = report.bs_report.add(timestamp_ms=timestamp_ms)
    for station, bands in clients:
        client = bs.clients.add(mac_address=station)
        for band, ifname, events in bands:
            entry = client.bs_band_report.add(band=band)
            if ifname is not None:
                entry.ifname = ifname
            for kind, offset_ms, rssi in events:
                event = entry.event_list.add(type=kind)
                if complete:
                    event.offset_ms = offset_ms
                if rssi is not None:
                    event.rssi = rssi
    return report.SerializePartialToString().hex()


def telemetry_vectors():
    from emosa.opensync.stats import PodStats

    topic = f"emosa/stats/{SERIAL}"
    publishes = [
        (line.split()[0], line.split()[1]) for line in STATS_RECORDED.read_text().splitlines()
    ]
    now = 1790313745.0  # the capture's time, fixed: station lifetimes are measured from it
    stats = PodStats(topic, interval=10, clock=lambda: now)
    steps = []
    inputs = [(t, d, False) for t, d in publishes] + [
        (publishes[1][0], publishes[1][1], False),  # the second again: out of order
        (publishes[2][0], publishes[2][1], True),  # retained
        ("emosa/stats/ANOTHERPOD", publishes[2][1], False),  # another pod's topic
        (topic, survey_report(int(now * 1000) - 2000), False),  # a survey sample: kept
        (topic, survey_report(int(now * 1000), channel=None), False),  # no channel: incomplete
        # probe requests (spec §3.9): a 2.4 GHz probe and a connect event of one station,
        # a 5 GHz probe without its VIF, a probe without RSSI, and an unusable MAC
        (
            topic,
            bs_report(
                int(now * 1000) - 1000,
                [
                    ("02:00:00:00:99:99", ((0, "wl0.1", ((0, 1500, 34), (1, 1200, None))),)),
                    ("02:00:00:00:95:95", ((1, None, ((0, 500, 40),)),)),
                    ("02:00:00:00:94:94", ((0, "wl0.1", ((0, 800, None),)),)),
                    ("02:00:00:00:93", ((0, "wl0.1", ((0, 800, 20),)),)),
                ],
            ),
            False,
        ),
        # an older probe of the first station is not kept; a newer one of the second is,
        # upper-case MAC and all
        (
            topic,
            bs_report(
                int(now * 1000),
                [
                    ("02:00:00:00:99:99", ((0, "wl0.1", ((0, 9000, 20),)),)),
                    ("02:00:00:00:95:95".upper(), ((1, "wl1.1", ((0, 100, 42),)),)),
                ],
            ),
            False,
        ),
        # an event without its required offset: incomplete
        (
            topic,
            bs_report(
                int(now * 1000),
                [("02:00:00:00:92:92", ((0, "wl0.1", ((0, 0, 30),)),))],
                complete=False,
            ),
            False,
        ),
    ]
    for t, data, retained in inputs:
        accepted = stats.receive(t, bytes.fromhex(data), retained=retained)
        status = stats.status()
        status.pop("last_report_at")
        steps.append(
            {
                "topic": t,
                "payload": data,
                "retained": retained,
                "expected": {"accepted": bool(accepted), "status": plain(status)},
            }
        )
    return {
        "description": "spec §3.6: the pod's statistics as the agent keeps them. Three "
        "sts.Report publishes recorded from an opensync-lab pod (OpenSync 6.6.1.0, raw client "
        "reports every 10 s, topic " + topic + "), then a repeated, a retained and a foreign "
        "publish, then a raw on-channel survey (spec §3.8) and one lacking its required "
        "channel, then band-steering reports with probe requests (spec §3.9), the last "
        "lacking an event's required offset; the clock stands at "
        + str(now)
        + " throughout. Per step: whether the "
        "report is used, and the agent's statistics status after it.",
        "topic": topic,
        "reporting_interval": 10,
        "clock": now,
        "steps": steps,
    }


def client_report(timestamp_ms, clients, *, channel=6):
    """A raw client report publish (spec §3.6): clients as (mac, rssi, tx_rate, rx_rate,
    counters or None), each connected for the whole period."""
    from emosa.opensync.stats import Report

    report = Report(nodeID="")
    radio = report.clients.add(band=0, channel=channel, timestamp_ms=timestamp_ms)
    for station, rssi, tx_rate, rx_rate, counters in clients:
        client = radio.client_list.add(
            mac_address=station, ssid="private_ssid", connected=True, duration_ms=5000
        )
        client.stats.rssi = rssi
        client.stats.tx_rate = tx_rate
        client.stats.rx_rate = rx_rate
        for name, value in (counters or {}).items():
            setattr(client.stats, name, value)
    return report.SerializePartialToString().hex()


METRICS_WALL = 1790313745.0  # the agent's wall clock for statistic ages (epoch seconds), fixed
COUNTERS_FULL = {
    "tx_bytes": 123456,
    "rx_bytes": 65432,
    "tx_frames": 1000,
    "rx_frames": 800,
    "tx_retries": 12,
    "rx_retries": 3,
    "tx_errors": 2,
    "rx_errors": 1,
}


def metrics_vectors():
    """The Multi-AP Policy kept, and AP Metrics Responses from the pod's statistics."""
    from emosa.opensync.stats import PodStats
    from emosa.wire.channel import OperatingRadio
    from emosa.wire.cmdu import MidSequence, Reassembler, Tlv, fragment_message
    from emosa.wire.coordinator import ReportSource
    from emosa.wire.pod_metrics import PodMetricReporter
    from emosa.wire.reporting_policy import ReportingPolicyCoordinator, ReportingPolicyStore

    raw = pod_rows()
    stations = ["02:00:00:00:0a:00", "02:00:00:00:10:00"]
    bssid = mac("82:00:00:00:01:00")
    ruid = mac(RUID)
    topic = f"emosa/stats/{SERIAL}"
    wall = METRICS_WALL

    def request(message_type, mid, items):
        return fragment_message(mac(AGENT), mac(CONTROLLER), message_type, mid, items)[0]

    def query(mid, bssids):
        return request(0x800B, mid, (Tlv(0x93, bytes([len(bssids)]) + b"".join(bssids)),))

    # RDK's Metric Reporting Policy (captured): 5 s, RCPI 0x78, hysteresis 5, utilization
    # 0x3c, traffic and link metrics included; its other companions are kept, not applied
    rdk_like = (
        Tlv(0x89, bytes.fromhex("000001") + ruid + bytes.fromhex("023c78")),
        Tlv(0x8A, bytes.fromhex("0501") + ruid + bytes.fromhex("78053cc0")),
        Tlv(0x0B, bytes.fromhex("d89c8e00")),
    )
    policy = request(0x8003, 14, rdk_like)
    survey = survey_report(int(wall * 1000) - 2000)
    clients = client_report(
        int(wall * 1000) - 1000,
        [
            (stations[0], 40, 144.4, 72.2, COUNTERS_FULL),
            (stations[1], 30, 65.0, 58.5, None),
            ("02:00:00:00:77:77", 25, 6.0, 6.0, None),  # measured, not on the pod's BSS
        ],
    )
    old_survey = survey_report(int(wall * 1000) - 60000)
    bad_rates = client_report(
        int(wall * 1000) - 1000,
        [
            (stations[0], 400, float("inf"), -5.0, COUNTERS_FULL),
            (stations[1], 30, float("nan"), 2.0**33, None),
        ],
    )
    cases = [
        (
            "policy-then-query",
            [survey, clients],
            [(0.0, "frame", policy), (0.0, "frame", query(30, [bssid]))],
        ),
        (
            "query-unknown-bss",
            [survey, clients],
            [(0.0, "frame", query(31, [mac("02:00:00:00:66:66")]))],
        ),
        ("query-all-bss", [survey, clients], [(0.0, "frame", query(32, []))]),
        (
            # due every 5 s: one report per due tick, and one (not three) after missed periods
            "periodic",
            [survey, clients],
            [
                (0.0, "frame", policy),
                (4.9, "tick", None),
                (5.0, "tick", None),
                (17.5, "tick", None),
            ],
        ),
        (
            # the survey is older than three reporting periods and the publish interval
            "stale-survey",
            [old_survey, clients],
            [(0.0, "frame", policy), (0.0, "frame", query(33, [bssid])), (5.0, "tick", None)],
        ),
        (
            # rates that cannot be measurements (infinite, negative, NaN, beyond the TLV's
            # four octets) are absent, and an SNR beyond the RCPI's range gives 220 (8.4)
            "rates-not-measurements",
            [survey, bad_rates],
            [(0.0, "frame", policy), (0.0, "frame", query(35, [bssid]))],
        ),
        (
            "no-statistics",
            [],
            [(0.0, "frame", policy), (0.0, "frame", query(34, [bssid])), (5.0, "tick", None)],
        ),
        (
            # a new session on the kept policy (spec 3.8): the schedule starts again, its
            # first report one interval after the session's start; no tick writes the record
            "new-session",
            [survey, clients],
            [
                (0.0, "frame", policy),
                (5.0, "tick", None),
                (7.0, "session", None),
                (10.0, "tick", None),
                (12.0, "tick", None),
            ],
        ),
        (
            # a record kept for another radio (a pod recreated with another radio, rdk-1004,
            # 7 October): never reported on, and the next policy received replaces it whole
            "stored-policy-of-another-radio",
            [survey, clients],
            [
                (0.0, "tick", None),
                (5.0, "tick", None),
                (6.0, "frame", policy),
                (11.0, "tick", None),
            ],
            {
                "identity": {
                    "controller": mac(CONTROLLER).hex(),
                    "local_al": mac(AGENT).hex(),
                    "ruid": "020000000900",
                },
                "policy": {"metrics": {"interval_seconds": 5, "radios": []}},
                "latest_mid": 9,
                "latest_request": [],
                "receipt_count": 3,
                "next_due": 5.0,
                "periods_due_without_report": 0,
                "last_unfulfilled_due": None,
                "boot_id": "conformance",
                "schedule_rebases": 2,
            },
        ),
        (
            # the same policy again (a new MID) does not postpone the due report
            "policy-redelivered",
            [survey, clients],
            [
                (0.0, "frame", policy),
                (3.0, "frame", request(0x8003, 15, Reassembler().feed(policy).tlvs)),
                (5.0, "tick", None),
            ],
        ),
    ]
    other = mac("02:00:00:00:99:00")  # another agent's radio
    multicast = bytes.fromhex("01005e000001")

    def refused(name, mid, *tlvs, controller=CONTROLLER):
        frame = fragment_message(mac(AGENT), mac(controller), 0x8003, mid, tlvs)[0]
        return (name, [survey, clients], [(0.0, "frame", frame)])

    # what the policy and query parsers refuse, one case each (plan 8.4)
    cases += [
        refused(
            "refused-policy-multicast-disallowed", 40, Tlv(0x89, b"\x01" + multicast + b"\x00\x00")
        ),
        refused(
            "refused-policy-duplicate-disallowed",
            41,
            Tlv(0x89, b"\x00\x02" + mac(stations[0]) * 2 + b"\x00"),
        ),
        refused(
            "refused-policy-steering-foreign-radio",
            42,
            Tlv(0x89, b"\x00\x00\x01" + other + bytes.fromhex("023c78")),
        ),
        refused(
            "refused-policy-steering-reserved-mode",
            43,
            Tlv(0x89, b"\x00\x00\x01" + ruid + bytes.fromhex("073c78")),
        ),
        refused(
            "refused-policy-metrics-reserved-threshold",
            44,
            Tlv(0x8A, b"\x05\x01" + ruid + bytes.fromhex("ff053cc0")),
        ),
        refused(
            "refused-policy-qos-reserved",
            45,
            Tlv(0xDB, b"\x00\x00" + bytes(19)),
        ),
        refused(
            "policy-qos-lists",
            46,
            Tlv(0xDB, b"\x01" + mac(stations[0]) + b"\x01" + mac(stations[1]) + bytes(20)),
        ),
        refused("refused-policy-unbound-controller", 47, *rdk_like, controller="02:00:00:e0:00:09"),
        (
            "refused-policy-mid-reused",  # the same MID with other contents
            [survey, clients],
            [
                (0.0, "frame", policy),
                (
                    0.5,
                    "frame",
                    fragment_message(
                        mac(AGENT), mac(CONTROLLER), 0x8003, 14, (Tlv(0x8A, b"\x0a\x00"),)
                    )[0],
                ),
            ],
        ),
        (
            "refused-query-two-tlvs",
            [survey, clients],
            [
                (
                    0.0,
                    "frame",
                    request(
                        0x800B,
                        48,
                        (Tlv(0x93, b"\x01" + bssid), Tlv(0x93, b"\x01" + bssid)),
                    ),
                )
            ],
        ),
        (
            "refused-query-length",
            [survey, clients],
            [(0.0, "frame", request(0x800B, 49, (Tlv(0x93, b"\x02" + bssid),)))],
        ),
    ]
    view = device_view(decode(raw))
    radio = view.radio(ruid)
    caps = radio_capabilities(radio, channel=6, max_bss=5, max_eirp=30)
    facts = topology(
        agent_al=mac(AGENT),
        controller_al=mac(CONTROLLER),
        radio=radio,
        channel=6,
        bsses=radio.bsses,
        ages={mac(m): 10 for m in stations},
    )
    binding = PeerBinding("conformance", 1, mac(AGENT), mac(CONTROLLER), (mac(CONTROLLER),))
    out = []
    for name, publishes, steps, *kept in cases:
        stored_before = kept[0] if kept else None  # a record on disk when the case starts
        now = [0.0]
        clock = lambda now=now: now[0]  # noqa: E731
        # the report source is refreshed at every step's time, as the agent refreshes it
        source = ReportSource(binding, "pod-1", "c" * 64, clock=clock)
        stats = PodStats(topic, interval=5, clock=lambda: wall)
        for payload in publishes:
            stats.receive(topic, bytes.fromhex(payload))
        sent = []
        mids = MidSequence(499)
        with tempfile.TemporaryDirectory() as directory:
            store = ReportingPolicyStore(Path(directory) / "policy.sqlite", boot_id="conformance")
            if stored_before is not None:
                store.save(stored_before)

            def session(source=source, stats=stats, sent=sent, mids=mids, clock=clock, store=store):
                """The reporter and the policy coordinator of one session, as the agent
                creates them at each session's start."""
                holder = {}
                reporter = PodMetricReporter(
                    source,
                    stats,
                    sent.append,
                    mids,
                    admitted=lambda: True,
                    policy=lambda: (
                        holder["policy"].value["policy"] if holder["policy"].value else {}
                    ),
                    esp_be=bytes.fromhex("3fff00"),
                    freshness=3 * 5 + 5,
                    clock=clock,
                    wall=lambda: wall,
                )
                holder["policy"] = ReportingPolicyCoordinator(
                    source, sent.append, store, reporter=reporter, clock=clock
                )
                return reporter, holder["policy"]

            reporter, coordinator = session()
            recorded = []
            for index, (at, kind, frame) in enumerate(steps):
                now[0] = at
                source.publish(
                    (1, 1 + index),
                    caps,
                    facts,
                    observed_at=at,
                    lifetime=1.5,
                    operating_radios=(OperatingRadio(ruid, 81, 6, radio.tx_power),),
                )
                before = len(sent)
                if kind == "tick":
                    coordinator.tick()
                    result = None
                elif kind == "session":  # a new session (or agent start) on the kept record
                    coordinator.close()
                    reporter, coordinator = session()
                    result = None
                else:
                    message = Reassembler().feed(frame)
                    try:
                        result = coordinator.handle(message, at)
                        if result is None:
                            result = reporter.handle(
                                message, at, ingress="conformance", generation=1
                            )
                    except EmosaError as exc:  # refused: its reason, and what was sent before
                        result = exc.code.value
                recorded.append(
                    {
                        "at": at,
                        **(
                            {"request": frame.hex()}
                            if frame is not None
                            else {"session": True}
                            if kind == "session"
                            else {"tick": True}
                        ),
                        "expected": {"result": result, "frames": [f.hex() for f in sent[before:]]},
                    }
                )
            value = coordinator.value
            stored = store.read()  # written only when a policy was received
            coordinator.close()
            store.close()
        out.append(
            {
                "name": name,
                **({"stored": plain(stored_before)} if stored_before is not None else {}),
                "publishes": publishes,
                "steps": recorded,
                "expected_counts": {
                    "policy": dict(coordinator.counts),
                    "reporter": dict(reporter.counts),
                },
                "expected_policy": plain(
                    {k: v for k, v in (value or {}).items() if k not in ("boot_id",)}
                )
                if value
                else None,
                "expected_stored": plain(
                    {k: v for k, v in (stored or {}).items() if k not in ("boot_id",)}
                )
                if stored
                else None,
            }
        )
    return {
        "description": "spec §2.4, §3.8: the Multi-AP Policy kept and the AP metrics reported "
        "from the pod's own statistics. The recorded pod rows (stations "
        + ", ".join(stations)
        + " on 82:00:00:00:01:00, radio "
        + RUID
        + "); the pod's statistics from the case's "
        "publishes (topic " + topic + ", reporting every 5 s) at the wall clock; the declared "
        "best-effort ESP 3fff00; a statistic is fresh for 20 s (three periods and the 5 s "
        "publish interval). Each step is a controller frame, a tick, or a new session "
        "('session': the agent's policy coordinator started again on the kept record, as a "
        "new session or a start of the agent does) at a time 'at' (seconds): the agent's "
        "result and the frames it sends; its own messages take MIDs from 500. "
        "expected_policy is the agent's record at the end, expected_stored the record kept "
        "on disk, written only when a policy is received (both without the boot identity). "
        "A case's 'stored', when present, is the record on disk before its first step.",
        "agent": {
            "al_mac": AGENT,
            "controller_al": CONTROLLER,
            "radio": RUID,
            "first_mid": 500,
            "wall": wall,
            "topic": topic,
            "reporting_interval": 5,
            "freshness": 20,
            "esp_be": "3fff00",
        },
        "ovsdb_tables": raw,
        "stations": stations,
        "cases": out,
    }


def backhaul_steering_vectors():
    """Backhaul Steering (spec §9): the move handed to the uplink scope and answered."""
    from emosa.wire.backhaul_steering import BackhaulSteeringCoordinator
    from emosa.wire.cmdu import Reassembler, Tlv, fragment_message
    from emosa.wire.coordinator import ReportSource

    raw = json.loads(UPLINK_ROWS["multi-ap"].read_text())["tables"]
    station = "bhaul-sta-24"
    rows = decode(raw)
    view = device_view(rows)
    link = backhaul(view, station)
    radio = view.radio(link.ruid)
    caps = radio_capabilities(radio, channel=radio.channel, max_bss=5, max_eirp=30)
    facts = topology(
        agent_al=mac(AGENT),
        controller_al=mac(CONTROLLER),
        radio=radio,
        channel=radio.channel,
        bsses=radio.bsses,
        ages={m: 0 for b in radio.bsses for m in b.stations},
        uplink=link,
    )
    sta = facts.backhaul_stations[0][1]
    target = mac("02:00:00:00:09:00")
    binding = PeerBinding("conformance", 1, mac(AGENT), mac(CONTROLLER), (mac(CONTROLLER),))

    def request(mid, station_mac=sta, bssid=target):
        value = station_mac + bssid + bytes((115, 36))
        return fragment_message(mac(AGENT), mac(CONTROLLER), 0x8019, mid, (Tlv(0x9E, value),))[0]

    other = mac("02:00:00:00:77:77")
    multicast = bytes.fromhex("030000000001")
    # a step: (session, at, kind, detail); kind "frame" (detail: request frame, executor's
    # answer), "tick" (detail: the move's outcome, whether the pod's State is available),
    # "close" (the session ends)
    cases = [
        (
            "move-succeeds",
            [
                (0, 0.0, "frame", (request(40), None)),
                (0, 1.0, "tick", (None, True)),
                (0, 20.0, "tick", (True, True)),
            ],
        ),
        (
            "move-fails",
            [
                (0, 0.0, "frame", (request(41), None)),
                (0, 30.0, "tick", ("not confirmed within the deadline", True)),
            ],
        ),
        ("not-the-backhaul-station", [(0, 0.0, "frame", (request(42, station_mac=other), None))]),
        ("multicast-target", [(0, 0.0, "frame", (request(43, bssid=multicast), None))]),
        ("executor-refuses", [(0, 0.0, "frame", (request(44), "held_on_option_2"))]),
        (
            "move-in-progress",
            [(0, 0.0, "frame", (request(45), None)), (0, 1.0, "frame", (request(46), None))],
        ),
        (
            "repeated-request",
            [
                (0, 0.0, "frame", (request(47), None)),
                (0, 0.5, "frame", (request(47), None)),
                (0, 10.0, "tick", (True, True)),
                (0, 11.0, "frame", (request(47), None)),
            ],
        ),
        (
            "deadline",
            [
                (0, 0.0, "frame", (request(48), None)),
                (0, 119.0, "tick", (None, True)),
                (0, 120.0, "tick", (None, True)),
            ],
        ),
        # the move changes the pod's topology and the agent renews its session: the next
        # session answers the move the previous one began
        (
            "answered-by-next-session",
            [
                (0, 0.0, "frame", (request(49), None)),
                (0, 2.0, "close", None),
                (1, 8.0, "frame", (request(50), None)),
                (1, 9.0, "tick", (True, True)),
            ],
        ),
        # the pod's State is away when the outcome is known: answered on a later tick,
        # unless it stays away past the deadline and 30 s
        (
            "source-away",
            [
                (0, 0.0, "frame", (request(51), None)),
                (0, 5.0, "tick", (True, False)),
                (0, 6.0, "tick", (True, True)),
            ],
        ),
        (
            "source-away-too-long",
            [
                (0, 0.0, "frame", (request(52), None)),
                (0, 120.0, "tick", (None, False)),
                (0, 151.0, "tick", (None, False)),
            ],
        ),
    ]
    out = []
    for name, steps in cases:
        now = [0.0]
        source = ReportSource(binding, "pod-1", "c" * 64, clock=lambda now=now: now[0])
        shared, sessions, sent, handed = {}, {}, [], []
        state = {"reply": None, "outcome": None}
        recorded = []
        for index, (session, at, kind, detail) in enumerate(steps):
            now[0] = at
            available = kind != "tick" or detail[1]
            if available:
                source.publish((1, 1 + index), caps, facts, observed_at=at, lifetime=1.5)
            else:
                source.invalidate()
            if session not in sessions:
                sessions[session] = BackhaulSteeringCoordinator(
                    source,
                    sent.append,
                    lambda bssid, handed=handed, state=state: (
                        handed.append(bssid),
                        state["reply"],
                    )[1],
                    lambda bssid, state=state: state["outcome"],
                    clock=lambda now=now: now[0],
                    shared=shared,
                )
            coordinator, before = sessions[session], len(sent)
            step = {"session": session, "at": at}
            if kind == "close":
                coordinator.close()
                step["close"] = True
                result = None
            elif kind == "tick":
                state["outcome"] = detail[0]
                result = coordinator.tick()
                step.update({"tick": True, "outcome": detail[0], "source_available": detail[1]})
            else:
                frame, reply = detail
                state["reply"] = reply
                result = coordinator.handle(Reassembler().feed(frame), at)
                step.update({"request": frame.hex(), "executor": reply})
            step["expected"] = {"result": result, "frames": [f.hex() for f in sent[before:]]}
            recorded.append(step)
        last = max(sessions)
        out.append(
            {
                "name": name,
                "steps": recorded,
                "handed_to_uplink": handed,
                "expected_status": plain(sessions[last].status()),
            }
        )
    return {
        "description": "spec §8.3: Backhaul Steering. The recorded pod rows with a Multi-AP "
        "backhaul station (" + station + ", " + sta.hex(":") + "), so the agent reports it; the "
        "controller's Backhaul Steering Requests, and ticks. A step names its session (a "
        "renewed session shares the move under way); a request step gives the uplink scope's "
        "answer to the hand-over ('executor': null, or why not), a tick step the move's "
        "outcome (null under way, true applied, or why it failed) and whether the pod's State "
        "is available. Per step: the result and the frames sent. handed_to_uplink lists the "
        "moves handed over (target BSSIDs); expected_status is the last session's status.",
        "agent": {"al_mac": AGENT, "controller_al": CONTROLLER},
        "backhaul_stations": [s.hex(":") for _, s in facts.backhaul_stations],
        "ovsdb_tables": raw,
        "cases": out,
    }


def scope_write_case(name, raw, backend, intent):
    """A scope's plan and its one guarded transaction on the recorded rows."""

    async def run():
        try:
            await backend.snapshot()  # the AP scope binds the pod here, and may refuse
            await backend.plan(intent)
        except EmosaError as exc:
            return None, exc.code.value
        attempt = {"attempt_id": "a", "transaction_id": "t", "session_generation": 1}
        return (await backend.submit(intent, attempt)).status, None

    status, refusal = asyncio.run(run())
    expected = {"status": status, "transactions": backend.session.sent}
    if refusal:
        expected["refusal"] = refusal
    return {"name": name, "ovsdb_tables": raw, "intent": intent.record(), "expected": expected}


def scope_writes_vectors():
    """The telemetry scope, the wired uplink, the probe watch and a steering window over a watch
    row."""
    from emosa.opensync.probe_watch import WatchBackend, WatchIntent
    from emosa.opensync.steering import SteeringBackend, SteeringIntent
    from emosa.opensync.telemetry import TelemetryBackend, TelemetryIntent

    def rows(**extra):
        return {**pod_rows(), **copy.deepcopy(extra)}

    telemetry = TelemetryIntent(
        "pod-1", "10.101.0.40", 8883, f"emosa/stats/{SERIAL}", "2.4G", 5, 5, 5, True
    )
    node_uuid, node = next(iter(pod_rows()["AWLAN_Node"].items()))
    ours = {
        "Wifi_Stats_Config": {
            "00000000-0000-4000-8000-0000000000c1": {
                "stats_type": "client",
                "radio_type": "2.4G",
                "report_type": "raw",
                "reporting_interval": 5,
                "sampling_interval": 5,
            },
            "00000000-0000-4000-8000-0000000000c2": {
                "stats_type": "survey",
                "radio_type": "2.4G",
                "survey_type": "on-chan",
                "report_type": "raw",
                "reporting_interval": 5,
                "sampling_interval": 5,
            },
        },
    }
    cloud = {
        "AWLAN_Node": {
            node_uuid: {
                **node,
                "mqtt_settings": ["map", [["broker", "cloud.example"], ["port", "443"]]],
            }
        }
    }
    watch_marker = ["map", [["emosa", "watch"]]]
    s1, s2 = "02:00:00:00:99:99", "02:00:00:00:98:98"
    group = {
        "Band_Steering_Config": {
            "00000000-0000-4000-8000-0000000000b1": {"if_name_2g": "home-ap-24"}
        }
    }
    watching_s1 = {
        **group,
        "Band_Steering_Clients": {
            "00000000-0000-4000-8000-0000000000d1": {
                "mac": s1,
                "cs_mode": "off",
                "cs_params": watch_marker,
            },
        },
    }
    steered_s1 = {
        **group,
        "Band_Steering_Clients": {
            "00000000-0000-4000-8000-0000000000d1": {
                "mac": s1,
                "cs_mode": "away",
                "cs_params": ["map", [["cs_enforce_period", "15"]]],
            },
        },
    }
    steer = SteeringIntent(
        "pod-1", "02:00:00:00:0a:00", "82:00:00:00:01:00", "02:00:00:12:75:2c", 81, 6, True, 15, 900
    )
    watched_station = {
        **group,
        "Band_Steering_Clients": {
            "00000000-0000-4000-8000-0000000000d2": {
                "mac": "02:00:00:00:0a:00",
                "cs_mode": "off",
                "cs_params": watch_marker,
            },
        },
    }
    cases = []
    for name, extra in (
        ("telemetry-fresh-pod", {}),
        ("telemetry-rows-present", ours),
        ("telemetry-another-broker", cloud),
    ):
        raw = rows(**extra)
        backend = TelemetryBackend(
            "pod-1", Recorder(copy.deepcopy(raw)), serial=SERIAL, radio_type="2.4G", survey=True
        )
        cases.append({"scope": "telemetry", **scope_write_case(name, raw, backend, telemetry)})
    # the wired uplink (spec 8.4): the pod's Ethernet uplink port into br-home
    from emosa.opensync.wired import WiredBackend, WiredIntent

    wired = WiredIntent("pod-1", "eth1", "br-home")
    eth1 = "00000000-0000-4000-8000-0000000000e1"

    def uplink(**row):
        return {
            "Connection_Manager_Uplink": {
                eth1: {
                    "if_name": "eth1",
                    "if_type": "eth",
                    "is_used": True,
                    "bridge": ["set", []],
                    **row,
                }
            }
        }

    for name, extra in (
        ("wired-uplink-fresh", uplink()),
        ("wired-uplink-already-bridged", uplink(bridge="br-home")),
        ("wired-uplink-another-bridge", uplink(bridge="br-wan")),
        ("wired-uplink-not-in-use", uplink(is_used=False)),
        ("wired-uplink-wifi-uplink", uplink(if_type="vif")),
    ):
        raw = rows(**extra)
        backend = WiredBackend("pod-1", Recorder(copy.deepcopy(raw)), serial=SERIAL, port="eth1")
        cases.append({"scope": "wired-uplink", **scope_write_case(name, raw, backend, wired)})
    for name, extra, stations in (
        ("watch-new-group", {}, (s1, s2)),
        ("watch-add-and-remove", watching_s1, (s2,)),
        ("watch-station-steered", steered_s1, (s1,)),
    ):
        raw = rows(**extra)
        backend = WatchBackend("pod-1", Recorder(copy.deepcopy(raw)), serial=SERIAL)
        cases.append(
            {
                "scope": "probe-watch",
                **scope_write_case(
                    name, raw, backend, WatchIntent("pod-1", "home-ap-24", "2.4G", stations)
                ),
            }
        )
    station = "02:00:00:00:0a:00"
    neighbor = {
        "bssid": "02:00:00:12:75:2c",
        "if_name": "home-ap-24",
        "channel": 6,
        "op_class": 81,
        "priority": 1,
    }
    steering_cases = (
        ("steering-replaces-watch-row", watched_station, steer),
        # what the plan refuses or reuses (plan 8.4)
        (
            "steering-another-managers-row",
            {
                **group,
                "Band_Steering_Clients": {
                    "00000000-0000-4000-8000-0000000000d3": {"mac": station, "cs_mode": "away"}
                },
            },
            steer,
        ),
        (
            "steering-two-groups",
            {
                "Band_Steering_Config": {
                    "00000000-0000-4000-8000-0000000000b1": {"if_name_2g": "home-ap-24"},
                    "00000000-0000-4000-8000-0000000000b2": {"if_name_2g": "home-ap-24"},
                }
            },
            steer,
        ),
        (
            "steering-two-neighbors",
            {
                **group,
                "Wifi_VIF_Neighbors": {
                    "00000000-0000-4000-8000-0000000000e1": neighbor,
                    "00000000-0000-4000-8000-0000000000e2": {**neighbor, "priority": 2},
                },
            },
            steer,
        ),
        (
            "steering-neighbor-present",
            {**group, "Wifi_VIF_Neighbors": {"00000000-0000-4000-8000-0000000000e1": neighbor}},
            steer,
        ),
        (
            "steering-station-not-associated",
            group,
            dataclasses.replace(steer, station="02:00:00:00:99:99"),
        ),
        ("steering-source-not-the-pods", group, dataclasses.replace(steer, source_bssid=station)),
        ("steering-another-pod", group, dataclasses.replace(steer, pod_id="pod-2")),
    )
    for name, extra, intent in steering_cases:
        raw = rows(**extra)
        backend = SteeringBackend("pod-1", Recorder(copy.deepcopy(raw)), serial=SERIAL)
        cases.append({"scope": "steering", **scope_write_case(name, raw, backend, intent)})
    cases.extend(ap_scope_cases())
    return {
        "description": "spec §3.4, §3.6, §3.7, §3.9: the scopes' guarded OVSDB writes on the "
        "recorded pod rows (with the case's changes): the AP scope's M2 intents (with the "
        "case's profile, multi_bss and passphrases by reference), the telemetry scope's "
        "statistics publishing, the probe watch's rows, and a steering window that replaces "
        "the station's watch row. Per case: the plan's refusal (a Reason), or the submission's "
        "status and every transaction sent. The server replies to every insert with the UUID "
        "00000000-0000-4000-8000-0000000000ff. Every scope is bound to pod_id.",
        "serial": SERIAL,
        "pod_id": "pod-1",
        "cases": cases,
    }


def ap_scope_cases():
    """The AP scope's plan (spec §3.4): its refusals, and its writes for an update and a
    cold pod's create, as PodBackend decides them on the recorded rows."""
    primary = Intent("pod-1", "radio-1", "bss-1", "emosa-mesh-2", "ref-primary")
    iot = {"role": "fronthaul", "ssid": "emosa-iot", "secret_ref": "ref-extra-1"}
    backhaul = {"role": "backhaul", "ssid": "emosa-bh", "secret_ref": "ref-extra-3"}
    replace = dataclasses.replace

    def changed(table, column, value, where=None):
        raw = pod_rows()
        for row in raw[table].values():
            if where is None or all(row.get(k) == v for k, v in where.items()):
                row[column] = value
        return raw

    def without_vif_state():
        raw = pod_rows()
        raw["Wifi_VIF_State"] = {
            u: r for u, r in raw["Wifi_VIF_State"].items() if r["if_name"] != "home-ap-24"
        }
        return raw

    home = {"if_name": "home-ap-24"}
    cases = (
        ("ap-update", pod_rows(), primary, False),
        ("ap-cold-create", cold(pod_rows()), primary, False),
        ("ap-ssid-too-long", pod_rows(), replace(primary, ssid="x" * 33), False),
        ("ap-security-unsupported", pod_rows(), replace(primary, security_mode="wpa3-sae"), False),
        ("ap-disabled", pod_rows(), replace(primary, enabled=False), False),
        ("ap-no-secret-ref", pod_rows(), replace(primary, secret_ref=""), False),
        ("ap-unknown-secret", pod_rows(), replace(primary, secret_ref="ref-unknown"), False),
        ("ap-additional-on-one-bss-radio", pod_rows(), replace(primary, additional=(iot,)), False),
        (
            "ap-additional-role-unknown",
            pod_rows(),
            replace(primary, additional=({**iot, "role": "mesh"},)),
            True,
        ),
        ("ap-eight-additional", pod_rows(), replace(primary, additional=(iot,) * 8), True),
        (
            "ap-more-of-a-role-than-slots",
            pod_rows(),
            replace(primary, additional=(backhaul, backhaul)),
            True,
        ),
        ("ap-another-bss", pod_rows(), replace(primary, bss_id="bss-2"), False),
        (
            "ap-another-pod",
            changed("AWLAN_Node", "serial_number", "MVXPOD0000000000"),
            primary,
            False,
        ),
        ("ap-no-radio-of-band", changed("Wifi_Radio_Config", "freq_band", "5G"), primary, False),
        (
            "ap-vif-on-another-radio",
            changed("Wifi_Radio_Config", "vif_configs", ["set", []], {"freq_band": "2.4G"}),
            primary,
            False,
        ),
        ("ap-vif-state-absent", without_vif_state(), primary, False),
        ("ap-vif-not-an-ap", changed("Wifi_VIF_Config", "mode", "sta", home), primary, False),
    )
    out = []
    for name, raw, intent, multi_bss in cases:
        with tempfile.TemporaryDirectory() as directory:
            vault = SecretStore(Path(directory) / "secrets")
            for ref, passphrase in PASSPHRASES.items():
                vault.write_simulated(ref, passphrase)
            backend = PodBackend(
                "pod-1", Recorder(copy.deepcopy(raw)), vault, serial=SERIAL, multi_bss=multi_bss
            )
            case = scope_write_case(name, raw, backend, intent)
        out.append(
            {
                "scope": "ap",
                "profile": DEFAULT,
                "multi_bss": multi_bss,
                "passphrases": PASSPHRASES,
                **case,
            }
        )
    return out


class Crash(BaseException):
    """The process stops (after the journal's SUBMITTED record, before the reply)."""


def journal_retention_vectors():
    """The journal's retention (spec §6): what stays when operations are added."""
    from emosa.model import Intent, Operation
    from emosa.store import RETAINED_RECENT, Store

    intent = Intent("pod-1", "02:00:00:00:01:00", "02:00:00:00:01:01", "home", "wsc-ref").record()
    template = Operation(
        "00000000-0000-4000-8000-000000000000",
        "run-1",
        "fleet",
        "semantic",
        intent,
        "0" * 64,
        "key-0",
        "2026-10-03T00:00:00Z",
        "2026-10-03T00:00:00Z",
        "2026-10-03T00:02:00Z",
    ).to_dict()

    def record(n, pod, state):
        """the template, numbered n, for pod, in state (both harnesses build it so)"""
        op = copy.deepcopy(template)
        op["operation_id"] = f"00000000-0000-4000-8000-{n:012d}"
        op["idempotency_key"] = f"key-{n}"
        op["intent"]["pod_id"] = pod
        op["state"] = state
        return Operation.from_dict(op)

    filler = RETAINED_RECENT + 6
    # steps: ("add" | "save", n, pod, state); a checkpoint after each step listed in checks
    cases = [
        ("recent-only", [("add", n, "pod-1", "REJECTED") for n in range(1, filler + 1)]),
        (
            "active-kept",
            [("add", 1, "pod-1", "REQUESTED")]
            + [("add", n, "pod-1", "CANCELLED") for n in range(2, filler + 2)],
        ),
        (
            "reconciled-kept-until-superseded",
            [("add", 1, "pod-1", "OBSERVED_APPLIED")]
            + [("add", n, "pod-1", "REJECTED") for n in range(2, filler + 2)]
            + [("add", filler + 2, "pod-1", "TIMED_OUT")],
        ),
        (
            "each-pod",
            [("add", 1, "pod-1", "OBSERVED_APPLIED"), ("add", 2, "pod-2", "OWNERSHIP_CONFLICT")]
            + [("add", n, "pod-3", "FAILED") for n in range(3, filler + 3)],
        ),
        (
            "finished-after-save",
            [("add", 1, "pod-1", "SUBMITTED")]
            + [("add", n, "pod-1", "REJECTED") for n in range(2, filler + 2)]
            + [("save", 1, "pod-1", "FAILED"), ("add", filler + 2, "pod-1", "REJECTED")],
        ),
    ]
    out = []
    for name, steps in cases:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "journal")
            recorded = []
            for index, (kind, n, pod, state) in enumerate(steps):
                op = record(n, pod, state)
                if kind == "add":
                    store.add(op)
                else:
                    store.save(op)
                step = {"step": [kind, n, pod, state]}
                if index >= RETAINED_RECENT - 1:  # the window is full from here on
                    step["kept"] = [int(o.operation_id[-12:]) for o in store.operations()]
                recorded.append(step)
            store.close()
        out.append({"name": name, "steps": recorded})
    return {
        "description": "spec §6: the journal's retention. Each case adds operations to an empty "
        "journal (and saves one in another state): every operation is the template with "
        "operation_id 00000000-0000-4000-8000-<n as 12 digits>, idempotency_key key-<n>, "
        "intent.pod_id and state from the step. A step: [add|save, n, pod, state]; once "
        "the journal holds retained_recent operations, 'kept' lists the numbers of the "
        "operations it holds after the step, oldest first: every active one, each pod's "
        "latest in a state reconciliation follows, and the retained_recent most recent.",
        "retained_recent": RETAINED_RECENT,
        "template": template,
        "cases": out,
    }


def engine_vectors():
    """The operation lifecycle (spec §5): request, execute, reconcile, recover."""
    from emosa.backends.base import Snapshot, SubmitResult
    from emosa.clock import ManualClock
    from emosa.model import Observation
    from emosa.opensync.probe_watch import WatchIntent
    from emosa.reconcile import Engine
    from emosa.store import Store

    class Scripted:
        mode = "scripted"

        def __init__(self):
            self.pod = {
                "config": "",
                "observed": "",
                "fresh": True,
                "ready": True,
                "instance": None,
            }
            self.plan_error = None
            self.result = "committed"

        async def snapshot(self):
            return Snapshot(
                {"watched": self.pod["config"]},
                Observation(
                    "pod-1",
                    "probe-watch",
                    {"watched": self.pod["observed"]},
                    "ovsdb",
                    self.mode,
                    1,
                    "2026-01-01T00:00:00Z",
                    self.pod["fresh"],
                    "scripted",
                    revision=1,
                ),
                self.pod["ready"],
                1,
                "conformance",
                self.pod["instance"],
            )

        async def plan(self, intent):
            if self.plan_error:
                raise EmosaError(Reason(self.plan_error), "scripted refusal")
            return {"action": "scripted"}

        async def submit(self, intent, attempt):
            if self.result == "crash":
                raise Crash()
            if self.result == "lost":
                raise ConnectionError("the reply was lost")
            instance = {"instance": self.pod["instance"]} if self.pod["instance"] else {}
            return {
                "committed": SubmitResult(
                    "committed", {"attribution": "reply", "transaction_validated": True, **instance}
                ),
                "conflict": SubmitResult("conflict", {}, Reason.PRECONDITION_FAILED),
                "rejected": SubmitResult("rejected", {}, Reason.NOT_READY),
                "unknown": SubmitResult(
                    "unknown", {"attribution": "unknown"}, Reason.OUTCOME_UNKNOWN
                ),
            }[self.result]

    a, b = "02:00:00:00:99:99", "02:00:00:00:98:98"
    # steps: ("request", stations, key, deadline), ("execute", n), ("pod", config, observed,
    # fresh, ready[, instance]), ("script", plan_error, submit), ("advance", seconds),
    # ("reconcile",), ("recover",), ("cancel", n); a watched value is ",".join(stations);
    # instance: which start of the pod's OpenSync the snapshot shows (spec §5)
    cases = [
        (
            "applied",
            [("request", [a], "k1", 30), ("execute", 0), ("pod", a, a, True, True), ("reconcile",)],
        ),
        (
            "already-applied",
            [("pod", a, a, True, True), ("request", [a], "k1", 30), ("execute", 0)],
        ),
        (
            "timed-out-then-late",
            [
                ("request", [a], "k1", 30),
                ("execute", 0),
                ("advance", 31),
                ("reconcile",),
                ("pod", a, a, True, True),
                ("reconcile",),
            ],
        ),
        (
            "lost-reply-then-applied",
            [
                ("script", None, "lost"),
                ("request", [a], "k1", 30),
                ("execute", 0),
                ("pod", a, a, True, True),
                ("reconcile",),
            ],
        ),
        (
            "unknown-then-deadline",
            [
                ("script", None, "unknown"),
                ("request", [a], "k1", 30),
                ("execute", 0),
                ("advance", 31),
                ("reconcile",),
            ],
        ),
        (
            # finding 22: the write landed (the pod's config has it), its reply was lost, and
            # the pod never applies it: past the deadline it no longer blocks the pod
            "unknown-landed-unapplied-then-deadline",
            [
                ("script", None, "unknown"),
                ("request", [a], "k1", 30),
                ("execute", 0),
                ("pod", a, "", True, True),
                ("reconcile",),
                ("request", [b], "k2", 30),
                ("advance", 31),
                ("reconcile",),
                ("request", [b], "k3", 30),
            ],
        ),
        (
            "conflict",
            [
                ("script", None, "conflict"),
                ("request", [a], "k1", 30),
                ("execute", 0),
                ("request", [b], "k2", 30),
            ],
        ),
        ("rejected", [("script", None, "rejected"), ("request", [a], "k1", 30), ("execute", 0)]),
        (
            "changed-by-another-manager",
            [
                ("request", [a], "k1", 30),
                ("execute", 0),
                ("pod", a, a, True, True),
                ("reconcile",),
                ("pod", b, b, True, True),
                ("reconcile",),
            ],
        ),
        (
            # its OpenSync starts again: the template without the write is no conflict
            "pod-restarted",
            [
                ("pod", "", "", True, True, "start-1"),
                ("request", [a], "k1", 30),
                ("execute", 0),
                ("pod", a, a, True, True, "start-1"),
                ("reconcile",),
                ("pod", "", "", True, True, "start-2"),
                ("reconcile",),
                ("request", [a], "k2", 30),
                ("execute", 1),
            ],
        ),
        (
            # a conflict seen on one start no longer blocks the pod on the next
            "conflict-then-restart",
            [
                ("pod", "", "", True, True, "start-1"),
                ("request", [a], "k1", 30),
                ("execute", 0),
                ("pod", a, a, True, True, "start-1"),
                ("reconcile",),
                ("pod", b, b, True, True, "start-1"),
                ("reconcile",),
                ("request", [a], "k2", 30),
                ("pod", "", "", True, True, "start-2"),
                ("reconcile",),
                ("request", [a], "k3", 30),
            ],
        ),
        ("busy", [("request", [a], "k1", 30), ("request", [b], "k2", 30)]),
        (
            "plan-refused",
            [("script", "NOT_READY", "committed"), ("request", [a], "k1", 30), ("execute", 0)],
        ),
        (
            "pod-not-ready",
            [("pod", "", "", False, False), ("request", [a], "k1", 30), ("execute", 0)],
        ),
        (
            "same-key",
            [("request", [a], "k1", 30), ("request", [a], "k1", 30), ("request", [b], "k1", 30)],
        ),
        (
            "stale-observation",
            [
                ("request", [a], "k1", 30),
                ("execute", 0),
                ("pod", a, a, False, True),
                ("reconcile",),
                ("pod", a, a, True, True),
                ("reconcile",),
            ],
        ),
        ("cancelled", [("request", [a], "k1", 30), ("cancel", 0)]),
        (
            "recovered-after-crash",
            [
                ("script", None, "crash"),
                ("request", [a], "k1", 30),
                ("execute", 0),
                ("recover",),
                ("pod", a, a, True, True),
                ("reconcile",),
            ],
        ),
    ]
    out = []
    for name, steps in cases:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "journal")
            vault = SecretStore(Path(directory) / "secrets")
            clock = ManualClock()
            backend = Scripted()
            engine = Engine(store, vault, {"pod-1": backend}, clock, intent_type=WatchIntent)
            ids, recorded, seen = [], [], 0
            for step in steps:
                kind, error = step[0], None
                if kind == "request":
                    intent = WatchIntent("pod-1", "home-ap-24", "2.4G", tuple(step[1]))
                    try:
                        op = engine.request(
                            intent,
                            source="conformance",
                            key=step[2],
                            run_id="run-1",
                            deadline=step[3],
                        )
                        if op.operation_id not in ids:
                            ids.append(op.operation_id)
                    except EmosaError as exc:
                        error = exc.code.value
                elif kind == "execute":
                    with contextlib.suppress(Crash):
                        asyncio.run(engine.execute(ids[step[1]]))
                elif kind == "pod":
                    backend.pod = {
                        "config": step[1],
                        "observed": step[2],
                        "fresh": step[3],
                        "ready": step[4],
                        "instance": step[5] if len(step) > 5 else None,
                    }
                elif kind == "script":
                    backend.plan_error, backend.result = step[1], step[2]
                elif kind == "advance":
                    clock.advance(step[1])
                elif kind == "reconcile":
                    asyncio.run(engine.reconcile("pod-1"))
                elif kind == "recover":
                    engine = Engine(
                        store, vault, {"pod-1": backend}, clock, intent_type=WatchIntent
                    )
                    engine.recover()
                elif kind == "cancel":
                    engine.cancel(ids[step[1]])
                events = store.events("run-1", after=seen, limit=500)
                seen = events[-1]["sequence"] if events else seen
                recorded.append(
                    {
                        "step": list(step),
                        "expected": {
                            "error": error,
                            "operations": [
                                {
                                    "state": o.state.value,
                                    "reason": o.reason,
                                    "deadline_elapsed": o.deadline_elapsed,
                                    "original_outcome": o.original_outcome,
                                    "late_resolution": o.late_resolution,
                                    "changed": o.changed,
                                    "blocked_for_resubmission": o.blocked_for_resubmission,
                                    "applied_evidence": o.application_evidence is not None,
                                    "attribution": (o.application_evidence or {}).get(
                                        "attribution"
                                    ),
                                    "commit_attribution": o.commit_evidence.get("attribution"),
                                }
                                for o in (store.get(i) for i in ids)
                            ],
                            "ownership": store.ownership("pod-1") is not None,
                            "events": [e["phase"] for e in events],
                        },
                    }
                )
            store.close()
        out.append({"name": name, "steps": recorded})
    return {
        "description": "spec §5: the operation lifecycle over a scripted pod (its configured and "
        "observed watch set, whether the observation is fresh and the pod ready; the plan's "
        "refusal; the submission's outcome: committed, conflict, rejected, unknown, lost (no "
        "reply) or crash (the process stops after the journal's SUBMITTED record)). The "
        "intent is a probe-watch intent (its target: the watched stations, joined by commas). "
        "A step: request (stations, idempotency key, deadline in seconds), execute (the n-th "
        "operation), pod, script, advance (the clock, seconds), reconcile, recover (a new "
        "process on the same journal), cancel. Per step: the request's error, every "
        "operation's lifecycle fields, whether the pod's ownership is lost, and the journal "
        "events the step added (their phases).",
        "cases": out,
    }


def early_report_vectors():
    """The Early AP Capability Report's delivery: retransmissions and Acks (EasyMesh 6.1)."""
    from emosa.wire.cmdu import MidSequence, Tlv, fragment_message
    from emosa.wire.coordinator import ReportCoordinator, ReportSource

    raw = pod_rows()
    view = device_view(decode(raw))
    radio = view.radio(mac(RUID))
    caps = radio_capabilities(radio, channel=6, max_bss=5, max_eirp=30)
    facts = topology(
        agent_al=mac(AGENT),
        controller_al=mac(CONTROLLER),
        radio=radio,
        channel=6,
        bsses=radio.bsses,
        ages={m: 10 for b in radio.bsses for m in b.stations},
    )
    binding = PeerBinding("conformance", 1, mac(AGENT), mac(CONTROLLER), (mac(CONTROLLER),))

    def ack(mid, error=False):
        tlvs = (Tlv(0xA3, bytes([1]) + mac("02:00:00:00:0a:00")),) if error else ()
        return fragment_message(mac(AGENT), mac(CONTROLLER), 0x8000, mid, tlvs)[0]

    # steps: ("notify", at), ("tick", at), ("ack", at, n or None (a foreign MID), error),
    # ("revision", at): the pod's State published anew with a new revision
    cases = [
        ("acknowledged-at-once", [("notify", 0.0), ("ack", 0.1, 0, False)]),
        (
            "retransmitted-then-acknowledged",
            [("notify", 0.0), ("tick", 0.1), ("tick", 0.25), ("tick", 0.5), ("ack", 0.6, 1, False)],
        ),
        (
            "three-then-timeout",
            [("notify", 0.0), ("tick", 0.25), ("tick", 0.5), ("tick", 0.75), ("tick", 1.0)],
        ),
        ("foreign-ack", [("notify", 0.0), ("ack", 0.1, None, False), ("tick", 0.25)]),
        (
            "ack-with-error-code",
            [("notify", 0.0), ("ack", 0.1, 0, True), ("tick", 0.25), ("ack", 0.3, 1, False)],
        ),
        ("late-ack", [("notify", 0.0), ("ack", 1.2, 0, False)]),
        (
            "source-changed",
            [
                ("notify", 0.0),
                ("tick", 0.25),
                ("revision", 0.3),
                ("tick", 0.5),
                ("ack", 0.6, 0, False),
            ],
        ),
    ]
    out = []
    for name, steps in cases:
        now = [0.0]
        source = ReportSource(binding, "pod-1", "c" * 64, clock=lambda now=now: now[0])
        source.publish((1, 1), caps, facts, observed_at=0.0, lifetime=1.5)
        sent = []
        coordinator = ReportCoordinator(
            source, sent.append, mids=MidSequence(499), clock=lambda now=now: now[0]
        )
        recorded, early_mids, revision = [], [], 1
        for step in steps:
            kind, at = step[0], step[1]
            now[0] = at
            before = len(sent)
            result = None
            if kind == "notify":
                coordinator.notify_early()
            elif kind == "tick":
                coordinator.tick()
            elif kind == "revision":
                revision += 1
                source.publish((1, revision), caps, facts, observed_at=at, lifetime=1.5)
            else:
                which, error = step[2], step[3]
                mid = early_mids[which] if which is not None else 999
                result = coordinator.receive(ack(mid, error), ingress="conformance", generation=1)
            for frame in sent[before:]:
                early_mids.append(int.from_bytes(frame[18:20], "big"))
            recorded.append(
                {
                    "step": list(step),
                    "expected": {"result": result, "frames": [f.hex() for f in sent[before:]]},
                }
            )
        out.append({"name": name, "steps": recorded, "expected_counts": dict(coordinator.counts)})
    return {
        "description": "EasyMesh 6.1 §5.2.2 (the Early AP Capability Report before M1): its "
        "delivery. The report is the recorded pod's capability TLVs (radio " + RUID + ", "
        "channel 6, five BSSes, max EIRP 30), sent at once, then every 250 ms with a new MID, "
        "three transmissions at most, until an Ack names one of its MIDs within one second. "
        "A step: notify (the report is due), tick, or ack (of the n-th transmission's MID, or "
        "of a foreign MID when n is null; with an Error Code companion when error), or "
        "revision (the pod's State published anew with a new revision: a pending report is "
        "dropped on the next tick), at a time 'at' in seconds. Per step: the Ack's result "
        "and the frames sent; the agent's MIDs start at 500.",
        "agent": {"al_mac": AGENT, "controller_al": CONTROLLER, "radio": RUID, "first_mid": 500},
        "ovsdb_tables": raw,
        "cases": out,
    }


def steering_queue_vectors():
    """Client steering on the pod: one window at a time, a short queue (spec §3.7)."""
    from emosa.agent.steering import ClientSteering
    from emosa.clock import ManualClock
    from emosa.opensync.steering import SteeringBackend
    from emosa.store import Store
    from emosa.wire.steering import SteeringRequest

    raw = pod_rows()
    source = "82:00:00:00:01:00"
    s0, s1, away = "02:00:00:00:0a:00", "02:00:00:00:10:00", "02:00:00:00:55:55"
    target = "02:00:00:12:75:2c"
    extra = [f"02:00:00:00:6{i}:00" for i in range(9)]  # stations queued behind a window

    def request(station, window=15, imminent=True, channel=6):
        return SteeringRequest(
            mac(source),
            True,
            imminent,
            False,
            window,
            0,
            (mac(station),),
            ((mac(target), 81, channel),),
        )

    # steps: ("start", at, station, mid, window, imminent[, channel]), ("tick", at),
    # ("restart", at); a case's options are the Recorder's
    cases = [
        (
            "one-window-not-applied",
            [("start", 0.0, s0, 900, 15, True), ("tick", 0.0), ("tick", 5.0), ("tick", 10.5)],
        ),
        (
            "queued-then-started",
            [
                ("start", 0.0, s0, 900, 15, True),
                ("start", 0.0, s1, 901, 15, True),
                ("tick", 0.0),
                ("start", 5.0, s1, 902, 20, False),
                ("tick", 10.5),
                ("tick", 21.0),
            ],
        ),
        (
            "queue-expires",
            [
                ("start", 0.0, s0, 900, 15, True),
                ("start", 0.0, s1, 901, 15, True),
                ("tick", 0.0),
                ("tick", 10.5),
            ],
        ),
        (
            "moved-while-queued",
            [
                ("start", 0.0, s0, 900, 15, True),
                ("start", 1.0, away, 901, 15, True),
                ("tick", 1.0),
                ("tick", 10.5),
            ],
        ),
        (
            "full-queue",
            [("start", 0.0, s0, 900, 15, True)]
            + [("start", 0.0, m, 901 + i, 15, True) for i, m in enumerate(extra)],
        ),
        ("station-not-on-source", [("start", 0.0, away, 900, 15, True), ("tick", 0.0)]),
        ("invalid-target", [("start", 0.0, s0, 900, 15, True, 0)]),
        (
            "restart-before-sent",
            [("start", 0.0, s0, 900, 15, True), ("restart", 1.0), ("tick", 1.0)],
        ),
        (
            "kicked-then-stayed",
            [("start", 0.0, s0, 900, 15, True), ("tick", 0.0), ("tick", 5.0), ("tick", 40.0)],
            {"owm": True},
        ),
        (
            "outcome-unknown-window-open",
            [("start", 0.0, s0, 900, 15, True), ("tick", 0.0)],
            {"owm": True, "lost": [0]},
        ),
        (
            "kick-reply-lost",
            [("start", 0.0, s0, 900, 15, True), ("tick", 0.0)],
            {"owm": True, "lost": [1]},
        ),
        (
            "close-reply-lost",
            [("start", 0.0, s0, 900, 15, True), ("tick", 0.0), ("tick", 40.0)],
            {"owm": True, "lost": [2]},
        ),
    ]
    out = []
    for name, steps, *options in cases:
        options = options[0] if options else {}
        with tempfile.TemporaryDirectory() as directory:
            session = Recorder(copy.deepcopy(raw), **options)
            clock = ManualClock()

            def scope(session=session, clock=clock, directory=directory):
                return ClientSteering(
                    "pod-1",
                    SteeringBackend("pod-1", session, serial=SERIAL),
                    Store(Path(directory) / "steering"),
                    SecretStore(Path(directory) / "secrets"),
                    run_id="run-1",
                    clock=clock,
                )

            steering = scope()
            recorded, sent = [], 0
            for step in steps:
                at = step[1]
                clock.advance(at - clock.monotonic())
                result = None
                if step[0] == "start":
                    result = steering.start(request(step[2], *step[4:]), step[3])
                elif step[0] == "restart":
                    steering.store.close()
                    steering = scope()
                else:
                    asyncio.run(steering.tick())
                status = copy.deepcopy(steering.status())
                if status["active"]:
                    status["active"].pop("operation_id")
                for entry in status["history"]:
                    entry.pop("operation_id")
                recorded.append(
                    {
                        "step": list(step),
                        "expected": {
                            "result": result,
                            "transactions": session.sent[sent:],
                            "status": status,
                        },
                    }
                )
                sent = len(session.sent)
            steering.store.close()
        out.append(
            {
                "name": name,
                "owm": options.get("owm", False),
                "lost": list(options.get("lost", ())),
                "steps": recorded,
            }
        )
    return {
        "description": "spec §3.7: the agent's client steering on the recorded pod rows "
        "(stations " + s0 + " and " + s1 + " on " + source + "; " + away + " is not the pod's), "
        "each mandate one window steering the station to " + target + " (class 81, channel "
        "6): one window at a time, a queue of eight that a newer mandate for a queued station "
        "replaces, a queued mandate dropped when it waited over 10 s or its station left the "
        "source. The recorded rows never show owm steering, so a window is not applied (10 "
        "s) and is closed; in a case with owm, the client row a transaction inserts appears "
        "in the rows with that UUID and cs_state steering, so the window is applied and the "
        "station kicked. A case's lost transactions (numbered from 0) are carried out but "
        "their replies are lost (no reply). A step: start (at, station, the request's MID, "
        "window, disassociation imminent, and the target's channel when not 6), tick (at) or "
        "restart (at: the agent restarts on its journal), the clock in seconds. Per step: "
        "start's refusal (null when started or queued), the transactions sent, and the "
        "steering status (without operation IDs). Inserts are answered with the UUID "
        "00000000-0000-4000-8000-0000000000ff.",
        "source_bssid": source,
        "target": target,
        "serial": SERIAL,
        "ovsdb_tables": raw,
        "cases": out,
    }


def probe_watch_vectors():
    """Which stations the pod watches for probe requests (spec §3.9)."""
    from emosa.agent.probe_watch import ProbeWatch
    from emosa.clock import ManualClock
    from emosa.opensync.probe_watch import WatchBackend
    from emosa.store import Store

    raw = pod_rows()
    associated = "02:00:00:00:0a:00"  # on the pod: never watched
    heard = [f"02:00:00:00:8{i:x}:00" for i in range(16)]
    many = [f"02:00:00:01:{i:02x}:00" for i in range(40)]
    steered = {
        "Band_Steering_Config": {
            "00000000-0000-4000-8000-0000000000b1": {"if_name_2g": "home-ap-24"}
        },
        "Band_Steering_Clients": {
            "00000000-0000-4000-8000-0000000000d1": {
                "mac": heard[2],
                "cs_mode": "away",
                "cs_params": ["map", [["cs_enforce_period", "15"]]],
            },
        },
    }
    # steps: ("ask", at, stations), ("tick", at)
    cases = [
        (
            "first-write",
            {},
            [("ask", 0.0, [heard[0], heard[1], associated]), ("tick", 0.0), ("tick", 1.0)],
        ),
        (
            "minimum-interval",
            {},
            [
                ("ask", 0.0, [heard[0]]),
                ("tick", 0.0),
                ("tick", 31.0),
                ("ask", 32.0, [heard[1]]),
                ("tick", 35.0),
                ("tick", 42.0),
            ],
        ),
        ("at-most-32", {}, [("ask", 0.0, many[:20]), ("ask", 1.0, many[20:]), ("tick", 1.0)]),
        (
            "forgotten-after-600-s",
            {},
            [("ask", 0.0, [heard[0]]), ("ask", 500.0, [heard[1]]), ("tick", 601.0)],
        ),
        ("another-row-blocks", steered, [("ask", 0.0, [heard[2], heard[3]]), ("tick", 0.0)]),
    ]
    out = []
    for name, extra, steps in cases:
        rows = {**raw, **copy.deepcopy(extra)}
        with tempfile.TemporaryDirectory() as directory:
            session = Recorder(copy.deepcopy(rows))
            clock = ManualClock()
            watch = ProbeWatch(
                "pod-1",
                WatchBackend("pod-1", session, serial=SERIAL),
                Store(Path(directory) / "probe-watch"),
                SecretStore(Path(directory) / "secrets"),
                if_name="home-ap-24",
                band="2.4G",
                run_id="run-1",
                clock=clock,
                monotonic=clock.monotonic,
            )
            recorded, sent = [], 0
            for step in steps:
                clock.advance(step[1] - clock.monotonic())
                if step[0] == "ask":
                    watch.ask(step[2])
                else:
                    asyncio.run(watch.tick())
                status = copy.deepcopy(watch.status())
                if status["operation"]:
                    status["operation"].pop("operation_id")
                recorded.append(
                    {
                        "step": list(step),
                        "expected": {"transactions": session.sent[sent:], "status": status},
                    }
                )
                sent = len(session.sent)
            watch.store.close()
        out.append({"name": name, "ovsdb_tables": rows, "steps": recorded})
    return {
        "description": "spec §3.9: the probe watch on the recorded pod rows (the case's), its "
        "rows on the fronthaul home-ap-24 (2.4G): the stations asked about (never one "
        "associated with the pod or with another manager's client row), at most the 32 "
        "asked most recently, dropped 600 s after they were last asked, written at most "
        "every 10 s, one write at a time (applied when the pod shows it, else timed out after "
        "30 s). The recorded rows do not change with a write. A step: ask (at, stations) or "
        "tick (at), the clock in seconds. Per step: the transactions sent and the watch's "
        "status (without the operation ID). Inserts are answered with the UUID "
        "00000000-0000-4000-8000-0000000000ff.",
        "serial": SERIAL,
        "cases": out,
    }


def session_timing_vectors():
    """The agent's onboarding attempts and its own renewals (spec §2.5)."""
    from emosa.agent.renew import RenewRules
    from emosa.easymesh_payloads import DeviceInventory, InventoryRadio
    from emosa.wire.cmdu import MidSequence
    from emosa.wire.coordinator import ReportSource
    from emosa.wire.onboarding import ClientReannouncement, OnboardingRecovery, OnboardingSession

    raw = pod_rows()
    view = device_view(decode(raw))
    radio = view.radio(mac(RUID))
    caps = radio_capabilities(radio, channel=6, max_bss=5, max_eirp=30)
    facts = topology(
        agent_al=mac(AGENT),
        controller_al=mac(CONTROLLER),
        radio=radio,
        channel=6,
        bsses=radio.bsses,
        ages={m: 10 for b in radio.bsses for m in b.stations},
    )
    binding = PeerBinding("conformance", 1, mac(AGENT), mac(CONTROLLER), (mac(CONTROLLER),))
    inventory = DeviceInventory(
        b"owned", b"0.1.0", b"model", (InventoryRadio(mac(RUID), b"model"),)
    )

    def refused(*_):
        raise AssertionError("no controller answers in these cases")

    def timeouts(first, count, *, generation=1):
        """Attempts that no controller answers: each tick a second apart, from ``first``."""
        return [("tick", first + i, generation) for i in range(count)]

    # steps: ("tick", at, database generation or None: the pod's State lost), ("renew", at)
    attempts = [
        ("discovery-then-timeout", timeouts(0.0, 7)),
        ("backoff-doubles-to-30", timeouts(0.0, 160)),
        (
            "source-lost-while-discovering",
            [
                ("tick", 0.0, 1),
                ("tick", 1.0, 1),
                ("tick", 2.0, None),
                ("tick", 3.0, 1),
                ("tick", 4.0, 1),
                ("tick", 5.0, 1),
            ],
        ),
        (
            "new-generation-while-discovering",
            [("tick", 0.0, 1), ("tick", 1.0, 2), ("tick", 2.0, 2), ("tick", 3.0, 2)],
        ),
        ("no-start-without-source", [("tick", 0.0, None), ("tick", 1.0, None), ("tick", 2.0, 1)]),
        (
            "renew-starts-at-once",
            [*timeouts(0.0, 16), ("renew", 16.0), ("tick", 16.0, 1), ("tick", 17.0, 1)],
        ),
        (
            "renew-while-discovering",
            [
                ("tick", 0.0, 1),
                ("tick", 0.5, 1),
                ("renew", 0.7),
                ("tick", 0.7, 1),
                ("tick", 1.2, 1),
                ("tick", 1.7, 1),
            ],
        ),
    ]
    out_attempts = []
    for name, steps in attempts:
        now = [0.0]
        clock = lambda now=now: now[0]  # noqa: E731
        source = ReportSource(binding, "pod-1", "c" * 64, clock=clock)
        sent = []
        recovery = OnboardingRecovery(
            source,
            lambda source=source, sent=sent, clock=clock: OnboardingSession(
                source, sent.append, refused, inventory, mids=MidSequence(499), clock=clock
            ),
            clock=clock,
        )
        revision, recorded = [0, 0], []
        for step in steps:
            now[0] = step[1]
            before_sent, before_history, before_starts = (
                len(sent),
                len(recovery.history),
                recovery.starts,
            )
            if step[0] == "renew":
                recovery.renew()
            else:
                generation = step[2]
                if generation is None:
                    source.invalidate()
                else:
                    revision = [generation, revision[1] + 1]
                    source.publish(tuple(revision), caps, facts, observed_at=now[0], lifetime=2)
                asyncio.run(recovery.tick())
            ended = None
            for entry in list(recovery.history)[before_history:]:
                if entry["event"] == "source_lost":
                    ended = "source_lost"
                elif entry["event"] == "failed":
                    events = entry["session"]["events"]
                    ended = (
                        "discovery_timeout"
                        if events and events[-1] == "discovery_timeout"
                        else "failed"
                    )
            searches = [f for f in sent[before_sent:] if int.from_bytes(f[16:18], "big") == 0x0007]
            recorded.append(
                {
                    "step": list(step),
                    "expected": {
                        "started": recovery.starts > before_starts,
                        "searches": len(searches),
                        "ended": ended,
                        "discovering": recovery.session is not None
                        and recovery.session.state == "discovering",
                        "attempts_started": recovery.starts,
                        "failures": recovery.failures,
                        "next_start": recovery.next_start,
                    },
                }
            )
        recovery.close()
        out_attempts.append({"name": name, "steps": recorded})

    # steps: ("contact", at), ("topology_query", at), ("check", at, unserved, awaiting[,
    # provisioning]); a case's third member, when given, is its topology_query_window
    renewals = [
        (
            "controller-silent-in-any-state",
            [
                ("check", 100.0, False, False),
                ("check", 131.0, False, False),
                ("check", 200.0, False, False),
                ("check", 262.0, False, False),
            ],
        ),
        (
            "contact-defers-silence",
            [("contact", 100.0), ("check", 131.0, False, False), ("check", 231.0, False, False)],
        ),
        (
            "no-m2-after-30-s",
            [
                ("contact", 0.0),
                ("check", 1.0, False, True),
                ("check", 31.0, False, True),
                ("contact", 31.5),
                ("check", 31.5, False, True),
                ("check", 32.0, False, True),
                ("check", 62.5, False, True),
            ],
        ),
        (
            "m2-arrives-in-time",
            [
                ("check", 1.0, False, True),
                ("check", 20.0, False, False),
                ("check", 40.0, False, True),
                ("check", 60.0, False, True),
            ],
        ),
        (
            "unserved-after-60-s",
            [
                ("check", 1.0, True, False),
                ("contact", 30.0),
                ("check", 61.0, True, False),
                ("contact", 61.5),
                ("check", 61.5, True, False),
                ("check", 100.0, False, False),
            ],
        ),
        (
            "unserved-served-again",
            [
                ("check", 1.0, True, False),
                ("check", 50.0, False, False),
                ("check", 60.0, True, False),
                ("check", 110.0, True, False),
            ],
        ),
        (
            "all-three-at-once",
            [
                ("check", 0.0, True, True),
                ("check", 131.0, True, True),
                ("check", 162.0, True, True),
            ],
        ),
        (
            "no-topology-query-while-provisioning",
            [
                ("check", 0.0, False, False, True),
                ("topology_query", 20.0),
                ("contact", 50.0),
                ("check", 79.0, False, False, True),
                ("contact", 80.0),
                ("check", 81.0, False, False, True),
                ("check", 82.0, False, False, True),
                ("contact", 130.0),
                ("check", 141.0, False, False, True),
                ("topology_query", 141.5),
                ("check", 200.0, False, False, True),
                ("check", 202.0, False, False, True),
            ],
            60,
        ),
        (
            "topology-query-rule-off-without-a-window",
            [
                ("check", 0.0, False, False, True),
                ("contact", 100.0),
                ("check", 200.0, False, False, True),
            ],
        ),
        (
            "topology-query-window-from-m2",
            [
                ("check", 0.0, False, False, False),
                ("topology_query", 30.0),
                ("contact", 60.0),
                ("check", 70.0, False, False, False),
                ("check", 71.0, False, False, True),
                ("contact", 120.0),
                ("check", 130.0, False, False, True),
                ("check", 131.5, False, False, True),
            ],
            60,
        ),
        (
            "topology-query-window-restarts-on-a-new-m2",
            [
                ("check", 0.0, False, False, True),
                ("check", 50.0, False, True, False),
                ("check", 55.0, False, False, True),
                ("contact", 100.0),
                ("check", 110.0, False, False, True),
                ("check", 115.5, False, False, True),
            ],
            60,
        ),
        (
            "unserved-ends-provisioning",
            [
                ("check", 0.0, True, False, True),
                ("contact", 61.0),
                ("check", 61.0, True, False, True),
                ("check", 62.0, False, False, True),
                ("check", 121.0, False, False, True),
                ("check", 122.5, False, False, True),
            ],
            60,
        ),
    ]
    out_renewals = []
    for name, steps, *window in renewals:
        rules = RenewRules(0.0, topology_query_window=window[0] if window else None)
        recorded = []
        for step in steps:
            if step[0] == "contact":
                rules.contact(step[1])
                reasons = []
            elif step[0] == "topology_query":
                rules.topology_query(step[1])
                reasons = []
            else:
                reasons = rules.check(
                    step[1],
                    unserved=step[2],
                    awaiting=step[3],
                    provisioning=step[4] if len(step) > 4 else False,
                )
            recorded.append({"step": list(step), "expected": {"reasons": reasons}})
        case = {"name": name, "steps": recorded}
        if window:
            case["topology_query_window"] = window[0]
        out_renewals.append(case)
    # steps: ("provisioned", Topology Responses sent), ("tick", Topology Responses sent)
    reannouncements = [
        ("after-the-next-query", [("provisioned", 0), ("tick", 0), ("tick", 1), ("tick", 2)]),
        ("not-before-m2", [("tick", 3), ("provisioned", 3), ("tick", 3), ("tick", 4)]),
        ("the-first-m2-marks", [("provisioned", 0), ("provisioned", 2), ("tick", 1), ("tick", 3)]),
    ]
    out_reannouncements = []
    for name, steps in reannouncements:
        rule, recorded = ClientReannouncement(), []
        for kind, responses in steps:
            due = None
            if kind == "provisioned":
                rule.provisioned(responses)
            else:
                due = rule.due(responses)
            recorded.append({"step": [kind, responses], "expected": {"due": due}})
        out_reannouncements.append({"name": name, "steps": recorded})
    return {
        "description": "spec §2.5: when the agent starts an onboarding attempt and ends it, "
        "when it renews one on its own, and when it announces its clients again. attempts: "
        "the reference's OnboardingRecovery over OnboardingSession on a published pod State "
        "that no controller answers; the clock "
        "starts at 0 s. A step: tick (at, the pod's database generation, or null: its State "
        "lost; otherwise the State is published anew at that time) or renew (at: an "
        "AP-Autoconfiguration Renew). Per step: whether an attempt started, how many "
        "AP-Autoconfiguration Searches were sent, how the attempt ended (discovery_timeout, "
        "source_lost or null), whether one is discovering, and the attempts started, failures "
        "and next start time after it. renewals: emosa.agent.renew.RenewRules created at 0 s, "
        "with the case's topology_query_window when it has one (none: that rule is off). "
        "A step: contact (at: a frame from the controller), topology_query (at: a Topology "
        "Query from the controller) or check (at, unserved, awaiting, and provisioning: false "
        "when absent): the reasons to renew, in order. reannouncements: "
        "emosa.wire.onboarding.ClientReannouncement of one session. A step: provisioned (M2 "
        "accepted) or tick, with the Topology Responses the session has sent: whether every "
        "client is announced again now (null for provisioned).",
        "attempts": out_attempts,
        "renewals": out_renewals,
        "reannouncements": out_reannouncements,
    }


VECTOR_SETS = {
    "al-mac.json": al_mac_vectors,
    "operation-transitions.json": transition_vectors,
    "translation-northbound.json": northbound_vectors,
    "translation-southbound.json": southbound_vectors,
    "fleet.json": fleet_vectors,
    "fleet-sessions.json": fleet_session_vectors,
    "gtp.json": gtp_vectors,
    "uplink.json": uplink_vectors,
    "cmdu.json": cmdu_vectors,
    "wsc-m2.json": wsc_m2_vectors,
    "control.json": control_vectors,
    "steering.json": steering_vectors,
    "onboarding.json": onboarding_vectors,
    "telemetry.json": telemetry_vectors,
    "metrics.json": metrics_vectors,
    "backhaul-steering.json": backhaul_steering_vectors,
    "scope-writes.json": scope_writes_vectors,
    "engine.json": engine_vectors,
    "journal-retention.json": journal_retention_vectors,
    "early-report.json": early_report_vectors,
    "steering-queue.json": steering_queue_vectors,
    "probe-watch.json": probe_watch_vectors,
    "session-timing.json": session_timing_vectors,
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
