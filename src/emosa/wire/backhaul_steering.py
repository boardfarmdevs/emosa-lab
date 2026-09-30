"""Backhaul Steering from the controller (EasyMesh §9, Backhaul Steering Request).

A Backhaul Steering Request (``0x8019``) carries one Backhaul Steering Request
TLV (``0x9E``): the backhaul STA's MAC address, the target BSSID, and the
target's operating class and channel. The agent acknowledges it within one
second (1905 Ack), moves the station, and then answers with a Backhaul Steering
Response (``0x801A``, the request's MID): one Backhaul Steering Response TLV
(``0x9F``: the STA, the target, result ``0x00`` success or ``0x01`` failure)
and, with a failure, an Error Code TLV (``0xA3``: reason ``0x06``, the STA),
as the RDK lab's own agents answer.

The move is the uplink scope's (``emosa.agent.uplink``): the pod's Multi-AP
backhaul station is pinned to one upstream BSSID, and a move pins it to the
target instead; it is done when the pod's State shows the station on the target.
This module only decides what is asked, hands it over and reports the outcome:

- a request for the pod's backhaul station, to a unicast target, is handed to
  the executor, which returns None, or why it cannot move the station now;
- a request it cannot hand over is answered with a failure at once;
- a started move is answered when ``outcome`` reports it applied (success) or
  failed, or with a failure at the deadline.

The move changes the pod's reported topology, and the agent may renew its
session over it (seen live: 1.3 s after the station joined the target). The
move under way and the answers already given are the agent's (``shared``), so
the next session's coordinator answers it.
"""

import time
from dataclasses import dataclass

from emosa.errors import EmosaError, Reason
from emosa.wire.cmdu import Tlv, fragment_message, invalid
from emosa.wire.reports import PreparedReport

REQUEST = 0x8019
RESPONSE = 0x801A
ACK = 0x8000
REQUEST_TLV = 0x9E
RESPONSE_TLV = 0x9F
ERROR_CODE_TLV = 0xA3
SUCCESS, FAILURE = 0x00, 0x01
# Backhaul steering rejected, or the association with the target failed.
ASSOCIATION_FAILED = 0x06
# The uplink switch's own deadline (90 s), a refresh and the pod's State to show it.
DEADLINE = 120


def unicast(value, label):
    if len(value) != 6 or not any(value) or value[0] & 1:
        invalid(f"{label} must be a unicast MAC address")
    return value


@dataclass(frozen=True)
class BackhaulSteeringRequest:
    station: bytes
    target: bytes
    operating_class: int
    channel: int

    def record(self):
        return {
            "station": self.station.hex(":"),
            "target": self.target.hex(":"),
            "operating_class": self.operating_class,
            "channel": self.channel,
        }


def decode_request(tlvs):
    found = [t for t in tlvs if t.kind == REQUEST_TLV]
    if len(found) != 1 or len(found[0].value) != 14:
        invalid("one Backhaul Steering Request TLV required")
    value = found[0].value
    return BackhaulSteeringRequest(
        unicast(value[:6], "backhaul STA"), value[6:12], value[12], value[13]
    )


def response_tlvs(request, error):
    """The Backhaul Steering Response's TLVs: success, or failure with its reason."""
    tlvs = [
        Tlv(RESPONSE_TLV, request.station + request.target + bytes([FAILURE if error else SUCCESS]))
    ]
    if error:
        tlvs.append(Tlv(ERROR_CODE_TLV, bytes([error]) + request.station))
    return tuple(tlvs)


def refusal(request, stations):
    """Why EMOSA cannot hand a request over, or None."""
    if request.station not in stations:
        return "not_backhaul_station"
    target = request.target
    if len(target) != 6 or not any(target) or target[0] & 1:
        return "invalid_target"
    return None


