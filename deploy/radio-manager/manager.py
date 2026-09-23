"""Autonomous radio manager in the dedicated VM; no operation-engine imports."""

import argparse
import asyncio
import fcntl
import json
import signal
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from common import AP, ROOT, guard

from emosa.opensync.session import OvsSession
from emosa.simulation.radio import MONITOR, RadioManager
from emosa.simulation.station_telemetry import LabMqtt


class Driver:
    async def request(self, action, payload=None):
        process = await asyncio.create_subprocess_exec(
            "lxc",
            "--force-local",
            "--project",
            "default",
            "exec",
            AP,
            "--",
            "python3",
            str(ROOT / "node.py"),
            action,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, _ = await asyncio.wait_for(
                process.communicate(json.dumps(payload).encode() if payload else None), 25
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise RuntimeError("hostapd/nl80211 helper unavailable")
        return json.loads(out)

    async def apply(self, config):
        await self.request("apply", asdict(config))

    async def observe(self):
        return await self.request("observe")


async def serve(directory):
    guard()
    endpoint = "unix:" + str(directory / "database/db.sock")
    session = OvsSession(endpoint, monitor_columns=MONITOR)
    manager = RadioManager(session, Driver())
    mqtt = LabMqtt(directory) if (directory / "native-owner.json").exists() else None
    stopped = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
    with (ROOT / "manager.lock").open("w") as lock, (directory / "manager.jsonl").open("a") as log:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            while not stopped.is_set():
                try:
                    policy = json.loads((directory / "policy.json").read_text())
                    event = await manager.cycle(withhold=policy["withhold"])
                    if (
                        mqtt
                        and event.get("publication") == "observed-state"
                        and "stations" in event
                    ):
                        event["telemetry_published"] = mqtt.publish(
                            event["stations"], event["observation"]["ssid"]
                        )
                except Exception as error:
                    event = {"error": type(error).__name__}
                event["monotonic"] = time.monotonic()
                log.write(json.dumps(event) + "\n")
                log.flush()
                with suppress(TimeoutError):
                    await asyncio.wait_for(stopped.wait(), 0.5)
        finally:
            if mqtt:
                mqtt.close()
            await session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if directory.parent != ROOT / "runs" or not directory.is_dir():
        raise SystemExit("Expected an existing owned run directory")
    asyncio.run(serve(directory))
