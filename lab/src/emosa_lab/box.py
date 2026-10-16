# SPDX-License-Identifier: Apache-2.0
"""The lab in a box (easymesh-labs alignment plan 8.2): the real agent, C or Python,
against a fake pod and a scripted controller in a private network namespace, with no
lab VM.

    python -m emosa_lab.box boot --agent c --directory DIR

The pod is a disposable ovsdb-server holding the recorded rows of an unchanged
OpenSync 6.6.1 pod (tests/fixtures/opensync/pod-6.6.1-hwsim-tables.json); it dials
the agent as a pod does after its redirect. The agent's 1905 interface is one end of
a veth pair; the controller's side sees every frame it sends.

The box runs in a user and network namespace of its own (unshare), so it needs no
root and touches no host interface.
"""

import argparse
import asyncio
import codecs
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import ovs.jsonrpc
import ovs.stream

from emosa.agent.fleet import agent_config, derive_al
from emosa.opensync.schema import LEGACY, reference_path, reference_schema
from emosa.opensync.session import OvsSession
from emosa.wire.cmdu import Reassembler, Tlv, fragment_message
from emosa.wsc import decode_attributes
from emosa_lab.simulation.database import SimDatabase
from emosa_lab.simulation.manager import APPLY_COLUMNS
from emosa_lab.simulation.wsc_provisioning import registrar_reply

ROOT = Path(__file__).resolve().parents[3]
POD_ROWS = ROOT / "tests/fixtures/opensync/pod-6.6.1-hwsim-tables.json"
# another pod, on a Multi-AP Wi-Fi backhaul (bhaul-sta-24 on emosa-lab-bh, data plane option 1)
MULTI_AP_ROWS = ROOT / "tests/fixtures/opensync/pod-6.6.1-hwsim-uplink-multi-ap.json"
# a pod on its GTP bootstrap: bhaul-sta-24 in vconf mode on the bootstrap BSS, cm on the gretap.
# For the operator steer onto a parent and back (spec 8.6 revert); the agent awaits a steer.
BOOTSTRAP_ROWS = ROOT / "tests/fixtures/opensync/pod-6.6.1-hwsim-bootstrap.json"
BOOTSTRAP_BSS = "02:00:00:00:1b:01"
# a fleet GRE parent AP (spec 8.6): a steer onto it is a 3-address GRE join, not a Multi-AP switch
GRE_PARENT_AP = "72:00:00:00:0a:01"
BACKHAUL_PARENT, BACKHAUL_STATION = "02:00:00:00:19:01", "02:00:00:00:14:00"
UPLINK = {
    "mode": "multi-ap",
    "bssid": BACKHAUL_PARENT,
    "credentials": "config",
    "ssid": "emosa-lab-bh",
    "secret_ref": "backhaul",
    "station": "bhaul-sta-24",
}
CONTROLLER_AL = "02:00:00:00:00:02"
AGENT_PORT = 6651
ETHERTYPE = 0x893A
AGENT_INTERFACE, CONTROLLER_INTERFACE = "em0", "ctl0"
AUTOCONFIG_SEARCH, AUTOCONFIG_RESPONSE, AUTOCONFIG_WSC = 0x0007, 0x0008, 0x0009
REGISTRAR = ROOT / ".cache/wsc-registrar/component-registrar"  # scripts/build-wsc-registrar.py
REGISTRAR_SSID = "EMOSA-WSC-component"  # deploy/wire/component-registrar.c
COLD_VIF = "home-ap-24"  # the profiles' fronthaul VIF
# a legacy pod (spec 3.10): OpenSync 2.0's schema without multi_ap, two Broadcom radios, its
# station on its GRE uplink and no AP yet; its profile's fronthaul wl1.2 and GRE parent AP wl1.1
LEGACY_ROWS = ROOT / "tests/fixtures/opensync/pod-2.0-legacy-tables.json"
LEGACY_PROFILE = "bcm-pod-2.4.3-gre-v1"
LEGACY_PROFILE_5G = "bcm-pod-2.4.3-gre-5g-v1"  # with wl0 as a 5 GHz fronthaul only (wl0.2)
LEGACY_FRONTHAUL, LEGACY_PARENT, LEGACY_SLOT_5G = "wl1.2", "wl1.1", "wl0.2"
LEGACY_UNDERLAY = "169.254.5.0/24"  # not the fixture's leftover 169.254.2.1 on wl1.1


def in_namespace():
    """Re-execute this command in a private user and network namespace, once."""
    if os.environ.get("EMOSA_BOX_NAMESPACE") == "1":
        return
    env = {**os.environ, "EMOSA_BOX_NAMESPACE": "1"}
    command = ["unshare", "--net", "--map-root-user", sys.executable, "-m", "emosa_lab.box"]
    os.execvpe("unshare", command + sys.argv[1:], env)


def network(interface=AGENT_INTERFACE):
    """lo, and the veth pair between the agent's interface and the controller's."""
    for command in (
        ["ip", "link", "set", "lo", "up"],
        ["ip", "link", "add", interface, "type", "veth", "peer", "name", CONTROLLER_INTERFACE],
        ["ip", "link", "set", interface, "up"],
        ["ip", "link", "set", CONTROLLER_INTERFACE, "up"],
    ):
        subprocess.run(command, check=True)


def recorded_pod(rows=POD_ROWS, replace=None, drop=()):
    """The recorded pod's tables as one insert transaction (references by name), each
    text in replace (a MAC, say) changed into its value. drop: interfaces left out, as on
    a pod whose bootstrap made their radio only (their VIF and Inet rows, and the
    references to them)."""
    text = Path(rows).read_text()
    for old, new in (replace or {}).items():
        text = text.replace(old, new)
    tables = json.loads(text)["tables"]
    gone = {
        uuid
        for table in ("Wifi_VIF_Config", "Wifi_VIF_State", "Wifi_Inet_Config")
        for uuid, row in tables.get(table, {}).items()
        if row.get("if_name") in drop
    }
    tables = {t: {u: r for u, r in rows.items() if u not in gone} for t, rows in tables.items()}
    names = {uuid: f"row{i}" for i, uuid in enumerate(u for rows in tables.values() for u in rows)}

    def named(value):
        if isinstance(value, list) and len(value) == 2 and value[0] == "uuid":
            if value[1] in gone:  # a set of one, its only member left out
                return ["set", []]
            return ["named-uuid", names[value[1]]] if value[1] in names else value
        if isinstance(value, list) and len(value) == 2 and value[0] == "set":
            return ["set", [named(v) for v in value[1] if not gone.intersection(uuids(v))]]
        if isinstance(value, list):
            return [named(v) for v in value]
        return value

    operations = []
    for table, rows in tables.items():
        for uuid, row in rows.items():
            operations.append(
                {
                    "op": "insert",
                    "table": table,
                    "uuid-name": names[uuid],
                    "row": {column: named(value) for column, value in row.items()},
                }
            )
    return tables, operations


async def table_rows(db, table):
    """One table of the pod's database, every column, decoded."""
    schema = json.loads(Path(reference_path()).read_text())
    columns = sorted(schema["tables"][table]["columns"])
    session = OvsSession(db.endpoint, read_only=True, monitor_columns={table: columns})
    try:
        snapshot = await session.snapshot()
    finally:
        await session.close()
    return snapshot["tables"].get(table, {})


async def db_transact(db, operations):
    session = OvsSession(db.endpoint, schema_generation=db.generation)
    try:
        return await session.transact(operations)
    finally:
        await session.close()


async def seed_rows(db, schema):
    """spec 3.10: a legacy pod's vendor seed as the box keeps it, every row of the database as it
    was first made, raw ({table: [row with _uuid]}). The pod restores its seed at every start of
    its OpenSync, its rows with the same UUIDs (a converted pod, 14 October)."""
    tables = sorted(json.loads(Path(schema).read_text())["tables"])
    results = await db_transact(db, [{"op": "select", "table": t, "where": []} for t in tables])
    return {t: result["rows"] for t, result in zip(tables, results, strict=True)}


def reseeded(seed):
    """The seed's rows inserted again, each with its own UUID: one transaction."""
    return [
        {
            "op": "insert",
            "table": table,
            "uuid": row["_uuid"][1],
            "row": {c: v for c, v in row.items() if c not in ("_uuid", "_version")},
        }
        for table, rows in seed.items()
        for row in rows
    ]


FIRST_OCTETS = ("82", "a2", "c2", "e2", "86", "a6", "c6", "e6")  # locally administered


def uuids(value):
    """The UUIDs of a raw OVSDB reference or set of references."""
    if isinstance(value, list) and len(value) == 2 and value[0] == "uuid":
        return [value[1]]
    if isinstance(value, list) and len(value) == 2 and value[0] == "set":
        return [item[1] for item in value[1] if isinstance(item, list) and item[0] == "uuid"]
    return []


async def attach_vifs(db):
    """As OpenSync's wm does for a VIF a manager created: its State gets a BSSID (the radio's MAC
    under another locally administered first octet) and joins its radio's vif_states, where an
    agent's view of the pod finds the radio's BSSes. The number of changes made."""
    radio_configs = await table_rows(db, "Wifi_Radio_Config")
    radio_states = await table_rows(db, "Wifi_Radio_State")
    vif_states = await table_rows(db, "Wifi_VIF_State")
    by_config = {
        uuids(r.get("radio_config"))[0]: (u, r)
        for u, r in radio_states.items()
        if uuids(r.get("radio_config"))
    }
    taken = {v.get("mac") for v in vif_states.values() if isinstance(v.get("mac"), str)}
    changes = []
    for uuid, vif in vif_states.items():
        config = uuids(vif.get("vif_config"))
        if vif.get("mode") != "ap" or not config:
            continue
        radio_config = next(
            (u for u, r in radio_configs.items() if config[0] in uuids(r.get("vif_configs"))), None
        )
        if radio_config not in by_config:
            continue
        state, radio = by_config[radio_config]
        if not isinstance(vif.get("mac"), str) and isinstance(radio.get("mac"), str):
            mac = next(
                (o + radio["mac"][2:] for o in FIRST_OCTETS if o + radio["mac"][2:] not in taken),
                None,
            )
            if mac:
                taken.add(mac)
                where = [["_uuid", "==", ["uuid", uuid]]]
                changes.append(
                    {"op": "update", "table": "Wifi_VIF_State", "where": where, "row": {"mac": mac}}
                )
        if uuid not in uuids(radio.get("vif_states")):
            changes.append(
                {
                    "op": "mutate",
                    "table": "Wifi_Radio_State",
                    "where": [["_uuid", "==", ["uuid", state]]],
                    "mutations": [["vif_states", "insert", ["set", [["uuid", uuid]]]]],
                }
            )
    if changes:
        await db_transact(db, changes)
    return len(changes)


REPLY_CUT_PORT = 6661  # the reply cut's end, which the pod dials in place of the agent


class ReplyCut:
    """Between the pod's database and its agent: every message passed on, but the agent's first
    transaction that `match` picks meets a fault, as when the pod's management link fails at
    that moment:
    - refuse=False: the database commits it, its reply is dropped and both ends closed (spec 5:
      the outcome unknown, INDETERMINATE); the pod dials again through it;
    - refuse=True: it never reaches the database, and the agent gets an error result for it on
      the same connection (the write did not happen, the link stays up).
    After that fault every message is passed on as before."""

    def __init__(self, match, refuse=False):
        self.match, self.refuse, self.done, self.server = match, refuse, False, None

    async def start(self):
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", REPLY_CUT_PORT)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def serve(self, pod_reader, pod_writer):
        try:
            agent_reader, agent_writer = await asyncio.open_connection("127.0.0.1", AGENT_PORT)
        except OSError:
            pod_writer.close()
            return
        awaited = set()  # the ids of the matched transactions, whose replies are cut

        def from_agent(message):  # "pass", "drop" or "cut"
            if self.done or message.get("method") != "transact" or not self.match(message):
                return "pass"
            if not self.refuse:
                awaited.add(json.dumps(message.get("id")))
                return "pass"
            self.done = True
            error = {"error": "box: refused", "details": "the reply cut refused it"}
            reply = {"id": message.get("id"), "result": [error], "error": None}
            agent_writer.write(json.dumps(reply).encode())
            return "drop"

        def from_pod(message):
            if json.dumps(message.get("id")) in awaited and "method" not in message:
                self.done = True
                return "cut"
            return "pass"

        async def pump(reader, writer, verdict):
            decoder, text, buffer = json.JSONDecoder(), codecs.getincrementaldecoder("utf-8")(), ""
            try:
                while data := await reader.read(65536):
                    buffer += text.decode(data)
                    while buffer.strip():
                        try:
                            message, end = decoder.raw_decode(buffer.lstrip())
                        except ValueError:
                            break  # the rest of the message is still to come
                        buffer = buffer.lstrip()[end:]
                        outcome = verdict(message)
                        if outcome == "cut":
                            return
                        if outcome == "pass":
                            writer.write(json.dumps(message).encode())
                    await writer.drain()
                    await agent_writer.drain()  # a refusal written back to the agent
            except (ConnectionError, OSError):
                pass

        pumps = [
            asyncio.ensure_future(pump(agent_reader, pod_writer, from_agent)),
            asyncio.ensure_future(pump(pod_reader, agent_writer, from_pod)),
        ]
        await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in pumps:
            task.cancel()
        for writer in (pod_writer, agent_writer):
            writer.close()


def writes_registrar_ssid(message):
    """The agent's write of the registrar's SSID (an M2's configuration)."""
    return REGISTRAR_SSID in json.dumps(message)


def deletes_steering_client(message):
    """The agent's close of a steering window: a delete of a Band_Steering_Clients row."""
    return any(
        isinstance(op, dict)
        and op.get("op") == "delete"
        and op.get("table") == "Band_Steering_Clients"
        for op in (message.get("params") or [])[1:]
    )


REPLY_CUTS = {  # the faults a box can put between the pod and its agent
    "lost-write": lambda: ReplyCut(writes_registrar_ssid),
    "refused-close": lambda: ReplyCut(deletes_steering_client, refuse=True),
}


async def dial(db, endpoint, *, connect=True):
    """Make the pod's database connect to the agent, as a pod does after its redirect; or,
    with connect=False, drop that connection (the pod's management link cut)."""
    action = "add-remote" if connect else "remove-remote"

    def command():
        error, stream = ovs.stream.Stream.open_block(
            ovs.stream.Stream.open("unix:" + str(db.directory / "control.sock")), 5000
        )
        if error:
            raise RuntimeError("the pod's database has no control socket")
        rpc = ovs.jsonrpc.Connection(stream)
        try:
            error, reply = rpc.transact_block(
                ovs.jsonrpc.Message.create_request("ovsdb-server/" + action, [endpoint])
            )
            if error or reply.error is not None:
                raise RuntimeError("the pod's database did not take the remote")
        finally:
            rpc.close()

    await asyncio.to_thread(command)


async def wait_for_port(port, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            await asyncio.sleep(0.05)
    raise RuntimeError(f"nothing listens on port {port}")


def agent_command(agent, config_path, binary=None, profiles=None):
    """The agent's command line; ``profiles`` the profiles' directory (EMOSA_PROFILES for the
    Python agent, ``--profiles`` for the C), the bundled ones by default."""
    if agent == "c":
        path = Path(binary) if binary else ROOT / "c/build/emosa-agent-c"
        return [
            str(path),
            str(config_path),
            "--profiles",
            str(profiles or ROOT / "src/emosa/profiles"),
        ]
    return [sys.executable, "-m", "emosa.agent.pod", str(config_path)]


class Controller:
    """The controller's end of the veth: the messages the agent sends, and ours to it."""

    def __init__(self, interface, agent_al):
        self.socket = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE))
        self.socket.bind((interface, ETHERTYPE))
        self.socket.setblocking(False)
        self.reassembler = Reassembler()
        self.messages, self.arrived = [], []
        self.last_sent = time.monotonic()
        self.agent = bytes.fromhex(agent_al.replace(":", ""))
        self.al = bytes.fromhex(CONTROLLER_AL.replace(":", ""))
        self.mid = 0x4000

    def poll(self):
        while True:
            try:
                frame = self.socket.recv(65535)
            except BlockingIOError:
                return
            try:
                message = self.reassembler.feed(frame, ingress=CONTROLLER_INTERFACE)
            except Exception:  # a frame the reference refuses is not a message
                continue
            if message is not None:
                self.messages.append(message)
                self.arrived.append(time.monotonic())

    def when(self, message):
        """When the controller's side received this message (monotonic seconds)."""
        return self.arrived[next(i for i, m in enumerate(self.messages) if m is message)]

    def latest(self, message_type):
        found = [m for m in self.messages if m.message_type == message_type]
        return found[-1] if found else None

    def send(self, message_type, tlvs, mid=None):
        if mid is None:
            self.mid = (self.mid + 1) & 0xFFFF
            mid = self.mid
        for frame in fragment_message(self.agent, self.al, message_type, mid, tuple(tlvs)):
            self.socket.send(frame)
        self.last_sent = time.monotonic()
        return mid

    def send_frame(self, frame):
        """Raw bytes on the controller's end of the link, as any host on the LAN may."""
        self.socket.send(frame)

    def reply(self, message_type, mid):
        """The agent's message of this type for our request mid (a response or an ACK)."""
        return next(
            (m for m in self.messages if m.message_type == message_type and m.mid == mid), None
        )

    def close(self):
        self.socket.close()


TELEMETRY = {  # the pod's statistics through a broker in the box (spec 3.6)
    "mode": "mqtt",
    "broker": "127.0.0.1",
    "port": 1883,
    "subscribe": "127.0.0.1:1883",
    "reporting_interval": 10,
    "survey": True,
}
FOREIGN_BROKER = [["broker", "cloud.example.net"], ["port", "443"], ["topics", "operator/stats"]]
# spec 3.10: a legacy pod's seed with its way to its operator's cloud, qm's broker (FOREIGN_BROKER)
# and headers, and the statistics rows it samples for them
OPERATOR_HEADERS = [["locationId", "site-1"], ["nodeId", "node-1"]]
OPERATOR_STATS = (
    {"stats_type": "client", "radio_type": "2.4G", "report_type": "raw", "reporting_interval": 60},
    {
        "stats_type": "survey",
        "radio_type": "5G",
        "survey_type": "on-chan",
        "reporting_interval": 60,
    },
)
CLOUD_COLUMNS = ("mqtt_headers", "mqtt_settings", "mqtt_topics")
# spec 4, the pods' modules: a module of the pod's own (a hook, say) with a setting and an update,
# the pod's Node_Config with a key the module no longer has and other modules' rows (a converted
# pod's seed has its operator's cloud's for several of its managers' modules, 14 October)
NODE_UPDATE = "b" * 64
NODE_MODULES = {"hook": {"config": {"redirector": "auto"}, "update": NODE_UPDATE}}
FOREIGN_NODE_ROWS = (
    ("vendor", "level", "debug"),
    ("fm", "enabled", "true"),
    ("healthcheck", "interval", "60"),
    ("healthcheck", "failures", "10"),
    ("iot", "claim_key", "operator-claim"),
)
NODE_ROWS = (("hook", "stale", "x"), *FOREIGN_NODE_ROWS)
NODE_ADVERTISE = "192.0.2.1"


class Box:
    """The agent, its pod and its controller, started in the current network namespace.
    With telemetry, a broker (mosquitto) takes the pod's statistics; `foreign_broker`
    starts the pod with another manager's broker in its MQTT settings."""

    def __init__(
        self,
        agent,
        directory,
        *,
        binary=None,
        telemetry=False,
        foreign_broker=False,
        backhaul=False,
        bootstrap=False,
        multi_bss=False,
        uplink=None,
        topology_query_window=None,
        netns=False,
        kept_target=None,
        profile=None,
        m2_modes=None,
        wired=False,
        cold=False,
        reply_cut=False,
        underlay=None,
        gre_child=False,
        legacy=False,
        ethernet_first=None,
        gtp_station=None,
        operator_cloud=False,
        stations_off=False,
        modules=None,
        seed_stations=False,
        seeded_fronthaul=False,
        vendor_bridges=False,
        old_profiles=False,
        phy_netdev_up=None,
    ):
        self.agent, self.directory, self.binary = agent, directory, binary
        # seeded_fronthaul (legacy): the seed with wl1.2 on, as the operator's cloud left it (its
        # SSID, key and oftag), its State taken by wm: a hooked boot's fronthaul (15 October);
        # "applied": with the controller's SSID and key, as EMOSA made it, but in no bridge port
        self.seeded_fronthaul = seeded_fronthaul
        # vendor_bridges (legacy): the bridges' Inet rows as a hooked boot's seed has them (15
        # October): br-home's down (network false), br-wan's NAT on (no patch pair: the seed's)
        self.vendor_bridges = vendor_bridges
        # old_profiles: the agents read the profiles as an image before 15 October has them (the
        # legacy ones' fronthaul rows without group_rekey and ft_psk), a binary deployed alone
        self.old_profiles = old_profiles
        # phy_netdev_up: the fleet's setting for the radios' own netdevs (spec 3.10), when set; the
        # agents' default (on) otherwise
        self.phy_netdev_up = phy_netdev_up
        # seed_stations (legacy): the seed with onboarding stations on wl0 and wl1, as a converted
        # pod's own seed brings them back at a hooked boot (14 October)
        self.seed_stations = seed_stations
        # modules: the fleet's modules (spec 4), the pod's Node_Config with NODE_ROWS beside
        self.modules = modules
        # operator_cloud (legacy): the seed with its operator's cloud, qm's broker and headers and
        # two statistics rows (spec 3.10)
        self.operator_cloud = operator_cloud
        # stations_off: the pod with a backhaul station on each radio, every one off and none its
        # uplink, as the Pis' bootstrap leaves a wired pod (14 October)
        self.stations_off = stations_off
        # legacy: the pod is a legacy pod (spec 3.10), on its own schema and rows, its agent on
        # the legacy profile unless the scenario gives another
        self.legacy = legacy
        # ethernet_first (legacy): wired as the first legacy pod was admitted on the router (13
        # October): cm on eth0 and no GRE, its patch pair already joining br-wan to br-home, and
        # its station "linked" beside the agent to the GTP's BSS, associated there but unused, or
        # as its vendor's "seed" has it: no station, the operator's credentials unlinked
        self.ethernet_first = ethernet_first
        # gtp_station: the agent's uplink.bootstrap (spec 3.10, the GTP station), its secret
        # GTP_KEY in the agent's secret directory
        self.gtp_station = gtp_station
        # underlay (with a gre-parent profile): the pod's 169.254.N.0/24 as a GRE parent (spec
        # 8.6), given to the agent as the fleet registry would; its parent AP takes the .1
        self.underlay = underlay
        self.topology_query_window = topology_query_window
        # netns: the agent's interface in a namespace of its own (EMOSA_NETNS), as on a
        # gateway whose EasyMesh controller is local
        self.netns, self.holder, self.netns_path = netns, None, None
        self.telemetry, self.foreign_broker, self.backhaul = telemetry, foreign_broker, backhaul
        # multi_bss: the controller answers each M1 with an M2 set, a fronthaul and a backhaul
        # BSS (the registrar's backhaul mode); uplink: the agent's uplink setting instead of
        # UPLINK (with backhaul)
        self.multi_bss, self.uplink = multi_bss, uplink
        # kept_target (with backhaul): the upstream a controller's move left on an earlier
        # start, kept in the agent's state for this one
        self.kept_target = kept_target
        # m2_modes: the set the controller sends instead (one registrar mode per BSS);
        # profile: the agent's pod profile instead of the fleet's default
        self.m2_modes = m2_modes or (("configure", "backhaul") if multi_bss else ("configure",))
        # radio_m2_modes: another set for the radio of this RUID ("02:00:00:00:05:00"), as a
        # controller configures each band (spec 9)
        self.radio_m2_modes = {}
        self.profile = profile
        # wired: a wired pod, cm using its Ethernet port eth1 as the uplink; the agent's
        # uplink mode ethernet (spec 8.4)
        self.wired = wired
        # cold: the pod's fronthaul VIF left out, as on a pod whose bootstrap made its radio
        # only (the Pis'): the agent creates it from the M2 (spec 3.2), and the pod's managers
        # give it a BSSID on the radio
        self.cold = cold
        self.drop = (COLD_VIF,) if cold else ()
        # reply_cut: the pod dials the agent through a ReplyCut, one of REPLY_CUTS (lost-write:
        # the reply to the agent's first write of the registrar's SSID lost; refused-close: the
        # agent's first steering close refused on the same connection)
        self.reply_cut = REPLY_CUTS[reply_cut]() if reply_cut else None
        # bootstrap (with backhaul): the pod on its GTP bootstrap, the agent awaiting a steer
        self.bootstrap = bootstrap
        self.pod_rows = BOOTSTRAP_ROWS if bootstrap else (MULTI_AP_ROWS if backhaul else POD_ROWS)
        # gre_child (with backhaul): the recorded pod as it is, its 5 GHz station on its GRE uplink,
        # with the agent's uplink setting: a GRE child of the Pis' shape (spec 8.6, 9)
        if gre_child:
            self.pod_rows = POD_ROWS
        if stations_off:
            self.pod_rows = MULTI_AP_ROWS
        self.schema = None  # the pod's schema file: the 6.6 reference unless a legacy pod's
        if legacy:
            self.pod_rows = LEGACY_ROWS
            self.profile = profile or LEGACY_PROFILE
        self.process = self.db = self.controller = self.log = self.broker = None
        self.interface = AGENT_INTERFACE  # the agent's 1905 interface (a fleet names its own)

    async def __aenter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        network(self.interface)
        if self.legacy:  # OpenSync 2.0's schema as the legacy pod has it: no multi_ap
            self.schema = self.directory / "legacy.ovsschema"
            self.schema.write_text(json.dumps(reference_schema(LEGACY)))
        tables, operations = recorded_pod(self.pod_rows, drop=self.drop)
        self.serial = next(iter(tables["AWLAN_Node"].values()))["serial_number"]
        if self.foreign_broker:
            node = next(o for o in operations if o["table"] == "AWLAN_Node")
            node["row"]["mqtt_settings"] = ["map", FOREIGN_BROKER]
        if self.operator_cloud:
            node = next(o for o in operations if o["table"] == "AWLAN_Node")
            node["row"]["mqtt_settings"] = ["map", FOREIGN_BROKER]
            node["row"]["mqtt_headers"] = ["map", OPERATOR_HEADERS]
            operations = [
                *operations,
                *(
                    {"op": "insert", "table": "Wifi_Stats_Config", "row": dict(r)}
                    for r in OPERATOR_STATS
                ),
            ]
        if self.modules:
            operations = [
                *operations,
                *(
                    {
                        "op": "insert",
                        "table": "Node_Config",
                        "row": {"module": m, "key": k, "value": v, "persist": True},
                    }
                    for m, k, v in NODE_ROWS
                ),
            ]
        if self.stations_off:
            for o in operations:
                if (
                    o["table"] in ("Wifi_VIF_Config", "Wifi_VIF_State")
                    and o["row"].get("mode") == "sta"
                ):
                    o["row"]["enabled"] = False
                    if o["table"] == "Wifi_VIF_State":
                        o["row"]["parent"] = ["set", []]
                if o["table"] == "Connection_Manager_Uplink":
                    o["row"]["is_used"] = False
        if self.telemetry:
            # anonymous, local, and as the namespace's own root: no privileges to drop
            broker_config = self.directory / "broker.conf"
            broker_config.write_text("listener 1883 127.0.0.1\nallow_anonymous true\nuser root\n")
            self.broker = subprocess.Popen(
                ["mosquitto", "-c", str(broker_config)],
                stdout=(self.directory / "broker.log").open("w"),
                stderr=subprocess.STDOUT,
            )
            await wait_for_port(1883)
        # its own short directory: Unix socket paths are limited to 108 bytes
        self.db = SimDatabase(schema=self.schema, generation=LEGACY if self.legacy else None)
        await self.db.start()
        session = OvsSession(self.db.endpoint, schema_generation=self.db.generation)
        try:
            await session.transact(operations)
            if self.wired:
                await session.transact(
                    [
                        {
                            "op": "insert",
                            "table": "Connection_Manager_Uplink",
                            "row": {
                                "if_name": "eth1",
                                "if_type": "eth",
                                "is_used": True,
                                "has_L2": True,
                                "has_L3": True,
                            },
                        }
                    ]
                )
            if self.ethernet_first:
                await session.transact(ethernet_first(self.ethernet_first))
            if self.seed_stations:  # True: on wl0 and wl1; else the radios named
                names = ("wl0", "wl1") if self.seed_stations is True else self.seed_stations
                await session.transact(seed_stations(names))
            if self.vendor_bridges:
                await session.transact(vendor_bridges())
            if self.seeded_fronthaul:
                await session.transact(seeded_fronthaul(self.seeded_fronthaul == "applied"))
        finally:
            await session.close()
        if self.legacy:  # its vendor's seed, as every start of its OpenSync restores it
            self.seed = await seed_rows(self.db, self.schema)
        entry = {
            "pod_id": self.serial,
            "port": AGENT_PORT,
            "interface": self.interface,
            "al_mac": derive_al(self.serial, [CONTROLLER_AL]),
        }
        self.al_mac = entry["al_mac"]
        state_root = self.directory / "state"
        # a run root, as on a gateway (a RAM disk there): the status in the run directory
        self.run_root = self.directory / "run"
        fleet = {
            "controller_al": CONTROLLER_AL,
            "state_root": str(state_root),
            "run_root": str(self.run_root),
        }
        if self.telemetry:
            fleet["telemetry"] = TELEMETRY
        if self.multi_bss:
            fleet["multi_bss"] = True
        if self.profile:
            fleet["profile"] = self.profile
        if self.underlay:
            # the fleet's own GTP underlay and this pod's allocated underlay, as the registry
            # would hand them to the agent (spec 8.6)
            fleet["gtp_underlay"] = "169.254.1.0/24"
            entry["underlay"] = self.underlay
        if self.wired:
            fleet["uplink"] = {"mode": "ethernet"}
        if self.phy_netdev_up is not None:
            fleet["phy_netdev_up"] = self.phy_netdev_up
        if self.topology_query_window:
            fleet["topology_query_window"] = self.topology_query_window
        if self.modules:  # the update's URL names the fleet's advertise address
            fleet["modules"], fleet["advertise"] = self.modules, NODE_ADVERTISE
        if self.backhaul:  # option 1, its credentials from the agent's own configuration
            fleet["uplink"] = self.uplink or UPLINK
            secrets = state_root / self.serial / "secrets"
            secrets.mkdir(parents=True, mode=0o700)
            (secrets / "backhaul").write_text("recorded-psk-replaced")
            (secrets / "backhaul").chmod(0o600)
            if self.kept_target:
                kept = state_root / self.serial / "uplink"
                kept.mkdir(parents=True, mode=0o700)
                configured = (self.uplink or UPLINK)["bssid"]
                (kept / "target.json").write_text(
                    json.dumps({"configured": configured, "target": self.kept_target, "moves": 1})
                )
        if self.gtp_station:  # the site's GTP BSS, its passphrase by reference (spec 3.10)
            fleet["uplink"] = {
                **fleet.get("uplink", {"mode": "off"}),
                "bootstrap": self.gtp_station,
            }
            secrets = state_root / self.serial / "secrets"
            secrets.mkdir(parents=True, mode=0o700, exist_ok=True)
            (secrets / self.gtp_station["secret_ref"]).write_text(GTP_KEY)
            (secrets / self.gtp_station["secret_ref"]).chmod(0o600)
        config = agent_config(entry, fleet)
        # the agent's interface carries its AL MAC, as the labs' macvlan per agent does
        subprocess.run(["ip", "link", "set", self.interface, "address", self.al_mac], check=True)
        if self.netns:
            self.holder = subprocess.Popen(["unshare", "--net", "sleep", "infinity"])
            own = os.readlink("/proc/self/ns/net")
            self.netns_path = f"/proc/{self.holder.pid}/ns/net"
            for _ in range(50):
                if os.path.exists(self.netns_path) and os.readlink(self.netns_path) != own:
                    break
                await asyncio.sleep(0.1)
            subprocess.run(
                ["ip", "link", "set", self.interface, "netns", str(self.holder.pid)], check=True
            )
            for command in (
                ["ip", "link", "set", "lo", "up"],
                ["ip", "link", "set", self.interface, "up"],
            ):
                subprocess.run(["nsenter", f"--net={self.netns_path}", *command], check=True)
        state_root.mkdir(mode=0o700, exist_ok=True)  # the fleet's state_root
        self.status_path = state_root / self.serial / "status.json"
        self.controller = Controller(CONTROLLER_INTERFACE, self.al_mac)
        self.env = {
            **os.environ,
            "EMOSA_SCHEMAS": str(ROOT / "schemas"),
            "EMOSA_PROFILES": str(
                old_profiles(self.directory) if self.old_profiles else ROOT / "src/emosa/profiles"
            ),
            **({"EMOSA_NETNS": self.netns_path} if self.netns else {}),
        }
        if self.reply_cut:
            await self.reply_cut.start()
        await self.start(config, fleet)
        return self

    async def start(self, config, fleet):
        """The pod's agent started with this configuration, and the pod dialing it."""
        config_path = self.directory / "agent.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        self.log = (self.directory / "agent.log").open("w")
        self.command = agent_command(
            self.agent, config_path, self.binary, self.env["EMOSA_PROFILES"]
        )
        self.process = subprocess.Popen(
            self.command, stdout=self.log, stderr=self.log, env=self.env
        )
        await asyncio.sleep(1)
        port = REPLY_CUT_PORT if self.reply_cut else AGENT_PORT
        await dial(self.db, f"tcp:127.0.0.1:{port}")

    def restart_agent(self):
        """The agent started again with its configuration and state directory."""
        self.process = subprocess.Popen(
            self.command, stdout=self.log, stderr=self.log, env=self.env
        )

    async def __aexit__(self, *exc):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.log:
            self.log.close()
        if self.controller:
            self.controller.close()
        if self.db:
            await self.db.close()
        if self.reply_cut:
            await self.reply_cut.close()
        if self.broker and self.broker.poll() is None:
            self.broker.terminate()
            self.broker.wait(5)
        if self.holder and self.holder.poll() is None:
            self.holder.terminate()  # its namespace, and the agent's end of the veth, go with it
            self.holder.wait(5)

    def status(self):
        try:
            return json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            return None

    async def until(self, predicate, seconds=60):
        """Poll the controller's side until predicate() holds or the agent exits."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and self.process.poll() is None:
            self.controller.poll()
            value = predicate()
            if value:
                return value
            await asyncio.sleep(0.2)
        return None

    async def rows(self, table):
        """The pod's rows of one table, every column, decoded."""
        return await table_rows(self.db, table)

    def counts(self):
        return ((self.status() or {}).get("session") or {}).get("counts") or {}

    def sent(self, message_type):
        """The agent's messages of this type so far, in order."""
        self.controller.poll()
        return [m for m in self.controller.messages if m.message_type == message_type]

    async def ask(self, message_type, tlvs, expected, seconds=15):
        """A request to the agent, and its reply of the expected type with the request's MID."""
        mid = self.controller.send(message_type, tlvs)
        return await self.until(lambda: self.controller.reply(expected, mid), seconds=seconds)

    async def transact(self, operations):
        return await db_transact(self.db, operations)

    async def vif(self, table, if_name):
        """(uuid, row) of the pod's VIF by name in Wifi_VIF_Config or Wifi_VIF_State."""
        for uuid, row in (await self.rows(table)).items():
            if row.get("if_name") == if_name:
                return uuid, row
        return None, None

    async def client_join(self, mac, if_name="home-ap-24"):
        """A station associates with the pod's BSS: owm's client row, on the VIF's State."""
        uuid, _ = await self.vif("Wifi_VIF_State", if_name)
        await self.transact(
            [
                {
                    "op": "insert",
                    "table": "Wifi_Associated_Clients",
                    "uuid-name": "joined",
                    "row": {"mac": mac, "state": "active"},
                },
                {
                    "op": "mutate",
                    "table": "Wifi_VIF_State",
                    "where": [["_uuid", "==", ["uuid", uuid]]],
                    "mutations": [
                        ["associated_clients", "insert", ["set", [["named-uuid", "joined"]]]]
                    ],
                },
            ]
        )

    async def client_leave(self, mac, if_name="home-ap-24"):
        uuid, _ = await self.vif("Wifi_VIF_State", if_name)
        client = next(
            u
            for u, r in (await self.rows("Wifi_Associated_Clients")).items()
            if r.get("mac") == mac
        )
        await self.transact(
            [
                {
                    "op": "mutate",
                    "table": "Wifi_VIF_State",
                    "where": [["_uuid", "==", ["uuid", uuid]]],
                    "mutations": [["associated_clients", "delete", ["set", [["uuid", client]]]]],
                },
                {
                    "op": "delete",
                    "table": "Wifi_Associated_Clients",
                    "where": [["_uuid", "==", ["uuid", client]]],
                },
            ]
        )

    async def new_pod_database(self, replace=None, *, away=0, before_dial=None):
        """The pod's OpenSync started again (its container with its extender, say): a new
        database from the template, with new row UUIDs (and replace's changes: other
        radios), dialing the agent again, ``away`` seconds after the old one went. A legacy
        pod's is its vendor's seed again, every row with the UUID it had there (spec 3.10: its
        radio rows too, so they do not tell the starts apart). ``before_dial``, a coroutine
        function, reads the new database before the agent can act on it (the C agent clears a
        seed's stations within milliseconds of the dial): its result is returned."""
        old = self.db
        await old.close()
        await asyncio.sleep(away)
        self.db = SimDatabase(schema=self.schema, generation=old.generation)
        await self.db.start()
        if self.legacy and replace is None:
            await self.transact(reseeded(self.seed))
        else:
            tables, operations = recorded_pod(self.pod_rows, replace, self.drop)
            await self.transact(operations)
            if self.ethernet_first:  # the same legacy pod again, as its seed has it
                await self.transact(ethernet_first(self.ethernet_first))
        seen = await before_dial() if before_dial else None
        await dial(self.db, f"tcp:127.0.0.1:{AGENT_PORT}")
        return seen

    def result(self, **extra):
        return {
            "agent": self.agent,
            "serial": self.serial,
            "al_mac": self.al_mac,
            "exit": self.process.poll(),
            "messages": sorted({f"0x{m.message_type:04x}" for m in self.controller.messages}),
            "session": (self.status() or {}).get("session", {})
            and self.status()["session"].get("state"),
            **extra,
        }


# spec 3.10: OpenSync 2.4.3's Broadcom wm (bcmwl_nas_update_security) restarts a legacy pod's
# authenticator (nas, eapd) in full for a new AP or a change of its security entries beyond the
# ssid and key (encryption, mode, oftag); a new ssid or key alone is a fast reload
FULL_RELOAD = ("encryption", "mode", "oftag")
AUTH_RELOAD = "~reload"  # the suffix the agent's write gives an AP's oftag (PodBackend)


