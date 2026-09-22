import fcntl
import json
import os
import sqlite3
import uuid
from pathlib import Path

from emosa.clock import utc_now
from emosa.config import validate
from emosa.errors import EmosaError, Reason
from emosa.model import Operation
from emosa.secrets import redact
from emosa.topology_bindings import REGISTRY_KEY, merge_registry


class Store:
    def __init__(
        self, directory: Path, *, read_only=False, max_operations=10000, max_events_per_run=10000
    ):
        self.directory = Path(directory)
        self.read_only = read_only
        self.max_operations = max_operations
        self.max_events = max_events_per_run
        self.process_id = str(uuid.uuid4())
        self.lock = None
        if not read_only:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.lock = open(self.directory / "writer.lock", "a+")  # noqa: SIM115 - lifetime is Store
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self.lock.close()
                raise EmosaError(
                    Reason.BUSY, "state directory already has an active writer"
                ) from exc
        path = self.directory / "journal.db"
        self.db = sqlite3.connect(f"file:{path}?mode=ro" if read_only else path, uri=read_only)
        self.db.row_factory = sqlite3.Row
        if not read_only:
            os.chmod(path, 0o600)
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1');
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, pod TEXT NOT NULL,
                    idem TEXT NOT NULL, fingerprint TEXT NOT NULL, run TEXT NOT NULL,
                    record TEXT NOT NULL, UNIQUE(source,pod,idem));
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    run TEXT NOT NULL, seq INTEGER NOT NULL, record TEXT NOT NULL,
                    PRIMARY KEY(run,seq));
                CREATE TABLE IF NOT EXISTS ownership (
                    pod TEXT PRIMARY KEY, record TEXT NOT NULL);
            """)
        version = self.db.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        if version is None or version[0] != "1":
            self.close()
            raise EmosaError(Reason.SCHEMA_MISMATCH, "unsupported journal schema version")

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lock:
            self.lock.close()
            self.lock = None

    def pin_topologies(self, pods):
        """Persist explicit simulation identities under the existing writer lock.

        This additive versioned metadata record needs no operation-schema change.
        Removed pods remain reserved; a changed binding needs a future explicit
        migration, not silent replacement or automatic trust on first connection.
        """
        if self.read_only:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "read-only journal")
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (REGISTRY_KEY,)).fetchone()
        try:
            saved = json.loads(row[0]) if row else {}
            if not isinstance(saved, dict):
                raise ValueError
            merged = merge_registry(saved, pods)
        except (ValueError, TypeError, KeyError):
            raise EmosaError(
                Reason.SCHEMA_MISMATCH, "invalid persisted topology registry"
            ) from None
        if merged != saved:
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES (?,?)",
                    (REGISTRY_KEY, json.dumps(merged, sort_keys=True)),
                )
        return {p["pod_id"]: merged[p["pod_id"]] for p in pods if p["pod_id"] in merged}

    def get(self, operation_id: str) -> Operation:
        row = self.db.execute(
            "SELECT record FROM operations WHERE id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise EmosaError(Reason.NOT_FOUND, "operation not found")
        return Operation.from_dict(json.loads(row[0]))

    def lookup(self, source: str, pod: str, key: str):
        row = self.db.execute(
            "SELECT record FROM operations WHERE source=? AND pod=? AND idem=?", (source, pod, key)
        ).fetchone()
        return Operation.from_dict(json.loads(row[0])) if row else None

    def operations(self, run_id=None) -> list[Operation]:
        rows = (
            self.db.execute("SELECT record FROM operations WHERE run=? ORDER BY rowid", (run_id,))
            if run_id
            else self.db.execute("SELECT record FROM operations ORDER BY rowid")
        )
        return [Operation.from_dict(json.loads(r[0])) for r in rows]

    def add(self, op: Operation):
        validate("operation", op.to_dict())
        if self.db.execute("SELECT count(*) FROM operations").fetchone()[0] >= self.max_operations:
            raise EmosaError(
                Reason.BUSY, "journal operation budget exhausted; archive before reuse"
            )
        with self.db:
            self.db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?,?,?)",
                (
                    op.operation_id,
                    op.request_source,
                    op.pod_id,
                    op.idempotency_key,
                    op.intent_fingerprint,
                    op.run_id,
                    json.dumps(op.to_dict()),
                ),
            )
            self._event(op.run_id, op.operation_id, op.pod_id, op.state, {}, op.created_at)

    def save(self, op: Operation, payload=None):
        validate("operation", op.to_dict())
        with self.db:
            self.db.execute(
                "UPDATE operations SET record=? WHERE id=?",
                (json.dumps(op.to_dict()), op.operation_id),
            )
            self._event(
                op.run_id,
                op.operation_id,
                op.pod_id,
                op.state,
                {"reason": op.reason, **(payload or {})},
                op.updated_at,
            )

    def _event(self, run_id, operation_id, pod_id, phase, payload, timestamp=None):
        seq = self.db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run=?", (run_id,)
        ).fetchone()[0]
        reason = payload.get("reason")
        if isinstance(reason, dict):
            reason = reason.get("code", "NOT_READY")
        record = {
            "schema_version": 1,
            "event_id": str(uuid.uuid4()),
            "sequence": seq,
            "timestamp": timestamp or utc_now(),
            "process_clock_id": self.process_id,
            "run_id": run_id,
            "operation_id": operation_id,
            "pod_id": pod_id,
            "phase": phase,
            "reason": reason,
            "payload": redact(payload),
        }
        validate("event", record)
        self.db.execute("INSERT INTO events VALUES (?,?,?)", (run_id, seq, json.dumps(record)))
        self.db.execute(
            "DELETE FROM events WHERE run=? AND seq<=?", (run_id, seq - self.max_events)
        )

    def event(self, run_id, phase, payload, pod_id=None):
        with self.db:
            self._event(run_id, None, pod_id, phase, payload)

    def events(self, run_id, after=0, limit=100):
        rows = self.db.execute(
            "SELECT record FROM events WHERE run=? AND seq>? ORDER BY seq LIMIT ?",
            (run_id, after, min(max(limit, 1), 500)),
        )
        return [json.loads(r[0]) for r in rows]

    def conflict(self, pod_id, evidence):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO ownership VALUES (?,?)",
                (pod_id, json.dumps(redact(evidence))),
            )

    def ownership(self, pod_id):
        row = self.db.execute("SELECT record FROM ownership WHERE pod=?", (pod_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_run(self, record):
        prior = self.db.execute(
            "SELECT record FROM runs WHERE id=?", (record["run_id"],)
        ).fetchone()
        if prior and json.loads(prior[0]).get("execution_status") != "running":
            raise EmosaError(Reason.PRECONDITION_FAILED, "completed run records are immutable")
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?)",
                (record["run_id"], json.dumps(redact(record))),
            )

    def run(self, run_id):
        row = self.db.execute("SELECT record FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise EmosaError(Reason.NOT_FOUND, "run not found")
        return json.loads(row[0])
