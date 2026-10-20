# SPDX-License-Identifier: Apache-2.0
"""A pod's modules' settings, as an EMOSA scope (the node-config scope, spec 4, the pods' modules).

OpenSync's ``Node_Config`` holds settings for a module of the pod's own software, a row per
(module, key), and ``Node_State`` that module's report, a row per (module, key) too: a pod-side
hook (a script kept on the pod's flash, say) reads its module's settings and reports its state.
The agent configuration's ``modules`` (from the fleet entry) names each module and its settings;
the module names are the site's, never EMOSA's. EMOSA writes them once per start of the pod's
OpenSync in one guarded transaction: a row per setting (``persist`` true), a changed value
updated, a key of the module EMOSA no longer sets (or a second row of one key) removed. Another
module's rows are never touched, and the module ``emosa`` is EMOSA's own (a legacy pod's start
marker, ``emosa.opensync.uplink``), never a configured one. Applied when, on the same start, each
configured module's ``Node_Config`` rows are its settings and nothing else. What the module did
with them is its ``Node_State``: read, never written, and reported beside it
(``emosa.agent.node_config``).
"""

import json
import re
from dataclasses import dataclass

from emosa.backends.base import Snapshot, SubmitResult
from emosa.clock import utc_now
from emosa.errors import EmosaError, Reason
from emosa.model import Observation
from emosa.opensync.mapping import check_results, failed_check, where_uuid
from emosa.opensync.uplink import START_MODULE, start_instance

MODE = "opensync-node-config"
PROVENANCE = "opensync:Node_Config+Node_State"
MODULE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")  # Node_Config.module: 1 to 32
KEY = re.compile(r"[a-z0-9][a-z0-9_]{0,63}")  # Node_Config.key: 1 to 64
VALUE_MAX = 1024  # Node_Config.value: 1 to 1024, never empty
MODULES_MAX, KEYS_MAX = 4, 16
# What the scope monitors beyond the AP scope's tables (emosa.opensync.schema.TABLES)
MONITOR = {
    "Node_Config": ["module", "key", "value", "persist"],
    "Node_State": ["module", "key", "value"],
}


@dataclass(frozen=True)
class NodeConfigIntent:
    """The settings of the pod's modules: {module: {key: value}}."""

    pod_id: str
    modules: dict

    def record(self):
        return {
            "pod_id": self.pod_id,
            "modules": {m: dict(sorted(kv.items())) for m, kv in sorted(self.modules.items())},
        }

    def validate(self):
        if not isinstance(self.pod_id, str) or not self.pod_id:
            raise EmosaError(Reason.INVALID_INPUT, "explicit pod required")
        if not isinstance(self.modules, dict) or not 1 <= len(self.modules) <= MODULES_MAX:
            raise EmosaError(Reason.INVALID_INPUT, f"one to {MODULES_MAX} modules")
        for module, settings in self.modules.items():
            if not isinstance(module, str) or not MODULE.fullmatch(module):
                raise EmosaError(Reason.INVALID_INPUT, "a module name is [a-z0-9_-], 1 to 32")
            if module == START_MODULE:
                raise EmosaError(Reason.INVALID_INPUT, f"the module {module} is EMOSA's own")
            if not isinstance(settings, dict) or not 1 <= len(settings) <= KEYS_MAX:
                raise EmosaError(Reason.INVALID_INPUT, f"one to {KEYS_MAX} settings per module")
            for key, value in settings.items():
                if not isinstance(key, str) or not KEY.fullmatch(key):
                    raise EmosaError(Reason.INVALID_INPUT, "a setting's key is [a-z0-9_], 1 to 64")
                if not isinstance(value, str) or not 1 <= len(value) <= VALUE_MAX:
                    raise EmosaError(Reason.INVALID_INPUT, "a setting's value is 1 to 1024 chars")

    def target(self, vault=None):
        self.validate()
        return {"differs": []}


def module_rows(decoded, table, module):
    """The table's rows of ``module``: [(uuid, row)], by key then uuid."""
    return sorted(
        (
            (u, r)
            for u, r in decoded.get(table, {}).items()
            if r.get("module") == module and isinstance(r.get("key"), str)
        ),
        key=lambda item: (item[1]["key"], item[0]),
    )


def module_state(decoded, module):
    """The module's Node_State: {key: value}, the first row of each key."""
    state = {}
    for _, row in module_rows(decoded, "Node_State", module):
        state.setdefault(row["key"], row.get("value"))
    return state


