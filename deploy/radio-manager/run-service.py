"""Pod-initiated OVSDB → actual EMOSA process → hwsim AP and independent clients."""

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import Counter

from common import NODES, ROOT, guard, inside, run, write
from run import CHANGED_KEY, clients, connect, eventually, require_idle, setup

from emosa.config import load
from emosa.evaluation.evidence import artifact, write_json
from emosa.evaluation.service import AdapterProcess
from emosa.opensync.session import OvsSession
from emosa.secrets import SecretStore
from emosa.simulation.connecting_pod import AL_MAC, SERIAL, configuration
from emosa.simulation.database import SimDatabase
from emosa.simulation.radio import MONITOR, seed_radio_database

PROVENANCE = "independent-hostapd-nl80211-manager:Wifi_VIF_State"


async def experiment(label):
    guard()
    require_idle()
    directory = ROOT / "runs" / label
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    db = SimDatabase(directory / "database")
    writer = OvsSession(db.endpoint, monitor_columns=MONITOR)
    listener = "unix:" + str(db.directory / "emosa.sock")
    config = configuration(directory, listener)
    config["request_source"] = "service-radio-lab"
    config["pods"][0].update(if_name="wlan0", radio_name="phy1", state_provenance=PROVENANCE)
    write(directory / "adapter.json", config)
    load("config", directory / "adapter.json")
    service = AdapterProcess(
        directory / "adapter.json", config["socket_path"], directory / "adapter.log"
    )
    vault = SecretStore(directory / "secrets")
    vault.write_simulated("change", CHANGED_KEY)
    vault.write_simulated("restart", "RadioServiceRestart2026!")
    manager = capture = None
    console = (directory / "manager-console.log").open("ab")
    capture_log = (directory / "capture.log").open("ab")
    report = {
        "schema_version": 1,
        "label": label,
        "status": "running",
        "passed": False,
        "initiating_interface": "semantic local API",
        "backend_mode": "ovsdb-sim",
        "pod_connection": "pod-initiated private Unix OVSDB stream",
        "radio": "mac80211_hwsim",
        "radio_behavior_proven": False,
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
        "started_monotonic": time.monotonic(),
        "cases": {},
    }
    cases = report["cases"]
    operations = []
    write_json(directory / "result.json", report)

    async def agents(state):
        return await service.wait_for("agents", lambda r: r["agents"][0]["state"] == state)

    async def submit(name, ssid):
        value = await service.call(
            "component.submit",
            {
                "intent": {
                    "pod_id": "pod-1",
                    "radio_id": "radio-1",
                    "bss_id": "bss-1",
                    "ssid": ssid,
                    "secret_ref": name,
                },
                "idempotency_key": name,
                "run_id": label,
                "apply_seconds": 90,
            },
        )
        operations.append(value["operation_id"])
        return value

    async def finish(op, state="OBSERVED_APPLIED"):
        return await service.wait_for(
            "operation.show",
            lambda r: r["state"] == state,
            {"operation_id": op["operation_id"]},
            timeout=35,
        )

    async def withhold_barrier():
        boundary = time.monotonic()
        write(directory / "policy.json", {"withhold": True})

        async def latest():
            lines = (directory / "manager.jsonl").read_text().splitlines()
            return json.loads(lines[-1]) if lines else {}

        return await eventually(
            latest,
            lambda r: r.get("monotonic", 0) > boundary and r.get("configuration") == "withheld",
        )

    try:
        write(directory / "topology.json", setup(directory))
        source_files = [
            *ROOT.glob("*.py"),
            ROOT / "peer-reference.json",
            *(ROOT / "source/emosa").rglob("*.py"),
            *(ROOT / "schemas").glob("*.json"),
        ]
        write(
            directory / "source-hashes.json",
            {
                str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(source_files)
            },
        )
        capture = subprocess.Popen(
            ["tcpdump", "-i", "hwsim0", "-U", "-s", "0", "-w", str(directory / "radio.pcap")],
            stdout=capture_log,
            stderr=capture_log,
        )
        await db.start()
        await seed_radio_database(writer)
        result = await writer.transact(
            [
                {
                    "op": "insert",
                    "table": "AWLAN_Node",
                    "row": {
                        "serial_number": SERIAL,
                        "model": "EMOSA hwsim synthetic extender",
                        "firmware_version": "simulation-only",
                    },
                }
            ]
        )
        assert len(result) == 1 and "uuid" in result[0]
        await service.start()
        cases["before_connection"] = await agents("pending")
        write(directory / "policy.json", {"withhold": False})
        manager = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / "manager.py"), str(directory), stdout=console, stderr=console
        )
        await db.manager_remote(listener)
        first = cases["pod_connected"] = await agents("ready")
        assert first["agents"][0]["inventory"]["device_identity"][0]["serial_number"] == SERIAL
        connect("emosa-radio-initial", "RadioInitial2026!")
        cases["initial_clients"] = await clients(directory, "initial", "emosa-radio-initial")

        op = await submit("change", "emosa-service-changed")
        cases["configuration_applied"] = await finish(op)
        repeated = await submit("change", "emosa-service-changed")
        assert repeated["operation_id"] == op["operation_id"] and len(repeated["attempts"]) == 1
        connect("emosa-service-changed", CHANGED_KEY)
        cases["changed_clients"] = await clients(directory, "changed", "emosa-service-changed")
        cases["withhold_barrier"] = await withhold_barrier()
        pending = await submit("restart", "emosa-service-restarted")
        cases["committed_before_crash"] = await finish(pending, "CONFIG_COMMITTED")
        # Independent observation still sees the prior SSID while Config is committed.
        cases["withheld_clients"] = await clients(directory, "withheld", "emosa-service-changed")
        await service.stop(crash=True)
        await service.start()
        cases["pending_after_restart"] = await finish(pending, "CONFIG_COMMITTED")
        write(directory / "policy.json", {"withhold": False})
        recovered = cases["applied_after_restart"] = await finish(pending)
        assert len(recovered["attempts"]) == 1
        connect("emosa-service-restarted", "RadioServiceRestart2026!")
        cases["restart_clients"] = await clients(directory, "restarted", "emosa-service-restarted")
        await agents("ready")
        await db.manager_remote(listener, connect=False)
        offline = cases["pod_disconnected"] = await agents("unavailable")
        cases["clients_without_management"] = await clients(
            directory, "disconnected", "emosa-service-restarted"
        )
        await db.manager_remote(listener)
        ready = cases["pod_reconnected"] = await agents("ready")
        assert (
            ready["agents"][0]["inventory"]["generation"]
            > (offline["agents"][0]["inventory"]["generation"])
        )
        assert ready["agents"][0]["al_mac"] == AL_MAC
        starts = [x for x in service.lifecycle if x["event"] == "started"]
        assert len({x["adapter_instance_id"] for x in starts}) == 2
        observation = await service.call("pods")
        assert observation["pods"][0]["observation"]["provenance"] == PROVENANCE
        write_json(directory / "final-pods.json", observation)
        write_json(directory / "events.json", await service.call("events", {"run_id": label}))
        write_json(
            directory / "operations.json",
            [
                await service.call("operation.show", {"operation_id": oid})
                for oid in dict.fromkeys(operations)
            ],
        )
        report["status"] = "awaiting_capture_validation"
    except BaseException as exc:
        report.update(status="failed", error=type(exc).__name__, detail=str(exc))
        raise
    finally:
        cleanup_errors = []

        async def cleanup(action):
            try:
                await action()
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)

        async def stop_child(process, *, capture=False):
            if process is None or process.returncode is not None:
                return
            process.send_signal(signal.SIGINT if capture else signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 35)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise

        await cleanup(service.stop)
        await cleanup(lambda: stop_child(manager))
        await cleanup(writer.close)
        await cleanup(db.stop)
        if capture is not None:
            try:
                if capture.poll() is None:
                    capture.send_signal(signal.SIGINT)
                    await asyncio.to_thread(capture.wait, 10)
                if capture.returncode != 0:
                    raise RuntimeError("packet capture failed")
                beacons = run(
                    "tshark",
                    "-r",
                    str(directory / "radio.pcap"),
                    "-Y",
                    "wlan.fc.type_subtype == 8 && wlan.sa == 02:00:00:ec:02:00",
                    "-T",
                    "fields",
                    "-e",
                    "wlan.ssid",
                )
                counts = Counter(bytes.fromhex(line).decode() for line in beacons.splitlines())
                eapol = run(
                    "tshark",
                    "-r",
                    str(directory / "radio.pcap"),
                    "-Y",
                    "eapol && wlan.addr == 02:00:00:00:02:00",
                    "-T",
                    "fields",
                    "-e",
                    "frame.number",
                    "-e",
                    "wlan_rsna_eapol.keydes.msgnr",
                )
                (directory / "eapol.tsv").write_text(eapol)
                assert {
                    "emosa-radio-initial",
                    "emosa-service-changed",
                    "emosa-service-restarted",
                } <= counts.keys()
                assert {"1", "2", "3", "4"} <= {line.split("\t")[-1] for line in eapol.splitlines()}
                write(
                    directory / "packet-observations.json",
                    {
                        "source": "independent tshark decoding",
                        "beacon_counts": counts,
                        "eapol_frames": len(eapol.splitlines()),
                        "capture": artifact(directory / "radio.pcap", directory),
                    },
                )
            except Exception as exc:
                if capture.poll() is None:
                    capture.kill()
                    await asyncio.to_thread(capture.wait, 5)
                cleanup_errors.append("capture:" + type(exc).__name__)
        for node in NODES:
            for unit in ("ap", "client", "data"):
                try:
                    inside(
                        node,
                        "systemctl",
                        "stop",
                        f"emosa-radio-manager-{unit}.service",
                        check=False,
                    )
                except Exception as exc:
                    cleanup_errors.append("unit:" + type(exc).__name__)
        try:
            require_idle()
        except Exception as exc:
            cleanup_errors.append("remaining_units:" + type(exc).__name__)
        console.close()
        capture_log.close()
        report["service_lifecycle"] = service.lifecycle
        report["cleanup_errors"] = cleanup_errors
        if cleanup_errors:
            report["status"] = "failed"
        elif report["status"] == "awaiting_capture_validation":
            report.update(status="passed", passed=True, radio_behavior_proven=True)
        report["elapsed_seconds"] = round(time.monotonic() - report["started_monotonic"], 3)
        report["artifacts"] = [
            artifact(p, directory)
            for p in sorted(directory.glob("*.json"))
            if p.name not in {"adapter.json", "policy.json", "result.json"}
        ]
        write_json(directory / "result.json", report)
    if not report["passed"]:
        raise RuntimeError("Service radio experiment failed; inspect retained result")
    print(json.dumps({"label": label, "status": report["status"], "cases": list(cases)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", args.label):
        raise SystemExit("Use a new label of 1–24 lowercase letters, numbers or hyphens")
    os.umask(0o077)
    guard()
    ROOT.mkdir(exist_ok=True, mode=0o700)
    with (ROOT / "run.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(experiment(args.label))
