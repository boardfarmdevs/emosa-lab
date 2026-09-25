"""Read-only adapter topology demo; owned database fixtures supply simulated facts.

Two pod-initiated sessions, two radios/four interfaces per pod. No host networking,
radio, EasyMesh packets or physical endpoints. The fixture writes its own data;
the adapter is configured read-only throughout this demonstration.
"""

import argparse
import asyncio
import copy
import json
import sys
from pathlib import Path

from emosa.config import load
from emosa.opensync.mapping import check_results
from emosa.opensync.session import OvsSession
from emosa_lab.evaluation.evidence import write_json
from emosa_lab.evaluation.service import AdapterProcess
from emosa_lab.simulation.connecting_pod import configuration
from emosa_lab.simulation.database import SimDatabase, SimManager


def agent_binding(index):
    """Synthetic addresses are supplied by the fixture, never discovered hardware."""

    def mac(kind, number):
        return f"02:00:00:{index:02x}:{kind:02x}:{number:02x}"

    def interface(identifier, name, mode, number):
        return {
            "interface_id": identifier,
            "if_name": name,
            "mode": mode,
            "expected_mac": mac(0x10, number),
        }

    return {
        "al_mac": mac(0x30, 1),
        "expected_serial": f"EMOSA-TOPOLOGY-SIM-{index:03d}",
        "topology": {
            "radios": [
                {
                    "radio_id": "radio-1",
                    "if_name": "lab-radio",
                    "ruid": mac(0x40, 1),
                    "expected_mac": mac(0x20, 1),
                    "interfaces": [
                        interface("bss-1", "lab-ap", "ap", 1),
                        interface("bss-guest", "guest-ap", "ap", 2),
                    ],
                },
                {
                    "radio_id": "radio-2",
                    "if_name": "lab-radio-2",
                    "ruid": mac(0x40, 2),
                    "expected_mac": mac(0x20, 2),
                    "interfaces": [
                        interface("bss-iot", "iot-ap", "ap", 3),
                        interface("backhaul-station", "lab-sta", "sta", 4),
                    ],
                },
            ]
        },
    }


async def seed(database, index):
    """Seed complete explicit simulation facts, separate from adapter predicates."""
    binding = agent_binding(index)
    await database.seed(serial_number=binding["expected_serial"])
    session = OvsSession(database.endpoint)
    try:
        tables = (await session.snapshot())["tables"]
        first, second = binding["topology"]["radios"]
        rc_id = next(iter(tables["Wifi_Radio_Config"]))
        rs_id = next(iter(tables["Wifi_Radio_State"]))
        vc_id, vc = next(iter(tables["Wifi_VIF_Config"].items()))
        vs_id = next(iter(tables["Wifi_VIF_State"]))
        ops = []
        for name, interface in (
            ("guest", first["interfaces"][1]),
            ("iot", second["interfaces"][0]),
            ("sta", second["interfaces"][1]),
        ):
            config = {
                **vc,
                "if_name": interface["if_name"],
                "mode": interface["mode"],
                "ssid": f"simulation-{name}",
            }
            state = {
                k: v for k, v in config.items() if k not in {"bridge", "multi_ap", "wpa_oftags"}
            }
            state.update(vif_config=["named-uuid", name], mac=interface["expected_mac"])
            ops.extend(
                [
                    {"op": "insert", "table": "Wifi_VIF_Config", "uuid-name": name, "row": config},
                    {
                        "op": "insert",
                        "table": "Wifi_VIF_State",
                        "uuid-name": name + "state",
                        "row": state,
                    },
                ]
            )
        ops.extend(
            [
                {
                    "op": "update",
                    "table": "Wifi_VIF_State",
                    "where": [],
                    "row": {"mac": first["interfaces"][0]["expected_mac"]},
                },
            ]
        )
        # Target the original row explicitly: an unscoped update would overwrite
        # the newly inserted fixture identities in this same transaction.
        ops[-1]["where"] = [["_uuid", "==", ["uuid", vs_id]]]
        ops.extend(
            [
                {
                    "op": "update",
                    "table": "Wifi_Radio_Config",
                    "where": [["_uuid", "==", ["uuid", rc_id]]],
                    "row": {"vif_configs": ["set", [["uuid", vc_id], ["named-uuid", "guest"]]]},
                },
                {
                    "op": "update",
                    "table": "Wifi_Radio_State",
                    "where": [["_uuid", "==", ["uuid", rs_id]]],
                    "row": {
                        "mac": first["expected_mac"],
                        "vif_states": ["set", [["uuid", vs_id], ["named-uuid", "gueststate"]]],
                    },
                },
                {
                    "op": "insert",
                    "table": "Wifi_Radio_Config",
                    "uuid-name": "secondradio",
                    "row": {
                        "if_name": second["if_name"],
                        "freq_band": "2.4G",
                        "enabled": True,
                        "vif_configs": ["set", [["named-uuid", "iot"], ["named-uuid", "sta"]]],
                    },
                },
                {
                    "op": "insert",
                    "table": "Wifi_Radio_State",
                    "row": {
                        "if_name": second["if_name"],
                        "radio_config": ["named-uuid", "secondradio"],
                        "freq_band": "2.4G",
                        "channel": 6,
                        "enabled": True,
                        "mac": second["expected_mac"],
                        "vif_states": [
                            "set",
                            [["named-uuid", "iotstate"], ["named-uuid", "stastate"]],
                        ],
                    },
                },
            ]
        )
        check_results(
            await session.transact(ops), [1 if o["op"] == "update" else None for o in ops]
        )
    finally:
        await session.close()


