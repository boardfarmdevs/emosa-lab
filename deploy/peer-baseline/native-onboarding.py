"""Owned native controller → EMOSA → simulated pod → independent radio/client.

Run only on the existing emosa-lab VM. A candidate wrapper backs up/restores the
controller. No native agent runs on the simulated pod. Every label is retained.
"""

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import runpy
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from run import CONTROLLER, node, stop_collect
from setup import ROOT, run

from emosa.opensync.session import OvsSession
from emosa.simulation.database import SimDatabase
from emosa.simulation.forwarding import ForwardingSource
from emosa.simulation.native_onboarding import OWNER, RADIO_ROOT
from emosa.simulation.neighbor_binding import NeighborSource
from emosa.simulation.radio import MONITOR, seed_radio_database
from emosa.simulation.wsc_provisioning import SERIAL
from emosa.simulation.wsc_wire import RADIO_BSSID, write

RADIO_ROOT_DIR = RADIO_ROOT.parent


def inventory_bss(objects):
    rows = {key: value for obj in objects for key, value in obj.items()}
    device = next(
        (k for k, v in rows.items() if isinstance(v, dict) and v.get("ID") == "02:00:00:00:30:01"),
        None,
    )
    return {
        k: v
        for k, v in rows.items()
        if device and k.startswith(device) and re.search(r"Radio\.\d+\.(BSS\.\d+\.)?$", k)
    }


def inventory_stations(objects):
    bsses = [k for k, v in inventory_bss(objects).items() if v.get("BSSID") == RADIO_BSSID]
    if len(bsses) != 1:
        return {}
    return {
        k: v
        for obj in objects
        for k, v in obj.items()
        if k.startswith(bsses[0] + "STA.")
        and isinstance(v, dict)
        and v.get("MACAddress") == "02:00:00:00:02:00"
    }


