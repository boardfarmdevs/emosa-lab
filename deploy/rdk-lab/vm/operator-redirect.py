#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The operator's redirect, in the RDK lab: what an OpenSync operator's cloud does once for
each pod handed to EMOSA (plan 5.3), on the pods' built-in redirector address.

    operator-redirect.py ADDRESS PORT TARGET     e.g. 10.101.0.40 6640 tcp:10.0.0.1:6640

The pod image's redirector is tcp:10.101.0.40:6640, on the gateway's WAN side. With EMOSA
wholly in the gateway, its fleet's front port is on the gateway's LAN address; this answers
the redirector address there and sends each pod on: it reads the pod's AWLAN_Node (its
serial, for the log), writes manager_addr = TARGET and ends the session, as the fleet's front
port does (spec 3.1; OpenSync's connection manager acts on a new manager address once
disconnected). It is the operator's, not EMOSA's: it runs on the WAN side (the VM), outside
the home. Standard library only.
"""

import json
import logging
import socket
import sys
import threading

DATABASE = "Open_vSwitch"
TIMEOUT = 10  # seconds for one pod's exchange
IP_FREEBIND = 15  # linux/in.h: bind before the address is on the bridge
log = logging.getLogger("operator-redirect")


class Session:
    """JSON-RPC over the pod's OVSDB connection (EMOSA the client, the pod the server)."""

    def __init__(self, conn):
        self.conn, self.buffer, self.decoder, self.next_id = conn, "", json.JSONDecoder(), 0

    def _send(self, message):
        self.conn.sendall(json.dumps(message).encode())

    def _messages(self):
        while True:
            text = self.buffer.lstrip()
            try:
                message, end = self.decoder.raw_decode(text)
            except ValueError:
                chunk = self.conn.recv(65536)
                if not chunk:
                    raise ConnectionError("the pod closed the connection") from None
                self.buffer = text + chunk.decode("utf-8", "replace")
                if len(self.buffer) > 1 << 20:
                    raise ConnectionError("a message over 1 MiB") from None
                continue
            self.buffer = text[end:]
            yield message

    def call(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        self._send({"method": method, "params": params, "id": request_id})
        for message in self._messages():
            if message.get("method") == "echo":  # the pod's keepalive
                self._send(
                    {"result": message.get("params"), "error": None, "id": message.get("id")}
                )
            elif message.get("id") == request_id:
                if message.get("error"):
                    raise ConnectionError(f"{method} refused: {message['error']}")
                return message.get("result")


def redirect(conn, peer, target):
    conn.settimeout(TIMEOUT)
    session = Session(conn)
    select = {"op": "select", "table": "AWLAN_Node", "where": [], "columns": ["serial_number"]}
    rows = session.call("transact", [DATABASE, select])[0].get("rows", [])
    serial = rows[0].get("serial_number") if len(rows) == 1 else None
    update = {"op": "update", "table": "AWLAN_Node", "where": [], "row": {"manager_addr": target}}
    result = session.call("transact", [DATABASE, update])
    if not result or result[0].get("count") != 1:
        raise ConnectionError(f"manager_addr update failed: {result}")
    log.info("%s (%s): manager_addr %s", peer, serial or "no serial", target)


def serve(conn, peer, target):
    try:
        redirect(conn, peer, target)
    except (OSError, ConnectionError, ValueError, IndexError, AttributeError) as exc:
        log.warning("%s: not redirected: %s", peer, exc)
    finally:
        conn.close()


def main():
    if len(sys.argv) != 4 or not sys.argv[3].startswith("tcp:"):
        sys.exit("usage: operator-redirect.py ADDRESS PORT tcp:HOST:PORT")
    address, port, target = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.setsockopt(socket.IPPROTO_IP, IP_FREEBIND, 1)
    listener.bind((address, port))
    listener.listen(16)
    log.info("the pods' redirector %s:%d sends them to %s", address, port, target)
    while True:
        conn, (host, peer_port) = listener.accept()
        threading.Thread(
            target=serve, args=(conn, f"{host}:{peer_port}", target), daemon=True
        ).start()


if __name__ == "__main__":
    main()
