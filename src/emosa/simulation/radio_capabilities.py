"""Two-pod, read-only capability-input demonstration on disposable OVSDB servers."""

import argparse
import asyncio
import copy
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from emosa.config import load, validate
from emosa.evaluation.evidence import write_json
from emosa.evaluation.service import AdapterProcess
from emosa.opensync.mapping import check_results
from emosa.opensync.session import OvsSession
from emosa.simulation.connecting_pod import configuration
from emosa.simulation.database import SimDatabase
from emosa.simulation.topology import agent_binding, seed
from emosa.topology_bindings import binding_digest, canonical_binding


def fixture_profile(binding, schema_fingerprint, *, now=None):
    """Explicit invented lab capacities, independent of observed BSS/channel counts.

    This helper is exclusively for our owned two-radio fixture, never hardware.
    """
    now = now or datetime.now(UTC)
    radios = []
    for radio in binding["topology"]["radios"]:
        first = radio["radio_id"] == "radio-1"
        radios.append(
            {
                "radio_id": radio["radio_id"],
                "ruid": radio["ruid"],
                "country": "US",
                "max_bss": 4 if first else 2,
                "operating_classes": [
                    {
                        "operating_class": op,
                        "max_eirp_dbm": 20 if first else 17,
                        "non_operable_channels": [12, 13] if op in (81, 84) else [],
                    }
                    for op in ([115] if first else [81, 83, 84])
                ],
                "evidence_id": "synthetic-radio-contract",
            }
        )
    evidence = {
        "source_kind": "synthetic_fixture",
        "scope": "Invented simulation capacities; no device, RF or regulatory certification",
        "device": {"model": "EMOSA synthetic extender", "firmware_version": "simulation-only"},
        "binding_sha256": binding_digest(binding),
        "radios": radios,
        "complete_operating_class_inventory": True,
    }
    evidence_bytes = (json.dumps(evidence, indent=2) + "\n").encode()
    profile = {
        "schema_version": 1,
        "source_kind": "synthetic_fixture",
        "pod_id": binding["pod_id"],
        "binding_sha256": binding_digest(binding),
        "schema_fingerprint": schema_fingerprint,
        "device": evidence["device"],
        "issued_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "complete_operating_class_inventory": True,
        "evidence": [
            {
                "id": "synthetic-radio-contract",
                "file": "synthetic-radio-contract.json",
                "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
                "review_note": "Synthetic fixture declaration only; not physical qualification",
                "covers": [
                    "device_identity",
                    "regulatory_domain",
                    "max_bss",
                    "complete_operating_classes",
                ],
            }
        ],
        "radios": radios,
    }
    validate("radio-capabilities", profile)
    return profile, evidence_bytes


def write_inputs(directory, binding, fingerprint):
    directory.mkdir(mode=0o700)
    profile, evidence = fixture_profile(binding, fingerprint)
    path = directory / "capabilities.json"
    write_json(path, profile)
    (directory / profile["evidence"][0]["file"]).write_bytes(evidence)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


