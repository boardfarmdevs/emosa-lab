"""Bounded read-only report coordination; no discovery admission or pod writes.

A caller supplies an established peer binding and complete, qualified facts.
Only the isolated simulator currently provides that source. An Ack is transport
correlation, not controller inventory or permission to begin WSC.
"""

import math
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace

from emosa.errors import EmosaError, Reason
from emosa.wire.autoconfiguration import SECURITY_ENVELOPES, PeerBinding
from emosa.wire.channel import OperatingRadio
from emosa.wire.cmdu import MidSequence, Reassembler, decode_frame, invalid
from emosa.wire.reports import (
    EarlyCapabilities,
    ReportStamp,
    TopologyFacts,
    early_report,
    topology_response,
)


@dataclass(frozen=True)
class ReportSnapshot:
    stamp: ReportStamp
    capabilities: EarlyCapabilities
    topology: TopologyFacts = field(repr=False)
    context_token: str
    operating_radios: tuple[OperatingRadio, ...] = ()


class ReportSource:
    """Atomic publication on one event loop, with a bounded observation lease.

    Revision is the source's (connection generation, database revision). Publish
    immutable facts from one complete snapshot. Explicit invalidation changes the
    token even when the same revision is later observed. Refreshing an unchanged
    revision can renew the source lease, never a previously prepared report.
    """

    def __init__(self, binding: PeerBinding, pod_id, inputs_digest, *, clock=time.monotonic):
        if (
            not isinstance(pod_id, str)
            or not 1 <= len(pod_id) <= 64
            or not isinstance(inputs_digest, str)
            or len(inputs_digest) != 64
            or any(c not in "0123456789abcdef" for c in inputs_digest)
        ):
            invalid("invalid report source identity or input digest")
        self.binding, self.pod_id, self.inputs_digest = binding, pod_id, inputs_digest
        self._identity = (binding, pod_id, inputs_digest)
        self.clock = clock
        self.instance = uuid.uuid4().hex
        self.epoch = 0
        self._snapshot = self._revision = self._last_facts = None
        self._last_observed = float("-inf")

    def invalidate(self):
        self._snapshot = None
        self.epoch += 1

    def publish(
        self, revision, capabilities, topology, *, observed_at, lifetime=1.0, operating_radios=()
    ):
        try:
            if (
                (self.binding, self.pod_id, self.inputs_digest) != self._identity
                or type(revision) is not tuple
                or len(revision) != 2
                or any(type(n) is not int or n < 0 for n in revision)
                or type(observed_at) not in (int, float)
                or not math.isfinite(observed_at)
                or type(lifetime) not in (int, float)
                or not math.isfinite(lifetime)
                or not 0 < lifetime <= 2
                or observed_at < self._last_observed
                or (self._revision is not None and revision < self._revision)
                or type(capabilities) is not EarlyCapabilities
                or type(topology) is not TopologyFacts
                or topology.device.al_mac != self.binding.local_al
                or type(operating_radios) is not tuple
                or any(type(r) is not OperatingRadio for r in operating_radios)
            ):
                invalid("invalid or out-of-order report publication")
            facts = (capabilities, topology, operating_radios)
            capacities = {r.basic.ruid: r.basic.max_bss for r in capabilities.radios}
            if len({r.ruid for r in operating_radios}) != len(operating_radios) or any(
                r.ruid not in capacities for r in operating_radios
            ):
                invalid("operating radio inventory does not match capabilities")
            radios = topology.operational.radios
            if set(capacities) != {r.ruid for r in radios} or any(
                len(r.bsses) > capacities[r.ruid] for r in radios
            ):
                invalid("capability and topology radio inventories disagree")
            if revision == self._revision and self._last_facts != facts:
                invalid("report facts changed without a new source revision")
            if self._snapshot is not None and self.clock() >= self._snapshot.stamp.valid_until:
                # Even when nobody polled during the gap, a lapsed observation
                # lease cannot silently preserve an established peer context.
                self.invalidate()
            token = f"{self.instance}/{self.epoch}/{revision[0]}/{revision[1]}"
            stamp = ReportStamp(token, observed_at, observed_at + lifetime)
            stamp.check(stamp, self.clock())
        except EmosaError:
            self.invalidate()
            raise
        self._revision, self._last_observed, self._last_facts = revision, observed_at, facts
        # Ordinary database revisions may change operational facts. Disconnect,
        # invalidation or a new database generation requires discovery again.
        context = f"{self.instance}/{self.epoch}/{revision[0]}"
        self._snapshot = ReportSnapshot(stamp, capabilities, topology, context, operating_radios)
        return self._snapshot

    def current(self):
        if (self.binding, self.pod_id, self.inputs_digest) != self._identity:
            self.invalidate()
            return None
        snapshot = self._snapshot
        if snapshot is not None:
            try:
                snapshot.stamp.check(snapshot.stamp, self.clock())
            except EmosaError:
                self.invalidate()
                return None
        return snapshot


