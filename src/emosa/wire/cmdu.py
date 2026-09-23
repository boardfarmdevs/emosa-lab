"""Bounded IEEE 1905.1-2013 + 1905.1a-2014 / EasyMesh 6.1 envelope.

See doc/protocol/ieee1905-envelope.md for normative rules and local limits.
This layer preserves TLV occurrences; only a message-specific interpreter can
decide aggregation, required fields, supported values and application authority.
"""

import struct
import time
from dataclasses import dataclass, field

from emosa.errors import EmosaError, Reason

ETHERTYPE = 0x893A
MULTICAST = bytes.fromhex("0180c2000013")
HEADER = struct.Struct("!BBHHBB")
MAX_CMDU = 1500
MAX_VALUE = 0x3FFF
MAX_TLVS = 256
END = b"\x00\x00\x00"


def invalid(message):
    raise EmosaError(Reason.INVALID_INPUT, message)


def uint(value, bits, label):
    if type(value) is not int or not 0 <= value < 1 << bits:
        invalid(f"invalid {label}")
    return value


def mac(value):
    if not isinstance(value, bytes) or len(value) != 6:
        invalid("MAC address must contain six octets")
    return value


@dataclass(frozen=True)
class Tlv:
    kind: int
    value: bytes

    def encode(self):
        uint(self.kind, 8, "TLV type")
        if self.kind == 0 or not isinstance(self.value, bytes) or len(self.value) > MAX_VALUE:
            invalid("invalid TLV value or explicit end marker")
        return struct.pack("!BH", self.kind, len(self.value)) + self.value


def parse_tlvs(payload, *, final):
    """Mask reserved length bits; only the final fragment may contain EOM.

    Bytes following EOM are Ethernet padding and are never interpreted as TLVs.
    No TLV value or raw frame is included in an error.
    """
    offset, tlvs = 0, []
    while offset < len(payload):
        if len(payload) - offset < 3:
            invalid("truncated TLV header")
        kind, length = struct.unpack_from("!BH", payload, offset)
        length &= MAX_VALUE  # 1905.1a section 6.4: reserved field, ignored on receipt.
        offset += 3
        if kind == 0:
            if length or not final:
                invalid("invalid or non-final end marker")
            return tuple(tlvs)
        if offset + length > len(payload):
            invalid("truncated TLV value or unsupported octet-boundary fragmentation")
        if len(tlvs) >= MAX_TLVS:
            invalid("TLV count exceeds local budget")
        tlvs.append(Tlv(kind, payload[offset : offset + length]))
        offset += length
    if final:
        invalid("missing final end marker")
    if not tlvs:
        invalid("empty non-final fragment")
    return tuple(tlvs)


@dataclass(frozen=True)
class Fragment:
    destination: bytes
    source: bytes
    message_type: int
    mid: int
    fid: int
    last: bool
    relay: bool
    payload: bytes
    version: int = 0

    def encode(self):
        """Structural encoder. The caller owns message/peer transmission policy."""
        mac(self.destination)
        mac(self.source)
        for value, bits, label in (
            (self.message_type, 16, "message type"),
            (self.mid, 16, "MID"),
            (self.fid, 8, "FID"),
        ):
            uint(value, bits, label)
        if self.version != 0 or type(self.last) is not bool or type(self.relay) is not bool:
            invalid("cannot transmit reserved version or invalid flags")
        if not isinstance(self.payload, bytes) or len(self.payload) > MAX_CMDU - HEADER.size:
            invalid("fragment exceeds Ethernet CMDU budget")
        header = HEADER.pack(
            0,
            0,
            self.message_type,
            self.mid,
            self.fid,
            (0x80 if self.last else 0) | (0x40 if self.relay else 0),
        )
        return self.destination + self.source + struct.pack("!H", ETHERTYPE) + header + self.payload


def decode_frame(frame):
    """Ethernet II bytes, without FCS or VLAN tags; preserve unknown versions."""
    if not isinstance(frame, bytes) or not 22 <= len(frame) <= 14 + MAX_CMDU:
        invalid("truncated or oversized Ethernet frame")
    if struct.unpack_from("!H", frame, 12)[0] != ETHERTYPE:
        invalid("not an untagged IEEE 1905 Ethernet frame")
    version, _reserved, kind, mid, fid, flags = HEADER.unpack_from(frame, 14)
    return Fragment(
        frame[:6],
        frame[6:12],
        kind,
        mid,
        fid,
        bool(flags & 0x80),
        bool(flags & 0x40),
        frame[22:],
        version,
    )


def fragment_message(destination, source, message_type, mid, tlvs, *, relay=False, mtu=1500):
    """TLV-boundary fragmentation for the initial non-DPP EasyMesh subset.

    No speculative octet splitting: section 15.2 permits it only for a specific
    DPP-capable unicast peer/procedure. That negotiated path is not implemented.
    """
    if type(mtu) is not int or not 14 <= mtu <= MAX_CMDU:
        invalid("invalid CMDU MTU")
    if not isinstance(tlvs, (list, tuple)) or len(tlvs) > MAX_TLVS:
        invalid("TLV count exceeds local budget")
    budget = mtu - HEADER.size
    chunks, current = [], b""
    for tlv in tlvs:
        encoded = tlv.encode()
        # Leave room for EOM in the final fragment; never send EOM-only fragment.
        if len(encoded) + len(END) > budget:
            invalid("TLV too large for supported TLV-boundary fragmentation")
        if len(current) + len(encoded) + len(END) > budget:
            chunks.append(current)
            current = b""
        current += encoded
    chunks.append(current + END)
    if len(chunks) > 64:
        invalid("fragment count exceeds local budget")
    return tuple(
        Fragment(
            destination, source, message_type, mid, fid, fid == len(chunks) - 1, relay, payload
        ).encode()
        for fid, payload in enumerate(chunks)
    )


