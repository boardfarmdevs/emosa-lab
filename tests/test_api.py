import asyncio
import json
import time

import pytest

from emosa.config import validate
from emosa.errors import EmosaError
from emosa.secrets import SecretStore
from emosa_lab.app import Application
from emosa_lab.local_api import LocalServer, request

pytestmark = pytest.mark.unit


def test_service_local_api_and_read_only_mode(tmp_path):
    async def scenario():
        secrets = SecretStore(tmp_path / "secrets")
        secrets.write_simulated("test-key", "only-a-test-secret")
        config = {
            "schema_version": 1,
            "backend_mode": "model",
            "state_directory": str(tmp_path / "state"),
            "secret_directory": str(tmp_path / "secrets"),
            "socket_path": str(tmp_path / "api.sock"),
            "write_mode": "read-only",
            "request_source": "local-test",
            "pods": [
                {
                    "pod_id": "pod-1",
                    "endpoint": "unix:/simulation-only",
                    "database": "Open_vSwitch",
                    "if_name": "lab-ap",
                    "radio_name": "lab-radio",
                    "radio_id": "radio-1",
                    "bss_id": "bss-1",
                }
            ],
        }
        validate("config", config)
        app = Application(config)
        try:
            await app.start()
            start = time.monotonic()
            status = await request(config["socket_path"], "status", {})
            assert time.monotonic() - start < 0.25
            assert status["protocol"]["state"] == "blocked"
            assert status["write_mode"] == "read-only"
            caps = await request(config["socket_path"], "capabilities", {"pod_id": "pod-1"})
            assert caps["bss-set"]["status"] == "temporarily_unavailable"
            assert (tmp_path / "api.sock").stat().st_mode & 0o777 == 0o600
            intent = {
                "pod_id": "pod-1",
                "radio_id": "radio-1",
                "bss_id": "bss-1",
                "ssid": "new-ssid",
                "secret_ref": "test-key",
            }
            plan = await request(config["socket_path"], "plan", {"intent": intent})
            assert "only-a-test-secret" not in json.dumps(plan)
            with pytest.raises(EmosaError, match="read-only"):
                await request(
                    config["socket_path"],
                    "component.submit",
                    {"intent": intent, "idempotency_key": "key", "run_id": "run"},
                )
            app.config["write_mode"] = "managed-fields"
            op = await request(
                config["socket_path"],
                "component.submit",
                {"intent": intent, "idempotency_key": "key", "run_id": "run"},
            )
            with pytest.raises(EmosaError) as exc:
                await request(
                    config["socket_path"],
                    "operation.wait",
                    {"operation_id": op["operation_id"], "timeout": 0},
                )
            assert exc.value.details["wait_timeout"]
            await request(config["socket_path"], "quiesce", {})
            assert (await request(config["socket_path"], "status", {}))["quiesced"]
        finally:
            await app.close()
        assert not (tmp_path / "api.sock").exists()

    asyncio.run(scenario())


def test_api_malformed_and_oversized_frame_no_side_effect(tmp_path):
    async def scenario():
        called = []

        async def handler(method, params):
            called.append(method)
            return {}

        path = tmp_path / "api.sock"
        server = await LocalServer(path, handler, frame_limit=1024).start()
        try:
            for body in [b"not-json\n", b"x" * 2048 + b"\n", b'{"schema_version":2}\n']:
                reader, writer = await asyncio.open_unix_connection(path)
                writer.write(body)
                await writer.drain()
                reply = json.loads(await reader.readline())
                assert reply["error"]["code"] == "INVALID_INPUT"
                writer.close()
                await writer.wait_closed()
            assert not called
        finally:
            await server.close()

    asyncio.run(scenario())
