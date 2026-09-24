"""Semantic EMOSA → real OVSDB → independent hostapd/hwsim → client experiment."""

import argparse
import asyncio
import fcntl
import hashlib
import json
import secrets
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from common import AP, CLIENTS, NODES, ROOT, SERVER, guard, inside, lxc, run, write

from emosa.model import Intent, State
from emosa.opensync.mapping import OpenSyncBackend
from emosa.opensync.session import OvsSession
from emosa.reconcile import Engine
from emosa.secrets import SecretStore
from emosa.store import Store
from emosa_lab.simulation.database import SimDatabase
from emosa_lab.simulation.radio import MONITOR, seed_radio_database

# Public synthetic edge case: preserve literal quote and backslash bytes.
CHANGED_KEY = 'Radio "quoted\\ key2026!'


def start_unit(node, unit, *args):
    inside(node, "systemd-run", "--quiet", "--property=Type=exec", "--unit", unit, *args)


def require_idle():
    for name in NODES:
        if inside(
            name,
            "systemctl",
            "list-units",
            "--state=active,activating,deactivating",
            "--no-legend",
            "--plain",
            "emosa-radio-manager-*",
        ).strip():
            raise RuntimeError("Previous radio-manager units still active; preserve them first")


def setup(directory):
    info = json.loads(lxc("list", AP, "--format", "json"))[0]
    write(directory / "topology-before.json", info)
    if "eth1" not in info["expanded_devices"]:
        lxc("config", "device", "add", AP, "eth1", "nic", "name=eth1", "network=em-base-bh")
    elif info["expanded_devices"]["eth1"].get("network") != "em-base-bh":
        raise RuntimeError("Unexpected existing backhaul device")
    for name in NODES:
        inside(name, "mkdir", "-p", "-m", "700", str(ROOT))
        for filename in ("node.py", "observer.py", "peer-reference.json"):
            lxc("file", "push", "--quiet", str(ROOT / filename), name + str(ROOT) + "/")
    inside(AP, "ip", "link", "set", "wlan1", "down") if "wlan1" in inside(AP, "iw", "dev") else None
    if "wlan1" in inside(AP, "iw", "dev"):
        inside(AP, "iw", "dev", "wlan1", "del")
    for node, interfaces, address in (
        (SERVER, ("eth1",), "192.0.2.1"),
        (AP, ("eth1", "eth2"), "192.0.2.2"),
    ):
        inside(node, "ip", "link", "set", "br-lan", "up")
        inside(node, "ip", "address", "replace", address + "/24", "dev", "br-lan")
        for iface in interfaces:
            inside(node, "ip", "link", "set", iface, "master", "br-lan")
            inside(node, "ip", "link", "set", iface, "up")
    for node, iface, address in (
        (CLIENTS[0], "eth1", "192.0.2.20"),
        (CLIENTS[1], "wlan0", "192.0.2.21"),
    ):
        inside(node, "ip", "link", "set", iface, "up")
        inside(node, "ip", "address", "replace", address + "/24", "dev", iface)
    start_unit(
        SERVER, "emosa-radio-manager-data.service", "python3", str(ROOT / "observer.py"), "serve"
    )
    run("ip", "link", "set", "hwsim0", "up")
    return {name: json.loads(inside(name, "ip", "-j", "address")) for name in NODES}


def connect(ssid, key):
    inside(
        CLIENTS[1],
        "python3",
        str(ROOT / "observer.py"),
        "connect",
        input=json.dumps({"ssid": ssid, "passphrase": key}),
    )


async def clients(directory, label, ssid):
    nonce = secrets.token_hex(16)
    lxc(
        "file",
        "push",
        "--quiet",
        "--mode",
        "0600",
        "-",
        SERVER + str(ROOT / "response.json"),
        input=json.dumps({"nonce": nonce, "case": label}),
    )
    results = {}
    for name in CLIENTS:
        start = time.monotonic()
        while True:
            try:
                value = await asyncio.to_thread(
                    inside,
                    name,
                    "python3",
                    str(ROOT / "observer.py"),
                    "probe",
                    "--nonce",
                    nonce,
                    "--ssid",
                    ssid,
                )
                results[name] = json.loads(value)
                results[name]["elapsed_seconds"] = round(time.monotonic() - start, 3)
                if results[name]["elapsed_seconds"] > 30:
                    raise RuntimeError("client exceeded 30-second budget")
                break
            except subprocess.CalledProcessError:
                if time.monotonic() - start > 25:
                    raise RuntimeError("client did not pass within 30 seconds") from None
                await asyncio.sleep(0.5)
    write(directory / f"clients-{label}.json", {"nonce": nonce, "observations": results})
    return {"clients": "passed", "nonce": nonce}


