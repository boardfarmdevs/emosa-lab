"""Durable selected policy receipt and conservative periodic report accounting.

EasyMesh 6.1 §§7.3, 10.2.1, 15.1, 17.1.8/32 and Tables 34, 35, 115.
An Ack confirms receipt. No measurement, policy application or reporting success
is inferred from it. The native measurement producer remains unqualified.
"""

import hashlib
import json
import sqlite3
import time

from emosa.errors import EmosaError, Reason
from emosa.wire.cmdu import fragment_message, invalid
from emosa.wire.reports import PreparedReport


def address(value):
    if len(value) != 6 or not any(value) or value[0] & 1:
        invalid("policy requires a unicast MAC address")
    return value.hex()


def addresses(data):
    if not data or len(data) < 1 + 6 * data[0]:
        invalid("truncated policy station list")
    length = 1 + 6 * data[0]
    values = [address(data[i : i + 6]) for i in range(1, length, 6)]
    if len(set(values)) != len(values):
        invalid("duplicate policy station identity")
    return values, data[length:]


def decode_policy(tlvs, ruid):
    """Decode the entire selected request before recording any policy change.

    Omitted policy TLVs leave the corresponding previous policy unchanged.
    Reserved bit fields are ignored on reception and retained in raw evidence.
    Companions EMOSA does not interpret (e.g. RDK's Default 802.1Q, Traffic
    Separation, Channel Scan Reporting and Unsuccessful Association policies, a
    vendor TLV) are recorded as received and not applied: nothing in a policy is
    applied to the pod, and the Ack confirms receipt only. Withholding the Ack
    instead makes a controller give the radio up (RDK's does).
    """
    result = {}
    for tlv in tlvs:
        data = tlv.value
        if tlv.kind == 0x8A:
            if "metrics" in result or len(data) < 2 or len(data) != 2 + data[1] * 10:
                invalid("duplicate or malformed metric reporting policy")
            if data[1] > 1:
                raise EmosaError(Reason.UNSUPPORTED_OPERATION, "sole-radio policy required")
            radios = []
            if data[1]:
                radio, rcpi, hysteresis, utilization, flags = data[2:8], *data[8:12]
                if radio != ruid or rcpi > 220:
                    invalid("foreign radio or reserved RCPI threshold")
                radios.append(
                    {
                        "ruid": address(radio),
                        "rcpi_threshold": rcpi,
                        "rcpi_hysteresis_db": hysteresis,
                        "utilization_threshold": utilization,
                        "include_traffic": bool(flags & 0x80),
                        "include_link": bool(flags & 0x40),
                        "include_wifi6_status": bool(flags & 0x20),
                    }
                )
            result["metrics"] = {"interval_seconds": data[0], "radios": radios}
        elif tlv.kind == 0x89:
            if "steering" in result:
                invalid("duplicate steering policy")
            local, data = addresses(data)
            btm, data = addresses(data)
            if not data or len(data) != 1 + 9 * data[0]:
                invalid("malformed steering radio list")
            if data[0] > 1:
                raise EmosaError(Reason.UNSUPPORTED_OPERATION, "sole-radio policy required")
            radios = []
            if data[0]:
                radio, policy, utilization, rcpi = data[1:7], *data[7:10]
                if radio != ruid or policy > 2 or rcpi > 220:
                    invalid("foreign radio or reserved steering field")
                radios.append(
                    {
                        "ruid": address(radio),
                        "policy": policy,
                        "utilization_threshold": utilization,
                        "rcpi_threshold": rcpi,
                    }
                )
            result["steering"] = {
                "local_disallowed": local,
                "btm_disallowed": btm,
                "radios": radios,
            }
        elif tlv.kind == 0xDB:
            # Table 115 permits multiple TLVs. Retain each independent pair of
            # lists; do not invent replacement/union application semantics.
            mscs, data = addresses(data)
            scs, reserved = addresses(data)
            if len(reserved) != 20:
                invalid("malformed QoS management policy reserved field")
            result.setdefault("qos", []).append({"mscs_disallowed": mscs, "scs_disallowed": scs})
        else:
            result.setdefault("not_applied", []).append({"kind": tlv.kind, "length": len(data)})
    return result