class BackhaulSteeringCoordinator:
    """Acknowledges Backhaul Steering Requests, hands them over, answers with the outcome.

    ``executor(bssid)`` starts moving the pod's backhaul station to ``bssid``
    (lower-case text) and returns None, or a reason why it did not start.
    ``outcome(bssid)`` is None while the move is under way, True once the pod's
    State shows the station on ``bssid``, or the reason it failed.
    """

    def __init__(self, source, send_frame, executor, outcome, *, clock=time.monotonic, shared=None):
        self.source, self.binding, self.send_frame = source, source.binding, send_frame
        self.executor, self.outcome, self.clock = executor, outcome, clock
        self.counts = {}
        # the agent's, across its sessions: "pending" {"mid", "request", "deadline"} and
        # "answered", MID -> (request, error, expiry): a repeated request is answered again
        self.shared = shared if shared is not None else {}
        self.shared.setdefault("pending", None)
        self.shared.setdefault("answered", {})
        self.last = None

    @property
    def pending(self):
        return self.shared["pending"]

    @pending.setter
    def pending(self, value):
        self.shared["pending"] = value

    @property
    def answered(self):
        return self.shared["answered"]

    @answered.setter
    def answered(self, value):
        self.shared["answered"] = value

    def _record(self, event):
        self.counts[event] = self.counts.get(event, 0) + 1
        return event

    def _snapshot(self):
        snapshot = self.source.current()
        if snapshot is None:
            raise EmosaError(Reason.NOT_READY, "the pod's state is not available")
        return snapshot

    def _send(self, message_type, mid, tlvs, snapshot, deadline):
        PreparedReport(
            message_type,
            mid,
            snapshot.stamp,
            min(deadline, snapshot.stamp.valid_until),
            fragment_message(
                self.binding.controller_al, self.binding.local_al, message_type, mid, tlvs
            ),
        ).send(self.send_frame, lambda: self._snapshot().stamp, clock=self.clock)

    def _respond(self, mid, request, error, snapshot):
        now = self.clock()
        self._send(RESPONSE, mid, response_tlvs(request, error), snapshot, now + 1)
        self.answered[mid] = (request, error, now + 30)

    def handle(self, message, received_at):
        if message.message_type != REQUEST:
            return None
        if (
            message.source != self.binding.controller_al
            or message.destination != self.binding.local_al
            or message.relay
        ):
            invalid("backhaul steering request from an unbound controller or envelope")
        now = self.clock()
        if now >= received_at + 1:
            raise EmosaError(Reason.NOT_READY, "backhaul steering Ack deadline expired")
        self.answered = {m: a for m, a in self.answered.items() if a[2] > now}
        request = decode_request(message.tlvs)
        snapshot = self._snapshot()
        self._send(ACK, message.mid, (), snapshot, received_at + 1)
        if message.mid in self.answered:
            request, error, _ = self.answered[message.mid]
            self._respond(message.mid, request, error, snapshot)
            return self._record("backhaul_steering_repeated_response_sent")
        if self.pending is not None and self.pending["mid"] == message.mid:
            return self._record("backhaul_steering_repeated_ack_sent")
        stations = {sta for _, sta in snapshot.topology.backhaul_stations}
        reason = refusal(request, stations)
        if reason is None and self.pending is not None:
            reason = "move_in_progress"
        if reason is None:
            reason = self.executor(request.target.hex(":"))
        self.last = {"mid": message.mid, "request": request.record(), "refused": reason}
        if reason is not None:
            self._respond(message.mid, request, ASSOCIATION_FAILED, snapshot)
            return self._record(f"backhaul_steering_refused_{reason}")
        self.pending = {"mid": message.mid, "request": request, "deadline": now + DEADLINE}
        return self._record("backhaul_steering_started")

    def tick(self):
        """Answer a started move once its outcome is known, or at its deadline."""
        if self.pending is None:
            return None
        request, now = self.pending["request"], self.clock()
        result = self.outcome(request.target.hex(":"))
        expired = now >= self.pending["deadline"]
        if result is None and not expired:
            return None
        snapshot = self.source.current()
        if snapshot is None:
            # the pod's State is being read again after the move: answer on a later tick,
            # unless it stays away well past the deadline
            if now < self.pending["deadline"] + 30:
                return None
            mid, self.pending = self.pending["mid"], None
            self.last = {**(self.last or {}), "mid": mid, "result": "unanswered"}
            return self._record("backhaul_steering_unanswered")
        error = 0 if result is True else ASSOCIATION_FAILED
        mid, self.pending = self.pending["mid"], None
        self._respond(mid, request, error, snapshot)
        outcome = "succeeded" if result is True else "failed"
        self.last = {
            "mid": mid,
            "request": request.record(),
            "result": outcome,
            "reason": None if result is True else (result or "deadline"),
        }
        return self._record(f"backhaul_steering_{outcome}")

    def status(self):
        return {
            "counts": dict(self.counts),
            "pending": None
            if self.pending is None
            else {"mid": self.pending["mid"], "request": self.pending["request"].record()},
            "last": self.last,
        }

    def close(self):
        """A closed session takes no new request; a move under way is answered by the next."""
        self.executor = lambda bssid: "session_closed"