def auth_reload(state, config):
    """The reload a legacy pod's wm makes applying an AP's Config over its State (raw rows, the
    State None for a new AP): "full", "fast" or None."""
    if state is None:
        return "full"
    old, new = ovsdb_map(state.get("security")), ovsdb_map(config.get("security"))
    if any(old.get(k) != new.get(k) for k in FULL_RELOAD):
        return "full"
    if state.get("ssid") != config.get("ssid") or old.get("key") != new.get("key"):
        return "fast"
    return None


async def bridges_ports(db):
    """Each Bridge's name -> the names of the Ports in its ports."""
    ports = await table_rows(db, "Port")
    return {
        b.get("name"): {ports[u].get("name") for u in uuids(b.get("ports")) if u in ports}
        for b in (await table_rows(db, "Bridge")).values()
    }


async def bridges_up(db):
    """Each bridge's name -> whether its interface is up: its Inet row enabled with network
    (OpenSync's NM), or True with no Inet row (a bridge the box's pod does not model)."""
    rows = {r.get("if_name"): r for r in (await table_rows(db, "Wifi_Inet_Config")).values()}
    return {
        name: name not in rows
        or (rows[name].get("enabled") is True and rows[name].get("network") is True)
        for name in await bridges_ports(db)
    }


async def dhcp_reaches_lan(db, home="br-home", wan="br-wan"):
    """spec 3.10: a home AP's client reaches the router's LAN (its DHCP server) through the pod in
    bridge mode: the home bridge up, the WAN bridge's NAT off and OpenSync's patch pair joining
    the two (patch-h2w in the home bridge, patch-w2h in the WAN bridge)."""
    members, up = await bridges_ports(db), await bridges_up(db)
    inets = {r.get("if_name"): r for r in (await table_rows(db, "Wifi_Inet_Config")).values()}
    return (
        up.get(home) is True
        and home in inets
        and (inets.get(wan) or {}).get("NAT") is not True
        and "patch-h2w" in members.get(home, set())
        and "patch-w2h" in members.get(wan, set())
    )


async def apply_configuration(db):
    """The pod's managers: each VIF's State takes its Config's applied columns (a new
    VIF gets its State row), as OpenSync's wm does once the radio has applied them. On a
    legacy pod, each AP's authenticator reload is recorded (db.applied) with whether the
    authenticator serves the AP after a full one (db.served: its Port in its bridge, the bridge
    up, spec 3.10), and an oftag the agent marked is applied again once the agent restores it (wm
    takes every change)."""
    session = OvsSession(db.endpoint, schema_generation=db.generation)
    marked = set()
    members = await bridges_ports(db) if db.generation == LEGACY else {}
    up = await bridges_up(db) if db.generation == LEGACY else {}
    try:
        snapshot = await session.snapshot()
        tables, schema = snapshot["tables"], snapshot["schema"]
        states = {
            schema.decode("Wifi_VIF_State", "if_name", row["if_name"]): uuid
            for uuid, row in tables.get("Wifi_VIF_State", {}).items()
        }
        changes = []
        for uuid, row in tables["Wifi_VIF_Config"].items():
            if schema.decode("Wifi_VIF_Config", "mode", row.get("mode", "")) != "ap":
                continue  # a station's State is cm's and its supplicant's, not wm's
            name = schema.decode("Wifi_VIF_Config", "if_name", row["if_name"])
            applied = {c: row[c] for c in [*APPLY_COLUMNS, "multi_ap"] if c in row}
            oftag = ovsdb_map(row.get("security")).get("oftag")
            state = tables.get("Wifi_VIF_State", {}).get(states.get(name))
            legacy = db.generation == LEGACY
            kind = auth_reload(state, row) if legacy else None
            db.applied.append((name, kind, oftag))
            if kind == "full":  # the authenticator's start: it hears the AP's bridge, when up
                bridge = row.get("bridge")
                db.served[name] = not bridge or (
                    name in members.get(bridge, set()) and up.get(bridge, True)
                )
            if legacy and state and (oftag or "").endswith(AUTH_RELOAD):
                marked.add(name)  # a new AP's: once the box has attached its State (onboarded)
            if name in states:
                where = [["_uuid", "==", ["uuid", states[name]]]]
                changes.append(
                    {"op": "update", "table": "Wifi_VIF_State", "where": where, "row": applied}
                )
            else:
                applied |= {"if_name": row["if_name"], "vif_config": ["uuid", uuid]}
                changes.append({"op": "insert", "table": "Wifi_VIF_State", "row": applied})
        await session.transact(changes)
        deadline = time.monotonic() + 15
        while marked and time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            rows = (await session.snapshot())["tables"]["Wifi_VIF_Config"].values()
            if not any(
                (ovsdb_map(r.get("security")).get("oftag") or "").endswith(AUTH_RELOAD)
                for r in rows
                if schema.decode("Wifi_VIF_Config", "if_name", r["if_name"]) in marked
            ):
                break
        else:
            marked = set()  # none marked, or the agent restored none in time: nothing to apply
    finally:
        await session.close()
    if marked:
        await apply_configuration(db)  # the agent's restore, as wm takes it


async def auth_reload_settled(db, seconds=15):
    """A legacy pod's APs the agent made: once their State is attached the agent restores the
    oftags it marked (spec 3.10), and wm takes that change too."""
    session = OvsSession(db.endpoint, schema_generation=db.generation)
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            rows = (await session.snapshot())["tables"].get("Wifi_VIF_Config", {}).values()
            if not any(
                (ovsdb_map(r.get("security")).get("oftag") or "").endswith(AUTH_RELOAD)
                for r in rows
            ):
                break
            await asyncio.sleep(0.3)
    finally:
        await session.close()
    await apply_configuration(db)


async def boot(box):
    """The agent takes the pod's connection and searches for its controller. Its status is
    in its run directory, the state directory's status.json a link to it (spec §6)."""
    searched = await box.until(lambda: box.controller.latest(AUTOCONFIG_SEARCH))
    await box.until(lambda: box.status() is not None)
    target = box.run_root / box.serial / "status.json"
    linked = (
        box.status_path.is_symlink()
        and os.readlink(box.status_path) == str(target)
        and target.is_file()
    )
    return box.result(passed=bool(searched) and linked, status_linked=linked)


async def onboard(box):
    """The controller answers: the Response, then an M2 from hostap's registrar for the
    agent's own M1. The agent writes the credentials to the pod, the pod's manager
    applies them, and the agent sees them applied."""
    state = await onboarded(box)
    if "failed" in state:
        return box.result(passed=False, failed=state["failed"])
    return box.result(passed=state["applied"], wrote=True, operations=state["operations"])


async def answer_m1(box, ruid, wsc):
    """The registrar's answer to one M1: an M2, or with multi_bss an M2 set (one per BSS,
    each from its own registrar session: m2_session distinct)."""
    modes = box.radio_m2_modes.get(ruid.hex(":"), box.m2_modes)
    m2s = [await asyncio.to_thread(registrar_reply, REGISTRAR, wsc, mode) for mode in modes]
    box.controller.send(AUTOCONFIG_WSC, (Tlv(0x82, ruid), *(Tlv(0x11, m2) for m2 in m2s)))


async def onboarded(box):
    """onboard's steps, which other scenarios start from: the agent's radio (its RUID) and
    whether the credentials were applied, or what failed. As a controller does, every
    Search gets a Response and every M1 an M2 until the credentials are applied (a slow
    agent may end a session before its answer arrives and start another)."""
    if not await box.until(lambda: box.controller.latest(AUTOCONFIG_SEARCH)):
        return {"failed": "no search"}
    deadline, searches, m1s, ruid, wrote = time.monotonic() + 90, 0, 0, None, False
    pending = False  # an M2 sent whose credentials the pod's managers have not applied yet
    settle = False  # a legacy pod's APs the agent made: their oftags restored once applied

    def applied():
        operations = (box.status() or {}).get("operations") or []
        return any(
            "APPLIED" in str(op.get("state", "")) for op in operations if isinstance(op, dict)
        )

    while time.monotonic() < deadline and box.process.poll() is None:
        found = box.sent(AUTOCONFIG_SEARCH)
        # an EasyMesh 6.1 Response: registrar, 2.4 GHz, the controller service, Profile 1,
        # and Controller Capability with KiB/MiB counters and the Early AP Capability Report
        for search in found[searches:]:
            box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
        searches = len(found)
        found = box.sent(AUTOCONFIG_WSC)
        for m1 in found[m1s:]:
            wsc = next(t.value for t in m1.tlvs if t.kind == 0x11)
            ruid = next(t.value[:6] for t in m1.tlvs if t.kind == 0x85)  # AP Radio Basic Caps
            await answer_m1(box, ruid, wsc)
            pending = True
        m1s = len(found)
        if pending:
            rows = await box.rows("Wifi_VIF_Config")
            if any(r.get("ssid") == REGISTRAR_SSID for r in rows.values()):
                wrote, pending = True, False
                await apply_configuration(box.db)  # the pod's managers apply it, once
                if box.cold or box.legacy:  # VIFs the agent created (the legacy pod has none)
                    await attach_vifs(box.db)
                    settle = box.legacy  # its oftags restored once applied (spec 3.10)
        if wrote and applied():
            if settle:
                await auth_reload_settled(box.db)
            break
        await asyncio.sleep(0.3)
    if ruid is None:
        return {"failed": "no M1"}
    if not wrote:
        return {"failed": "the credentials never reached the pod"}
    return {
        "ruid": ruid,
        "applied": applied(),
        "operations": (box.status() or {}).get("operations"),
    }


def operational_bssid(topology):
    """The first BSSID in a Topology Response's AP Operational BSS TLV."""
    value = next((t.value for t in topology.tlvs if t.kind == 0x83), b"")
    # radios(1), then per radio: RUID(6), BSSes(1), per BSS: BSSID(6), SSID length(1), SSID
    if len(value) >= 14 and value[0] and value[7]:
        return value[8:14]
    return None


async def answers(box):
    """After onboarding, frames not for the agent (to the broadcast address, a runt) are
    ignored, and the controller's requests: each gets its response (or its 1905
    ACK) with the request's message ID, except the two that report the pod's statistics
    (spec 3.8): with no telemetry in the box there are none, and the agent withholds the
    Link Metric and AP Metrics Responses and records why instead of inventing values."""
    state = await onboarded(box)
    if not state.get("applied"):
        return box.result(passed=False, failed=state.get("failed", "credentials not applied"))
    ruid, stranger = state["ruid"], bytes.fromhex("02aabbccddee")

    async def ask(message_type, tlvs, expected, seconds=15):
        mid = box.controller.send(message_type, tlvs)
        return await box.until(lambda: box.controller.reply(expected, mid), seconds=seconds)

    # frames not for the agent: a Topology Query to the broadcast address (not the 1905
    # multicast nor the agent), and a runt (shorter than a CMDU header); both ignored
    box.controller.mid = (box.controller.mid + 1) & 0xFFFF
    broadcast_mid = box.controller.mid
    for frame in fragment_message(
        b"\xff" * 6, box.controller.al, 0x0002, broadcast_mid, (Tlv(0xB3, b"\x01"),)
    ):
        box.controller.send_frame(frame)
    runt = box.controller.agent + box.controller.al + b"\x89\x3a\x00\x00\x00\x02\x00"
    box.controller.send_frame(runt)
    await asyncio.sleep(2)
    ignored = box.controller.reply(0x0003, broadcast_mid) is None and box.process.poll() is None
    topology = await ask(0x0002, (Tlv(0xB3, b"\x01"),), 0x0003)
    bssid = operational_bssid(topology) if topology else None
    requests = {
        "topology": None,  # asked above; its BSSID names the BSS for the queries below
        "ap_capability": (0x8001, (), 0x8002),
        "channel_preference": (0x8004, (), 0x8005),
        "client_capability": (0x8009, (Tlv(0x90, (bssid or bytes(6)) + stranger),), 0x800A),
        "backhaul_sta_capability": (0x8027, (), 0x8028),
        "unassociated_sta_metrics": (0x800F, (Tlv(0x97, bytes((81, 1, 6, 1)) + stranger),), 0x8000),
        "policy_config": (0x8003, (Tlv(0x8A, b"\x00\x01" + ruid + bytes(4)),), 0x8000),
    }
    answered = {"topology": "0x0003" if topology else None}
    for name, request in requests.items():
        if request is None:
            continue
        message_type, tlvs, expected = request
        reply = await ask(message_type, tlvs, expected)
        answered[name] = f"0x{expected:04x}" if reply else None
    # withheld without statistics: no response, and the reason in the session's counts
    withheld = {
        "link_metric": (
            (0x0005, (Tlv(0x08, b"\x00\x02"),), 0x0006),
            "neighbor_measurement_unavailable",
        ),
        "ap_metrics": (
            (0x800B, (Tlv(0x93, b"\x01" + (bssid or bytes(6))),), 0x800C),
            "ap_measurements_unavailable",
        ),
    }
    abstained = {}
    for name, ((message_type, tlvs, expected), reason) in withheld.items():
        reply = await ask(message_type, tlvs, expected, seconds=5)
        counts = ((box.status() or {}).get("session") or {}).get("counts") or {}
        abstained[name] = reason if reply is None and counts.get(reason) else None
    session = (box.status() or {}).get("session") or {}
    # where the agent records its replies, the same in both implementations
    recorded = {
        "policy_receipt_ack_sent": (session.get("counts") or {}).get("policy_receipt_ack_sent"),
        "topology_response_sent": ((session.get("reports") or {}).get("counts") or {}).get(
            "topology_response_sent"
        ),
        "topology_response_in_session_counts": "topology_response_sent"
        in (session.get("counts") or {}),
    }
    return box.result(
        passed=ignored and bool(bssid) and all(answered.values()) and all(abstained.values()),
        ignored=ignored,
        bssid=bssid.hex(":") if bssid else None,
        answered=answered,
        abstained=abstained,
        recorded=recorded,
    )


async def refuse(box):
    """An EasyMesh 6.1 controller whose Response lacks the Controller Capability TLV:
    the agent refuses it (incompatible, spec 2.5) and sends no M1."""
    search = await box.until(lambda: box.controller.latest(AUTOCONFIG_SEARCH))
    if not search:
        return box.result(passed=False, failed="no search")
    response = (Tlv(0x0F, b"\0"), Tlv(0x10, b"\0"), Tlv(0x80, b"\x01\0"), Tlv(0xB3, b"\x01"))
    box.controller.send(AUTOCONFIG_RESPONSE, response, mid=search.mid)

    def refused():
        session = (box.status() or {}).get("session") or {}
        return session.get("state") == "incompatible" and session

    session = await box.until(refused, seconds=20)
    await asyncio.sleep(2)  # an M1 would follow the Response at once
    box.controller.poll()
    m1 = box.controller.latest(AUTOCONFIG_WSC)
    issues = (session or {}).get("admission_issues")
    return box.result(passed=bool(session) and m1 is None, admission_issues=issues)


RESPONSE_61 = (
    Tlv(0x0F, b"\0"),
    Tlv(0x10, b"\0"),
    Tlv(0x80, b"\x01\0"),
    Tlv(0xB3, b"\x01"),
    Tlv(0xDD, b"\xc0"),
)  # as onboarded() sends it
RENEW = 0x000A
TOPOLOGY_QUERY, TOPOLOGY_RESPONSE, TOPOLOGY_NOTIFICATION = 0x0002, 0x0003, 0x0001
TOPOLOGY_DISCOVERY = 0x0000
EARLY_REPORT, ACK = 0x8043, 0x8000
NEW_STATION = "02:00:00:00:77:01"


async def next_message(box, message_type, after, seconds=60):
    """The first message of this type beyond the first `after` ones."""
    return await box.until(
        lambda: (lambda found: found[after] if len(found) > after else None)(
            box.sent(message_type)
        ),
        seconds=seconds,
    )


async def provision(box, searches=0, m1s=0, *, seconds=90):
    """As a controller does until the agent is provisioning again: a Response to every Search
    beyond the first `searches`, a registrar's M2 to every M1 beyond the first `m1s`, and the
    pod's managers applying the credentials once written. A slow agent can end a session
    before its answer arrives and start another; it gets answered too. True once
    provisioning after an M2."""
    deadline = time.monotonic() + seconds
    answered_searches, answered_m1s, m2_sent, pending = searches, m1s, False, False

    async def written():
        rows = await box.rows("Wifi_VIF_Config")
        return any(r.get("ssid") == REGISTRAR_SSID for r in rows.values())

    while time.monotonic() < deadline and box.process.poll() is None:
        found = box.sent(AUTOCONFIG_SEARCH)
        for search in found[answered_searches:]:
            box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
        answered_searches = len(found)
        found = box.sent(AUTOCONFIG_WSC)
        for m1 in found[answered_m1s:]:
            wsc = next(t.value for t in m1.tlvs if t.kind == 0x11)
            ruid = next(t.value[:6] for t in m1.tlvs if t.kind == 0x85)
            await answer_m1(box, ruid, wsc)
            m2_sent = pending = True
        answered_m1s = len(found)
        if pending and await written():
            pending = False
            await apply_configuration(box.db)  # the pod's managers apply it, once
        session = (box.status() or {}).get("session") or {}
        if m2_sent and not pending and session.get("state") == "provisioning":
            return True
        await asyncio.sleep(0.3)
    return False


def kept_reconnections(box):
    """The agent's new database generations that continued its pod source (spec 2.5)."""
    return ((box.status() or {}).get("reconnections") or {}).get("continued", 0)


def fresh_reconnections(box, reason):
    """The agent's new database generations that were another pod source, for this reason."""
    reconnections = (box.status() or {}).get("reconnections") or {}
    return (reconnections.get("fresh") or {}).get(reason, 0)


async def back(box, searches, m1s, continued, *, seconds=90):
    """After the pod's link came back (a new database generation, spec 2.5): "kept" once the
    agent has counted the reconnection as continuing its source (beyond ``continued``, the count
    before the link went) and the session is provisioning without a new Search or M1, its
    configuration applied; "fresh" once it onboarded anew, answered as a controller does (as
    provision); None when neither. Credentials the agent writes again on a pod that lost them
    (a database from its template) are applied by the pod's managers."""
    deadline = time.monotonic() + seconds
    answered_searches, answered_m1s, fresh = searches, m1s, False
    while time.monotonic() < deadline and box.process.poll() is None:
        found = box.sent(AUTOCONFIG_SEARCH)
        for search in found[answered_searches:]:
            box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
            fresh = True
        answered_searches = len(found)
        found = box.sent(AUTOCONFIG_WSC)
        for m1 in found[answered_m1s:]:
            wsc = next(t.value for t in m1.tlvs if t.kind == 0x11)
            ruid = next(t.value[:6] for t in m1.tlvs if t.kind == 0x85)
            await answer_m1(box, ruid, wsc)
            fresh = True
        answered_m1s = len(found)
        written = any(
            r.get("ssid") == REGISTRAR_SSID for r in (await box.rows("Wifi_VIF_Config")).values()
        )
        on_air = any(
            r.get("ssid") == REGISTRAR_SSID for r in (await box.rows("Wifi_VIF_State")).values()
        )
        if written and not on_air:
            await apply_configuration(box.db)  # the pod's managers apply it
            if box.cold or box.legacy:  # VIFs the agent created (as onboarded)
                await attach_vifs(box.db)
        status = box.status() or {}
        session = status.get("session") or {}
        operations = status.get("operations") or []
        if (
            written
            and on_air
            and session.get("state") == "provisioning"
            and all_applied(operations[-1:])
            and (fresh or kept_reconnections(box) > continued)
        ):
            return "fresh" if fresh else "kept"
        await asyncio.sleep(0.3)
    return None


async def early_report(box):
    """With EasyMesh 6.1 the Early AP Capability Report goes before M1 and, without an Ack,
    three times in all, 250 ms apart, each with a new MID (spec 2.5); acknowledged, once."""
    search = await box.until(lambda: box.controller.latest(AUTOCONFIG_SEARCH))
    if not search:
        return box.result(passed=False, failed="no search")
    box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
    if not await next_message(box, AUTOCONFIG_WSC, 0):
        return box.result(passed=False, failed="no M1")
    await asyncio.sleep(2.5)  # no M2 and no Ack: the retries run out
    unacknowledged = box.sent(EARLY_REPORT)
    mids = [m.mid for m in unacknowledged]
    # a Renew starts again; this time the controller acknowledges the first transmission
    searches, before = len(box.sent(AUTOCONFIG_SEARCH)), len(unacknowledged)
    box.controller.send(RENEW, (Tlv(0x01, box.controller.al), Tlv(0x0F, b"\0"), Tlv(0x10, b"\0")))
    search = await next_message(box, AUTOCONFIG_SEARCH, searches)
    if not search:
        return box.result(passed=False, failed="no search after the Renew")
    box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
    first = await next_message(box, EARLY_REPORT, before, seconds=20)
    if first:
        box.controller.send(ACK, (), mid=first.mid)
    await asyncio.sleep(2.5)
    acknowledged = len(box.sent(EARLY_REPORT)) - before
    return box.result(
        # three at most, each a new MID; a loop busy at start may get fewer into its 1 s
        passed=2 <= len(mids) <= 3 and len(set(mids)) == len(mids) and acknowledged == 1,
        unacknowledged_transmissions=len(mids),
        distinct_mids=len(set(mids)),
        acknowledged_transmissions=acknowledged,
    )


async def renew(box):
    """A Renew from the controller: the agent starts again at once (a Search, then a fresh
    M1), takes the new M2 and is provisioning again (spec 2.4, 2.5)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    sent_at = time.monotonic()
    box.controller.send(RENEW, (Tlv(0x01, box.controller.al), Tlv(0x0F, b"\0"), Tlv(0x10, b"\0")))
    search = await next_message(box, AUTOCONFIG_SEARCH, searches, seconds=10)
    again = await provision(box, searches, m1s) if search else False
    return box.result(
        passed=bool(search) and again,
        search_after_s=round(box.controller.when(search) - sent_at, 1) if search else None,
        provisioning_again=again,
        operations=[op.get("state") for op in (box.status() or {}).get("operations") or []],
    )


async def no_m2(box):
    """Suite finding: an M1 the controller never answers (it restarted in between). After
    30 s without an M2 the agent starts onboarding again (spec 2.5). The box answers every
    Search, never an M1, and times the Search that follows an M1 by 25 s or more."""
    deadline, answered, waited = time.monotonic() + 120, 0, None
    while time.monotonic() < deadline and box.process.poll() is None and waited is None:
        searches, m1s = box.sent(AUTOCONFIG_SEARCH), box.sent(AUTOCONFIG_WSC)
        for search in searches[answered:]:
            at = box.controller.when(search)
            before = [box.controller.when(m) for m in m1s if box.controller.when(m) < at]
            if before and at - before[-1] >= 25:
                waited = at - before[-1]  # the no-M2 rule, not an earlier session's end
                break
            box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
        answered = len(searches)
        await asyncio.sleep(0.2)
    return box.result(
        passed=waited is not None and 29 <= waited <= 45,
        search_after_m1_s=round(waited, 1) if waited else None,
        m1s=len(box.sent(AUTOCONFIG_WSC)),
    )


async def silent_controller(box):
    """Suite finding: a controller that stops talking, in any state. After 130 s without a
    message from it the agent starts onboarding again (spec 2.5)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    searches, quiet_since = len(box.sent(AUTOCONFIG_SEARCH)), box.controller.last_sent
    again = await next_message(box, AUTOCONFIG_SEARCH, searches, seconds=180)
    waited = box.controller.when(again) - quiet_since if again else None
    return box.result(
        passed=bool(again) and 128 <= waited <= 150,
        search_after_silence_s=round(waited, 1) if waited else None,
    )


async def forgotten_agent(box):
    """Suite finding: RDK's controller restarted and forgot the agent, yet kept sending it
    its other queries, so the silence rule never fired. With topology_query_window (20 s
    here), a provisioned agent that gets no Topology Query for that long onboards again
    (spec 2.5). The box queries as RDK's controller does, every 5 s: for 30 s a Topology
    Query with the others (the agent stays), then the others only (it searches)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))

    def others():
        box.controller.send(0x8001, ())  # AP Capability Query
        box.controller.send(0x0005, (Tlv(0x08, b"\x00\x02"),))  # Link Metric Query

    for _ in range(6):  # known: 30 s with a Topology Query every 5 s
        box.controller.send(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),))
        last_query = time.monotonic()
        others()
        await asyncio.sleep(5)
    stayed = len(box.sent(AUTOCONFIG_SEARCH)) == searches
    again, deadline = None, time.monotonic() + 60
    while again is None and time.monotonic() < deadline and box.process.poll() is None:
        others()  # forgotten: the other queries only
        again = await next_message(box, AUTOCONFIG_SEARCH, searches, seconds=5)
    waited = box.controller.when(again) - last_query if again else None
    provisioned = await provision(box, searches, m1s) if again else False
    return box.result(
        passed=stayed and bool(again) and 19 <= waited <= 30 and provisioned,
        stayed_while_queried=stayed,
        search_after_last_topology_query_s=round(waited, 1) if waited else None,
        provisioning_again=provisioned,
    )


async def agent_netns(box):
    """EMOSA_NETNS: the agent's interface in a network namespace of its own, as on a gateway
    whose EasyMesh controller runs alongside (RDK's took an agent whose AL MAC was the MAC of
    one of its own interfaces for its co-located agent, 5 October 2026): no interface of the
    agent's own namespace has the AL MAC, and the agent onboards through the other one."""
    links = subprocess.run(["ip", "-o", "link"], capture_output=True, text=True).stdout
    state = await onboarded(box)
    return box.result(
        passed=box.al_mac not in links and bool(state.get("applied")),
        al_mac_in_own_namespace=box.al_mac in links,
        onboarding=state,
    )


async def unserved_pod(box):
    """Suite finding: provisioned, but the pod serves none of the controller's BSSes (its
    configuration was lost, a write lost to an uplink move) and nothing is being written.
    After 60 s the agent asks for the configuration again: onboarding anew (spec 2.5)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    searches = len(box.sent(AUTOCONFIG_SEARCH))
    lost_at = time.monotonic()
    # the pod's managers lost the fronthaul BSS: no BSS of the controller's is served
    uuid, _ = await box.vif("Wifi_VIF_State", "home-ap-24")
    radio = next(
        u for u, r in (await box.rows("Wifi_Radio_State")).items() if r.get("if_name") == "phy1"
    )
    await box.transact(
        [
            {
                "op": "mutate",
                "table": "Wifi_Radio_State",
                "where": [["_uuid", "==", ["uuid", radio]]],
                "mutations": [["vif_states", "delete", ["set", [["uuid", uuid]]]]],
            },
            {"op": "delete", "table": "Wifi_VIF_State", "where": [["_uuid", "==", ["uuid", uuid]]]},
        ]
    )
    again = await next_message(box, AUTOCONFIG_SEARCH, searches, seconds=120)
    waited = box.controller.when(again) - lost_at if again else None
    return box.result(
        passed=bool(again) and 58 <= waited <= 90,
        search_after_loss_s=round(waited, 1) if waited else None,
    )


RECREATED_RADIO = {
    "02:00:00:00:01:00": "02:00:00:00:6a:00",
    "82:00:00:00:01:00": "82:00:00:00:6a:00",
}


async def pod_recreated(box):
    """Finding (a), rdk-1004 7 October: the pod recreated in place (its container cloned and
    started again: a new database from the template, the same serial, and another AP radio
    from the lab's pool). The agent takes the new pod's connection and onboards it, its
    Search and M1 on the new radio."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    await box.new_pod_database(RECREATED_RADIO)
    again = await provision(box, searches, m1s)
    rows = await box.rows("Wifi_VIF_Config")
    written = any(r.get("ssid") == REGISTRAR_SSID for r in rows.values())
    status = box.status() or {}
    radio = json.dumps(status.get("pod") or {})
    new_radio = "02:00:00:00:6a:00" in radio and "02:00:00:00:01:00" not in radio
    return box.result(
        passed=again and written and new_radio,
        provisioning_again=again,
        credentials_on_new_pod=written,
        new_radio_in_status=new_radio,
        ruid=(status.get("pod") or {}).get("ruid"),
    )


async def new_source(box):
    """Suite finding: the pod dropped with its extender and back with a fresh database (a new
    generation, the configuration gone). Its OpenSync started again (new radio rows): another
    pod source (spec 2.5), so the agent ends the session on the old source and onboards on the
    new one, its configuration written there by the new session's M2."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued, restarted = kept_reconnections(box), fresh_reconnections(box, "pod_restarted")
    await box.new_pod_database()
    mode = await back(box, searches, m1s, continued)
    rows = await box.rows("Wifi_VIF_Config")
    written = any(r.get("ssid") == REGISTRAR_SSID for r in rows.values())
    why = fresh_reconnections(box, "pod_restarted") > restarted
    return box.result(
        passed=mode == "fresh" and written and why,
        session=mode,
        fresh_as_pod_restarted=why,
        credentials_on_new_database=written,
        searches=len(box.sent(AUTOCONFIG_SEARCH)) - searches,
        m1s=len(box.sent(AUTOCONFIG_WSC)) - m1s,
    )


def association_events(box, after=0):
    """(station, BSSID, joined) of each Client Association Event the agent notified."""
    events = []
    for message in box.sent(TOPOLOGY_NOTIFICATION)[after:]:
        for tlv in message.tlvs:
            if tlv.kind == 0x92 and len(tlv.value) >= 13:
                events.append(
                    (tlv.value[:6].hex(":"), tlv.value[6:12].hex(":"), bool(tlv.value[12] & 0x80))
                )
    return events


def client_ages(topology):
    """{station: seconds since association} from a Topology Response's Associated Clients
    TLV (0x84): BSSes(1), per BSS: BSSID(6), clients(2), per client: MAC(6) and age(2)."""
    ages = {}
    value = next((t.value for t in topology.tlvs if t.kind == 0x84), b"")
    at, count = 1, value[0] if value else 0
    for _ in range(count):
        clients = int.from_bytes(value[at + 6 : at + 8], "big")
        at += 8
        for _ in range(clients):
            ages[value[at : at + 6].hex(":")] = int.from_bytes(value[at + 6 : at + 8], "big")
            at += 8
    return ages


async def clients(box):
    """A station joins and leaves the pod's BSS: a Topology Notification with a Client
    Association Event each time (spec 2.4); the Topology Response lists the station with its
    age as of that response (suite finding: ages taken as of each Topology Response)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)  # re-announcement done
    await asyncio.sleep(2)
    before = len(box.sent(TOPOLOGY_NOTIFICATION))
    await box.client_join(NEW_STATION)
    joined = await box.until(
        lambda: [e for e in association_events(box, before) if e[0] == NEW_STATION and e[2]],
        seconds=20,
    )
    first = await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)
    await asyncio.sleep(4)
    second = await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)
    ages = [client_ages(r).get(NEW_STATION) if r else None for r in (first, second)]
    before = len(box.sent(TOPOLOGY_NOTIFICATION))
    await box.client_leave(NEW_STATION)
    left = await box.until(
        lambda: [e for e in association_events(box, before) if e[0] == NEW_STATION and not e[2]],
        seconds=20,
    )
    aged = None not in ages and 3 <= ages[1] - ages[0] <= 8
    return box.result(
        passed=bool(joined) and aged and bool(left), joined=bool(joined), ages=ages, left=bool(left)
    )


async def reannounce(box):
    """Suite finding: a controller that restarted never learns the clients that joined before
    it did. Once provisioned, at the controller's next Topology Query the agent announces
    every current client again, once (spec 2.5)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    await asyncio.sleep(2)
    before = len(box.sent(TOPOLOGY_NOTIFICATION))
    await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)
    await asyncio.sleep(3)
    announced = sorted(e[0] for e in association_events(box, before) if e[2])
    before = len(box.sent(TOPOLOGY_NOTIFICATION))
    await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)
    await asyncio.sleep(3)
    again = sorted(e[0] for e in association_events(box, before) if e[2])
    current = ["02:00:00:00:0a:00", "02:00:00:00:10:00"]  # the recorded pod's fronthaul clients
    return box.result(
        passed=announced == current and again == [], announced=announced, announced_again=again
    )


def tlv_value(message, kind):
    return next((t.value for t in message.tlvs if t.kind == kind), None) if message else None


async def channel_selection(box):
    """Channel Selection Requests (spec 2.4, 3.4): one the pod can honour (its channel
    preferred) is accepted, code 0, without moving the radio; one it cannot (RDK's, with its
    channel ruled out) is declined, code 2. Each is followed by the Operating Channel Report."""
    state = await onboarded(box)
    if not state.get("applied"):
        return box.result(passed=False, failed="not onboarded")
    ruid = state["ruid"]
    # from spec/conformance/control.json: channel-selection-accepted and -rdk-declined
    accepted = (Tlv(0x8B, ruid + bytes.fromhex("01510106e0")),)
    declined = (
        Tlv(
            0x8B,
            ruid
            + bytes.fromhex(
                "05510c01020304050708090a0b0c0d00510106105308010203040507"
                "080910530106e0540905060708090a0b0c0d10"
            ),
        ),
        Tlv(0x8D, ruid + b"\x00"),
    )
    codes, reports = {}, {}
    for name, tlvs in (("accepted", accepted), ("declined", declined)):
        reported = len(box.sent(0x8008))
        response = await box.ask(0x8006, tlvs, 0x8007)
        value = tlv_value(response, 0x8E)
        codes[name] = value[6] if value and len(value) >= 7 else None
        report = await next_message(box, 0x8008, reported, seconds=10)
        reports[name] = (tlv_value(report, 0x8F) or b"").hex() or None
    radio = next(
        r for r in (await box.rows("Wifi_Radio_State")).values() if r.get("if_name") == "phy1"
    )
    return box.result(
        passed=codes == {"accepted": 0, "declined": 2}
        and all(reports.values())
        and radio.get("channel") == 6,
        codes=codes,
        operating_channel_reports=reports,
        channel=radio.get("channel"),
    )


async def channel_scan(box):
    """A Channel Scan Request (spec 2.4, 3.4): acknowledged, then a Channel Scan Report with a
    timestamp and one result per requested channel, status 1 (scan not supported)."""
    state = await onboarded(box)
    if not state.get("applied"):
        return box.result(passed=False, failed="not onboarded")
    request = Tlv(0xA6, b"\x00\x01" + state["ruid"] + bytes.fromhex("0151020106"))
    reported = len(box.sent(0x801C))
    ack = await box.ask(0x801B, (request,), ACK)
    report = await next_message(box, 0x801C, reported, seconds=10)
    results = [t.value for t in report.tlvs if t.kind == 0xA7] if report else []
    statuses = sorted((r[7], r[8]) for r in results if len(r) >= 9)  # (channel, status)
    return box.result(
        passed=bool(ack) and statuses == [(1, 1), (6, 1)] and tlv_value(report, 0xA8) is not None,
        results=statuses,
    )


# The pod's statistics (spec 3.6, 3.8, 3.9): a broker in the box; the box publishes as the
# pod's qm would, built from the pinned schema (emosa_lab.conformance's report builders)

STATIONS = ("02:00:00:00:0a:00", "02:00:00:00:10:00")  # the recorded fronthaul's clients


def ovsdb_map(value):
    """An OVSDB map column as a dict, raw (["map", pairs]) or decoded."""
    if isinstance(value, list) and len(value) == 2 and value[0] == "map":
        return dict(value[1])
    return dict(value or {})


def telemetry_status(box):
    return (box.status() or {}).get("telemetry") or {}


def now_ms():
    return int(time.time() * 1000)


async def publish(box, payload_hex, *, retain=False):
    import paho.mqtt.publish as mqtt_publish

    topic = telemetry_status(box).get("topic") or f"emosa/stats/{box.serial}"
    await asyncio.to_thread(
        mqtt_publish.single,
        topic,
        bytes.fromhex(payload_hex),
        hostname="127.0.0.1",
        port=1883,
        retain=retain,
    )


def pod_statistics(at_ms):
    """A survey of the pod's channel and a client report of its two stations."""
    from emosa_lab.conformance import COUNTERS_FULL, client_report, survey_report

    return (
        survey_report(at_ms - 1500),
        client_report(
            at_ms - 1000,
            [(STATIONS[0], 40, 144.4, 72.2, COUNTERS_FULL), (STATIONS[1], 30, 65.0, 58.5, None)],
        ),
    )


async def telemetry_applied(box):
    def applied():
        operation = telemetry_status(box).get("operation") or {}
        return operation.get("state") == "OBSERVED_APPLIED" and operation

    return await box.until(applied, seconds=60)


async def telemetry(box):
    """The agent has its pod publish its statistics: the broker, the pod's topic and the
    reports in one guarded write, applied once the pod's database has it; the reports then
    reach the agent through the broker, and a repeated one is dropped (spec 3.6)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    applied = await telemetry_applied(box)
    node = next(iter((await box.rows("AWLAN_Node")).values()))
    settings = ovsdb_map(node.get("mqtt_settings"))
    reports = sorted(r.get("stats_type") for r in (await box.rows("Wifi_Stats_Config")).values())
    survey, clients = pod_statistics(now_ms())
    await publish(box, clients)
    accepted = await box.until(lambda: telemetry_status(box).get("reports_accepted"), seconds=20)
    await publish(box, clients)  # the same report again
    await asyncio.sleep(2)
    status = telemetry_status(box)
    stations = sorted(status.get("stations") or {})
    return box.result(
        passed=bool(applied)
        and settings.get("broker") == "127.0.0.1"
        and "client" in reports
        and accepted == 1
        and status.get("reports_accepted") == 1
        and (status.get("reports_rejected") or 0) >= 1
        and stations == sorted(STATIONS),
        mqtt_settings=settings,
        stats_types=reports,
        reports_accepted=status.get("reports_accepted"),
        reports_rejected=status.get("reports_rejected"),
        stations=stations,
    )


async def foreign_broker(box):
    """The pod already publishes to another manager's broker (the operator's cloud): the
    agent does not take it over; its write is refused and the pod's settings stay (spec 3.6)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")

    def settled():
        operation = telemetry_status(box).get("operation") or {}
        return operation.get("state") in {"REJECTED", "FAILED", "OWNERSHIP_CONFLICT"} and operation

    operation = await box.until(settled, seconds=60)
    node = next(iter((await box.rows("AWLAN_Node")).values()))
    settings = sorted(map(list, ovsdb_map(node.get("mqtt_settings")).items()))
    return box.result(
        passed=bool(operation) and settings == sorted(FOREIGN_BROKER),
        operation=(operation or {}).get("state"),
        reason=(operation or {}).get("reason"),
        mqtt_settings=settings,
    )


METRIC_POLICY = bytes.fromhex("000001020000000100023c78")  # Steering Policy, from metrics.json


