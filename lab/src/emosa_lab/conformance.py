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


# -- the 1905 envelope and the provisioned agent's control plane ------------------


def cmdu_vectors():
    from emosa.wire.cmdu import Reassembler, Tlv, fragment_message

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
    decode = []
    good = fragment_message(agent, controller, 0x8014, 900, (Tlv(0x9B, bytes(13)),))[0]
    header = 14 + 8  # Ethernet + 1905 header

    def case(name, frames):
        parser, result, error = Reassembler(), None, None
        try:
            for frame in frames:
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
        decode.append({"name": name, "frames": [f.hex() for f in frames], "expected": expected})

    case("single", [good])
    large = tuple(Tlv(0x11, bytes([i]) * 600) for i in range(3))
    case("fragments-in-order", list(fragment_message(agent, controller, 0x0009, 42, large)))
    case("first-fragment-only", [fragment_message(agent, controller, 0x0009, 43, large)[0]])
    case("reserved-version", [good[:14] + b"\x01" + good[15:]])
    case("truncated-tlv", [good[: header + 5]])
    case("missing-end-of-message", [good[: header + 16]])
    case("ethernet-padding-after-end", [good + bytes(20)])
    return {
        "description": "spec §2.1: the IEEE 1905.1 envelope. 'encode': a message to the "
        "Ethernet frames the agent transmits (fragmented at TLV boundaries within the MTU). "
        "'decode': received frames, fed in order, to the reassembled message, none while "
        "incomplete, or the reason code of the rejection.",
        "encode": encode,
        "decode": decode,
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


def control_vectors():
    from emosa.wire.channel import ChannelCoordinator, ChannelPolicyStore
    from emosa.wire.cmdu import MidSequence, Reassembler, Tlv, fragment_message
    from emosa.wire.reporting_policy import ReportingPolicyCoordinator, ReportingPolicyStore
    from emosa.wire.steering import SteeringCoordinator

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
            "channel-selection-rdk-declined",  # RDK's request: 40 MHz classes, 0 dBm power limit
            [request(0x8006, 13, (Tlv(0x8B, ruid + rdk_preferences), Tlv(0x8D, ruid + b"\x00")))],
        ),
        ("policy-rdk", [request(0x8003, 14, rdk_policy)]),  # RDK's TLV set, captured
        (
            "channel-scan",  # RDK's layout: no fresh scan, one radio, one class
            [request(0x801B, 19, (Tlv(0xA6, b"\x00\x01" + ruid + bytes.fromhex("0151020106")),))],
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
    ]
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
            )
            steps = []
            for frame in frames:
                message, before = Reassembler().feed(frame), len(sent)
                result = next(
                    (r for r in (h.handle(message, 0.0) for h in handlers) if r is not None), None
                )
                steps.append(
                    {
                        "request": frame.hex(),
                        "expected": {"result": result, "frames": [f.hex() for f in sent[before:]]},
                    }
                )
            for handler in handlers:
                handler.close()
            cases.append({"name": name, "steps": steps, "handed_to_pod": handed})
    return {
        "description": "spec §2.4, §3.4, §3.7: a provisioned agent over the recorded pod rows "
        "(stations " + ", ".join(stations) + " on 82:00:00:00:01:00, radio " + RUID + " on "
        "class 81 channel 6 at the pod's 30 dBm, max EIRP 30). Each step is a controller "
        "request frame and the frames the agent sends in answer, in order; the agent's own "
        "messages take MIDs from 500 and a Channel Scan Report carries scan_timestamp. "
        "'handed_to_pod' lists the steering mandates the agent carries out (spec §3.7), "
        "as decoded from the request.",
        "agent": {
            "al_mac": AGENT,
            "controller_al": CONTROLLER,
            "radio": RUID,
            "first_mid": 500,
            "scan_timestamp": SCAN_TIMESTAMP,
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
    }


STATS_RECORDED = ROOT / "tests" / "fixtures" / "opensync" / "pod-6.6.1-hwsim-client-stats.hex"


def survey_report(timestamp_ms, *, channel=6, busy=41):
    """A raw on-channel survey publish (spec §3.8), built from the pinned schema."""
    from emosa.opensync.stats import Report

    report = Report(nodeID="")
    survey = report.survey.add(band=0, survey_type=0, timestamp_ms=timestamp_ms)
    sample = survey.survey_list.add(duration_ms=5000, busy=busy)
    if channel is not None:
        sample.channel = channel
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
        "channel; the clock stands at " + str(now) + " throughout. Per step: whether the "
        "report is used, and the agent's statistics status after it.",
        "topic": topic,
        "reporting_interval": 10,
        "clock": now,
        "steps": steps,
    }


VECTOR_SETS = {
    "al-mac.json": al_mac_vectors,
    "operation-transitions.json": transition_vectors,
    "translation-northbound.json": northbound_vectors,
    "translation-southbound.json": southbound_vectors,
    "fleet.json": fleet_vectors,
    "uplink.json": uplink_vectors,
    "cmdu.json": cmdu_vectors,
    "control.json": control_vectors,
    "steering.json": steering_vectors,
    "onboarding.json": onboarding_vectors,
    "telemetry.json": telemetry_vectors,
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
