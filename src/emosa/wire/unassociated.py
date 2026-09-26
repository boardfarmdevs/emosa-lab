"""Unassociated STA Link Metrics (EasyMesh §11, spec §3.9).

An Unassociated STA Link Metrics Query (``0x800F``) carries one query TLV
(``0x97``): an operating class and, per channel, the stations to measure. The
agent acknowledges it within one second (1905 Ack) with an Error Code TLV
(``0xA3``) for every station it cannot report:

- reason ``0x01``: the station is associated with a BSS of the pod;
- reason ``0x02``: the pod has not heard the station on the requested
  operating class and channel within ``PROBE_LIFETIME`` (a channel other than
  the pod's operating channel is never heard: the pod does not scan off
  channel).

The other stations follow in an Unassociated STA Link Metrics Response
(``0x8010``, TLV ``0x98``): per station the channel, the time since the probe
request it was measured on and the uplink RCPI from that probe's SNR (as for
associated stations, §3.8). When every station is refused, the Ack is the
whole answer.

The measurements are the pod's own: the probe requests its band steering
records for the stations it watches (``emosa.opensync.stats``). Without them
every station is refused; nothing is filled in.
"""

import time

from emosa.errors import EmosaError, Reason
from emosa.operating_classes import GLOBAL_OPERATING_CLASSES
from emosa.wire.autoconfiguration import SECURITY_ENVELOPES
from emosa.wire.cmdu import Tlv, fragment_message, invalid
from emosa.wire.pod_metrics import rcpi
from emosa.wire.reports import PreparedReport

QUERY = 0x800F
RESPONSE = 0x8010
ACK = 0x8000
QUERY_TLV = 0x97
RESPONSE_TLV = 0x98
ERROR_CODE_TLV = 0xA3
STA_ASSOCIATED = 0x01
STA_NOT_HEARD = 0x02
PROBE_LIFETIME = 120  # seconds: a probe older than this is not a measurement
MAX_STATIONS = 64  # the controller's own bound on one query (EM_MAX_UNASSOC_STA)


def decode_query(tlvs):
    """The one query TLV: (operating class, ((channel, (station, ...)), ...))."""
    values = [t.value for t in tlvs if t.kind == QUERY_TLV]
    if len(values) != 1:
        invalid("one Unassociated STA Link Metrics Query TLV required")
    body = values[0]
    if len(body) < 2:
        invalid("truncated unassociated station query")
    op_class, count, at = body[0], body[1], 2
    channels, seen = [], set()
    for _ in range(count):
        if at + 2 > len(body):
            invalid("truncated unassociated station query channel")
        channel, n = body[at], body[at + 1]
        at += 2
        if at + 6 * n > len(body):
            invalid("truncated unassociated station list")
        stations = []
        for i in range(n):
            mac = body[at + 6 * i : at + 6 * i + 6]
            if not any(mac) or mac[0] & 1:
                invalid("queried station must be a unicast MAC address")
            if mac not in seen:
                seen.add(mac)
                stations.append(mac)
        at += 6 * n
        channels.append((channel, tuple(stations)))
    if at != len(body):
        invalid("trailing unassociated station query octets")
    if not seen:
        invalid("unassociated station query names no station")
    if len(seen) > MAX_STATIONS:
        invalid("unassociated station query exceeds the station bound")
    return op_class, tuple(channels)


