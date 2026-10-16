# SPDX-License-Identifier: Apache-2.0
"""A wired pod's Ethernet uplink in its home bridge, as an EMOSA scope (spec §8.4).

OpenSync 6.6's ``cm`` takes an Ethernet port as the pod's uplink once its DHCP and router
checks pass (``Connection_Manager_Uplink``: ``if_type=eth``, ``is_used=true``). The
operator's cloud then bridges it: ``Connection_Manager_Uplink.bridge`` names the bridge
``cm`` adds the port to, which puts the pod's clients on the uplink's LAN (spec §8.1 D1).
Without the cloud, EMOSA writes it: the fronthaul's bridge from the pod's profile
(``br-home``), in one guarded transaction.

OpenSync forgets the bridge at every start, so the write is made once per start
(``start_instance``), like the statistics and the Wi-Fi uplink switch. A bridge another
manager set is not taken over: that write is refused. Applied means the row shows the
bridge on the same start.

A legacy pod (spec 3.10) has no ``Connection_Manager_Uplink.bridge``: its ``cm`` puts the uplink
in the WAN bridge (``br-wan``) and the pod is a bridge when OpenSync's patch pair joins that
bridge to the home bridge (``patch-w2h`` in the WAN bridge, ``patch-h2w`` in the home bridge,
each the other's peer), which ``cm`` reads but never makes. EMOSA writes the pair: two patch
Interfaces, their Ports and the two Bridges' ``ports``, in one guarded transaction. The pair
stays in the pod's Open vSwitch database across its starts; half a pair is another manager's.
"""

import json
import re
from dataclasses import asdict, dataclass

from emosa.backends.base import Snapshot, SubmitResult
from emosa.clock import utc_now
from emosa.errors import EmosaError, Reason
from emosa.model import Observation
from emosa.opensync.mapping import check_results, where_uuid
from emosa.opensync.uplink import start_instance

MODE = "opensync-6.6-wired-uplink"
PROVENANCE = "opensync-cm:Connection_Manager_Uplink"
WIRED = "ethernet"
INTERFACE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
# What the scope monitors beyond the AP scope's tables (emosa.opensync.schema.TABLES); a legacy
# pod's pair is read from its bridges' ports
MONITOR = {
    "Connection_Manager_Uplink": ["if_name", "if_type", "is_used", "bridge"],
    "Bridge": ["name", "ports"],
    "Port": ["name", "interfaces"],
}
# OpenSync's patch pair (cm2_net.c: a pod with patch-w2h is a bridge): the WAN bridge's end, the
# home bridge's end
PATCH_WAN, PATCH_HOME = "patch-w2h", "patch-h2w"


@dataclass(frozen=True)
class WiredIntent:
    """Bridge the pod's Ethernet uplink ``port`` into ``bridge``."""

    pod_id: str
    port: str
    bridge: str

    def record(self):
        return asdict(self)

    def validate(self):
        if not isinstance(self.port, str) or not INTERFACE.match(self.port):
            raise EmosaError(Reason.INVALID_INPUT, "uplink port must be an interface name")
        if not isinstance(self.bridge, str) or not INTERFACE.match(self.bridge):
            raise EmosaError(Reason.INVALID_INPUT, "bridge must be an interface name")

    def target(self, vault=None):
        self.validate()
        return {"port": self.port, "uplink": True, "bridge": self.bridge}


def uplink_row(decoded, port):
    """The port's Connection_Manager_Uplink row as (uuid, row), or None."""
    rows = [
        (u, r)
        for u, r in decoded.get("Connection_Manager_Uplink", {}).items()
        if r.get("if_name") == port
    ]
    return rows[0] if len(rows) == 1 else None


def in_use(row):
    return row is not None and row.get("if_type") == "eth" and row.get("is_used") is True


def bridge_ports(decoded, name):
    """(uuid, the Port names in it) of the one Bridge `name`, or None."""
    bridges = [(u, r) for u, r in decoded.get("Bridge", {}).items() if r.get("name") == name]
    if len(bridges) != 1:
        return None
    ports = decoded.get("Port", {})
    uuid, row = bridges[0]
    return uuid, {ports[p].get("name") for p in row.get("ports") or [] if p in ports}


def port_names(decoded):
    return {r.get("name") for r in decoded.get("Port", {}).values()}


def paired(decoded, wan, home):
    """OpenSync's patch pair between the bridges `wan` and `home`: "paired" (both ends in their
    bridges), "none" (neither Port exists) or "other" (half a pair, or ends elsewhere)."""
    w, h = bridge_ports(decoded, wan), bridge_ports(decoded, home)
    if w and h and PATCH_WAN in w[1] and PATCH_HOME in h[1]:
        return "paired"
    names = port_names(decoded)
    return "none" if PATCH_WAN not in names and PATCH_HOME not in names else "other"