@dataclass
class _Notification:
    snapshot: ReportSnapshot = field(repr=False)
    deadline: float
    next_retry: float
    mids: set[int] = field(default_factory=set)
    transmissions: int = 0


class ReportCoordinator:
    """One explicitly bound peer/pod; synchronous sends, bounded mutable state.

    tick() must run periodically even without incoming traffic. There is no
    profile-qualified flag or operation callback; this cannot open the wire gate.
    Local retry policy is three transmissions, 250 ms apart, within one second.
    """

    def __init__(self, source, send_frame, *, mids=None, clock=time.monotonic):
        self.source, self.binding, self.send_frame = source, source.binding, send_frame
        self.clock = clock
        self.mids = mids if mids is not None else MidSequence(secrets.randbits(16))
        self.assembly = Reassembler(clock=clock)
        self.pending = None
        self.closed = False
        self.events = deque(maxlen=64)
        self.counts = {}
        self._queries = {}
        self._tokens, self._token_time = 8.0, clock()

    def _record(self, event, *, mid=None):
        self.counts[event] = self.counts.get(event, 0) + 1
        self.events.append({"event": event, **({"mid": mid} if mid is not None else {})})
        return event

    def _stamp(self):
        if self.source.binding != self.binding:
            return None
        current = self.source.current()
        return current.stamp if current is not None else None

    def _facts(self):
        if self.closed:
            raise EmosaError(Reason.NOT_READY, "report coordinator is closed")
        if self.source.binding != self.binding:
            raise EmosaError(Reason.NOT_READY, "report peer binding changed")
        snapshot = self.source.current()
        if snapshot is None:
            raise EmosaError(Reason.NOT_READY, "report source is unavailable")
        return snapshot

    def notify_early(self):
        if self.pending is not None:
            raise EmosaError(Reason.BUSY, "an Early Report is already awaiting acknowledgment")
        snapshot = self._facts()
        now = self.clock()
        self.pending = _Notification(snapshot, min(now + 1, snapshot.stamp.valid_until), now)
        try:
            self._transmit_early()
        except (EmosaError, OSError):
            self.pending = None
            self._record("early_send_failed")
            raise

    def _transmit_early(self):
        pending = self.pending
        pending.snapshot.stamp.check(self._stamp(), self.clock())
        report = early_report(
            self.binding,
            pending.snapshot.capabilities,
            pending.snapshot.stamp,
            self.mids,
            clock=self.clock,
        )
        report = replace(report, deadline=pending.deadline)
        # Register only after a complete on-time send. A partial send never
        # becomes acknowledgment-eligible success in this component.
        report.send(self.send_frame, self._stamp, clock=self.clock)
        pending.mids.add(report.mid)
        pending.transmissions += 1
        pending.next_retry = self.clock() + 0.25
        self._record("early_sent", mid=report.mid)

    def tick(self):
        if self.closed:
            return
        self.assembly.expire()
        now = self.clock()
        self._queries = {mid: end for mid, end in self._queries.items() if end > now}
        if self.pending is None:
            return
        pending = self.pending
        try:
            pending.snapshot.stamp.check(self._stamp(), self.clock())
        except EmosaError:
            self.pending = None
            self._record("early_source_invalidated")
            return
        if self.clock() >= pending.deadline:
            self.pending = None
            self._record("early_ack_timeout")
        elif self.clock() >= pending.next_retry and pending.transmissions < 3:
            try:
                self._transmit_early()
            except (EmosaError, OSError):
                self.pending = None
                self._record("early_send_failed")

    def receive(self, frame, *, ingress, generation):
        if self.closed:
            return self._record("closed_input")
        try:
            fragment = decode_frame(frame)
            # Reject alien sources before allocating reassembly state.
            self.binding.check(fragment, ingress=ingress, generation=generation)
            if fragment.message_type not in (2, 0x8000):
                return self._record("unsupported_message")
            message = self.assembly.feed(frame, ingress=ingress)
            if message is None:
                return "incomplete"
            received_at = self.clock()
            if message.relay or any(t.kind in SECURITY_ENVELOPES for t in message.tlvs):
                invalid("unsupported report input envelope")
            if message.message_type == 0x8000:
                return self._ack(message)
            return self._query(message, received_at, ingress, generation)
        except EmosaError:
            return self._record("input_or_source_rejected")
        except OSError:
            return "topology_send_failed"

    def _ack(self, message):
        if self.pending is not None:
            try:
                self.pending.snapshot.stamp.check(self._stamp(), self.clock())
            except EmosaError:
                self.pending = None
                self._record("early_source_invalidated")
            if self.pending is not None and self.clock() >= self.pending.deadline:
                self.pending = None
                self._record("early_ack_timeout")
        if self.pending is None or message.mid not in self.pending.mids:
            return self._record("unmatched_ack", mid=message.mid)
        # No Error Code applies to an Early Report in this selected contract.
        # Known error/security companions cannot be treated as a clean receipt.
        if any(t.kind in (0xA3, 0xBC) for t in message.tlvs):
            return self._record("ack_error_rejected", mid=message.mid)
        self.pending = None
        return self._record("early_acknowledged", mid=message.mid)

    def _query(self, message, received_at, ingress, generation):
        self.tick()
        if message.mid in self._queries:
            return self._record("duplicate_query", mid=message.mid)
        now = self.clock()
        self._tokens = min(8.0, self._tokens + max(0, now - self._token_time) * 4)
        self._token_time = now
        if self._tokens < 1 or len(self._queries) >= 128:
            return self._record("query_budget_exhausted")
        self._tokens -= 1
        # Consume a complete query once, including a rejected/late response.
        # A legitimate retry uses a new MID under EasyMesh 15.1.
        self._queries[message.mid] = now + 5
        snapshot = self._facts()
        report = topology_response(
            message,
            self.binding,
            snapshot.topology,
            snapshot.stamp,
            ingress=ingress,
            generation=generation,
            received_at=received_at,
            clock=self.clock,
        )
        try:
            report.send(self.send_frame, self._stamp, clock=self.clock)
        except (EmosaError, OSError):
            self._record("topology_send_failed", mid=message.mid)
            raise
        return self._record("topology_response_sent", mid=message.mid)

    def close(self):
        self.closed, self.pending = True, None
        self.assembly = Reassembler(clock=self.clock)
        self._queries.clear()

    def status(self):
        return {
            "scope": "read_only_report_coordinator",
            "closed": self.closed,
            "source_available": self.source.current() is not None,
            "early_pending": self.pending is not None,
            "counts": dict(self.counts),
            "events": list(self.events),
            "operations_created": 0,
            "wsc_admission": "blocked_full_profile_and_durable_operation_integration",
            "controller_onboarding_proven": False,
            "physical_pod_proven": False,
        }