class ReportingPolicyStore:
    """One bounded policy record; persistent intent and due-work accounting.

    The caller supplies the OS boot identity because monotonic timestamps can
    survive process restart, but must be rebased after a machine reboot.

    Commits survive a process crash but are not synced to disk one by one
    (WAL, synchronous=NORMAL): the record is written twice per reporting
    period on the agent's loop, and a synced commit took up to 0.9 s on a
    loaded lab host, long enough for the pod's report lease to lapse. An OS
    crash can lose the latest accounting; the schedule is rebased after a
    reboot anyway.
    """

    def __init__(self, path, *, boot_id):
        if not isinstance(boot_id, str) or not 1 <= len(boot_id) <= 128:
            invalid("OS boot identity required for durable reporting schedule")
        self.boot_id = boot_id
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS reporting_policy "
            "(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)"
        )

    def save(self, value):
        encoded = json.dumps(value, sort_keys=True, allow_nan=False)
        if len(encoded) > 32768:
            invalid("reporting policy record exceeds budget")
        with self.db:
            self.db.execute(
                "INSERT INTO reporting_policy VALUES(1,?) "
                "ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                (encoded,),
            )

    def read(self):
        row = self.db.execute("SELECT value FROM reporting_policy WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.db.close()


class ReportingPolicyCoordinator:
    """Persist receipt and reserve due work before optional guarded transmission.

    Due intervals become persistent missing-report counts, never fake telemetry
    or a retry flood. This intentionally does not claim §10 reporting compliance.
    """

    def __init__(self, source, send_frame, store, *, reporter=None, clock=time.monotonic):
        self.source, self.binding, self.send_frame = source, source.binding, send_frame
        self.store, self.clock = store, clock
        self.closed = False
        self.recent = {}
        self.counts = {}
        self.value = store.read()
        self.reporter = reporter

    def record(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return key

    def snapshot(self):
        current = self.source.current()
        if self.closed or self.source.binding != self.binding or current is None:
            raise EmosaError(Reason.NOT_READY, "reporting policy source unavailable")
        if len(current.capabilities.radios) != 1:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "sole-radio policy required")
        return current

    def identity(self, snapshot):
        return {
            "controller": self.binding.controller_al.hex(),
            "local_al": self.binding.local_al.hex(),
            "ruid": snapshot.capabilities.radios[0].basic.ruid.hex(),
        }

    def persist(self, value):
        try:
            self.store.save(value)
        except sqlite3.Error as exc:
            raise EmosaError(Reason.NOT_READY, "reporting policy persistence failed") from exc
        self.value = value

    def tick(self):
        now = self.clock()
        self.recent = {k: v for k, v in self.recent.items() if v[1] > now}
        if self.closed or self.value is None:
            return
        try:
            snapshot = self.snapshot()
        except EmosaError:
            return
        if self.value["identity"] != self.identity(snapshot):
            return  # Old intent cannot become another pod/controller's policy.
        value = dict(self.value)
        interval = value["policy"].get("metrics", {}).get("interval_seconds", 0)
        if value["boot_id"] != self.store.boot_id:
            value["boot_id"] = self.store.boot_id
            value["next_due"] = now + interval if interval else None
            value["schedule_rebases"] += 1
            self.persist(value)
        due = value["next_due"]
        if due is not None and now >= due:
            periods = int((now - due) // interval) + 1
            prior_unfulfilled = value["last_unfulfilled_due"]
            value["periods_due_without_report"] += periods
            value["next_due"] = due + periods * interval
            value["last_unfulfilled_due"] = due + (periods - 1) * interval
            if self.reporter is not None:
                value["latest_report_attempt"] = {
                    "due": value["last_unfulfilled_due"],
                    "status": "reserved_outcome_unknown",
                }
            # Reserve before I/O. A crash can overcount one missing report, but
            # cannot postpone the deadline, falsely prove a send, or replay a
            # burst of old reports. A send is not independent controller receipt.
            self.persist(value)
            if self.reporter is not None:
                try:
                    self.reporter.periodic(value["policy"], value["last_unfulfilled_due"])
                except (EmosaError, OSError) as exc:
                    value = {
                        **value,
                        "latest_report_attempt": {
                            "due": value["last_unfulfilled_due"],
                            "status": "unavailable_or_send_incomplete",
                            "reason": exc.code.value if isinstance(exc, EmosaError) else "IO_ERROR",
                        },
                    }
                    self.persist(value)
                else:
                    value = {
                        **value,
                        "periods_due_without_report": value["periods_due_without_report"] - 1,
                        "reports_transmitted": value.get("reports_transmitted", 0) + 1,
                        "last_unfulfilled_due": (
                            due + (periods - 2) * interval if periods > 1 else prior_unfulfilled
                        ),
                        "latest_report_attempt": {
                            "due": value["last_unfulfilled_due"],
                            "status": "transmitted_controller_receipt_unverified",
                        },
                    }
                    self.persist(value)
                    self.record("periodic_metric_report_transmitted")
                    return
            self.record("metric_reporting_due_without_qualified_source")

    def handle(self, message, received_at):
        if message.message_type != 0x8003:
            return None
        self.tick()
        snapshot = self.snapshot()
        if (
            message.source != self.binding.controller_al
            or message.destination != self.binding.local_al
            or message.relay
        ):
            invalid("policy from an unbound controller or envelope")
        if self.clock() >= received_at + 1:
            raise EmosaError(Reason.NOT_READY, "policy Ack deadline expired")
        raw = [{"kind": t.kind, "value": t.value.hex()} for t in message.tlvs]
        encoded = json.dumps(raw)
        if len(encoded) > 8192:
            invalid("policy request exceeds budget")
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        update = decode_policy(message.tlvs, snapshot.capabilities.radios[0].basic.ruid)
        prior = self.recent.get(message.mid)
        if prior and prior[0] != digest:
            invalid("conflicting policy request MID")
        if not prior:
            if len(self.recent) >= 64:
                raise EmosaError(Reason.NOT_READY, "policy request budget exhausted")
            value = self.value or {
                "identity": self.identity(snapshot),
                "policy": {},
                "receipt_count": 0,
                "next_due": None,
                "periods_due_without_report": 0,
                "last_unfulfilled_due": None,
                "boot_id": self.store.boot_id,
                "schedule_rebases": 0,
            }
            if value["identity"] != self.identity(snapshot):
                raise EmosaError(Reason.NOT_READY, "stored policy identity mismatch")
            policy = {**value["policy"], **update}
            due = value["next_due"]
            # Identical re-delivery (including new MIDs and process restart)
            # must not keep postponing the controller's reporting obligations.
            if policy.get("metrics") != value["policy"].get("metrics"):
                interval = policy["metrics"]["interval_seconds"]
                due = self.clock() + interval if interval else None
            value = {
                **value,
                "policy": policy,
                "latest_mid": message.mid,
                "latest_request": raw,
                "receipt_count": value["receipt_count"] + 1,
                "next_due": due,
            }
            snapshot.stamp.check(self.snapshot().stamp, self.clock())
            self.persist(value)
            self.recent[message.mid] = (digest, self.clock() + 5)
        PreparedReport(
            0x8000,
            message.mid,
            snapshot.stamp,
            min(received_at + 1, snapshot.stamp.valid_until),
            fragment_message(
                self.binding.controller_al, self.binding.local_al, 0x8000, message.mid, ()
            ),
        ).send(self.send_frame, lambda: self.snapshot().stamp, clock=self.clock)
        return self.record("policy_receipt_ack_sent")

    def status(self):
        return {
            "counts": dict(self.counts),
            "received_policy": self.value,
            "policy_application_proven": False,
            "required_reporting_proven": False,
            "reporting_gap": "qualified_measurements_and_fulfilled_reporting_pending",
        }

    def close(self):
        self.closed = True
        self.recent.clear()
