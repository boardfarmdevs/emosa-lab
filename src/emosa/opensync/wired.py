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
# What the scope monitors beyond the AP scope's tables (emosa.opensync.schema.TABLES).
MONITOR = {"Connection_Manager_Uplink": ["if_name", "if_type", "is_used", "bridge"]}


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


class WiredBackend:
    """The wired uplink scope of one pod, bound by serial."""

    mode = MODE

    def __init__(self, pod_id, session, *, serial, port):
        self.pod_id, self.session = pod_id, session
        self.expected_serial, self.port = serial, port
        self.instance = None
        self.last = None
        self.write_count = 0

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
        return {
            "port": self.port,
            "uplink": in_use(row),
            "bridge": (row or {}).get("bridge") or None,
        }

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
        return {
            "mapping": MODE,
            "action": "wired-uplink-bridge",
            "port": intent.port,
            "bridge": intent.bridge,
            "instance": self.instance,
            "fields": ["Connection_Manager_Uplink.bridge"],
            "guard": "pod serial and the uplink row's if_name, if_type, is_used and bridge",
        }

    async def submit(self, intent, attempt):
        await self.plan(intent)
        raw = await self.session.snapshot()
        decoded, node_uuid = self._binding(raw)
        if raw["generation"] != attempt["session_generation"]:
            return SubmitResult("rejected", {}, Reason.NOT_READY)
        row_uuid, row = self._check(intent, decoded)
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
        self.write_count += 1
        try:
            results = await self.session.transact(
                transaction, attempt["transaction_id"], attempt["session_generation"]
            )
            check_results(results, [None, None, 1])
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
                "action": "wired-uplink-bridge",
                "instance": self.instance,
                "results": json.loads(json.dumps(results, default=str)),
            },
            None,
        )
