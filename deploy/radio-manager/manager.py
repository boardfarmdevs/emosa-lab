"""Autonomous radio manager in the dedicated VM; no operation-engine imports."""

import argparse
import asyncio
import fcntl
import json
import runpy
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
    def __init__(self, neighbor_label=None, path_observer=None):
        self.neighbor_label = neighbor_label
        self.path_observer = path_observer

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
            *(
                ["--neighbor-label", self.neighbor_label]
                if action == "observe" and self.neighbor_label
                else []
            ),
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
        before = await asyncio.to_thread(self.path_observer.observe) if self.path_observer else None
        value = await self.request("observe")
        if self.path_observer:
            after = await asyncio.to_thread(self.path_observer.observe)
            value["forwarding"]["peer_path_observation"] = {"before": before, "after": after}
        return value


async def serve(directory):
    guard()
    endpoint = "unix:" + str(directory / "database/db.sock")
    session = OvsSession(endpoint, monitor_columns=MONITOR)
    native = (directory / "native-owner.json").exists()
    path_observer = (
        runpy.run_path(str(ROOT / "peer-path.py"))["PathObserver"](directory)
        if (native and (directory / "peer-metrics-requested").exists())
        else None
    )
    manager = RadioManager(session, Driver(directory.name if native else None, path_observer))
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
                        event["telemetry_withheld"] = policy.get("telemetry_withheld", False)
                        if not event["telemetry_withheld"]:
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
            if path_observer:
                path_observer.close()
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
