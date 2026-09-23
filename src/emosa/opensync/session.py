"""Persistent upstream OVS JSON-RPC transport, isolated in a bounded worker.

Only basic RFC 7047 monitor is used. No custom JSON-RPC codec or pod database.
"""

import asyncio
import copy
import errno
import hashlib
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import ovs.fatal_signal
import ovs.jsonrpc
import ovs.stream
import ovs.timeval

from emosa.errors import EmosaError, Reason
from emosa.opensync.schema import TABLES, Schema
from emosa.opensync.tls_listener import TlsListener, listener_address, server_context

TLS_PROFILE_LOCK = threading.Lock()
UNIX_SIGNAL_HOOK_READY = False


class BoundedStream:
    """Limit bytes admitted to the upstream parser before message completion."""

    def __init__(self, stream, limit):
        self.inner, self.limit, self.admitted = stream, limit, 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def recv(self, size):
        if self.admitted >= self.limit:
            return errno.EMSGSIZE, b""
        error, data = self.inner.recv(min(size, self.limit - self.admitted))
        self.admitted += len(data)
        return error, data


class OvsSession:
    def __init__(
        self,
        endpoint,
        database="Open_vSwitch",
        *,
        request_limit=16,
        message_limit=16 * 1024 * 1024,
        row_limit=10000,
        timeout=5,
        read_only=False,
        monitor_columns=None,
        tls_files=None,
        peer_certificate_sha256=None,
    ):
        global UNIX_SIGNAL_HOOK_READY
        local_endpoint = endpoint.startswith(("unix:", "punix:", "tcp:127.0.0.1:")) or re.fullmatch(
            r"ptcp:[0-9]+:127\.0\.0\.1", endpoint
        )
        tls_listener = endpoint.startswith("pssl:") and tls_files and peer_certificate_sha256
        tls_endpoint = (
            endpoint.startswith("ssl:") and read_only and tls_files and peer_certificate_sha256
        )
        if not local_endpoint and not tls_endpoint and not tls_listener:
            raise EmosaError(
                Reason.MISSING_PREREQUISITE,
                "only isolated simulation transports qualified; TLS/pod trust pending",
            )
        if (tls_listener or tls_endpoint) and not re.fullmatch(
            r"[0-9a-f]{64}", peer_certificate_sha256
        ):
            raise EmosaError(Reason.INVALID_INPUT, "TLS requires an explicit SHA-256 peer pin")
        self.listener_context = None
        if tls_listener:
            listener_address(endpoint)
            self.listener_context = server_context(tls_files)
        if endpoint.startswith("punix:") and not UNIX_SIGNAL_HOOK_READY:
            # Upstream Unix listeners register unlink hooks on first use. Initialize
            # their signal machinery on the main thread before the bounded worker
            # opens a socket. Existing application handlers are preserved upstream.
            if threading.current_thread() is not threading.main_thread():
                raise EmosaError(Reason.INVALID_INPUT, "create Unix listener on the main thread")
            ovs.fatal_signal.add_hook(lambda: None, None, False)
            UNIX_SIGNAL_HOOK_READY = True
        # ovs Python PassiveStream uses host:port, unlike the C CLI's port:host.
        if endpoint.startswith("ptcp:"):
            _, port, host = endpoint.split(":")
            endpoint = f"ptcp:{host}:{port}"
        self.endpoint, self.database = endpoint, database
        self.read_only = read_only
        self.monitor_columns = monitor_columns if monitor_columns is not None else TABLES
        self.selected_tables = {}
        self.peer_certificate_sha256 = peer_certificate_sha256
        self.tls_locked = False
        if tls_endpoint:
            if not TLS_PROFILE_LOCK.acquire(blocking=False):
                raise EmosaError(Reason.BUSY, "upstream TLS file configuration is process-global")
            self.tls_locked = True
            ovs.stream.Stream.ssl_set_private_key_file(tls_files["private_key"])
            ovs.stream.Stream.ssl_set_certificate_file(tls_files["certificate"])
            ovs.stream.Stream.ssl_set_ca_cert_file(tls_files["ca"])
        self.request_limit, self.message_limit, self.row_limit = (
            request_limit,
            message_limit,
            row_limit,
        )
        self.timeout = timeout
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="emosa-ovs")
        self.inflight = 0
        self.rpc = None
        self.connection = None
        self.schema = None
        self.cache = {}
        self.ready = False
        self.generation = 0
        self.revision = 0
        self.monitor_id = None
        self.max_message_seen = 0
        self.closed = False
        self.stopping = threading.Event()
        self.thread_id = None

    async def _work(self, fn, *args):
        if self.closed:
            raise EmosaError(Reason.NOT_READY, "session closed")
        if self.inflight >= self.request_limit:
            raise EmosaError(Reason.BUSY, "OVSDB request budget exhausted")
        self.inflight += 1
        future = asyncio.get_running_loop().run_in_executor(self.pool, fn, *args)
        # A cancelled caller must not release a worker slot while its request is still running.
        future.add_done_callback(lambda _: setattr(self, "inflight", self.inflight - 1))
        return await asyncio.shield(future)

    def _pump(self):
        if self.rpc is None:
            self.thread_id = threading.get_ident()
            self.rpc = ovs.jsonrpc.Session.open(self.endpoint)
            if self.listener_context is not None:
                # pssl is deliberately not registered as a plaintext OVS method.
                # A missing/failed custom listener therefore cannot fall back to TCP.
                try:
                    self.rpc.pstream = TlsListener(
                        self.endpoint, self.listener_context, self.peer_certificate_sha256
                    )
                except BaseException:
                    self.rpc.close()
                    self.rpc = None
                    raise
                now = ovs.timeval.msec()
                self.rpc.reconnect.set_passive(True, now)
                self.rpc.reconnect.set_probe_interval(5000)
                self.rpc.reconnect.listening(now)
            # Independent jitter per session; upstream reconnect supplies exponential backoff.
            self.rpc.reconnect.set_backoff(
                1000 + random.randrange(250), 8000 + random.randrange(1000)
            )
        self.rpc.run()
        # Upstream ovs 4.0.0 ptcp creates a blocking listening socket. Keep the
        # documented nonblocking accept contract without replacing its transport.
        if self.rpc.pstream is not None:
            self.rpc.pstream.socket.setblocking(False)
        current = self.rpc.rpc
        if current is not self.connection:
            self.connection = current
            self.ready = False
            self.monitor_id = None
            if current is not None:
                self.generation += 1
                if self.peer_certificate_sha256:
                    certificate = current.stream.socket.getpeercert(binary_form=True)
                    if hashlib.sha256(certificate).hexdigest() != self.peer_certificate_sha256:
                        self._invalidate()
                        raise EmosaError(
                            Reason.PRECONDITION_FAILED, "TLS peer certificate pin mismatch"
                        )
                current.stream = BoundedStream(current.stream, self.message_limit)
        message = self.rpc.recv()
        if current is not None and current.get_status():
            self.ready = False
            self.rpc.run()
        if message is not None:
            size = len(json.dumps(message.to_json()).encode())
            self.max_message_seen = max(self.max_message_seen, size)
            if current is not None:
                current.stream.admitted = len(current.input.encode())
            if size > self.message_limit:
                self._invalidate()
                raise EmosaError(Reason.NOT_READY, "OVSDB decoded message budget exhausted")
            if message.type == ovs.jsonrpc.Message.T_NOTIFY:
                if message.method == "update" and message.params[0] == self.monitor_id:
                    self._update(message.params[1])
                return None
        return message

    def _invalidate(self):
        self.ready = False
        self.monitor_id = None
        if self.rpc:
            self.rpc.force_reconnect()

    def _connected(self):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.stopping.is_set():
                raise EmosaError(Reason.NOT_READY, "session shutting down")
            self._pump()
            if self.rpc.is_connected():
                return
            time.sleep(0.002)
        raise EmosaError(Reason.NOT_READY, "OVSDB endpoint did not connect")

    def _request(self, method, params, transaction_id=None, discard_reply=False):
        request = ovs.jsonrpc.Message.create_request(method, params)
        if transaction_id is not None:
            request.id = transaction_id
        generation = self.generation
        if self.rpc.send(request):
            raise ConnectionError("OVSDB send failed")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.stopping.is_set():
                raise ConnectionError("session shutting down; transaction outcome unknown")
            message = self._pump()
            if self.generation != generation or not self.rpc.is_connected():
                self.ready = False
                raise ConnectionError("OVSDB session changed while awaiting reply")
            if message is not None and message.id == request.id:
                if discard_reply:
                    # Fault at adapter ingress: real response is discarded before interpretation.
                    self._invalidate()
                    raise ConnectionError("injected loss of transaction reply")
                if message.type == ovs.jsonrpc.Message.T_ERROR:
                    raise EmosaError(Reason.PRECONDITION_FAILED, "OVSDB request rejected")
                return message.result
            time.sleep(0.001)
        self._invalidate()
        raise TimeoutError("OVSDB reply deadline elapsed")

    def _update(self, updates, initial=False):
        candidate = {} if initial else copy.deepcopy(self.cache)
        try:
            for table, rows in updates.items():
                if table not in self.selected_tables:
                    raise ValueError("unexpected monitor table")
                target = candidate.setdefault(table, {})
                for row_id, change in rows.items():
                    if "new" not in change:
                        target.pop(row_id, None)
                    else:
                        row = target.setdefault(row_id, {})
                        row.update(change["new"])
                        self.schema.row(
                            table, row
                        )  # Validate complete candidate before publishing.
            if sum(len(rows) for rows in candidate.values()) > self.row_limit:
                raise ValueError("monitor cache budget exceeded")
        except Exception as exc:
            self._invalidate()
            raise EmosaError(
                Reason.NOT_READY, "monitor continuity lost; resynchronization required"
            ) from exc
        self.cache = candidate
        self.revision += 1

    def _sync(self):
        self._connected()
        if not self.ready:
            self.schema = Schema(self._request("get_schema", [self.database]))
            if not self.read_only:
                self.schema.qualify_synthetic()
            self.selected_tables = {
                table: [
                    column for column in columns if column in self.schema.db.tables[table].columns
                ]
                for table, columns in self.monitor_columns.items()
                if table in self.schema.db.tables
            }
            self.monitor_id = f"emosa-{self.generation}"
            requests = {
                table: {"columns": columns} for table, columns in self.selected_tables.items()
            }
            initial = self._request("monitor", [self.database, self.monitor_id, requests])
            self._update(initial, initial=True)
            self.ready = True
        # Drain currently queued notifications; never discard an update on backpressure.
        for _ in range(128):
            revision = self.revision
            self._pump()
            if revision == self.revision:
                break
        if not self.ready:
            raise EmosaError(Reason.NOT_READY, "connection lost while draining monitor")

    def _snapshot(self):
        try:
            self._sync()
        except (ConnectionError, TimeoutError, EmosaError):
            self.ready = False
            raise
        return {
            "tables": copy.deepcopy(self.cache),
            "generation": self.generation,
            "revision": self.revision,
            "ready": self.ready,
            "schema": self.schema,
            "max_message_seen": self.max_message_seen,
        }

    async def snapshot(self):
        return await self._work(self._snapshot)

    def _transact(self, operations, transaction_id, generation, discard_reply):
        self._sync()
        if generation is not None and self.generation != generation:
            raise EmosaError(Reason.NOT_READY, "generation changed before submission")
        return self._request(
            "transact", [self.database, *operations], transaction_id, discard_reply
        )

    async def transact(
        self, operations, transaction_id=None, generation=None, *, discard_reply=False
    ):
        if self.read_only and any(operation.get("op") != "select" for operation in operations):
            raise EmosaError(
                Reason.UNSUPPORTED_OPERATION, "read-only session refuses modifying operations"
            )
        return await self._work(
            self._transact, operations, transaction_id, generation, discard_reply
        )

    async def reconnect(self):
        await self._work(self._invalidate)

    async def close(self):
        if not self.closed:
            self.stopping.set()
            self.closed = True
            await asyncio.get_running_loop().run_in_executor(
                self.pool, lambda: self.rpc.close() if self.rpc else None
            )
            self.pool.shutdown(wait=False, cancel_futures=True)
            if self.tls_locked:
                self.tls_locked = False
                TLS_PROFILE_LOCK.release()
