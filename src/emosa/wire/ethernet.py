# SPDX-License-Identifier: Apache-2.0
"""Single-interface Linux packet endpoint, with explicit lifetime and no pod writes.

The caller supplies an isolated lab interface and owns link authentication,
message policy and identity. Opening a socket does not authenticate a controller.
No bridge, VLAN, promiscuous mode, MAC, radio or interface configuration changes.
"""

import os
import socket
import struct

from emosa.errors import EmosaError, Reason
from emosa.wire.cmdu import ETHERTYPE, MULTICAST, decode_frame, invalid, mac


class EthernetEndpoint:
    def __init__(self, interface, local_mac, *, timeout=1.0, netns=None):
        """netns: the interface's network namespace (a name under /run/netns, or a path such
        as /proc/PID/ns/net; none or empty: this one). The socket is opened there and the
        thread returns to its own namespace: on a gateway whose EasyMesh controller is local,
        the agents' interfaces, each with its agent's AL MAC, live in a namespace of their
        own, since RDK's controller takes an agent whose AL MAC is the MAC of one of its own
        interfaces for its co-located agent (rdk-1004, 5 October 2026)."""
        if not isinstance(interface, str) or not 1 <= len(interface.encode()) <= 15:
            invalid("invalid Ethernet interface name")
        mac(local_mac)
        if local_mac[0] & 1 or not any(local_mac) or not 0 < timeout <= 60:
            invalid("invalid local unicast MAC or socket timeout")
        self.local_mac = local_mac
        self.socket = None
        if netns:
            path = netns if "/" in netns else f"/run/netns/{netns}"
            try:
                own = os.open("/proc/self/ns/net", os.O_RDONLY)
                try:
                    there = os.open(path, os.O_RDONLY)
                    try:
                        os.setns(there, os.CLONE_NEWNET)
                        try:
                            self._open(interface, local_mac, timeout)
                        finally:
                            # back to the agent's own namespace; the socket stays where it
                            # was opened
                            os.setns(own, os.CLONE_NEWNET)
                    finally:
                        os.close(there)
                finally:
                    os.close(own)
            except OSError as exc:
                self.close()
                raise EmosaError(
                    Reason.NOT_READY, f"network namespace {netns} unavailable"
                ) from exc
            return
        self._open(interface, local_mac, timeout)

    def _open(self, interface, local_mac, timeout):
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
        try:
            fragment = decode_frame(frame)
        except EmosaError:
            # not a 1905 frame (a runt, an oversized frame): dropped, as any host on the
            # link can send one
            return None
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
