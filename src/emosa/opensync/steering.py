"""Client steering on an OpenSync 6.6 pod as an EMOSA scope (the steering scope).

``owm``'s band steering steers one station away from its BSS when it has:

- a ``Band_Steering_Config`` row naming the station's VIF (``if_name_2g`` or
  ``if_name_5g``): the steering group;
- a ``Wifi_VIF_Neighbors`` row for the target on that VIF: the BTM candidate;
- the station's ``Band_Steering_Clients`` row with client steering "away"
  (``cs_mode``, ``cs_params.cs_enforce_period``), a BTM-then-deauth kick
  (``sc_kick_type`` ``btm_deauth``) and its BTM parameters
  (``sc_btm_params``: ``bssid``, ``disassoc_imminent``, the only keys it reads).

The window opens with one guarded transaction: the pod is the bound serial, no
other client row exists for the station (another manager's steering is never
taken over), and the group and neighbor rows are reused when present or
inserted when absent. It is applied when ``owm`` reports ``cs_state``
``steering`` for the row. The kick is then a second guarded write, a one-shot
``force_kick`` ``directed`` while ``cs_state`` is still ``steering``. Closing
deletes exactly the rows the opening inserted, by UUID; rows another manager
owned before are left alone.

The client row is written complete: ``owm`` reads its fields unchecked and a
partial row crashed it (seen on 6.6.1).
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

MODE = "opensync-6.6-client-steering"
PROVENANCE = "owm:Band_Steering_Clients.cs_state"
MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
WINDOW = (5, 120)  # cs_enforce_period bounds, seconds
# What the scope monitors beyond the AP scope's tables (emosa.opensync.schema.TABLES).
MONITOR = {
    "Band_Steering_Config": ["if_name_2g", "if_name_5g"],
    "Band_Steering_Clients": [
        "mac",
        "cs_mode",
        "cs_state",
        "force_kick",
        "sc_kick_type",
        "sc_btm_params",
        "steering_kick_cnt",
    ],
    "Wifi_VIF_Neighbors": ["bssid", "if_name", "channel", "op_class"],
}
GROUP_COLUMN = {"2.4G": "if_name_2g", "5G": "if_name_5g", "5GL": "if_name_5g", "5GU": "if_name_5g"}
# Every field owm dereferences; the steering fields are added per request.
CLIENT_ROW = {
    "hwm": 0,
    "lwm": 0,
    "bottom_lwm": 0,
    "kick_type": "none",
    "kick_reason": 0,
    "reject_detection": "none",
    "max_rejects": 0,
    "rejects_tmout_secs": 0,
    "backoff_secs": 0,
    "pref_5g_pre_assoc_block_timeout_msecs": 0,
    "pref_6g_pre_assoc_block_timeout_msecs": 0,
    "kick_debounce_period": 0,
    "sc_kick_reason": 0,
    "sc_kick_debounce_period": 0,
    "sticky_kick_debounce_period": 0,
    "sticky_kick_reason": 0,
    "sticky_kick_type": "none",
    "pref_5g": "never",
    "pref_bs_allowed": "never",
    "pref_6g": "never",
    "kick_upon_idle": False,
    "pre_assoc_auth_block": False,
    "send_rrm_after_assoc": False,
    "neighbor_list_filter_by_beacon_report": False,
    "steering_success_cnt": 0,
    "steering_fail_cnt": 0,
    "steering_kick_cnt": 0,
    "sticky_kick_cnt": 0,
}


def ht_mode(op_class):
    """The neighbor's channel width from its global operating class (Table E-4)."""
    if op_class in (83, 84, 116, 117, 119, 120, 122, 123, 126, 127):
        return "HT40"
    if op_class in (128, 133):
        return "HT80"
    if op_class in (129, 134):
        return "HT160"
    return "HT20"


@dataclass(frozen=True)
class SteeringIntent:
    """Steer ``station`` from the pod's BSS ``source_bssid`` to ``target_bssid``."""

    pod_id: str
    station: str
    source_bssid: str
    target_bssid: str
    op_class: int
    channel: int
    disassoc_imminent: bool
    window: int
    request_mid: int

    def record(self):
        return asdict(self)

    def validate(self):
        for value in (self.station, self.source_bssid, self.target_bssid):
            if not isinstance(value, str) or not MAC.match(value):
                raise EmosaError(Reason.INVALID_INPUT, "addresses must be lower-case MACs")
        if self.target_bssid == self.source_bssid:
            raise EmosaError(Reason.INVALID_INPUT, "the target is the source BSS")
        if not 0 < self.op_class < 256 or not 0 < self.channel < 234:
            raise EmosaError(Reason.INVALID_INPUT, "target operating class or channel invalid")
        if not WINDOW[0] <= self.window <= WINDOW[1]:
            raise EmosaError(Reason.INVALID_INPUT, "steering window out of range")

    def steering(self):
        """The observed value of an applied window: the target, while owm steers."""
        return f"{self.target_bssid}/steering"

    def target(self, vault=None):
        self.validate()
        return {self.station: self.steering()}

    def btm_params(self):
        return {"bssid": self.target_bssid, "disassoc_imminent": str(int(self.disassoc_imminent))}


