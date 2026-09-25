"""The pod's statistics publishing as an EMOSA scope (the telemetry scope).

OpenSync 6.6 publishes its statistics when ``AWLAN_Node.mqtt_settings`` names
a broker (``qm`` connects with the pod's device certificate) and a
``Wifi_Stats_Config`` row asks for a report (``owm`` produces client and
survey reports for its radios). EMOSA writes both in one guarded transaction:

- ``mqtt_settings``: broker, port, the pod's topic, QoS 0, no compression;
- one raw client report for the configured radio type, at the reporting and
  sampling intervals.

The database starts from its template on every OpenSync start, so the write is
made once per start (``start_instance``), like the uplink switch. A broker
another manager configured (for example the operator's cloud) is not taken
over: that write is refused, and the pod's statistics keep going where they go.

Applied means the configuration is in the pod's database on the same start.
Whether reports then arrive is the subscriber's observation
(``emosa.opensync.stats``), reported beside it.
"""

import json
from dataclasses import asdict, dataclass

from emosa.backends.base import Snapshot, SubmitResult
from emosa.clock import utc_now
from emosa.errors import EmosaError, Reason
from emosa.model import Observation
from emosa.opensync.mapping import check_results, where_uuid
from emosa.opensync.uplink import start_instance

MODE = "opensync-6.6-telemetry"
PROVENANCE = "opensync:AWLAN_Node.mqtt_settings+Wifi_Stats_Config"
RADIO_TYPES = ("2.4G", "5G", "5GL", "5GU", "6G")
# What the scope monitors beyond the AP scope's tables (emosa.opensync.schema.TABLES).
MONITOR = {
    "AWLAN_Node": ["mqtt_settings"],
    "Wifi_Stats_Config": [
        "stats_type",
        "radio_type",
        "report_type",
        "reporting_interval",
        "sampling_interval",
    ],
}


@dataclass(frozen=True)
class TelemetryIntent:
    """Have the pod publish raw client reports for ``radio_type`` to ``broker``/``topic``."""

    pod_id: str
    broker: str
    port: int
    topic: str
    radio_type: str = "2.4G"
    reporting_interval: int = 10
    sampling_interval: int = 5

    def record(self):
        return asdict(self)

    def validate(self):
        if not self.broker or any(c in self.broker for c in " /:") or len(self.broker) > 253:
            raise EmosaError(Reason.INVALID_INPUT, "broker must be a host name or address")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise EmosaError(Reason.INVALID_INPUT, "broker port out of range")
        if not self.topic or len(self.topic) > 128 or any(c in self.topic for c in "#+\0"):
            raise EmosaError(Reason.INVALID_INPUT, "topic must be a plain topic name")
        if self.radio_type not in RADIO_TYPES:
            raise EmosaError(Reason.INVALID_INPUT, "unknown radio type")
        if not 1 <= self.sampling_interval <= self.reporting_interval <= 3600:
            raise EmosaError(Reason.INVALID_INPUT, "sampling must not exceed reporting interval")

    def settings(self):
        return {
            "broker": self.broker,
            "port": str(self.port),
            "topics": self.topic,
            "qos": "0",
            "compress": "none",
        }

    def client_stats(self):
        return f"{self.radio_type}/raw/{self.reporting_interval}/{self.sampling_interval}"

    def target(self, vault=None):
        self.validate()
        return {"mqtt": self.settings(), "client_stats": self.client_stats()}


def client_rows(decoded, radio_type):
    return [
        (u, r)
        for u, r in decoded.get("Wifi_Stats_Config", {}).items()
        if r.get("stats_type") == "client" and r.get("radio_type") == radio_type
    ]


def row_key(row):
    return (
        f"{row.get('radio_type')}/{row.get('report_type') or 'raw'}/"
        f"{row.get('reporting_interval')}/{row.get('sampling_interval')}"
    )