async def metrics(box):
    """With the pod's statistics, AP metrics as a native agent reports them (spec 3.8): the
    Metric Reporting Policy kept; an AP Metrics Query answered with the AP Metrics TLV
    (channel utilization from the survey, the BSS's stations), the Associated STA Link
    Metrics and Traffic Stats of its stations; and unsolicited at the policy's interval."""
    state = await onboarded(box)
    if not state.get("applied") or not await telemetry_applied(box):
        return box.result(passed=False, failed="not onboarded or no telemetry")
    ruid = state["ruid"]
    bssid = bytes.fromhex("820000000100")

    async def keep_publishing():
        while True:
            for payload in pod_statistics(now_ms()):
                await publish(box, payload)
            await asyncio.sleep(5)

    publisher = asyncio.create_task(keep_publishing())
    try:
        await box.until(lambda: len(telemetry_status(box).get("stations") or {}) == 2, seconds=20)
        # every 5 s, link metrics and traffic stats for the radio (from metrics.json)
        policy = (
            Tlv(0x89, METRIC_POLICY[:3] + ruid + METRIC_POLICY[9:]),
            Tlv(0x8A, b"\x05\x01" + ruid + bytes.fromhex("78053cc0")),
        )
        acked = await box.ask(0x8003, policy, ACK)
        response = await box.ask(0x800B, (Tlv(0x93, b"\x01" + bssid),), 0x800C)
        ap = tlv_value(response, 0x94) or b""
        link = [t.value[:6].hex(":") for t in (response.tlvs if response else []) if t.kind == 0x96]
        traffic = [
            t.value[:6].hex(":") for t in (response.tlvs if response else []) if t.kind == 0xA2
        ]
        queried = response.mid if response else None
        before = len(box.sent(0x800C))
        await asyncio.sleep(12)
        unsolicited = [m for m in box.sent(0x800C)[before:] if m.mid != queried]
    finally:
        publisher.cancel()
    utilization = ap[6] if len(ap) >= 9 else None
    stations = int.from_bytes(ap[7:9], "big") if len(ap) >= 9 else None
    return box.result(
        passed=bool(acked)
        and ap[:6] == bssid
        and utilization == round(41 * 255 / 100)
        # traffic statistics only for the station whose counters the pod measures (spec 3.6)
        and stations == 2
        and sorted(link) == sorted(STATIONS)
        and traffic == [STATIONS[0]]
        and len(unsolicited) >= 1,
        utilization=utilization,
        stations=stations,
        link_metrics=sorted(link),
        traffic_stats=sorted(traffic),
        unsolicited=len(unsolicited),
    )


async def unassociated(box):
    """Unassociated STA Link Metrics (spec 3.9): the agent watches the stations asked about
    (a monitor-only steering row on the pod); a station the pod heard is reported with its
    RCPI; an associated one is refused with reason 1, one not heard with reason 2."""
    if not (await onboarded(box)).get("applied") or not await telemetry_applied(box):
        return box.result(passed=False, failed="not onboarded or no telemetry")
    heard, unheard, associated = "02:00:00:00:99:99", "02:00:00:00:98:98", STATIONS[0]
    query = Tlv(
        0x97,
        bytes([81, 1, 6, 3])
        + b"".join(bytes.fromhex(m.replace(":", "")) for m in (associated, heard, unheard)),
    )

    def refused(ack):
        # Error Code TLV: reason(1), station(6)
        return (
            sorted((t.value[1:7].hex(":"), t.value[0]) for t in ack.tlvs if t.kind == 0xA3)
            if ack
            else None
        )

    first = await box.ask(0x800F, (query,), ACK)

    async def watched():
        rows = (await box.rows("Band_Steering_Clients")).values()
        return sorted(r.get("mac") for r in rows if "emosa" in str(r.get("cs_params")))

    deadline, watch = time.monotonic() + 30, []
    while time.monotonic() < deadline and heard not in watch:
        watch = await watched()
        await asyncio.sleep(0.5)
    from emosa_lab.conformance import bs_report

    await publish(box, bs_report(now_ms() - 500, [(heard, ((0, "home-ap-24", ((0, 1500, 34),)),))]))
    await asyncio.sleep(2)
    reported = len(box.sent(0x8010))
    second = await box.ask(0x800F, (query,), ACK)
    response = await next_message(box, 0x8010, reported, seconds=10)
    measured = [t.value for t in (response.tlvs if response else []) if t.kind == 0x98]
    stations = []
    for value in measured:  # class(1), count(1), per station: MAC(6) channel(1) age(4) RCPI(1)
        at = 2
        for _ in range(value[1] if len(value) > 1 else 0):
            stations.append((value[at : at + 6].hex(":"), value[at + 6], value[at + 11]))
            at += 12
    return box.result(
        passed=refused(first) == sorted([(associated, 1), (heard, 2), (unheard, 2)])
        and heard in watch
        and refused(second) == sorted([(associated, 1), (unheard, 2)])
        and [s[:2] for s in stations] == [(heard, 6)]
        and stations[0][2] > 0,
        first_refusals=refused(first),
        watched=watch,
        second_refusals=refused(second),
        measured=stations,
    )


# Client steering (spec 3.7): the box plays the pod's owm on the steering rows

FRONTHAUL_BSSID = bytes.fromhex("820000000100")
TARGET = "02:00:00:12:75:2c"  # another AP's BSS, from spec/conformance/control.json


def steering_request(station, *, mandate=True, target=TARGET):
    """A Client Steering Request TLV (0x9B) from the pod's BSS: the mode (mandate, BTM
    disassociation imminent, abridged), a 5 s window and disassociation timer, one station
    and one target (operating class 81, channel 6)."""
    flags = 0xE0 if mandate else 0x60
    mac = bytes.fromhex(station.replace(":", ""))
    return Tlv(
        0x9B,
        FRONTHAUL_BSSID
        + bytes([flags])
        + b"\x00\x05\x00\x05\x01"
        + mac
        + b"\x01"
        + bytes.fromhex(target.replace(":", ""))
        + b"\x51\x06",
    )


async def steering_rows(box):
    clients = [
        r for r in (await box.rows("Band_Steering_Clients")).values() if r.get("mac") == STATIONS[0]
    ]
    neighbors = [
        r for r in (await box.rows("Wifi_VIF_Neighbors")).values() if r.get("bssid") == TARGET
    ]
    groups = list((await box.rows("Band_Steering_Config")).values())
    return clients, neighbors, groups


async def wait_rows(box, predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and box.process.poll() is None:
        box.controller.poll()
        rows = await steering_rows(box)
        if predicate(*rows):
            return rows
        await asyncio.sleep(0.3)
    return None


async def steering_leftover(box):
    """Box finding 20, rdk-emosa-1005 8 October: a steering window whose close failed (the pod's
    management link cut while it was open) left its rows in the pod, and every later mandate for
    the station was refused as another manager's steering, for good. Once the pod is back, the
    window's rows are closed by their UUIDs, and a second mandate for the station is carried
    out."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    ack = await box.ask(0x8014, (steering_request(STATIONS[0]),), ACK)
    opened = await wait_rows(box, lambda c, n, g: c and n and g)
    if not opened:
        return box.result(passed=False, failed="no steering window", acked=bool(ack))
    continued = kept_reconnections(box)
    await dial(box.db, f"tcp:127.0.0.1:{AGENT_PORT}", connect=False)  # owm never takes it
    cut_at = time.monotonic()

    def counts():
        return ((box.status() or {}).get("steering") or {}).get("counts") or {}

    close_failed = await box.until(lambda: counts().get("close_failed"), seconds=60)
    left, _, _ = await steering_rows(box)  # the pod's own database, the link still cut
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    away = time.monotonic() - cut_at
    await dial(box.db, f"tcp:127.0.0.1:{AGENT_PORT}")
    # spec 2.5: back within the reconnection window the session is kept, else a fresh one
    mode = await back(box, searches, m1s, continued)
    expected = "kept" if away < RECONNECT_WINDOW - 5 else "fresh"
    again = mode == expected if abs(away - RECONNECT_WINDOW) > 5 else mode is not None
    closed = await wait_rows(box, lambda c, n, g: not c and not n and not g, seconds=30)
    second = await box.ask(0x8014, (steering_request(STATIONS[0]),), ACK)
    reopened = await wait_rows(box, lambda c, n, g: c and n and g)
    refused = counts().get("refused_ownership_conflict", 0)
    return box.result(
        passed=bool(close_failed)
        and bool(left)
        and again
        and bool(closed)
        and bool(second)
        and bool(reopened)
        and not refused,
        close_failed_seen=bool(close_failed),
        rows_left_while_away=len(left),
        link_away_s=round(away, 1),
        session=mode,
        provisioning_again=again,
        leftover_closed=bool(closed),
        second_window=bool(reopened),
        refused_ownership_conflict=refused,
        counts=counts(),
    )


async def steering_close_refused(box):
    """Box finding 23: a window's close refused by the pod's database with the link up (the
    box's reply cut answers the delete with an error), so its rows stay in the pod on the same
    source. The sweep tries again after its back-off (30 s) and closes them by their UUIDs, and
    a second mandate for the station is carried out, not refused as another manager's (before:
    nothing tried again until the pod's next source, and the window's record could leave the
    journal first, leaving the rows for good)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    ack = await box.ask(0x8014, (steering_request(STATIONS[0]),), ACK)
    opened = await wait_rows(box, lambda c, n, g: c and n and g)
    if not opened:
        return box.result(passed=False, failed="no steering window", acked=bool(ack))

    def counts():
        return ((box.status() or {}).get("steering") or {}).get("counts") or {}

    # owm never takes the window: it ends unapplied, and its close is refused
    close_failed = await box.until(lambda: counts().get("close_failed"), seconds=60)
    left, _, _ = await steering_rows(box)
    closed = await wait_rows(box, lambda c, n, g: not c and not n and not g, seconds=60)
    second = await box.ask(0x8014, (steering_request(STATIONS[0]),), ACK)
    reopened = await wait_rows(box, lambda c, n, g: c and n and g)
    refused = counts().get("refused_ownership_conflict", 0)
    return box.result(
        passed=bool(close_failed)
        and box.reply_cut.done
        and bool(left)
        and bool(closed)
        and bool(second)
        and bool(reopened)
        and not refused,
        close_refused=box.reply_cut.done,
        close_failed_seen=bool(close_failed),
        rows_left=len(left),
        leftover_closed=bool(closed),
        second_window=bool(reopened),
        refused_ownership_conflict=refused,
        counts=counts(),
    )


async def steering(box):
    """A steering mandate for one station and one target: acknowledged; a steering window
    opened as one write (the station's client row, a group, the target as a neighbor);
    applied when owm reports it steering, then the directed kick; when owm stops steering
    the window closes, deleting exactly the rows it inserted (spec 3.7)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    ack = await box.ask(0x8014, (steering_request(STATIONS[0]),), ACK)
    refusals = [t for t in (ack.tlvs if ack else []) if t.kind == 0xA3]
    opened = await wait_rows(box, lambda c, n, g: c and n and g)
    if not opened:
        return box.result(passed=False, failed="no steering window", acked=bool(ack))
    client = opened[0][0]
    btm = ovsdb_map(client.get("sc_btm_params"))
    uuid = next(
        u
        for u, r in (await box.rows("Band_Steering_Clients")).items()
        if r.get("mac") == STATIONS[0]
    )
    where = [["_uuid", "==", ["uuid", uuid]]]
    await box.transact(
        [
            {
                "op": "update",
                "table": "Band_Steering_Clients",
                "where": where,
                "row": {"cs_state": "steering"},
            }
        ]
    )  # owm takes the window
    kicked = await wait_rows(box, lambda c, n, g: c and c[0].get("force_kick") == "directed")
    await box.transact(
        [
            {
                "op": "update",
                "table": "Band_Steering_Clients",
                "where": where,
                "row": {"cs_state": "none"},
            }
        ]
    )  # the station went: owm stops steering
    await box.client_leave(STATIONS[0])
    closed = await wait_rows(box, lambda c, n, g: not c and not n and not g, seconds=30)
    return box.result(
        passed=bool(ack)
        and not refusals
        and client.get("cs_mode") == "away"
        and btm.get("bssid") == TARGET
        and bool(kicked)
        and bool(closed),
        acked=bool(ack),
        cs_mode=client.get("cs_mode"),
        kick_type=client.get("sc_kick_type"),
        btm_target=btm.get("bssid"),
        kicked=bool(kicked),
        closed=bool(closed),
    )


async def steering_refusals(box):
    """What the agent does not carry out (spec 3.7): a station not on the source BSS (an
    Error Code, reason 2); a steering opportunity (Steering Completed at once: EMOSA makes no
    choice of its own); a target the agent would have to choose. None opens a window."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    stranger = "02:00:00:00:99:99"
    ack = await box.ask(0x8014, (steering_request(stranger),), ACK)
    refused = sorted(
        (t.value[1:7].hex(":"), t.value[0]) for t in (ack.tlvs if ack else []) if t.kind == 0xA3
    )
    completed = len(box.sent(0x8017))
    opportunity = await box.ask(0x8014, (steering_request(STATIONS[0], mandate=False),), ACK)
    done = await next_message(box, 0x8017, completed, seconds=5)
    wildcard = await box.ask(
        0x8014, (steering_request(STATIONS[0], target="ff:ff:ff:ff:ff:ff"),), ACK
    )
    await asyncio.sleep(3)
    clients, neighbors, groups = await steering_rows(box)
    return box.result(
        passed=refused == [(stranger, 2)]
        and bool(opportunity)
        and bool(done)
        and bool(wildcard)
        and not clients
        and not neighbors,
        refused=refused,
        steering_completed=bool(done),
        agent_selected_acked=bool(wildcard),
        windows_opened=len(clients),
    )


# The pod's Wi-Fi backhaul (spec 8.3): the Multi-AP pod with option 1; the box plays cm and
# the supplicant when the station moves

TARGET_PARENT = "02:00:00:00:19:02"  # another backhaul BSS of the controller's


def uplink_status(box):
    return (box.status() or {}).get("uplink") or {}


def backhaul_steering_request(station, target=TARGET_PARENT, operating_class=81, channel=6):
    """Backhaul Steering Request TLV (0x9E): the backhaul station, the target BSS and its
    operating class and channel."""
    return Tlv(
        0x9E,
        bytes.fromhex(station.replace(":", ""))
        + bytes.fromhex(target.replace(":", ""))
        + bytes((operating_class, channel)),
    )


def steering_answer(response):
    """(station, target, status) of a Backhaul Steering Response's TLV (0x9F)."""
    value = tlv_value(response, 0x9F) or b""
    return (value[:6].hex(":"), value[6:12].hex(":"), value[12]) if len(value) >= 13 else None


async def pinned(box, bssid):
    """The station's multi_ap credential is pinned to this BSSID on the pod."""
    rows = (await box.rows("Wifi_Credential_Config")).values()
    return any(str(r.get("bssid") or "").lower() == bssid for r in rows)


async def move_station(box, bssid):
    """cm and the supplicant: the backhaul station now on this parent."""
    uuid, _ = await box.vif("Wifi_VIF_State", "bhaul-sta-24")
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_VIF_State",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {"parent": bssid},
            }
        ]
    )


async def four_address(box, parent):
    """cm and owm complete the Multi-AP (4-address) join: the station on `parent`, cm using it
    (for a pod that started on a GTP bootstrap, so move_station's parent change alone isn't the
    4-address State the uplink switch waits for)."""
    uuid, _ = await box.vif("Wifi_VIF_State", "bhaul-sta-24")
    cm = next(iter(await box.rows("Connection_Manager_Uplink")))
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_VIF_State",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {
                    "parent": parent,
                    "ssid": "emosa-lab-bh",
                    "multi_ap": "backhaul_sta",
                    "wds": True,
                },
            },
            {
                "op": "update",
                "table": "Connection_Manager_Uplink",
                "where": [["_uuid", "==", ["uuid", cm]]],
                "row": {"if_name": "bhaul-sta-24", "if_type": "vif", "is_used": True},
            },
        ]
    )


async def on_gtp_bootstrap(box):
    """cm takes the station back to its GTP bootstrap: vconf on the bootstrap BSS, the gretap uplink
    (the state the pod returns to once EMOSA's credential is gone)."""
    uuid, _ = await box.vif("Wifi_VIF_State", "bhaul-sta-24")
    cm = next(iter(await box.rows("Connection_Manager_Uplink")))
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_VIF_State",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {
                    "parent": BOOTSTRAP_BSS,
                    "ssid": "opensync-lab-bhaul",
                    "multi_ap": "none",
                    "wds": False,
                },
            },
            {
                "op": "update",
                "table": "Connection_Manager_Uplink",
                "where": [["_uuid", "==", ["uuid", cm]]],
                "row": {"if_name": "g-bhaul-sta-24", "if_type": "gre", "is_used": True},
            },
        ]
    )


async def gre_join(box, parent):
    """cm and owm complete a 3-address GRE join onto `parent`: the station associated (not
    Multi-AP), cm using the gretap it built (spec 8.6)."""
    uuid, _ = await box.vif("Wifi_VIF_State", "bhaul-sta-24")
    cm = next(iter(await box.rows("Connection_Manager_Uplink")))
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_VIF_State",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {
                    "parent": parent,
                    "ssid": "emosa-lab-bh",
                    "multi_ap": "none",
                    "wds": False,
                },
            },
            {
                "op": "update",
                "table": "Connection_Manager_Uplink",
                "where": [["_uuid", "==", ["uuid", cm]]],
                "row": {"if_name": "g-bhaul-sta-24", "if_type": "gre", "is_used": True},
            },
        ]
    )


async def fallback_station(box):
    """The pod's fallback station (bhaul-sta-50) as pod-bh-fallback leaves it: enabled, its own
    credential and parent. Returns the columns EMOSA must never touch, to compare after a move."""
    uuid, row = await box.vif("Wifi_VIF_Config", "bhaul-sta-50")
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_VIF_Config",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {"enabled": True, "parent": "02:00:00:00:1c:01"},
            }
        ]
    )
    _, now = await box.vif("Wifi_VIF_Config", "bhaul-sta-50")
    return {k: now.get(k) for k in ("enabled", "ssid", "credential_configs", "parent")}


async def backhaul_on(box):
    """Onboarded, and the uplink switch (option 1) applied: the station pinned to its parent."""
    if not (await onboarded(box)).get("applied"):
        return False

    def applied():
        operation = uplink_status(box).get("operation") or {}
        return operation.get("state") == "OBSERVED_APPLIED"

    return bool(await box.until(applied, seconds=60))


async def backhaul_capability(box):
    """The Backhaul STA Capability Report names each of the pod's backhaul STA radios, the one
    in use first: its 2.4 GHz station on the Multi-AP backhaul, then its disabled 5 GHz one
    (EasyMesh 6.1, 9.3; spec 2.4, 8.3); the uplink switch pins the first to the configured
    parent."""
    on = await backhaul_on(box)
    report = await box.ask(0x8027, (), 0x8028)
    radios = [
        (
            t.value[:6].hex(":"),
            t.value[7:13].hex(":") if len(t.value) >= 13 and t.value[6] & 0x80 else None,
        )
        for t in (report.tlvs if report else [])
        if t.kind == 0xCB
    ]
    expected = [
        ("02:00:00:00:14:00", BACKHAUL_STATION),  # in use, on the 2.4 GHz radio
        ("02:00:00:00:15:00", OTHER_STATION),  # disabled, on the 5 GHz radio
    ]
    return box.result(
        passed=on and radios == expected and await pinned(box, BACKHAUL_PARENT),
        uplink_applied=on,
        radios=radios,
    )


async def wired_stations_off(box):
    """spec 8.4, 8.3: a wired pod whose bootstrap made a backhaul station on each radio, every one
    off (the Pis' bootstrap, 14 October: a station row per band, only the one in use on, none on a
    wired pod). The agent onboards it and bridges its Ethernet uplink, and writes nothing to the
    stations: each row as the bootstrap left it. Each still counts as its radio's backhaul STA
    (EasyMesh 6.1, 9.3), so a controller's move has a row to enable."""

    async def stations():
        return {
            r.get("if_name"): {
                k: r.get(k) for k in ("enabled", "ssid", "parent", "security", "credential_configs")
            }
            for r in (await box.rows("Wifi_VIF_Config")).values()
            if r.get("mode") == "sta"
        }

    before = await stations()
    onboarding = await onboarded(box)

    def applied():
        operation = ((box.status() or {}).get("uplink") or {}).get("operation") or {}
        return operation.get("state") == "OBSERVED_APPLIED"

    done = await box.until(applied, seconds=60)
    await asyncio.sleep(8)
    after = await stations()
    report = await box.ask(0x8027, (), 0x8028)
    radios = sorted(t.value[:6].hex(":") for t in (report.tlvs if report else []) if t.kind == 0xCB)
    uplink = (box.status() or {}).get("uplink") or {}
    return box.result(
        passed=bool(onboarding.get("applied"))
        and bool(done)
        and sorted(before) == ["bhaul-sta-24", OTHER_STATION_IF]
        and not any(s["enabled"] for s in before.values())
        and after == before
        and radios == ["02:00:00:00:14:00", "02:00:00:00:15:00"],  # both radios' RUIDs
        onboarded=bool(onboarding.get("applied")),
        uplink_applied=bool(done),
        uplink={k: uplink.get(k) for k in ("mode", "bridge", "in_use", "waiting")},
        stations_untouched=after == before,
        stations=after,
        backhaul_sta_radios=radios,
    )


async def backhaul_steering(box, *, renew_midway=False):
    """A Backhaul Steering Request for the pod's backhaul station: acknowledged, the station
    re-pinned to the target; once the pod's State shows it there, the Backhaul Steering
    Response with the request's MID, success (spec 2.4, 8.3). With `renew_midway` a Renew
    and a new onboarding come between: the answer is kept (suite finding)."""
    if not await backhaul_on(box):
        return box.result(passed=False, failed="uplink switch not applied")
    answered = len(box.sent(0x801A))
    mid = box.controller.send(0x8019, (backhaul_steering_request(BACKHAUL_STATION),))
    ack = await box.until(lambda: box.controller.reply(ACK, mid), seconds=5)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not await pinned(box, TARGET_PARENT):
        await asyncio.sleep(0.3)
    repinned = await pinned(box, TARGET_PARENT)
    again = None
    if renew_midway:
        searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
        box.controller.send(
            RENEW, (Tlv(0x01, box.controller.al), Tlv(0x0F, b"\0"), Tlv(0x10, b"\0"))
        )
        again = await provision(box, searches, m1s)
    await move_station(box, TARGET_PARENT)
    response = await next_message(box, 0x801A, answered, seconds=60)
    answer = steering_answer(response)
    return box.result(
        passed=bool(ack)
        and repinned
        and response is not None
        and response.mid == mid
        and answer == (BACKHAUL_STATION, TARGET_PARENT, 0)
        and (again is not False),
        acked=bool(ack),
        repinned=repinned,
        renewed_and_provisioned=again,
        response_mid_matches=bool(response and response.mid == mid),
        answer=answer,
    )


async def backhaul_steering_renewal(box):
    return await backhaul_steering(box, renew_midway=True)


async def backhaul_steering_refused(box):
    """Without option 1 (the pod on OpenSync's GRE) a Backhaul Steering Request is answered
    at once: failure, with an Error Code for the station (spec 2.4, 8.3)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    station = "02:00:00:00:05:00"  # the recorded pod's backhaul station, on GRE
    answered = len(box.sent(0x801A))
    mid = box.controller.send(0x8019, (backhaul_steering_request(station),))
    response = await next_message(box, 0x801A, answered, seconds=10)
    errors = [
        (t.value[0], t.value[1:7].hex(":"))
        for t in (response.tlvs if response else [])
        if t.kind == 0xA3
    ]
    answer = steering_answer(response)
    return box.result(
        passed=response is not None
        and response.mid == mid
        and answer is not None
        and answer[2] == 1
        and errors == [(6, station)],
        answer=answer,
        errors=errors,
    )


async def revert_by_record(box):
    """Spec 8.6: a pod on its GTP bootstrap, steered onto a parent and then back to its recorded
    bootstrap BSS. The agent records its bootstrap upstream before any move; a steer onto
    TARGET_PARENT pins the station there (a Multi-AP move here: the box has no GRE parent peer);
    a steer back to the bootstrap BSS is a revert - the agent restores the station's vconf snapshot,
    deletes the credential it inserted, and the station lands back on the bootstrap. Both answered
    success."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    recorded = await box.until(lambda: uplink_status(box).get("bootstrap"), seconds=30)
    if not recorded or recorded.get("bssid") != BOOTSTRAP_BSS:
        return box.result(passed=False, failed="bootstrap not recorded", bootstrap=recorded)
    # onto the parent
    answered = len(box.sent(0x801A))
    onto_mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, TARGET_PARENT),)
    )
    await box.until(lambda: box.controller.reply(ACK, onto_mid), seconds=5)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not await pinned(box, TARGET_PARENT):
        await asyncio.sleep(0.3)
    onto_pinned = await pinned(box, TARGET_PARENT)
    await four_address(box, TARGET_PARENT)
    onto = await next_message(box, 0x801A, answered, seconds=60)
    onto_ok = onto is not None and steering_answer(onto) == (BACKHAUL_STATION, TARGET_PARENT, 0)
    # back to the bootstrap: a revert
    answered2 = len(box.sent(0x801A))
    back_mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, BOOTSTRAP_BSS),)
    )
    await box.until(lambda: box.controller.reply(ACK, back_mid), seconds=5)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and await pinned(box, TARGET_PARENT):
        await asyncio.sleep(0.3)
    deleted = not await pinned(box, TARGET_PARENT)  # the agent removed its own credential
    await on_gtp_bootstrap(box)
    back = await next_message(box, 0x801A, answered2, seconds=60)
    back_ok = back is not None and steering_answer(back) == (BACKHAUL_STATION, BOOTSTRAP_BSS, 0)
    return box.result(
        passed=onto_pinned and onto_ok and deleted and back_ok,
        bootstrap=recorded,
        onto_pinned=onto_pinned,
        onto=steering_answer(onto),
        credential_deleted=deleted,
        back=steering_answer(back),
    )


async def restart_during_move(box):
    """Spec 8.6: a pod OpenSync restart during a move drops it (the DB is rebuilt and the pod
    re-pins to its bootstrap by itself). EMOSA re-applies the active move from its persisted target
    once the pod is back, with no new operator steer."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    await box.until(lambda: uplink_status(box).get("bootstrap"), seconds=30)
    answered = len(box.sent(0x801A))
    onto_mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, TARGET_PARENT),)
    )
    await box.until(lambda: box.controller.reply(ACK, onto_mid), seconds=5)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not await pinned(box, TARGET_PARENT):
        await asyncio.sleep(0.3)
    await four_address(box, TARGET_PARENT)
    onto = await next_message(box, 0x801A, answered, seconds=60)
    onto_ok = onto is not None and steering_answer(onto) == (BACKHAUL_STATION, TARGET_PARENT, 0)
    # the pod's OpenSync restarts: a fresh database, back on its bootstrap, the move dropped;
    # another pod source (spec 2.5): a fresh session
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued = kept_reconnections(box)
    await box.new_pod_database()
    mode = await back(box, searches, m1s, continued)
    # EMOSA re-applies the move from its persisted target, with no new operator steer
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not await pinned(box, TARGET_PARENT):
        await asyncio.sleep(0.3)
    reapplied = await pinned(box, TARGET_PARENT)
    return box.result(
        passed=onto_ok and mode == "fresh" and reapplied,
        onto=steering_answer(onto),
        session=mode,
        reapplied_without_a_steer=reapplied,
    )


async def fallback_during_move(box):
    """Spec 8.6 (EMOSA's half): a GRE move and its revert leave the pod's fallback station exactly
    as pod-bh-fallback set it - enabled, its own credential, its own parent. EMOSA writes only the
    moved station's columns, never the fallback's (a GRE move disables no other station). The other
    half (the fallback not taking EMOSA's credential) is the pod side: yocto-53's load_creds and the
    Pi soak, not the box."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    # a fleet GRE parent peer (so a steer onto it is a GRE join), and the fallback station up
    peer_status(box, "RPIPOD0FALLBACKPARENT", al=PARENT_AL, gre_parent_ap=GRE_PARENT_AP)
    fallback = await fallback_station(box)
    await box.until(lambda: uplink_status(box).get("bootstrap"), seconds=30)
    await asyncio.sleep(2.5)  # the agent's peer directory (1 s cache) picks up the GRE parent
    # onto the GRE parent: a 3-address GRE move
    answered = len(box.sent(0x801A))
    onto_mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, GRE_PARENT_AP),)
    )
    await box.until(lambda: box.controller.reply(ACK, onto_mid), seconds=5)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not await pinned(box, GRE_PARENT_AP):
        await asyncio.sleep(0.3)
    creds = [
        r
        for r in (await box.rows("Wifi_Credential_Config")).values()
        if str(r.get("bssid") or "").lower() == GRE_PARENT_AP
    ]
    is_gre = bool(creds) and creds[0].get("onboard_type") == "gre"
    await gre_join(box, GRE_PARENT_AP)
    onto = await next_message(box, 0x801A, answered, seconds=60)
    onto_ok = onto is not None and steering_answer(onto) == (BACKHAUL_STATION, GRE_PARENT_AP, 0)
    _, onto_fb = await box.vif("Wifi_VIF_Config", "bhaul-sta-50")
    kept_onto = {k: onto_fb.get(k) for k in fallback} == fallback
    # back to the bootstrap: the revert
    answered2 = len(box.sent(0x801A))
    back_mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, BOOTSTRAP_BSS),)
    )
    await box.until(lambda: box.controller.reply(ACK, back_mid), seconds=5)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and await pinned(box, GRE_PARENT_AP):
        await asyncio.sleep(0.3)
    await on_gtp_bootstrap(box)
    back = await next_message(box, 0x801A, answered2, seconds=60)
    back_ok = back is not None and steering_answer(back) == (BACKHAUL_STATION, BOOTSTRAP_BSS, 0)
    _, back_fb = await box.vif("Wifi_VIF_Config", "bhaul-sta-50")
    kept_back = {k: back_fb.get(k) for k in fallback} == fallback
    return box.result(
        passed=is_gre and onto_ok and kept_onto and back_ok and kept_back,
        gre_move=is_gre,
        onto=steering_answer(onto),
        fallback_kept_through_move=kept_onto,
        back=steering_answer(back),
        fallback_kept_through_revert=kept_back,
    )


SWITCH_DEADLINE = 95  # past an uplink switch's 90 s to be confirmed (spec 8.3)
RECONNECT_WINDOW = 60  # spec 2.5: a provisioned session held this long for its pod


async def gre_pinned(box):
    """The station carries a gre credential pinned to the fleet parent's AP (a 3-address join)."""
    return bool(await gre_pins(box))


async def gre_pins(box):
    """The UUIDs of the gre credentials pinned to the fleet parent's AP."""
    rows = await box.rows("Wifi_Credential_Config")
    return {
        uuid
        for uuid, r in rows.items()
        if str(r.get("bssid") or "").lower() == GRE_PARENT_AP and r.get("onboard_type") == "gre"
    }


def kept_target(box):
    """The upstream a controller's move left for the pod's later starts (spec 8.3), or None."""
    try:
        path = box.directory / "state" / box.serial / "uplink" / "target.json"
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


async def gre_child_moved(box):
    """A GRE child onboarded on its GTP bootstrap and moved onto a fleet GRE parent by the
    controller's Backhaul Steering Request, cm using the gretap on the parent: the answer, or None
    when the move did not complete."""
    if not (await onboarded(box)).get("applied"):
        return None
    peer_status(box, "RPIPOD0KEPTPARENT", al=PARENT_AL, gre_parent_ap=GRE_PARENT_AP)
    await box.until(lambda: uplink_status(box).get("bootstrap"), seconds=30)
    await asyncio.sleep(2.5)  # the agent's peer directory (1 s cache) picks up the GRE parent
    answered = len(box.sent(0x801A))
    mid = box.controller.send(0x8019, (backhaul_steering_request(BACKHAUL_STATION, GRE_PARENT_AP),))
    await box.until(lambda: box.controller.reply(ACK, mid), seconds=5)
    await until_async(lambda: gre_pinned(box), seconds=30)
    await gre_join(box, GRE_PARENT_AP)
    onto = await next_message(box, 0x801A, answered, seconds=60)
    answer = steering_answer(onto)
    return answer if answer == (BACKHAUL_STATION, GRE_PARENT_AP, 0) else None


async def until_async(predicate, seconds=60, step=0.3):
    """predicate() (a coroutine) until it holds; its last value."""
    deadline = time.monotonic() + seconds
    value = await predicate()
    while not value and time.monotonic() < deadline:
        await asyncio.sleep(step)
        value = await predicate()
    return value


async def gre_child_kept(box):
    """Spec 8.3 "Moved" for a GRE child (8.6), V54's fault row "pi2's OpenSync restarts": the
    controller's move onto a fleet GRE parent is kept for the pod's later starts. The pod's
    OpenSync starts again (a new database: back on its bootstrap, the move gone). On the new start
    the agent re-applies the move without a new steer, the same 3-address GRE join pinned to the
    parent AP, and it is applied once cm uses the gretap there: no hold, no second answer."""
    moved = await gre_child_moved(box)
    if moved is None:
        return box.result(passed=False, failed="the move onto the GRE parent did not complete")
    target = kept_target(box) or {}
    answers = len(box.sent(0x801A))
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    before = await gre_pins(box)
    continued = kept_reconnections(box)
    await box.new_pod_database()  # back on its bootstrap, the move gone
    # its OpenSync started again: a fresh session (spec 2.5), the kept target as before
    again = await back(box, searches, m1s, continued) == "fresh"

    async def pinned_anew():
        # the move written again on the new start: a credential row the old start did not have
        # (a fast agent may write it before the bootstrap can be seen, so this is the evidence)
        return (await gre_pins(box)) - before

    reapplied = await until_async(pinned_anew, seconds=60)
    on_bootstrap = bool(before) and bool(reapplied)
    await gre_join(box, GRE_PARENT_AP)

    def applied():
        status = uplink_status(box)
        operation = status.get("operation") or {}
        return operation.get("state") == "OBSERVED_APPLIED" and (
            str(status.get("parent") or "").lower() == GRE_PARENT_AP
        )

    done = await box.until(applied, seconds=60)
    status = uplink_status(box)
    return box.result(
        passed=on_bootstrap
        and bool(again)
        and bool(reapplied)
        and bool(done)
        and str(target.get("target") or "").lower() == GRE_PARENT_AP
        and target.get("mode") == "gre"
        and not status.get("held")
        and len(box.sent(0x801A)) == answers,
        kept_target=target,
        written_anew_after_restart=on_bootstrap,
        provisioned_again=again,
        reapplied_without_a_steer=bool(reapplied),
        applied=bool(done),
        held=status.get("held"),
        answers_after_restart=len(box.sent(0x801A)) - answers,
    )


async def gre_child_kept_gone(box):
    """Spec 8.3 "Moved" for a GRE child (8.6), V54's fault row "pi1 reboots": the kept GRE
    parent is gone when the child's pod starts again. The agent re-applies the move; the station
    cannot join (no cm gretap on the parent), so the pod's OpenSync starts again on its bootstrap,
    as its router checks fail. The switch is not applied within its 90 s: the agent drops the
    target without a hold and, a GRE child having no configured upstream, writes nothing more:
    the pod stays on its bootstrap until the controller steers it again."""
    moved = await gre_child_moved(box)
    if moved is None:
        return box.result(passed=False, failed="the move onto the GRE parent did not complete")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued = kept_reconnections(box)
    await box.new_pod_database()  # the child's restart, the parent gone meanwhile
    first = await back(box, searches, m1s, continued)  # a restart: a fresh session
    # its configuration observed on this start before the next (restart-during-write is the race)
    await box.until(lambda: operation_states(box)[-1:] == [("OBSERVED_APPLIED", None)], seconds=30)
    tried = await until_async(lambda: gre_pinned(box), seconds=60)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued = kept_reconnections(box)
    await box.new_pod_database()  # cm's router checks fail: OpenSync again, on its bootstrap
    again = first == "fresh" and await back(box, searches, m1s, continued) == "fresh"

    def timed_out():
        return (uplink_status(box).get("operation") or {}).get("state") == "TIMED_OUT"

    failed = await box.until(timed_out, seconds=SWITCH_DEADLINE + 60)
    await asyncio.sleep(10)
    status = uplink_status(box)
    rewritten = await gre_pinned(box)
    return box.result(
        passed=bool(tried)
        and bool(again)
        and bool(failed)
        and not status.get("held")
        and kept_target(box) is None
        and not rewritten,
        tried_kept_target=bool(tried),
        provisioned_again=again,
        kept_switch_timed_out=bool(failed),
        held=status.get("held"),
        target_dropped=kept_target(box) is None,
        written_again=rewritten,
        waiting=status.get("waiting"),
    )


async def gre_child_kept_gone_up(box):
    """Spec 8.3 "Moved" for a GRE child (8.6), "pi1 reboots" as the Pis met it (13 October): the
    kept GRE parent is gone when the child's pod starts again, but the pod keeps a working uplink
    (on the Pis, pod-bh-fallback's 2.4 GHz station), so its OpenSync does not start again. The
    agent re-applies the move and the station cannot join. The switch is not applied within its
    90 s: the agent drops the target without a hold and, a GRE child having no configured upstream,
    undoes its own write as the revert to the bootstrap does: the station's columns back as the pod
    had them on this start, EMOSA's credential deleted. The fallback station stays as the pod set
    it, and nothing more is written."""
    moved = await gre_child_moved(box)
    if moved is None:
        return box.result(passed=False, failed="the move onto the GRE parent did not complete")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued = kept_reconnections(box)
    await box.new_pod_database()  # the child's restart, the parent gone meanwhile
    fallback = await fallback_station(box)
    # its OpenSync started again: a fresh session (spec 2.5), the undo as before
    session = await back(box, searches, m1s, continued)
    tried = await until_async(lambda: gre_pinned(box), seconds=60)
    # the kept switch not applied within its deadline: the target dropped (the undo, if any,
    # follows at once and becomes the latest operation)
    failed = await box.until(lambda: kept_target(box) is None, seconds=SWITCH_DEADLINE + 60)

    async def undone():
        return not await gre_pinned(box)

    unpinned = await until_async(undone, seconds=30)
    await asyncio.sleep(5)  # nothing more is written
    status = uplink_status(box)
    _, station = await box.vif("Wifi_VIF_Config", "bhaul-sta-24")
    _, kept_fallback = await box.vif("Wifi_VIF_Config", "bhaul-sta-50")
    columns = ("ssid", "credential_configs")
    # the pod's own configuration back: its SSID (not the move's "" marker), and only the pod's
    # own credentials linked, none pinned to a BSSID
    credentials = await box.rows("Wifi_Credential_Config")
    restored = bool(station.get("ssid")) and all(
        u in credentials and not isinstance(credentials[u].get("bssid"), str)
        for u in uuids(station.get("credential_configs"))
    )
    return box.result(
        passed=session == "fresh"
        and bool(tried)
        and bool(failed)
        and bool(unpinned)
        and restored
        and not await gre_pinned(box)
        and not status.get("held")
        and kept_target(box) is None
        and {k: kept_fallback.get(k) for k in fallback} == fallback,
        session=session,
        tried_kept_target=bool(tried),
        kept_switch_timed_out=bool(failed),
        credential_removed=bool(unpinned),
        station_restored=restored,
        station=[station.get(k) for k in columns],
        held=status.get("held"),
        target_dropped=kept_target(box) is None,
        fallback_untouched={k: kept_fallback.get(k) for k in fallback} == fallback,
        waiting=status.get("waiting"),
    )


