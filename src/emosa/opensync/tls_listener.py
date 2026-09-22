"""Bounded TLS accept boundary for the pinned upstream OVS JSON-RPC session.

Only authenticated streams reach ovs.jsonrpc.Connection. Framing, parsing,
monitoring and transactions remain upstream. No global OVS TLS settings are used.
"""

import errno
import hashlib
import ipaddress
import socket
import ssl
import time

import ovs.stream

from emosa.errors import EmosaError, Reason


def listener_address(endpoint):
    try:
        kind, port, host = endpoint.split(":")
        address = ipaddress.IPv4Address(host)
        if kind != "pssl" or not 1 <= int(port) <= 65535:
            raise ValueError
        return str(address), int(port)
    except ValueError as exc:
        raise EmosaError(Reason.INVALID_INPUT, "TLS listener requires pssl:PORT:IPv4") from exc


def server_context(files):
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(files["ca"])
        context.load_cert_chain(files["certificate"], files["private_key"])
        return context
    except (OSError, ValueError, ssl.SSLError) as exc:
        raise EmosaError(Reason.INVALID_INPUT, "cannot load private TLS listener material") from exc


class TlsListener:
    """One explicitly bound pod; four pending handshakes with a three-second budget.

    Rejected and incomplete handshakes never replace an active OVS session.
    The pinned upstream session accepts a new *authenticated* connection and
    invalidates the old generation. This is reconnect, not automatic enrollment.
    """

    def __init__(self, endpoint, context, pin, *, pending_limit=4, handshake_seconds=3):
        self.name, self.context, self.pin = endpoint, context, pin
        self.pending_limit, self.handshake_seconds = pending_limit, handshake_seconds
        self.pending = {}
        self.accepted = self.rejected = self.expired = self.overflow = 0
        self.pin_rejected = 0
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.setblocking(False)
            self.socket.bind(listener_address(endpoint))
            self.socket.listen(pending_limit)
        except OSError as exc:
            self.socket.close()
            raise EmosaError(Reason.NOT_READY, "cannot bind configured TLS listener") from exc

    def accept(self):
        # Bounded work per pump, even while another pod or connection is active.
        now = time.monotonic()
        for _ in range(self.pending_limit):
            try:
                sock, _ = self.socket.accept()
            except BlockingIOError:
                break
            except OSError as exc:
                raise EmosaError(Reason.NOT_READY, "TLS listener unavailable") from exc
            if len(self.pending) >= self.pending_limit:
                sock.close()
                self.overflow += 1
                continue
            try:
                sock.setblocking(False)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                secure = self.context.wrap_socket(
                    sock, server_side=True, do_handshake_on_connect=False
                )
            except (OSError, ssl.SSLError):
                sock.close()
                self.rejected += 1
                continue
            self.pending[secure] = now + self.handshake_seconds
        for sock, deadline in tuple(self.pending.items()):
            if now >= deadline:
                self.expired += 1
            else:
                try:
                    sock.do_handshake()
                    certificate = sock.getpeercert(binary_form=True)
                    if certificate and hashlib.sha256(certificate).hexdigest() == self.pin:
                        del self.pending[sock]
                        self.accepted += 1
                        return 0, ovs.stream.SSLStream(sock, self.name, 0, is_server=True)
                    self.rejected += 1
                    self.pin_rejected += 1
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    continue
                except (OSError, ssl.SSLError):
                    self.rejected += 1
            del self.pending[sock]
            sock.close()
        return errno.EAGAIN, None

    def close(self):
        for sock in self.pending:
            sock.close()
        self.pending.clear()
        self.socket.close()

    def statistics(self):
        return {
            "pending": len(self.pending),
            "pending_limit": self.pending_limit,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "pin_rejected": self.pin_rejected,
            "expired": self.expired,
            "overflow": self.overflow,
        }