class UnassociatedCoordinator:
    """Answers Unassociated STA Link Metrics Queries from the pod's probe requests.

    ``probes`` is the pod's statistics cache (``PodStats``) or None when the
    agent has no telemetry; ``probe(mac)`` gives the station's last probe.
    ``watch(macs)``, when given, is told the stations queried on the pod's own
    operating class and channel that are not associated with it: the pod is
    to watch their probe requests (``emosa.agent.probe_watch``).
    """

    def __init__(
        self,
        source,
        send_frame,
        mids,
        *,
        probes=None,
        watch=None,
        clock=time.monotonic,
        wall=time.time,
    ):
        self.source, self.binding, self.send_frame = source, source.binding, send_frame
        self.mids, self.probes, self.watch, self.clock, self.wall = (
            mids,
            probes,
            watch,
            clock,
            wall,
        )
        self.counts = {}
        self.last = None
        self.closed = False

    def record(self, event):
        self.counts[event] = self.counts.get(event, 0) + 1
        return event

    def snapshot(self):
        snapshot = self.source.current()
        if self.closed or snapshot is None:
            raise EmosaError(Reason.NOT_READY, "the pod's state is not available")
        return snapshot

    def send(self, message_type, mid, tlvs, snapshot, deadline):
        PreparedReport(
            message_type,
            mid,
            snapshot.stamp,
            min(deadline, snapshot.stamp.valid_until),
            fragment_message(
                self.binding.controller_al, self.binding.local_al, message_type, mid, tlvs
            ),
        ).send(self.send_frame, lambda: self.snapshot().stamp, clock=self.clock)

    def measure(self, op_class, channel, station, *, associated, operating, now):
        """(channel, age ms, RCPI) of the station's last probe, or the refusal reason."""
        if station in associated:
            return STA_ASSOCIATED
        if operating is None or (op_class, channel) != operating:
            return STA_NOT_HEARD
        probe = self.probes.probe(station.hex(":")) if self.probes is not None else None
        if (
            probe is None
            or probe.band != GLOBAL_OPERATING_CLASSES.get(op_class, (None,))[0]
            or not 0 <= now - probe.measured_at <= PROBE_LIFETIME
        ):
            return STA_NOT_HEARD
        return channel, round((now - probe.measured_at) * 1000), rcpi(probe.snr_db)

    def handle(self, message, received_at):
        if message.message_type != QUERY:
            return None
        if (
            message.source != self.binding.controller_al
            or message.destination != self.binding.local_al
            or message.relay
            or any(t.kind in SECURITY_ENVELOPES for t in message.tlvs)
        ):
            invalid("unassociated station query from an unbound controller or envelope")
        if self.clock() >= received_at + 1:
            raise EmosaError(Reason.NOT_READY, "unassociated station query Ack deadline expired")
        op_class, channels = decode_query(message.tlvs)
        snapshot = self.snapshot()
        associated = {c.mac for b in snapshot.topology.clients.bsses for c in b.clients}
        radios = snapshot.operating_radios
        operating = (radios[0].operating_class, radios[0].channel) if len(radios) == 1 else None
        now = self.wall()
        errors, measured, heard_here = [], [], []
        for channel, stations in channels:
            if (op_class, channel) == operating:
                heard_here += [m for m in stations if m not in associated]
            for station in stations:
                result = self.measure(
                    op_class, channel, station, associated=associated, operating=operating, now=now
                )
                if isinstance(result, int):
                    errors.append(Tlv(ERROR_CODE_TLV, bytes([result]) + station))
                else:
                    measured.append((station, *result))
        self.send(ACK, message.mid, tuple(errors), snapshot, received_at + 1)
        if self.watch is not None and heard_here:
            self.watch([m.hex(":") for m in heard_here])
        self.last = {
            "mid": message.mid,
            "operating_class": op_class,
            "queried": sum(len(s) for _, s in channels),
            "measured": len(measured),
        }
        if not measured:
            return self.record("unassociated_query_refused")
        body = bytearray((op_class, len(measured)))
        for station, channel, age_ms, value in measured:
            body += station + bytes((channel,)) + age_ms.to_bytes(4, "big") + bytes((value,))
        self.send(
            RESPONSE,
            self.mids.next(),
            (Tlv(RESPONSE_TLV, bytes(body)),),
            snapshot,
            self.clock() + 1,
        )
        return self.record("unassociated_metrics_response_sent")

    def status(self):
        return {
            "counts": dict(self.counts),
            "last": self.last,
            "measurement_source": "probe_requests" if self.probes is not None else None,
        }

    def close(self):
        self.closed = True
