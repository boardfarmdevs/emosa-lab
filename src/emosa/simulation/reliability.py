"""Bounded TLS fleet and recovery experiment using the actual adapter service.

Synthetic databases and independent State publishers; no radio or EasyMesh wire.
The report contains observations, not private trust files or database credentials.
"""

import argparse
import asyncio
import contextlib
import json
import math
import os
import secrets
import time
from pathlib import Path

from emosa.config import load
from emosa.evaluation.evidence import write_json
from emosa.evaluation.service import AdapterProcess
from emosa.opensync.session import OvsSession
from emosa.secrets import SecretStore
from emosa.simulation.connecting_pod import configuration, poll
from emosa.simulation.database import SimDatabase, SimManager
from emosa.simulation.tls import create_pki, trust_config, unused_port


def resources(pid):
    """Linux process observations; CPU seconds are cumulative for this PID."""
    root = Path(f"/proc/{pid}")
    status = dict(line.split(":", 1) for line in (root / "status").read_text().splitlines())
    # Ignore the parenthesized command, which can contain spaces.
    fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
    return {
        "rss_kib": int(status["VmRSS"].split()[0]),
        "threads": int(status["Threads"]),
        "file_descriptors": len(list((root / "fd").iterdir())),
        "cpu_seconds": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
    }


def distribution(values):
    ordered = sorted(values)
    return {
        "samples": len(values),
        **{
            name: ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]
            for name, quantile in (("p50", 0.5), ("p95", 0.95), ("max", 1))
        },
    }