async def backhaul_steering_own_bss(box):
    """Suite finding (a pod's station on its own backhaul BSS loops br-home): a Backhaul
    Steering Request whose target is one of the pod's own BSSes is refused at once, failure
    with an Error Code, and the station stays pinned where it is (spec 8.3)."""
    if not await backhaul_on(box):
        return box.result(passed=False, failed="uplink switch not applied")
    own = next(
        r.get("mac")
        for r in (await box.rows("Wifi_VIF_State")).values()
        if r.get("if_name") == "home-ap-24"
    )
    answered = len(box.sent(0x801A))
    mid = box.controller.send(0x8019, (backhaul_steering_request(BACKHAUL_STATION, target=own),))
    response = await next_message(box, 0x801A, answered, seconds=10)
    answer = steering_answer(response)
    errors = [
        (t.value[0], t.value[1:7].hex(":"))
        for t in (response.tlvs if response else [])
        if t.kind == 0xA3
    ]
    await asyncio.sleep(2)
    return box.result(
        passed=response is not None
        and response.mid == mid
        and answer is not None
        and answer[2] == 1
        and errors == [(6, BACKHAUL_STATION)]
        and not await pinned(box, own)
        and await pinned(box, BACKHAUL_PARENT),
        own_bssid=own,
        answer=answer,
        errors=errors,
    )


# A move to a BSS on another band (spec 8.3): the recorded pod's 5 GHz backhaul station,
# disabled while its 2.4 GHz one is the backhaul, takes the uplink

OTHER_STATION_IF, OTHER_STATION = "bhaul-sta-50", "02:00:00:00:15:00"
TARGET_5G = "02:00:00:00:19:05"  # a 5 GHz backhaul BSS of the controller's


async def station_config(box, if_name):
    """(enabled, BSSID its sole credential is pinned to) of the station's Config row."""
    _, row = await box.vif("Wifi_VIF_Config", if_name)
    if row is None:
        return None, None
    links = row.get("credential_configs") or []
    creds = await box.rows("Wifi_Credential_Config")
    pins = [str(creds[u].get("bssid") or "").lower() for u in links if u in creds]
    return row.get("enabled") is True, pins[0] if len(pins) == 1 else None


async def associate(box, station, parent, channel, previous):
    """cm and the supplicant: ``station`` associated with ``parent`` (4-address, Multi-AP),
    its radio on ``channel``; ``previous`` off and no longer cm's uplink."""
    creds = await box.rows("Wifi_Credential_Config")
    _, config = await box.vif("Wifi_VIF_Config", station)
    ssid = next(
        (creds[u].get("ssid") for u in config.get("credential_configs") or [] if u in creds), ""
    )
    state, _ = await box.vif("Wifi_VIF_State", station)
    old, _ = await box.vif("Wifi_VIF_State", previous)
    radio = next(
        u
        for u, r in (await box.rows("Wifi_Radio_State")).items()
        if state in (r.get("vif_states") or [])
    )
    uplinks = await box.rows("Connection_Manager_Uplink")
    ops = [
        {
            "op": "update",
            "table": "Wifi_VIF_State",
            "where": [["_uuid", "==", ["uuid", state]]],
            "row": {
                "enabled": True,
                "ssid": ssid,
                "multi_ap": "backhaul_sta",
                "wds": True,
                "parent": parent,
            },
        },
        {
            "op": "update",
            "table": "Wifi_VIF_State",
            "where": [["_uuid", "==", ["uuid", old]]],
            "row": {"enabled": False, "parent": ["set", []], "wds": ["set", []]},
        },
        {
            "op": "update",
            "table": "Wifi_Radio_State",
            "where": [["_uuid", "==", ["uuid", radio]]],
            "row": {"channel": channel},
        },
        {
            "op": "update",
            "table": "Connection_Manager_Uplink",
            "where": [["if_name", "==", previous]],
            "row": {"is_used": False},
        },
    ]
    if not any(r.get("if_name") == station for r in uplinks.values()):
        ops.append(
            {
                "op": "insert",
                "table": "Connection_Manager_Uplink",
                "row": {"if_name": station, "if_type": "vif", "has_L2": True, "has_L3": True},
            }
        )
    ops.append(
        {
            "op": "update",
            "table": "Connection_Manager_Uplink",
            "where": [["if_name", "==", station]],
            "row": {"is_used": True},
        }
    )
    await box.transact(ops)


def steering_errors(response):
    return [
        (t.value[0], t.value[1:7].hex(":"))
        for t in (response.tlvs if response else [])
        if t.kind == 0xA3
    ]


async def backhaul_steering_across_bands(box):
    """spec 8.3: a Backhaul Steering Request to a BSS on another band moves the uplink to the
    pod's backhaul station on that band: that station enabled and pinned to the target, the
    station in use disabled, in one transaction. Once the pod's State shows the new station on
    the target, the response, success, names it: the backhaul STA associated after the move
    (EasyMesh 6.1, 17.2.33)."""
    if not await backhaul_on(box):
        return box.result(passed=False, failed="uplink switch not applied")
    answered = len(box.sent(0x801A))
    mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, TARGET_5G, 115, 36),)
    )
    ack = await box.until(lambda: box.controller.reply(ACK, mid), seconds=5)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        moved = await station_config(box, OTHER_STATION_IF) == (True, TARGET_5G)
        off = (await station_config(box, "bhaul-sta-24"))[0] is False
        if moved and off:
            break
        await asyncio.sleep(0.3)
    await associate(box, OTHER_STATION_IF, TARGET_5G, 36, "bhaul-sta-24")
    response = await next_message(box, 0x801A, answered, seconds=60)
    answer = steering_answer(response)
    return box.result(
        passed=bool(ack)
        and moved
        and off
        and response is not None
        and response.mid == mid
        and answer == (OTHER_STATION, TARGET_5G, 0),
        acked=bool(ack),
        five_ghz_station_pinned=moved,
        two_ghz_station_disabled=off,
        answer=answer,
    )


async def backhaul_steering_across_bands_fails(box):
    """spec 8.3: a move to another band's station that is not applied within the switch's
    deadline returns to the previous upstream with the previous station (enabled and pinned
    again, the other disabled), no hold, and is answered with a failure."""
    if not await backhaul_on(box):
        return box.result(passed=False, failed="uplink switch not applied")
    answered = len(box.sent(0x801A))
    mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, TARGET_5G, 115, 36),)
    )
    response = await next_message(box, 0x801A, answered, seconds=150)
    answer = steering_answer(response)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        back = await station_config(box, "bhaul-sta-24") == (True, BACKHAUL_PARENT)
        other_off = (await station_config(box, OTHER_STATION_IF))[0] is False
        if back and other_off:
            break
        await asyncio.sleep(0.5)
    held = (uplink_status(box).get("held")) is not None
    return box.result(
        passed=response is not None
        and response.mid == mid
        and answer is not None
        and answer[2] == 1
        and steering_errors(response) == [(6, BACKHAUL_STATION)]
        and back
        and other_off
        and not held,
        answer=answer,
        errors=steering_errors(response),
        back_on_the_previous_station=back,
        other_station_disabled=other_off,
        held=held,
    )


async def refused_with_channel_code(box, operating_class, channel, target):
    """A Backhaul Steering Request refused at once: failure, Error Code 0x04 (the backhaul STA
    cannot operate on the channel), nothing written: the station stays pinned where it is."""
    if not await backhaul_on(box):
        return box.result(passed=False, failed="uplink switch not applied")
    answered = len(box.sent(0x801A))
    mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, target, operating_class, channel),)
    )
    response = await next_message(box, 0x801A, answered, seconds=10)
    answer = steering_answer(response)
    await asyncio.sleep(2)
    stays = await station_config(box, "bhaul-sta-24") == (True, BACKHAUL_PARENT)
    return box.result(
        passed=response is not None
        and response.mid == mid
        and answer is not None
        and answer[2] == 1
        and steering_errors(response) == [(4, BACKHAUL_STATION)]
        and stays,
        answer=answer,
        errors=steering_errors(response),
        station_stays=stays,
    )


async def backhaul_steering_no_station_on_band(box):
    """spec 8.3: a target on a band the pod has no backhaul station on (6 GHz)."""
    return await refused_with_channel_code(box, 131, 37, "02:00:00:00:19:06")


async def backhaul_steering_channel_not_operable(box):
    """spec 8.3: a 2.4 GHz target on another channel than the radio that carries the pod's BSSes
    (channel 6): its station cannot leave that channel."""
    return await refused_with_channel_code(box, 81, 11, "02:00:00:00:19:0b")


# Pods as parents of other pods (spec 8.5). The other pod's agent is in the box as the peer
# directory reads it: its status beside the agent's, written as that agent writes it.

PARENT_AL, CHILD_AL = "02:72:00:00:00:0a", "02:72:00:00:00:0b"
CHILD_STATION = "02:00:00:00:0c:01"  # the child pod's backhaul station
MEASURED = {  # a station's measurement as an agent's status gives it (spec 3.6)
    "tx_rate_mbps": 72.2,
    "rx_rate_mbps": 65.0,
    "snr_db": 40,
    "tx_frames": 1200,
    "rx_frames": 900,
    "tx_errors": 3,
    "rx_errors": 1,
}


def peer_status(
    box,
    pod_id,
    *,
    al,
    backhaul_bsses=(),
    station=None,
    parent=None,
    stations=None,
    gre_parent_ap=None,
):
    """Another pod's agent in the fleet, as its status gives it (alive: the box's own process).
    gre_parent_ap: the BSSID of this peer's GRE parent AP (spec 8.6), so the agent reads it as a
    fleet parent and a steer onto it is a 3-address GRE join, not a Multi-AP switch."""
    directory = box.run_root / pod_id
    directory.mkdir(parents=True, exist_ok=True)
    status = {
        "pod_id": pod_id,
        "agent_al": al,
        "worker_pid": os.getpid(),
        "updated": time.time(),
        "pod": {
            "bsses": [
                {"role": "backhaul", "bssid": b, "ssid": "mesh_backhaul"} for b in backhaul_bsses
            ],
            "backhaul": None
            if station is None
            else {"station": "bhaul-sta-24", "mac": station, "parent": parent, "band": "2.4G"},
        },
        "telemetry": {"stations": stations or {}},
    }
    if gre_parent_ap is not None:
        status["gre_parent"] = {"ap_bssid": gre_parent_ap}
    (directory / "status.json").write_text(json.dumps(status))


def neighbor_tlvs(topology):
    """(local interface, [neighbor ALs]) of each 1905 Neighbor Device TLV of a Topology Response."""
    out = []
    for t in topology.tlvs if topology else ():
        if t.kind == 0x07 and len(t.value) >= 6:
            entries = t.value[6:]
            out.append(
                (
                    t.value[:6].hex(":"),
                    [entries[i : i + 6].hex(":") for i in range(0, len(entries), 7)],
                )
            )
    return out


def link_metrics(response):
    """The TLVs of a Link Metric Response: (kind, neighbor AL, local interface, neighbor interface,
    values), TX values (errors, packets, throughput, availability, PHY rate), RX (errors, packets,
    RSSI)."""
    out = []
    for t in response.tlvs if response else ():
        v = t.value
        if t.kind == 0x09 and len(v) >= 41:
            values = struct.unpack("!IIHHH", v[27:41])
        elif t.kind == 0x0A and len(v) >= 35:
            values = struct.unpack("!IIB", v[26:35])
        else:
            continue
        out.append((t.kind, v[6:12].hex(":"), v[12:18].hex(":"), v[18:24].hex(":"), list(values)))
    return out


def backhaul_neighbors(box):
    return ((box.status() or {}).get("pod") or {}).get("backhaul_neighbors") or []


async def pod_child(box):
    """spec 8.5: the pod on its EasyMesh backhaul, its upstream another pod's backhaul BSS: the
    Topology Response names that pod's agent as its 1905 neighbor on the backhaul station (the
    controller on Ethernet as before), and a Link Metric Query is answered from the parent's
    measurement of the station, the directions swapped."""
    measured = {**MEASURED, "measured_at": time.time()}
    peer_status(
        box,
        "PARENTPOD",
        al=PARENT_AL,
        backhaul_bsses=(BACKHAUL_PARENT,),
        stations={BACKHAUL_STATION: measured},
    )
    if not await backhaul_on(box):
        return box.result(passed=False, failed="uplink switch not applied")
    linked = await box.until(lambda: backhaul_neighbors(box), seconds=15)
    topology = await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)
    neighbors = neighbor_tlvs(topology)
    response = await box.ask(0x0005, (Tlv(0x08, b"\x00\x02"),), 0x0006)
    metrics = link_metrics(response)
    expected = [
        (0x09, PARENT_AL, BACKHAUL_STATION, BACKHAUL_PARENT, [1, 900, 65, 100, 65535]),
        (0x0A, PARENT_AL, BACKHAUL_STATION, BACKHAUL_PARENT, [3, 1200, 40]),
    ]
    return box.result(
        passed=bool(linked)
        and neighbors[0][1] == [box.controller.al.hex(":")]
        and (BACKHAUL_STATION, [PARENT_AL]) in neighbors
        and metrics == expected,
        status_neighbors=backhaul_neighbors(box),
        neighbors=neighbors,
        link_metrics=metrics,
    )


async def pod_parent(box):
    """spec 8.5: another pod's station on this pod's backhaul BSS, that pod's backhaul parent: the
    Topology Response names its agent as a 1905 neighbor on the BSS, and a Link Metric Query is
    answered from this pod's own measurement of the station (its client report, spec 3.6)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    await attach_vifs(box.db)  # wm gives the backhaul BSS the agent created its BSSID
    _, bss = await box.vif("Wifi_VIF_State", "b-ap-24")
    bssid = (bss or {}).get("mac")
    if not bssid:
        return box.result(passed=False, failed="no backhaul BSS")
    peer_status(box, "CHILDPOD", al=CHILD_AL, station=CHILD_STATION, parent=bssid)
    await box.client_join(CHILD_STATION, "b-ap-24")
    if not await telemetry_applied(box):
        return box.result(passed=False, failed="statistics publishing not applied")
    from emosa_lab.conformance import COUNTERS_FULL, client_report

    now = int(time.time() * 1000)
    await publish(box, client_report(now - 1000, [(CHILD_STATION, 40, 72.2, 65.0, COUNTERS_FULL)]))

    def measured():
        return CHILD_STATION in (telemetry_status(box).get("stations") or {})

    reported = await box.until(measured, seconds=30)
    linked = await box.until(lambda: backhaul_neighbors(box), seconds=15)
    topology = await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)
    neighbors = neighbor_tlvs(topology)
    response = await box.ask(
        0x0005, (Tlv(0x08, b"\x01" + bytes.fromhex(CHILD_AL.replace(":", "")) + b"\x02"),), 0x0006
    )
    metrics = link_metrics(response)
    seen = (telemetry_status(box).get("stations") or {}).get(CHILD_STATION) or {}
    expected = [
        (
            0x09,
            CHILD_AL,
            bssid,
            CHILD_STATION,
            [
                seen.get("tx_errors"),
                seen.get("tx_frames"),
                int(seen.get("tx_rate_mbps") or 0),
                100,
                65535,
            ],
        ),
        (
            0x0A,
            CHILD_AL,
            bssid,
            CHILD_STATION,
            [seen.get("rx_errors"), seen.get("rx_frames"), seen.get("snr_db")],
        ),
    ]
    return box.result(
        passed=bool(reported)
        and bool(linked)
        and (bssid, [CHILD_AL]) in neighbors
        and metrics == expected,
        backhaul_bss=bssid,
        status_neighbors=backhaul_neighbors(box),
        neighbors=neighbors,
        link_metrics=metrics,
        measured=seen,
    )


async def pod_parent_loop(box):
    """spec 8.5: a Backhaul Steering Request whose target is a BSS of a pod that hangs off this
    one would put a loop into br-home: refused at once (failure, an Error Code), the station
    left where it is."""
    if not await backhaul_on(box):
        return box.result(passed=False, failed="uplink switch not applied")
    own = next(
        r.get("mac")
        for r in (await box.rows("Wifi_VIF_State")).values()
        if r.get("if_name") == "home-ap-24"
    )
    downstream = "72:00:00:00:0d:00"  # the child pod's backhaul BSS; the child is on this pod
    peer_status(
        box,
        "CHILDPOD",
        al=CHILD_AL,
        backhaul_bsses=(downstream,),
        station=CHILD_STATION,
        parent=own,
    )
    await asyncio.sleep(1.5)  # the agent's directory is read at most once a second
    answered = len(box.sent(0x801A))
    mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, target=downstream),)
    )
    response = await next_message(box, 0x801A, answered, seconds=10)
    answer = steering_answer(response)
    await asyncio.sleep(2)
    return box.result(
        passed=response is not None
        and response.mid == mid
        and answer is not None
        and answer[2] == 1
        and not await pinned(box, downstream)
        and await pinned(box, BACKHAUL_PARENT),
        answer=answer,
    )


# spec 8.6 (draft): a pod as the GRE parent of other pods. The box's pod runs a gre-parent
# profile with an underlay; the controller's backhaul BSS becomes the parent AP b-ap-24, and
# a child is a lease in the underlay whose MAC is associated to it.
GRE_UNDERLAY = "169.254.2.0/24"
GRE_AP = "b-ap-24"
GRE_CHILD_MAC = "02:00:00:00:2a:01"
GRE_CHILD_IP = "169.254.2.10"  # its tunnel is pgd2_10


async def gre_parent_up(box, parent=GRE_AP):
    """Onboard a gre-parent pod: the M2's backhaul BSS written as the parent AP. (parent AP
    Config row, None) on success, else (None, a failed result)."""
    state = await onboarded(box)
    if not state.get("applied"):
        return None, box.result(passed=False, failed=state.get("failed", "not onboarded"))
    _, ap = await box.vif("Wifi_VIF_Config", parent)
    if not ap:
        return None, box.result(passed=False, failed=f"no parent AP {parent}")
    return ap, None


async def gre_inet(box, if_name):
    """The pod's Wifi_Inet_Config row for an interface, decoded, or None."""
    for row in (await box.rows("Wifi_Inet_Config")).values():
        if row.get("if_name") == if_name:
            return row
    return None


async def gre_parent_create(box):
    """spec 8.6: a backhaul BSS of the controller's M2 set is written as the pod's GRE parent AP
    (b-ap-24): not Multi-AP, no bridge, no bridging between its stations, hidden; its Inet row the
    pod's underlay at .1 with DHCP .10 to .250. The agent recognizes it as its parent AP."""
    ap, failure = await gre_parent_up(box)
    if failure:
        return failure
    inet = await gre_inet(box, GRE_AP)
    dhcpd = ovsdb_map((inet or {}).get("dhcpd"))
    status = (box.status() or {}).get("gre_parent") or {}
    ap_ok = (
        ap.get("multi_ap") == "none"
        and ap.get("bridge") == ""
        and ap.get("ap_bridge") is False
        and ap.get("ssid_broadcast") == "disabled"
    )
    inet_ok = (
        inet is not None
        and inet.get("inet_addr") == "169.254.2.1"
        and inet.get("netmask") == "255.255.255.0"
        and inet.get("mtu") == 1600
        and dhcpd.get("start") == "169.254.2.10"
        and dhcpd.get("stop") == "169.254.2.250"
    )
    return box.result(
        passed=ap_ok
        and inet_ok
        and status.get("ap") == GRE_AP
        and status.get("underlay") == GRE_UNDERLAY,
        parent_ap={
            k: ap.get(k) for k in ("ssid", "multi_ap", "bridge", "ap_bridge", "ssid_broadcast")
        },
        underlay_inet=inet,
        gre_status=status,
    )


GRE_PROFILE = "opensync-lab-hwsim-6.6.1-gre-v1"
PLAIN_PROFILE = "opensync-lab-hwsim-6.6.1-v1"


async def with_profile(box, profile, done):
    """The pod's profile changed (a promotion or demotion, spec 8.6): its agent killed, its
    configuration given the profile, the agent started again on its journal, and every Search and
    M1 answered until done(operations before) holds. False at the deadline."""
    box.process.kill()
    box.process.wait(10)
    config_path = box.directory / "agent.json"
    config = json.loads(config_path.read_text())
    config["profile"] = profile
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    operations = len((box.status() or {}).get("operations") or [])
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.restart_agent()
    found = await serve_radios(box, lambda: done(operations), searches=searches, m1s=m1s)
    return found is not None


async def pod_own_rows(box):
    """The pod's own rows as a GRE parent's changes must leave them: its Inet rows and VIFs other
    than EMOSA's (the fronthaul, the slots, the tunnels), and br-home's ports other than the
    tunnels'."""
    emosa = {COLD_VIF, "svc-d-ap-24", "svc-e-ap-24", "fh-24", GRE_AP}

    def own(rows):
        return {
            u: r
            for u, r in rows.items()
            if r.get("if_name") not in emosa and not str(r.get("if_name", "")).startswith("pgd")
        }

    ports = await box.rows("Port")
    tunnels = {u for u, p in ports.items() if str(p.get("name", "")).startswith("pgd")}
    bridge = next(
        (b for b in (await box.rows("Bridge")).values() if b.get("name") == "br-home"), {}
    )
    return {
        "inet": own(await box.rows("Wifi_Inet_Config")),
        "vifs": own(await box.rows("Wifi_VIF_Config")),
        "br-home": sorted(set(uuids(bridge.get("ports"))) - tunnels),
    }


async def gre_parent_demoted(box):
    """spec 8.6, a parent demoted (the mirror of finding A): a GRE parent with a child's tunnel,
    its profile flipped back to the plain one and its agent restarted on its journal. The
    unchanged M2 set makes the same b-ap-24 row a Multi-AP backhaul BSS again and its Inet row the
    profile's slot row: no underlay address and no DHCP server left on the backhaul. The tunnel
    left on it goes with its br-home port, the pod's own rows are untouched, and the agent no
    longer advertises a parent AP."""
    ap, failure = await gre_parent_up(box)
    if failure:
        return failure
    await gre_parent_ready(box)
    await box.transact(
        [
            {
                "op": "insert",
                "table": "DHCP_leased_IP",
                "row": {"hwaddr": GRE_CHILD_MAC, "inet_addr": GRE_CHILD_IP},
            }
        ]
    )
    await box.client_join(GRE_CHILD_MAC, GRE_AP)
    if not await gre_await_tunnel(box, "pgd2_10", present=True):
        return box.result(passed=False, failed="the child's tunnel was not made")
    await gre_nm_apply(box)
    await apply_configuration(box.db)
    uuid, _ = await box.vif("Wifi_VIF_Config", GRE_AP)
    own_before = await pod_own_rows(box)

    async def demoted(operations):
        ops = (box.status() or {}).get("operations") or []
        _, vif = await box.vif("Wifi_VIF_Config", GRE_AP)
        inet = await gre_inet(box, GRE_AP) or {}
        return (
            len(ops) > operations
            and all_applied(ops)
            and (vif or {}).get("multi_ap") == "backhaul_bss"
            and not ovsdb_map(inet.get("dhcpd"))
            and await gre_inet(box, "pgd2_10") is None
        )

    if not await with_profile(box, PLAIN_PROFILE, demoted):
        return box.result(passed=False, failed="b-ap-24 was not made a Multi-AP backhaul again")
    after_uuid, vif = await box.vif("Wifi_VIF_Config", GRE_AP)
    inet = await gre_inet(box, GRE_AP) or {}
    status = (box.status() or {}).get("gre_parent") or {}
    own_after = await pod_own_rows(box)
    # an optional column unset is the empty set
    plain = {
        k: None if inet.get(k) == ["set", []] else inet.get(k)
        for k in ("ip_assign_scheme", "inet_addr", "netmask", "mtu")
    }
    return box.result(
        passed=after_uuid == uuid
        and (vif or {}).get("multi_ap") == "backhaul_bss"
        and (vif or {}).get("bridge") == "br-home"
        and plain == {"ip_assign_scheme": "none", "inet_addr": None, "netmask": None, "mtu": 1500}
        and not ovsdb_map(inet.get("dhcpd"))
        and not await gre_port_in_home(box, "pgd2_10")
        and own_after == own_before
        and status.get("withdrawn") is True
        and status.get("ap_bssid") is None,
        same_row=after_uuid == uuid,
        multi_ap=(vif or {}).get("multi_ap"),
        inet=plain,
        dhcpd=ovsdb_map(inet.get("dhcpd")),
        own_rows_unchanged=own_after == own_before,
        gre_status=status,
    )


async def gre_parent_promoted(box):
    """Finding A (spec 8.6): a pod onboarded with a Multi-AP backhaul BSS (b-ap-24, multi_ap
    backhaul_bss), then promoted to GRE parent while onboarded: its profile flipped to the
    gre-parent one and its agent restarted on its journal. The controller's unchanged M2 set is
    not taken as applied while b-ap-24 is still a Multi-AP BSS: the agent converts that same row
    into the parent AP (multi_ap none, no bridge, hidden) and writes its underlay Inet row, in one
    transaction. A Renew afterwards finds it applied and writes nothing."""
    state = await onboarded(box)
    if not state.get("applied"):
        return box.result(passed=False, failed=state.get("failed", "not onboarded"))
    uuid, before = await box.vif("Wifi_VIF_Config", GRE_AP)
    if not before or before.get("multi_ap") != "backhaul_bss":
        return box.result(passed=False, failed="no Multi-AP backhaul BSS b-ap-24 to promote")
    await apply_configuration(box.db)

    async def converted(operations):
        ops = (box.status() or {}).get("operations") or []
        _, ap = await box.vif("Wifi_VIF_Config", GRE_AP)
        return len(ops) > operations and all_applied(ops) and (ap or {}).get("multi_ap") == "none"

    if not await with_profile(box, GRE_PROFILE, converted):
        return box.result(passed=False, failed="b-ap-24 was not converted into the parent AP")
    after_uuid, ap = await box.vif("Wifi_VIF_Config", GRE_AP)
    inet = await gre_inet(box, GRE_AP)
    ops = (box.status() or {}).get("operations") or []
    wrote = not (ops[-1].get("application_evidence") or {}).get("observed_noop")
    # a Renew: the same M2 set, now applied
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.controller.send(RENEW, (Tlv(0x01, box.controller.al), Tlv(0x0F, b"\0"), Tlv(0x10, b"\0")))
    count = len(ops)

    async def taken_again():
        again = (box.status() or {}).get("operations") or []
        return len(again) > count and all_applied(again)

    renewed = await serve_radios(box, taken_again, searches=searches, m1s=m1s) is not None
    again = (box.status() or {}).get("operations") or []
    noop = bool((again[-1].get("application_evidence") or {}).get("observed_noop"))
    status = (box.status() or {}).get("gre_parent") or {}
    rendered = {k: (ap or {}).get(k) for k in ("multi_ap", "bridge", "ap_bridge", "ssid_broadcast")}
    return box.result(
        passed=after_uuid == uuid
        and rendered
        == {"multi_ap": "none", "bridge": "", "ap_bridge": False, "ssid_broadcast": "disabled"}
        and (inet or {}).get("inet_addr") == "169.254.2.1"
        and wrote
        and renewed
        and noop
        and status.get("ap") == GRE_AP,
        same_row=after_uuid == uuid,
        parent_ap=rendered,
        underlay_inet=(inet or {}).get("inet_addr"),
        conversion_written=wrote,
        renew_noop=noop,
    )


async def gre_parent_ready(box):
    """nm applies the agent's underlay to the AP's Wifi_Inet_State, and the pod's bootstrap has
    br-home: what a tunnel needs besides the parent AP. br-home hangs from the Open_vSwitch root
    (Open_vSwitch -> Bridge -> Port -> Interface), so it is not garbage-collected."""
    await box.transact(
        [
            {
                "op": "insert",
                "table": "Wifi_Inet_State",
                "row": {"if_name": GRE_AP, "if_type": "vif", "inet_addr": "169.254.2.1"},
            },
            {
                "op": "insert",
                "table": "Interface",
                "uuid-name": "brhomeif",
                "row": {"name": "br-home"},
            },
            {
                "op": "insert",
                "table": "Port",
                "uuid-name": "brhomeport",
                "row": {"name": "br-home", "interfaces": ["set", [["named-uuid", "brhomeif"]]]},
            },
            {
                "op": "insert",
                "table": "Bridge",
                "uuid-name": "brhomebr",
                "row": {"name": "br-home", "ports": ["set", [["named-uuid", "brhomeport"]]]},
            },
            {
                "op": "insert",
                "table": "Open_vSwitch",
                "row": {"bridges": ["set", [["named-uuid", "brhomebr"]]]},
            },
        ]
    )


async def gre_port_in_home(box, name):
    """The Port `name` is a member of br-home (the tunnel joined the bridge)."""
    ports = await box.rows("Port")
    bridge = next(
        (r for r in (await box.rows("Bridge")).values() if r.get("name") == "br-home"), None
    )
    members = {ports[u]["name"] for u in uuids((bridge or {}).get("ports")) if u in ports}
    return name in members


async def gre_wm_remove_ap(box, if_name):
    """OpenSync's wm tears down a VIF whose Config the agent deleted: its Wifi_VIF_State and the
    clients on it go, and the radio's State drops the reference. Nothing when it is already gone."""
    uuid, _ = await box.vif("Wifi_VIF_State", if_name)
    if uuid is None:
        return
    vif = (await box.rows("Wifi_VIF_State")).get(uuid, {})
    ops = []
    for radio_uuid, radio in (await box.rows("Wifi_Radio_State")).items():
        if uuid in uuids(radio.get("vif_states")):
            ops.append(
                {
                    "op": "mutate",
                    "table": "Wifi_Radio_State",
                    "where": [["_uuid", "==", ["uuid", radio_uuid]]],
                    "mutations": [["vif_states", "delete", ["set", [["uuid", uuid]]]]],
                }
            )
    for client in uuids(vif.get("associated_clients")):
        ops.append(
            {
                "op": "delete",
                "table": "Wifi_Associated_Clients",
                "where": [["_uuid", "==", ["uuid", client]]],
            }
        )
    ops.append(
        {"op": "delete", "table": "Wifi_VIF_State", "where": [["_uuid", "==", ["uuid", uuid]]]}
    )
    for state_uuid, state in (await box.rows("Wifi_Inet_State")).items():
        if state.get("if_name") == if_name:  # nm drops the interface's address with the netdev
            ops.append(
                {
                    "op": "delete",
                    "table": "Wifi_Inet_State",
                    "where": [["_uuid", "==", ["uuid", state_uuid]]],
                }
            )
    await box.transact(ops)


async def gre_nm_apply(box):
    """nm brings up each gretap the agent created: it gives the gre interface a Wifi_Inet_State,
    which is how the tunnel reconcile sees the tunnel applied (OBSERVED_APPLIED)."""
    states = {r.get("if_name") for r in (await box.rows("Wifi_Inet_State")).values()}
    ops = [
        {
            "op": "insert",
            "table": "Wifi_Inet_State",
            "row": {"if_name": r.get("if_name"), "if_type": "gre"},
        }
        for r in (await box.rows("Wifi_Inet_Config")).values()
        if r.get("if_type") == "gre" and r.get("if_name") not in states
    ]
    if ops:
        await box.transact(ops)


async def gre_await_applied(box, seconds=30):
    """Poll until the tunnel reconcile's latest operation is OBSERVED_APPLIED."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and box.process.poll() is None:
        operation = ((box.status() or {}).get("gre_parent") or {}).get("operation") or {}
        if operation.get("state") == "OBSERVED_APPLIED":
            return True
        await asyncio.sleep(0.3)
    return False


async def gre_await_config_gone(box, if_name, seconds=30):
    """Poll until the agent has deleted a VIF's Wifi_VIF_Config row."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and box.process.poll() is None:
        if (await box.vif("Wifi_VIF_Config", if_name))[1] is None:
            return True
        await asyncio.sleep(0.3)
    return False


