#!/usr/bin/env python3
"""Bounded R0 probe. Run with the locked EMOSA Python in the native container.

The probe subclass labels observations and recognizes the pinned OWM's explicit
RSN-only PSK representation. Transactions, guards and reconciliation use the
normal EMOSA implementation. The application native backend remains gated.
"""

import asyncio
import json
import os
import resource
import socket
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from emosa.model import Intent, State
from emosa.opensync.mapping import OpenSyncBackend, check_results
from emosa.opensync.schema import TABLES
from emosa.opensync.session import OvsSession
from emosa.reconcile import Engine
from emosa.secrets import SecretStore
from emosa.store import Store
from emosa_lab.simulation.database import SimDatabase


class NativeProbe(OpenSyncBackend):
    mode = "opensync-native"

    def _values(self, row):
        # Pinned ow_ovsdb_vifstate_fill_akm emits wpa-psk for the PSK AKM;
        # protocol/cipher selection comes from the separate boolean columns.
        # Require explicit false values for every alternative cipher.
        if row.get("wpa_key_mgmt") == ["wpa-psk"] and all(
            row.get(column) is False
            for column in (
                "wpa_pairwise_tkip",
                "wpa_pairwise_ccmp",
                "rsn_pairwise_tkip",
                "rsn_pairwise_ccmp256",
                "rsn_pairwise_gcmp",
                "rsn_pairwise_gcmp256",
            )
        ):
            row = {**row, "wpa_key_mgmt": ["wpa2-psk"]}
        return super()._values(row)

    async def snapshot(self):
        snapshot = await super().snapshot()
        snapshot.observed.provenance = "native-OWM:Wifi_VIF_State:simulated-OSW-dummy-feedback"
        return snapshot


async def eventually(function, predicate, timeout=15):
    end = time.monotonic() + timeout
    while True:
        result = await function()
        if predicate(result):
            return result
        if time.monotonic() >= end:
            raise AssertionError("Native path did not converge before the probe deadline")
        await asyncio.sleep(0.05)


