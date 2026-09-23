"""Selected IEEE 1905 topology values, with no inferred links or bridge state.

IEEE 1905.1-2013 6.4.5-9, 1905.1a-2014 and EasyMesh 6.1 Table 14.
Reserved media/role values require ignoring the whole TLV, not substituting an
Ethernet interface; this component reports UNSUPPORTED_OPERATION to its caller.
"""

from dataclasses import dataclass
from typing import ClassVar

from emosa.errors import EmosaError, Reason
from emosa.wire.cmdu import MAX_VALUE, invalid, mac, uint


@dataclass(frozen=True)
class LocalInterface:
    mac: bytes
    media_type: int
    media_specific: bytes


@dataclass(frozen=True)
class DeviceInformation:
    kind: ClassVar[int] = 3
    al_mac: bytes
    interfaces: tuple[LocalInterface, ...]


@dataclass(frozen=True)
class BridgingCapability:
    kind: ClassVar[int] = 4
    tuples: tuple[tuple[bytes, ...], ...]


@dataclass(frozen=True)
class Non1905Neighbors:
    kind: ClassVar[int] = 6
    local_interface: bytes
    neighbors: tuple[bytes, ...]


@dataclass(frozen=True)
class Neighbor:
    al_mac: bytes
    bridges_present: bool


@dataclass(frozen=True)
class Neighbors1905:
    kind: ClassVar[int] = 7
    local_interface: bytes
    neighbors: tuple[Neighbor, ...]


def _media(interface, *, sending):
    kind, value = interface.media_type, interface.media_specific
    uint(kind, 16, "media type")
    if not isinstance(value, bytes):
        invalid("media-specific information must be octets")
    lengths = {0: 0, 1: 0, 0x108: 0, 0x109: 0, 0x200: 7, 0x201: 7, 0x300: 0, 0xFFFF: 0}
    lengths.update({kind: 10 for kind in range(0x100, 0x108)})
    if kind not in lengths:
        raise EmosaError(Reason.UNSUPPORTED_OPERATION, "reserved media value; ignore entire TLV")
    if len(value) != lengths[kind]:
        invalid("incorrect media-specific information length")
    if 0x100 <= kind <= 0x107:
        if value[6] >> 4 not in (0, 4, 8, 9, 10):
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "reserved Wi-Fi role; ignore entire TLV")
        if sending and value[6] & 15:
            invalid("reserved Wi-Fi role bits must be zero on transmission")


def _count(items):
    if type(items) is not tuple:
        invalid("topology lists must be tuples")
    return bytes([uint(len(items), 8, "topology count")])


def encode_topology(value):
    """Value layout only. Complete inventory and cross-reference checks are separate."""
    if type(value) is DeviceInformation:
        data = bytearray(mac(value.al_mac) + _count(value.interfaces))
        for interface in value.interfaces:
            if type(interface) is not LocalInterface:
                invalid("invalid local interface")
            _media(interface, sending=True)
            data.extend(
                mac(interface.mac)
                + interface.media_type.to_bytes(2, "big")
                + bytes([len(interface.media_specific)])
                + interface.media_specific
            )
    elif type(value) is BridgingCapability:
        data = bytearray(_count(value.tuples))
        for group in value.tuples:
            data.extend(_count(group))
            for address in group:
                data.extend(mac(address))
    elif type(value) in (Non1905Neighbors, Neighbors1905):
        data = bytearray(mac(value.local_interface))
        if type(value.neighbors) is not tuple:
            invalid("invalid neighbor list")
        if len(value.neighbors) > (MAX_VALUE - 6) // (6 if type(value) is Non1905Neighbors else 7):
            invalid("neighbor list exceeds the TLV value budget")
        for neighbor in value.neighbors:
            if type(value) is Non1905Neighbors:
                data.extend(mac(neighbor))
            else:
                if type(neighbor) is not Neighbor or type(neighbor.bridges_present) is not bool:
                    invalid("invalid 1905 neighbor")
                data.extend(mac(neighbor.al_mac) + bytes([0x80 if neighbor.bridges_present else 0]))
    else:
        invalid("unknown topology value type")
    if len(data) > MAX_VALUE:
        invalid("topology value exceeds the TLV length budget")
    return bytes(data)


class _Reader:
    def __init__(self, data):
        if not isinstance(data, bytes) or len(data) > MAX_VALUE:
            invalid("invalid topology input size")
        self.data, self.offset = data, 0

    def take(self, count):
        if count > len(self.data) - self.offset:
            invalid("truncated topology value")
        start = self.offset
        self.offset += count
        return self.data[start : self.offset]

    def octet(self):
        return self.take(1)[0]


def decode_topology(kind, data):
    reader = _Reader(data)
    if kind == 3:
        al, count = reader.take(6), reader.octet()
        interfaces = []
        for _ in range(count):
            address, media = reader.take(6), int.from_bytes(reader.take(2), "big")
            interface = LocalInterface(address, media, reader.take(reader.octet()))
            _media(interface, sending=False)
            interfaces.append(interface)
        result = DeviceInformation(al, tuple(interfaces))
    elif kind == 4:
        result = BridgingCapability(
            tuple(
                tuple(reader.take(6) for _ in range(reader.octet())) for _ in range(reader.octet())
            )
        )
    elif kind in (6, 7):
        local = reader.take(6)
        size = 6 if kind == 6 else 7
        remaining = len(data) - reader.offset
        if remaining % size:
            invalid("partial topology neighbor record")
        if kind == 6:
            result = Non1905Neighbors(
                local, tuple(reader.take(6) for _ in range(remaining // size))
            )
        else:
            result = Neighbors1905(
                local,
                tuple(
                    Neighbor(reader.take(6), bool(reader.octet() & 0x80))
                    for _ in range(remaining // size)
                ),
            )
    else:
        raise EmosaError(Reason.UNSUPPORTED_OPERATION, "topology TLV is not implemented")
    if reader.offset != len(data):
        invalid("trailing topology value bytes")
    return result