async def gre_await_tunnel(box, name, present, seconds=60):
    """Poll the pod's own rows (not the status, which lags a tick) until the tunnel `name` is
    there (present) or gone: its gre Wifi_Inet_Config row and its br-home port both exist, or
    both do not."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and box.process.poll() is None:
        there = await gre_inet(box, name) is not None and await gre_port_in_home(box, name)
        if there == present:
            return True
        await asyncio.sleep(0.3)
    return False


async def gre_parent_child(box):
    """spec 8.6: a child that associated to the parent AP and took a lease in the underlay gets a
    gre Wifi_Inet_Config row pgd<b3>_<b4> from the AP's address to the lease, and a port of
    br-home."""
    ap, failure = await gre_parent_up(box)
    if failure:
        return failure
    await gre_parent_ready(box)
    await box.transact(
        [
            {
                "op": "insert",
                "table": "DHCP_leased_IP",
                "row": {"hwaddr": GRE_CHILD_MAC, "inet_addr": GRE_CHILD_IP},
            }
        ]
    )
    await box.client_join(GRE_CHILD_MAC, GRE_AP)
    made = await gre_await_tunnel(box, "pgd2_10", present=True)
    row = await gre_inet(box, "pgd2_10")
    bridged = await gre_port_in_home(box, "pgd2_10")
    await gre_nm_apply(box)  # nm brings the gretap up
    applied = await gre_await_applied(box)
    return box.result(
        passed=bool(made)
        and row is not None
        and row.get("if_type") == "gre"
        and row.get("gre_ifname") == GRE_AP
        and row.get("gre_remote_inet_addr") == GRE_CHILD_IP
        and row.get("gre_local_inet_addr") == "169.254.2.1"
        and row.get("mtu") == 1562
        and bridged
        and bool(applied),
        tunnel=row,
        in_br_home=bridged,
        gre_status=(box.status() or {}).get("gre_parent"),
    )


LEGACY_PARENT_IP, LEGACY_CHILD_IP = "169.254.5.1", "169.254.5.10"  # LEGACY_UNDERLAY's; pgd5_10


async def legacy_parent_ready(box):
    """As gre_parent_ready, on a legacy pod (spec 3.10): nm's State of the parent AP wl1.1 on its
    underlay's .1 (updated when the seed has a row for it), and br-home from the Open_vSwitch
    root unless the pod has one already."""
    states = {r.get("if_name"): u for u, r in (await box.rows("Wifi_Inet_State")).items()}
    row = {"if_name": LEGACY_PARENT, "if_type": "vif", "inet_addr": LEGACY_PARENT_IP}
    ops = [
        {
            "op": "update",
            "table": "Wifi_Inet_State",
            "where": [["_uuid", "==", ["uuid", states[LEGACY_PARENT]]]],
            "row": row,
        }
        if LEGACY_PARENT in states
        else {"op": "insert", "table": "Wifi_Inet_State", "row": row}
    ]
    if not any(r.get("name") == "br-home" for r in (await box.rows("Bridge")).values()):
        ops += [
            {
                "op": "insert",
                "table": "Interface",
                "uuid-name": "brhomeif",
                "row": {"name": "br-home"},
            },
            {
                "op": "insert",
                "table": "Port",
                "uuid-name": "brhomeport",
                "row": {"name": "br-home", "interfaces": ["set", [["named-uuid", "brhomeif"]]]},
            },
            {
                "op": "insert",
                "table": "Bridge",
                "uuid-name": "brhomebr",
                "row": {"name": "br-home", "ports": ["set", [["named-uuid", "brhomeport"]]]},
            },
            {
                "op": "insert",
                "table": "Open_vSwitch",
                "row": {"bridges": ["set", [["named-uuid", "brhomebr"]]]},
            },
        ]
    await box.transact(ops)


async def legacy_parent_child(box):
    """spec 3.10, 8.6, a legacy pod as another pod's parent (the owner's mix and match, 14
    October): with the legacy profile its GRE parent AP is wl1.1 on its underlay; a child (a
    6.6 pod's station, say) that associated there and took a lease gets a gre Wifi_Inet_Config row
    pgd5_10 from the AP's .1 to the lease (MTU 1562) and a port of br-home, in the pod's OpenSync
    2.0 schema, as on a 6.6 parent (gre-parent-child)."""
    ap, failure = await gre_parent_up(box, LEGACY_PARENT)
    if failure:
        return failure
    await legacy_parent_ready(box)
    await box.transact(
        [
            {
                "op": "insert",
                "table": "DHCP_leased_IP",
                "row": {"hwaddr": GRE_CHILD_MAC, "inet_addr": LEGACY_CHILD_IP},
            }
        ]
    )
    await box.client_join(GRE_CHILD_MAC, LEGACY_PARENT)
    made = await gre_await_tunnel(box, "pgd5_10", present=True)
    row = await gre_inet(box, "pgd5_10")
    bridged = await gre_port_in_home(box, "pgd5_10")
    await gre_nm_apply(box)
    applied = await gre_await_applied(box)
    return box.result(
        passed=bool(made)
        and row is not None
        and row.get("if_type") == "gre"
        and row.get("gre_ifname") == LEGACY_PARENT
        and row.get("gre_remote_inet_addr") == LEGACY_CHILD_IP
        and row.get("gre_local_inet_addr") == LEGACY_PARENT_IP
        and row.get("mtu") == 1562
        and bridged
        and bool(applied),
        tunnel=row,
        in_br_home=bridged,
        gre_status=(box.status() or {}).get("gre_parent"),
    )


async def legacy_parent_child_gone(box):
    """legacy-parent-child, then the child leaves: its lease and association end, and its tunnel
    row and br-home port are removed, as on a 6.6 parent (gre-parent-child-gone)."""
    ap, failure = await gre_parent_up(box, LEGACY_PARENT)
    if failure:
        return failure
    await legacy_parent_ready(box)
    await box.transact(
        [
            {
                "op": "insert",
                "table": "DHCP_leased_IP",
                "row": {"hwaddr": GRE_CHILD_MAC, "inet_addr": LEGACY_CHILD_IP},
            }
        ]
    )
    await box.client_join(GRE_CHILD_MAC, LEGACY_PARENT)
    if not await gre_await_tunnel(box, "pgd5_10", present=True):
        return box.result(passed=False, failed="the tunnel was never made")
    await gre_nm_apply(box)
    await gre_await_applied(box)
    await box.client_leave(GRE_CHILD_MAC, LEGACY_PARENT)
    lease = next(
        u
        for u, r in (await box.rows("DHCP_leased_IP")).items()
        if r.get("inet_addr") == LEGACY_CHILD_IP
    )
    await box.transact(
        [{"op": "delete", "table": "DHCP_leased_IP", "where": [["_uuid", "==", ["uuid", lease]]]}]
    )
    removed = await gre_await_tunnel(box, "pgd5_10", present=False)
    return box.result(
        passed=bool(removed),
        removed=bool(removed),
        tunnel=await gre_inet(box, "pgd5_10"),
        gre_status=(box.status() or {}).get("gre_parent"),
    )


async def gre_parent_child_gone(box):
    """spec 8.6: when a child's lease or association ends, its tunnel row and its br-home port are
    removed."""
    ap, failure = await gre_parent_up(box)
    if failure:
        return failure
    await gre_parent_ready(box)
    await box.transact(
        [
            {
                "op": "insert",
                "table": "DHCP_leased_IP",
                "row": {"hwaddr": GRE_CHILD_MAC, "inet_addr": GRE_CHILD_IP},
            }
        ]
    )
    await box.client_join(GRE_CHILD_MAC, GRE_AP)
    if not await gre_await_tunnel(box, "pgd2_10", present=True):
        return box.result(passed=False, failed="the tunnel was never made")
    await gre_nm_apply(box)  # the gretap up, so the create op settles before the child leaves
    await gre_await_applied(box)
    await box.client_leave(GRE_CHILD_MAC, GRE_AP)
    lease = next(
        u
        for u, r in (await box.rows("DHCP_leased_IP")).items()
        if r.get("inet_addr") == GRE_CHILD_IP
    )
    await box.transact(
        [{"op": "delete", "table": "DHCP_leased_IP", "where": [["_uuid", "==", ["uuid", lease]]]}]
    )
    removed = await gre_await_tunnel(box, "pgd2_10", present=False)
    row = await gre_inet(box, "pgd2_10")
    bridged = await gre_port_in_home(box, "pgd2_10")
    return box.result(
        passed=bool(removed) and row is None and not bridged,
        tunnel=row,
        in_br_home=bridged,
        gre_status=(box.status() or {}).get("gre_parent"),
    )


async def gre_drift_remove_tunnel(box, name):
    """Remove a tunnel's rows as if nm or a hand delete took it (drift): its gre Wifi_Inet_Config
    and Wifi_Inet_State, and its Port out of br-home (the Port and its Interface with it). The
    child's lease and association are left in place."""

    def delete(table, u):
        return {"op": "delete", "table": table, "where": [["_uuid", "==", ["uuid", u]]]}

    ops = [
        delete(t, u)
        for t in ("Wifi_Inet_Config", "Wifi_Inet_State")
        for u, r in (await box.rows(t)).items()
        if r.get("if_name") == name
    ]
    ports = await box.rows("Port")
    port_uuid = next((u for u, r in ports.items() if r.get("name") == name), None)
    if port_uuid is not None:
        bridge = next(
            (u for u, r in (await box.rows("Bridge")).items() if r.get("name") == "br-home"), None
        )
        if bridge is not None:
            ops.append(
                {
                    "op": "mutate",
                    "table": "Bridge",
                    "where": [["_uuid", "==", ["uuid", bridge]]],
                    "mutations": [["ports", "delete", ["set", [["uuid", port_uuid]]]]],
                }
            )
        ops += [delete("Interface", iu) for iu in uuids(ports[port_uuid].get("interfaces"))]
        ops.append(delete("Port", port_uuid))
    if ops:
        await box.transact(ops)


async def gre_parent_child_redrift(box):
    """spec 8.6 / 7f 11 Oct: a child's tunnel that drifts away while the child is still an
    associated lease (a re-appearance with the same lease after a backhaul bounce, or a hand
    delete) is re-created. The applied-state alone never stands in for the tunnels present."""
    ap, failure = await gre_parent_up(box)
    if failure:
        return failure
    await gre_parent_ready(box)
    await box.transact(
        [
            {
                "op": "insert",
                "table": "DHCP_leased_IP",
                "row": {"hwaddr": GRE_CHILD_MAC, "inet_addr": GRE_CHILD_IP},
            }
        ]
    )
    await box.client_join(GRE_CHILD_MAC, GRE_AP)
    if not await gre_await_tunnel(box, "pgd2_10", present=True):
        return box.result(passed=False, failed="the tunnel was never made")
    await gre_nm_apply(box)
    if not await gre_await_applied(box):
        return box.result(passed=False, failed="the first tunnel never settled")
    first = (((box.status() or {}).get("gre_parent") or {}).get("operation") or {}).get(
        "operation_id"
    )
    # the tunnel drifts away while the child stays an associated lease (the gre row and its port
    # are deleted). The scope re-creates it so promptly that the gone state is not reliably
    # observable; the proof is a fresh write (a new operation), not a glimpse of the gap.
    await gre_drift_remove_tunnel(box, "pgd2_10")
    # the scope re-creates it in a fresh write; nm brings the new gretap up so that write settles.
    # Poll both together (a sequential remade->nm->applied races the prompt re-create under load).
    remade = False
    op_id = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and box.process.poll() is None:
        await gre_nm_apply(box)
        present = await gre_inet(box, "pgd2_10") is not None and await gre_port_in_home(
            box, "pgd2_10"
        )
        operation = ((box.status() or {}).get("gre_parent") or {}).get("operation") or {}
        op_id = operation.get("operation_id")
        if present and op_id not in (None, first) and operation.get("state") == "OBSERVED_APPLIED":
            remade = True
            break
        await asyncio.sleep(0.3)
    row = await gre_inet(box, "pgd2_10")
    bridged = await gre_port_in_home(box, "pgd2_10")
    return box.result(
        passed=remade  # a fresh write (op != first) re-made the tunnel and settled
        and row is not None
        and row.get("gre_remote_inet_addr") == GRE_CHILD_IP
        and row.get("gre_ifname") == GRE_AP
        and bridged,
        tunnel=row,
        in_br_home=bridged,
        first_op=first,
        re_created_by=op_id,
        gre_status=(box.status() or {}).get("gre_parent"),
    )


async def gre_parent_withdrawn(box):
    """spec 8.6: the controller drops the backhaul BSS from the M2 set (a renew with only the
    fronthaul). EMOSA's AP scope removes the parent AP b-ap-24 and its underlay Inet row; the
    tunnel scope, seeing the AP and its children gone, then removes every child's gretap and
    br-home port. The window between the two transactions (the gretaps briefly in br-home with
    their local address gone) is bounded and leaves br-home's own port untouched, the agent up and
    its tunnel reconcile settled. NM/osw behaviour in that window is the pod's (yocto-53's twin
    scenario 3, real OpenSync); the box has no netdevs."""
    ap, failure = await gre_parent_up(box)
    if failure:
        return failure
    await gre_parent_ready(box)
    await box.transact(
        [
            {
                "op": "insert",
                "table": "DHCP_leased_IP",
                "row": {"hwaddr": GRE_CHILD_MAC, "inet_addr": GRE_CHILD_IP},
            }
        ]
    )
    await box.client_join(GRE_CHILD_MAC, GRE_AP)
    if not await gre_await_tunnel(box, "pgd2_10", present=True):
        return box.result(passed=False, failed="the tunnel was never made")
    await gre_nm_apply(box)  # the gretap up and the create op settled before the withdrawal
    await gre_await_applied(box)
    # the controller withdraws the backhaul BSS: the next M2 set has only the fronthaul
    box.m2_modes = ("configure",)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.controller.send(RENEW, (Tlv(0x01, box.controller.al), Tlv(0x0F, b"\0"), Tlv(0x10, b"\0")))
    again = await provision(box, searches, m1s)
    ap_gone = await gre_await_config_gone(box, GRE_AP)
    ap_inet_gone = await gre_inet(box, GRE_AP) is None
    # the AP scope's transaction is done; wm now tears down the netdev, and the window to the
    # tunnel scope's transaction begins
    window_start = time.monotonic()
    await gre_wm_remove_ap(box, GRE_AP)
    removed = await gre_await_tunnel(box, "pgd2_10", present=False)
    window = time.monotonic() - window_start
    settled = await gre_await_applied(box)  # the removal reconciled (settle lags the DB by a tick)
    row = await gre_inet(box, "pgd2_10")
    bridged = await gre_port_in_home(box, "pgd2_10")
    home_port = await gre_port_in_home(box, "br-home")  # br-home's own port untouched
    status = (box.status() or {}).get("gre_parent") or {}
    return box.result(
        passed=again
        and ap_gone
        and ap_inet_gone
        and bool(removed)
        and row is None
        and not bridged
        and home_port
        and window < 10
        and box.process.poll() is None
        and bool(settled),
        window_s=round(window, 1),
        br_home_own_port=home_port,
        gre_status=status,
    )


# The reference workload's faults, in the box


RUID_24, RUID_50 = "02:00:00:00:01:00", "02:00:00:00:05:00"  # the recorded pod's phy1, phy5
PARENT_AP_50 = "b-ap-50"


def radio_operations(box, band):
    """The operations of the agent's scope for its radio of this band (spec 9)."""
    return ((box.status() or {}).get("radio_operations") or {}).get(band) or []


async def radio_config(box, band):
    """(uuid, row) of the pod's Wifi_Radio_Config of a band."""
    for uuid, row in (await box.rows("Wifi_Radio_Config")).items():
        if row.get("freq_band") == band:
            return uuid, row
    return None, None


async def serve_radios(box, done, *, searches=0, m1s=0, seconds=90):
    """A controller configuring each radio of the pod (spec 9): every Search gets a Response and
    every M1 an M2 set for the radio its AP Radio Basic Capabilities name; the pod's managers
    apply what the agent writes, until done() holds. The RUIDs of the M1s answered, in order,
    or None at the deadline."""
    deadline, last, ruids = time.monotonic() + seconds, None, []
    while time.monotonic() < deadline and box.process.poll() is None:
        found = box.sent(AUTOCONFIG_SEARCH)
        for search in found[searches:]:
            box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
        searches = len(found)
        found = box.sent(AUTOCONFIG_WSC)
        for m1 in found[m1s:]:
            wsc = next(t.value for t in m1.tlvs if t.kind == 0x11)
            ruid = next(t.value[:6] for t in m1.tlvs if t.kind == 0x85)
            ruids.append(ruid.hex(":"))
            await answer_m1(box, ruid, wsc)
        m1s = len(found)
        configs = await box.rows("Wifi_VIF_Config")
        if configs != last:
            last = configs
            await apply_configuration(box.db)
            await attach_vifs(box.db)
        if await done():
            return ruids
        await asyncio.sleep(0.3)
    return None


def all_applied(operations):
    return bool(operations) and all(op.get("state") == "OBSERVED_APPLIED" for op in operations)


async def both_radios_applied(box):
    status = box.status() or {}
    return all_applied(status.get("operations") or []) and all_applied(radio_operations(box, "5G"))


def operational_radios(topology):
    """{RUID: [BSSID, ...]} of a Topology Response's AP Operational BSS TLV."""
    value = next((t.value for t in topology.tlvs if t.kind == 0x83), b"")
    radios, at = {}, 1
    for _ in range(value[0] if value else 0):
        ruid, count, at = value[at : at + 6].hex(":"), value[at + 6], at + 7
        radios[ruid] = []
        for _ in range(count):
            radios[ruid].append(value[at : at + 6].hex(":"))
            at += 7 + value[at + 6]
    return radios


async def second_radio_parent(box):
    """Spec 9: a GRE parent whose parent AP is on its 5 GHz radio (the Pis' adapter B). The agent
    sends an M1 for each radio, the 2.4 GHz one first; the 2.4 GHz radio's M2 set configures the
    fronthaul (its backhaul BSS has no slot there and is left out), the 5 GHz radio's set makes
    b-ap-50 the parent AP on the 5 GHz radio, through that radio's own scope, broadcasting its
    SSID (spec 8.6, the gre-5g profiles). The pod's station on that radio and the radio's channel
    are left alone; the Topology Response reports both radios, b-ap-50 under the 5 GHz one."""
    before_radio = (await radio_config(box, "5G"))[1]
    before_station = (await box.vif("Wifi_VIF_Config", "bhaul-sta-50"))[1]
    ruids = await serve_radios(box, lambda: both_radios_applied(box))
    if ruids is None:
        return box.result(passed=False, failed="the two radios were not both configured")
    radio_uuid, radio = await radio_config(box, "5G")
    ap_uuid, ap = await box.vif("Wifi_VIF_Config", PARENT_AP_50)
    _, ap_state = await box.vif("Wifi_VIF_State", PARENT_AP_50)
    _, fronthaul = await box.vif("Wifi_VIF_Config", COLD_VIF)
    on_24 = uuids((await radio_config(box, "2.4G"))[1].get("vif_configs"))
    inet = await gre_inet(box, PARENT_AP_50)
    station = (await box.vif("Wifi_VIF_Config", "bhaul-sta-50"))[1]
    status = box.status() or {}
    topology = await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)
    reported = operational_radios(topology) if topology else {}
    # each M1's WSC RF Bands: 1 for the 2.4 GHz radio's, 2 for the 5 GHz radio's
    rf_bands = [
        next(
            a.value[0]
            for a in decode_attributes(next(t.value for t in m1.tlvs if t.kind == 0x11))
            if a.kind == 0x103C
        )
        for m1 in box.sent(AUTOCONFIG_WSC)
    ]
    bssid = (ap_state or {}).get("mac")
    unchanged = {k: v for k, v in before_radio.items() if k != "vif_configs"} == {
        k: v for k, v in radio.items() if k != "vif_configs"
    }
    passed = (
        ruids[:2] == [RUID_24, RUID_50]
        and rf_bands[:2] == [1, 2]
        and ap is not None
        and ap_uuid in uuids(radio.get("vif_configs"))
        and ap_uuid not in on_24
        and (ap.get("ssid"), ap.get("multi_ap"), ap.get("bridge"), ap.get("ssid_broadcast"))
        == ("EMOSA-WSC-backhaul", "none", "", "enabled")
        and (inet or {}).get("inet_addr") == "169.254.2.1"
        and fronthaul.get("ssid") == REGISTRAR_SSID
        and unchanged
        and station == before_station
        and [op.get("bsses") for op in radio_operations(box, "5G")] == [["EMOSA-WSC-backhaul"]]
        and (status.get("gre_parent") or {}).get("ap") == PARENT_AP_50
        and reported.get(RUID_50) == [bssid]
        and RUID_24 in reported
    )
    return box.result(
        passed=passed,
        m1_ruids=ruids,
        m1_rf_bands=rf_bands,
        parent_ap={k: (ap or {}).get(k) for k in ("ssid", "multi_ap", "bridge")},
        radio_operations=status.get("radio_operations"),
        reported=reported,
        radio_unchanged=unchanged,
        station_unchanged=station == before_station,
    )


async def second_radio_no_power(box):
    """Spec 9, found on rdk-1010 (12 October): OpenSync leaves a radio's Wifi_Radio_State.tx_power
    unset when the radio's VIFs disagree (its station at 20 dBm, b-ap-50 at 30). The agent still
    answers the controller's channel procedures: a Channel Preference Query at once, from the
    capabilities, and a Channel Selection Request naming that radio, whose Operating Channel
    Report gives the radio's advertised maximum EIRP as a bound. Refused, RDK re-sent its query
    every second and renewed the agent every minute."""
    if await serve_radios(box, lambda: both_radios_applied(box)) is None:
        return box.result(passed=False, failed="the two radios were not both configured")
    uuid = next(
        u for u, r in (await box.rows("Wifi_Radio_State")).items() if r.get("freq_band") == "5G"
    )
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_Radio_State",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {"tx_power": ["set", []]},
            }
        ]
    )
    await asyncio.sleep(2)  # the agent's refresh takes the pod's rows
    refused = (
        ((box.status() or {}).get("session") or {}).get("counts", {}).get("rejected_NOT_READY", 0)
    )
    queried = len(box.sent(0x8005))
    preference = await box.ask(0x8004, (), 0x8005, seconds=5)
    reported = len(box.sent(0x8008))
    radio_b = bytes.fromhex(RUID_50.replace(":", ""))
    selection = await box.ask(0x8006, (Tlv(0x8B, radio_b + b"\x00"),), 0x8007, seconds=5)
    report = await next_message(box, 0x8008, reported, seconds=10)
    responses = [t.value for t in selection.tlvs if t.kind == 0x8E] if selection else []
    operating = [t.value for t in report.tlvs if t.kind == 0x8F] if report else []
    radio_b_report = next((v for v in operating if v[:6] == radio_b), None)

    def bound_count():
        """operating_power_bound_reported: the reference counts its channel procedures apart,
        the C agent with the session's; the status file follows within a second or two."""
        session = (box.status() or {}).get("session") or {}
        channels = (session.get("channels") or {}).get("counts") or {}
        key = "operating_power_bound_reported"
        return (session.get("counts") or {}).get(key) or channels.get(key)

    bound = await box.until(bound_count, seconds=10)
    status = box.status() or {}
    counts = (status.get("session") or {}).get("counts") or {}
    return box.result(
        passed=preference is not None
        and bool(bound)
        and len(box.sent(0x8005)) - queried == 1
        and [(v[:6], v[6]) for v in responses] == [(radio_b, 0)]
        and radio_b_report is not None
        and (radio_b_report[7], radio_b_report[8], radio_b_report[9]) == (115, 44, 23)
        and counts.get("rejected_NOT_READY", 0) == refused,
        preference_answered=preference is not None,
        selection_codes=[v[6] for v in responses],
        radio_b_operating=radio_b_report.hex() if radio_b_report else None,
        not_ready_refusals=counts.get("rejected_NOT_READY", 0) - refused,
        bound_counted=bound,
    )


async def second_radio_child_credentials(box):
    """Spec 8.6 and 9, found by the rdk-1010 proof (12 October): a GRE child on the Pis' shape
    (its station bhaul-sta-50 on its 5 GHz radio, its uplink credentials from the controller's M2
    sets). Its 2.4 GHz radio maps no backhaul BSS, so the backhaul credentials are only in the
    5 GHz radio's own set. Once both radios' sets are applied the uplink has them and waits only
    for an upstream (a Backhaul Steering Request), not for credentials: before the fix the agent
    looked in the fronthaul's set alone and a steer onto a parent's b-ap-50 stalled there."""
    if await serve_radios(box, lambda: both_radios_applied(box)) is None:
        return box.result(passed=False, failed="the two radios were not both configured")
    radio_set = [op.get("bsses") for op in radio_operations(box, "5G")]
    fronthaul_set = [op.get("ssid") for op in (box.status() or {}).get("operations") or []]
    waiting = await box.until(
        lambda: (uplink_status(box).get("waiting") or "").startswith("no upstream") or None,
        seconds=15,
    )
    status = uplink_status(box)
    return box.result(
        passed=bool(waiting) and radio_set and radio_set[-1] == ["EMOSA-WSC-backhaul"],
        radio_set=radio_set,
        fronthaul_set=fronthaul_set,
        waiting=status.get("waiting"),
        station=status.get("station"),
    )


async def second_radio_withdrawn(box):
    """Spec 9 / 8.6 withdrawal by band: after a Renew the controller's 5 GHz set has no backhaul
    BSS (the operator dropped 5 GHz from the backhaul's bands). The 5 GHz radio's scope removes
    b-ap-50 and its underlay Inet row; the 2.4 GHz radio's configuration is not touched (its
    unchanged M2 set is a no-op), nor the pod's station on the 5 GHz radio."""
    if await serve_radios(box, lambda: both_radios_applied(box)) is None:
        return box.result(passed=False, failed="the two radios were not both configured")
    _, fronthaul = await box.vif("Wifi_VIF_Config", COLD_VIF)
    station = (await box.vif("Wifi_VIF_Config", "bhaul-sta-50"))[1]
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.radio_m2_modes[RUID_50] = ("configure",)
    box.controller.send(RENEW, (Tlv(0x01, box.controller.al), Tlv(0x0F, b"\0"), Tlv(0x10, b"\0")))

    async def withdrawn():
        operations = radio_operations(box, "5G")
        gone = (await box.vif("Wifi_VIF_Config", PARENT_AP_50))[1] is None
        return gone and len(operations) == 2 and all_applied(operations)

    ruids = await serve_radios(box, withdrawn, searches=searches, m1s=m1s)
    if ruids is None:
        return box.result(passed=False, failed="b-ap-50 was not withdrawn")
    _, after = await box.vif("Wifi_VIF_Config", COLD_VIF)
    operations = radio_operations(box, "5G")
    return box.result(
        passed=ruids[:2] == [RUID_24, RUID_50]
        and await gre_inet(box, PARENT_AP_50) is None
        and after == fronthaul
        and (await box.vif("Wifi_VIF_Config", "bhaul-sta-50"))[1] == station
        and operations[-1].get("bsses") == []
        and all_applied((box.status() or {}).get("operations") or []),
        m1_ruids=ruids,
        radio_operations=operations,
        fronthaul_unchanged=after == fronthaul,
    )


def advertised_classes(m1):
    """{operating class: its non-operable channels} of the radio an M1's AP Radio Basic
    Capabilities TLV names (RUID, max BSS, class count, then class, max EIRP, count, channels)."""
    value = next(t.value for t in m1.tlvs if t.kind == 0x85)
    classes, at = {}, 8
    for _ in range(value[7]):
        count = value[at + 2]
        classes[value[at]] = list(value[at + 3 : at + 3 + count])
        at += 3 + count
    return classes


def recovery_starts(box):
    """How many sessions the agent has started (its recovery loop's attempts)."""
    session = (box.status() or {}).get("session") or {}
    return (session.get("recovery") or {}).get("attempts_started", 0)


def channel_reports(box, after):
    """(RUID, class, channel) of each Operating Channel Report TLV beyond the first ``after``
    reports."""
    out = []
    for message in box.sent(0x8008)[after:]:
        for t in message.tlvs:
            if t.kind == 0x8F and len(t.value) >= 10:
                out.append((t.value[:6].hex(":"), t.value[7], t.value[8]))
    return out


def preference_reports(box, after):
    """(RUID, {class: channels at preference 0}) of each Channel Preference TLV in the Channel
    Preference Reports beyond the first ``after`` (an answer to a query names none)."""
    out = []
    for message in box.sent(0x8005)[after:]:
        for t in message.tlvs:
            if t.kind != 0x8B or len(t.value) < 7:
                continue
            body, groups = t.value[7:], {}
            for _ in range(t.value[6]):
                opclass, n = body[0], body[1]
                if body[2 + n] >> 4 == 0:
                    groups[opclass] = list(body[2 : 2 + n])
                body = body[3 + n :]
            out.append((t.value[:6].hex(":"), groups))
    return out


# radio B on 149: every channel of its band's classes but 149 at preference 0 (as its M1 would
# advertise them non-operable)
RADIO_B_ON_149 = {
    115: [36, 40, 44, 48],
    124: [153, 157, 161],
    125: [153, 157, 161, 165, 169, 173, 177],
}


async def radio_b_moves(box, *, cut=0):
    """Spec 9's channel rule in V54's shape (radio B on UNII-3 channel 149), kept (spec 2.5). With
    both radios configured, the pod's 5 GHz station roams to a parent on 149 and the radio, b-ap-50
    on it, moves from 44 with it: the pod's doing, its State channel-less for a moment, then 149.
    With ``cut``, the pod's link to its agent is cut for that many seconds around it (the station
    lost its parent: OpenSync's CM opens the link again). The agent keeps its session: no Search
    or M1; it reports radio B's new operable channel in a Channel Preference Report (every other
    channel of its classes at preference 0) and its Operating Channel Report on 149, and after a
    cut a Topology Notification. Nothing is written: b-ap-50, the fronthaul and the radio's Config
    are left as they were."""
    if await serve_radios(box, lambda: both_radios_applied(box)) is None:
        return box.result(passed=False, failed="the two radios were not both configured")
    ap = (await box.vif("Wifi_VIF_Config", PARENT_AP_50))[1]
    fronthaul = (await box.vif("Wifi_VIF_Config", COLD_VIF))[1]
    radio = (await radio_config(box, "5G"))[1]
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    before = len((box.status() or {}).get("operations") or []), len(radio_operations(box, "5G"))
    starts = recovery_starts(box)
    continued, notified = kept_reconnections(box), topology_notifications(box)
    channels, preferences = len(box.sent(0x8008)), len(box.sent(0x8005))
    state = next(
        u for u, r in (await box.rows("Wifi_Radio_State")).items() if r.get("freq_band") == "5G"
    )
    if cut:
        await dial(box.db, f"tcp:127.0.0.1:{AGENT_PORT}", connect=False)
    for channel in (["set", []], 149):  # between two channels, then on the new parent's
        await box.transact(
            [
                {
                    "op": "update",
                    "table": "Wifi_Radio_State",
                    "where": [["_uuid", "==", ["uuid", state]]],
                    "row": {"channel": channel},
                }
            ]
        )
        await asyncio.sleep(2)
    if cut:
        await asyncio.sleep(max(0, cut - 4))
        await dial(box.db, f"tcp:127.0.0.1:{AGENT_PORT}")

    async def reported():
        return any(
            r == RUID_50 and ch == 149 for r, _, ch in channel_reports(box, channels)
        ) and any(r == RUID_50 for r, _ in preference_reports(box, preferences))

    done = await until_async(reported, seconds=60)
    await asyncio.sleep(10)  # no session follows
    status = box.status() or {}
    preferred = next((g for r, g in preference_reports(box, preferences) if r == RUID_50), {})
    unchanged = {
        "b-ap-50": (await box.vif("Wifi_VIF_Config", PARENT_AP_50))[1] == ap,
        "fronthaul": (await box.vif("Wifi_VIF_Config", COLD_VIF))[1] == fronthaul,
        "radio": (await radio_config(box, "5G"))[1] == radio,
    }
    sessions = recovery_starts(box) - starts
    radios = (status.get("pod") or {}).get("radios") or []
    reported_channels = {r.get("band"): r.get("channel") for r in radios}
    new_operations = (
        len(status.get("operations") or []) - before[0],
        len(radio_operations(box, "5G")) - before[1],
    )
    after_cut = not cut or (
        kept_reconnections(box) > continued and topology_notifications(box) > notified
    )
    return box.result(
        passed=bool(done)
        and sessions == 0
        and len(box.sent(AUTOCONFIG_SEARCH)) == searches
        and len(box.sent(AUTOCONFIG_WSC)) == m1s
        and preferred == RADIO_B_ON_149
        and all(unchanged.values())
        and new_operations == (0, 0)
        and reported_channels.get("5G") == 149
        and after_cut,
        reported=bool(done),
        sessions=sessions,
        searches=len(box.sent(AUTOCONFIG_SEARCH)) - searches,
        m1s=len(box.sent(AUTOCONFIG_WSC)) - m1s,
        preferences={str(k): v for k, v in preferred.items()},
        operating_channel_reports=channel_reports(box, channels),
        unchanged=unchanged,
        new_operations=list(new_operations),
        reported_channels=reported_channels,
        kept_after_cut=after_cut,
    )


async def second_radio_moves_channel(box):
    """Radio B's channel follows the pod's station on the same source (the link stays up): the
    session kept, radio B's new operable channel reported (radio_b_moves)."""
    return await radio_b_moves(box)


async def reconnect_channel_follows(box):
    """A backhaul move whose station lost its parent (the pod's link cut for 15 s around it), the
    AP on radio B following the station to 149: the session kept across the reconnection, radio
    B's new operable channel reported and a Topology Notification (radio_b_moves)."""
    return await radio_b_moves(box, cut=15)


async def renew_on_bootstrap(box):
    """V54 regression guard (a gateway's controller restart, 12 October): a GRE child of the Pis'
    shape on its bootstrap, both radios configured. The controller restarts: a Renew, and the
    pod's station dropped by the router for a moment, so the station has no parent and its 5 GHz
    radio, whose channel follows the station, shows none, then the same one again. The agent
    onboards once, for the Renew (the held channel covers the moment, spec 9): one session, both
    radios' unchanged M2 sets observed applied without writing, and none of the pod's station,
    credential or Inet rows touched."""
    if await serve_radios(box, lambda: both_radios_applied(box)) is None:
        return box.result(passed=False, failed="the two radios were not both configured")
    station = (await box.vif("Wifi_VIF_Config", "bhaul-sta-50"))[1]
    credentials = await box.rows("Wifi_Credential_Config")
    inet = await box.rows("Wifi_Inet_Config")
    starts = recovery_starts(box)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    before = len((box.status() or {}).get("operations") or []), len(radio_operations(box, "5G"))
    box.controller.send(RENEW, (Tlv(0x01, box.controller.al), Tlv(0x0F, b"\0"), Tlv(0x10, b"\0")))
    radios = (await box.rows("Wifi_Radio_State")).items()
    radio, radio_row = next((u, r) for u, r in radios if r.get("freq_band") == "5G")
    vif, vif_row = await box.vif("Wifi_VIF_State", "bhaul-sta-50")
    flap = [  # dropped by the router, then back on the same parent and channel
        ({"channel": ["set", []]}, {"parent": ["set", []]}),
        ({"channel": radio_row.get("channel")}, {"parent": vif_row.get("parent") or ["set", []]}),
    ]
    for radio_change, vif_change in flap:
        await box.transact(
            [
                {
                    "op": "update",
                    "table": "Wifi_Radio_State",
                    "where": [["_uuid", "==", ["uuid", radio]]],
                    "row": radio_change,
                },
                {
                    "op": "update",
                    "table": "Wifi_VIF_State",
                    "where": [["_uuid", "==", ["uuid", vif]]],
                    "row": vif_change,
                },
            ]
        )
        await asyncio.sleep(0.5)

    async def taken_again():
        ops, five = (box.status() or {}).get("operations") or [], radio_operations(box, "5G")
        more = len(ops) > before[0] and len(five) > before[1]
        return more and all_applied(ops) and all_applied(five)

    ruids = await serve_radios(box, taken_again, searches=searches, m1s=m1s)
    if ruids is None:
        return box.result(passed=False, failed="the Renew's session did not configure both radios")
    await asyncio.sleep(10)  # no further session follows
    status = box.status() or {}
    noop = [
        bool((ops[-1].get("application_evidence") or {}).get("observed_noop"))
        for ops in (status.get("operations") or [{}], radio_operations(box, "5G") or [{}])
    ]
    unchanged = {
        "station": (await box.vif("Wifi_VIF_Config", "bhaul-sta-50"))[1] == station,
        "credentials": await box.rows("Wifi_Credential_Config") == credentials,
        "inet": await box.rows("Wifi_Inet_Config") == inet,
    }
    sessions = recovery_starts(box) - starts
    return box.result(
        passed=sessions == 1
        and list(dict.fromkeys(ruids)) == [RUID_24, RUID_50]
        and all(noop)
        and all(unchanged.values()),
        sessions=sessions,
        m1_ruids=ruids,
        observed_noop=noop,
        unchanged=unchanged,
        uplink_waiting=uplink_status(box).get("waiting"),
    )


def advertised_max_eirp(m1):
    """(RUID, [max EIRP of each class]) of the radio an M1's AP Radio Basic Capabilities name."""
    value = next(t.value for t in m1.tlvs if t.kind == 0x85)
    eirps, at = [], 8
    for _ in range(value[7]):
        eirps.append(value[at + 1])
        at += 3 + value[at + 2]
    return value[:6].hex(":"), eirps


CONFIG_TABLES = (
    "Wifi_VIF_Config",
    "Wifi_Inet_Config",
    "Wifi_Credential_Config",
    "Wifi_Radio_Config",
)


async def set_radio_power(box, power):
    """Every radio's Wifi_Radio_State.tx_power in one transaction, as the pod's managers fill it."""
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_Radio_State",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {"tx_power": power},
            }
            for uuid in await box.rows("Wifi_Radio_State")
        ]
    )


async def power_rises(box):
    """Spec 2.5 (the Pis on their new mt76 modules, 12 October): a pod whose source began
    before it reported its radios' power (the agent advertised its own or 20 dBm) reports more,
    30 dBm, after onboarding. Each radio's capabilities are taken again with the higher maximum
    EIRP: exactly one fresh session, its M1s advertising 30 dBm, its unchanged M2 sets observed
    applied without writing, no Config row of the pod changed and its uplink untouched. The
    channel procedures are then answered (before, refused while the power exceeded the advertised
    maximum: RDK renewed the agent every 40 s). A fall to 23 dBm changes nothing."""
    if await serve_radios(box, lambda: both_radios_applied(box)) is None:
        return box.result(passed=False, failed="the two radios were not both configured")
    before_m1s = {ruid: eirps for ruid, eirps in map(advertised_max_eirp, box.sent(AUTOCONFIG_WSC))}
    config = {t: await box.rows(t) for t in CONFIG_TABLES}
    uplink = {k: uplink_status(box).get(k) for k in ("waiting", "uplink", "in_use", "parent")}
    starts = recovery_starts(box)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    before = len((box.status() or {}).get("operations") or []), len(radio_operations(box, "5G"))
    await set_radio_power(box, 30)

    async def taken_again():
        ops, five = (box.status() or {}).get("operations") or [], radio_operations(box, "5G")
        more = len(ops) > before[0] and len(five) > before[1]
        return more and all_applied(ops) and all_applied(five)

    ruids = await serve_radios(box, taken_again, searches=searches, m1s=m1s)
    if ruids is None:
        return box.result(
            passed=False, failed="no fresh session for the risen power", advertised=before_m1s
        )
    after_m1s = {
        ruid: eirps for ruid, eirps in map(advertised_max_eirp, box.sent(AUTOCONFIG_WSC)[m1s:])
    }
    radio_a = bytes.fromhex(RUID_24.replace(":", ""))
    reported = len(box.sent(0x8008))
    selection = await box.ask(0x8006, (Tlv(0x8B, radio_a + b"\x00"),), 0x8007, seconds=5)
    report = await next_message(box, 0x8008, reported, seconds=10)
    responses = [t.value for t in selection.tlvs if t.kind == 0x8E] if selection else []
    operating = [t.value for t in report.tlvs if t.kind == 0x8F] if report else []
    radio_a_report = next((v for v in operating if v[:6] == radio_a), None)
    sessions = recovery_starts(box) - starts
    await set_radio_power(box, 23)  # a fall: nothing
    await asyncio.sleep(10)
    status = box.status() or {}
    noop = [
        bool((ops[-1].get("application_evidence") or {}).get("observed_noop"))
        for ops in (status.get("operations") or [{}], radio_operations(box, "5G") or [{}])
    ]
    unchanged = {t: await box.rows(t) == rows for t, rows in config.items()}
    uplink_after = {k: uplink_status(box).get(k) for k in uplink}
    return box.result(
        passed=sessions == 1
        and recovery_starts(box) - starts == 1
        and list(dict.fromkeys(ruids)) == [RUID_24, RUID_50]
        and all(set(e) == {30} for e in after_m1s.values())
        and set(after_m1s) == {RUID_24, RUID_50}
        and [(v[:6], v[6]) for v in responses] == [(radio_a, 0)]
        and radio_a_report is not None
        and radio_a_report[9] == 30
        and all(noop)
        and all(unchanged.values())
        and uplink_after == uplink,
        advertised_before=before_m1s,
        advertised_after=after_m1s,
        sessions_on_the_rise=sessions,
        sessions_after_the_fall=recovery_starts(box) - starts - sessions,
        selection_codes=[v[6] for v in responses],
        radio_a_operating=radio_a_report.hex() if radio_a_report else None,
        observed_noop=noop,
        config_unchanged=unchanged,
        uplink_unchanged=uplink_after == uplink,
    )


async def adapter_restart(box):
    """The agent process killed and started again (its journal kept): it takes the pod back,
    onboards again and finds the configuration already running, without writing (spec 5)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    box.process.kill()
    box.process.wait(10)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.restart_agent()
    again = await provision(box, searches, m1s)
    operations = (box.status() or {}).get("operations") or []
    last = operations[-1] if operations else {}
    noop = bool((last.get("application_evidence") or {}).get("observed_noop"))
    return box.result(
        passed=again and last.get("state") == "OBSERVED_APPLIED" and noop,
        provisioning_again=again,
        operations=[op.get("state") for op in operations],
        observed_without_writing=noop,
    )


async def cut_link(box, seconds):
    """The pod's management connection to the agent cut for this long (the pod's database drops
    the remote, as OpenSync's CM does for a backhaul move), then dialed again: a new database
    generation of the same database. The session states the agent showed meanwhile."""
    await dial(box.db, f"tcp:127.0.0.1:{AGENT_PORT}", connect=False)
    states, until = set(), time.monotonic() + seconds
    while time.monotonic() < until:
        states.add(((box.status() or {}).get("session") or {}).get("state"))
        await asyncio.sleep(0.5)
    await dial(box.db, f"tcp:127.0.0.1:{AGENT_PORT}")
    return sorted(s for s in states if s)


def topology_notifications(box):
    return len(box.sent(TOPOLOGY_NOTIFICATION))


async def transport_cut(box):
    """The pod's management connection to the agent cut for 20 s, as for a backhaul move (spec
    2.5): the provisioned session is held meanwhile and kept when the pod dials again (the same
    pod, its database and capabilities unchanged): no Search or M1, a Topology Notification,
    the configuration found running without a write."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued, notified = kept_reconnections(box), topology_notifications(box)
    writes = len((box.status() or {}).get("operations") or [])
    states = await cut_link(box, 20)
    mode = await back(box, searches, m1s, continued)
    notification = await box.until(lambda: topology_notifications(box) > notified, seconds=10)
    operations = (box.status() or {}).get("operations") or []
    counts = ((box.status() or {}).get("session") or {}).get("counts") or {}
    return box.result(
        passed=mode == "kept"
        and states == ["provisioning"]
        and bool(notification)
        and len(operations) == writes
        and counts.get("reconnection_kept", 0) >= 1,
        session=mode,
        states_during_cut=states,
        topology_notification=bool(notification),
        new_operations=len(operations) - writes,
        reconnection_kept=counts.get("reconnection_kept"),
    )


async def transport_cut_long(box):
    """The pod's management connection cut for longer than the reconnection window (spec 2.5):
    the held session loses its source once the window has passed, and when the pod dials again
    it onboards anew (Search and M1), its configuration found running."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued, passed = kept_reconnections(box), fresh_reconnections(box, "window_passed")
    states = await cut_link(box, RECONNECT_WINDOW + 5)
    mode = await back(box, searches, m1s, continued)
    why = fresh_reconnections(box, "window_passed") > passed
    return box.result(
        passed=mode == "fresh" and "provisioning" in states and why,
        session=mode,
        states_during_cut=states,
        fresh_as_window_passed=why,
    )


async def pod_vanished(box):
    """spec 3.1, the echo probe (14 October: the gateway's agent kept a pod rebooted behind
    a switch as its source for hours): the pod's database frozen (SIGSTOP: no FIN, nothing read,
    nothing answered), as a pod that loses power or reboots without closing its connection. The
    agent probes the idle connection after 5 s and drops it 5 s later: its report source lost
    (report_source_available false) within 25 s, not held while the connection looks open."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    available = bool((box.status() or {}).get("report_source_available"))
    pid = box.db.process.pid
    os.kill(pid, signal.SIGSTOP)
    frozen = time.monotonic()
    try:
        lost = await box.until(
            lambda: (box.status() or {}).get("report_source_available") is False, seconds=40
        )
        lost_after = time.monotonic() - frozen
    finally:
        os.kill(pid, signal.SIGCONT)
    return box.result(
        passed=available and bool(lost) and lost_after < 25,
        available_before=available,
        source_lost=bool(lost),
        lost_after_s=round(lost_after, 1),
    )