def pair_ops(wan, home):
    """The patch pair's operations and their result counts (spec 8.4): no Port of either name,
    the two patch Interfaces (each the other's peer) and their Ports, each Port into its bridge
    (`wan`'s patch-w2h, `home`'s patch-h2w)."""
    ops = [
        {
            "op": "wait",
            "table": "Port",
            "where": [["name", "==", name]],
            "columns": ["name"],
            "until": "==",
            "rows": [],
            "timeout": 0,
        }
        for name in (PATCH_WAN, PATCH_HOME)
    ]
    for name, peer, tag in ((PATCH_WAN, PATCH_HOME, "w"), (PATCH_HOME, PATCH_WAN, "h")):
        ops.append(
            {
                "op": "insert",
                "table": "Interface",
                "uuid-name": f"patch_{tag}_if",
                "row": {"name": name, "type": "patch", "options": ["map", [["peer", peer]]]},
            }
        )
    for name, tag in ((PATCH_WAN, "w"), (PATCH_HOME, "h")):
        ops.append(
            {
                "op": "insert",
                "table": "Port",
                "uuid-name": f"patch_{tag}_port",
                "row": {"name": name, "interfaces": ["set", [["named-uuid", f"patch_{tag}_if"]]]},
            }
        )
    for bridge_name, tag in ((wan, "w"), (home, "h")):
        ops.append(
            {
                "op": "mutate",
                "table": "Bridge",
                "where": [["name", "==", bridge_name]],
                "mutations": [["ports", "insert", ["set", [["named-uuid", f"patch_{tag}_port"]]]]],
            }
        )
    return ops, [None] * 6 + [1, 1]


