"""Bind pod forwarding identities to passively observed native peer discovery.

Bounded owned-lab profile, not authentication or a generic topology database.
IEEE 1905.1-2013 §§6.1, 6.3.1, 8.1 and 8.2.1; 2014 amendment reserved-field rules.
"""

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass

from emosa.errors import EmosaError
from emosa.wire.cmdu import MULTICAST, decode_frame, parse_tlvs

DISCOVERY_LEASE_NS = 65_000_000_000  # Local conservative lease; transmission period is <=61 s.


def unicast(value):
    if not isinstance(value, bytes) or len(value) != 6 or not any(value) or value[0] & 1:
        raise ValueError("invalid discovery identity")
    return value


def topology_discovery(frame):
    part = decode_frame(frame)
    if (
        part.version != 0
        or part.message_type != 0
        or part.destination != MULTICAST
        or part.fid != 0
        or not part.last
        or part.relay
    ):
        raise ValueError("unsupported topology discovery envelope")
    tlvs = parse_tlvs(part.payload, final=True)
    al = [t.value for t in tlvs if t.kind == 1]
    interface = [t.value for t in tlvs if t.kind == 2]
    if len(al) != 1 or len(interface) != 1:
        raise ValueError("missing or duplicate discovery identities")
    pair = (unicast(al[0]), unicast(interface[0]))
    if part.source not in pair:
        raise ValueError("discovery source is not its advertised AL/interface")
    return pair


def bridge_discovery(frame):
    if len(frame) < 16 or frame[:6] != bytes.fromhex("0180c200000e") or frame[12:14] != b"\x88\xcc":
        raise ValueError("unsupported LLDP envelope")
    fields, offset, ended = {}, 14, False
    while offset + 2 <= len(frame):
        header = int.from_bytes(frame[offset : offset + 2], "big")
        kind, size = header >> 9, header & 511
        offset += 2
        if offset + size > len(frame):
            raise ValueError("truncated LLDP TLV")
        if kind == 0:
            if size:
                raise ValueError("invalid LLDP end")
            ended = True
            break
        if kind in (1, 2, 3):
            if kind in fields:
                raise ValueError("duplicate LLDP identity/TTL")
            fields[kind] = frame[offset : offset + size]
        offset += size
    if (
        not ended
        or set(fields) != {1, 2, 3}
        or len(fields[1]) != 7
        or len(fields[2]) != 7
        or fields[1][0] != 4
        or fields[2][0] != 3
        or fields[3] != b"\x00\xb4"
    ):
        raise ValueError("LLDP is not the selected IEEE 1905 bridge-discovery shape")
    pair = (unicast(fields[1][1:]), unicast(fields[2][1:]))
    if frame[6:12] not in pair:
        raise ValueError("LLDP source is not its advertised AL/interface")
    return pair


@dataclass(frozen=True)
class NeighborBinding:
    generation: int
    capture_epoch: str
    local_interface: str
    local_ifindex: int
    neighbor_al: str
    neighbor_interface: str
    discovery_received_ns: int
    bridges_present: bool
    valid_until_ns: int


class NeighborSource:
    """Reject unhealthy, stale, mismatched or ambiguous source observations."""

    def __init__(self, controller_al, local_al, run_label, *, clock=time.monotonic_ns):
        self.controller, self.local = unicast(controller_al), unicast(local_al)
        self.run_label, self.clock = run_label, clock
        self.binding = None
        self.reason = "awaiting_discovery"
        self.watermarks = {}

    def invalidate(self, reason="source_unavailable"):
        self.binding, self.reason = None, reason

    def refresh(self, sample):
        self.invalidate()
        try:
            if sample is None:
                return
            payload = sample.neighbor_observation
            now = self.clock()
            port = sample.interfaces["eth1"]
            if (
                type(payload) is not dict
                or payload["running"] is not True
                or payload["errors"]
                or payload["run_label"] != self.run_label
                or payload["interface"] != "eth1"
                or payload["ifindex"] != port["ifindex"]
                or payload["mac"] != port["mac"]
                or payload["boot_id"] != sample.epoch[1]
                or payload["netns_inode"] != sample.epoch[2]
                or type(payload["heartbeat_ns"]) is not int
                or not payload["heartbeat_ns"] <= now < payload["heartbeat_ns"] + 2_000_000_000
                or now >= sample.started_ns + 2_000_000_000
            ):
                raise ValueError("listener and forwarding observation are not current and bound")
            epoch = str(uuid.UUID(payload["capture_epoch"]))
            if len(self.watermarks) >= 8 and epoch not in self.watermarks:
                raise ValueError("capture restart budget exhausted")
            watermark, digest = self.watermarks.get(epoch, (0, None))
            actual_digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            if payload["heartbeat_ns"] < watermark or (
                payload["heartbeat_ns"] == watermark and actual_digest != digest
            ):
                raise ValueError("listener heartbeat replayed")
            self.watermarks[epoch] = (payload["heartbeat_ns"], actual_digest)
            frames = payload["frames"]
            if type(frames) is not list or len(frames) > 64:
                raise ValueError("discovery inventory budget")
            discoveries, bridges, firsts = {}, {}, {}
            previous = 0
            for item in frames:
                received = item["received_ns"]
                if (
                    type(received) is not int
                    or not previous < received <= payload["heartbeat_ns"]
                    or not isinstance(item["frame_hex"], str)
                    or len(item["frame_hex"]) > 3028
                ):
                    raise ValueError("invalid listener frame chronology")
                previous = received
                frame = bytes.fromhex(item["frame_hex"])
                try:
                    if frame[12:14] == b"\x89\x3a":
                        pair = topology_discovery(frame)
                        if pair[0] != self.local and now - received < DISCOVERY_LEASE_NS:
                            discoveries[pair] = received
                            firsts.setdefault(pair, received)
                    elif frame[12:14] == b"\x88\xcc":
                        pair = bridge_discovery(frame)
                        if now - received < 180_000_000_000:
                            bridges[pair] = received
                except (ValueError, EmosaError):
                    continue  # Malformed/unknown network frames never authorize a binding.
            if len(discoveries) != 1:
                self.reason = "awaiting_or_ambiguous_discovery"
                return
            ((pair, received),) = discoveries.items()
            if pair[0] != self.controller:
                raise ValueError("neighbor differs from the selected controller")
            # Allow the corresponding LLDP frame to arrive after Discovery.
            if pair not in bridges and now - firsts[pair] < 1_000_000_000:
                self.reason = "awaiting_bridge_discovery"
                return
            until = min(
                sample.started_ns + 2_000_000_000,
                payload["heartbeat_ns"] + 2_000_000_000,
                received + DISCOVERY_LEASE_NS,
            )
            if pair in bridges:
                until = min(until, bridges[pair] + 180_000_000_000)
            self.binding = NeighborBinding(
                sample.generation,
                epoch,
                port["mac"],
                port["ifindex"],
                pair[0].hex(":"),
                pair[1].hex(":"),
                received,
                pair not in bridges,
                until,
            )
            self.reason = "observed_neighbor_bound"
        except (KeyError, TypeError, ValueError, EmosaError):
            self.invalidate("invalid_or_stale_discovery_source")

    def current(self):
        if self.binding and self.clock() >= self.binding.valid_until_ns:
            self.invalidate("binding_expired")
        return self.binding

    def status(self):
        value = self.current()
        return {
            "available": value is not None,
            "reason": self.reason,
            "binding": asdict(value) if value else None,
            "measurement_source_qualified": False,
            "media_representation": "owned-simulator Ethernet fixture; physical PHY unqualified",
        }