def station_parent(topology):
    """The BSSID the pod's backhaul station is on, as a Topology Response's 1905 Device
    Information TLV names it: the 802.11 interface with the non-AP STA role."""
    value = next((t.value for t in topology.tlvs if t.kind == 0x03), b"") if topology else b""
    if len(value) < 7:
        return None
    body = value[7:]
    for _ in range(value[6]):
        if len(body) < 9:
            return None
        size = body[8]
        info = body[9 : 9 + size]
        if size >= 7 and info[6] & 0xF0 == 0x40:  # non-AP STA
            return info[:6].hex(":")
        body = body[9 + size :]
    return None


async def reconnect_moved_uplink(box):
    """A Backhaul Steering move onto another parent, the pod's link to its agent cut for 15 s
    around it as OpenSync's CM does when the station leaves its parent (spec 2.5): the session
    kept, no Search or M1. The agent answers the move once the station is on its target, sends a
    Topology Notification, and its next Topology Response names the new parent on the backhaul
    station's interface."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    await box.until(lambda: uplink_status(box).get("bootstrap"), seconds=30)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued, notified = kept_reconnections(box), topology_notifications(box)
    answered = len(box.sent(0x801A))
    onto_mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, TARGET_PARENT),)
    )
    await box.until(lambda: box.controller.reply(ACK, onto_mid), seconds=5)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not await pinned(box, TARGET_PARENT):
        await asyncio.sleep(0.3)
    # the station leaves its parent: CM drops the link, the station joins the target, and CM
    # opens the link again
    await dial(box.db, f"tcp:127.0.0.1:{AGENT_PORT}", connect=False)
    await four_address(box, TARGET_PARENT)
    await asyncio.sleep(15)
    await dial(box.db, f"tcp:127.0.0.1:{AGENT_PORT}")
    mode = await back(box, searches, m1s, continued)
    onto = await next_message(box, 0x801A, answered, seconds=60)
    onto_ok = onto is not None and steering_answer(onto) == (BACKHAUL_STATION, TARGET_PARENT, 0)
    notification = await box.until(lambda: topology_notifications(box) > notified, seconds=10)
    topology = await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)
    parent = station_parent(topology)
    return box.result(
        passed=mode == "kept" and onto_ok and bool(notification) and parent == TARGET_PARENT,
        session=mode,
        onto=steering_answer(onto),
        topology_notification=bool(notification),
        station_parent=parent,
    )


async def reconnect_config_lost(box):
    """spec 2.5's safety: a kept reconnection whose pod no longer has the agent's configuration,
    though the same start of its OpenSync (its radio rows kept: a path not seen yet, here the AP
    row's SSID changed while the link was cut). The agent ends the session at once, counted in
    the status's reconnections as config_lost, rather than after the unserved rule's 60 s. Spec
    5's ownership rule stands: a configuration changed under the same start is another
    manager's, so the fresh session's M2 operation is refused (OWNERSHIP_CONFLICT), not forced."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued = kept_reconnections(box)
    lost = ((box.status() or {}).get("reconnections") or {}).get("config_lost", 0)
    await dial(box.db, f"tcp:127.0.0.1:{AGENT_PORT}", connect=False)
    rows = await box.rows("Wifi_VIF_Config")
    ours = next((u for u, r in rows.items() if r.get("ssid") == REGISTRAR_SSID), None)
    if ours is None:
        return box.result(passed=False, failed="the agent's AP row was not found")
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_VIF_Config",
                "where": [["_uuid", "==", ["uuid", ours]]],
                "row": {"ssid": "changed-while-away"},
            }
        ]
    )
    await apply_configuration(box.db)  # the pod runs it
    await asyncio.sleep(10)
    operations = len(operation_states(box))
    back_at = time.monotonic()
    await dial(box.db, f"tcp:127.0.0.1:{AGENT_PORT}")
    # as a controller does, until the fresh session's M2 has its operation
    deadline, answered_searches, answered_m1s, fresh_after = (
        time.monotonic() + 50,
        searches,
        m1s,
        None,
    )
    while time.monotonic() < deadline and box.process.poll() is None:
        found = box.sent(AUTOCONFIG_SEARCH)
        if fresh_after is None and len(found) > searches:
            fresh_after = time.monotonic() - back_at
        for search in found[answered_searches:]:
            box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
        answered_searches = len(found)
        found = box.sent(AUTOCONFIG_WSC)
        for m1 in found[answered_m1s:]:
            wsc = next(t.value for t in m1.tlvs if t.kind == 0x11)
            ruid = next(t.value[:6] for t in m1.tlvs if t.kind == 0x85)
            await answer_m1(box, ruid, wsc)
        answered_m1s = len(found)
        if len(operation_states(box)) > operations:
            break
        await asyncio.sleep(0.3)
    states = operation_states(box)
    counted = ((box.status() or {}).get("reconnections") or {}).get("config_lost", 0) > lost
    return box.result(
        passed=counted
        and fresh_after is not None
        and fresh_after < 15
        and states[operations:] == [("REJECTED", "OWNERSHIP_CONFLICT")]
        and kept_reconnections(box) > continued,
        reconnection_kept_first=kept_reconnections(box) > continued,
        config_lost_counted=counted,
        fresh_session_after_s=None if fresh_after is None else round(fresh_after, 1),
        operations=[list(s) for s in states[-3:]],
    )


async def fronthaul_channel_moves(box):
    """spec 2.5, the Pis' 2.4 GHz fallback (their fronthaul AP on the radio the station shares,
    which follows the station's channel): the pod's fronthaul radio moves from 6 to 11 on the
    same source. Another pod source (fronthaul_moved): a fresh session whose M1 advertises 11
    the radio's only operable channel, provisioning again; the source is never refused (the
    fronthaul's move kept with its reports comes with spec 9's 2.4 GHz contract)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued, moved = kept_reconnections(box), fresh_reconnections(box, "fronthaul_moved")
    state = next(
        u for u, r in (await box.rows("Wifi_Radio_State")).items() if r.get("freq_band") == "2.4G"
    )
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_Radio_State",
                "where": [["_uuid", "==", ["uuid", state]]],
                "row": {"channel": 11},
            }
        ]
    )
    mode = await back(box, searches, m1s, continued)
    m1 = next(iter(box.sent(AUTOCONFIG_WSC)[m1s:]), None)
    classes = advertised_classes(m1) if m1 else {}
    counted = fresh_reconnections(box, "fronthaul_moved") > moved
    return box.result(
        passed=mode == "fresh"
        and counted
        and classes.get(81) == [c for c in range(1, 14) if c != 11],
        session=mode,
        fresh_as_fronthaul_moved=counted,
        non_operable_81=classes.get(81),
    )


async def reconnect_not_provisioned(box):
    """The pod's link cut for 15 s while the agent's first M1 waits for its M2 (spec 2.5): a
    session not yet provisioned is not held. It loses its source at once, and when the pod dials
    again the agent onboards anew, a Search and an M1."""
    search = await box.until(lambda: box.controller.latest(AUTOCONFIG_SEARCH))
    if not search:
        return box.result(passed=False, failed="no search")
    box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
    if not await box.until(lambda: box.controller.latest(AUTOCONFIG_WSC), seconds=20):
        return box.result(passed=False, failed="no M1")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    starts = recovery_starts(box)
    states = await cut_link(box, 15)
    mode = await back(box, searches, m1s, kept_reconnections(box))
    return box.result(
        passed=mode == "fresh" and recovery_starts(box) > starts and "provisioning" not in states,
        session=mode,
        states_during_cut=states,
        sessions_started=recovery_starts(box) - starts,
    )


async def write_lost_unapplied(box):
    """spec 5 (finding 22): the pod's link lost while the agent's write commits (the box's
    ReplyCut): the write is in the pod's Config, its reply lost (INDETERMINATE), and the pod's
    State never shows it (its managers do not apply it). Past its deadline (120 s) the
    operation ends TIMED_OUT and no longer blocks the pod: the agent onboards again, the next
    M2 is written, and once the pod's managers apply that one it is observed applied."""
    deadline = time.monotonic() + 330
    searches = m1s = 0
    first, seen, applying = None, {}, False
    while time.monotonic() < deadline and box.process.poll() is None:
        found = box.sent(AUTOCONFIG_SEARCH)
        for search in found[searches:]:
            box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
        searches = len(found)
        found = box.sent(AUTOCONFIG_WSC)
        for m1 in found[m1s:]:
            wsc = next(t.value for t in m1.tlvs if t.kind == 0x11)
            ruid = next(t.value[:6] for t in m1.tlvs if t.kind == 0x85)
            await answer_m1(box, ruid, wsc)
        m1s = len(found)
        for op in (box.status() or {}).get("operations") or []:
            if isinstance(op, dict) and op.get("operation_id"):
                seen[op["operation_id"]] = (op.get("state"), op.get("reason"))
        if first is None:
            first = next((i for i, (s, _) in seen.items() if s == "INDETERMINATE"), None)
        later = {i: s for i, (s, _) in seen.items() if first and i != first}
        if not applying and any(
            s in ("CONFIG_COMMITTED", "OBSERVED_APPLIED") for s in later.values()
        ):
            applying = True
            await apply_configuration(box.db)  # the pod's managers apply the later write
        if "OBSERVED_APPLIED" in later.values():
            break
        await asyncio.sleep(0.5)
    first_state = seen.get(first, (None, None))
    busy = sum(1 for s, r in seen.values() if s == "REJECTED" and r == "BUSY")
    return box.result(
        passed=bool(first)
        and first_state[0] == "TIMED_OUT"
        and "OBSERVED_APPLIED" in [s for i, (s, _) in seen.items() if i != first],
        reply_cut=box.reply_cut.done,
        first_operation=first_state,
        rejected_busy=busy,
        later_applied=applying and "OBSERVED_APPLIED" in later.values(),
    )


def operation_states(box):
    """[(state, reason)] of the agent's operations, in order."""
    return [
        (op.get("state"), op.get("reason"))
        for op in (box.status() or {}).get("operations") or []
        if isinstance(op, dict)
    ]


async def restart_during_write(box):
    """spec 5 (found by the box, 12 October): the pod's OpenSync starts again between the agent's
    write and its observation. The controller's first M2 is written (CONFIG_COMMITTED) but the
    pod's managers have not applied it when the pod's database comes back from its template.
    That write went with its start, so it fails at once (NOT_READY) instead of holding the pod
    until its deadline: the new start's M2 is written, not refused BUSY, and observed applied."""
    before, searches, m1s = 0, 0, 0
    deadline = time.monotonic() + 60
    committed = False
    while time.monotonic() < deadline and box.process.poll() is None and not committed:
        found = box.sent(AUTOCONFIG_SEARCH)
        for search in found[searches:]:
            box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
        searches = len(found)
        found = box.sent(AUTOCONFIG_WSC)
        for m1 in found[m1s:]:  # answered, and the write left unapplied by the pod's managers
            wsc = next(t.value for t in m1.tlvs if t.kind == 0x11)
            ruid = next(t.value[:6] for t in m1.tlvs if t.kind == 0x85)
            await answer_m1(box, ruid, wsc)
        m1s = len(found)
        states = operation_states(box)
        committed = len(states) > before and states[-1][0] == "CONFIG_COMMITTED"
        await asyncio.sleep(0.2)
    if not committed:
        return box.result(passed=False, failed="the first write was not committed")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued = kept_reconnections(box)
    await box.new_pod_database()  # its OpenSync again, before the write was observed
    # its OpenSync started again: a fresh session (spec 2.5), its M2 written on the new start
    mode = await back(box, searches, m1s, continued)
    await box.until(lambda: operation_states(box)[-1:] == [("OBSERVED_APPLIED", None)], seconds=30)
    states = operation_states(box)
    interrupted = states[before] if len(states) > before else None
    return box.result(
        passed=mode == "fresh"
        and interrupted == ("FAILED", "NOT_READY")
        and states[-1] == ("OBSERVED_APPLIED", None)
        and ("REJECTED", "BUSY") not in states[before:],
        session=mode,
        interrupted_write=interrupted,
        operations=states[before:],
    )


REGISTRAR_KEY = "OnlySimulationWscKey2026!"  # deploy/wire/component-registrar.c
REGISTRAR_BACKHAUL_KEY = "OnlySimulationWscBackhaul2026!"
LEGACY_NO_COLUMNS = ("wpa", "wpa_key_mgmt", "wpa_psks", "multi_ap")


def legacy_row_ok(row, security):
    """A legacy pod's VIF row (spec 3.10): its security map exactly as given, and none of the
    6.6 columns (the pod's schema has none: a row with one could not have been written)."""
    return (
        row is not None
        and ovsdb_map(row.get("security")) == security
        and not any(c in row for c in LEGACY_NO_COLUMNS)
    )


async def legacy_onboard(box):
    """spec 3.10: a legacy pod onboarded. Its schema qualifies against OpenSync 2.0's without
    multi_ap; the controller's fronthaul is written on the cold pod as wl1.2 with WPA2-PSK in the
    security map (encryption WPA-PSK, the key, the profile's oftag) and no multi_ap, applied by the
    pod's managers, and reported as the agent's fronthaul BSS."""
    state = await onboarded(box)
    if not state.get("applied"):
        return box.result(passed=False, failed=state.get("failed", "not applied"))
    _, config = await box.vif("Wifi_VIF_Config", LEGACY_FRONTHAUL)
    _, observed = await box.vif("Wifi_VIF_State", LEGACY_FRONTHAUL)
    wanted = {"encryption": "WPA-PSK", "key": REGISTRAR_KEY, "oftag": "home--1"}
    pod = await box.until(lambda: ((box.status() or {}).get("pod") or {}).get("ssid"), seconds=20)
    return box.result(
        passed=legacy_row_ok(config, wanted)
        and legacy_row_ok(observed, wanted)
        and (config or {}).get("bridge") == "br-home"
        and pod == REGISTRAR_SSID
        and not (box.status() or {}).get("pod_refused"),
        fronthaul={k: (config or {}).get(k) for k in ("if_name", "ssid", "bridge", "mode")},
        security_keys=sorted(ovsdb_map((config or {}).get("security"))),
        reported_ssid=pod,
    )


async def legacy_auth_reload(box):
    """spec 3.10: a hooked boot's fronthaul, the seed's wl1.2 with the operator's SSID and key
    under the profile's oftag. Its SSID and key alone would be a fast reload of the pod's
    authenticator (wm's model, auth_reload); the agent's write marks the oftag, a full reload, and
    once wm shows that write the agent restores it, a second full reload. Nothing fast, and the
    row ends as the profile has it."""
    seed = seeded_fronthaul()[0]["row"]
    wanted = {"encryption": "WPA-PSK", "key": REGISTRAR_KEY, "oftag": "home--1"}
    plain = auth_reload(seed, {"ssid": REGISTRAR_SSID, "security": ["map", list(wanted.items())]})
    state = await onboarded(box)
    if not state.get("applied"):
        return box.result(passed=False, failed=state.get("failed", "not applied"))
    _, config = await box.vif("Wifi_VIF_Config", LEGACY_FRONTHAUL)
    _, observed = await box.vif("Wifi_VIF_State", LEGACY_FRONTHAUL)
    reloads = [(k, o) for name, k, o in box.db.applied if name == LEGACY_FRONTHAUL and k]
    restored = (box.status() or {}).get("auth_reloads")
    return box.result(
        passed=plain == "fast"
        and reloads[-2:] == [("full", "home--1" + AUTH_RELOAD), ("full", "home--1")]
        and all(k == "full" for k, _ in reloads)
        and (restored or 0) >= 1
        and legacy_row_ok(config, wanted)
        and legacy_row_ok(observed, wanted)
        and (config or {}).get("ssid") == REGISTRAR_SSID,
        without_marker=plain,
        reloads=reloads,
        auth_reloads=restored,
    )


async def no_auth_reload_6_6(box):
    """spec 3.10: the authenticator's reload is a legacy pod's only: on a 6.6 pod (the Pis'
    generation) the agent's write of the controller's fronthaul carries no oftag marker, and no
    further write follows it (no auth_reloads in its status)."""
    state = await onboarded(box)
    if not state.get("applied"):
        return box.result(passed=False, failed=state.get("failed", "not applied"))
    writes = (box.status() or {}).get("writes")
    await asyncio.sleep(6)  # a restore would come on the agent's refresh cadence
    status = box.status() or {}
    _, config = await box.vif("Wifi_VIF_Config", COLD_VIF)
    marks = [o for _, _, o in box.db.applied if (o or "").endswith(AUTH_RELOAD)]
    return box.result(
        passed=bool(config)
        and not marks
        and AUTH_RELOAD not in json.dumps(config)
        and "auth_reloads" not in status
        and "bridge_ports" not in status
        and status.get("writes") == writes,
        writes=[writes, status.get("writes")],
        marks=marks,
    )


async def home_port(box, name):
    """The AP `name`'s Port and Interface: (in br-home's ports, an Interface of its name)."""
    interfaces = {r.get("name") for r in (await box.rows("Interface")).values()}
    return name in await bridge_members(box, "br-home"), name in interfaces


async def legacy_bridge_port(box):
    """spec 3.10: a hooked boot (the pod's seed has no VIF rows and br-home only its own port).
    The controller's fronthaul made as wl1.2 naming br-home gets its Interface and Port into
    br-home from the agent, and the authenticator's last full reload (the oftag's restore) comes
    after it: the box's authenticator serves wl1.2 (its Port in br-home at that reload)."""
    state = await onboarded(box)
    if not state.get("applied"):
        return box.result(passed=False, failed=state.get("failed", "not applied"))
    port = await home_port(box, LEGACY_FRONTHAUL)
    reloads = [(k, o) for name, k, o in box.db.applied if name == LEGACY_FRONTHAUL and k]
    status = box.status() or {}
    return box.result(
        passed=port == (True, True)
        and box.db.served.get(LEGACY_FRONTHAUL) is True
        and reloads[-1:] == [("full", "home--1")]
        and status.get("bridge_ports") == 1,
        port=port,
        served=box.db.served.get(LEGACY_FRONTHAUL),
        reloads=reloads,
        bridge_ports=status.get("bridge_ports"),
    )


async def legacy_bridge_port_repair(box):
    """spec 3.10: a live pod whose fronthaul EMOSA made before it wrote bridge ports (wl1.2 with
    the controller's SSID and key, naming br-home, no Port): without a restart of the pod the
    agent puts wl1.2 into br-home and marks its oftag, then restores it once wm shows the marker,
    two full reloads with the port there; the AP's SSID and key untouched."""
    state = await onboarded(box)
    if not state.get("applied"):
        return box.result(passed=False, failed=state.get("failed", "not applied"))
    ported, deadline = False, time.monotonic() + 20  # the agent's refresh cadence
    while time.monotonic() < deadline and not ported:
        ported = (await home_port(box, LEGACY_FRONTHAUL))[0]
        if not ported:
            await asyncio.sleep(0.3)
    if ported:
        await apply_configuration(box.db)  # wm takes the marker (and then the restore)
    port = await home_port(box, LEGACY_FRONTHAUL)
    _, config = await box.vif("Wifi_VIF_Config", LEGACY_FRONTHAUL)
    reloads = [(k, o) for name, k, o in box.db.applied if name == LEGACY_FRONTHAUL and k]
    wanted = {"encryption": "WPA-PSK", "key": REGISTRAR_KEY, "oftag": "home--1"}
    status = box.status() or {}
    return box.result(
        passed=port == (True, True)
        and box.db.served.get(LEGACY_FRONTHAUL) is True
        and reloads[-2:] == [("full", "home--1" + AUTH_RELOAD), ("full", "home--1")]
        and legacy_row_ok(config, wanted)
        and (config or {}).get("ssid") == REGISTRAR_SSID
        and status.get("bridge_ports") == 1
        and (status.get("auth_reloads") or 0) >= 1,
        port=port,
        served=box.db.served.get(LEGACY_FRONTHAUL),
        reloads=reloads,
        bridge_ports=status.get("bridge_ports"),
        auth_reloads=status.get("auth_reloads"),
    )


BRIDGE_MODE = {"home_up": True, "wan_nat": False, "pair": True, "vif": [86400, 0], "dhcp": True}


async def bridge_mode_state(box):
    """The pod's bridge mode as the box reads it (spec 3.10): br-home up, br-wan's NAT, the patch
    pair, the fronthaul's group_rekey and ft_psk, and whether a client's DHCP reaches the LAN."""
    inets = {r.get("if_name"): r for r in (await box.rows("Wifi_Inet_Config")).values()}
    home, wan = inets.get("br-home") or {}, inets.get("br-wan") or {}
    inner, outer = await bridge_members(box, "br-home"), await bridge_members(box, "br-wan")
    _, config = await box.vif("Wifi_VIF_Config", LEGACY_FRONTHAUL)
    return {
        "home_up": home.get("enabled") is True and home.get("network") is True,
        "wan_nat": wan.get("NAT"),
        "pair": "patch-h2w" in inner and "patch-w2h" in outer,
        "vif": [(config or {}).get("group_rekey"), (config or {}).get("ft_psk")],
        "dhcp": await dhcp_reaches_lan(box.db),
    }


async def legacy_bridge_mode(box):
    """spec 3.10, the bridge mode: a hooked boot's seed has br-home's interface down (its Inet row
    network false), br-wan's NAT on and no patch pair (vendor_bridges), so the home AP's clients
    reach neither the pod's authenticator (it listens on br-home) nor the router's LAN. The agent
    writes the bridge mode the pod's cloud runs: br-home up, br-wan's NAT off, the patch pair, and
    the fronthaul made with the cloud's group_rekey and ft_psk; the authenticator's last full
    reload (the oftag's restore) comes after it all, so it serves wl1.2, and a client's DHCP
    reaches the LAN through the patch pair."""
    state = await onboarded(box)
    if not state.get("applied"):
        return box.result(passed=False, failed=state.get("failed", "not applied"))
    mode = await bridge_mode_state(box)
    reloads = [(k, o) for name, k, o in box.db.applied if name == LEGACY_FRONTHAUL and k]
    status = box.status() or {}
    return box.result(
        passed=mode == BRIDGE_MODE
        and box.db.served.get(LEGACY_FRONTHAUL) is True
        and reloads[-1:] == [("full", "home--1")]
        and (status.get("bridge_mode_writes") or 0) >= 1,
        bridge_mode=mode,
        served=box.db.served.get(LEGACY_FRONTHAUL),
        reloads=reloads,
        bridge_mode_writes=status.get("bridge_mode_writes"),
    )


async def legacy_bridge_mode_repair(box):
    """spec 3.10, the bridge mode on a live pod: wl1.2 as EMOSA made it before (the controller's
    SSID and key, oftag home--1, naming br-home, no Port, no group_rekey or ft_psk) on a hooked
    boot's bridges (br-home down, br-wan's NAT on, no pair). With no restart of the pod the agent
    puts wl1.2 into br-home and writes the bridge mode, marking the oftag once, then restores it
    once wm shows the marker: two full reloads with all of it there, the client's DHCP through
    the patch pair, the SSID and key untouched."""
    state = await onboarded(box)
    if not state.get("applied"):
        return box.result(passed=False, failed=state.get("failed", "not applied"))
    ready, deadline = False, time.monotonic() + 20  # the agent's refresh cadence
    while time.monotonic() < deadline and not ready:
        ready = (await home_port(box, LEGACY_FRONTHAUL))[0] and await dhcp_reaches_lan(box.db)
        if not ready:
            await asyncio.sleep(0.3)
    if ready:
        await apply_configuration(box.db)  # wm takes the marker (and then the restore)
    mode = await bridge_mode_state(box)
    _, config = await box.vif("Wifi_VIF_Config", LEGACY_FRONTHAUL)
    reloads = [(k, o) for name, k, o in box.db.applied if name == LEGACY_FRONTHAUL and k]
    wanted = {"encryption": "WPA-PSK", "key": REGISTRAR_KEY, "oftag": "home--1"}
    status = box.status() or {}
    return box.result(
        passed=mode == BRIDGE_MODE
        and box.db.served.get(LEGACY_FRONTHAUL) is True
        and reloads[-2:] == [("full", "home--1" + AUTH_RELOAD), ("full", "home--1")]
        and legacy_row_ok(config, wanted)
        and (config or {}).get("ssid") == REGISTRAR_SSID
        and (status.get("bridge_mode_writes") or 0) >= 1,
        bridge_mode=mode,
        served=box.db.served.get(LEGACY_FRONTHAUL),
        reloads=reloads,
        bridge_mode_writes=status.get("bridge_mode_writes"),
        auth_reloads=status.get("auth_reloads"),
    )


async def phy_taps(box):
    """The tap Inet rows on the pod's radios: {if_name: row}."""
    radios = {r.get("if_name") for r in (await box.rows("Wifi_Radio_Config")).values()}
    return {
        r.get("if_name"): r
        for r in (await box.rows("Wifi_Inet_Config")).values()
        if r.get("if_type") == "tap" and r.get("if_name") in radios
    }


async def legacy_phy_netdev(box):
    """spec 3.10, the radios' own netdevs (16 October): by default the agent writes a tap Inet row
    (enabled, network, ip_assign_scheme none, NAT off) for each radio carrying a home AP whose VIFs
    are all EMOSA's and with no VIF or Inet row on its own netdev: wl0 here, the 5 GHz home AP
    wl0.2's radio, whose own netdev a pod converted at boot leaves down; not wl1, the profile's
    station netdev. Its status gives each home AP's radio's port_state as nm reports it
    (the box plays nm's Wifi_Master_State row). Restarted with the fleet's phy_netdev_up false, the
    agent removes its row and only it (wl1.1's Inet row stays)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    wanted = {"if_type": "tap", "enabled": True, "network": True, "ip_assign_scheme": "none"}

    async def written():
        taps = await phy_taps(box)
        return taps if "wl0" in taps else None

    taps = await serve_radios(box, written) is not None and await phy_taps(box)
    shape = set(taps or {}) == {"wl0"} and all(
        {k: r.get(k) for k in wanted} == wanted and r.get("NAT") is not True for r in taps.values()
    )
    # the box as nm: wl0's interface reported, its link running
    row = {"if_name": "wl0", "if_type": "tap", "port_state": "active", "network_state": "up"}
    await db_transact(box.db, [{"op": "insert", "table": "Wifi_Master_State", "row": row}])

    async def reported():
        phy = (box.status() or {}).get("phy_netdev_up") or {}
        return phy.get("port_state") == {"wl0": "active", "wl1": None} and phy

    on = await serve_radios(box, reported) is not None and (box.status() or {}).get("phy_netdev_up")
    box.process.kill()
    box.process.wait(10)
    config_path = box.directory / "agent.json"
    config = json.loads(config_path.read_text())
    config["phy_netdev_up"] = False
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.restart_agent()

    async def removed():  # the restarted agent's own status (the old one's file stays a moment)
        phy = (box.status() or {}).get("phy_netdev_up") or {}
        return phy.get("on") is False and phy.get("writes", 0) >= 1

    await serve_radios(box, removed, searches=searches, m1s=m1s)
    after = await phy_taps(box)
    others = sorted(r.get("if_name") for r in (await box.rows("Wifi_Inet_Config")).values())
    return box.result(
        passed=shape
        and (on or {}).get("on") is True
        and (on or {}).get("writes", 0) >= 1
        and not after
        and "wl1.1" in others,
        shape=shape,
        taps=sorted(taps or {}),
        phy_on=on,
        phy_off=(box.status() or {}).get("phy_netdev_up"),
        left=others,
    )


async def legacy_phy_netdev_foreign(box):
    """spec 3.10, the radios' own netdevs: a radio that also carries an AP EMOSA did not make (the
    operator's wl0.1 beside wl0.2, as a cloud-made pod's 5 GHz radio carries its cloud's, whose
    onboarding raised the netdev) gets no row, and neither does wl1 (the pod's station on its own
    netdev): the agent writes nothing, its status counting no write, while it reports both radios'
    port_state."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")

    async def reporting():  # both home APs' radios seen, the radio plan applied
        phy = (box.status() or {}).get("phy_netdev_up") or {}
        vifs = {r.get("if_name") for r in (await box.rows("Wifi_VIF_Config")).values()}
        return set(phy.get("port_state") or {}) == {"wl0", "wl1"} and "wl0.1" in vifs and phy

    phy = await serve_radios(box, reporting) is not None
    await asyncio.sleep(5)  # a few more of the agent's steps
    status = (box.status() or {}).get("phy_netdev_up") or {}
    taps = await phy_taps(box)
    return box.result(
        passed=phy and status.get("on") is True and status.get("writes") == 0 and not taps,
        phy=status,
        taps=sorted(taps),
    )


async def legacy_parent(box):
    """spec 3.10, 8.6: on a legacy pod the M2 set's backhaul BSS is the GRE parent AP wl1.1, with
    no multi_ap (the pod has none), its WPA2-PSK in the security map, hidden, no bridge, no
    bridging between its stations; its Inet row moves to the pod's underlay (.1, DHCP .10 to
    .250), replacing the address the pod's former manager had left there (169.254.2.1)."""
    ap, failure = await gre_parent_up(box, LEGACY_PARENT)
    if failure:
        return failure
    inet = await gre_inet(box, LEGACY_PARENT)
    dhcpd = ovsdb_map((inet or {}).get("dhcpd"))
    status = (box.status() or {}).get("gre_parent") or {}
    ap_ok = (
        legacy_row_ok(ap, {"encryption": "WPA-PSK", "key": REGISTRAR_BACKHAUL_KEY})
        and ap.get("bridge") == ""
        and ap.get("ap_bridge") is False
        and ap.get("ssid_broadcast") == "disabled"
    )
    inet_ok = (
        inet is not None
        and inet.get("inet_addr") == "169.254.5.1"
        and inet.get("netmask") == "255.255.255.0"
        and dhcpd.get("start") == "169.254.5.10"
        and dhcpd.get("stop") == "169.254.5.250"
    )
    return box.result(
        passed=ap_ok
        and inet_ok
        and status.get("ap") == LEGACY_PARENT
        and status.get("underlay") == LEGACY_UNDERLAY,
        parent_ap={k: ap.get(k) for k in ("ssid", "bridge", "ap_bridge", "ssid_broadcast")},
        security_keys=sorted(ovsdb_map(ap.get("security"))),
        underlay_inet=inet,
        gre_status=status,
    )


async def bridge_members(box, bridge):
    """The Port names in the pod's Bridge `bridge`."""
    ports = await box.rows("Port")
    row = next((r for r in (await box.rows("Bridge")).values() if r.get("name") == bridge), {})
    return {ports[u]["name"] for u in uuids(row.get("ports")) if u in ports}


async def patch_pair(box):
    """The pod's patch pair as written: {end: (in its bridge's ports, its type, its peer)}."""
    interfaces = {r.get("name"): r for r in (await box.rows("Interface")).values()}
    wan, home = await bridge_members(box, "br-wan"), await bridge_members(box, "br-home")
    pair = {}
    for name, members in (("patch-w2h", wan), ("patch-h2w", home)):
        row = interfaces.get(name) or {}
        pair[name] = (name in members, row.get("type"), ovsdb_map(row.get("options")).get("peer"))
    return pair


async def legacy_wired(box):
    """spec 3.10, 8.4: a wired legacy pod (no Connection_Manager_Uplink.bridge), cm using eth1 as
    its uplink in the WAN bridge br-wan. The agent bridges it the 2.x way, OpenSync's patch pair:
    patch-w2h in br-wan and patch-h2w in br-home, each a patch Interface whose peer is the other,
    in one guarded write; applied, and the uplink reported as ethernet, in use, in br-home."""
    onboarding = await onboarded(box)

    async def paired():
        pair = await patch_pair(box)
        return pair if all(member for member, _, _ in pair.values()) else None

    pair = await until_async(paired, seconds=60)

    def applied():
        operation = ((box.status() or {}).get("uplink") or {}).get("operation") or {}
        return operation.get("state") == "OBSERVED_APPLIED"

    done = await box.until(applied, seconds=60)
    uplink = (box.status() or {}).get("uplink") or {}
    return box.result(
        passed=bool(onboarding.get("applied"))
        and pair
        == {"patch-w2h": (True, "patch", "patch-h2w"), "patch-h2w": (True, "patch", "patch-w2h")}
        and bool(done)
        and uplink.get("in_use") == "eth1"
        and uplink.get("bridge") == "br-home",
        onboarded=bool(onboarding.get("applied")),
        pair=pair,
        applied=bool(done),
        uplink={k: uplink.get(k) for k in ("mode", "port", "bridge", "in_use", "waiting")},
    )


async def legacy_wired_half_pair(box):
    """spec 8.4: a legacy pod with half a patch pair (patch-w2h alone in br-wan, another
    manager's): left alone, the agent's write refused (OWNERSHIP_CONFLICT), no patch-h2w made."""
    wan = next(u for u, r in (await box.rows("Bridge")).items() if r.get("name") == "br-wan")
    await box.transact(
        [
            {"op": "insert", "table": "Interface", "uuid-name": "i", "row": {"name": "patch-w2h"}},
            {
                "op": "insert",
                "table": "Port",
                "uuid-name": "p",
                "row": {"name": "patch-w2h", "interfaces": ["set", [["named-uuid", "i"]]]},
            },
            {
                "op": "mutate",
                "table": "Bridge",
                "where": [["_uuid", "==", ["uuid", wan]]],
                "mutations": [["ports", "insert", ["set", [["named-uuid", "p"]]]]],
            },
        ]
    )
    onboarding = await onboarded(box)

    def refused():
        operation = ((box.status() or {}).get("uplink") or {}).get("operation") or {}
        return operation if operation.get("reason") == "OWNERSHIP_CONFLICT" else None

    operation = await box.until(refused, seconds=60)
    await asyncio.sleep(3)
    home = await bridge_members(box, "br-home")
    return box.result(
        passed=bool(onboarding.get("applied")) and bool(operation) and "patch-h2w" not in home,
        onboarded=bool(onboarding.get("applied")),
        operation=operation,
        home_ports=sorted(home),
    )


LEGACY_STATION = "02:00:00:00:41:01"  # wl1's MAC in the legacy pod's rows


async def legacy_pin(box):
    """The legacy pod's station row and its one linked credential, once the station is pinned to
    the GRE parent AP by its parent column; else None."""
    _, station = await box.vif("Wifi_VIF_Config", "wl1")
    creds = await box.rows("Wifi_Credential_Config")
    linked = [creds[u] for u in uuids((station or {}).get("credential_configs")) if u in creds]
    if str((station or {}).get("parent") or "").lower() != GRE_PARENT_AP or len(linked) != 1:
        return None
    return station, linked[0]


