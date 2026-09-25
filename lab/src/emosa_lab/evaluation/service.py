"""Control an actual adapter child process through its public local API."""

import asyncio
import sys
from pathlib import Path

from emosa.errors import EmosaError
from emosa_lab.local_api import request


class AdapterProcess:
    def __init__(self, config_path, socket_path, log_path):
        self.config_path = Path(config_path)
        self.socket_path = str(socket_path)
        self.log_path = Path(log_path)
        self.process = None
        self.log = None
        self.lifecycle = []

    async def call(self, method, params=None):
        return await request(self.socket_path, method, params or {})

    async def wait_for(self, method, predicate, params=None, *, timeout=30):
        end = asyncio.get_running_loop().time() + timeout
        while True:
            if self.process is None or self.process.returncode is not None:
                raise RuntimeError("Adapter process exited; inspect its private log")
            try:
                value = await self.call(method, params)
                if predicate(value):
                    return value
            except EmosaError as exc:
                if not exc.details.get("service_unavailable"):
                    raise
            if asyncio.get_running_loop().time() >= end:
                raise TimeoutError(f"Adapter {method} observation did not converge")
            await asyncio.sleep(0.05)

    async def start(self):
        if self.process is not None and self.process.returncode is None:
            raise RuntimeError("Adapter is already running")
        self.log = self.log_path.open("ab")
        try:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "emosa_lab.cli",
                "serve",
                "--config",
                str(self.config_path),
                stdout=self.log,
                stderr=self.log,
            )
            view = await self.wait_for("agents", lambda _: True)
            self.lifecycle.append(
                {
                    "event": "started",
                    "pid": self.process.pid,
                    "adapter_instance_id": view["adapter_instance_id"],
                }
            )
            return view
        except BaseException:
            await self.stop()
            raise

    async def stop(self, *, crash=False):
        if self.process is not None:
            escalation = False
            if self.process.returncode is None:
                self.process.kill() if crash else self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 10)
                except TimeoutError:
                    escalation = True
                    self.process.kill()
                    await self.process.wait()
            self.lifecycle.append(
                {
                    "event": "stopped",
                    "pid": self.process.pid,
                    "requested_crash": crash,
                    "kill_escalation": escalation,
                    "returncode": self.process.returncode,
                }
            )
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None
