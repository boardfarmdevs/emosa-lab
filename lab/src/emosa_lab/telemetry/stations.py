"""Selected OpenSync client-report decoding; no implied physical qualification.

The owned lab profile binds one MQTT topic and nodeID to one radio/BSS. Its
publisher sends complete snapshots with association offsets measured by hostapd.
Other publishers require their own reviewed sampling/identity contract.
"""

import re
import time
from dataclasses import dataclass

from google.protobuf.message import DecodeError

# The pinned schema lives with the adapter (emosa.opensync.stats).
from emosa.opensync.stats import DESCRIPTOR_SHA256, PROTO_SHA256, Report  # noqa: F401

MAC = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")


@dataclass(frozen=True)
class StationSample:
    timestamp_ms: int
    observed_at: float
    valid_until: float
    ssid: str
    clients: tuple[tuple[bytes, int], ...]


class StationSource:
    """Bounded, event-loop-owned cache. Re-delivery never renews freshness."""

    def __init__(self, node_id, topic, *, clock=time.monotonic, wall=time.time):
        self.node_id, self.topic = node_id, topic
        self.clock, self.wall = clock, wall
        self.sample = None
        self.last_timestamp = -1
        self.generation = 0
        self.accepted = self.rejected = 0

    def disconnect(self):
        self.sample = None
        self.generation += 1
        # Retain the watermark across reconnect: a queued old sample is not new.

    def receive(self, topic, payload, *, retained=False):
        try:
            if retained or topic != self.topic or not 1 <= len(payload) <= 65536:
                raise ValueError("unqualified telemetry delivery")
            report = Report.FromString(payload)
            if not report.IsInitialized() or report.nodeID != self.node_id:
                raise ValueError("missing fields or wrong pod identity")
            if len(report.clients) != 1:
                raise ValueError("expected a complete sole-radio client report")
            radio = report.clients[0]
            if radio.band != 0 or radio.channel != 6 or not radio.HasField("timestamp_ms"):
                raise ValueError("report outside the selected radio contract")
            timestamp = radio.timestamp_ms
            age = self.wall() - timestamp / 1000
            if timestamp <= self.last_timestamp or not 0 <= age < 2:
                raise ValueError("duplicate, late, out-of-order or future observation")
            if len(radio.client_list) > 64:
                raise ValueError("station inventory exceeds selected bound")
            clients, ssids = {}, set()
            for client in radio.client_list:
                mac = client.mac_address
                if (
                    MAC.fullmatch(mac) is None
                    or int(mac[:2], 16) & 1
                    or mac == "00:00:00:00:00:00"
                    or not client.HasField("connected")
                    or not client.connected
                    or not client.HasField("ssid")
                    or not client.HasField("connect_offset_ms")
                    or client.HasField("mld_address")
                    or mac in clients
                ):
                    raise ValueError("ambiguous or incomplete station observation")
                clients[mac] = min(65535, client.connect_offset_ms // 1000)
                ssids.add(client.ssid)
            if len(ssids) > 1:
                raise ValueError("more than one BSS in sole-BSS report")
            observed_at = self.clock() - age
            sample = StationSample(
                timestamp,
                observed_at,
                observed_at + 2,
                next(iter(ssids), ""),
                tuple(
                    (bytes.fromhex(mac.replace(":", "")), n) for mac, n in sorted(clients.items())
                ),
            )
        except (ValueError, DecodeError):
            self.rejected += 1
            return False
        self.sample, self.last_timestamp = sample, timestamp
        self.accepted += 1
        return True

    def current(self):
        if self.sample is not None and self.clock() < self.sample.valid_until:
            return self.sample
        return None
