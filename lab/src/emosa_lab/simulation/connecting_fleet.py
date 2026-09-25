"""Two explicitly bound simulated pods, one real adapter service; no EasyMesh wire."""

import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path

from emosa.config import load
from emosa.opensync.session import OvsSession
from emosa.secrets import SecretStore
from emosa_lab.evaluation.evidence import write_json
from emosa_lab.evaluation.service import AdapterProcess
from emosa_lab.simulation.connecting_pod import configuration, poll
from emosa_lab.simulation.database import SimDatabase, SimManager


async def run(directory):
    directory = directory.resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    databases = [SimDatabase(), SimDatabase()]
    managers = [SimManager(db.endpoint) for db in databases]
    admins = [OvsSession(db.endpoint) for db in databases]
    listeners = ["unix:" + str(db.directory / "emosa.sock") for db in databases]
    config = configuration(directory, listeners[0])
    config["request_source"] = "connecting-fleet-demo"
    first = config["pods"][0]
    config["pods"].append(
        {
            **first,
            "pod_id": "pod-2",
            "endpoint": "p" + listeners[1],
            "virtual_agent": {
                "al_mac": "02:00:00:00:30:02",
                "expected_serial": "EMOSA-SIM-EXTENDER-002",
            },
        }
    )
    config_path = directory / "adapter.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    load("config", config_path)
    vault = SecretStore(directory / "secrets")
    keys = [secrets.token_urlsafe(24), secrets.token_urlsafe(24)]
    for i, key in enumerate(keys, 1):
        vault.write_simulated(f"pod-{i}-key", key)
    service = AdapterProcess(config_path, config["socket_path"], directory / "adapter.log")
    report = {
        "schema_version": 1,
        "scope": "two configured pods through one adapter process",
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
        "radio_behavior_proven": False,
        "automatic_enrollment_proven": False,
        "passed": False,
        "stages": {},
    }
    stages = report["stages"]

    async def view(states):
        return await service.wait_for(
            "agents", lambda r: [a["state"] for a in r["agents"]] == states
        )

    async def submit(index, name):
        return await service.call(
            "component.submit",
            {
                "intent": {
                    "pod_id": f"pod-{index + 1}",
                    "radio_id": "radio-1",
                    "bss_id": "bss-1",
                    "ssid": f"emosa-pod-{index + 1}-{name}",
                    "secret_ref": f"pod-{index + 1}-key",
                },
                "idempotency_key": name,
                "run_id": f"fleet-pod-{index + 1}",
                "apply_seconds": 60,
            },
        )

    async def operation(op, state):
        return await service.wait_for(
            "operation.show", lambda r: r["state"] == state, {"operation_id": op["operation_id"]}
        )

    async def apply(index, op):
        async def cycle():
            await managers[index].command("apply")
            return await service.call("operation.show", {"operation_id": op["operation_id"]})

        return await poll(cycle, lambda r: r["state"] == "OBSERVED_APPLIED")

    try:
        for i, db in enumerate(databases):
            await db.start()
            await db.seed(serial_number=config["pods"][i]["virtual_agent"]["expected_serial"])
            await managers[i].start()
        await service.start()
        stages["before_connection"] = await view(["pending", "pending"])
        for db, listener in zip(databases, listeners, strict=True):
            await db.manager_remote(listener)
        stages["connected"] = await view(["ready", "ready"])
        ops = await asyncio.gather(submit(0, "first"), submit(1, "first"))
        assert ops[0]["operation_id"] != ops[1]["operation_id"]
        stages["independent_operations"] = await asyncio.gather(apply(0, ops[0]), apply(1, ops[1]))
        for i, op in enumerate(ops):
            repeated = await submit(i, "first")
            assert repeated["operation_id"] == op["operation_id"]
            assert len(repeated["attempts"]) == 1
            rows = (await admins[i].snapshot())["tables"]["Wifi_VIF_Config"]
            row = next(iter(rows.values()))
            assert row["ssid"] == f"emosa-pod-{i + 1}-first"
            assert dict(row["wpa_psks"][1])["key"] == keys[i]
        stages["separate_database_keys_verified"] = True

        # No manager cycle for pod 1: its Config commit must not block pod 2.
        waiting = await submit(0, "recovery")
        stages["withheld"] = await operation(waiting, "CONFIG_COMMITTED")
        other = await submit(1, "while-peer-withheld")
        stages["other_pod_progress"] = await apply(1, other)
        await service.stop(crash=True)
        await service.start()
        stages["restart_pending"] = await operation(waiting, "CONFIG_COMMITTED")
        stages["recovered_operation"] = await apply(0, waiting)
        assert len(stages["recovered_operation"]["attempts"]) == 1
        await databases[0].manager_remote(listeners[0], connect=False)
        stages["one_disconnected"] = await view(["unavailable", "ready"])
        other = await submit(1, "while-peer-offline")
        stages["other_pod_while_offline"] = await apply(1, other)
        await databases[0].manager_remote(listeners[0])
        stages["reconnected"] = await view(["ready", "ready"])
        assert (
            stages["reconnected"]["agents"][0]["inventory"]["generation"]
            > (stages["one_disconnected"]["agents"][0]["inventory"]["generation"])
        )
        result = await admins[0].transact(
            [
                {
                    "op": "update",
                    "table": "AWLAN_Node",
                    "where": [],
                    "row": {"serial_number": "UNEXPECTED-SYNTHETIC-POD"},
                }
            ]
        )
        assert result == [{"count": 1}]
        stages["wrong_identity"] = await view(["unavailable", "ready"])
        rejected = await submit(0, "wrong-identity")
        stages["wrong_identity_operation"] = await operation(rejected, "REJECTED")
        assert not stages["wrong_identity_operation"]["attempts"]
        assert stages["wrong_identity_operation"]["reason"] == "NOT_READY"
        rows = (await admins[0].snapshot())["tables"]["Wifi_VIF_Config"]
        assert next(iter(rows.values()))["ssid"] == "emosa-pod-1-recovery"
        assert (await view(["unavailable", "ready"]))["agents"][1]["fresh"]
        await admins[0].transact(
            [
                {
                    "op": "update",
                    "table": "AWLAN_Node",
                    "where": [],
                    "row": {"serial_number": first["virtual_agent"]["expected_serial"]},
                }
            ]
        )
        stages["restored"] = await view(["ready", "ready"])
        stages["history"] = [
            await service.call("events", {"run_id": f"fleet-pod-{i}"}) for i in (1, 2)
        ]
        operation_sets = [
            {event["operation_id"] for event in h["events"] if event["operation_id"]}
            for h in stages["history"]
        ]
        assert all(operation_sets) and operation_sets[0].isdisjoint(operation_sets[1])
        for expected, actual in zip(config["pods"], stages["restored"]["agents"], strict=True):
            assert actual["al_mac"] == expected["virtual_agent"]["al_mac"]
            assert (
                actual["inventory"]["device_identity"][0]["serial_number"]
                == (expected["virtual_agent"]["expected_serial"])
            )
        starts = [x for x in service.lifecycle if x["event"] == "started"]
        assert len({x["adapter_instance_id"] for x in starts}) == 2
        report["passed"] = True
    finally:
        await service.stop()
        report["service_lifecycle"] = service.lifecycle
        write_json(directory / "report.json", report)
        results = await asyncio.gather(
            *(manager.close() for manager in managers),
            *(admin.close() for admin in admins),
            return_exceptions=True,
        )
        results += await asyncio.gather(*(db.close() for db in databases), return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            report["passed"] = False
            report["cleanup_failed"] = True
            write_json(directory / "report.json", report)
            raise RuntimeError("Fleet fixture cleanup failed; inspect private logs")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    report = asyncio.run(run(args.directory))
    print(json.dumps({"passed": report["passed"], "report": str(args.directory / "report.json")}))


if __name__ == "__main__":
    main()