async def recreate_rows(session):
    """Owned fixture fault: replace all radio/VIF UUIDs while preserving facts."""
    snap = await session.snapshot()
    tables = {
        t: snap["tables"][t]
        for t in ("Wifi_Radio_Config", "Wifi_Radio_State", "Wifi_VIF_Config", "Wifi_VIF_State")
    }
    names = {
        u: f"replacement{i}" for i, u in enumerate(u for rows in tables.values() for u in rows)
    }

    def translate(value):
        if isinstance(value, list):
            if len(value) == 2 and value[0] == "uuid" and value[1] in names:
                return ["named-uuid", names[value[1]]]
            return [translate(v) for v in value]
        return value

    ops = [{"op": "delete", "table": t, "where": []} for t in tables]
    for table, rows in tables.items():
        for uid, row in rows.items():
            ops.append(
                {
                    "op": "insert",
                    "table": table,
                    "uuid-name": names[uid],
                    "row": {
                        k: translate(v) for k, v in row.items() if k not in {"_uuid", "_version"}
                    },
                }
            )
    check_results(
        await session.transact(ops), [len(rows) for rows in tables.values()] + [None] * len(names)
    )
    after = await session.snapshot()
    current = {u for t in tables for u in after["tables"][t]}
    assert len(current) == len(names) and current.isdisjoint(names)
    return {"rows_recreated": len(current), "all_uuids_changed": True}