def pairs(value):
    return ["map", [[k, v] for k, v in sorted(value.items())]]


class SteeringBackend:
    """The steering scope of one pod, bound by serial."""

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
    def windows(decoded):
        """station -> "<target>/<cs_state>" for every away-steering client row."""
        result = {}
        for row in decoded.get("Band_Steering_Clients", {}).values():
            if row.get("cs_mode") == "away" and row.get("sc_kick_type") == "btm_deauth":
                target = (row.get("sc_btm_params") or {}).get("bssid")
                result[row.get("mac")] = f"{target}/{row.get('cs_state') or 'none'}"
        return result

    @staticmethod
    def source(decoded, bssid):
        """(if_name, group column, station MACs) of the pod's AP VIF ``bssid``, or None."""
        for vif_uuid, vif in decoded.get("Wifi_VIF_State", {}).items():
            if vif.get("mac") != bssid or vif.get("mode") != "ap" or not vif.get("enabled"):
                continue
            band = next(
                (
                    r.get("freq_band")
                    for r in decoded.get("Wifi_Radio_State", {}).values()
                    if vif_uuid in (r.get("vif_states") or [])
                ),
                None,
            )
            clients = decoded.get("Wifi_Associated_Clients", {})
            stations = {
                clients[u].get("mac") for u in (vif.get("associated_clients") or []) if u in clients
            }
            return vif.get("if_name"), GROUP_COLUMN.get(band), stations
        return None

    async def snapshot(self):
        try:
            raw = await self.session.snapshot()
            decoded, _ = self._binding(raw)
            windows = self.windows(decoded)
            self.last = Snapshot(
                windows,
                Observation(
                    self.pod_id,
                    "client-steering",
                    dict(windows),
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

    async def observe(self, station, source_bssid):
        """(cs_state or None, whether the station is still on the source BSS)."""
        raw = await self.session.snapshot()
        decoded, _ = self._binding(raw)
        rows = [
            r for r in decoded.get("Band_Steering_Clients", {}).values() if r.get("mac") == station
        ]
        found = self.source(decoded, source_bssid)
        return (
            rows[0].get("cs_state") if len(rows) == 1 else None,
            found is not None and station in found[2],
        )

    def _existing(self, decoded, intent, if_name):
        groups = [
            u
            for u, r in decoded.get("Band_Steering_Config", {}).items()
            if if_name in (r.get("if_name_2g"), r.get("if_name_5g"))
        ]
        neighbors = [
            u
            for u, r in decoded.get("Wifi_VIF_Neighbors", {}).items()
            if r.get("bssid") == intent.target_bssid and r.get("if_name") == if_name
        ]
        clients = [
            u
            for u, r in decoded.get("Band_Steering_Clients", {}).items()
            if r.get("mac") == intent.station
        ]
        return groups, neighbors, clients

    async def plan(self, intent):
        intent.target()
        if intent.pod_id != self.pod_id:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "request exceeds the bound pod")
        raw = await self.session.snapshot()
        decoded, _ = self._binding(raw)
        if not raw["ready"]:
            raise EmosaError(Reason.NOT_READY, "the pod's database is not ready")
        found = self.source(decoded, intent.source_bssid)
        if found is None:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "the source is not an AP BSS of the pod")
        if_name, column, stations = found
        if column is None:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "the source radio's band has no group")
        if intent.station not in stations:
            raise EmosaError(Reason.NOT_READY, "the station is not associated with the source")
        groups, neighbors, clients = self._existing(decoded, intent, if_name)
        if clients:
            # The station's steering belongs to another manager (or an earlier window).
            raise EmosaError(Reason.OWNERSHIP_CONFLICT, "the station has a steering row already")
        if len(groups) > 1 or len(neighbors) > 1:
            raise EmosaError(Reason.OWNERSHIP_CONFLICT, "ambiguous steering group or neighbor")
        return {
            "mapping": MODE,
            "action": "client-steering-window",
            "source_if": if_name,
            "group_column": column,
            "group": groups[0] if groups else None,
            "neighbor": neighbors[0] if neighbors else None,
            "instance": self.instance,
            "fields": ["Band_Steering_Config", "Wifi_VIF_Neighbors", "Band_Steering_Clients"],
            "guard": "pod serial; no steering row for the station; group and neighbor as seen",
        }

    async def submit(self, intent, attempt):
        plan = await self.plan(intent)
        raw = await self.session.snapshot()
        decoded, node_uuid = self._binding(raw)
        if raw["generation"] != attempt["session_generation"]:
            return SubmitResult("rejected", {}, Reason.NOT_READY)
        if_name, column = plan["source_if"], plan["group_column"]

        def absent(table, where):
            return {
                "op": "wait",
                "table": table,
                "where": where,
                "columns": ["_uuid"],
                "until": "==",
                "rows": [],
                "timeout": 0,
            }

        def present(table, row_id, columns, row):
            return {
                "op": "wait",
                "table": table,
                "where": where_uuid(row_id),
                "columns": columns,
                "until": "==",
                "rows": [{c: row[c] for c in columns}],
                "timeout": 0,
            }

        transaction = [
            present(
                "AWLAN_Node", node_uuid, ["serial_number"], {"serial_number": self.expected_serial}
            ),
            absent("Band_Steering_Clients", [["mac", "==", intent.station]]),
        ]
        counts, created = [None, None], []
        if plan["group"]:
            transaction.append(
                present("Band_Steering_Config", plan["group"], [column], {column: if_name})
            )
            counts.append(None)
        else:
            transaction += [
                absent("Band_Steering_Config", [["if_name_2g", "==", if_name]]),
                absent("Band_Steering_Config", [["if_name_5g", "==", if_name]]),
                {"op": "insert", "table": "Band_Steering_Config", "row": {column: if_name}},
            ]
            counts += [None, None, None]
            created.append(("group", len(transaction) - 1))
        neighbor_where = [["bssid", "==", intent.target_bssid], ["if_name", "==", if_name]]
        if plan["neighbor"]:
            transaction.append(
                present(
                    "Wifi_VIF_Neighbors",
                    plan["neighbor"],
                    ["bssid", "if_name"],
                    {"bssid": intent.target_bssid, "if_name": if_name},
                )
            )
            counts.append(None)
        else:
            transaction += [
                absent("Wifi_VIF_Neighbors", neighbor_where),
                {
                    "op": "insert",
                    "table": "Wifi_VIF_Neighbors",
                    "row": {
                        "bssid": intent.target_bssid,
                        "if_name": if_name,
                        "channel": intent.channel,
                        "op_class": intent.op_class,
                        "ht_mode": ht_mode(intent.op_class),
                        "priority": 1,
                    },
                },
            ]
            counts += [None, None]
            created.append(("neighbor", len(transaction) - 1))
        transaction.append(
            {
                "op": "insert",
                "table": "Band_Steering_Clients",
                "row": {
                    **CLIENT_ROW,
                    "mac": intent.station,
                    "cs_mode": "away",
                    "cs_params": pairs({"cs_enforce_period": str(intent.window)}),
                    "sc_kick_type": "btm_deauth",
                    "sc_btm_params": pairs(intent.btm_params()),
                },
            }
        )
        counts.append(None)
        created.append(("client", len(transaction) - 1))
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
        rows = {name: results[index]["uuid"][1] for name, index in created}
        return SubmitResult(
            "committed",
            {
                "attribution": "reply",
                "transaction_validated": True,
                "transaction_id": attempt["transaction_id"],
                "session_generation": raw["generation"],
                "action": "client-steering-window",
                "instance": self.instance,
                "source_if": if_name,
                "created": rows,
                "results": json.loads(json.dumps(results, default=str)),
            },
            None,
        )

    async def kick(self, station, client_row):
        """The one-shot directed kick, only while owm still steers the station."""
        raw = await self.session.snapshot()
        _, node_uuid = self._binding(raw)
        results = await self.session.transact(
            [
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
                    "op": "update",
                    "table": "Band_Steering_Clients",
                    "where": [
                        *where_uuid(client_row),
                        ["mac", "==", station],
                        ["cs_state", "==", "steering"],
                    ],
                    "row": {"force_kick": "directed"},
                },
            ]
        )
        check_results(results, [None, 1])

    async def close(self, intent, created):
        """Delete the rows the window created (by UUID, with their identifying columns).

        Idempotent: a row already gone (closed before, or the pod restarted from
        its template) matches nothing.
        """
        operations = []
        if created.get("client"):
            operations.append(
                {
                    "op": "delete",
                    "table": "Band_Steering_Clients",
                    "where": [*where_uuid(created["client"]), ["mac", "==", intent.station]],
                }
            )
        if created.get("neighbor"):
            operations.append(
                {
                    "op": "delete",
                    "table": "Wifi_VIF_Neighbors",
                    "where": [
                        *where_uuid(created["neighbor"]),
                        ["bssid", "==", intent.target_bssid],
                    ],
                }
            )
        if created.get("group"):
            operations.append(
                {
                    "op": "delete",
                    "table": "Band_Steering_Config",
                    "where": where_uuid(created["group"]),
                }
            )
        if not operations:
            return 0
        results = await self.session.transact(operations)
        check_results(results, [None] * len(operations))
        return sum(r.get("count", 0) for r in results)