async def run(directory):
    directory = directory.resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    databases = [SimDatabase(), SimDatabase()]
    admins = [OvsSession(db.endpoint) for db in databases]
    listeners = ["unix:" + str(db.directory / "emosa.sock") for db in databases]
    config = configuration(directory, listeners[0])
    config["write_mode"] = "read-only"
    config["pods"][0]["virtual_agent"] = agent_binding(1)
    other = copy.deepcopy(config["pods"][0])
    other.update(pod_id="pod-2", endpoint="p" + listeners[1], virtual_agent=agent_binding(2))
    config["pods"].append(other)
    config_path = directory / "adapter.json"
    service = AdapterProcess(config_path, config["socket_path"], directory / "adapter.log")
    report = {
        "schema_version": 1,
        "scope": "synthetic capability inputs via a read-only adapter service",
        "passed": False,
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
        "radio_behavior_proven": False,
        "stages": {},
    }
    stages = report["stages"]

    async def view(index=0, ready=True):
        return await service.wait_for(
            "radio.capabilities", lambda r: r["ready"] is ready, {"pod_id": f"pod-{index + 1}"}
        )

    async def change(table, row):
        check_results(
            await admins[0].transact([{"op": "update", "table": table, "where": [], "row": row}]),
            [1 if table == "AWLAN_Node" else 2],
        )

    async def cli(expected):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "emosa.cli",
            "--socket",
            config["socket_path"],
            "pod",
            "pod-1",
            "radio-capabilities",
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
        assert process.returncode == expected
        return {"exit_code": expected, "result": json.loads(stdout)}

    try:
        initial = []
        for index, db in enumerate(databases):
            await db.start()
            await seed(db, index + 1)
            check_results(
                await admins[index].transact(
                    [
                        {
                            "op": "update",
                            "table": "Wifi_Radio_State",
                            "where": [],
                            "row": {"country": "US"},
                        }
                    ]
                ),
                [2],
            )
            raw = await admins[index].snapshot()
            initial.append(raw["tables"])
            pod = config["pods"][index]
            pod["radio_capabilities"] = write_inputs(
                directory / pod["pod_id"], canonical_binding(pod), raw["schema"].fingerprint
            )
        write_json(config_path, config)
        load("config", config_path)
        await service.start()
        for db, listener in zip(databases, listeners, strict=True):
            await db.manager_remote(listener)
        stages["connected"] = [await view(0), await view(1)]
        stages["cli_ready"] = await cli(0)
        profile_path = Path(config["pods"][0]["radio_capabilities"]["path"])
        original = profile_path.read_bytes()
        profile_path.write_bytes(original + b" ")
        stages["profile_changed"] = await view(ready=False)
        assert stages["profile_changed"]["blockers"] == ["profile_digest_mismatch"]
        stages["cli_blocked"] = await cli(5)
        stages["other_pod_unchanged"] = await view(1)
        profile_path.write_bytes(original)
        evidence_path = profile_path.parent / "synthetic-radio-contract.json"
        evidence = evidence_path.read_bytes()
        evidence_path.write_bytes(b"modified synthetic evidence")
        stages["evidence_changed"] = await view(ready=False)
        assert stages["evidence_changed"]["blockers"] == ["evidence_digest_mismatch"]
        evidence_path.write_bytes(evidence)
        await change("Wifi_Radio_State", {"country": "CA"})
        stages["country_changed"] = await view(ready=False)
        assert stages["country_changed"]["blockers"] == ["regulatory_context_mismatch"]
        await change("Wifi_Radio_State", {"country": "US"})
        await change("AWLAN_Node", {"firmware_version": "different-synthetic-firmware"})
        stages["firmware_changed"] = await view(ready=False)
        assert stages["firmware_changed"]["blockers"] == ["capability_device_mismatch"]
        await change("AWLAN_Node", {"firmware_version": "simulation-only"})
        await databases[0].manager_remote(listeners[0], connect=False)
        stages["disconnected"] = await view(ready=False)
        stages["other_pod_while_disconnected"] = await view(1)
        await databases[0].manager_remote(listeners[0])
        stages["reconnected"] = await view()
        await service.stop(crash=True)
        await service.start()
        stages["restarted"] = [await view(0), await view(1)]
        for result in (stages["reconnected"], stages["restarted"][0]):
            assert result["radios"] == stages["connected"][0]["radios"]
        assert (
            stages["restarted"][0]["adapter_instance_id"]
            != stages["connected"][0]["adapter_instance_id"]
        )
        assert stages["other_pod_unchanged"]["radios"] == stages["connected"][1]["radios"]
        assert all(
            not s["radios"]
            for s in stages.values()
            if isinstance(s, dict) and s.get("ready") is False
        )
        for admin, before in zip(admins, initial, strict=True):
            assert (await admin.snapshot())["tables"] == before
        report["observed_tables_unchanged_after_restoring_fixture_faults"] = True
        report["passed"] = True
        return report
    finally:
        await service.stop()
        for admin in admins:
            await admin.close()
        for db in databases:
            await db.close()
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