async def legacy_gre_move(box):
    """spec 3.10, 8.6: a legacy pod as a GRE child, on its own GRE uplink (wl1, g-wl1), moved onto
    a fleet GRE parent by the controller's Backhaul Steering Request. Its credential has no
    bssid, priority or enabled: the agent writes one with ssid, security and onboard_type gre
    only, and pins the parent in the station's own parent column (credential-list mode, ssid
    empty). Once the pod joins there, the move is applied and answered."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    peer_status(box, "RPIPOD0KEPTPARENT", al=PARENT_AL, gre_parent_ap=GRE_PARENT_AP)
    await box.until(lambda: uplink_status(box).get("bootstrap"), seconds=30)
    await asyncio.sleep(2.5)  # the agent's peer directory (1 s cache) picks up the GRE parent
    answered = len(box.sent(0x801A))
    mid = box.controller.send(0x8019, (backhaul_steering_request(LEGACY_STATION, GRE_PARENT_AP),))
    await box.until(lambda: box.controller.reply(ACK, mid), seconds=5)
    pinned = await until_async(lambda: legacy_pin(box), seconds=30)
    if not pinned:
        return box.result(passed=False, failed="no parent pin", uplink=uplink_status(box))
    station, credential = pinned
    uuid, _ = await box.vif("Wifi_VIF_State", "wl1")
    await box.transact(  # the closed target joins the parent: the station on it, cm's GRE stays
        [
            {
                "op": "update",
                "table": "Wifi_VIF_State",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {"parent": GRE_PARENT_AP, "ssid": "emosa-lab-bh", "wds": False},
            }
        ]
    )
    onto = await next_message(box, 0x801A, answered, seconds=60)
    answer = steering_answer(onto)

    def applied():
        operation = uplink_status(box).get("operation") or {}
        return operation.get("state") == "OBSERVED_APPLIED"

    done = await box.until(applied, seconds=60)
    security = ovsdb_map(credential.get("security"))
    return box.result(
        passed=answer == (LEGACY_STATION, GRE_PARENT_AP, 0)
        and bool(done)
        and credential.get("onboard_type") == "gre"
        and credential.get("ssid") == "emosa-lab-bh"
        and security.get("encryption") == "WPA-PSK"
        and not {"bssid", "priority", "enabled"} & set(credential)
        and station.get("ssid") == "",
        answer=answer,
        applied=bool(done),
        credential={k: v for k, v in credential.items() if k != "security"},
        station={k: station.get(k) for k in ("ssid", "parent", "enabled")},
    )


GTP_BSSID = "02:00:00:00:1b:02"  # the legacy fixture's station parent: the GTP's BSS
GTP_SSID, GTP_KEY = "emosa-lab-gtp", "OnlySimulationGtp2026!"


OPERATOR_CREDENTIALS = ("operator-bh-1", "operator-bh-2")  # a seed's onboarding, unlinked
# the operator's APs a seed brings back on the 5 GHz radio, which no legacy profile names
OPERATOR_APS = {"wl0.1": "operator-bh-1", "wl0.2": "operator-home"}


SEED_STATION_SSID = "operator-onboarding"


def seed_stations(names=("wl0", "wl1")):
    """A converted pod's onboarding stations as its seed brings them back (14 October: its wl0
    and wl1 joined another pod's operator backhaul): on the radios named, credential-list mode,
    the operator's onboarding credential linked, each in its radio's vif_configs, wl0 with an Inet
    row (wl1 has the seed's)."""
    operations = [
        {
            "op": "insert",
            "table": "Wifi_Credential_Config",
            "uuid-name": "onboarding",
            "row": {
                "ssid": SEED_STATION_SSID,
                "onboard_type": "gre",
                "security": ["map", [["encryption", "WPA-PSK"], ["key", "operator-key"]]],
            },
        },
        {
            "op": "insert",
            "table": "Wifi_Inet_Config",
            "row": {
                "if_name": "wl0",
                "if_type": "vif",
                "enabled": False,
                "network": False,
                "ip_assign_scheme": "dhcp",
            },
        },
    ]
    for name in names:
        operations += [
            {
                "op": "insert",
                "table": "Wifi_VIF_Config",
                "uuid-name": f"sta_{name}",
                "row": {
                    "if_name": name,
                    "mode": "sta",
                    "enabled": True,
                    "vif_radio_idx": 0,
                    "credential_configs": ["set", [["named-uuid", "onboarding"]]],
                },
            },
            {
                "op": "mutate",
                "table": "Wifi_Radio_Config",
                "where": [["if_name", "==", name]],
                "mutations": [["vif_configs", "insert", ["set", [["named-uuid", f"sta_{name}"]]]]],
            },
        ]
    return operations


def old_profiles(directory):
    """The bundled profiles as an image before 15 October has them: the legacy ones' fronthaul
    rows without group_rekey and ft_psk. The directory written."""
    out = directory / "profiles-old"
    out.mkdir(exist_ok=True)
    for path in (ROOT / "src/emosa/profiles").glob("*.json"):
        data = json.loads(path.read_text())
        if data.get("schema") == "opensync-2.0":
            for column in ("group_rekey", "ft_psk"):
                data["fronthaul"]["vif"].pop(column, None)
        (out / path.name).write_text(json.dumps(data, indent=2) + "\n")
    return out


def vendor_bridges():
    """A hooked boot's bridges (15 October, a converted pod's monitor): br-home's Inet row with
    its interface down (network false), br-wan's with NAT on, as the vendor's seed has them; the
    cloud sets bridge mode (br-home up, br-wan NAT off, the patch pair)."""
    return [
        {
            "op": "insert",
            "table": "Wifi_Inet_Config",
            "row": {
                "if_name": "br-home",
                "if_type": "bridge",
                "enabled": True,
                "network": False,
                "ip_assign_scheme": "none",
            },
        },
        {
            "op": "insert",
            "table": "Wifi_Inet_Config",
            "row": {
                "if_name": "br-wan",
                "if_type": "bridge",
                "enabled": True,
                "network": True,
                "NAT": True,
                "ip_assign_scheme": "dhcp",
            },
        },
    ]


def seeded_fronthaul(applied=False):
    """A hooked boot's fronthaul (15 October): wl1.2 on, naming br-home, as the operator's cloud
    left it (its SSID, key and the profile's oftag), its State taken by wm and its radio wl1's;
    applied: with the controller's SSID and key (an AP EMOSA made before it wrote bridge ports).
    Neither has a Port: wm never adds one."""
    key, ssid = (REGISTRAR_KEY, REGISTRAR_SSID) if applied else ("operator-key", "operator-home")
    security = ["map", [["encryption", "WPA-PSK"], ["key", key], ["oftag", "home--1"]]]
    row = {
        "if_name": LEGACY_FRONTHAUL,
        "mode": "ap",
        "enabled": True,
        "ssid": ssid,
        "bridge": "br-home",
        "vif_radio_idx": 2,
        "security": security,
    }
    return [
        {"op": "insert", "table": "Wifi_VIF_Config", "uuid-name": "c_fh", "row": row},
        {
            "op": "insert",
            "table": "Wifi_VIF_State",
            "uuid-name": "s_fh",
            "row": {
                **{k: row[k] for k in ("if_name", "mode", "enabled", "ssid", "security")},
                "mac": "02:00:00:00:41:02",
                "vif_config": ["named-uuid", "c_fh"],
            },
        },
        {
            "op": "mutate",
            "table": "Wifi_Radio_Config",
            "where": [["if_name", "==", "wl1"]],
            "mutations": [["vif_configs", "insert", ["set", [["named-uuid", "c_fh"]]]]],
        },
        {
            "op": "mutate",
            "table": "Wifi_Radio_State",
            "where": [["if_name", "==", "wl1"]],
            "mutations": [["vif_states", "insert", ["set", [["named-uuid", "s_fh"]]]]],
        },
    ]


def operator_aps():
    """The operator's APs as a vendor's seed has them, applied by its wm: wl0.1, a hidden
    backhaul AP with its own address and DHCP server (169.254.3.1/24), and wl0.2, a home AP in
    br-home; and wl1 on channel 1 at HT40, as the operator left it."""
    operations = []
    for index, (name, ssid) in enumerate(OPERATOR_APS.items(), start=1):
        tag = name.replace(".", "_")
        row = {
            "if_name": name,
            "mode": "ap",
            "enabled": True,
            "ssid": ssid,
            "vif_radio_idx": index,
            "security": ["map", [["encryption", "WPA-PSK"], ["key", "operator-key"]]],
        }
        if name == "wl0.2":
            row["bridge"] = "br-home"
        operations += [
            {"op": "insert", "table": "Wifi_VIF_Config", "uuid-name": f"c_{tag}", "row": row},
            {
                "op": "insert",
                "table": "Wifi_VIF_State",
                "uuid-name": f"s_{tag}",
                "row": {
                    **{k: row[k] for k in ("if_name", "mode", "enabled", "ssid")},
                    "mac": f"02:00:00:00:40:0{index}",
                    "vif_config": ["named-uuid", f"c_{tag}"],
                },
            },
            {
                "op": "mutate",
                "table": "Wifi_Radio_Config",
                "where": [["if_name", "==", "wl0"]],
                "mutations": [["vif_configs", "insert", ["set", [["named-uuid", f"c_{tag}"]]]]],
            },
            {
                "op": "mutate",
                "table": "Wifi_Radio_State",
                "where": [["if_name", "==", "wl0"]],
                "mutations": [["vif_states", "insert", ["set", [["named-uuid", f"s_{tag}"]]]]],
            },
        ]
    operations += [
        {
            "op": "insert",
            "table": "Wifi_Inet_Config",
            "row": {
                "if_name": "wl0.1",
                "if_type": "vif",
                "enabled": True,
                "network": True,
                "ip_assign_scheme": "static",
                "inet_addr": "169.254.3.1",
                "netmask": "255.255.255.0",
                "dhcpd": ["map", [["start", "169.254.3.2"], ["stop", "169.254.3.254"]]],
            },
        },
        {
            "op": "update",
            "table": "Wifi_Radio_Config",
            "where": [["if_name", "==", "wl1"]],
            "row": {"channel": 1, "ht_mode": "HT40"},
        },
    ]
    return operations


def ethernet_first(kind):
    """The legacy pod as the first one was admitted on the router (13 October), one transaction:
    cm on eth0 and no GRE (2.x cm builds none while the Ethernet link is used), and OpenSync's
    patch pair joining br-wan to br-home. Its station wl1: "linked", in credential-list mode, its
    one credential the GTP's (written beside the agent, by hand), pinned by its parent to the
    GTP's BSS, where it is associated; or as the vendor's "seed" has it: no station row, wl1's
    Inet row off (dhcp), and the operator's onboarding credentials, linked by nothing."""
    operations = [
        {
            "op": "delete",
            "table": "Connection_Manager_Uplink",
            "where": [["if_name", "==", "g-wl1"]],
        },
        {
            "op": "insert",
            "table": "Connection_Manager_Uplink",
            "row": {
                "if_name": "eth0",
                "if_type": "eth",
                "is_used": True,
                "has_L2": True,
                "has_L3": True,
            },
        },
    ]
    if kind == "linked":
        operations += [
            {
                "op": "insert",
                "table": "Wifi_Credential_Config",
                "uuid-name": "gtp",
                "row": {
                    "ssid": GTP_SSID,
                    "onboard_type": "gre",
                    "security": ["map", [["encryption", "WPA-PSK"], ["key", GTP_KEY]]],
                },
            },
            {
                "op": "update",
                "table": "Wifi_VIF_Config",
                "where": [["if_name", "==", "wl1"]],
                "row": {
                    "credential_configs": ["set", [["named-uuid", "gtp"]]],
                    "parent": GTP_BSSID,
                },
            },
        ]
    else:  # the seed: wl1's rows go (its Radio's vif_configs reference is weak)
        operations += [
            {"op": "delete", "table": table, "where": [["if_name", "==", "wl1"]]}
            for table in ("Wifi_VIF_Config", "Wifi_VIF_State")
        ]
        operations.append(
            {
                "op": "insert",
                "table": "Wifi_Inet_Config",
                "row": {
                    "if_name": "wl1",
                    "if_type": "vif",
                    "enabled": False,
                    "network": False,
                    "ip_assign_scheme": "dhcp",
                    "mtu": 1552,
                },
            }
        )
        operations.append(  # wl1 where the legacy profile has it
            {
                "op": "update",
                "table": "Wifi_Radio_Config",
                "where": [["if_name", "==", "wl1"]],
                "row": {"channel": 6, "ht_mode": "HT20"},
            }
        )
        operations += [
            {
                "op": "insert",
                "table": "Wifi_Credential_Config",
                "row": {
                    "ssid": ssid,
                    "onboard_type": "gre",
                    "security": ["map", [["encryption", "WPA-PSK"], ["key", "operator-key"]]],
                },
            }
            for ssid in OPERATOR_CREDENTIALS
        ]
    if kind in ("operator", "operator-dark"):  # the seed with the operator's APs on wl0, wl1 on 1
        operations += operator_aps()
    if (
        kind == "operator-dark"
    ):  # a hooked boot's: wm shows wl0 with no channel, its cloud never came
        operations.append(
            {
                "op": "update",
                "table": "Wifi_Radio_State",
                "where": [["if_name", "==", "wl0"]],
                "row": {"channel": ["set", []]},
            }
        )
    for name, peer, bridge, tag in (
        ("patch-w2h", "patch-h2w", "br-wan", "w2h"),
        ("patch-h2w", "patch-w2h", "br-home", "h2w"),
    ):
        operations += [
            {
                "op": "insert",
                "table": "Interface",
                "uuid-name": f"i_{tag}",
                "row": {"name": name, "type": "patch", "options": ["map", [["peer", peer]]]},
            },
            {
                "op": "insert",
                "table": "Port",
                "uuid-name": f"p_{tag}",
                "row": {"name": name, "interfaces": ["set", [["named-uuid", f"i_{tag}"]]]},
            },
            {
                "op": "mutate",
                "table": "Bridge",
                "where": [["name", "==", bridge]],
                "mutations": [["ports", "insert", ["set", [["named-uuid", f"p_{tag}"]]]]],
            },
        ]
    return operations


async def legacy_own_uplink(box):
    """What the legacy pod's own uplink is made of, beside cm's rows: its station's Config, its
    credentials and the patch pair."""
    _, station = await box.vif("Wifi_VIF_Config", "wl1")
    station = station or {}
    credentials = await box.rows("Wifi_Credential_Config")
    return {
        "station": {
            k: str(station.get(k))
            for k in ("mode", "enabled", "ssid", "parent", "credential_configs")
        },
        "credentials": sorted((r.get("ssid"), r.get("onboard_type")) for r in credentials.values()),
        "pair": await patch_pair(box),
    }


async def lose_ethernet(box):
    """The wired legacy pod's Ethernet goes, as 2.x cm meets it: its link to the agent drops, cm
    builds its GRE over the station (g-wl1 in use, eth0 no longer), and the link comes back over
    it 15 s later. {"own": the pod's own uplink before, "wired"/"wireless": the station's parent
    in a Topology Response before and after, "session": kept/fresh, "notification": a Topology
    Notification after}."""
    query = (Tlv(0xB3, b"\x01"),)
    wired = station_parent(await box.ask(TOPOLOGY_QUERY, query, TOPOLOGY_RESPONSE))
    own = await legacy_own_uplink(box)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued, notified = kept_reconnections(box), topology_notifications(box)
    await dial(box.db, f"tcp:127.0.0.1:{AGENT_PORT}", connect=False)
    await box.transact(
        [
            {
                "op": "update",
                "table": "Connection_Manager_Uplink",
                "where": [["if_name", "==", "eth0"]],
                "row": {"is_used": False, "has_L2": False, "has_L3": False},
            },
            {
                "op": "insert",
                "table": "Connection_Manager_Uplink",
                "row": {
                    "if_name": "g-wl1",
                    "if_type": "gre",
                    "is_used": True,
                    "has_L2": True,
                    "has_L3": True,
                },
            },
        ]
    )
    await asyncio.sleep(15)
    await dial(box.db, f"tcp:127.0.0.1:{AGENT_PORT}")
    mode = await back(box, searches, m1s, continued)
    notification = await box.until(lambda: topology_notifications(box) > notified, seconds=10)
    wireless = station_parent(await box.ask(TOPOLOGY_QUERY, query, TOPOLOGY_RESPONSE))
    return {
        "own": own,
        "wired": wired,
        "wireless": wireless,
        "session": mode,
        "notification": bool(notification),
    }


async def legacy_ethernet_lost(box):
    """spec 3.10, 8.2, 2.5: a legacy pod wired as the first was admitted (ethernet_first), its
    agent's uplink mode off, GRE parent on its underlay. Its Ethernet goes: its link to the agent
    drops, cm builds its GRE over the station already on the GTP's BSS (g-wl1 in use), and the
    link comes back over it 15 s later. The session is kept (no Search or M1) with a Topology
    Notification; its next Topology Response names the station on the GTP's BSS (a Wi-Fi
    backhaul, none while wired); the agent writes nothing of the pod's uplink (its station, its
    credentials, the patch pair), and its fronthaul and parent AP stay applied. With uplink mode
    gre (legacy-ethernet-lost-gre, a GRE child awaiting a steer), the station written beside the
    agent is not taken for the agent's own move: the GTP's BSS is its recorded bootstrap."""
    ap, failure = await gre_parent_up(box, LEGACY_PARENT)
    if failure:
        return failure
    await box.until(lambda: all_applied((box.status() or {}).get("operations") or []), seconds=30)
    lost = await lose_ethernet(box)
    own, after = lost["own"], await legacy_own_uplink(box)
    _, parent_ap = await box.vif("Wifi_VIF_Config", LEGACY_PARENT)
    status = box.status() or {}
    uplink = uplink_status(box)
    gtp = status.get("gtp_station") or {}
    return box.result(
        passed=lost["session"] == "kept"
        and lost["notification"]
        and lost["wired"] is None
        and lost["wireless"] == GTP_BSSID
        and after == own
        and all(member for member, _, _ in own["pair"].values())
        and parent_ap == ap
        and all_applied(status.get("operations") or [])
        and not status.get("pod_refused")
        and (not box.backhaul or (uplink.get("bootstrap") or {}).get("bssid") == GTP_BSSID)
        # with uplink.bootstrap: the station already there is left alone, nothing written
        and (not box.gtp_station or (bool(gtp) and gtp.get("operation") is None)),
        session=lost["session"],
        topology_notification=lost["notification"],
        station_parent={"wired": lost["wired"], "wireless": lost["wireless"]},
        own_uplink_unchanged=after == own,
        own_uplink=after,
        gtp_station=gtp or None,
        parent_ap_unchanged=parent_ap == ap,
        uplink={k: uplink.get(k) for k in ("mode", "bootstrap", "in_use", "operation")},
    )


async def gtp_station_written(box):
    """(the station's Config uuid and row, its one linked credential) once a wl1 station row
    exists; else None."""
    uuid, station = await box.vif("Wifi_VIF_Config", "wl1")
    if not station or station.get("mode") != "sta":
        return None
    credentials = await box.rows("Wifi_Credential_Config")
    linked = [credentials[u] for u in uuids(station.get("credential_configs")) if u in credentials]
    return (uuid, station, linked[0]) if len(linked) == 1 else None


async def legacy_gtp_station(box):
    """spec 3.10, the GTP station: a legacy pod as its vendor's seed has it, wired (cm on eth0, no
    GRE), with no station and only the operator's credentials, unlinked; its agent's uplink mode
    off with uplink.bootstrap (the GTP BSS's SSID, its passphrase by reference, no pin). The agent
    writes the station once, in one transaction: a credential (ssid, the security map,
    onboard_type gre, nothing else), wl1 mode=sta enabled in vconf form (the GTP's SSID and
    security, vif_radio_idx 0, the credential linked, no parent), in its radio's vif_configs, and
    wl1's Inet enabled; applied once wm shows the station. Meanwhile cm's uplink, the radio and the
    home AP are not written, nor the operator's credentials. Then the Ethernet goes (as
    legacy-ethernet-lost): the session is kept, the station is the Wi-Fi backhaul on the GTP's
    BSS, and nothing more is written."""
    ap, failure = await gre_parent_up(box, LEGACY_PARENT)
    if failure:
        return failure
    radio_uuid, _ = next(
        (u, r)
        for u, r in (await box.rows("Wifi_Radio_Config")).items()
        if r.get("if_name") == "wl1"
    )

    async def untouched():
        """What the station's write must leave alone: cm's uplink rows, wl1's radio and home AP."""
        radios, home = (
            await box.rows("Wifi_Radio_Config"),
            await box.vif("Wifi_VIF_Config", "wl1.2"),
        )
        radio = radios.get(radio_uuid) or {}
        cm = await box.rows("Connection_Manager_Uplink")
        return {
            "uplink": sorted((r.get("if_name"), r.get("is_used")) for r in cm.values()),
            "radio": {k: str(radio.get(k)) for k in ("channel", "ht_mode", "enabled")},
            "home_ap": {k: str((home[1] or {}).get(k)) for k in ("ssid", "enabled", "security")},
        }

    before = await untouched()
    written = await until_async(lambda: gtp_station_written(box), seconds=30)
    if not written:
        return box.result(
            passed=False,
            failed="no GTP station",
            gtp_station=(box.status() or {}).get("gtp_station"),
        )
    config_uuid, station, credential = written
    # wm takes the station, in its radio's vif_states, and its supplicant joins the GTP's BSS,
    # beside the Ethernet uplink
    await box.transact(
        [
            {
                "op": "insert",
                "table": "Wifi_VIF_State",
                "uuid-name": "sta",
                "row": {
                    "if_name": "wl1",
                    "mode": "sta",
                    "enabled": True,
                    "mac": LEGACY_STATION,
                    "ssid": GTP_SSID,
                    "parent": GTP_BSSID,
                    "vif_config": ["uuid", config_uuid],
                },
            },
            {
                "op": "mutate",
                "table": "Wifi_Radio_State",
                "where": [["if_name", "==", "wl1"]],
                "mutations": [["vif_states", "insert", ["set", [["named-uuid", "sta"]]]]],
            },
        ]
    )

    def applied():
        operation = ((box.status() or {}).get("gtp_station") or {}).get("operation") or {}
        return operation.get("state") == "OBSERVED_APPLIED"

    done = await box.until(applied, seconds=30)
    radios = await box.rows("Wifi_Radio_Config")
    in_radio = config_uuid in uuids((radios.get(radio_uuid) or {}).get("vif_configs"))
    inet = next(
        (r for r in (await box.rows("Wifi_Inet_Config")).values() if r.get("if_name") == "wl1"), {}
    )
    operators = {
        r.get("ssid")
        for r in (await box.rows("Wifi_Credential_Config")).values()
        if r.get("ssid") in OPERATOR_CREDENTIALS
    }
    wired = await untouched()
    lost = await lose_ethernet(box)
    after = await legacy_own_uplink(box)
    security = {"encryption": "WPA-PSK", "key": GTP_KEY}
    station_ok = (
        station.get("enabled") is True
        and station.get("ssid") == GTP_SSID
        and ovsdb_map(station.get("security")) == security
        and station.get("vif_radio_idx") == 0
        and station.get("parent") in (None, "", ["set", []])  # no pin by default
    )
    credential_ok = (
        credential.get("ssid") == GTP_SSID
        and credential.get("onboard_type") == "gre"
        and ovsdb_map(credential.get("security")) == security
        and not {"bssid", "priority", "enabled"} & set(credential)
    )
    status = box.status() or {}
    return box.result(
        passed=station_ok
        and credential_ok
        and in_radio
        and inet.get("enabled") is True
        and inet.get("network") is True
        and bool(done)
        and wired == before
        and operators == set(OPERATOR_CREDENTIALS)
        and lost["session"] == "kept"
        and lost["notification"]
        and lost["wired"] is None
        and lost["wireless"] == GTP_BSSID
        and after == lost["own"]
        and all_applied(status.get("operations") or []),
        station={k: station.get(k) for k in ("mode", "enabled", "ssid", "vif_radio_idx", "parent")},
        credential={k: v for k, v in credential.items() if k != "security"},
        station_ok=station_ok,
        credential_ok=credential_ok,
        in_radio=in_radio,
        inet={k: inet.get(k) for k in ("enabled", "network", "ip_assign_scheme")},
        applied=bool(done),
        untouched_while_wired=wired == before,
        operators_kept=sorted(operators),
        session=lost["session"],
        station_parent={"wired": lost["wired"], "wireless": lost["wireless"]},
        nothing_more_written=after == lost["own"],
        gtp_station=status.get("gtp_station"),
    )


def radio_plan_operation(box):
    return ((box.status() or {}).get("radio_plan") or {}).get("operation") or {}


async def radio_plan_applied(box):
    """Drive the pod's managers until the agent's radio plan of this start is applied: its
    disabled VIFs' State follows their Config, as wm's does. The operation, or None."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and box.process.poll() is None:
        await apply_configuration(box.db)
        operation = radio_plan_operation(box)
        if operation.get("state") == "OBSERVED_APPLIED":
            return operation
        await asyncio.sleep(0.5)
    return None


async def radio_plan_rows(box):
    """{if_name: (Config enabled, State enabled)} of the pod's VIFs, the Inet rows' enabled, and
    wl1's channel and ht_mode."""
    configs, states = await box.rows("Wifi_VIF_Config"), await box.rows("Wifi_VIF_State")
    state = {r.get("if_name"): r.get("enabled") for r in states.values()}
    radios = await box.rows("Wifi_Radio_Config")
    wl1 = next((r for r in radios.values() if r.get("if_name") == "wl1"), {})
    return {
        "vifs": {
            r.get("if_name"): (r.get("mode"), r.get("enabled"), state.get(r.get("if_name")))
            for r in configs.values()
        },
        "inets": {
            r.get("if_name"): r.get("enabled")
            for r in (await box.rows("Wifi_Inet_Config")).values()
        },
        "wl1": (wl1.get("channel"), wl1.get("ht_mode")),
    }


async def associate_station(box):
    """wm takes the GTP station and its supplicant joins the GTP's BSS (the box plays both): the
    station's State, in its radio's vif_states, its parent the GTP's BSSID."""
    config_uuid, station = await box.vif("Wifi_VIF_Config", "wl1")
    if not station or station.get("mode") != "sta":
        return False
    state_uuid, _ = await box.vif("Wifi_VIF_State", "wl1")
    row = {
        "mode": "sta",
        "enabled": True,
        "mac": LEGACY_STATION,
        "ssid": GTP_SSID,
        "parent": GTP_BSSID,
        "vif_config": ["uuid", config_uuid],
    }
    if state_uuid:
        await box.transact(
            [
                {
                    "op": "update",
                    "table": "Wifi_VIF_State",
                    "where": [["_uuid", "==", ["uuid", state_uuid]]],
                    "row": row,
                }
            ]
        )
        return True
    await box.transact(
        [
            {
                "op": "insert",
                "table": "Wifi_VIF_State",
                "uuid-name": "sta",
                "row": {**row, "if_name": "wl1"},
            },
            {
                "op": "mutate",
                "table": "Wifi_Radio_State",
                "where": [["if_name", "==", "wl1"]],
                "mutations": [["vif_states", "insert", ["set", [["named-uuid", "sta"]]]]],
            },
        ]
    )
    return True


async def healthcheck(box):
    """The pods' vendor healthcheck in the box's terms (spec 3.10): every Wifi_VIF_Config row's
    interface beacons (an AP: its State enabled, and its radio's State on with a channel) or is
    associated (a station: its State's parent), whatever the row's own enabled. The interfaces
    that fail, sorted."""
    configs, states = await box.rows("Wifi_VIF_Config"), await box.rows("Wifi_VIF_State")
    state = {r.get("if_name"): r for r in states.values()}
    radio_states = {r.get("if_name"): r for r in (await box.rows("Wifi_Radio_State")).values()}
    on_air = {}  # each VIF Config row's radio, as wm shows it: on, with a channel
    for radio in (await box.rows("Wifi_Radio_Config")).values():
        shown = radio_states.get(radio.get("if_name")) or {}
        up = shown.get("enabled") is True and isinstance(shown.get("channel"), int)
        for uuid in uuids(radio.get("vif_configs")):
            on_air[uuid] = up
    failing = []
    for uuid, row in configs.items():
        name = row.get("if_name")
        seen = state.get(name) or {}
        if row.get("mode") == "sta":
            parent = seen.get("parent")
            ok = isinstance(parent, str) and parent not in ("", "00:00:00:00:00:00")
        else:
            ok = seen.get("enabled") is True and on_air.get(uuid, True)
        if not ok:
            failing.append(name)
    return sorted(failing)


async def wm_radios(box):
    """wm's part for the radios, as the box plays it where a scenario needs it: a radio's State
    takes its Config's enabled and channel once its Config names a channel."""
    states = {r.get("if_name"): u for u, r in (await box.rows("Wifi_Radio_State")).items()}
    changes = [
        {
            "op": "update",
            "table": "Wifi_Radio_State",
            "where": [["_uuid", "==", ["uuid", states[row.get("if_name")]]]],
            "row": {"enabled": row.get("enabled") is True, "channel": row["channel"]},
        }
        for row in (await box.rows("Wifi_Radio_Config")).values()
        if isinstance(row.get("channel"), int) and row.get("if_name") in states
    ]
    if changes:
        await box.transact(changes)


async def legacy_gtp_station_unassociated(box):
    """spec 3.10, the station's guard: the pods' vendor healthcheck reboots a pod after ten
    minutes of a configured station that is not associated. The GTP station written on the seed
    does not associate (the box plays no supplicant): the healthcheck fails on wl1 meanwhile. Once
    it has been unassociated for the guard's time (8 s here, 300 by default) the agent deletes it,
    its credential and its radio's reference with it, and counts it; the healthcheck passes again.
    After the back-off (12 s here) the station is written again; this time it associates, and stays
    past the guard."""
    ap, failure = await gre_parent_up(box, LEGACY_PARENT)
    if failure:
        return failure
    written = await until_async(lambda: gtp_station_written(box), seconds=30)
    if not written:
        return box.result(passed=False, failed="no GTP station")
    config_uuid, _, _ = written
    first = gtp_station_operation(box).get("operation_id")
    written_at = time.monotonic()
    failing_while = await healthcheck(box)

    async def gone():
        _, row = await box.vif("Wifi_VIF_Config", "wl1")
        return row is None

    removed = await until_async(gone, seconds=30)
    removed_after = time.monotonic() - written_at
    radios = await box.rows("Wifi_Radio_Config")
    referenced = any(config_uuid in uuids(r.get("vif_configs")) for r in radios.values())
    credentials = await box.rows("Wifi_Credential_Config")
    credential_gone = not any(r.get("ssid") == GTP_SSID for r in credentials.values())
    # the agent's status file follows on its own cadence: the count once it is written there
    await box.until(
        lambda: ((box.status() or {}).get("gtp_station") or {}).get("removed_unassociated"),
        seconds=10,
    )
    counted = ((box.status() or {}).get("gtp_station") or {}).get("removed_unassociated")
    failing_after = await healthcheck(box)

    async def again():
        found = await gtp_station_written(box)
        operation = gtp_station_operation(box).get("operation_id")
        return found if found and operation not in (None, first) else None

    rewritten = await until_async(again, seconds=40)
    if rewritten:
        await associate_station(box)
    await asyncio.sleep(12)  # past the guard: an associated station stays
    _, kept = await box.vif("Wifi_VIF_Config", "wl1")
    final = (box.status() or {}).get("gtp_station") or {}
    failing_end = await healthcheck(box)
    # the station written again on its own Inet row (a DHCP client), never on a tap row the
    # radios' netdev step might have written while the station had none (spec 3.10)
    inets = [r for r in (await box.rows("Wifi_Inet_Config")).values() if r.get("if_name") == "wl1"]
    own_inet = [(r.get("if_type"), r.get("ip_assign_scheme")) for r in inets]
    return box.result(
        passed=failing_while == ["wl1"]
        and bool(removed)
        and removed_after < 20
        and not referenced
        and credential_gone
        and counted == 1
        and not failing_after
        and bool(rewritten)
        and kept is not None
        and final.get("associated") is True
        and final.get("removed_unassociated") == 1
        and not failing_end
        and own_inet == [("vif", "dhcp")],
        healthcheck_failing_while_unassociated=failing_while,
        station_inet=own_inet,
        removed_after_s=round(removed_after, 1),
        radio_reference_left=referenced,
        credential_removed=credential_gone,
        removed_counted=counted,
        healthcheck_failing_after_removal=failing_after,
        written_again=bool(rewritten),
        kept_once_associated=kept is not None,
        healthcheck_failing_at_end=failing_end,
        gtp_station=final,
    )


def emosa_key(box):
    """The agent's own passphrase for the unclaimed APs (spec 3.10), read from its secret store as
    only the box may: never in a status, a log or the journal."""
    path = box.directory / "state" / box.serial / "secrets" / "emosa-unclaimed"
    return path.read_text() if path.exists() else None


async def neutralized(box, names):
    """The unclaimed APs ``names`` neutral (spec 3.10): hidden, in no bridge, on the agent's own key
    (WPA2-PSK), and still on (their beacons are the vendor healthcheck's)."""
    key = emosa_key(box)
    rows = {r.get("if_name"): r for r in (await box.rows("Wifi_VIF_Config")).values()}
    return bool(key) and all(
        rows.get(n, {}).get("ssid_broadcast") == "disabled"
        and rows.get(n, {}).get("bridge") in ("", None)
        and ovsdb_map(rows.get(n, {}).get("security")) == {"encryption": "WPA-PSK", "key": key}
        and rows.get(n, {}).get("enabled") is True
        for n in names
    )


async def legacy_radio_plan(box):
    """spec 3.10, the radio plan (radio_plan emosa): a wired legacy pod as its vendor's seed has
    it, the operator's APs on wl0 (wl0.1 with its own address and DHCP server, wl0.2 in br-home)
    and wl1 on channel 1 at HT40, the GTP station configured. The agent writes the plan once on
    that start: wl0.1's Inet off and wl1 on the profile's channel 6 at HT20. No AP is disabled
    (the pods' vendor healthcheck reboots a pod whose configured VIF has no BSSID): every VIF
    stays on, the pod passes the healthcheck once its station is associated. wl0.1's Inet turned
    on again on that start is left alone: one write per start. The pod's OpenSync starts again
    on its seed (its rows with the same UUIDs): the plan is written once more on the new start,
    and nothing after."""
    ap, failure = await gre_parent_up(box, LEGACY_PARENT)
    if failure:
        return failure
    first = await radio_plan_applied(box)
    if not first:
        return box.result(
            passed=False, failed="no radio plan", radio_plan=(box.status() or {}).get("radio_plan")
        )
    await until_async(lambda: gtp_station_written(box), seconds=30)
    await associate_station(box)
    rows = await radio_plan_rows(box)
    vifs = rows["vifs"]
    kept_on = all(vifs.get(n) == ("ap", True, True) for n in OPERATOR_APS)
    neutral = await neutralized(box, OPERATOR_APS)
    claimed = all(vifs.get(n, (None, None))[1] is True for n in (LEGACY_FRONTHAUL, LEGACY_PARENT))
    station = vifs.get("wl1", (None, None))[:2] == ("sta", True)
    planned = rows["inets"].get("wl0.1") is False and rows["wl1"] == (6, "HT20")
    failing = await healthcheck(box)
    # someone turns wl0.1's Inet on again on this start: left alone, no second write
    uuid = next(
        u for u, r in (await box.rows("Wifi_Inet_Config")).items() if r.get("if_name") == "wl0.1"
    )
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_Inet_Config",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {"enabled": True},
            }
        ]
    )
    await asyncio.sleep(8)
    again = (await radio_plan_rows(box))["inets"].get("wl0.1") is True
    same_start = radio_plan_operation(box).get("operation_id") == first.get("operation_id")
    # the pod's OpenSync starts again on its seed: a new start, the plan written once more
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued = kept_reconnections(box)
    await box.new_pod_database()
    session = await back(box, searches, m1s, continued)
    second = await radio_plan_applied(box)
    reseeded = (
        second is not None
        and second.get("operation_id") != first.get("operation_id")
        and second.get("instance") != first.get("instance")
    )
    await until_async(lambda: gtp_station_written(box), seconds=30)  # the new start's too
    await associate_station(box)
    rows_again = await radio_plan_rows(box)
    failing_again = await healthcheck(box)
    neutral_again = await neutralized(box, OPERATOR_APS)
    await asyncio.sleep(8)
    once = radio_plan_operation(box).get("operation_id") == (second or {}).get("operation_id")
    return box.result(
        passed=kept_on
        and claimed
        and station
        and planned
        and not failing
        and again
        and same_start
        and reseeded
        and all(rows_again["vifs"].get(n) == ("ap", True, True) for n in OPERATOR_APS)
        and rows_again["inets"].get("wl0.1") is False
        and rows_again["vifs"].get("wl1", (None, None))[:2] == ("sta", True)
        and not failing_again
        and neutral
        and neutral_again
        and once,
        vifs=vifs,
        inets=rows["inets"],
        wl1=rows["wl1"],
        operator_aps_kept_on=kept_on,
        operator_aps_neutral=neutral,
        operator_aps_neutral_after_reseed=neutral_again,
        claimed_enabled=claimed,
        station_enabled=station,
        healthcheck_failing=failing,
        inet_on_again_left_alone=again and same_start,
        session_after_reseed=session,
        healthcheck_failing_after_reseed=failing_again,
        reseed_written_once=reseeded and once,
        radio_plan=(box.status() or {}).get("radio_plan"),
    )


