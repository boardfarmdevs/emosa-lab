import asyncio
import json
import os
import socket
import stat
from pathlib import Path

from emosa.config import validate
from emosa.errors import EmosaError, Reason
from emosa.secrets import redact


class LocalServer:
    def __init__(self, path, handler, *, frame_limit=65536, client_limit=16):
        self.path = Path(path)
        self.handler = handler
        self.frame_limit = frame_limit
        self.client_limit = client_limit
        self.clients = 0
        self.server = None
        self.connections = set()

    async def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists():
            if not stat.S_ISSOCK(self.path.lstat().st_mode):
                raise EmosaError(Reason.INVALID_INPUT, "local API path is not a socket")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.2)
                probe.connect(str(self.path))
            except ConnectionRefusedError:
                self.path.unlink()
            else:
                raise EmosaError(Reason.BUSY, "local API socket already active")
            finally:
                probe.close()
        self.server = await asyncio.start_unix_server(
            self._client, str(self.path), limit=self.frame_limit
        )
        os.chmod(self.path, 0o600)
        return self

    async def _client(self, reader, writer):
        request_id = None
        self.connections.add(writer)
        self.clients += 1
        try:
            if self.clients > self.client_limit:
                raise EmosaError(Reason.BUSY, "local API client budget exhausted")
            try:
                line = await asyncio.wait_for(reader.readline(), 5)
                if len(line) > self.frame_limit or not line.endswith(b"\n"):
                    raise ValueError("invalid frame")
                request = json.loads(line)
            except (ValueError, TimeoutError) as exc:
                raise EmosaError(
                    Reason.INVALID_INPUT, "invalid or oversized local API frame"
                ) from exc
            if isinstance(request, dict) and isinstance(request.get("request_id"), str):
                request_id = request["request_id"][:128]
            validate("local-api", request)
            if "method" not in request:
                raise EmosaError(Reason.INVALID_INPUT, "request envelope required")
            result = await self.handler(request["method"], request["params"])
            response = {"schema_version": 1, "request_id": request_id, "result": redact(result)}
        except EmosaError as exc:
            response = {"schema_version": 1, "request_id": request_id, "error": exc.public()}
        except Exception:
            response = {
                "schema_version": 1,
                "request_id": request_id,
                "error": {
                    "code": "NOT_READY",
                    "message": "local service could not complete request",
                    "details": {},
                },
            }
        try:
            validate("local-api", response)
            encoded = (json.dumps(response) + "\n").encode()
            if len(encoded) > self.frame_limit:
                encoded = (
                    json.dumps(
                        {
                            "schema_version": 1,
                            "request_id": request_id,
                            "error": {
                                "code": "BUSY",
                                "message": "response exceeds frame budget; paginate",
                                "details": {},
                            },
                        }
                    )
                    + "\n"
                ).encode()
            writer.write(encoded)
            await asyncio.wait_for(writer.drain(), 5)
        except (ConnectionError, TimeoutError):
            pass
        finally:
            self.clients -= 1
            self.connections.discard(writer)
            writer.close()
            await writer.wait_closed()

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            for writer in tuple(self.connections):
                writer.close()
            self.path.unlink(missing_ok=True)


async def request(path, method, params, *, timeout=65):
    import uuid

    envelope = {
        "schema_version": 1,
        "request_id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }
    validate("local-api", envelope)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path), limit=1048576), 3
        )
    except (OSError, TimeoutError) as exc:
        raise EmosaError(
            Reason.NOT_READY, "local service unavailable", service_unavailable=True
        ) from exc
    try:
        writer.write((json.dumps(envelope) + "\n").encode())
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), timeout))
        validate("local-api", reply)
        if reply.get("request_id") != envelope["request_id"]:
            raise EmosaError(Reason.INVALID_INPUT, "local reply identity mismatch")
        if "error" in reply:
            error = reply["error"]
            raise EmosaError(Reason(error["code"]), error["message"], **error["details"])
        return reply["result"]
    finally:
        writer.close()
        await writer.wait_closed()
