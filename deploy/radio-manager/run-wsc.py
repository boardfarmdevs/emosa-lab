"""Actual Ethernet WSC component → OVSDB → hwsim → independent client probes.

Uses the existing owned radio lab and synthetic payload peer, not a native
EasyMesh controller. No semantic submission supplies the configuration change.
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
import sys
import time
from collections import Counter

from common import CLIENTS, NODES, ROOT, guard, inside, run, write
from run import clients, connect, eventually, require_idle, setup

from emosa.evaluation.evidence import artifact
from emosa.opensync.session import OvsSession
from emosa.simulation.database import SimDatabase
from emosa.simulation.radio import MONITOR, seed_radio_database
from emosa.simulation.wsc_provisioning import SERIAL
from emosa.simulation.wsc_wire import RADIO_BSSID, SSID, verify_registrar

# Deliberately public fixture input from the independent C registrar. Used only
# by the observer; never submitted via the semantic API to drive configuration.
KEY = "OnlySimulationWscKey2026!"


async def experiment(label, *, lost_reply):
    guard()
    require_idle()
    directory = ROOT / "runs" / label
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    verify_registrar(ROOT / "registrar/component-registrar")
    write(
        directory / "wire-owner.json", {"owner": "emosa-owned-hwsim-wsc-v1", "backend": "ovsdb-sim"}
    )
    db = SimDatabase(directory / "database")
    writer = OvsSession(db.endpoint, monitor_columns=MONITOR)
    manager = driver = capture = None
    logs = {
        name: (directory / (name + ".log")).open("ab")
        for name in ("manager-console", "wire-driver", "capture")
    }
    report = {
        "schema_version": 1,
        "label": label,
        "status": "running",
        "passed": False,
        "initiating_interface": "wsc-component",
        "semantic_submission": False,
        "socket_io": True,
        "native_controller_used": False,
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
        "radio": "mac80211_hwsim",
        "radio_behavior_proven": False,
        "lost_reply_injected": lost_reply,
        "cases": {},
    }
    cases = report["cases"]
    started = time.monotonic()

    async def state_ready():
        raw = await writer.snapshot()
        row = next(iter(raw["tables"]["Wifi_VIF_State"].values()))
        row = raw["schema"].row("Wifi_VIF_State", row)
        return {"enabled": row.get("enabled"), "ssid": row.get("ssid"), "mac": row.get("mac")}

    async def barrier(path, seconds=40):
        end = time.monotonic() + seconds
        while not path.is_file():
            if driver is not None and driver.returncode is not None:
                raise RuntimeError(
                    "packet workers ended before required evidence; inspect wire logs"
                )
            if time.monotonic() >= end:
                raise TimeoutError("radio wire experiment barrier timed out")
            await asyncio.sleep(0.05)

    try:
        write(directory / "topology.json", setup(directory))
        source_files = [
            *ROOT.glob("*.py"),
            *(ROOT / "source/emosa").rglob("*.py"),
            *(ROOT / "schemas").glob("*.json"),
            ROOT / "peer-reference.json",
            ROOT / "registrar/provenance.json",
            ROOT / "registrar/component-registrar",
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
            stdout=logs["capture"],
            stderr=logs["capture"],
        )
        await db.start()
        await seed_radio_database(writer)
        inserted = await writer.transact(
            [
                {
                    "op": "insert",
                    "table": "AWLAN_Node",
                    "row": {
                        "serial_number": SERIAL,
                        "model": "EMOSA hwsim WSC extender",
                        "firmware_version": "simulation-only",
                    },
                }
            ]
        )
        assert len(inserted) == 1 and "uuid" in inserted[0]
        write(directory / "policy.json", {"withhold": False})
        manager = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "manager.py"),
            str(directory),
            stdout=logs["manager-console"],
            stderr=logs["manager-console"],
        )
        await eventually(
            state_ready,
            lambda r: r == {"enabled": True, "ssid": "emosa-radio-initial", "mac": RADIO_BSSID},
        )
        connect("emosa-radio-initial", "RadioInitial2026!")
        cases["initial_clients"] = await clients(directory, "initial", "emosa-radio-initial")
        boundary = time.monotonic()
        write(directory / "policy.json", {"withhold": True})

        async def manager_event():
            lines = (directory / "manager.jsonl").read_text().splitlines()
            return json.loads(lines[-1]) if lines else {}

        cases["withhold_barrier"] = await eventually(
            manager_event,
            lambda r: r.get("monotonic", 0) > boundary and r.get("configuration") == "withheld",
        )
        await db.manager_remote("unix:" + str(db.directory / "wire-pod.sock"))
        wire = directory / "ethernet"
        driver = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "wire-endpoint.py"),
            "--provisioning",
            "--registrar",
            str(ROOT / "registrar/component-registrar"),
            "--directory",
            str(wire),
            "--radio-directory",
            str(directory),
            *(["--lost-reply"] if lost_reply else []),
            stdout=logs["wire-driver"],
            stderr=logs["wire-driver"],
        )
        await barrier(wire / "wire-ready.json")
        before = json.loads((wire / "wire-ready.json").read_text())
        assert before["config_ssid"] == SSID and before["observed_ssid"] == "emosa-radio-initial"
        assert before["operation"]["initiating_interface"] == "wsc-component"
        assert before["operation"]["state"] == (
            "INDETERMINATE" if lost_reply else "CONFIG_COMMITTED"
        )
        cases["wire_operation_before_apply"] = before
        cases["withheld_clients"] = await clients(directory, "withheld", "emosa-radio-initial")
        write(directory / "policy.json", {"withhold": False})
        (wire / "allow-application").touch()
        await barrier(wire / "left.json")
        assert await asyncio.wait_for(driver.wait(), 10) == 0
        final = json.loads((wire / "left.json").read_text())
        assert final["operation"]["state"] == "OBSERVED_APPLIED"
        assert (
            final["operation"]["application_evidence"]["provenance"]
            == "independent-hostapd-nl80211-manager:Wifi_VIF_State"
        )
        cases["wire_operation_applied"] = final["operation"]
        connect(SSID, KEY)
        cases["changed_clients"] = await clients(directory, "changed", SSID)

        # A fresh supplicant log must report a real failed authentication with
        # a different key. Merely failing an HTTP probe is not this evidence.
        offset = int(inside(CLIENTS[1], "stat", "-c", "%s", str(ROOT / "client.log")))
        connect(SSID, "DeliberatelyWrongWscKey!")

        async def wrong_key():
            return await asyncio.to_thread(
                inside,
                CLIENTS[1],
                "python3",
                "-c",
                f"from pathlib import Path; print(Path({str(ROOT / 'client.log')!r})"
                f".read_bytes()[{offset}:].decode(errors='replace'))",
            )

        wrong = await eventually(wrong_key, lambda text: "WRONG_KEY" in text, 20)
        (directory / "wrong-key.log").write_text(wrong)
        status = inside(
            CLIENTS[1],
            "/opt/emosa-baseline/hostap/bin/wpa_cli",
            "-p",
            str(ROOT / "ctrl"),
            "-i",
            "wlan0",
            "status",
        )
        assert "wpa_state=COMPLETED" not in status
        cases["wrong_key"] = {"fresh_wrong_key_event": True, "authenticated": False}
        connect(SSID, KEY)
        cases["restored_clients"] = await clients(directory, "restored", SSID)
        report["status"] = "awaiting_capture_validation"
    except BaseException as exc:
        report.update(status="failed", error=type(exc).__name__, detail=str(exc))
        raise
    finally:
        errors = []

        async def cleanup(action):
            try:
                await action()
            except Exception as exc:
                errors.append(type(exc).__name__)

        async def stop_child(child):
            if child is not None and child.returncode is None:
                child.terminate()
                try:
                    await asyncio.wait_for(child.wait(), 35)
                except TimeoutError:
                    child.kill()
                    await child.wait()
                    raise

        await cleanup(lambda: stop_child(driver))
        await cleanup(lambda: stop_child(manager))
        await cleanup(writer.close)
        await cleanup(db.stop)
        if capture is not None:
            try:
                if capture.poll() is None:
                    capture.send_signal(signal.SIGINT)
                    await asyncio.to_thread(capture.wait, 10)
                assert capture.returncode == 0
                beacons = run(
                    "tshark",
                    "-r",
                    str(directory / "radio.pcap"),
                    "-Y",
                    "wlan.fc.type_subtype == 8 && wlan.sa == " + RADIO_BSSID,
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
                assert {"emosa-radio-initial", SSID} <= counts.keys()
                assert {"1", "2", "3", "4"} <= {s.split("\t")[-1] for s in eapol.splitlines()}
                write(
                    directory / "packet-observations.json",
                    {
                        "source": "independent tshark",
                        "beacon_counts": counts,
                        "eapol_frames": len(eapol.splitlines()),
                        "capture": artifact(directory / "radio.pcap", directory),
                    },
                )
            except Exception as exc:
                if capture.poll() is None:
                    capture.kill()
                    await asyncio.to_thread(capture.wait, 5)
                errors.append("capture:" + type(exc).__name__)
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
                    errors.append("unit:" + type(exc).__name__)
        try:
            require_idle()
        except Exception as exc:
            errors.append("remaining_units:" + type(exc).__name__)
        for stream in logs.values():
            stream.close()
        report["cleanup_errors"] = errors
        if report["status"] == "awaiting_capture_validation" and not errors:
            report.update(status="passed", passed=True, radio_behavior_proven=True)
        elif errors:
            report["status"] = "failed"
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write(directory / "result.json", report)
    assert report["passed"], "Inspect retained run result and logs"
    print(
        json.dumps(
            {
                "label": label,
                "passed": True,
                "lost_reply": lost_reply,
                "controller_onboarding_proven": False,
                "physical_pod_proven": False,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--lost-reply", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", args.label):
        parser.error("Use a new label of 1-24 lowercase letters, numbers or hyphens")
    os.umask(0o077)
    guard()
    with (ROOT / "run.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(experiment(args.label, lost_reply=args.lost_reply))
