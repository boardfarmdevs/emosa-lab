"""Prepare a live selected controller beside a connecting simulated pod and adapter.

This collects independent controller inventory and IEEE traffic. EMOSA wire
execution remains blocked; simultaneous components do not prove onboarding.
"""

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from run import CONTROLLER, guard, node, stop_collect
from setup import NODES, ROOT, inside, lxc

from emosa.config import load
from emosa.evaluation.evidence import artifact, write_json
from emosa.secrets import SecretStore
from emosa.simulation.connecting_pod import AL_MAC, SERIAL, configuration, verify
from emosa.simulation.database import SimDatabase, SimManager

STAGED = Path("/opt/emosa-radio-manager")
KEY = "ControllerTrialSynthetic2026!"


def idle():
    guard()
    for name in NODES:
        active = inside(
            name,
            "systemctl",
            "list-units",
            "--state=active,activating,deactivating",
            "--no-legend",
            "--plain",
            "emosa-baseline-*",
            "emosa-radio-manager-*",
        )
        if active.strip():
            raise RuntimeError("A lab service is still active; collect it before preparing")


def inventory(depth=6):
    if type(depth) is not int or not 1 <= depth <= 8:
        raise ValueError("inventory depth must be between one and eight")
    raw = inside(
        CONTROLLER,
        "ubus",
        "call",
        "Device.WiFi.DataElements.Network",
        "_get",
        json.dumps({"rel_path": "", "depth": depth}),
    )
    objects = []
    decoder = json.JSONDecoder()
    while raw.strip():
        value, end = decoder.raw_decode(raw.lstrip())
        if value.get("amxd-error-code", 0):
            raise RuntimeError("Native controller inventory unavailable")
        objects.append(value)
        raw = raw.lstrip()[end:]
    if not objects:
        raise RuntimeError("Empty native controller reply")
    return objects


def bml_policy():
    commands = []
    for al, bsses in (
        (
            "02:00:00:e0:00:01",
            (("emosa-trial-root", "fronthaul"), ("emosa-trial-backhaul", "backhaul")),
        ),
        (AL_MAC, (("emosa-controller-trial", "fronthaul"),)),
    ):
        commands.append(f"bml_clear_wifi_credentials {al}")
        for ssid, role in bsses:
            commands.append(f"bml_set_wifi_credentials {al} {ssid} {KEY} 24g-5g {role} 0")
    commands.append("bml_update_wifi_credentials")
    for command in commands:
        output = inside(CONTROLLER, "/opt/prpl-install-nl80211/bin/beerocks_cli", "-c", command)
        if "return value is: BML_RET_OK, Success status" not in output:
            raise RuntimeError("Native BML did not accept the synthetic trial policy")
    return {
        "submitted": True,
        "virtual_al_mac": AL_MAC,
        "virtual_bss_count": 1,
        "pod_application_proven": False,
    }