class NodeConfigBackend:
    """The node-config scope of one pod, bound by serial: its modules' settings ``intent``."""

    mode = MODE

    def __init__(self, pod_id, session, *, serial, intent):
        self.pod_id, self.session, self.expected_serial = pod_id, session, serial
        self.intent = intent
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

    def _changes(self, decoded, intent):
        """What the write makes now: rows to insert ((module, key, value)), to update ((uuid,
        row, value)) and to delete ((uuid, row)); and the configured view: each module's settings
        as the pod has them, what differs ("module/key"), and each module's Node_State."""
        inserts, updates, deletes, differs = [], [], [], []
        modules = {}
        for module, settings in sorted(intent.modules.items()):
            seen = set()
            config = {}
            for uuid, row in module_rows(decoded, "Node_Config", module):
                key = row["key"]
                if key in seen or key not in settings:  # a second row of a key, or not ours
                    deletes.append((uuid, row))
                    differs.append(f"{module}/{key}")
                    continue
                seen.add(key)
                config[key] = row.get("value")
                if row.get("value") != settings[key]:
                    updates.append((uuid, row, settings[key]))
                    differs.append(f"{module}/{key}")
            for key in sorted(set(settings) - seen):
                inserts.append((module, key, settings[key]))
                differs.append(f"{module}/{key}")
            modules[module] = {"config": config, "state": module_state(decoded, module)}
        configured = {
            "differs": sorted(set(differs)),
            "modules": modules,
            "pending": bool(inserts or updates or deletes),
        }
        return (inserts, updates, deletes), configured

    async def snapshot(self):
        try:
            raw = await self.session.snapshot()
            decoded, _ = self._binding(raw)
            _, configured = self._changes(decoded, self.intent)
            self.last = Snapshot(
                configured,
                Observation(
                    self.pod_id,
                    "node-config",
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

    def _check(self, intent):
        intent.validate()
        if intent.pod_id != self.pod_id:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "request exceeds the bound pod")

    async def plan(self, intent):
        self._check(intent)
        raw = await self.session.snapshot()
        decoded, _ = self._binding(raw)
        if not raw["ready"]:
            raise EmosaError(Reason.NOT_READY, "the pod's database is not ready")
        _, configured = self._changes(decoded, intent)
        return {
            "mapping": MODE,
            "action": "node-config",
            "settings": configured["differs"],
            "instance": self.instance,
            "fields": ["Node_Config.module", "Node_Config.key", "Node_Config.value"],
            "guard": "pod serial and each changed row as it is",
        }

    def transaction(self, intent, decoded, node_uuid):
        """The guarded write: its operations and the counts each must match (None: no write)."""
        (inserts, updates, deletes), _ = self._changes(decoded, intent)
        if not (inserts or updates or deletes):
            return None, None
        operations = [
            {
                "op": "wait",
                "table": "AWLAN_Node",
                "where": where_uuid(node_uuid),
                "columns": ["serial_number"],
                "until": "==",
                "rows": [{"serial_number": self.expected_serial}],
                "timeout": 0,
            }
        ]
        counts = [None]

        def as_it_is(uuid, row):
            return {
                "op": "wait",
                "table": "Node_Config",
                "where": where_uuid(uuid),
                "columns": ["module", "key", "value"],
                "until": "==",
                "rows": [{c: row.get(c) for c in ("module", "key", "value")}],
                "timeout": 0,
            }

        for uuid, row in deletes:
            operations += [
                as_it_is(uuid, row),
                {"op": "delete", "table": "Node_Config", "where": where_uuid(uuid)},
            ]
            counts += [None, 1]
        for uuid, row, value in updates:
            operations += [
                as_it_is(uuid, row),
                {
                    "op": "update",
                    "table": "Node_Config",
                    "where": where_uuid(uuid),
                    "row": {"value": value, "persist": True},
                },
            ]
            counts += [None, 1]
        for module, key, value in inserts:
            # the key not there as it was read: a row another writer added since is a conflict
            operations += [
                {
                    "op": "wait",
                    "table": "Node_Config",
                    "where": [["module", "==", module], ["key", "==", key]],
                    "columns": ["key"],
                    "until": "==",
                    "rows": [],
                    "timeout": 0,
                },
                {
                    "op": "insert",
                    "table": "Node_Config",
                    "row": {"module": module, "key": key, "value": value, "persist": True},
                },
            ]
            counts += [None, None]
        return operations, counts

    async def submit(self, intent, attempt):
        await self.plan(intent)
        raw = await self.session.snapshot()
        decoded, node_uuid = self._binding(raw)
        if raw["generation"] != attempt["session_generation"]:
            return SubmitResult("rejected", {}, Reason.NOT_READY)
        operations, counts = self.transaction(intent, decoded, node_uuid)
        if operations is None:
            return SubmitResult("rejected", {}, Reason.NOT_READY)  # nothing differs any more
        self.write_count += 1
        try:
            results = await self.session.transact(
                operations, attempt["transaction_id"], attempt["session_generation"]
            )
            check_results(results, counts)
        except (ConnectionError, TimeoutError):
            return SubmitResult("unknown", {"attribution": "unknown"}, Reason.OUTCOME_UNKNOWN)
        except EmosaError as exc:
            return failed_check(exc)
        return SubmitResult(
            "committed",
            {
                "attribution": "reply",
                "transaction_validated": True,
                "transaction_id": attempt["transaction_id"],
                "session_generation": raw["generation"],
                "action": "node-config",
                "instance": self.instance,
                "results": json.loads(json.dumps(results, default=str)),
            },
            None,
        )
