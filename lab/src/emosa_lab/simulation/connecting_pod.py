"""Pod-initiated OVSDB and a diagnostic virtual agent; no EasyMesh wire exchange."""

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import signal
import sys
from pathlib import Path

from emosa.config import validate
from emosa.errors import EmosaError
from emosa.secrets import SecretStore
from emosa_lab.local_api import request
from emosa_lab.simulation.database import SimDatabase, SimManager

SERIAL = "EMOSA-SIM-EXTENDER-001"
AL_MAC = "02:00:00:00:30:01"


def configuration(directory, listener):
    return {
        "schema_version": 1,
        "backend_mode": "ovsdb-sim",
        "state_directory": str(directory / "state"),
        "secret_directory": str(directory / "secrets"),
        "socket_path": str(directory / "control.sock"),
        "write_mode": "managed-fields",
        "request_source": "connecting-pod-demo",
        "pods": [
            {
                "pod_id": "pod-1",
                "endpoint": "p" + listener,
                "database": "Open_vSwitch",
                "if_name": "lab-ap",
                "radio_name": "lab-radio",
                "radio_id": "radio-1",
                "bss_id": "bss-1",
                "virtual_agent": {"al_mac": AL_MAC, "expected_serial": SERIAL},
            }
        ],
    }


async def poll(call, predicate, timeout=15):
    end = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            result = await call()
            if predicate(result):
                return result
        except EmosaError as exc:
            if not exc.details.get("service_unavailable"):
                raise
        if asyncio.get_running_loop().time() >= end:
            raise TimeoutError("connecting-pod demonstration did not converge")
        await asyncio.sleep(0.05)


async def verify(directory, database, manager, config, intent, listener):
    """Exercise the real service process exclusively through its local northbound API."""
    socket_path = config["socket_path"]
    report = {
        "schema_version": 1,
        "scope": "pod-initiated OVSDB to local diagnostic virtual agent",
        "transport": "private Unix stream; database initiates, EMOSA manages",
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
        "radio_behavior_proven": False,
        "stages": {},
        "passed": False,
    }
    process = None

    async def agents():
        return await request(socket_path, "agents", {})

    async def ready():
        return await poll(agents, lambda r: r["agents"][0]["state"] == "ready")

    async def start_adapter():
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "emosa_lab.cli",
            "serve",
            "--config",
            str(directory / "adapter.json"),
            stdout=log,
            stderr=log,
        )

    async def stop_adapter():
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                process.kill()
                await process.wait()

    with (directory / "adapter.log").open("ab") as log:
        try:
            process = await start_adapter()
            report["stages"]["before_connection"] = await poll(
                agents, lambda r: r["agents"][0]["state"] == "pending"
            )
            await database.manager_remote(listener)
            first = report["stages"]["connected"] = await ready()
            agent = first["agents"][0]
            if agent["inventory"]["device_identity"][0]["serial_number"] != SERIAL:
                raise AssertionError("northbound identity did not come from the simulated pod")
            operation = await request(
                socket_path,
                "component.submit",
                {"intent": intent, "idempotency_key": "demo-1", "run_id": "connecting-pod"},
            )

            async def apply_and_observe():
                await manager.command("apply")
                return await request(
                    socket_path, "operation.show", {"operation_id": operation["operation_id"]}
                )

            report["stages"]["configuration_applied"] = await poll(
                apply_and_observe, lambda r: r["state"] == "OBSERVED_APPLIED"
            )
            report["stages"]["updated_inventory"] = await poll(
                agents,
                lambda r: (
                    r["agents"][0]["fresh"]
                    and r["agents"][0]["inventory"]["bsses"][0]["ssid"] == intent["ssid"]
                ),
            )
            await database.manager_remote(listener, connect=False)
            report["stages"]["disconnected"] = await poll(
                agents, lambda r: r["agents"][0]["state"] == "unavailable"
            )
            await database.manager_remote(listener)
            recovered = report["stages"]["reconnected"] = await ready()
            if (
                recovered["agents"][0]["inventory"]["generation"]
                <= agent["inventory"]["generation"]
            ):
                raise AssertionError("reconnection did not establish a new session generation")
            await stop_adapter()
            process = await start_adapter()
            restarted = report["stages"]["adapter_restarted"] = await ready()
            if restarted["adapter_instance_id"] == first["adapter_instance_id"]:
                raise AssertionError("adapter restart not observed")
            if any(
                len(view["agents"]) != 1 or view["agents"][0]["al_mac"] != AL_MAC
                for view in (first, recovered, restarted)
            ):
                raise AssertionError("virtual identity changed or duplicated")
            report["passed"] = True
        finally:
            await stop_adapter()
            (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


async def run(directory, *, automated=False):
    directory = directory.resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    vault = SecretStore(directory / "secrets")
    vault.write_simulated("demo-key", secrets.token_urlsafe(24))
    database = SimDatabase()
    manager = SimManager(database.endpoint)
    listener = "unix:" + str(database.directory / "emosa.sock")
    config = configuration(directory, listener)
    validate("config", config)
    intent = {
        "pod_id": "pod-1",
        "radio_id": "radio-1",
        "bss_id": "bss-1",
        "ssid": "emosa-virtual-agent-demo",
        "secret_ref": "demo-key",
    }
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        await database.start()
        await database.seed(serial_number=SERIAL)
        await manager.start()
        for name, value in (("adapter.json", config), ("intent.json", intent)):
            (directory / name).write_text(json.dumps(value, indent=2) + "\n")
        print(f"Fixture ready: {directory / 'adapter.json'}", flush=True)
        print("Local diagnostic agent only; EasyMesh controller onboarding pending.", flush=True)
        if automated:
            report = await verify(directory, database, manager, config, intent, listener)
            print(
                f"Verified: {report['passed']}. Evidence: {directory / 'report.json'}", flush=True
            )
            return report
        connected = False
        while not stop.is_set():
            desired = not (directory / "disconnect").exists()
            if desired != connected:
                await database.manager_remote(listener, connect=desired)
                connected = desired
            if not (directory / "withhold").exists():
                await manager.command("apply")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 0.25)
    finally:
        try:
            await manager.close()
        finally:
            await database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True, help="new private run directory")
    parser.add_argument("--verify", action="store_true", help="run bounded automated demonstration")
    args = parser.parse_args()
    os.umask(0o077)
    asyncio.run(run(args.directory, automated=args.verify))


if __name__ == "__main__":
    main()