class TelemetryBackend:
    """The telemetry scope of one pod, bound by serial."""

    mode = MODE

    def __init__(self, pod_id, session, *, serial, radio_type):
        self.pod_id, self.session = pod_id, session
        self.expected_serial, self.radio_type = serial, radio_type
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
        node_uuid, node = next(iter(nodes.items()))
        return decoded, node_uuid, node

    def _configured(self, decoded, node):
        rows = client_rows(decoded, self.radio_type)
        return {
            "mqtt": dict(node.get("mqtt_settings") or {}),
            "client_stats": row_key(rows[0][1]) if len(rows) == 1 else None,
        }

    async def snapshot(self):
        try:
            raw = await self.session.snapshot()
            decoded, _, node = self._binding(raw)
            configured = self._configured(decoded, node)
            # These are configuration tables: what OpenSync applied is observed
            # through the reports themselves (emosa.opensync.stats).
            self.last = Snapshot(
                configured,
                Observation(
                    self.pod_id,
                    "telemetry",
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

    def _check(self, intent, decoded=None, node=None):
        intent.target()
        if intent.pod_id != self.pod_id or intent.radio_type != self.radio_type:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "request exceeds the bound pod")
        if node is None:
            return
        current = dict(node.get("mqtt_settings") or {})
        if current and current.get("broker") != intent.broker:
            # Another manager's broker (e.g. the operator's cloud): not taken over.
            raise EmosaError(Reason.OWNERSHIP_CONFLICT, "the pod's statistics go to another broker")
        rows = client_rows(decoded, self.radio_type)
        if len(rows) > 1 or (rows and row_key(rows[0][1]) != intent.client_stats()):
            raise EmosaError(
                Reason.OWNERSHIP_CONFLICT, "another client report is configured for this radio"
            )

    async def plan(self, intent):
        self._check(intent)
        raw = await self.session.snapshot()
        decoded, _, node = self._binding(raw)
        self._check(intent, decoded, node)
        if not raw["ready"]:
            raise EmosaError(Reason.NOT_READY, "the pod's database is not ready")
        return {
            "mapping": MODE,
            "action": "statistics-publishing",
            "broker": f"{intent.broker}:{intent.port}",
            "topic": intent.topic,
            "client_stats": intent.client_stats(),
            "instance": self.instance,
            "fields": ["AWLAN_Node.mqtt_settings", "Wifi_Stats_Config"],
            "guard": "pod serial and its current mqtt_settings",
        }

    async def submit(self, intent, attempt):
        await self.plan(intent)
        raw = await self.session.snapshot()
        decoded, node_uuid, node = self._binding(raw)
        if raw["generation"] != attempt["session_generation"]:
            return SubmitResult("rejected", {}, Reason.NOT_READY)
        current = dict(node.get("mqtt_settings") or {})
        transaction = [
            {
                "op": "wait",
                "table": "AWLAN_Node",
                "where": where_uuid(node_uuid),
                "columns": ["serial_number", "mqtt_settings"],
                "until": "==",
                "rows": [
                    {
                        "serial_number": self.expected_serial,
                        "mqtt_settings": ["map", [[k, v] for k, v in sorted(current.items())]],
                    }
                ],
                "timeout": 0,
            },
            {
                "op": "update",
                "table": "AWLAN_Node",
                "where": where_uuid(node_uuid),
                "row": {
                    "mqtt_settings": ["map", [[k, v] for k, v in sorted(intent.settings().items())]]
                },
            },
        ]
        counts = [None, 1]
        if not client_rows(decoded, self.radio_type):
            transaction.append(
                {
                    "op": "insert",
                    "table": "Wifi_Stats_Config",
                    "row": {
                        "stats_type": "client",
                        "radio_type": self.radio_type,
                        "report_type": "raw",
                        "reporting_interval": intent.reporting_interval,
                        "sampling_interval": intent.sampling_interval,
                    },
                }
            )
            counts.append(None)
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
                "action": "statistics-publishing",
                "instance": self.instance,
                "results": json.loads(json.dumps(results, default=str)),
            },
            None,
        )