async def run(directory, *, pods=4, cycles=1, interval=0):
    if pods not in {2, 4, 8, 16, 32} or not 0 <= cycles <= 100 or not 0 <= interval <= 60:
        raise ValueError("pods must be 2/4/8/16/32; cycles 0..100; interval 0..60 seconds")
    directory = directory.resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    started = time.monotonic()
    pki = create_pki(directory / "secrets", pods)
    # Fail closed on a bind race; avoid duplicate ephemeral selections in this run.
    ports = set()
    while len(ports) < pods:
        ports.add(unused_port())
    ports = sorted(ports)
    remotes = [f"ssl:127.0.0.1:{port}" for port in ports]
    databases = [SimDatabase(tls_files=pki["pods"][i]) for i in range(pods)]
    managers = [SimManager(db.endpoint) for db in databases]
    admins = [OvsSession(db.endpoint) for db in databases]
    config = configuration(directory, remotes[0])
    template = config["pods"][0]
    config.update(
        request_source="tls-reliability-experiment",
        limits={"local_clients": 64, "local_frame_bytes": 1048576, "ovsdb_requests": 16},
    )
    config["pods"] = [
        {
            **template,
            "pod_id": f"pod-{i + 1}",
            "endpoint": f"pssl:{ports[i]}:127.0.0.1",
            "tls": trust_config(pki, i),
            "virtual_agent": {
                "al_mac": f"02:00:00:00:40:{i + 1:02x}",
                "expected_serial": f"EMOSA-TLS-SIM-{i + 1:03}",
            },
        }
        for i in range(pods)
    ]
    config_path = directory / "adapter.json"
    write_json(config_path, config)
    load("config", config_path)
    vault = SecretStore(directory / "secrets")
    keys = [secrets.token_urlsafe(24) for _ in range(pods)]
    for i, key in enumerate(keys, 1):
        vault.write_simulated(f"pod-{i}-key", key)
    service = AdapterProcess(config_path, config["socket_path"], directory / "adapter.log")
    report = {
        "schema_version": 1,
        "scope": "real adapter service, synthetic pod-initiated mutual TLS OVSDB fleet",
        "pods": pods,
        "cycles_requested": cycles,
        "interval_seconds": interval,
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
        "radio_behavior_proven": False,
        "production_capacity_proven": False,
        "passed": False,
        "checks": [],
        "cycles": [],
        "resource_samples": [],
    }
    timings = []
    sampling = True

    async def sample():
        while sampling:
            if service.process and service.process.returncode is None:
                # A crash can race a sample; never invent a zero measurement.
                with contextlib.suppress(FileNotFoundError, ProcessLookupError, KeyError):
                    report["resource_samples"].append(
                        {
                            "elapsed_seconds": time.monotonic() - started,
                            "pid": service.process.pid,
                            **resources(service.process.pid),
                        }
                    )
            await asyncio.sleep(0.5)

    async def ready(first="ready"):
        return await service.wait_for(
            "agents",
            lambda r: [a["state"] for a in r["agents"]] == [first] + ["ready"] * (pods - 1),
            timeout=45,
        )

    async def submit(index, name, deadline=60):
        return await service.call(
            "component.submit",
            {
                "intent": {
                    "pod_id": f"pod-{index + 1}",
                    "radio_id": "radio-1",
                    "bss_id": "bss-1",
                    "ssid": f"fleet-{index + 1}-{name}",
                    "secret_ref": f"pod-{index + 1}-key",
                },
                "idempotency_key": name,
                "run_id": f"fleet-pod-{index + 1}",
                "apply_seconds": deadline,
            },
        )

    async def state(op, wanted):
        return await service.wait_for(
            "operation.show",
            lambda r: r["state"] == wanted,
            {"operation_id": op["operation_id"]},
            timeout=45,
        )

    async def apply(index, op):
        async def cycle():
            await managers[index].command("apply")
            return await service.call("operation.show", {"operation_id": op["operation_id"]})

        result = await poll(cycle, lambda r: r["state"] == "OBSERVED_APPLIED", timeout=45)
        assert len(result["attempts"]) == 1
        return result

    async def change(index, name):
        before = time.monotonic()
        op = await submit(index, name)
        await apply(index, op)
        timings.append(time.monotonic() - before)
        replay = await submit(index, name)
        assert replay["operation_id"] == op["operation_id"] and len(replay["attempts"]) == 1
        rows = (await admins[index].snapshot())["tables"]["Wifi_VIF_Config"]
        row = next(iter(rows.values()))
        assert row["ssid"] == f"fleet-{index + 1}-{name}"
        assert dict(row["wpa_psks"][1])["key"] == keys[index]
        return op["operation_id"]

    async def identity(serial):
        assert await admins[0].transact(
            [{"op": "update", "table": "AWLAN_Node", "where": [], "row": {"serial_number": serial}}]
        ) == [{"count": 1}]

    sampler = asyncio.create_task(sample())
    try:
        # Setup sequentially bounds the process burst; workload operations are concurrent.
        for i, db in enumerate(databases):
            await db.start()
            await db.seed(serial_number=config["pods"][i]["virtual_agent"]["expected_serial"])
            await managers[i].start()
        view = await service.start()
        assert all(a["state"] == "pending" for a in view["agents"])
        for db, remote in zip(databases, remotes, strict=True):
            await db.manager_remote(remote)
        connected = await ready()
        report["connection_seconds"] = time.monotonic() - started
        report["checks"].append("all distinct TLS certificates and serial bindings admitted")
        ids = await asyncio.gather(*(change(i, "initial") for i in range(pods)))
        assert len(set(ids)) == pods
        report["checks"].append("concurrent per-pod writes, database keys and idempotent replay")

        # A trusted certificate alone must not bypass the explicit database identity binding.
        await identity("WRONG-SYNTHETIC-SERIAL")
        await ready("unavailable")
        rejected = await state(await submit(0, "wrong-serial"), "REJECTED")
        assert rejected["reason"] == "NOT_READY" and not rejected["attempts"]
        await change(1, "peer-wrong-serial")
        await identity(config["pods"][0]["virtual_agent"]["expected_serial"])
        await ready()
        report["checks"].append(
            "trusted certificate with wrong database serial rejected; peer progresses"
        )

        for number in range(cycles):
            before = time.monotonic()
            prefix = f"c{number}"
            waiting = await submit(0, prefix + "-withheld")
            await state(waiting, "CONFIG_COMMITTED")
            await change(1, prefix + "-peer-wait")
            await service.stop(crash=True)
            await service.start()
            await ready()
            await state(waiting, "CONFIG_COMMITTED")
            await apply(0, waiting)

            generation = (await ready())["agents"][0]["inventory"]["generation"]
            await databases[0].manager_remote(remotes[0], connect=False)
            await ready("unavailable")
            await change(1, prefix + "-peer-off")
            await databases[0].manager_remote(remotes[0])
            recovered = await ready()
            assert recovered["agents"][0]["inventory"]["generation"] > generation

            generation = recovered["agents"][0]["inventory"]["generation"]
            await managers[0].close()
            await databases[0].stop()
            await ready("unavailable")
            await change(1, prefix + "-peer-db")
            await databases[0].start()
            await databases[0].manager_remote(remotes[0])
            assert (await ready())["agents"][0]["inventory"]["generation"] > generation
            # These fixture observers are idle while the database is down. Start
            # fresh fixture sessions after adapter recovery is independently seen.
            # Otherwise their old reconnect backoff can exceed a single read's
            # budget and be mistaken for failure of the continuously running adapter.
            await admins[0].close()
            admins[0] = OvsSession(databases[0].endpoint)
            managers[0] = SimManager(databases[0].endpoint)
            await managers[0].start()
            await change(0, prefix + "-db-restored")

            late = await submit(0, prefix + "-late", deadline=1)
            await state(late, "CONFIG_COMMITTED")
            await state(late, "TIMED_OUT")
            await managers[0].command("apply")
            resolved = await service.wait_for(
                "operation.show",
                lambda r: r["late_resolution"] == "applied_after_deadline",
                {"operation_id": late["operation_id"]},
            )
            assert resolved["state"] == "TIMED_OUT" and len(resolved["attempts"]) == 1
            report["cycles"].append(
                {
                    "cycle": number + 1,
                    "seconds": time.monotonic() - before,
                    "crash_restart": True,
                    "disconnect_reconnect": True,
                    "database_restart": True,
                    "late_timeout_preserved": True,
                }
            )
            await asyncio.sleep(interval)

        # Ownership loss remains latched; do not clear the journal to manufacture recovery.
        pending = await submit(0, "writer-conflict")
        await state(pending, "CONFIG_COMMITTED")
        await managers[0].command("compete")
        await state(pending, "OWNERSHIP_CONFLICT")
        refused = await state(await submit(0, "after-conflict"), "REJECTED")
        assert refused["reason"] == "OWNERSHIP_CONFLICT" and not refused["attempts"]
        await change(1, "peer-conflict")
        await service.stop(crash=True)
        await service.start()
        await ready()
        refused = await state(await submit(0, "after-conflict-restart"), "REJECTED")
        assert refused["reason"] == "OWNERSHIP_CONFLICT" and not refused["attempts"]
        report["checks"].append(
            "competing writer latches ownership across service restart; peer progresses"
        )
        final = await ready()
        assert [a["al_mac"] for a in final["agents"]] == [a["al_mac"] for a in connected["agents"]]
        operation_sets = []
        for i in range(pods):
            events, offset = [], 0
            while True:
                page = (
                    await service.call(
                        "events", {"run_id": f"fleet-pod-{i + 1}", "after": offset, "limit": 100}
                    )
                )["events"]
                if not page:
                    break
                events.extend(page)
                offset = page[-1]["sequence"]
            operation_sets.append({e["operation_id"] for e in events if e["operation_id"]})
        assert all(operation_sets)
        assert sum(map(len, operation_sets)) == len(set().union(*operation_sets))
        report["checks"].append("stable virtual AL identities and disjoint operation histories")
        report["operation_latency_seconds"] = distribution(timings)
        report["passed"] = True
    except BaseException as exc:
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        sampling = False
        await sampler
        await service.stop()
        report["service_lifecycle"] = service.lifecycle
        results = await asyncio.gather(
            *(m.close() for m in managers), *(a.close() for a in admins), return_exceptions=True
        )
        results += await asyncio.gather(*(db.close() for db in databases), return_exceptions=True)
        report["cleanup_passed"] = not any(isinstance(r, BaseException) for r in results)
        report["passed"] = report["passed"] and report["cleanup_passed"]
        report["elapsed_seconds"] = time.monotonic() - started
        samples = report["resource_samples"]
        if samples:
            report["adapter_peaks"] = {
                key: max(s[key] for s in samples)
                for key in ("rss_kib", "threads", "file_descriptors")
            }
        write_json(directory / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--pods", type=int, choices=(2, 4, 8, 16, 32), default=4)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--interval", type=float, default=0)
    args = parser.parse_args()
    os.umask(0o077)
    report = asyncio.run(
        run(args.directory, pods=args.pods, cycles=args.cycles, interval=args.interval)
    )
    print(json.dumps({"passed": report["passed"], "report": str(args.directory / "report.json")}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