async def legacy_5g_fronthaul(box):
    """spec 3.10, 9: the legacy pod's 5 GHz radio as a fronthaul only (bcm-pod-2.4.3-gre-5g-v1).
    The pod left wl0 on DFS channel 108, which EMOSA does not map; the profile places it on 36
    (HT80), so it is reported there from the start and its own M2 set configures it: the fronthaul
    BSS on its home AP wl0.2, created on wl0 with WPA2-PSK in the security map and the profile's
    oftag, no multi_ap, and wl0 given channel 36 at HT80 in the same write; the set's backhaul BSS
    has no 5 GHz slot and is left out, so the backhaul stays on wl1 (2.4 GHz)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")

    async def created():
        _, row = await box.vif("Wifi_VIF_Config", LEGACY_SLOT_5G)
        return row

    config = await until_async(created, seconds=60)
    radios, vifs = await box.rows("Wifi_Radio_Config"), await box.rows("Wifi_VIF_Config")
    wl0 = next((u for u, r in radios.items() if r.get("if_name") == "wl0"), None)
    on_wl0 = any(
        vifs.get(u, {}).get("if_name") == LEGACY_SLOT_5G
        for u in uuids(radios.get(wl0, {}).get("vif_configs"))
    )
    names = {r.get("if_name") for r in vifs.values()}
    wanted = {"encryption": "WPA-PSK", "key": REGISTRAR_KEY, "oftag": "home--1"}
    placed = {k: radios.get(wl0, {}).get(k) for k in ("channel", "ht_mode", "enabled")}
    return box.result(
        passed=legacy_row_ok(config, wanted)
        and on_wl0
        and (config or {}).get("bridge") == "br-home"
        and "wl0.1" not in names
        and placed == {"channel": 36, "ht_mode": "HT80", "enabled": True},
        fronthaul_5g={k: (config or {}).get(k) for k in ("if_name", "ssid", "bridge")},
        on_wl0=on_wl0,
        wl0=placed,
        vifs=sorted(n for n in names if n),
    )


def gtp_station_operation(box):
    return ((box.status() or {}).get("gtp_station") or {}).get("operation") or {}


async def legacy_reseed(box, *, away=0):
    """spec 3.10, a converted legacy pod re-seeded (14 October: its connection manager restarted
    its managers from the vendor's seed after more than 120 s without its Ethernet, and the pod
    went back to its operator, until it was admitted again): its database is its seed again, every
    row with the UUID it had there, its radio rows too, so they do not tell the starts apart. The
    pod configured first (the controller's M2 applied, the GTP station written, the radio plan
    applied), then its seed again, dialing the agent ``away`` seconds later. The agent counts the
    pod's starts by its own sessions: the fresh session (or a kept reconnection that found the
    configuration lost) is a new start, a new ownership period. The controller's M2 is written
    and applied again, with no ownership conflict; the GTP station and the radio plan are written
    once more (wl0.1's Inet off again, every AP left on and the pod passing the healthcheck);
    with the 5 GHz profile, wl0 back on 36 at HT80 with its home AP wl0.2."""
    five = box.profile == LEGACY_PROFILE_5G
    unclaimed = sorted(n for n in OPERATOR_APS if not (five and n == LEGACY_SLOT_5G))

    async def configured():
        """The pod as EMOSA configures it: {what: bool}."""
        rows = await radio_plan_rows(box)
        vifs = rows["vifs"]
        radios, configs = await box.rows("Wifi_Radio_Config"), await box.rows("Wifi_VIF_Config")
        wl0 = next((r for r in radios.values() if r.get("if_name") == "wl0"), {})
        _, home_5g = await box.vif("Wifi_VIF_Config", LEGACY_SLOT_5G)
        fronthaul = next((r for r in configs.values() if r.get("if_name") == LEGACY_FRONTHAUL), {})
        return {
            "fronthaul": fronthaul.get("ssid") == REGISTRAR_SSID,
            "unclaimed_on": all(vifs.get(n) == ("ap", True, True) for n in unclaimed),
            "unclaimed_neutral": await neutralized(box, unclaimed),
            "wl0.1_inet_off": rows["inets"].get("wl0.1") is False,
            "station": vifs.get("wl1", (None, None))[:2] == ("sta", True),
            "wl1": rows["wl1"] == (6, "HT20"),
            **(
                {
                    "wl0": (wl0.get("channel"), wl0.get("ht_mode")) == (36, "HT80"),
                    "home_5g": (home_5g or {}).get("ssid") == REGISTRAR_SSID
                    and (home_5g or {}).get("enabled") is True,
                }
                if five
                else {}
            ),
        }

    async def settled(before=({}, {})):
        """The radio plan and the GTP station of this start, other operations than ``before``'s
        (the last start's): (plan, station) operations, each None if not there in 60 s."""
        deadline, plan, station = time.monotonic() + 60, None, None
        while time.monotonic() < deadline and box.process.poll() is None:
            if plan is None:
                found = await radio_plan_applied(box)
                if found and found.get("operation_id") != before[0].get("operation_id"):
                    plan = found
            if station is None and await gtp_station_written(box):
                found = gtp_station_operation(box)
                if found.get("operation_id") not in (None, before[1].get("operation_id")):
                    station = found
                    await associate_station(box)  # the box plays wm and the supplicant
            if plan is not None and station is not None:
                break
            await asyncio.sleep(0.5)
        return plan, station

    ap, failure = await gre_parent_up(box, LEGACY_PARENT)
    if failure:
        return failure
    if five and not await box.until(lambda: radio_operations(box, "5G"), seconds=60):
        return box.result(passed=False, failed="no 5 GHz M2 before the re-seed")
    plan, station = await settled()
    before = await configured()
    if not plan or not station or not all(before.values()):
        return box.result(
            passed=False,
            failed="not configured before the re-seed",
            configured=before,
            radio_plan=(box.status() or {}).get("radio_plan"),
            gtp_station=(box.status() or {}).get("gtp_station"),
        )
    starts = (box.status() or {}).get("session_starts") or 0
    operations = len(operation_states(box))
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued = kept_reconnections(box)
    await box.new_pod_database(away=away)
    seeded = await configured()  # the seed's: the operator's again
    session = await back(box, searches, m1s, continued, seconds=90)
    plan_again, station_again = await settled((plan, station))
    if five:
        await box.until(lambda: all_applied(radio_operations(box, "5G")[-1:]), seconds=60)
        await apply_configuration(box.db)
    after = await configured()
    failing = await healthcheck(box)
    states = operation_states(box)
    conflicts = [s for s in states if "OWNERSHIP_CONFLICT" in s]
    new_start = ((box.status() or {}).get("session_starts") or 0) > starts
    written_again = (
        plan_again is not None
        and plan_again.get("operation_id") != plan.get("operation_id")
        and plan_again.get("instance") != plan.get("instance")
        and station_again is not None
        and station_again.get("operation_id") != station.get("operation_id")
    )
    return box.result(
        passed=session is not None
        and not all(seeded.values())
        and all(after.values())
        and not failing
        and not conflicts
        and len(states) > operations
        and states[-1][0] == "OBSERVED_APPLIED"
        and new_start
        and written_again,
        session=session,
        seeded=seeded,
        configured_again=after,
        healthcheck_failing=failing,
        operations=[list(s) for s in states[operations - 1 :]],
        ownership_conflicts=len(conflicts),
        session_starts=(box.status() or {}).get("session_starts"),
        plan_and_station_written_again=written_again,
        radio_plan=(box.status() or {}).get("radio_plan"),
    )


async def legacy_reseed_reclaim(box):
    return await legacy_reseed(box)


async def legacy_profile_5g(box):
    """spec 3.10, 9, plan A: a legacy pod configured on bcm-pod-2.4.3-gre-v1 (the operator's
    seed, the radio plan applied), then its fleet entry switched to
    bcm-pod-2.4.3-gre-5g-v1 and its agent restarted on its journal, on the same start of the
    pod's OpenSync. No ownership conflict: the earlier operations were planned under another
    profile (spec 5) and the agent's new session is a new start (spec 3.10). The 5 GHz radio's M2
    makes wl0.2 its home AP and puts wl0 on 36 at HT80; wl0.1 stays on, its Inet off."""
    ap, failure = await gre_parent_up(box, LEGACY_PARENT)
    if failure:
        return failure
    if not await radio_plan_applied(box):
        return box.result(passed=False, failed="no radio plan on the 2.4 GHz profile")

    async def five_ghz():
        radios = await box.rows("Wifi_Radio_Config")
        wl0 = next((r for r in radios.values() if r.get("if_name") == "wl0"), {})
        _, home = await box.vif("Wifi_VIF_Config", LEGACY_SLOT_5G)
        return {
            "wl0": (wl0.get("channel"), wl0.get("ht_mode")),
            "home_5g": {k: (home or {}).get(k) for k in ("ssid", "enabled")},
        }

    async def switched(operations):
        await apply_configuration(box.db)
        ops = (box.status() or {}).get("operations") or []
        placed = await five_ghz()
        return (
            len(ops) > operations
            and all_applied(ops[operations:])
            and all_applied(radio_operations(box, "5G")[-1:])
            and placed["wl0"] == (36, "HT80")
        )

    done = await with_profile(box, LEGACY_PROFILE_5G, switched)
    placed = await five_ghz()
    rows = await radio_plan_rows(box)
    states = operation_states(box) + [
        (op.get("state"), op.get("reason")) for op in radio_operations(box, "5G")
    ]
    conflicts = [s for s in states if "OWNERSHIP_CONFLICT" in s]
    return box.result(
        passed=done
        and not conflicts
        and placed == {"wl0": (36, "HT80"), "home_5g": {"ssid": REGISTRAR_SSID, "enabled": True}}
        and rows["vifs"].get("wl0.1") == ("ap", True, True)
        and await neutralized(box, ["wl0.1"])
        and rows["inets"].get("wl0.1") is False,
        switched=done,
        five_ghz=placed,
        wl0_1=rows["vifs"].get("wl0.1"),
        ownership_conflicts=len(conflicts),
        operations=[list(s) for s in states],
    )


async def legacy_reseed_reclaim_late(box):
    """legacy-reseed-reclaim with the pod away past the reconnection window (a reboot's or a
    long outage's): the session lost meanwhile, the pod's seed a fresh session's."""
    return await legacy_reseed(box, away=RECONNECT_WINDOW + 5)


def node_config_status(box):
    return (box.status() or {}).get("node_config") or {}


async def node_settings(box):
    """The pod's Node_Config: {(module, key): (value, persist)}."""
    return {
        (r.get("module"), r.get("key")): (r.get("value"), r.get("persist"))
        for r in (await box.rows("Node_Config")).values()
    }


async def node_report(box, **state):
    """The hook's Node_State as it writes it: its rows replaced by one per key."""
    old = [u for u, r in (await box.rows("Node_State")).items() if r.get("module") == "hook"]
    await box.transact(
        [
            {"op": "delete", "table": "Node_State", "where": [["_uuid", "==", ["uuid", u]]]}
            for u in old
        ]
        + [
            {"op": "insert", "table": "Node_State", "row": {"module": "hook", "key": k, "value": v}}
            for k, v in state.items()
        ]
    )


async def node_config(box):
    """spec 4, the pods' modules: the fleet's module "hook" (a setting, redirector auto, and an
    update by its SHA-256) on a pod whose Node_Config has a key the module no longer has and
    other modules' rows (as a converted pod's operator leaves them). The agent writes the module's
    rows in one guarded write, applied: its setting, update_url (the fleet's file server) and
    update_sha256, persist true; the stale key removed; the other modules' rows kept, each as it
    was. The box plays the hook: its Node_State (state ok, boot,
    version, the setting echoed, the update failed its check, three events). The status reports
    them, the setting echoed, the flag update_failed; each event is logged once. The pod's
    OpenSync starts again (a new database): the rows are written again on the new start."""

    def operation():
        return node_config_status(box).get("operation") or {}

    first = await box.until(lambda: operation().get("state") == "OBSERVED_APPLIED" and operation())
    if not first:
        return box.result(
            passed=False, failed="settings not applied", node_config=node_config_status(box)
        )
    settings = await node_settings(box)
    url = f"http://{NODE_ADVERTISE}:6650/{NODE_UPDATE}"
    wanted = {
        ("hook", "redirector"): ("auto", True),
        ("hook", "update_url"): (url, True),
        ("hook", "update_sha256"): (NODE_UPDATE, True),
        **{(m, k): (v, True) for m, k, v in FOREIGN_NODE_ROWS},  # never touched
    }
    await node_report(
        box,
        state="ok",
        boot_id="boot-1",
        version="c" * 64,
        running="c" * 64,
        slot="tb",
        redirector="auto",
        update="failed:check",
        update_sha256=NODE_UPDATE,
        events="1 apply;2 emosa;3 update:failed:check",
    )

    def module():
        hook = (node_config_status(box).get("modules") or {}).get("hook") or {}
        return hook if hook.get("state") == "ok" else None

    reported = await box.until(module, seconds=30)
    await asyncio.sleep(3)  # a few more reads of the same report: nothing logged twice
    log = (box.directory / "agent.log").read_text(errors="replace")
    logged = [log.count(f"module hook: event {e}") for e in ("1 apply", "2 emosa", "3 update")]
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued = kept_reconnections(box)
    await box.new_pod_database()
    session = await back(box, searches, m1s, continued)

    def again():
        op = operation()
        new = op.get("operation_id") != first.get("operation_id")
        return op if new and op.get("state") == "OBSERVED_APPLIED" else None

    second = await box.until(again, seconds=60)
    rewritten = await node_settings(box)
    return box.result(
        passed=settings == wanted
        and bool(reported)
        and reported.get("settings_echoed") == ["redirector", "update_sha256"]
        and reported.get("flags") == ["update_failed"]
        and reported.get("version") == "c" * 64
        and logged == [1, 1, 1]
        and session is not None
        and second is not None
        and second.get("instance") != first.get("instance")
        and {k: v for k, v in rewritten.items() if k[0] == "hook"}
        == {k: v for k, v in wanted.items() if k[0] == "hook"},
        settings=sorted([list(k), list(v)] for k, v in settings.items()),
        reported=reported,
        events_logged=logged,
        session_after_restart=session,
        rewritten=sorted([list(k), list(v)] for k, v in rewritten.items()),
    )


async def station_rows(box):
    """The pod's station rows: {if_name: its SSID} (its linked credentials' when it has none)."""
    return stations_of(await box.rows("Wifi_VIF_Config"), await box.rows("Wifi_Credential_Config"))


def seed_station_rows(seed):
    """station_rows of the seed as the box keeps it (raw rows, kept before the agent started): the
    C agent can delete a seed's stations before a read of the pod's database (CI, 15 October)."""
    return stations_of(
        {r["_uuid"][1]: r for r in seed.get("Wifi_VIF_Config", [])},
        {r["_uuid"][1]: r for r in seed.get("Wifi_Credential_Config", [])},
    )


def stations_of(vifs, credentials):
    """{if_name: SSID} of the station rows among `vifs` (uuid -> row), each its linked
    `credentials`' SSIDs when it has none."""
    found = {}
    for row in vifs.values():
        if row.get("mode") == "sta":
            linked = sorted(
                credentials[u].get("ssid") or ""
                for u in uuids(row.get("credential_configs"))
                if u in credentials
            )
            ssid = row.get("ssid") if isinstance(row.get("ssid"), str) else ""
            found[row.get("if_name")] = ssid or ",".join(linked)
    return found


async def legacy_seed_stations(box):
    """spec 3.10, a converted pod's seed stations: legacy-radio-plan's wired pod, its seed with the
    operator's onboarding stations on wl0 and wl1 (credential-list mode), which associate to nothing
    on a converted site and fail the vendor healthcheck. The radio plan's write on the start deletes
    both, with their credential, wl0's Inet row and their radios' references; the agent logs it.
    wl1 free, the GTP station is written there and associates: after admission the only station is
    EMOSA's, and the pod passes the healthcheck. The pod's OpenSync starts again on its seed (the
    stations back): the same on the new start."""
    seeded = seed_station_rows(box.seed)  # the pod's own read may come after the agent's deletion
    ap, failure = await gre_parent_up(box, LEGACY_PARENT)
    if failure:
        return failure

    async def settled():
        await apply_configuration(box.db)
        rows = await station_rows(box)
        if rows == {"wl1": GTP_SSID}:
            await associate_station(box)
            return rows
        return None

    first = await until_async(settled, seconds=90)
    inets = sorted(r.get("if_name") for r in (await box.rows("Wifi_Inet_Config")).values())
    credentials = sorted(r.get("ssid") for r in (await box.rows("Wifi_Credential_Config")).values())
    failing = await healthcheck(box)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued = kept_reconnections(box)
    # the stations the seed brought back, read before the agent acts on them
    reseeded = await box.new_pod_database(before_dial=lambda: station_rows(box))
    session = await back(box, searches, m1s, continued, seconds=90)
    again = await until_async(settled, seconds=90)
    failing_again = await healthcheck(box)
    log = (box.directory / "agent.log").read_text(errors="replace")
    plan = (box.status() or {}).get("radio_plan") or {}
    return box.result(
        passed=seeded == {"wl0": SEED_STATION_SSID, "wl1": SEED_STATION_SSID}
        and first == {"wl1": GTP_SSID}
        and "wl0" not in inets
        and SEED_STATION_SSID not in credentials
        and not failing
        and reseeded == seeded
        and session is not None
        and again == {"wl1": GTP_SSID}
        and not failing_again
        and log.count("stations") >= 2
        and plan.get("foreign_stations") == [],
        seeded=seeded,
        after_admission=first,
        inet_rows=inets,
        credentials=credentials,
        healthcheck_failing=failing,
        session_after_reseed=session,
        after_reseed=again,
        healthcheck_failing_after_reseed=failing_again,
        radio_plan=plan,
    )


async def legacy_dark_radio(box):
    """spec 3.10, every enabled AP beacons: legacy-radio-plan's wired pod as a hooked boot leaves
    its seed, its operator's cloud never come: wl0 (5 GHz, which the 2.4 profile neither binds nor
    places) carries the operator's APs wl0.1 and wl0.2, enabled, but wm shows it with no channel,
    so neither beacons and the vendor healthcheck fails them (14 October: "wl0.2 is not
    beaconing"). The radio plan's write on the start turns wl0 on at the profile's idle channel, 36
    at HT20; once wm shows it there the plan is applied and the pod passes the healthcheck. The
    pod's OpenSync starts again on its seed (wl0 dark again): the same on the new start. Last, wm
    showing wl0 dark again on that start: the healthcheck fails wl0.1 and wl0.2."""
    # the seed the pod restores, as the box keeps it (read from the pod, the agent may have
    # written already)
    seed_wl0 = next((r for r in box.seed.get("Wifi_Radio_State", []) if r["if_name"] == "wl0"), {})
    seeded_dark = not isinstance(seed_wl0.get("channel"), int)
    ap, failure = await gre_parent_up(box, LEGACY_PARENT)
    if failure:
        return failure

    async def applied(previous=None):
        await apply_configuration(box.db)
        await wm_radios(box)
        operation = radio_plan_operation(box)
        new = previous is None or operation.get("operation_id") != previous.get("operation_id")
        return operation if new and operation.get("state") == "OBSERVED_APPLIED" else None

    async def wl0():
        radios = await box.rows("Wifi_Radio_Config")
        row = next((r for r in radios.values() if r.get("if_name") == "wl0"), {})
        return (row.get("enabled"), row.get("channel"), row.get("ht_mode"))

    first = await until_async(applied, seconds=60)
    if not first:
        return box.result(
            passed=False, failed="no radio plan", radio_plan=(box.status() or {}).get("radio_plan")
        )
    await until_async(lambda: gtp_station_written(box), seconds=30)
    await associate_station(box)
    on = await wl0()
    failing = await healthcheck(box)
    plan = (box.status() or {}).get("radio_plan") or {}
    # the fronthaul radio's channel as wm shows it, beside the reported one: the same here
    pod = (box.status() or {}).get("pod") or {}
    observed = (pod.get("observed_channel"), pod.get("channel"), pod.get("channel_mismatch"))
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued = kept_reconnections(box)
    await box.new_pod_database()
    session = await back(box, searches, m1s, continued)
    second = await until_async(lambda: applied(first), seconds=60)
    await until_async(lambda: gtp_station_written(box), seconds=30)  # the new start's too
    await associate_station(box)
    on_again = await wl0()
    failing_again = await healthcheck(box)
    # the healthcheck's radio: wm shows wl0 dark again on this start (the plan written already)
    states = await box.rows("Wifi_Radio_State")
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_Radio_State",
                "where": [["_uuid", "==", ["uuid", u]]],
                "row": {"channel": ["set", []]},
            }
            for u, r in states.items()
            if r.get("if_name") == "wl0"
        ]
    )
    failing_dark = await healthcheck(box)
    return box.result(
        passed=seeded_dark
        and on == (True, 36, "HT20")
        and not failing
        and plan.get("dark_radios") == []
        and observed == (6, 6, False)
        and session is not None
        and second is not None
        and second.get("instance") != first.get("instance")
        and on_again == (True, 36, "HT20")
        and not failing_again
        and failing_dark == sorted(OPERATOR_APS),
        seeded_dark=seeded_dark,
        wl0=on,
        healthcheck_failing=failing,
        session_after_reseed=session,
        wl0_after_reseed=on_again,
        healthcheck_failing_after_reseed=failing_again,
        healthcheck_failing_wl0_dark=failing_dark,
        fronthaul_channel=observed,
        radio_plan=plan,
    )


async def legacy_seed_station_unwired(box):
    """spec 3.10, the station guard on a converted pod: the pod on its Wi-Fi uplink (its station wl1
    associated, cm on g-wl1), so the radio plan leaves its stations; its seed's onboarding station
    on wl0 never associates. Past the guard's time (8 s here, 300 by default) the agent deletes it,
    with its credential, its Inet row and its radio's reference, counts it
    (others_removed_unassociated) and logs it, and does not write it again; the pod's own uplink
    station is left alone, and the pod passes the healthcheck."""
    seeded = await station_rows(box)

    async def removed():
        rows = await station_rows(box)
        return rows if "wl0" not in rows else None

    after = await until_async(removed, seconds=40)
    await asyncio.sleep(13)  # past the back-off it would take: not written again
    again = await station_rows(box)
    inets = sorted(r.get("if_name") for r in (await box.rows("Wifi_Inet_Config")).values())
    credentials = sorted(r.get("ssid") for r in (await box.rows("Wifi_Credential_Config")).values())
    failing = await healthcheck(box)
    status = (box.status() or {}).get("gtp_station") or {}
    plan = (box.status() or {}).get("radio_plan") or {}
    log = (box.directory / "agent.log").read_text(errors="replace")
    return box.result(
        passed=seeded == {"wl0": SEED_STATION_SSID, "wl1": ""}
        and after == {"wl1": ""}
        and again == {"wl1": ""}
        and "wl0" not in inets
        and SEED_STATION_SSID not in credentials
        and not failing
        and status.get("others_removed_unassociated") == 1
        and plan.get("wired") is False
        and "(not EMOSA's) was not associated" in log,
        seeded=seeded,
        after=after,
        again=again,
        inet_rows=inets,
        credentials=credentials,
        healthcheck_failing=failing,
        gtp_station=status,
    )


async def operator_cloud(box):
    """What of its operator's cloud the pod has: {AWLAN_Node's MQTT column: its map} for each not
    empty, and "Wifi_Stats_Config": the statistics rows' types when there are any."""
    node = next(iter((await box.rows("AWLAN_Node")).values()), {})
    cloud = {c: ovsdb_map(node.get(c)) for c in CLOUD_COLUMNS if ovsdb_map(node.get(c))}
    stats = await box.rows("Wifi_Stats_Config")
    if stats:
        cloud["Wifi_Stats_Config"] = sorted(r.get("stats_type") for r in stats.values())
    return cloud


async def legacy_cloud_cleared(box):
    """spec 3.10, a converted pod talks only to EMOSA: legacy-radio-plan's pod, its seed with its
    operator's cloud (qm's broker and headers, a client and a survey statistics row) and the
    fleet's telemetry on. The radio plan clears it in its one write on the start: AWLAN_Node's
    MQTT settings, headers and topics empty, the statistics rows gone. The agent's own telemetry
    is off on a converted pod: nothing of EMOSA's written in their place. The pod's OpenSync starts
    again on its seed, the operator's cloud back: cleared once more on the new start."""
    ap, failure = await gre_parent_up(box, LEGACY_PARENT)
    if failure:
        return failure
    first = await radio_plan_applied(box)
    if not first:
        return box.result(
            passed=False, failed="no radio plan", radio_plan=(box.status() or {}).get("radio_plan")
        )
    await asyncio.sleep(8)  # the telemetry scope's ticks, were it on
    cleared = await operator_cloud(box)
    telemetry = ((box.status() or {}).get("telemetry") or {}).get("mode")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued = kept_reconnections(box)
    await box.new_pod_database()
    # the seed the pod restores, as the box keeps it (read from the pod, the agent may have
    # cleared it already: the C agent did, in CI)
    node = next(iter(box.seed.get("AWLAN_Node", [])), {})
    seeded = [c for c in CLOUD_COLUMNS if ovsdb_map(node.get(c))]
    if box.seed.get("Wifi_Stats_Config"):
        seeded.append("Wifi_Stats_Config")
    session = await back(box, searches, m1s, continued)

    async def applied_again():
        await apply_configuration(box.db)
        operation = radio_plan_operation(box)
        new = operation.get("operation_id") != first.get("operation_id")
        return operation if new and operation.get("state") == "OBSERVED_APPLIED" else None

    second = await until_async(applied_again, seconds=60)
    await asyncio.sleep(8)
    again = await operator_cloud(box)
    plan = (box.status() or {}).get("radio_plan") or {}
    return box.result(
        passed=not cleared
        and telemetry == "off"
        and sorted(seeded) == ["Wifi_Stats_Config", "mqtt_headers", "mqtt_settings"]
        and session is not None
        and second is not None
        and second.get("instance") != first.get("instance")
        and not again
        and plan.get("operator_cloud") == [],
        cloud_after_plan=cleared,
        telemetry=telemetry,
        seeded=seeded,
        session_after_reseed=session,
        cloud_after_reseed=again,
        radio_plan=plan,
    )


async def generation_refused(box):
    """spec 3.10: the pod's schema is not its profile's generation (a 6.6 profile on a legacy pod,
    or a legacy profile on a 6.6 pod). The agent refuses the pod (SCHEMA_MISMATCH in its status):
    no M1, nothing written, however long the controller waits."""
    before = await box.rows("Wifi_VIF_Config")
    refused = await box.until(lambda: (box.status() or {}).get("pod_refused"), seconds=30)
    search = box.controller.latest(AUTOCONFIG_SEARCH)
    if search:
        box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
    await asyncio.sleep(5)
    box.controller.poll()
    after = await box.rows("Wifi_VIF_Config")
    return box.result(
        passed=refused == "SCHEMA_MISMATCH"
        and box.controller.latest(AUTOCONFIG_WSC) is None
        and after == before,
        pod_refused=refused,
        m1=box.controller.latest(AUTOCONFIG_WSC) is not None,
        unchanged=after == before,
    )


SCENARIOS = {
    "boot": boot,
    "onboard": onboard,
    "refuse": refuse,
    "answers": answers,
    "early-report": early_report,
    "renew": renew,
    "no-m2": no_m2,
    "silent-controller": silent_controller,
    "forgotten-agent": forgotten_agent,
    "agent-netns": agent_netns,
    "unserved-pod": unserved_pod,
    "new-source": new_source,
    "pod-recreated": pod_recreated,
    "clients": clients,
    "reannounce": reannounce,
    "channel-selection": channel_selection,
    "channel-scan": channel_scan,
    "telemetry": telemetry,
    "foreign-broker": foreign_broker,
    "metrics": metrics,
    "unassociated": unassociated,
    "steering": steering,
    "steering-leftover": steering_leftover,
    "steering-close-refused": steering_close_refused,
    "steering-refusals": steering_refusals,
    "backhaul-capability": backhaul_capability,
    "wired-stations-off": wired_stations_off,
    "backhaul-steering": backhaul_steering,
    "revert-by-record": revert_by_record,
    "restart-during-move": restart_during_move,
    "fallback-during-move": fallback_during_move,
    "gre-child-kept": gre_child_kept,
    "gre-child-kept-gone": gre_child_kept_gone,
    "gre-child-kept-gone-up": gre_child_kept_gone_up,
    "backhaul-steering-renewal": backhaul_steering_renewal,
    "backhaul-steering-refused": backhaul_steering_refused,
    "backhaul-steering-own-bss": backhaul_steering_own_bss,
    "backhaul-steering-across-bands": backhaul_steering_across_bands,
    "backhaul-steering-across-bands-fails": backhaul_steering_across_bands_fails,
    "backhaul-steering-no-station-on-band": backhaul_steering_no_station_on_band,
    "backhaul-steering-channel-not-operable": backhaul_steering_channel_not_operable,
    "pod-child": pod_child,
    "pod-parent": pod_parent,
    "pod-parent-loop": pod_parent_loop,
    "gre-parent-create": gre_parent_create,
    "gre-parent-child": gre_parent_child,
    "gre-parent-child-gone": gre_parent_child_gone,
    "gre-parent-child-redrift": gre_parent_child_redrift,
    "gre-parent-withdrawn": gre_parent_withdrawn,
    "second-radio-parent": second_radio_parent,
    "second-radio-withdrawn": second_radio_withdrawn,
    "second-radio-child-credentials": second_radio_child_credentials,
    "second-radio-no-power": second_radio_no_power,
    "second-radio-moves-channel": second_radio_moves_channel,
    "renew-on-bootstrap": renew_on_bootstrap,
    "power-rises": power_rises,
    "gre-parent-promoted": gre_parent_promoted,
    "gre-parent-demoted": gre_parent_demoted,
    "adapter-restart": adapter_restart,
    "transport-cut": transport_cut,
    "transport-cut-long": transport_cut_long,
    "pod-vanished": pod_vanished,
    "reconnect-moved-uplink": reconnect_moved_uplink,
    "reconnect-channel-follows": reconnect_channel_follows,
    "reconnect-not-provisioned": reconnect_not_provisioned,
    "reconnect-config-lost": reconnect_config_lost,
    "fronthaul-channel-moves": fronthaul_channel_moves,
    "write-lost-unapplied": write_lost_unapplied,
    "restart-during-write": restart_during_write,
    "legacy-onboard": legacy_onboard,
    "legacy-auth-reload": legacy_auth_reload,
    "no-auth-reload-6-6": no_auth_reload_6_6,
    "legacy-bridge-port": legacy_bridge_port,
    "legacy-bridge-port-repair": legacy_bridge_port_repair,
    "legacy-bridge-mode": legacy_bridge_mode,
    "legacy-bridge-mode-repair": legacy_bridge_mode_repair,
    "legacy-bridge-mode-old-profile": legacy_bridge_mode_repair,
    "legacy-phy-netdev": legacy_phy_netdev,
    "legacy-phy-netdev-foreign": legacy_phy_netdev_foreign,
    "legacy-parent": legacy_parent,
    "legacy-parent-child": legacy_parent_child,
    "legacy-parent-child-gone": legacy_parent_child_gone,
    "legacy-pod-6-6-profile": generation_refused,
    "pod-6-6-legacy-profile": generation_refused,
    "legacy-wired": legacy_wired,
    "legacy-wired-half-pair": legacy_wired_half_pair,
    "legacy-gre-move": legacy_gre_move,
    "legacy-5g-fronthaul": legacy_5g_fronthaul,
    "legacy-ethernet-lost": legacy_ethernet_lost,
    "legacy-ethernet-lost-gre": legacy_ethernet_lost,
    "legacy-gtp-station": legacy_gtp_station,
    "legacy-gtp-station-present": legacy_ethernet_lost,
    "legacy-radio-plan": legacy_radio_plan,
    "legacy-reseed-reclaim": legacy_reseed_reclaim,
    "legacy-reseed-reclaim-5g": legacy_reseed_reclaim,
    "legacy-reseed-reclaim-late": legacy_reseed_reclaim_late,
    "legacy-profile-5g": legacy_profile_5g,
    "legacy-gtp-station-unassociated": legacy_gtp_station_unassociated,
    "legacy-cloud-cleared": legacy_cloud_cleared,
    "node-config": node_config,
    "legacy-seed-stations": legacy_seed_stations,
    "legacy-dark-radio": legacy_dark_radio,
    "legacy-seed-station-unwired": legacy_seed_station_unwired,
}
OPTIONS = {  # the box each scenario needs beyond the default
    "write-lost-unapplied": {"reply_cut": "lost-write"},
    "steering-close-refused": {"reply_cut": "refused-close"},
    "forgotten-agent": {"topology_query_window": 20},
    "agent-netns": {"netns": True},
    "telemetry": {"telemetry": True},
    "foreign-broker": {"telemetry": True, "foreign_broker": True},
    "metrics": {"telemetry": True},
    "unassociated": {"telemetry": True},
    "backhaul-capability": {"backhaul": True},
    "wired-stations-off": {"wired": True, "stations_off": True},
    "node-config": {"modules": NODE_MODULES},
    "backhaul-steering": {"backhaul": True},
    "revert-by-record": {
        "backhaul": True,
        "bootstrap": True,
        "uplink": {
            "mode": "gre",
            "station": "bhaul-sta-24",
            "credentials": "config",
            "ssid": "emosa-lab-bh",
            "secret_ref": "backhaul",
        },
    },
    "restart-during-move": {
        "backhaul": True,
        "bootstrap": True,
        "uplink": {
            "mode": "gre",
            "station": "bhaul-sta-24",
            "credentials": "config",
            "ssid": "emosa-lab-bh",
            "secret_ref": "backhaul",
        },
    },
    "fallback-during-move": {
        "backhaul": True,
        "bootstrap": True,
        "uplink": {
            "mode": "gre",
            "station": "bhaul-sta-24",
            "credentials": "config",
            "ssid": "emosa-lab-bh",
            "secret_ref": "backhaul",
        },
    },
    **{
        name: {
            "backhaul": True,
            "bootstrap": True,
            "uplink": {
                "mode": "gre",
                "station": "bhaul-sta-24",
                "credentials": "config",
                "ssid": "emosa-lab-bh",
                "secret_ref": "backhaul",
            },
        }
        for name in ("gre-child-kept", "gre-child-kept-gone", "gre-child-kept-gone-up")
    },
    "backhaul-steering-renewal": {"backhaul": True},
    "backhaul-steering-own-bss": {"backhaul": True},
    "backhaul-steering-across-bands": {"backhaul": True},
    "backhaul-steering-across-bands-fails": {"backhaul": True},
    "backhaul-steering-no-station-on-band": {"backhaul": True},
    "backhaul-steering-channel-not-operable": {"backhaul": True},
    "pod-child": {"backhaul": True},
    "pod-parent": {"multi_bss": True, "telemetry": True},
    "pod-parent-loop": {"backhaul": True},
    "gre-parent-create": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-v1",
        "underlay": GRE_UNDERLAY,
    },
    "gre-parent-child": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-v1",
        "underlay": GRE_UNDERLAY,
    },
    "gre-parent-child-gone": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-v1",
        "underlay": GRE_UNDERLAY,
    },
    "gre-parent-child-redrift": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-v1",
        "underlay": GRE_UNDERLAY,
    },
    "gre-parent-withdrawn": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-v1",
        "underlay": GRE_UNDERLAY,
    },
    "second-radio-parent": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-5g-v1",
        "underlay": GRE_UNDERLAY,
    },
    "second-radio-withdrawn": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-5g-v1",
        "underlay": GRE_UNDERLAY,
    },
    # a GRE child of the Pis' shape, its uplink credentials from the M2 sets (spec 8.6, 9)
    "second-radio-child-credentials": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-5g-v1",
        "underlay": GRE_UNDERLAY,
        "backhaul": True,
        "gre_child": True,
        "uplink": {"mode": "gre", "station": "bhaul-sta-50"},
    },
    "second-radio-no-power": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-5g-v1",
        "underlay": GRE_UNDERLAY,
    },
    "second-radio-moves-channel": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-5g-v1",
        "underlay": GRE_UNDERLAY,
    },
    **{
        name: {
            "multi_bss": True,
            "profile": "opensync-lab-hwsim-6.6.1-gre-5g-v1",
            "underlay": GRE_UNDERLAY,
            "backhaul": True,
            "gre_child": True,
            "uplink": {"mode": "gre", "station": "bhaul-sta-50"},
        }
        for name in ("renew-on-bootstrap", "power-rises")
    },
    # a GRE parent with a child, then demoted to the plain profile
    "gre-parent-demoted": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-v1",
        "underlay": GRE_UNDERLAY,
    },
    # onboarded on the plain profile, then promoted to GRE parent (finding A)
    "gre-parent-promoted": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-v1",
        "underlay": GRE_UNDERLAY,
    },
}
# spec 2.5's reconnections: the uplink move as restart-during-move's, radio B's move as
# second-radio-moves-channel's
OPTIONS["reconnect-moved-uplink"] = OPTIONS["restart-during-move"]
OPTIONS["reconnect-channel-follows"] = OPTIONS["second-radio-moves-channel"]
# spec 3.10: a legacy pod, and each generation's pod under the other's profile
OPTIONS["legacy-onboard"] = {"legacy": True}
# a hooked boot's fronthaul: the seed's wl1.2 as the operator's cloud left it
OPTIONS["legacy-auth-reload"] = {"legacy": True, "seeded_fronthaul": True}
# a hooked boot (no VIF rows), and a live pod's fronthaul EMOSA made before bridge ports
OPTIONS["legacy-bridge-port"] = {"legacy": True}
OPTIONS["legacy-bridge-port-repair"] = {"legacy": True, "seeded_fronthaul": "applied"}
# a hooked boot's bridges (br-home down, br-wan's NAT on, no pair): cold, and a live pod's
OPTIONS["legacy-bridge-mode"] = {"legacy": True, "vendor_bridges": True}
OPTIONS["legacy-bridge-mode-repair"] = {
    "legacy": True,
    "vendor_bridges": True,
    "seeded_fronthaul": "applied",
}
# the same on profiles without the cloud's group_rekey and ft_psk (an agent binary deployed alone)
OPTIONS["legacy-bridge-mode-old-profile"] = {
    **OPTIONS["legacy-bridge-mode-repair"],
    "old_profiles": True,
}
OPTIONS["legacy-parent"] = {"legacy": True, "multi_bss": True, "underlay": LEGACY_UNDERLAY}
OPTIONS["legacy-parent-child"] = OPTIONS["legacy-parent"]
OPTIONS["legacy-parent-child-gone"] = OPTIONS["legacy-parent"]
OPTIONS["legacy-pod-6-6-profile"] = {"legacy": True, "profile": PLAIN_PROFILE}
OPTIONS["pod-6-6-legacy-profile"] = {"profile": LEGACY_PROFILE}
OPTIONS["legacy-wired"] = {"legacy": True, "wired": True}
OPTIONS["legacy-wired-half-pair"] = {"legacy": True, "wired": True}
OPTIONS["legacy-5g-fronthaul"] = {
    "legacy": True,
    "multi_bss": True,
    "underlay": LEGACY_UNDERLAY,
    "profile": "bcm-pod-2.4.3-gre-5g-v1",
}
OPTIONS["legacy-ethernet-lost"] = {
    "legacy": True,
    "multi_bss": True,
    "underlay": LEGACY_UNDERLAY,
    "ethernet_first": "linked",
}
OPTIONS["legacy-ethernet-lost-gre"] = {
    **OPTIONS["legacy-ethernet-lost"],
    "backhaul": True,
    "uplink": {"mode": "gre", "station": "wl1"},
}
# spec 3.10, the GTP station: written on the seed; a station already there left alone
GTP_STATION = {"ssid": GTP_SSID, "secret_ref": "gtp"}
OPTIONS["legacy-gtp-station"] = {
    **OPTIONS["legacy-ethernet-lost"],
    "ethernet_first": "seed",
    "gtp_station": GTP_STATION,
}
OPTIONS["legacy-gtp-station-present"] = {
    **OPTIONS["legacy-ethernet-lost"],
    "gtp_station": GTP_STATION,
}
# spec 3.10, the radio plan: the seed with the operator's APs, the GTP station written beside
OPTIONS["legacy-radio-plan"] = {
    **OPTIONS["legacy-gtp-station"],
    "ethernet_first": "operator",
}
# spec 3.10, a re-seed: the radio plan's pod, its seed restored with the same UUIDs
OPTIONS["legacy-reseed-reclaim"] = OPTIONS["legacy-radio-plan"]
OPTIONS["legacy-reseed-reclaim-5g"] = {**OPTIONS["legacy-radio-plan"], "profile": LEGACY_PROFILE_5G}
# spec 3.10, the radios' own netdevs: the 5 GHz home AP on wl0 (on by default); and wl0 also
# carrying the operator's wl0.1, which EMOSA did not make
OPTIONS["legacy-phy-netdev"] = OPTIONS["legacy-5g-fronthaul"]
OPTIONS["legacy-phy-netdev-foreign"] = OPTIONS["legacy-reseed-reclaim-5g"]
OPTIONS["legacy-reseed-reclaim-late"] = OPTIONS["legacy-radio-plan"]
OPTIONS["legacy-profile-5g"] = OPTIONS["legacy-radio-plan"]
# the station's guard, its times shortened: removed after 8 s unassociated, written again 12 s later
OPTIONS["legacy-gtp-station-unassociated"] = {
    **OPTIONS["legacy-gtp-station"],
    "gtp_station": {**GTP_STATION, "unassociated_seconds": 8, "retry_seconds": [12]},
}
# spec 3.10, the operator's cloud cleared: the radio plan's pod, its seed with the cloud, and the
# fleet's telemetry on (off on a converted pod)
# spec 3.10, the seed's stations: the radio plan's pod with the operator's onboarding stations
OPTIONS["legacy-seed-stations"] = {**OPTIONS["legacy-radio-plan"], "seed_stations": True}
# every enabled AP beacons: the radio plan's pod, wm showing wl0 with no channel (a hooked boot's)
OPTIONS["legacy-dark-radio"] = {**OPTIONS["legacy-radio-plan"], "ethernet_first": "operator-dark"}
# the station guard: the pod on its Wi-Fi uplink, a seed station on wl0, the guard's time shortened
OPTIONS["legacy-seed-station-unwired"] = {
    "legacy": True,
    "multi_bss": True,
    "underlay": LEGACY_UNDERLAY,
    "gtp_station": {**GTP_STATION, "unassociated_seconds": 8, "retry_seconds": [12]},
    "seed_stations": ("wl0",),
}
OPTIONS["legacy-cloud-cleared"] = {
    **OPTIONS["legacy-radio-plan"],
    "operator_cloud": True,
    "telemetry": True,
}
OPTIONS["legacy-gre-move"] = {
    "legacy": True,
    "backhaul": True,
    "uplink": {
        "mode": "gre",
        "station": "wl1",
        "credentials": "config",
        "ssid": "emosa-lab-bh",
        "secret_ref": "backhaul",
    },
}


async def run(scenario, agent, directory, *, binary=None):
    from emosa_lab import box_features

    if scenario in box_features.SCENARIOS:  # the features' faults and refusals (plan 8.4)
        options = box_features.OPTIONS.get(scenario, {})
        async with Box(agent, directory, binary=binary, **options) as box:
            return await box_features.SCENARIOS[scenario](box)
    if scenario not in SCENARIOS:  # the fleet's and the GTP's (box_adapter)
        from emosa_lab import box_adapter

        return await box_adapter.run(scenario, agent, directory, binary=binary)
    async with Box(agent, directory, binary=binary, **OPTIONS.get(scenario, {})) as box:
        return await SCENARIOS[scenario](box)


def main():
    from emosa_lab import box_adapter, box_features

    names = {*SCENARIOS, *box_adapter.SCENARIOS, *box_adapter.STANDALONE, *box_features.SCENARIOS}
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", choices=sorted(names))
    parser.add_argument("--agent", choices=["c", "python"], required=True)
    parser.add_argument("--binary", help="the C agent (default c/build/emosa-agent-c)")
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    in_namespace()
    result = asyncio.run(
        run(args.scenario, args.agent, args.directory.resolve(), binary=args.binary)
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