async def experiment(
    label,
    *,
    active_seconds=0,
    recovery_checks=False,
    observe_station_removal=False,
    medium_loss=False,
    observe_tx_status=False,
    telemetry_gap_check=False,
    neighbor_gap_check=False,
    virtual_link=False,
):
    helpers = runpy.run_path(str(ROOT / "controller-trial.py"))
    helpers["idle"]()
    sys.path.insert(0, str(RADIO_ROOT_DIR))
    radio = runpy.run_path(str(RADIO_ROOT_DIR / "run.py"))
    discovery = runpy.run_path(str(ROOT / "discovery-trial.py"))
    directory = RADIO_ROOT / label
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    write(directory / "native-owner.json", OWNER)
    report = {
        "label": label,
        "status": "running",
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
        "native_controller": True,
        "semantic_submission": False,
        "cases": {},
        "active_seconds_requested": active_seconds,
        "sustained_operation_proven": False,
        "recovery_checks_requested": recovery_checks,
        "agent_counter_units": 0,
        "station_removal_observation_requested": observe_station_removal,
        "capture_health_required": True,
        "capture_buffer_kib": 8192,
        "reporting_policy_receipt_expected": active_seconds > 0,
        "medium_loss_requested": medium_loss,
        "tx_status_observation_requested": observe_tx_status,
        "telemetry_gap_check_requested": telemetry_gap_check,
        "forwarding_observation_requested": True,
        "neighbor_binding_requested": True,
        "neighbor_gap_check_requested": neighbor_gap_check,
        "virtual_link_requested": virtual_link,
        "netlink_capture_buffer_kib": 32768 if medium_loss else None,
    }
    write(directory / "result.json", report)
    write(
        directory / "source-hashes.json",
        {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (
                Path(__file__),
                ROOT / "controller-trial.py",
                ROOT / "node.py",
                ROOT / "prplmesh.reference.json",
                RADIO_ROOT_DIR / "manager.py",
                RADIO_ROOT_DIR / "node.py",
                RADIO_ROOT_DIR / "neighbor-observer.py",
                RADIO_ROOT_DIR / "egress-observer.py",
                *([RADIO_ROOT_DIR / "virtual-link.py"] if virtual_link else []),
                *([RADIO_ROOT_DIR / "station-events.py"] if observe_station_removal else []),
                *([RADIO_ROOT_DIR / "medium.py"] if medium_loss else []),
                *([RADIO_ROOT_DIR / "tx-status-trace.py"] if observe_tx_status else []),
                *sorted((RADIO_ROOT_DIR / "source/emosa").rglob("*.py")),
            )
        },
    )
    db = SimDatabase(directory / "database")
    admin = OvsSession(db.endpoint, monitor_columns=MONITOR)
    manager = worker = broker = None
    station_unit = None
    neighbor_unit = None
    egress_unit = None
    egress_path = RADIO_ROOT_DIR / "egress-observations" / (label + ".json")
    neighbor_path = RADIO_ROOT_DIR / "neighbor-observations" / (label + ".json")
    station_path = RADIO_ROOT_DIR / "station-events" / (label + ".jsonl")
    captures, logs = [], []
    namespace, link = "em-native-" + uuid.uuid4().hex[:8], "en" + uuid.uuid4().hex[:8]
    ns_created = link_created = False
    probe_units = []
    medium = None
    tx_trace = None
    telemetry_policy_restore = None
    shaping = None

    async def start_worker():
        return await asyncio.create_subprocess_exec(
            "ip",
            "netns",
            "exec",
            namespace,
            sys.executable,
            "-m",
            "emosa.simulation.native_onboarding",
            str(directory),
            *(["--telemetry", "--duration", str(active_seconds + 180)] if active_seconds else []),
            stdout=log("native-worker"),
            stderr=asyncio.subprocess.STDOUT,
        )

    def log(name):
        stream = (directory / (name + ".log")).open("ab")
        logs.append(stream)
        return stream

    def capture(interface, name, *filters):
        console = log(name + "-capture")
        child = subprocess.Popen(
            [
                "tcpdump",
                *([] if name == "netlink" else ["--immediate-mode"]),
                "-B",
                str(
                    report["netlink_capture_buffer_kib"]
                    if name == "netlink"
                    else report["capture_buffer_kib"]
                ),
                "-i",
                interface,
                "-U",
                "-s",
                "0",
                "-w",
                str(directory / (name + ".pcap")),
                *filters,
            ],
            stdout=console,
            stderr=console,
        )
        captures.append(child)

    async def wait_json(name, predicate, timeout=40):
        end = time.monotonic() + timeout
        path = directory / name
        while time.monotonic() < end:
            if worker is not None and worker.returncode is not None:
                raise RuntimeError("worker exited; inspect native-worker.log and session")
            if path.is_file() and predicate(value := json.loads(path.read_text())):
                return value
            await asyncio.sleep(0.1)
        raise TimeoutError("waiting for " + name)

    async def state():
        raw = await admin.snapshot()
        return raw["schema"].row(
            "Wifi_VIF_State", next(iter(raw["tables"]["Wifi_VIF_State"].values()))
        )

    try:
        write(directory / "topology.json", radio["setup"](directory))
        if virtual_link:
            shaping = runpy.run_path(str(RADIO_ROOT_DIR / "virtual-link.py"))["VirtualLink"]()
            report["virtual_link_configuration"] = shaping.start()
            write(directory / "result.json", report)
        collector = RADIO_ROOT_DIR / "neighbor-observer.py"
        radio["lxc"]("file", "push", "--quiet", str(collector), radio["AP"] + str(collector))
        neighbor_unit = "emosa-native-neighbor-" + label + ".service"
        radio["inside"](
            radio["AP"],
            "systemd-run",
            "--quiet",
            "--property=Type=exec",
            "--property=RemainAfterExit=yes",
            "--property=TimeoutStopSec=5",
            "--unit",
            neighbor_unit,
            "python3",
            str(collector),
            label,
            "--seconds",
            str(active_seconds + 180),
        )
        end = time.monotonic() + 8
        while True:
            try:
                value = json.loads(radio["inside"](radio["AP"], "cat", str(neighbor_path)))
                if value.get("running") and not value["errors"]:
                    break
            except (ValueError, subprocess.CalledProcessError):
                pass
            if time.monotonic() >= end:
                raise RuntimeError("pod backhaul discovery observer did not become ready")
            await asyncio.sleep(0.1)
        egress_collector = RADIO_ROOT_DIR / "egress-observer.py"
        radio["lxc"](
            "file", "push", "--quiet", str(egress_collector), radio["AP"] + str(egress_collector)
        )
        egress_unit = "emosa-native-egress-" + label + ".service"
        radio["inside"](
            radio["AP"],
            "systemd-run",
            "--quiet",
            "--property=Type=exec",
            "--property=RemainAfterExit=yes",
            "--property=TimeoutStopSec=5",
            "--unit",
            egress_unit,
            "python3",
            str(egress_collector),
            label,
            "--seconds",
            str(active_seconds + 180),
        )
        end = time.monotonic() + 8
        while True:
            try:
                value = json.loads(radio["inside"](radio["AP"], "cat", str(egress_path)))
                if value.get("running") and not value["errors"] and value["observation"]:
                    break
            except (ValueError, subprocess.CalledProcessError):
                pass
            if time.monotonic() >= end:
                raise RuntimeError("pod egress observer did not become ready")
            await asyncio.sleep(0.1)
        if medium_loss:
            module = runpy.run_path(str(RADIO_ROOT_DIR / "medium.py"))
            medium = module["Medium"](directory, active_seconds)
            packet_filter = medium.prepare()
            capture(module["INTERFACE"], "netlink", packet_filter)
            end = time.monotonic() + 5
            while not (directory / "netlink.pcap").exists():
                if captures[-1].poll() is not None or time.monotonic() >= end:
                    raise RuntimeError("netlink capture did not start")
                await asyncio.sleep(0.05)
            medium.start()
        if observe_tx_status:
            module = runpy.run_path(str(RADIO_ROOT_DIR / "tx-status-trace.py"))
            tx_trace = module["TxStatusTrace"](directory)
            tx_trace.start()
        if observe_station_removal:
            collector = RADIO_ROOT_DIR / "station-events.py"
            radio["lxc"]("file", "push", "--quiet", str(collector), radio["AP"] + str(collector))
            radio["inside"](
                radio["AP"],
                "python3",
                "-c",
                "import sys; from pathlib import Path; p=Path(sys.argv[1]); "
                "p.parent.mkdir(mode=0o700,exist_ok=True); p.open('x').close()",
                str(station_path),
            )
            station_unit = "emosa-native-events-" + label + ".service"
            radio["inside"](
                radio["AP"],
                "systemd-run",
                "--quiet",
                "--property=Type=exec",
                "--property=RemainAfterExit=yes",
                "--property=TimeoutStopSec=5",
                "--property=StandardOutput=append:" + str(station_path),
                "--unit",
                station_unit,
                "python3",
                str(collector),
                "--seconds",
                str(active_seconds + 180),
            )
            end = time.monotonic() + 8
            while True:
                lines = radio["inside"](radio["AP"], "cat", str(station_path)).splitlines()
                if lines and json.loads(lines[0]).get("event") == "ready":
                    break
                if time.monotonic() >= end:
                    raise RuntimeError("station-removal observer did not become ready")
                await asyncio.sleep(0.1)
        if active_seconds:
            mqtt_root = RADIO_ROOT_DIR / "mqtt-inputs/stage"
            executable = mqtt_root / "usr/sbin/mosquitto"
            configuration = directory / "mosquitto.conf"
            configuration.write_text(
                f"listener 0 {directory}/mqtt.sock\n"
                "allow_anonymous true\nuser root\npersistence false\n"
                "message_size_limit 65536\nmax_connections 4\n"
            )
            write(
                directory / "mqtt-provenance.json",
                {
                    "binary_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                    "packages": {
                        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted((RADIO_ROOT_DIR / "mqtt-inputs").glob("*.deb"))
                    },
                    "transport": "owned private Unix socket",
                    "physical_qualified": False,
                },
            )
            broker = await asyncio.create_subprocess_exec(
                str(executable),
                "-c",
                str(configuration),
                env={**os.environ, "LD_LIBRARY_PATH": str(mqtt_root / "usr/lib/x86_64-linux-gnu")},
                stdout=log("mqtt"),
                stderr=asyncio.subprocess.STDOUT,
            )
            end = time.monotonic() + 5
            while not (directory / "mqtt.sock").exists():
                if broker.returncode is not None or time.monotonic() > end:
                    raise RuntimeError("owned MQTT broker did not start")
                await asyncio.sleep(0.05)
        await db.start()
        await seed_radio_database(admin)
        results = await admin.transact(
            [
                {
                    "op": "insert",
                    "table": "AWLAN_Node",
                    "row": {
                        "serial_number": SERIAL,
                        "model": "EMOSA hwsim native onboarding fixture",
                        "firmware_version": "simulation-only",
                    },
                }
            ]
        )
        assert len(results) == 1 and "uuid" in results[0]
        write(directory / "policy.json", {"withhold": False})
        manager = await asyncio.create_subprocess_exec(
            sys.executable,
            str(RADIO_ROOT_DIR / "manager.py"),
            str(directory),
            stdout=log("manager"),
            stderr=asyncio.subprocess.STDOUT,
        )
        await radio["eventually"](state, lambda v: v.get("enabled") is True)
        # Initial provisioning remains separate from the later client cycles.
        write(directory / "policy.json", {"withhold": True})
        await wait_json("policy.json", lambda v: v["withhold"])
        capture("hwsim0", "radio")
        report["previous_controller_collection"] = stop_collect(
            directory / "previous-controller", names=(CONTROLLER,)
        )
        # Capture before native startup: the passive pod listener can receive
        # the initial Discovery before the controller's inventory is ready.
        pod_link = next(
            r
            for r in json.loads(radio["inside"](radio["AP"], "ip", "-j", "-d", "link", "show"))
            if r["ifname"] == "eth1"
        )
        forwarding_peer = next(
            r
            for r in json.loads(run("ip", "-j", "-d", "link", "show"))
            if r["ifindex"] == pod_link["link_index"]
        )
        if (
            forwarding_peer.get("master") != "em-base-bh"
            or forwarding_peer.get("link_index") != pod_link["ifindex"]
        ):
            raise RuntimeError("unexpected initial pod backhaul veth path")
        capture(forwarding_peer["ifname"], "forwarding")
        await asyncio.sleep(0.2)
        node(CONTROLLER, "prepare", "--backhaul", "wired")
        node(CONTROLLER, "hostap")
        node(CONTROLLER, "services")
        node(CONTROLLER, "agent")
        end = time.monotonic() + 55
        while True:
            try:
                before = helpers["inventory"]()
                if not discovery["transport_ready"]()[
                    "ready"
                ] or "02:00:00:ec:01:00" not in json.dumps(before):
                    raise RuntimeError("waiting for colocated gateway agent/radio")
                break
            except (RuntimeError, subprocess.CalledProcessError):
                if time.monotonic() > end:
                    raise
                await asyncio.sleep(0.25)
        write(directory / "controller-before.json", before)
        assert not inventory_bss(before)
        report["controller_policy"] = helpers["bml_policy"]()
        run("ip", "netns", "add", namespace)
        ns_created = True
        run("ip", "netns", "exec", namespace, "ip", "link", "set", "lo", "up")
        run("ip", "link", "add", link, "type", "veth", "peer", "name", link + "p")
        link_created = True
        run("ip", "link", "set", link + "p", "netns", namespace)
        for args in (
            ("set", link + "p", "name", "probe0"),
            ("set", "probe0", "address", "02:00:00:00:30:01"),
            ("set", "probe0", "up"),
        ):
            run("ip", "netns", "exec", namespace, "ip", "link", *args)
        run("ip", "link", "set", link, "master", "em-base-bh")
        run("ip", "link", "set", link, "up")
        # Read-only provenance: the proxy's control veth is not the pod's
        # forwarding interface. Whole-proxy counters cannot measure pod backhaul.
        node_links = {}
        peer_indices = set()
        for name in (CONTROLLER, radio["AP"]):
            rows = json.loads(radio["inside"](name, "ip", "-j", "-d", "-s", "link", "show"))
            node_links[name] = [r for r in rows if r["ifname"] in ("br-lan", "eth1")]
            peer_indices.update(r["link_index"] for r in node_links[name] if "link_index" in r)
        root_links = json.loads(run("ip", "-j", "-d", "-s", "link", "show"))
        pod_backhaul = next(r for r in node_links[radio["AP"]] if r["ifname"] == "eth1")
        pod_peer = next(r for r in root_links if r["ifindex"] == pod_backhaul["link_index"])
        if (
            pod_peer.get("master") != "em-base-bh"
            or pod_peer.get("link_index") != pod_backhaul["ifindex"]
        ):
            raise RuntimeError("owned pod forwarding veth is not on the expected backhaul bridge")
        if (pod_peer["ifindex"], pod_peer["ifname"]) != (
            forwarding_peer["ifindex"],
            forwarding_peer["ifname"],
        ):
            raise RuntimeError("pod backhaul veth changed during controller startup")
        write(
            directory / "neighbor-link-observations.json",
            {
                "scope": "read-only owned lab link inventory; not qualified per-neighbor metrics",
                "observed_at": time.time(),
                "containers": node_links,
                "vm_links": [
                    r
                    for r in root_links
                    if r["ifindex"] in peer_indices or r["ifname"] in (link, "em-base-bh")
                ],
                "adapter_control_interface": json.loads(
                    run(
                        "ip",
                        "netns",
                        "exec",
                        namespace,
                        "ip",
                        "-j",
                        "-d",
                        "-s",
                        "link",
                        "show",
                        "probe0",
                    )
                ),
                "measurement_source_qualified": False,
                "physical_pod_changed": False,
            },
        )
        capture(
            link,
            "ethernet",
            "ether",
            "proto",
            "0x893a",
            "and",
            "ether",
            "host",
            "02:00:00:00:30:01",
        )
        await asyncio.sleep(0.2)
        # The native candidate first advertises topology on its periodic timer.
        # Wait for the observed pod-side binding instead of racing the shorter
        # controller BSS-inventory deadline or seeding a fabricated neighbor.
        forwarding_source = ForwardingSource()
        neighbor_source = NeighborSource(
            bytes.fromhex("020000e00001"), bytes.fromhex("020000003001"), label
        )
        end = time.monotonic() + 70
        while True:
            forwarding_source.refresh(await admin.snapshot())
            neighbor_source.refresh(forwarding_source.sample)
            if neighbor_source.current() is not None:
                report["initial_observed_neighbor"] = neighbor_source.status()
                break
            if time.monotonic() >= end:
                raise TimeoutError("pod-side controller discovery did not establish a live binding")
            await asyncio.sleep(0.1)
        await db.manager_remote("unix:" + str(db.directory / "native-pod.sock"))
        worker = await start_worker()
        withheld = await wait_json("native-operation.json", lambda v: v["writes"] == 1)
        assert withheld["config_ssid"] == "emosa-controller-trial"
        assert withheld["observed_ssid"] == "emosa-radio-initial"
        assert withheld["operation"]["state"] == "CONFIG_COMMITTED"
        write(directory / "withheld-operation.json", withheld)
        report["cases"]["withheld"] = True
        write(directory / "policy.json", {"withhold": False})
        applied = await wait_json(
            "native-operation.json", lambda v: v["operation"]["state"] == "OBSERVED_APPLIED"
        )
        assert applied["operation_count"] == applied["writes"] == 1
        await wait_json(
            "native-session.json", lambda v: v["counts"].get("ap_capability_report_sent", 0) >= 1
        )
        end = time.monotonic() + 35
        while True:
            after = helpers["inventory"]()
            bsses = inventory_bss(after)
            if any(
                v.get("BSSID") == RADIO_BSSID and v.get("SSID") == "emosa-controller-trial"
                for v in bsses.values()
            ):
                break
            if time.monotonic() > end:
                write(directory / "controller-after.json", after)
                raise TimeoutError("native controller radio/BSS inventory incomplete")
            await asyncio.sleep(0.3)
        write(directory / "controller-after.json", after)
        report["controller_radio_bss"] = bsses
        if not active_seconds:
            # Reproduce the earlier bounded onboarding experiment unchanged.
            (directory / "stop-worker").touch()
            await asyncio.wait_for(worker.wait(), 10)
            assert worker.returncode == 0
        radio["connect"]("emosa-controller-trial", helpers["KEY"])
        report["cases"]["clients"] = await radio["clients"](
            directory, "onboarded", "emosa-controller-trial"
        )
        if active_seconds:
            for client, interface in zip(radio["CLIENTS"], ("eth1", "wlan0"), strict=True):
                unit = "emosa-soak-" + label
                radio["inside"](
                    client,
                    "systemd-run",
                    "--unit=" + unit,
                    "--property=Type=exec",
                    "/usr/bin/ping",
                    "-D",
                    "-n",
                    "-i",
                    "0.25",
                    "-I",
                    interface,
                    "192.0.2.1",
                )
                probe_units.append((client, unit))
            started = time.monotonic()
            samples = []
            recoveries = []
            client_outages = []
            next_disconnect = 25
            while time.monotonic() - started < active_seconds:
                if medium:
                    medium.check()
                if worker.returncode is not None:
                    raise RuntimeError("adapter exited during client activity")
                index = len(samples)
                current = json.loads((directory / "native-session.json").read_text())
                native = helpers["inventory"](depth=8)
                write(directory / f"active-inventory-{index:04d}.json", native)
                probes = await radio["clients"](
                    directory, f"active-{index:04d}", "emosa-controller-trial"
                )
                sample = {
                    "elapsed": time.monotonic() - started,
                    "adapter_pid": worker.pid,
                    "session": current,
                    "clients": probes,
                    "controller_station_present": bool(inventory_stations(native)),
                    "process_status": Path(f"/proc/{worker.pid}/status").read_text(),
                    "open_descriptors": len(list(Path(f"/proc/{worker.pid}/fd").iterdir())),
                }
                samples.append(sample)
                write(directory / "active-samples.json", samples)
                if telemetry_gap_check and index == 0:
                    fresh = await wait_json(
                        "native-session.json",
                        lambda v: (
                            v["report_source"]["inventory_complete"]
                            and v["telemetry"]["station_count"] == 1
                        ),
                    )
                    gap = {
                        "started_at": time.time(),
                        "session_before": fresh,
                        "operation_before": json.loads(
                            (directory / "native-operation.json").read_text()
                        ),
                    }
                    telemetry_policy_restore = json.loads((directory / "policy.json").read_text())
                    write(
                        directory / "policy.json",
                        {**telemetry_policy_restore, "telemetry_withheld": True},
                    )
                    write(directory / "telemetry-gap-check.json", gap)
                    await asyncio.sleep(4)
                    gap["session_withheld"] = await wait_json(
                        "native-session.json",
                        lambda v: v["telemetry"]["timestamp_ms"] is None,
                    )
                    gap["clients_withheld"] = await radio["clients"](
                        directory, "telemetry-withheld", "emosa-controller-trial"
                    )
                    write(directory / "policy.json", telemetry_policy_restore)
                    telemetry_policy_restore = None
                    gap["restored_at"] = time.time()
                    gap["session_after"] = await wait_json(
                        "native-session.json",
                        lambda v: (
                            v["report_source"]["inventory_complete"]
                            and v["telemetry"]["station_count"] == 1
                        ),
                    )
                    gap["operation_after"] = json.loads(
                        (directory / "native-operation.json").read_text()
                    )
                    gap["completed_at"] = time.time()
                    write(directory / "telemetry-gap-check.json", gap)
                    for session in (gap["session_withheld"], gap["session_after"]):
                        assert session["report_source"]["available"]
                        assert (
                            session["report_source"]["context_token"]
                            == fresh["report_source"]["context_token"]
                        )
                        assert session["counts"].get("search_sent") == 1
                        assert session["counts"].get("client_leave_notification", 0) == 0
                    assert not gap["session_withheld"]["report_source"]["inventory_complete"]
                    assert gap["session_withheld"]["report_source"]["operating_radio_count"] == 0
                    assert gap["operation_after"] == gap["operation_before"]
                    report["telemetry_gap_check_completed"] = True
                if neighbor_gap_check and index == 1:
                    fresh = await wait_json(
                        "native-session.json",
                        lambda v: (
                            v["observed_neighbor"]["available"]
                            and v["report_source"]["inventory_complete"]
                        ),
                    )
                    gap = {
                        "started_at": time.time(),
                        "session_before": fresh,
                        "operation_before": json.loads(
                            (directory / "native-operation.json").read_text()
                        ),
                    }
                    radio["inside"](
                        radio["AP"], "systemctl", "kill", "--signal=SIGSTOP", neighbor_unit
                    )
                    try:
                        await asyncio.sleep(4)
                        gap["session_withheld"] = await wait_json(
                            "native-session.json", lambda v: not v["observed_neighbor"]["available"]
                        )
                        gap["clients_withheld"] = await radio["clients"](
                            directory, "neighbor-withheld", "emosa-controller-trial"
                        )
                    finally:
                        radio["inside"](
                            radio["AP"], "systemctl", "kill", "--signal=SIGCONT", neighbor_unit
                        )
                    gap["restored_at"] = time.time()
                    gap["session_after"] = await wait_json(
                        "native-session.json",
                        lambda v: (
                            v["observed_neighbor"]["available"]
                            and v["report_source"]["inventory_complete"]
                        ),
                    )
                    gap["operation_after"] = json.loads(
                        (directory / "native-operation.json").read_text()
                    )
                    gap["completed_at"] = time.time()
                    write(directory / "neighbor-gap-check.json", gap)
                    for key in ("session_withheld", "session_after"):
                        value = gap[key]
                        assert value["report_source"]["available"]
                        assert (
                            value["report_source"]["context_token"]
                            == fresh["report_source"]["context_token"]
                        )
                        assert value["counts"].get("search_sent") == 1
                        assert value["counts"].get("client_leave_notification", 0) == 0
                    assert not gap["session_withheld"]["report_source"]["inventory_complete"]
                    assert gap["session_withheld"]["report_source"]["operating_radio_count"] == 1
                    assert gap["operation_after"] == gap["operation_before"]
                    report["neighbor_gap_check_completed"] = True
                if (
                    recovery_checks
                    and len(recoveries) < 2
                    and (time.monotonic() - started >= active_seconds * (len(recoveries) + 1) / 3)
                ):
                    fault_index = len(recoveries)
                    before = json.loads((directory / "native-operation.json").read_text())
                    previous = json.loads((directory / "native-session.json").read_text())
                    fault = {
                        "kind": "pod_connection_loss" if fault_index == 0 else "adapter_sigkill",
                        "elapsed_start": time.monotonic() - started,
                        "started_at": time.time(),
                        "old_pid": worker.pid,
                        "before": before,
                        "session_before": previous,
                        "manual_intervention": False,
                    }
                    recoveries.append(fault)
                    write(directory / "recovery-checks.json", recoveries)
                    if fault_index == 0:
                        await db.manager_remote(
                            "unix:" + str(db.directory / "native-pod.sock"), connect=False
                        )
                        lost = await wait_json(
                            "native-session.json", lambda v: v["state"] == "recovering"
                        )
                        fault["session_unavailable"] = lost
                        await asyncio.sleep(4)
                        await db.manager_remote("unix:" + str(db.directory / "native-pod.sock"))
                    else:
                        worker.kill()
                        await asyncio.wait_for(worker.wait(), 10)
                        fault["exit_code"] = worker.returncode
                        assert worker.returncode == -signal.SIGKILL
                        worker = await start_worker()
                    fault["restored_at"] = time.time()
                    recovered = await wait_json(
                        "native-operation.json",
                        lambda v, before=before: (
                            v["operation_count"] > before["operation_count"]
                            and v["operations"][-1]["state"] == "OBSERVED_APPLIED"
                        ),
                        timeout=60,
                    )
                    assert (
                        recovered["journal_write_attempts"] == before["journal_write_attempts"] == 1
                    )
                    assert recovered["operations"][-1]["attempts"] == 0
                    assert recovered["operations"][-1]["application_evidence"]["observed_noop"]
                    assert recovered["operations"][-1]["receipt"]["m1_sha256"] not in {
                        op["receipt"]["m1_sha256"] for op in before["operations"]
                    }
                    fault.update(
                        new_pid=worker.pid,
                        after=recovered,
                        session_after=json.loads((directory / "native-session.json").read_text()),
                        recovered_at=time.time(),
                        elapsed_end=time.monotonic() - started,
                        clients=await radio["clients"](
                            directory, f"recovery-{fault_index}", "emosa-controller-trial"
                        ),
                    )
                    write(directory / "recovery-checks.json", recoveries)
                if time.monotonic() - started >= next_disconnect:
                    next_disconnect += 25
                    client_outage = {"started_at": time.time()}
                    client_outages.append(client_outage)
                    write(directory / "client-outages.json", client_outages)
                    radio["inside"](
                        radio["CLIENTS"][1],
                        "systemctl",
                        "stop",
                        "emosa-radio-manager-client.service",
                    )
                    await asyncio.sleep(4)
                    detached = helpers["inventory"](depth=8)
                    write(directory / f"detached-inventory-{index:04d}.json", detached)
                    write(
                        directory / f"detached-session-{index:04d}.json",
                        json.loads((directory / "native-session.json").read_text()),
                    )
                    radio["connect"]("emosa-controller-trial", helpers["KEY"])
                    client_outage["restored_at"] = time.time()
                    write(directory / "client-outages.json", client_outages)
                await asyncio.sleep(3)
            report["active_observed_seconds"] = time.monotonic() - started
            report["active_samples"] = len(samples)
            report["recovery_checks_completed"] = len(recoveries)
            report["active_controller_station_seen"] = any(
                s["controller_station_present"] for s in samples
            )
            # This is a pilot, not the full soak/recovery acceptance verdict.
            (directory / "stop-worker").touch()
            await asyncio.wait_for(worker.wait(), 10)
            assert worker.returncode == 0
        report.update(
            status="observed_pending_capture_review",
            initial_operation=applied,
            operation=json.loads((directory / "native-operation.json").read_text()),
            controller_onboarding_proven=False,
        )
    except BaseException as exc:
        report.update(status="failed", error=type(exc).__name__, detail=str(exc))
        if isinstance(exc, subprocess.CalledProcessError):
            report["command_stderr"] = exc.stderr
        raise
    finally:
        errors = []
        if telemetry_policy_restore is not None:
            write(directory / "policy.json", telemetry_policy_restore)
        for client, unit in probe_units:
            try:
                radio["inside"](
                    client, "systemctl", "kill", "--signal=SIGINT", "--kill-whom=main", unit
                )
                # A successful transient unit can be garbage-collected as soon
                # as ping exits. A subsequent stop then returns "not loaded".
                # Check that the process is gone rather than treating that race
                # as a cleanup failure or suppressing all stop errors.
                for _ in range(20):
                    state = radio["inside"](
                        client, "systemctl", "show", unit, "-p", "MainPID", "-p", "ActiveState"
                    ).splitlines()
                    if "MainPID=0" in state and "ActiveState=inactive" in state:
                        break
                    await asyncio.sleep(0.1)
                else:
                    radio["inside"](client, "systemctl", "stop", unit)
                    raise RuntimeError("continuous probe did not terminate after SIGINT")
            except Exception as exc:
                errors.append("continuous_probe_cleanup:" + str(exc))
            try:
                (directory / (client + "-continuous-ping.log")).write_text(
                    radio["inside"](client, "journalctl", "--no-pager", "-o", "cat", "-u", unit)
                )
            except Exception as exc:
                errors.append("continuous_probe_collection:" + str(exc))
        for child in (worker, manager, broker):
            if child and child.returncode is None:
                try:
                    child.terminate()
                    await asyncio.wait_for(child.wait(), 35)
                except Exception as exc:
                    child.kill()
                    await child.wait()
                    errors.append(type(exc).__name__)
        if station_unit:
            try:
                radio["inside"](radio["AP"], "systemctl", "stop", station_unit)
                observed = radio["inside"](radio["AP"], "cat", str(station_path))
                (directory / "station-events.jsonl").write_text(observed)
                records = [json.loads(line) for line in observed.splitlines()]
                if not records or records[-1].get("event") != "finished" or records[-1]["errors"]:
                    raise RuntimeError("station observation did not complete cleanly")
                report["station_removal_observation"] = records[-1]
            except Exception as exc:
                errors.append("station_observer:" + str(exc))
        if egress_unit:
            try:
                radio["inside"](radio["AP"], "systemctl", "stop", egress_unit)
                observed = json.loads(radio["inside"](radio["AP"], "cat", str(egress_path)))
                write(directory / "egress-observer-final.json", observed)
                (directory / "egress-observations.jsonl").write_text(
                    radio["inside"](radio["AP"], "cat", str(egress_path.with_suffix(".jsonl")))
                )
                if observed["running"] or observed["errors"]:
                    raise RuntimeError("egress observer did not complete cleanly")
            except Exception as exc:
                errors.append("egress_observer:" + str(exc))
        if neighbor_unit:
            try:
                radio["inside"](radio["AP"], "systemctl", "stop", neighbor_unit)
                observed = json.loads(radio["inside"](radio["AP"], "cat", str(neighbor_path)))
                write(directory / "neighbor-observer-final.json", observed)
                if observed["running"] or observed["errors"]:
                    raise RuntimeError("neighbor observer did not complete cleanly")
            except Exception as exc:
                errors.append("neighbor_observer:" + str(exc))
        if medium:
            try:
                medium.stop()
                await asyncio.sleep(2)
            except Exception as exc:
                errors.append("medium_stop:" + str(exc))
        if tx_trace:
            try:
                tx_trace.stop()
            except Exception as exc:
                errors.append("tx_status_collection:" + str(exc))
            finally:
                try:
                    tx_trace.remove()
                except Exception as exc:
                    errors.append("tx_status_cleanup:" + str(exc))
        for child in captures:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:
                    await asyncio.to_thread(child.wait, 10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    await asyncio.to_thread(child.wait)
                    errors.append("capture_timeout")
            if child.returncode != 0:
                errors.append("capture_failed")
        if medium:
            try:
                medium.remove()
            except Exception as exc:
                errors.append("medium_remove:" + str(exc))
        if shaping:
            try:
                shaping.close()
            except Exception as exc:
                errors.append("virtual_link_cleanup:" + str(exc))
            report["virtual_link_restoration"] = {
                "before": shaping.before,
                "after": shaping.after,
                "restored": shaping.before is not None and shaping.before == shaping.after,
            }
        for name in ("radio", "ethernet", "forwarding", *(["netlink"] if medium_loss else [])):
            path = directory / (name + "-capture.log")
            if path.exists():
                drops = re.findall(r"^(\d+) packets dropped by kernel$", path.read_text(), re.M)
                if drops != ["0"]:
                    errors.append(name + "_capture_loss_or_missing_statistics")
        await admin.close()
        await db.stop()
        for container in radio["NODES"]:
            for unit in ("ap", "client", "data"):
                radio["inside"](
                    container,
                    "systemctl",
                    "stop",
                    f"emosa-radio-manager-{unit}.service",
                    check=False,
                )
        try:
            report["native_shutdown"] = stop_collect(
                directory / "native-shutdown", names=(CONTROLLER,)
            )
        except Exception as exc:
            errors.append("native_shutdown:" + type(exc).__name__)
        if ns_created:
            run("ip", "netns", "del", namespace)
        if link_created:
            subprocess.run(["ip", "link", "del", link], capture_output=True, check=False)
        for stream in logs:
            stream.close()
        report["cleanup_errors"] = errors
        write(directory / "result.json", report)
    assert not errors
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--active-seconds",
        type=int,
        default=0,
        help="Keep adapter active for a measured client-traffic pilot",
    )
    parser.add_argument(
        "--recovery-checks",
        action="store_true",
        help="Interrupt pod connection and SIGKILL/restart adapter",
    )
    parser.add_argument(
        "--observe-station-removal",
        action="store_true",
        help="Read kernel final-station events for measurement qualification; no counter mapping",
    )
    parser.add_argument(
        "--medium-loss",
        action="store_true",
        help="Optional unqualified wmediumd counter experiment with 20%% AP-to-client loss",
    )
    parser.add_argument(
        "--observe-tx-status",
        action="store_true",
        help="Passively trace exact-kernel TX-status flags during the owned medium-loss experiment",
    )
    parser.add_argument(
        "--telemetry-gap-check",
        action="store_true",
        help="Withhold station telemetry while OVSDB and client traffic stay active",
    )
    parser.add_argument(
        "--neighbor-gap-check",
        action="store_true",
        help="Pause passive neighbor observation while OVSDB and traffic stay active",
    )
    parser.add_argument(
        "--virtual-link",
        action="store_true",
        help="Temporarily shape owned pod egress for simulated service-work observation",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", args.label):
        parser.error("use a new label of 1–24 lowercase letters, digits or hyphens")
    if args.build.resolve().parent != ROOT or not args.build.name.startswith("candidate-"):
        parser.error("stage a separate candidate directory directly under /opt/emosa-baseline")
    if args.active_seconds and not 30 <= args.active_seconds <= 3600:
        parser.error("active-seconds must be zero or 30–3600")
    if args.recovery_checks and args.active_seconds < 150:
        parser.error("recovery checks require at least 150 active seconds")
    if args.observe_station_removal and not args.active_seconds:
        parser.error("station-removal observation requires active client cycles")
    if args.medium_loss and not args.observe_station_removal:
        parser.error("medium loss requires station-removal observation")
    if args.observe_tx_status and not args.medium_loss:
        parser.error("TX-status observation requires the medium-loss experiment")
    if args.telemetry_gap_check and args.active_seconds < 150:
        parser.error("telemetry gap check requires at least 150 active seconds")
    if args.neighbor_gap_check and args.active_seconds < 150:
        parser.error("neighbor gap check requires at least 150 active seconds")
    os.umask(0o077)
    wrapper = runpy.run_path(str(ROOT / "compatibility/controller-candidate.py"))
    with (RADIO_ROOT_DIR / "run.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        try:
            wrapper["trial"](
                args.build,
                args.label,
                experiment=lambda label: asyncio.run(
                    experiment(
                        label,
                        active_seconds=args.active_seconds,
                        recovery_checks=args.recovery_checks,
                        observe_station_removal=args.observe_station_removal,
                        medium_loss=args.medium_loss,
                        observe_tx_status=args.observe_tx_status,
                        telemetry_gap_check=args.telemetry_gap_check,
                        neighbor_gap_check=args.neighbor_gap_check,
                        virtual_link=args.virtual_link,
                    )
                ),
            )
        finally:
            signal.signal(signal.SIGTERM, previous)
