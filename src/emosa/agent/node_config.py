# SPDX-License-Identifier: Apache-2.0
"""The agent writes its pod's modules' settings and reads their reports (spec 4, the pods' modules).

``NodeConfig`` writes the settings (``emosa.opensync.node_config``) at most once per start of the
pod's OpenSync, with its own operation journal, when something differs from them. Not repeated on
that start, even when a row is changed again: the next start writes them again.

Each module reports in ``Node_State``, by the keys EMOSA reads: ``state`` (``ok`` or
``error:<reason>``), ``boot_id``, ``version`` (the SHA-256 of the module's installed file),
``running`` (the one its running process started from), ``slot`` (``none`` when nothing starts it
at the pod's next boot), ``update`` (``ok`` or ``failed:<reason>``) with ``update_sha256`` (the
request it answers), a key per setting it applied (its value, as applied) and ``events``
(``<seq> <word>[:<detail>]`` entries joined by ``;``, newest last, ``seq`` rising within a boot).
The agent puts them in its status with what they mean (``flags``) and logs each new event once.
"""

import logging
import time

from emosa.errors import EmosaError, Reason
from emosa.model import ACTIVE, State
from emosa.opensync.node_config import NodeConfigIntent
from emosa.operations import transition
from emosa.reconcile import Engine

log = logging.getLogger("emosa.agent.node_config")
DEADLINE = 30  # the settings are in the database once the write is
SOURCE = "node-config-policy"
RESTART_GRACE = 60  # seconds an installed update may take to run before it is flagged
REPORTED = ("state", "boot_id", "version", "running", "slot", "update", "update_sha256")


def events(text):
    """A module's ``events``: [(seq, entry)] in order, entries without a number left out."""
    found = []
    for entry in (text or "").split(";"):
        seq, _, rest = entry.strip().partition(" ")
        if seq.isdigit() and rest:
            found.append((int(seq), rest))
    return found


class NodeConfig:
    def __init__(self, pod_id, backend, store, vault, *, run_id, clock=None, monotonic=None):
        self.pod_id, self.backend, self.store, self.run_id = pod_id, backend, store, run_id
        self.intent = backend.intent
        self.engine = Engine(store, vault, {pod_id: backend}, clock, intent_type=NodeConfigIntent)
        self.engine.recover()
        self.waiting = None
        self.monotonic = monotonic or time.monotonic
        self.seen = {}  # module -> (boot_id, the last event seq logged)
        self.unstarted = {}  # module -> since when an update reported ok has not been running

    def latest(self):
        ops = self.store.operations()
        return ops[-1] if ops else None

    def _settle(self, op, snap):
        if op.state not in (State.CONFIG_COMMITTED, State.INDETERMINATE):
            return
        target = NodeConfigIntent(**op.intent).target()
        same_start = snap is not None and self.backend.instance == op.plan.get("instance")
        if (
            same_start
            and snap.ready
            and snap.observed.satisfies(target)
            and self.engine._matches(snap.config, target)
        ):
            evidence = self.engine._evidence(snap, instance=self.backend.instance)
            if op.state == State.INDETERMINATE:
                evidence["attribution"] = "current_condition_only"
            transition(op, State.OBSERVED_APPLIED, evidence=evidence)
            op.application_evidence, op.reason = evidence, None
            self.engine._save(op)
            log.info("the pod's module settings are applied")
        elif self.engine._expired(op):
            op.original_outcome, op.deadline_elapsed = op.state, True
            transition(op, State.TIMED_OUT)
            op.reason = Reason.APPLY_TIMEOUT
            self.engine._save(op)

    def key(self):
        """At most one write per OpenSync start."""
        return f"{self.backend.instance}:node-config"

    def _wanted(self, op, snap):
        if not snap.ready:
            self.waiting = "pod not bound"
            return False
        if self.store.ownership(self.pod_id):
            self.waiting = "another manager changed the settings: admit the pod anew"
            return False
        if op is not None and op.state in ACTIVE:
            self.waiting = "write in progress"
            return False
        done = self.store.lookup(SOURCE, self.pod_id, self.key())
        if done is not None:
            self.waiting = (
                None
                if done.state == State.OBSERVED_APPLIED
                else f"not written on this start: {done.state} {done.reason or ''}".strip()
            )
            return False
        self.waiting = None
        return bool(snap.config.get("pending"))

    def _report(self, snap):
        """Each module's new events logged once (by boot and seq), and its updates followed."""
        for module, seen in (snap.config.get("modules") or {}).items():
            state = seen.get("state") or {}
            boot = state.get("boot_id")
            last_boot, last_seq = self.seen.get(module, (None, -1))
            if boot != last_boot:
                last_seq = -1
                if last_boot is not None:
                    log.info("module %s: a new boot of the pod (%s)", module, boot)
            for seq, entry in events(state.get("events")):
                if seq > last_seq:
                    log.info("module %s: event %d %s", module, seq, entry)
                    last_seq = seq
            self.seen[module] = (boot, last_seq)
            wanted = self.intent.modules.get(module, {}).get("update_sha256")
            taken = (
                wanted
                and state.get("update") == "ok"
                and state.get("update_sha256") == wanted
                and state.get("running") != state.get("version")
            )
            if not taken:
                self.unstarted.pop(module, None)
            else:
                self.unstarted.setdefault(module, self.monotonic())

    async def tick(self):
        try:
            snap = await self.backend.snapshot()
        except (EmosaError, ConnectionError, TimeoutError):
            snap = None
        if snap is not None and snap.ready:
            self._report(snap)
        op = self.latest()
        if op is not None:
            self._settle(op, snap)
        if snap is None or not self._wanted(self.latest(), snap):
            return
        op = self.engine.request(
            self.intent, source=SOURCE, key=self.key(), run_id=self.run_id, deadline=DEADLINE
        )
        if op.state == State.REQUESTED:
            op = await self.engine.execute(op.operation_id)
            log.info("module settings %s: %s %s", op.operation_id, op.state, op.reason or "")

    def flags(self, module, settings, state):
        """What the module's report means: [flag]."""
        flags = []
        if str(state.get("state") or "").startswith("error:"):
            flags.append("state_error")
        if state.get("slot") == "none":
            flags.append("not_started_at_boot")  # the module's hook would not run at the next boot
        wanted = settings.get("update_sha256")
        if wanted and state.get("update_sha256") == wanted:
            if str(state.get("update") or "").startswith("failed:"):
                flags.append("update_failed")
            since = self.unstarted.get(module)
            if since is not None and self.monotonic() - since > RESTART_GRACE:
                flags.append("restart_not_taken")  # installed, not running
        elif wanted:
            flags.append("update_pending")
        return flags

    def status(self):
        op = self.latest()
        snap = self.backend.last
        seen = {} if snap is None else (snap.config.get("modules") or {})
        modules = {}
        for module, settings in sorted(self.intent.modules.items()):
            config = (seen.get(module) or {}).get("config") or {}
            state = (seen.get(module) or {}).get("state") or {}
            modules[module] = {
                "settings_written": config == settings,
                # the settings the module reports as applied (its Node_State echoes the value)
                "settings_echoed": sorted(k for k, v in settings.items() if state.get(k) == v),
                **{k: state.get(k) for k in REPORTED},
                "flags": self.flags(module, settings, state),
            }
        return {
            "modules": modules,
            "differs": None if snap is None else snap.config.get("differs"),
            "waiting": self.waiting,
            "operation": None
            if op is None
            else {
                "operation_id": op.operation_id,
                "state": op.state,
                "reason": op.reason,
                "instance": (op.plan or {}).get("instance"),
            },
        }
