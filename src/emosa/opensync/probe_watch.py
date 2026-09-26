"""Watching stations' probe requests on the pod (spec §3.9).

OpenSync 6.6's ``owm`` records a probe request, with its SNR, only for a
station that has a ``Band_Steering_Clients`` row, and reports it in the
band-steering report only for the VIFs of a ``Band_Steering_Config`` group
(``ow_steer_bm``, every 60 s). To hear the stations the controller asks about,
the agent keeps a **watch row** for each of them:

- the complete client row ``owm`` reads, with client steering off
  (``cs_mode`` ``off``), no kick, no probe blocking, no band preference, and
  without ``sticky_kick_type`` (optional; ``none`` makes ``owm`` warn);
- ``cs_params`` ``{"emosa": "watch"}``: ``owm`` ignores the key, and it marks
  the row as EMOSA's watch row on the pod itself, across agent restarts.

One guarded transaction makes the pod's watch rows equal to the requested
set: it inserts the missing rows (only where the station has no row at all)
and deletes watch rows no longer requested, by UUID. The group on the
fronthaul VIF is reused when present and inserted otherwise; it stays in
place (another scope, client steering, may use it). A station with any other
client row belongs to another manager (or a steering window) and is not
watched: the snapshot lists it as ``blocked``.
"""

import re
from dataclasses import asdict, dataclass

from emosa.backends.base import Snapshot, SubmitResult
from emosa.clock import utc_now
from emosa.errors import EmosaError, Reason
from emosa.model import Observation
from emosa.opensync.mapping import check_results, where_uuid
from emosa.opensync.steering import CLIENT_ROW, GROUP_COLUMN, WATCH_MARKER, is_watch_row, pairs
from emosa.opensync.uplink import start_instance

MODE = "opensync-6.6-probe-watch"
PROVENANCE = "owm:Band_Steering_Clients(cs_params emosa=watch)"
MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
MARKER = WATCH_MARKER
MAX_WATCHED = 32
# What the scope monitors beyond the AP scope's tables (emosa.opensync.schema.TABLES).
MONITOR = {
    "Band_Steering_Config": ["if_name_2g", "if_name_5g"],
    "Band_Steering_Clients": ["mac", "cs_mode", "cs_params"],
}
WATCH_ROW = {
    **{k: v for k, v in CLIENT_ROW.items() if k != "sticky_kick_type"},
    "cs_mode": "off",
    "cs_params": pairs(MARKER),
}


@dataclass(frozen=True)
class WatchIntent:
    """Keep watch rows on the pod for exactly ``stations`` (group on ``if_name``)."""

    pod_id: str
    if_name: str
    band: str
    stations: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "stations", tuple(sorted(self.stations)))

    def record(self):
        return {**asdict(self), "stations": list(self.stations)}

    def validate(self):
        if not self.if_name or self.band not in GROUP_COLUMN:
            raise EmosaError(Reason.INVALID_INPUT, "watch VIF or band invalid")
        if len(self.stations) > MAX_WATCHED:
            raise EmosaError(Reason.INVALID_INPUT, "too many watched stations")
        if len(set(self.stations)) != len(self.stations) or not all(
            isinstance(s, str) and MAC.match(s) for s in self.stations
        ):
            raise EmosaError(Reason.INVALID_INPUT, "watched stations must be lower-case MACs")

    def target(self, vault=None):
        self.validate()
        return {"watched": ",".join(self.stations)}