async def run(directory):
    directory = directory.resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    databases = [SimDatabase(), SimDatabase()]
    admins = [OvsSession(db.endpoint) for db in databases]
    managers = [SimManager(db.endpoint) for db in databases]
    listeners = ["unix:" + str(db.directory / "emosa.sock") for db in databases]
    config = configuration(directory, listeners[0])
    config["write_mode"] = "read-only"
    config["pods"][0]["virtual_agent"] = agent_binding(1)
    other = copy.deepcopy(config["pods"][0])
    other.update(pod_id="pod-2", endpoint="p" + listeners[1], virtual_agent=agent_binding(2))
    config["pods"].append(other)
    config_path = directory / "adapter.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    load("config", config_path)
    service = AdapterProcess(config_path, config["socket_path"], directory / "adapter.log")
    report = {
        "schema_version": 1,
        "scope": "complete synthetic topology via read-only adapter API",
        "passed": False,
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
        "radio_behavior_proven": False,
        "stages": {},
    }
    stages = report["stages"]

    async def view(index=0, predicate=lambda r: r["ready"]):
        return await service.wait_for("topology", predicate, {"pod_id": f"pod-{index + 1}"})

    async def cli(expected_exit):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "emosa_lab.cli",
            "--socket",
            config["socket_path"],
            "pod",
            "pod-1",
            "topology",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 15)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode == expected_exit
        return {"exit_code": process.returncode, "result": json.loads(stdout)}

    try:
        for index, db in enumerate(databases, 1):
            await db.start()
            await seed(db, index)
            await managers[index - 1].start()
        await service.start()
        for db, listener in zip(databases, listeners, strict=True):
            await db.manager_remote(listener)
        stages["connected"] = [await view(0), await view(1)]
        stages["cli_ready"] = await cli(0)
        before = stages["connected"][0]
        assert len(before["radios"]) == 2
        assert sum(len(r["interfaces"]) for r in before["radios"]) == 4
        result = await admins[0].transact(
            [
                {
                    "op": "update",
                    "table": "Wifi_VIF_Config",
                    "where": [["if_name", "==", "lab-ap"]],
                    "row": {"ssid": "topology-observed-after-manager"},
                }
            ]
        )
        check_results(result, [1])
        stages["config_only"] = await view()
        assert stages["config_only"]["operational_bss_value"] == before["operational_bss_value"]
        await managers[0].command("apply")
        stages["manager_applied"] = await view(
            predicate=lambda r: (
                r["ready"] and r["operational_bss_value"] != before["operational_bss_value"]
            )
        )
        stages["other_pod_unchanged"] = await view(1)
        assert (
            stages["other_pod_unchanged"]["operational_bss_value"]
            == stages["connected"][1]["operational_bss_value"]
        )
        extra = await admins[0].transact(
            [{"op": "insert", "table": "Wifi_VIF_Config", "row": {"if_name": "unexpected-ap"}}]
        )
        check_results(extra, [None])
        stages["unexpected_interface"] = await view(predicate=lambda r: not r["ready"])
        assert stages["unexpected_interface"]["operational_bss_value"] is None
        stages["cli_blocked"] = await cli(5)
        check_results(
            await admins[0].transact(
                [
                    {
                        "op": "delete",
                        "table": "Wifi_VIF_Config",
                        "where": [["if_name", "==", "unexpected-ap"]],
                    }
                ]
            ),
            [1],
        )
        await view()
        await databases[0].manager_remote(listeners[0], connect=False)
        stages["disconnected"] = await view(predicate=lambda r: not r["ready"])
        assert stages["disconnected"]["operational_bss_value"] is None
        stages["other_pod_while_disconnected"] = await view(1)
        stages["uuid_recreation"] = await recreate_rows(admins[0])
        await databases[0].manager_remote(listeners[0])
        stages["reconnected"] = await view()
        assert stages["reconnected"]["generation"] > before["generation"]
        await service.stop(crash=True)
        await service.start()
        stages["restarted"] = [await view(0), await view(1)]
        for result in [stages["reconnected"], stages["restarted"][0]]:
            assert result["binding_sha256"] == before["binding_sha256"]
            assert result["radios"] == stages["manager_applied"]["radios"]
            assert (
                result["operational_bss_value"]
                == stages["manager_applied"]["operational_bss_value"]
            )
        assert stages["restarted"][0]["adapter_instance_id"] != before["adapter_instance_id"]
        assert (
            stages["restarted"][1]["operational_bss_value"]
            == stages["connected"][1]["operational_bss_value"]
        )
        report["passed"] = True
        return report
    finally:
        await service.stop()
        for manager in managers:
            await manager.close()
        for admin in admins:
            await admin.close()
        for database in databases:
            await database.close()
        report["service_lifecycle"] = service.lifecycle
        write_json(directory / "report.json", report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new private result directory")
    args = parser.parse_args()
    result = asyncio.run(run(args.output))
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "report": str(args.output / "report.json"),
                "controller_onboarding_proven": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