class WiredBackend:
    """The wired uplink scope of one pod, bound by serial. legacy (spec 3.10): the pod's bridge
    mode is OpenSync's patch pair between `wan_bridge` and the intent's bridge."""

    mode = MODE

    def __init__(
        self, pod_id, session, *, serial, port, legacy=False, wan_bridge="br-wan", home_bridge=None
    ):
        self.pod_id, self.session = pod_id, session
        self.expected_serial, self.port = serial, port
        # legacy: the pair joins wan_bridge to home_bridge (the intent's bridge)
        self.legacy, self.wan_bridge, self.home_bridge = legacy, wan_bridge, home_bridge
        self.instance = None
        self.last = None
        self.write_count = 0

    def _paired(self, decoded, home):
        """The legacy pod's pair: "paired" (both ends in their bridges), "none" (neither Port
        exists) or "other" (anything else: half a pair, or ends in other bridges)."""
        return paired(decoded, self.wan_bridge, home)

    def _decode(self, raw):
        schema = raw["schema"]
        return {
            t: {u: schema.row(t, row) for u, row in rows.items()}
            for t, rows in raw["tables"].items()
        }

    def _binding(self, raw):
        decoded = self._decode(raw)
        nodes = decoded.get("AWLAN_Node", {})
        if len(nodes) != 1 or next(iter(nodes.values())).get("serial_number") != (
            self.expected_serial
        ):
            raise EmosaError(Reason.NOT_READY, "pod identity absent or not the bound serial")
        self.instance = start_instance(decoded)
        return decoded, next(iter(nodes))

    def _configured(self, decoded):
        found = uplink_row(decoded, self.port)
        row = found[1] if found else None
        if self.legacy:  # the bridge the pair joins the WAN bridge to, while the pair is there
            home = self.home_bridge
            bridge = home if home and self._paired(decoded, home) == "paired" else None
        else:
            bridge = (row or {}).get("bridge") or None
        return {"port": self.port, "uplink": in_use(row), "bridge": bridge}

    async def snapshot(self):
        try:
            raw = await self.session.snapshot()
            decoded, _ = self._binding(raw)
            configured = self._configured(decoded)
            # cm's own table: it shows the bridge it was given; the port in the bridge
            # is the pod's clients reaching the LAN, observed as their traffic.
            self.last = Snapshot(
                configured,
                Observation(
                    self.pod_id,
                    "wired-uplink",
                    dict(configured),
                    "ovsdb",
                    self.mode,
                    raw["generation"],
                    utc_now(),
                    bool(raw["ready"]),
                    PROVENANCE,
                    revision=raw["revision"],
                ),
                bool(raw["ready"]),
                raw["generation"],
                raw["schema"].fingerprint,
            )
        except (EmosaError, ConnectionError, TimeoutError):
            if self.last is None:
                raise
            self.last.ready = False
            self.last.observed.fresh = False
        return self.last

    def _check(self, intent, decoded=None):
        intent.target()
        if intent.pod_id != self.pod_id or intent.port != self.port:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "request exceeds the bound pod")
        if decoded is None:
            return None
        found = uplink_row(decoded, self.port)
        if not found or not in_use(found[1]):
            raise EmosaError(Reason.NOT_READY, "the port is not the pod's Ethernet uplink in use")
        if self.legacy:
            if not bridge_ports(decoded, self.wan_bridge) or not bridge_ports(
                decoded, intent.bridge
            ):
                raise EmosaError(Reason.NOT_READY, "the pod's WAN or home bridge is absent")
            if self._paired(decoded, intent.bridge) == "other":
                # half a pair, or its ends elsewhere: another manager's, not taken over
                raise EmosaError(Reason.OWNERSHIP_CONFLICT, "another patch pair is in place")
            return found
        current = found[1].get("bridge") or None
        if current is not None and current != intent.bridge:
            # Another manager's bridge (e.g. the operator's cloud): not taken over.
            raise EmosaError(Reason.OWNERSHIP_CONFLICT, "the uplink is in another bridge")
        return found

    async def plan(self, intent):
        self._check(intent)
        raw = await self.session.snapshot()
        decoded, _ = self._binding(raw)
        self._check(intent, decoded)
        if not raw["ready"]:
            raise EmosaError(Reason.NOT_READY, "the pod's database is not ready")
        if self.legacy:
            return {
                "mapping": MODE,
                "action": "wired-uplink-patch-pair",
                "port": intent.port,
                "bridge": intent.bridge,
                "wan_bridge": self.wan_bridge,
                "instance": self.instance,
                "fields": ["Interface", "Port", "Bridge.ports"],
                "guard": "pod serial, the uplink row's if_name, if_type and is_used, no pair",
            }
        return {
            "mapping": MODE,
            "action": "wired-uplink-bridge",
            "port": intent.port,
            "bridge": intent.bridge,
            "instance": self.instance,
            "fields": ["Connection_Manager_Uplink.bridge"],
            "guard": "pod serial and the uplink row's if_name, if_type, is_used and bridge",
        }

    def _pair_transaction(self, node_uuid, row_uuid, bridge):
        """The legacy pod's patch pair (spec 8.4): the guards, the two Interfaces and Ports, and
        each Port into its bridge."""
        transaction = [
            {
                "op": "wait",
                "table": "AWLAN_Node",
                "where": where_uuid(node_uuid),
                "columns": ["serial_number"],
                "until": "==",
                "rows": [{"serial_number": self.expected_serial}],
                "timeout": 0,
            },
            {
                "op": "wait",
                "table": "Connection_Manager_Uplink",
                "where": where_uuid(row_uuid),
                "columns": ["if_name", "if_type", "is_used"],
                "until": "==",
                "rows": [{"if_name": self.port, "if_type": "eth", "is_used": True}],
                "timeout": 0,
            },
        ]
        ops, counts = pair_ops(self.wan_bridge, bridge)
        return transaction + ops, [None, None, *counts]

    async def submit(self, intent, attempt):
        await self.plan(intent)
        raw = await self.session.snapshot()
        decoded, node_uuid = self._binding(raw)
        if raw["generation"] != attempt["session_generation"]:
            return SubmitResult("rejected", {}, Reason.NOT_READY)
        row_uuid, row = self._check(intent, decoded)
        if self.legacy:
            transaction, counts = self._pair_transaction(node_uuid, row_uuid, intent.bridge)
            return await self._send(raw, attempt, transaction, counts, "wired-uplink-patch-pair")
        current = row.get("bridge") or None
        transaction = [
            {
                "op": "wait",
                "table": "AWLAN_Node",
                "where": where_uuid(node_uuid),
                "columns": ["serial_number"],
                "until": "==",
                "rows": [{"serial_number": self.expected_serial}],
                "timeout": 0,
            },
            {
                "op": "wait",
                "table": "Connection_Manager_Uplink",
                "where": where_uuid(row_uuid),
                "columns": ["if_name", "if_type", "is_used", "bridge"],
                "until": "==",
                "rows": [
                    {
                        "if_name": self.port,
                        "if_type": "eth",
                        "is_used": True,
                        "bridge": current if current is not None else ["set", []],
                    }
                ],
                "timeout": 0,
            },
            {
                "op": "update",
                "table": "Connection_Manager_Uplink",
                "where": where_uuid(row_uuid),
                "row": {"bridge": intent.bridge},
            },
        ]
        return await self._send(raw, attempt, transaction, [None, None, 1], "wired-uplink-bridge")

    async def _send(self, raw, attempt, transaction, counts, action):
        """Send the guarded transaction and judge its reply."""
        self.write_count += 1
        try:
            results = await self.session.transact(
                transaction, attempt["transaction_id"], attempt["session_generation"]
            )
            check_results(results, counts)
        except (ConnectionError, TimeoutError):
            return SubmitResult("unknown", {"attribution": "unknown"}, Reason.OUTCOME_UNKNOWN)
        except EmosaError as exc:
            if exc.code == Reason.PRECONDITION_FAILED:
                return SubmitResult("conflict", {}, exc.code)
            return SubmitResult(
                "unknown" if exc.code == Reason.OUTCOME_UNKNOWN else "rejected", {}, exc.code
            )
        return SubmitResult(
            "committed",
            {
                "attribution": "reply",
                "transaction_validated": True,
                "transaction_id": attempt["transaction_id"],
                "session_generation": raw["generation"],
                "action": action,
                "instance": self.instance,
                "results": json.loads(json.dumps(results, default=str)),
            },
            None,
        )
