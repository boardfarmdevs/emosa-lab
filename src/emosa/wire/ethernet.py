"""Single-interface Linux packet endpoint, with explicit lifetime and no pod writes.

The caller supplies an isolated lab interface and owns link authentication,
message policy and identity. Opening a socket does not authenticate a controller.
No bridge, VLAN, promiscuous mode, MAC, radio or interface configuration changes.
"""

import socket
import struct

from emosa.errors import EmosaError, Reason
from emosa.wire.cmdu import ETHERTYPE, MULTICAST, decode_frame, invalid, mac


class EthernetEndpoint:
    def __init__(self, interface, local_mac, *, timeout=1.0):
        if not isinstance(interface, str) or not 1 <= len(interface.encode()) <= 15:
            invalid("invalid Ethernet interface name")
        mac(local_mac)
        if local_mac[0] & 1 or not any(local_mac) or not 0 < timeout <= 60:
            invalid("invalid local unicast MAC or socket timeout")
        self.local_mac = local_mac
        self.socket = None
        try:
            index = socket.if_nametoindex(interface)
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE))
            self.socket = sock
            sock.bind((interface, ETHERTYPE))
            address = sock.getsockname()
            if address[3] != 1 or address[4] != local_mac:  # ARPHRD_ETHER
                invalid("interface must be Ethernet with the declared local MAC")
            # PACKET_ADD_MEMBERSHIP / PACKET_MR_MULTICAST, scoped to this socket.
            membership = struct.pack("=IHH8s", index, 0, 6, MULTICAST)
            sock.setsockopt(263, 1, membership)
            sock.settimeout(timeout)
        except (OSError, EmosaError) as exc:
            self.close()
            if isinstance(exc, EmosaError):
                raise
            raise EmosaError(
                Reason.NOT_READY, "isolated Ethernet packet socket unavailable"
            ) from exc

    def receive(self):
        try:
            frame, address = self.socket.recvfrom(1515)
        except TimeoutError:
            return None
        if address[2] == 4:  # PACKET_OUTGOING
            return None
        fragment = decode_frame(frame)
        if fragment.destination not in (MULTICAST, self.local_mac):
            return None
        return frame

    def send(self, frame):
        fragment = decode_frame(frame)
        if fragment.source != self.local_mac:
            invalid("transmit source differs from declared interface MAC")
        if fragment.version != 0:
            invalid("reserved CMDU version cannot be originated")
        if self.socket.send(frame) != len(frame):
            raise EmosaError(Reason.OUTCOME_UNKNOWN, "incomplete Ethernet send")

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