@dataclass(frozen=True)
class Message:
    destination: bytes
    source: bytes
    message_type: int
    mid: int
    relay: bool
    tlvs: tuple[Tlv, ...]
    fragments: int


@dataclass
class Assembly:
    started: float
    relay: bool
    parts: dict = field(default_factory=dict)
    last: int | None = None
    size: int = 0
    poisoned: bool = False


class Reassembler:
    """Fixed-deadline, bounded contexts. Conflicting fragments poison a context.

    Unknown version messages are not interpreted here; an eventual relay layer
    must handle their forwarding separately under amended section 7.9.
    """

    def __init__(
        self,
        *,
        clock=time.monotonic,
        timeout=5.0,
        max_contexts=64,
        max_bytes=2 * 1024 * 1024,
        max_message_bytes=65536,
        max_fragments=64,
    ):
        if not 0 < timeout <= 60 or not 1 <= max_contexts <= 1024:
            invalid("invalid reassembly timeout or context budget")
        if not 1 <= max_fragments <= 256 or not 1 <= max_message_bytes <= max_bytes <= 16777216:
            invalid("invalid reassembly size budget")
        self.clock, self.timeout = clock, timeout
        self.max_contexts, self.max_bytes = max_contexts, max_bytes
        self.max_message_bytes, self.max_fragments = max_message_bytes, max_fragments
        self.contexts = {}
        self.buffered = 0

    def expire(self):
        now = self.clock()
        expired = [
            key for key, value in self.contexts.items() if now - value.started >= self.timeout
        ]
        for key in expired:
            self.buffered -= self.contexts.pop(key).size
        return len(expired)

    def _poison(self, state, reason):
        self.buffered -= state.size
        state.parts.clear()
        state.size = 0
        state.poisoned = True
        invalid(reason)

    def feed(self, frame, *, ingress="offline"):
        self.expire()
        part = decode_frame(frame)
        if part.version != 0:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "reserved CMDU version; no dispatch")
        key = (ingress, part.source, part.destination, part.message_type, part.mid)
        state = self.contexts.get(key)
        if state is None:
            if len(self.contexts) >= self.max_contexts:
                raise EmosaError(Reason.BUSY, "reassembly context budget exhausted")
            state = self.contexts[key] = Assembly(self.clock(), part.relay)
        if state.poisoned:
            invalid("conflicting message is quarantined until its original deadline")
        if part.fid >= self.max_fragments or part.relay != state.relay:
            self._poison(state, "fragment index or relay mismatch")
        try:
            tlvs = parse_tlvs(part.payload, final=part.last)
        except EmosaError:
            self._poison(state, "invalid fragment TLV boundaries or end marker")
        # Ignore reserved bits and padding when comparing retransmitted fragments.
        normalized = (part.last, tlvs)
        if part.fid in state.parts:
            if state.parts[part.fid] != normalized:
                self._poison(state, "conflicting duplicate fragment")
            return None
        if state.last is not None and (
            part.fid > state.last or (part.last and part.fid != state.last)
        ):
            self._poison(state, "inconsistent final fragment")
        if part.last and any(fid > part.fid for fid in state.parts):
            self._poison(state, "final fragment precedes stored fragments")
        size = sum(len(tlv.value) + 3 for tlv in tlvs)
        if state.size + size > self.max_message_bytes or self.buffered + size > self.max_bytes:
            self._poison(state, "reassembly byte budget exhausted")
        if sum(len(item[1]) for item in state.parts.values()) + len(tlvs) > MAX_TLVS:
            self._poison(state, "reassembled TLV count exceeds local budget")
        state.parts[part.fid] = normalized
        state.size += size
        self.buffered += size
        if part.last:
            state.last = part.fid
        if state.last is None or len(state.parts) != state.last + 1:
            return None
        values = tuple(tlv for fid in range(state.last + 1) for tlv in state.parts[fid][1])
        self.buffered -= state.size
        del self.contexts[key]
        return Message(
            part.destination,
            part.source,
            part.message_type,
            part.mid,
            part.relay,
            values,
            state.last + 1,
        )


class MidSequence:
    """New-message IDs only. Responses copy a request MID where its rule requires."""

    def __init__(self, previous):
        self.previous = uint(previous, 16, "previous MID")

    def next(self):
        self.previous = (self.previous + 1) & 0xFFFF
        return self.previous


class DuplicateWindow:
    """Call only after deriving the originating AL identity from valid message data.

    Do not mistake a relay's Ethernet source for the originator, or use this
    transport window as durable operation idempotency or peer authentication.
    """

    def __init__(self, *, clock=time.monotonic, timeout=60.0, capacity=4096):
        if not 0 < timeout <= 3600 or not 1 <= capacity <= 65536:
            invalid("invalid duplicate window")
        self.clock, self.timeout, self.capacity = clock, timeout, capacity
        self.entries = {}

    def seen(self, origin_al, mid):
        now = self.clock()
        self.entries = {key: end for key, end in self.entries.items() if end > now}
        key = (mac(origin_al), uint(mid, 16, "MID"))
        if key in self.entries:
            return True
        if len(self.entries) >= self.capacity:
            raise EmosaError(Reason.BUSY, "duplicate window budget exhausted")
        self.entries[key] = now + self.timeout
        return False