class WatchBackend:
    """The probe-watch scope of one pod, bound by serial."""

    mode = MODE

    def __init__(self, pod_id, session, *, serial):
        self.pod_id, self.session, self.expected_serial = pod_id, session, serial
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

    @staticmethod
    def rows(decoded):
        """(watch rows mac -> UUID, MACs with any other client row)."""
        watch, blocked = {}, set()
        for uuid, row in decoded.get("Band_Steering_Clients", {}).items():
            if is_watch_row(row) and row.get("mac") not in watch:
                watch[row.get("mac")] = uuid
            else:
                blocked.add(row.get("mac"))
        return watch, blocked

    async def snapshot(self):
        try:
            raw = await self.session.snapshot()
            decoded, _ = self._binding(raw)
            watch, blocked = self.rows(decoded)
            associated = {r.get("mac") for r in decoded.get("Wifi_Associated_Clients", {}).values()}
            config = {
                "watched": ",".join(sorted(watch)),
                "blocked": sorted(blocked),
                "associated": sorted(m for m in associated if m),
            }
            self.last = Snapshot(
                config,
                Observation(
                    self.pod_id,
                    "probe-watch",
                    {"watched": config["watched"]},
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

    async def plan(self, intent):
        intent.target()
        if intent.pod_id != self.pod_id:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "request exceeds the bound pod")
        raw = await self.session.snapshot()
        decoded, _ = self._binding(raw)
        if not raw["ready"]:
            raise EmosaError(Reason.NOT_READY, "the pod's database is not ready")
        if not any(
            v.get("if_name") == intent.if_name and v.get("mode") == "ap"
            for v in decoded.get("Wifi_VIF_State", {}).values()
        ):
            raise EmosaError(Reason.NOT_READY, "the watch VIF is not an AP VIF of the pod")
        column = GROUP_COLUMN[intent.band]
        groups = [
            u
            for u, r in decoded.get("Band_Steering_Config", {}).items()
            if intent.if_name in (r.get("if_name_2g"), r.get("if_name_5g"))
        ]
        if len(groups) > 1:
            raise EmosaError(Reason.OWNERSHIP_CONFLICT, "ambiguous steering group")
        watch, blocked = self.rows(decoded)
        if blocked & set(intent.stations):
            raise EmosaError(Reason.OWNERSHIP_CONFLICT, "a watched station has another client row")
        wanted = set(intent.stations)
        return {
            "mapping": MODE,
            "action": "probe-watch",
            "group_column": column,
            "group": groups[0] if groups else None,
            "add": sorted(wanted - set(watch)),
            "remove": {m: u for m, u in sorted(watch.items()) if m not in wanted},
            "instance": self.instance,
            "fields": ["Band_Steering_Config", "Band_Steering_Clients"],
            "guard": "pod serial; no client row for an added station; removed rows as seen",
        }

    async def submit(self, intent, attempt):
        plan = await self.plan(intent)
        raw = await self.session.snapshot()
        _, node_uuid = self._binding(raw)
        if raw["generation"] != attempt["session_generation"]:
            return SubmitResult("rejected", {}, Reason.NOT_READY)
        column = plan["group_column"]

        def wait(table, where, columns, rows):
            return {
                "op": "wait",
                "table": table,
                "where": where,
                "columns": columns,
                "until": "==",
                "rows": rows,
                "timeout": 0,
            }

        transaction = [
            wait(
                "AWLAN_Node",
                where_uuid(node_uuid),
                ["serial_number"],
                [{"serial_number": self.expected_serial}],
            )
        ]
        counts = [None]
        if plan["group"]:
            transaction.append(
                wait(
                    "Band_Steering_Config",
                    where_uuid(plan["group"]),
                    [column],
                    [{column: intent.if_name}],
                )
            )
            counts.append(None)
        else:
            transaction += [
                wait("Band_Steering_Config", [["if_name_2g", "==", intent.if_name]], ["_uuid"], []),
                wait("Band_Steering_Config", [["if_name_5g", "==", intent.if_name]], ["_uuid"], []),
                {"op": "insert", "table": "Band_Steering_Config", "row": {column: intent.if_name}},
            ]
            counts += [None, None, None]
        for mac, uuid in plan["remove"].items():
            transaction += [
                wait(
                    "Band_Steering_Clients",
                    where_uuid(uuid),
                    ["mac", "cs_mode"],
                    [{"mac": mac, "cs_mode": "off"}],
                ),
                {"op": "delete", "table": "Band_Steering_Clients", "where": where_uuid(uuid)},
            ]
            counts += [None, 1]
        for mac in plan["add"]:
            transaction += [
                wait("Band_Steering_Clients", [["mac", "==", mac]], ["_uuid"], []),
                {
                    "op": "insert",
                    "table": "Band_Steering_Clients",
                    "row": {**WATCH_ROW, "mac": mac},
                },
            ]
            counts += [None, None]
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
                return SubmitResult("rejected", {}, exc.code)
            return SubmitResult(
                "unknown" if exc.code == Reason.OUTCOME_UNKNOWN else "rejected", {}, exc.code
            )
        return SubmitResult(
            "committed",
            {
                "attribution": "reply",
                "transaction_validated": True,
                "transaction_id": attempt["transaction_id"],
                "added": plan["add"],
                "removed": sorted(plan["remove"]),
            },
        )
