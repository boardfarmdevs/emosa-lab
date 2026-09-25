"""Disposable OVSDB fixture for doc/guides/team-manual.md; no radio or physical endpoint.

Run from the checkout with uv run. Leave this process running while starting
emosa serve in another terminal. Creating DIRECTORY/withhold pauses simulated
application; removing it resumes application. Ctrl-C stops only this fixture.
The directory must be new; configuration, journal and private synthetic secrets
are retained. This is teaching scaffolding, not an additional adapter backend.
"""

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import signal
from pathlib import Path

from emosa.config import validate
from emosa.secrets import SecretStore
from emosa_lab.simulation.database import SimDatabase, SimManager


async def run(directory):
    directory = directory.resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    vault = SecretStore(directory / "secrets")
    vault.write_simulated("manual-key", "manual-" + secrets.token_hex(12))
    database = SimDatabase()  # Short private /tmp socket path, even in a long checkout path.
    manager = SimManager(database.endpoint)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await database.start()
        await database.seed()
        await manager.start()
        config = {
            "schema_version": 1,
            "backend_mode": "ovsdb-sim",
            "state_directory": str(directory / "state"),
            "secret_directory": str(directory / "secrets"),
            "socket_path": str(directory / "control.sock"),
            "write_mode": "managed-fields",
            "request_source": "team-manual",
            "pods": [
                {
                    "pod_id": "pod-1",
                    "endpoint": database.endpoint,
                    "database": "Open_vSwitch",
                    "if_name": "lab-ap",
                    "radio_name": "lab-radio",
                    "radio_id": "radio-1",
                    "bss_id": "bss-1",
                }
            ],
        }
        validate("config", config)
        intent = {
            "pod_id": "pod-1",
            "radio_id": "radio-1",
            "bss_id": "bss-1",
            "ssid": "emosa-team-demo",
            "secret_ref": "manual-key",
            "enabled": True,
            "security_mode": "wpa2-psk",
        }
        for name, value in (("adapter.json", config), ("intent.json", intent)):
            (directory / name).write_text(json.dumps(value, indent=2) + "\n")
        print(f"Fixture ready. Adapter config: {directory / 'adapter.json'}", flush=True)
        print("Synthetic State only; no EasyMesh packets, radio or pod.", flush=True)
        while not stop.is_set():
            await manager.command("withhold" if (directory / "withhold").exists() else "apply")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=0.25)
    finally:
        try:
            await manager.close()
        finally:
            await database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(".lab/manual-service"))
    args = parser.parse_args()
    os.umask(0o077)
    asyncio.run(run(args.directory))


if __name__ == "__main__":
    main()