async def eventually(function, predicate, seconds=30):
    start = time.monotonic()
    while True:
        result = await function()
        if predicate(result):
            if time.monotonic() - start > seconds:
                raise RuntimeError("observation exceeded deadline")
            return result
        if time.monotonic() - start > seconds:
            raise RuntimeError("observation did not converge")
        await asyncio.sleep(0.1)


async def experiment(label):
    guard()
    require_idle()
    directory = ROOT / "runs" / label
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    result = {
        "label": label,
        "status": "running",
        "initiating_interface": "semantic",
        "backend_mode": "ovsdb-sim",
        "radio": "mac80211_hwsim",
        "real_easymesh_exchange": False,
        "physical_pod": False,
        "cases": {},
        "acceptance_version": 1,
        "started_monotonic": time.monotonic(),
    }
    write(directory / "result.json", result)
    db = SimDatabase(directory / "database")
    vault = SecretStore(directory / "secrets")
    store = Store(directory / "store")
    backend = OpenSyncBackend(
        "pod-1",
        OvsSession(db.endpoint),
        vault,
        if_name="wlan0",
        radio_name="phy1",
        state_provenance="independent-hostapd-nl80211-manager:Wifi_VIF_State",
    )
    writer = OvsSession(db.endpoint, monitor_columns=MONITOR)
    engine = Engine(store, vault, {"pod-1": backend})
    manager = capture = None
    console = (directory / "manager-console.log").open("ab")
    capture_log = (directory / "capture.log").open("ab")

    async def start_manager():
        return await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / "manager.py"), str(directory), stdout=console, stderr=console
        )

    async def stop_manager():
        if manager and manager.returncode is None:
            manager.terminate()
            await asyncio.wait_for(manager.wait(), 35)

    async def operation(name, ssid, key, *, lost_reply=False, deadline=30):
        vault.write_simulated(name, key)
        intent = Intent("pod-1", "radio-1", "bss-1", ssid, name)
        op = engine.request(
            intent, source="radio-manager-lab", key=name, run_id=label, deadline=deadline
        )
        backend.drop_next_reply = lost_reply
        await engine.execute(op.operation_id)
        return op

    async def finish(op, expected=State.OBSERVED_APPLIED):
        async def refresh():
            await engine.reconcile("pod-1")
            return store.get(op.operation_id)

        return await eventually(refresh, lambda item: item.state == expected, 35)

    try:
        write(directory / "topology.json", setup(directory))
        source_files = [
            *ROOT.glob("*.py"),
            ROOT / "peer-reference.json",
            Path(__import__("emosa_lab.simulation.radio", fromlist=["__file__"]).__file__),
            Path(__import__("emosa.opensync.mapping", fromlist=["__file__"]).__file__),
        ]
        write(
            directory / "source-hashes.json",
            {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files},
        )
        (directory / "harness").mkdir()
        for p in source_files:
            shutil.copy2(p, directory / "harness" / p.name)
        capture = subprocess.Popen(
            ["tcpdump", "-i", "hwsim0", "-U", "-s", "0", "-w", str(directory / "radio.pcap")],
            stdout=capture_log,
            stderr=capture_log,
        )
        await db.start()
        await seed_radio_database(writer)
        write(directory / "policy.json", {"withhold": False})
        manager = await start_manager()
        await eventually(backend.snapshot, lambda s: s.observed.values.get("enabled") is True)
        connect("emosa-radio-initial", "RadioInitial2026!")
        result["cases"]["initial"] = await clients(directory, "initial", "emosa-radio-initial")

        op = await operation("change", "emosa-radio-changed", CHANGED_KEY)
        applied = await finish(op)
        # Idempotent redelivery must retain the same operation and transaction count.
        repeated = engine.request(
            Intent(**op.intent), source="radio-manager-lab", key="change", run_id=label
        )
        assert repeated.operation_id == op.operation_id and backend.write_count == 1
        connect("emosa-radio-changed", CHANGED_KEY)
        result["cases"]["change"] = {
            "operation": applied.to_dict(),
            **await clients(directory, "changed", "emosa-radio-changed"),
        }

        # Explicit driver withholding: Config commits, State and old radio stay old.
        write(directory / "policy.json", {"withhold": True})
        await asyncio.sleep(2)
        op = await operation("withheld", "emosa-radio-withheld", "RadioWithheld2026!", deadline=4)
        timed = await finish(op, State.TIMED_OUT)
        snap = await backend.snapshot()
        assert (
            snap.config["ssid"] == "emosa-radio-withheld"
            and snap.observed.values["ssid"] == "emosa-radio-changed"
        )
        result["cases"]["withheld"] = {
            "operation": timed.to_dict(),
            **await clients(directory, "withheld-old-radio", "emosa-radio-changed"),
        }
        write(directory / "policy.json", {"withhold": False})
        await eventually(
            backend.snapshot, lambda s: s.observed.values["ssid"] == "emosa-radio-withheld"
        )
        await engine.reconcile("pod-1")
        connect("emosa-radio-withheld", "RadioWithheld2026!")
        result["cases"]["late-application"] = {
            "operation": store.get(op.operation_id).to_dict(),
            **await clients(directory, "late-application", "emosa-radio-withheld"),
        }

        op = await operation(
            "lost-reply", "emosa-radio-lost-reply", "RadioLostReply2026!", lost_reply=True
        )
        applied = await finish(op)
        assert applied.commit_evidence["attribution"] == "unknown"
        connect("emosa-radio-lost-reply", "RadioLostReply2026!")
        result["cases"]["lost-reply"] = {
            "operation": applied.to_dict(),
            **await clients(directory, "lost-reply", "emosa-radio-lost-reply"),
        }

        offset = int(inside(CLIENTS[1], "stat", "-c", "%s", str(ROOT / "client.log")))
        connect("emosa-radio-lost-reply", "WrongRadioKey2026!")

        async def wrong_key():
            return inside(
                CLIENTS[1],
                "python3",
                "-c",
                f"from pathlib import Path; print(Path({str(ROOT / 'client.log')!r})"
                f".read_bytes()[{offset}:].decode(errors='replace'))",
            )

        evidence = await eventually(wrong_key, lambda s: "WRONG_KEY" in s, 20)
        (directory / "wrong-key.log").write_text(evidence)
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
        result["cases"]["wrong-key"] = {"fresh_wrong_key_event": True, "authenticated": False}
        connect("emosa-radio-lost-reply", "RadioLostReply2026!")
        await clients(directory, "correct-key-restored", "emosa-radio-lost-reply")

        # A driver-unsupported Config must not change the live channel or AP.
        raw = await writer.snapshot()
        radio_uuid = next(iter(raw["tables"]["Wifi_Radio_Config"]))

        async def set_channel(channel):
            response = await writer.transact(
                [
                    {
                        "op": "update",
                        "table": "Wifi_Radio_Config",
                        "where": [["_uuid", "==", ["uuid", radio_uuid]]],
                        "row": {"channel": channel},
                    }
                ]
            )
            assert response == [{"count": 1}]

        await set_channel(11)

        async def manager_event():
            return json.loads((directory / "manager.jsonl").read_text().splitlines()[-1])

        event = await eventually(manager_event, lambda e: e.get("configuration") == "unsupported")
        assert event["observation"]["channel"] == 6
        result["cases"]["unsupported-channel"] = {
            "requested": 11,
            "observed": 6,
            **await clients(directory, "unsupported-channel", "emosa-radio-lost-reply"),
        }
        await set_channel(6)

        # Restart only the semantic adapter/journal reader, preserving the radio manager.
        await backend.close()
        backend = OpenSyncBackend(
            "pod-1",
            OvsSession(db.endpoint),
            vault,
            if_name="wlan0",
            radio_name="phy1",
            state_provenance="independent-hostapd-nl80211-manager:Wifi_VIF_State",
        )
        store.close()
        store = Store(directory / "store")
        engine = Engine(store, vault, {"pod-1": backend})
        engine.recover()
        assert store.get(op.operation_id).commit_evidence["attribution"] == "unknown"
        result["cases"]["adapter-restart"] = await clients(
            directory, "adapter-restart", "emosa-radio-lost-reply"
        )

        # New OVSDB generation and manager process must recover from current Config.
        previous_generation = (await backend.snapshot()).generation
        restart_time = time.monotonic()
        await stop_manager()
        await db.stop()
        await backend.session.reconnect()
        await db.start()
        manager = await start_manager()
        await eventually(
            manager_event, lambda e: e["monotonic"] > restart_time and e.get("radio") == "observed"
        )
        recovered = await eventually(
            backend.snapshot,
            lambda s: (
                s.observed.values.get("enabled") is True and s.generation > previous_generation
            ),
        )
        result["cases"]["database-manager-restart"] = {
            "previous_generation": previous_generation,
            "new_generation": recovered.generation,
            **await clients(directory, "database-restart", "emosa-radio-lost-reply"),
        }

        # Radio State cannot establish forwarding. Prove the independent probes
        # detect a broken wired backhaul while the AP remains enabled.
        inside(AP, "ip", "link", "set", "eth1", "down")
        try:
            losses = {}
            for name, iface in ((CLIENTS[0], "eth1"), (CLIENTS[1], "wlan0")):
                process = subprocess.run(
                    [
                        "lxc",
                        "--force-local",
                        "--project",
                        "default",
                        "exec",
                        name,
                        "--",
                        "ping",
                        "-I",
                        iface,
                        "-c",
                        "1",
                        "-W",
                        "1",
                        "192.0.2.1",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert process.returncode != 0
                losses[name] = {"returncode": process.returncode, "output": process.stdout}
            snap = await backend.snapshot()
            assert snap.observed.values["enabled"] is True
            write(directory / "backhaul-loss.json", losses)
            result["cases"]["backhaul-loss"] = {
                "both_clients_detected_failure": True,
                "radio_state_enabled": True,
            }
        finally:
            inside(AP, "ip", "link", "set", "eth1", "up")
        result["cases"]["backhaul-recovery"] = await clients(
            directory, "backhaul-recovery", "emosa-radio-lost-reply"
        )

        # Withdraw successful State after AP failure while actuation is withheld.
        write(directory / "policy.json", {"withhold": True})
        await asyncio.sleep(2)
        inside(AP, "python3", str(ROOT / "node.py"), "stop")
        await eventually(backend.snapshot, lambda s: s.observed.values.get("enabled") is False)
        failed_ping = subprocess.run(
            [
                "lxc",
                "exec",
                CLIENTS[1],
                "--",
                "ping",
                "-I",
                "wlan0",
                "-c",
                "1",
                "-W",
                "1",
                "192.0.2.1",
            ],
            capture_output=True,
            text=True,
        )
        assert failed_ping.returncode != 0
        result["cases"]["ap-unavailable"] = {"state_enabled": False, "wireless_ping_failed": True}
        await stop_manager()
        write(directory / "policy.json", {"withhold": False})
        manager = await start_manager()
        await eventually(backend.snapshot, lambda s: s.observed.values.get("enabled") is True)
        result["cases"]["ap-recovery"] = await clients(
            directory, "ap-recovery", "emosa-radio-lost-reply"
        )
        result["status"] = "passed"
    except Exception as error:
        result.update(status="failed", error=type(error).__name__, detail=str(error))
        raise
    finally:
        write(directory / "result.json", result)
        await stop_manager()
        await backend.close()
        await writer.close()
        await db.stop()
        if capture:
            capture.send_signal(2)
            await asyncio.to_thread(capture.wait, 10)
        console.close()
        capture_log.close()
        if capture:
            try:
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
                required = {
                    "emosa-radio-initial",
                    "emosa-radio-changed",
                    "emosa-radio-withheld",
                    "emosa-radio-lost-reply",
                }
                if result["status"] == "passed":
                    assert required <= counts.keys()
                    assert {"1", "2", "3", "4"} <= {
                        line.split("\t")[-1] for line in eapol.splitlines()
                    }
                write(
                    directory / "packet-observations.json",
                    {
                        "beacon_counts": counts,
                        "eapol_frames": len(eapol.splitlines()),
                        "source": "independent tshark decoding",
                        "radio_pcap_sha256": hashlib.sha256(
                            (directory / "radio.pcap").read_bytes()
                        ).hexdigest(),
                    },
                )
            except Exception as error:
                result.update(status="failed", collection_error=type(error).__name__)
        write(directory / "operations.json", [op.to_dict() for op in store.operations()])
        store.close()
        for node in NODES:
            for unit in ("ap", "client", "data"):
                inside(
                    node, "systemctl", "stop", f"emosa-radio-manager-{unit}.service", check=False
                )
            archive = ROOT / "archive" / label
            inside(
                node,
                "python3",
                "-c",
                "from pathlib import Path; import shutil; "
                f"r=Path({str(ROOT)!r}); d=Path({str(archive)!r}); "
                "d.mkdir(parents=True,exist_ok=False); "
                "[shutil.copy2(p,d/p.name) for p in r.iterdir() if p.suffix in {'.log','.conf'}]",
            )
            lxc(
                "file", "pull", "--recursive", "--quiet", node + str(archive), str(directory / node)
            )
        print(
            json.dumps(
                {"label": label, "status": result["status"], "cases": list(result["cases"])}
            ),
            flush=True,
        )
        result["elapsed_seconds"] = round(time.monotonic() - result["started_monotonic"], 3)
        write(directory / "result.json", result)
        if result["status"] != "passed":
            raise RuntimeError("Radio manager experiment failed; retained result has details")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    if not __import__("re").fullmatch(r"[a-z0-9][a-z0-9-]{0,50}", args.label):
        raise SystemExit("Expected a new simple label")
    guard()
    ROOT.mkdir(exist_ok=True, mode=0o700)
    with (ROOT / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(experiment(args.label))
