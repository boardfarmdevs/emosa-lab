"""The agent has its pod publish its own statistics, and subscribes to them.

- ``TelemetrySetup`` writes the pod's MQTT settings and client report once per
  OpenSync start (``emosa.opensync.telemetry``), with its own operation journal.
  A failed or refused write is not retried on the same start: the next start,
  or a new admission, tries again.
- ``MqttSubscriber`` reads the pod's topic from the local broker and hands each
  message to ``PodStats`` (``emosa.opensync.stats``) on the agent's event loop.

The measurements go into the agent's status. They go on the wire only when a
report can be built from them completely: until then EMOSA sends nothing rather
than a partial or guessed EasyMesh metric (spec §3.6).
"""

import logging

import paho.mqtt.client as mqtt

from emosa.errors import EmosaError, Reason
from emosa.model import ACTIVE, State
from emosa.opensync.telemetry import TelemetryIntent
from emosa.operations import transition
from emosa.reconcile import Engine

log = logging.getLogger("emosa.agent.telemetry")
DEADLINE = 30  # the write is applied as soon as the pod's database has it
SOURCE = "telemetry-policy"


class TelemetrySetup:
    def __init__(self, pod_id, backend, store, vault, intent, *, run_id, clock=None):
        self.pod_id, self.backend, self.store, self.run_id = pod_id, backend, store, run_id
        self.intent = intent
        self.engine = Engine(store, vault, {pod_id: backend}, clock, intent_type=TelemetryIntent)
        self.engine.recover()
        self.waiting = None

    def latest(self):
        ops = self.store.operations()
        return ops[-1] if ops else None

    def _settle(self, op, snap):
        if op.state not in (State.CONFIG_COMMITTED, State.INDETERMINATE):
            return
        target = TelemetryIntent(**op.intent).target()
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
            log.info("statistics publishing configured on the pod (%s)", self.intent.topic)
        elif self.engine._expired(op):
            op.original_outcome, op.deadline_elapsed = op.state, True
            transition(op, State.TIMED_OUT)
            op.reason = Reason.APPLY_TIMEOUT
            self.engine._save(op)

    def key(self):
        """One write per OpenSync start and requested configuration."""
        return f"{self.backend.instance}:{self.intent.client_stats()}:{self.intent.topic}"

    def _wanted(self, op, snap):
        if not snap.ready:
            self.waiting = "pod not bound"
            return False
        if self.store.ownership(self.pod_id):
            self.waiting = "another manager changed the configuration: admit the pod anew"
            return False
        if op is not None and op.state in ACTIVE:
            self.waiting = "write in progress"
            return False
        done = self.store.lookup(SOURCE, self.pod_id, self.key())
        if done is not None:
            self.waiting = (
                None
                if done.state == State.OBSERVED_APPLIED
                else f"not configured on this start: {done.state} {done.reason or ''}".strip()
            )
            return False
        self.waiting = None
        return True

    async def tick(self):
        try:
            snap = await self.backend.snapshot()
        except (EmosaError, ConnectionError, TimeoutError):
            snap = None
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
            log.info("statistics publishing %s: %s %s", op.operation_id, op.state, op.reason or "")

    def status(self):
        op = self.latest()
        return {
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


class MqttSubscriber:
    """The pod's topic on the local broker, delivered on the event loop."""

    def __init__(self, host, port, topic, deliver, *, client_id):
        self.host, self.port, self.topic, self.deliver = host, port, topic, deliver
        self.connected = False
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=True
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(1, 30)
        self.loop = None

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self.connected = not reason_code.is_failure
        if self.connected:
            client.subscribe(self.topic, qos=0)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False

    def _on_message(self, client, userdata, message):
        # paho's network thread: hand over to the agent's loop
        self.loop.call_soon_threadsafe(
            self.deliver, message.topic, bytes(message.payload), message.retain
        )

    def start(self, loop):
        self.loop = loop
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()

    def stop(self):
        self.client.disconnect()
        self.client.loop_stop()