async def prepare(label):
    idle()
    directory = ROOT / "controller-trials" / label
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    matrix = json.loads((STAGED / "protocol-matrix.json").read_text())
    # No JSON flag can enable a transport that has not been implemented or qualified.
    report = {
        "schema_version": 1,
        "label": label,
        "status": "preparing",
        "preparation_checks_passed": False,
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
        "radio_behavior_proven": False,
        "wire_execution": "blocked_P0_and_transport_not_implemented",
        "matrix_status": matrix["status"],
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "matrix_sha256": hashlib.sha256((STAGED / "protocol-matrix.json").read_bytes()).hexdigest(),
        "acceptance_path": matrix["acceptance_path"],
        "required_next_evidence": [
            "Qualified EMOSA discovery/WSC traffic causally attributed to this virtual AL",
            "Native controller inventory containing that AL and the represented radio/BSS",
            "Authenticated complete-radio request linked to the operation and guarded Config write",
            "Fresh independently produced State plus client authentication and data traffic",
            "Repeat with an unchanged qualified physical pod",
        ],
    }
    write_json(directory / "result.json", report)
    database = SimDatabase()
    manager = SimManager(database.endpoint)
    listener = "unix:" + str(database.directory / "emosa.sock")
    config = configuration(directory, listener)
    config["request_source"] = "controller-trial-semantic-control"
    write_json(directory / "adapter.json", config)
    load("config", directory / "adapter.json")
    vault = SecretStore(directory / "secrets")
    vault.write_simulated("trial", KEY)
    intent = {
        "pod_id": "pod-1",
        "radio_id": "radio-1",
        "bss_id": "bss-1",
        "ssid": "emosa-controller-trial",
        "secret_ref": "trial",
    }
    capture = None
    console = (directory / "capture.log").open("ab")
    peer_touched = False
    try:
        report["previous_controller_collection"] = stop_collect(
            directory / "previous-controller", names=(CONTROLLER,)
        )
        peer_touched = True
        for filename in ("node.py", "reference.json"):
            source = Path(__file__).with_name(filename)
            lxc("file", "push", "--quiet", str(source), CONTROLLER + str(ROOT / filename))
        node(CONTROLLER, "prepare", "--backhaul", "wired")
        capture = subprocess.Popen(
            [
                "tcpdump",
                "-i",
                "em-base-bh",
                "-U",
                "-s",
                "0",
                "-w",
                str(directory / "controller-link.pcap"),
                "ether",
                "proto",
                "0x893a",
            ],
            stdout=console,
            stderr=console,
        )
        node(CONTROLLER, "hostap")
        node(CONTROLLER, "services")
        node(CONTROLLER, "agent")  # Only the controller's colocated local agent.
        deadline = time.monotonic() + 45
        while True:
            try:
                before = inventory()
                if "02:00:00:e0:00:01" not in json.dumps(before):
                    raise RuntimeError("Controller local device not present yet")
                report["controller_policy"] = bml_policy()
                break
            except (subprocess.CalledProcessError, RuntimeError):
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.5)
        write_json(directory / "controller-before.json", before)
        await database.start()
        await database.seed(serial_number=SERIAL)
        await manager.start()
        component = await verify(directory, database, manager, config, intent, listener)
        assert component["passed"] and not component["controller_onboarding_proven"]
        after = inventory()
        write_json(directory / "controller-after.json", after)
        # This is an explicit negative control: a local diagnostic entry is not a peer entry.
        device_ids = {
            value.get("ID")
            for obj in after
            for key, value in obj.items()
            if re.fullmatch(r"Device\.WiFi\.DataElements\.Network\.Device\.\d+\.", key)
            and isinstance(value, dict)
        }
        if AL_MAC in device_ids:
            raise RuntimeError(
                "Unexpected virtual AL in controller inventory; investigate other writers"
            )
        report["virtual_agent_in_local_directory"] = True
        report["virtual_agent_in_controller_inventory"] = False
        report["request_source"] = "local semantic control, not captured controller traffic"
        report["status"] = "prepared_wire_blocked"
        report["preparation_checks_passed"] = True
    except BaseException as exc:
        report.update(status="preparation_failed", error=type(exc).__name__)
        raise
    finally:
        errors = []
        for action in (manager.close, database.close):
            try:
                await action()
            except Exception as exc:
                errors.append(type(exc).__name__)
        if capture is not None:
            try:
                if capture.poll() is None:
                    capture.send_signal(signal.SIGINT)
                    await asyncio.to_thread(capture.wait, 10)
                if capture.returncode != 0:
                    raise RuntimeError("Independent capture failed")
                decoded = subprocess.run(
                    [
                        "tshark",
                        "-r",
                        str(directory / "controller-link.pcap"),
                        "-Y",
                        "ieee1905",
                        "-T",
                        "fields",
                        "-e",
                        "frame.number",
                        "-e",
                        "eth.src",
                        "-e",
                        "eth.dst",
                        "-e",
                        "ieee1905.message_type",
                        "-e",
                        "ieee1905.message_id",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                ).stdout
                (directory / "controller-frames.tsv").write_text(decoded)
                report["independent_controller_link_frames"] = len(decoded.splitlines())
                report["capture_observation"] = (
                    "IEEE frames observed; no EMOSA wire endpoint"
                    if decoded.strip()
                    else "No IEEE frames observed in this preparation window"
                )
                report["captured_traffic_caused_semantic_operation"] = False
            except Exception as exc:
                if capture.poll() is None:
                    capture.kill()
                    await asyncio.to_thread(capture.wait, 5)
                errors.append("capture:" + type(exc).__name__)
        if peer_touched:
            try:
                report["controller_shutdown"] = stop_collect(
                    directory / "shutdown", names=(CONTROLLER,)
                )
                report["native_shutdown_abnormal"] = any(
                    unit["after"].get("Result") != "success"
                    for units in report["controller_shutdown"].values()
                    for unit in units
                )
            except Exception as exc:
                errors.append("peer_collection:" + type(exc).__name__)
        console.close()
        try:
            idle()
        except Exception as exc:
            errors.append("remaining_units:" + type(exc).__name__)
        report["cleanup_errors"] = errors
        if errors:
            report.update(status="preparation_failed", preparation_checks_passed=False)
        report["artifacts"] = [
            artifact(p, directory)
            for p in sorted(directory.iterdir())
            if p.is_file() and p.name not in {"result.json", "adapter.json"}
        ]
        write_json(directory / "result.json", report)
    if not report["preparation_checks_passed"]:
        raise RuntimeError("Controller preparation failed; inspect retained evidence")
    print(
        json.dumps(
            {"label": label, "status": report["status"], "controller_onboarding_proven": False}
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", args.label):
        raise SystemExit("Use a new simple label of 1–24 characters")
    os.umask(0o077)
    idle()
    with (STAGED / "run.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(prepare(args.label))
