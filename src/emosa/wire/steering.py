"""Client steering requests from the controller (EasyMesh §11, Client Steering Request).

A Client Steering Request (``0x8014``) carries one Steering Request TLV (``0x9B``):
the source BSSID, the request mode (mandate or opportunity) with its BTM flags,
the opportunity window, the BTM disassociation timer, the stations and the
target BSSs. The agent acknowledges it within one second (1905 Ack), with an
Error Code TLV (``0xA3``, reason ``0x02``) for every listed station that is not
associated with the source BSS.

What the pod does with it is the steering scope's (``emosa.agent.steering``).
This module only decides what is asked and whether EMOSA can hand it over:

- a **mandate** for one station and one named target on a BSS of the pod is
  handed to the executor;
- anything else (several stations or targets, the wildcard target the agent
  would have to choose itself, a BSS that is not the pod's) is acknowledged and
  not carried out, and counted with its reason;
- an **opportunity** leaves the choice to the agent. EMOSA makes no steering
  decisions of its own, so the window ends at once: Steering Completed
  (``0x8017``) follows the Ack.

No Client Steering BTM Report (``0x8015``) is sent: an unchanged OpenSync 6.6
pod does not expose the station's BTM status to EMOSA (rdk-lab.md §7).
"""

import struct
import time
from dataclasses import dataclass

from emosa.errors import EmosaError, Reason
from emosa.wire.cmdu import Tlv, fragment_message, invalid
from emosa.wire.reports import PreparedReport

REQUEST = 0x8014
COMPLETED = 0x8017
ACK = 0x8000
STEERING_REQUEST_TLV = 0x9B
ERROR_CODE_TLV = 0xA3
STA_NOT_ASSOCIATED = 0x02
WILDCARD = b"\xff" * 6
MANDATE = 0x80
DISASSOC_IMMINENT = 0x40
ABRIDGED = 0x20


def unicast(value, label):
    if len(value) != 6 or not any(value) or value[0] & 1:
        invalid(f"{label} must be a unicast MAC address")
    return value


@dataclass(frozen=True)
class SteeringRequest:
    source_bssid: bytes
    mandate: bool
    disassoc_imminent: bool
    abridged: bool
    window: int  # Steering Opportunity Window, seconds
    disassoc_timer: int  # BTM Disassociation Timer, TUs
    stations: tuple[bytes, ...]
    targets: tuple[tuple[bytes, int, int], ...]  # (BSSID, operating class, channel)

    def record(self):
        return {
            "source_bssid": self.source_bssid.hex(":"),
            "mandate": self.mandate,
            "disassoc_imminent": self.disassoc_imminent,
            "abridged": self.abridged,
            "window": self.window,
            "disassoc_timer": self.disassoc_timer,
            "stations": [s.hex(":") for s in self.stations],
            "targets": [[b.hex(":"), c, n] for b, c, n in self.targets],
        }


def decode_request(tlvs):
    """The one Steering Request TLV of a Client Steering Request (Profile-1 layout)."""
    found = [t for t in tlvs if t.kind == STEERING_REQUEST_TLV]
    if len(found) != 1:
        invalid("one Steering Request TLV required")
    data = found[0].value
    if len(data) < 12:
        invalid("truncated Steering Request TLV")
    source, flags = data[:6], data[6]
    window, timer = struct.unpack("!HH", data[7:11])
    count, offset = data[11], 12
    if len(data) < offset + 6 * count + 1:
        invalid("truncated station list")
    stations = tuple(
        unicast(data[offset + 6 * i : offset + 6 * i + 6], "station") for i in range(count)
    )
    offset += 6 * count
    count, offset = data[offset], offset + 1
    if len(data) != offset + 8 * count:
        invalid("malformed target BSS list")
    targets = tuple(
        (
            data[offset + 8 * i : offset + 8 * i + 6],
            data[offset + 8 * i + 6],
            data[offset + 8 * i + 7],
        )
        for i in range(count)
    )
    if len(set(stations)) != len(stations):
        invalid("duplicate station")
    return SteeringRequest(
        unicast(source, "source BSSID"),
        bool(flags & MANDATE),
        bool(flags & DISASSOC_IMMINENT),
        bool(flags & ABRIDGED),
        window,
        timer,
        stations,
        targets,
    )


def refusal(request, associated):
    """Why EMOSA cannot carry out a request it acknowledges, or None."""
    if not request.mandate:
        return "opportunity"  # EMOSA makes no steering decisions of its own
    if len(request.stations) != 1:
        return "not_one_station"
    if request.stations[0] not in associated:
        return "station_not_associated"
    if len(request.targets) != 1:
        return "not_one_target"
    bssid, _, channel = request.targets[0]
    if bssid == WILDCARD:
        return "agent_selected_target"  # the agent would have to choose the target
    unicast(bssid, "target BSSID")
    if bssid == request.source_bssid or not channel:
        return "invalid_target"
    return None


class SteeringCoordinator:
    """Acknowledges Client Steering Requests and hands the executable ones over.

    ``executor(request)`` starts a mandate on the pod and returns None, or a
    reason why it did not start (for example a steering already in progress).
    """

    def __init__(self, source, send_frame, executor, mids, *, clock=time.monotonic):
        self.source, self.binding, self.send_frame = source, source.binding, send_frame
        self.executor, self.mids, self.clock = executor, mids, clock
        self.counts = {}
        self.recent = {}  # MID -> expiry: a re-delivered request is acknowledged again only
        self.last = None

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

    def handle(self, message, received_at):
        if message.message_type != REQUEST:
            return None
        if (
            message.source != self.binding.controller_al
            or message.destination != self.binding.local_al
            or message.relay
        ):
            invalid("steering request from an unbound controller or envelope")
        now = self.clock()
        if now >= received_at + 1:
            raise EmosaError(Reason.NOT_READY, "steering Ack deadline expired")
        self.recent = {m: t for m, t in self.recent.items() if t > now}
        request = decode_request(message.tlvs)
        snapshot = self._snapshot()
        associated = {
            c.mac
            for b in snapshot.topology.clients.bsses
            if b.bssid == request.source_bssid
            for c in b.clients
        }
        errors = tuple(
            Tlv(ERROR_CODE_TLV, bytes([STA_NOT_ASSOCIATED]) + s)
            for s in request.stations
            if s not in associated
        )
        repeated = message.mid in self.recent
        self._send(ACK, message.mid, errors, snapshot, received_at + 1)
        if repeated:
            return self._record("client_steering_repeated_ack_sent")
        if len(self.recent) >= 64:
            raise EmosaError(Reason.NOT_READY, "steering request budget exhausted")
        self.recent[message.mid] = now + 5
        reason = refusal(request, associated)
        if reason is None:
            reason = self.executor(request, message.mid)
        self.last = {"mid": message.mid, "request": request.record(), "refused": reason}
        if reason == "opportunity":
            # The window ends at once: nothing is steered on the agent's own account.
            self._send(COMPLETED, self.mids.next(), (), snapshot, self.clock() + 1)
            return self._record("client_steering_opportunity_completed")
        if reason is not None:
            return self._record(f"client_steering_refused_{reason}")
        return self._record("client_steering_started")

    def status(self):
        return {"counts": dict(self.counts), "last": self.last}