async def main():
    if (
        socket.gethostname() != "opensync-native-r0"
        or subprocess.check_output(["systemd-detect-virt", "--container"], text=True).strip()
        != "lxc"
    ):
        raise SystemExit("Run only in the isolated opensync-native-r0 container")
    root = Path(os.environ.get("EMOSA_NATIVE_ROOT", "/opt/native"))
    if root not in (Path("/opt/native"), Path("/opt/native/reproduction")):
        raise SystemExit("Unexpected native build root")
    build = root / "device/core/work/native-gcc-ubuntu24.04-x86_64"
    evidence = Path(tempfile.mkdtemp(prefix="path-", dir=root))
    print(evidence, flush=True)
    hold = evidence / "hold-driver-feedback"
    os.environ["EMOSA_OVS_BIN"] = str(root / "ovsdb")
    database = SimDatabase(evidence / "database")
    vault = SecretStore(evidence / "synthetic-secrets")
    vault.write_simulated("test-key", "native-simulated-new-key")
    store = Store(evidence / "journal")
    columns = {table: list(names) for table, names in TABLES.items()}
    columns["Wifi_VIF_State"] += [
        "rsn_pairwise_tkip",
        "rsn_pairwise_ccmp256",
        "rsn_pairwise_gcmp",
        "rsn_pairwise_gcmp256",
    ]
    backend = NativeProbe("pod-1", OvsSession(database.endpoint, monitor_columns=columns), vault)
    manager = None
    log = (evidence / "manager.log").open("w")
    result = {
        "schema_version": 1,
        "backend_mode": "opensync-native",
        "initiating_interface": "semantic",
        "interoperability": "not_evaluated",
        "physical_pod": "not_connected",
        "probe_passed": False,
    }
    try:
        await database.start()
        # Config seed only. No process except OWM creates or updates State tables.
        ap = {
            "if_name": "lab-ap",
            "mode": "ap",
            "enabled": True,
            "ssid": "initial-network",
            "wpa": True,
            "wpa_key_mgmt": ["set", ["wpa2-psk"]],
            "wpa_psks": ["map", [["key", "initial-simulation-key"]]],
            "rsn_pairwise_ccmp": True,
            "wpa_pairwise_tkip": False,
            "wpa_pairwise_ccmp": False,
            "security": ["map", []],
        }
        radio = {
            "if_name": "lab-radio",
            "enabled": True,
            "freq_band": "5G",
            "channel": 36,
            "ht_mode": "HT20",
            "hw_mode": "11a",
            "tx_chainmask": 1,
            "vif_configs": ["set", [["named-uuid", "vif"]]],
        }
        response = await backend.session.transact(
            [
                {"op": "insert", "table": "Wifi_VIF_Config", "uuid-name": "vif", "row": ap},
                {"op": "insert", "table": "Wifi_Radio_Config", "row": radio},
            ]
        )
        check_results(response, [None, None])
        env = {
            **os.environ,
            "LD_LIBRARY_PATH": str(build / "lib"),
            "LD_PRELOAD": str(root / "driver.so"),
            "PLUME_OVSDB_SOCK_PATH": str(evidence / "database/db.sock"),
            "OSW_DRV_TARGET_DISABLED": "1",
            "OSW_DRV_NL80211_DISABLE": "1",
            "EMOSA_NATIVE_HOLD_FILE": str(hold),
            "OW_CORE_LOG_SEVERITY": "info",
        }
        manager = subprocess.Popen(
            [str(build / "bin/owm")], env=env, cwd=evidence, stdout=log, stderr=log
        )
        baseline = await eventually(backend.snapshot, lambda snap: snap is not None and snap.ready)
        result["baseline"] = asdict(baseline)
        raw = await backend.session.snapshot()
        state_row = next(iter(raw["tables"]["Wifi_VIF_State"].values()))
        decoded = raw["schema"].row("Wifi_VIF_State", state_row)
        assert backend._values(decoded)["security_mode"] == "wpa2-psk"
        checked = 0
        for column in (
            "wpa_pairwise_tkip",
            "wpa_pairwise_ccmp",
            "rsn_pairwise_tkip",
            "rsn_pairwise_ccmp256",
            "rsn_pairwise_gcmp",
            "rsn_pairwise_gcmp256",
        ):
            assert backend._values({**decoded, column: True})["security_mode"] is None
            incomplete = {key: value for key, value in decoded.items() if key != column}
            assert backend._values(incomplete)["security_mode"] is None
            checked += 2
        result["mixed_or_unobserved_cipher_rejections"] = checked
        engine = Engine(store, vault, {"pod-1": backend})

        async def operation(name, timeout):
            intent = Intent("pod-1", "radio-1", "bss-1", name, "test-key")
            op = engine.request(
                intent,
                source="native-r0-semantic-probe",
                key=name,
                run_id=evidence.name,
                deadline=timeout,
            )
            submitted = await engine.execute(op.operation_id)
            assert submitted.state == State.CONFIG_COMMITTED, submitted.to_dict()
            return op

        async def refresh(op):
            await engine.reconcile("pod-1")
            return store.get(op.operation_id)

        good = await operation("native-applied", 20)
        await eventually(lambda: refresh(good), lambda op: op.state == State.OBSERVED_APPLIED)
        result["applied"] = store.get(good.operation_id).to_dict()
        hold.touch()
        withheld = await operation("native-withheld", 2)
        await eventually(lambda: refresh(withheld), lambda op: op.state == State.TIMED_OUT)
        result["withheld"] = store.get(withheld.operation_id).to_dict()
        snap = await backend.snapshot()
        assert snap.observed.values["ssid"] == "native-applied"
        result["withheld_observation"] = asdict(snap.observed)
        hold.unlink()
        await eventually(
            backend.snapshot, lambda snap: snap.observed.values["ssid"] == "native-withheld"
        )
        result["released_observation"] = asdict((await backend.snapshot()).observed)
        generation = (await backend.snapshot()).generation
        await database.stop()
        disconnected = await backend.snapshot()
        assert disconnected is not None and not disconnected.ready
        result["disconnected"] = asdict(disconnected)
        await database.start()
        resumed = await eventually(
            backend.snapshot, lambda snap: snap.ready and snap.generation > generation
        )
        result["resynchronized"] = asdict(resumed)
        result["manager_after_database_restart_exit_code"] = manager.poll()
        assert manager.poll() is None, "Native manager did not survive database restart"
        assert resumed.observed.values["ssid"] == "native-withheld"
        # A fresh read of persisted State alone cannot prove OWM reconnected.
        after_restart = await operation("native-reconnected", 20)
        await eventually(
            lambda: refresh(after_restart), lambda op: op.state == State.OBSERVED_APPLIED
        )
        result["applied_after_database_restart"] = store.get(after_restart.operation_id).to_dict()
        result["probe_passed"] = True
    except Exception as exc:
        result["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        if manager is not None and manager.poll() is None:
            manager.terminate()
            try:
                manager.wait(timeout=5)
            except subprocess.TimeoutExpired:
                manager.kill()
                manager.wait()
        result["manager_final_exit_code"] = manager.returncode if manager else None
        result["write_count"] = backend.write_count
        result["operations_at_end"] = [op.to_dict() for op in store.operations()]
        result["maximum_child_rss_kib"] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        (evidence / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
        log.close()
        await backend.close()
        store.close()
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
