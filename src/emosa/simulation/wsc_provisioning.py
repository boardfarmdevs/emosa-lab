"""Owned WSC-to-OVSDB experiment; no physical endpoint or service activation.

Ethernet frames pass through the real decoder in memory. A separate executable
using pinned upstream hostap builds fresh M2 payloads; it is NOT a controller.
The independent simulated manager, not EMOSA, publishes application State.
"""

import argparse
import asyncio
import hashlib
import json
import multiprocessing
import os
import signal
import subprocess
import tempfile
from pathlib import Path

from emosa.easymesh_payloads import (
    APRadioAdvancedCapabilities,
    APRadioBasicCapabilities,
    BasicOperatingClass,
    Profile2APCapability,
)
from emosa.errors import EmosaError, Reason
from emosa.model import State
from emosa.opensync.mapping import OpenSyncBackend
from emosa.opensync.session import OvsSession
from emosa.reconcile import Engine
from emosa.secrets import SecretStore
from emosa.simulation.database import SimDatabase, SimManager
from emosa.store import Store
from emosa.wire.autoconfiguration import PeerBinding, WscExchange
from emosa.wire.cmdu import MidSequence, Reassembler, Tlv, fragment_message
from emosa.wire.operation_bridge import ComponentTarget, ScopeContext, WscComponentBridge
from emosa.wsc_messages import M1Device

SERIAL = "owned-wsc-component"
AGENT, CONTROLLER = bytes.fromhex("020000005001"), bytes.fromhex("020000005002")
RUID, BSSID = bytes.fromhex("020000005010"), bytes.fromhex("020000005011")
BINDING = PeerBinding("owned-memory-link", 1, AGENT, CONTROLLER, (CONTROLLER,))
TARGET = ComponentTarget("pod-1", "radio-1", "bss-1", RUID, BSSID)
CASES = ("configure", "lost-reply", "identity-race", "crash-after-commit")


class BoundBackend(OpenSyncBackend):
    """Fixture identity is pinned across every plan/read/submit, not M2 MAC data."""

    def __init__(self, session, vault):
        super().__init__(
            "pod-1", session, vault, expected_serial=SERIAL, mapping_scope="sole-fronthaul-radio"
        )
        self.anchor = None

    def _binding(self, raw):
        result = super()._binding(raw)
        rows = result[-1]
        radio = next(iter(rows["Wifi_Radio_State"].values()))
        vif = next(iter(rows["Wifi_VIF_State"].values()))
        if (
            not raw["ready"]
            or radio.get("mac") != "02:00:00:00:50:10"
            or vif.get("mac") != "02:00:00:00:50:11"
            or radio.get("freq_band") != "2.4G"
            or radio.get("channel") != 6
        ):
            raise EmosaError(Reason.NOT_READY, "owned component source identity changed")
        anchor = ScopeContext(
            raw["generation"],
            raw["schema"].fingerprint,
            hashlib.sha256(
                json.dumps({t: sorted(rs) for t, rs in rows.items()}, sort_keys=True).encode()
            ).hexdigest(),
        )
        if self.anchor is not None and (
            self.anchor.schema_fingerprint != anchor.schema_fingerprint
            or self.anchor.binding_token != anchor.binding_token
        ):
            raise EmosaError(Reason.NOT_READY, "owned component graph changed")
        # Reconciliation may observe a fresh connection after an uncertain
        # commit. The bridge separately pins generation for write authority;
        # submit also compares its attempt generation atomically at transport.
        self.anchor = anchor
        return result

    async def context(self):
        self._binding(await self.session.snapshot())
        return self.anchor


def assemble(frames):
    decoder, message = Reassembler(), None
    for frame in frames:
        message = decoder.feed(frame)
    if message is None:
        raise AssertionError("complete component CMDU required")
    return message


def registrar_reply(executable, m1, mode="configure"):
    result = subprocess.run(
        [str(executable), mode], input=m1, capture_output=True, timeout=5, check=False
    )
    if result.returncode or not 1 <= len(result.stdout) <= 4096:
        raise EmosaError(Reason.NOT_READY, "independent component registrar failed")
    return result.stdout


def make_bridge(engine, backend, run_id):
    device = M1Device(
        uuid=bytes.fromhex("02000000500140008000000000000001"),
        al_mac=AGENT,
        authentication_types=0x20,
        encryption_types=8,
        connection_types=1,
        configuration_methods=0x0280,
        wps_state=2,
        manufacturer=b"EMOSA synthetic laboratory",
        model_name=b"Owned WSC component",
        model_number=b"1",
        serial_number=SERIAL.encode(),
        primary_device_type=bytes.fromhex("00060050f2040001"),
        device_name=b"Synthetic represented AP",
        rf_band=1,
        association_state=0,
        device_password_id=4,
        configuration_error=0,
        os_version=1,
    )
    exchange = WscExchange(
        BINDING,
        device,
        APRadioBasicCapabilities(RUID, 1, (BasicOperatingClass(81, 20, ()),)),
        Profile2APCapability(0, 0, 0, 0),
        APRadioAdvancedCapabilities(RUID, 0),
        mids=MidSequence(100),
        timeout=30,
    )
    return WscComponentBridge(engine, exchange, TARGET, backend.context, run_id=run_id)


async def start_exchange(bridge, registrar, frames):
    sent = []
    await bridge.start(sent.append)
    frames.extend(sent)
    m1 = next(t.value for t in assemble(sent).tlvs if t.kind == 0x11)
    m2 = await asyncio.to_thread(registrar_reply, registrar, m1)
    return m1, m2


async def receive(bridge, payload, frames, *, mid=201, extra=()):
    incoming = fragment_message(
        AGENT, CONTROLLER, 9, mid, (Tlv(0x82, RUID), Tlv(0x11, payload), *extra)
    )
    frames.extend(incoming)
    return await bridge.receive(assemble(incoming), ingress=BINDING.ingress, generation=1)


async def reject(bridge, payload, frames, **kwargs):
    try:
        await receive(bridge, payload, frames, **kwargs)
    except EmosaError as exc:
        return exc.code
    raise AssertionError("hostile/unsupported input created a component operation")


async def seed(database):
    await database.seed(serial_number=SERIAL, sole_radio=True)
    admin = OvsSession(database.endpoint)
    try:
        result = await admin.transact(
            [
                {"op": "update", "table": table, "where": [], "row": row}
                for table, row in (
                    ("Wifi_Radio_Config", {"freq_band": "2.4G"}),
                    (
                        "Wifi_Radio_State",
                        {"freq_band": "2.4G", "channel": 6, "mac": "02:00:00:00:50:10"},
                    ),
                    ("Wifi_VIF_State", {"mac": "02:00:00:00:50:11"}),
                )
            ]
        )
        assert result == [{"count": 1}] * 3
    finally:
        await admin.close()


async def observe(engine, operation_id):
    end = asyncio.get_running_loop().time() + 5
    while True:
        await engine.reconcile("pod-1")
        op = engine.store.get(operation_id)
        if op.state == State.OBSERVED_APPLIED:
            return op
        if asyncio.get_running_loop().time() >= end:
            raise AssertionError(f"independent manager State was not observed: {op.state}")
        await asyncio.sleep(0.02)


async def read_ssids(endpoint):
    """Separate read session; export no key, credential fingerprint or raw rows."""
    reader = OvsSession(endpoint, read_only=True)
    try:
        raw = await reader.snapshot()
        return {
            table: [raw["schema"].row(table, row)["ssid"] for row in raw["tables"][table].values()]
            for table in ("Wifi_VIF_Config", "Wifi_VIF_State")
        }
    finally:
        await reader.close()


def public_operation(engine, operation_id):
    op = engine.store.get(operation_id)
    receipt = engine.store.wsc_receipt(operation_id)
    return {
        "operation_id": op.operation_id,
        "initiating_interface": op.initiating_interface,
        "state": op.state,
        "reason": op.reason,
        "attempts": len(op.attempts),
        "commit_attribution": op.commit_evidence.get("attribution"),
        "application_evidence": op.application_evidence,
        "receipt": {
            key: value
            for key, value in receipt.items()
            if key not in {"request_fingerprint", "process_id", "exchange_id"}
        },
    }


def crash_worker(directory, endpoint, registrar, report_path):
    """Internal child, given only its parent's disposable database and private state."""

    async def work():
        vault = SecretStore(Path(directory) / "secrets")
        store = Store(Path(directory) / "journal")
        backend = BoundBackend(OvsSession(endpoint), vault)
        engine = Engine(store, vault, {"pod-1": backend})
        bridge = make_bridge(engine, backend, "crash-after-commit")
        frames = []
        _, m2 = await start_exchange(bridge, registrar, frames)
        op = await receive(bridge, m2, frames)
        submit = backend.submit

        async def die_after_commit(intent, attempt):
            result = await submit(intent, attempt)
            assert result.status == "committed"
            with open(report_path, "x") as report:
                json.dump(
                    {
                        "operation_id": op.operation_id,
                        "writes": backend.write_count,
                        "frames": [f.hex() for f in frames],
                    },
                    report,
                )
                report.flush()
                os.fsync(report.fileno())
            os.kill(os.getpid(), signal.SIGKILL)
            raise AssertionError("SIGKILL did not terminate child")

        backend.submit = die_after_commit
        await engine.execute(op.operation_id)

    asyncio.run(work())


async def run_case(registrar, case):
    if case not in CASES:
        raise ValueError("unknown owned component case")
    database, manager, session, store, bridge = SimDatabase(), None, None, None, None
    frames, checks = [], {}
    with tempfile.TemporaryDirectory(prefix="emosa-wsc-") as directory:
        root = Path(directory)
        try:
            await database.start()
            await seed(database)
            manager = await SimManager(database.endpoint).start()
            if case == "crash-after-commit":
                report_path = root / "child.json"
                child = multiprocessing.get_context("spawn").Process(
                    target=crash_worker,
                    args=(directory, database.endpoint, str(registrar), str(report_path)),
                )
                child.start()
                try:
                    await asyncio.to_thread(child.join, 20)
                    assert child.exitcode == -signal.SIGKILL
                finally:
                    if child.is_alive():
                        child.kill()
                        await asyncio.to_thread(child.join, 5)
                    child.close()
                record = json.loads(report_path.read_text())
                frames = [bytes.fromhex(f) for f in record["frames"]]
                session = OvsSession(database.endpoint)
                vault, store = SecretStore(root / "secrets"), Store(root / "journal")
                backend = BoundBackend(session, vault)
                engine = Engine(store, vault, {"pod-1": backend})
                op_id = record["operation_id"]
                assert store.get(op_id).state == State.SUBMITTED
                engine.recover()
                assert store.get(op_id).state == State.INDETERMINATE
                await engine.execute(op_id)
                assert backend.write_count == 0
                await manager.command("apply")
                await observe(engine, op_id)
                assert backend.write_count == 0
                old_m2 = next(t.value for t in assemble(frames).tlvs if t.kind == 0x11)
                bridge = make_bridge(engine, backend, "restarted-exchange")
                await start_exchange(bridge, registrar, frames)
                assert bridge.exchange.m1_sha256 != store.wsc_receipt(op_id)["m1_sha256"]
                checks["old_m2_against_fresh_m1"] = await reject(bridge, old_m2, frames)
                checks.update(
                    real_process_killed_after_commit=True,
                    durable_submitted_recovered_as_indeterminate=True,
                    writes_before_crash=record["writes"],
                    writes_after_restart=backend.write_count,
                )
            else:
                # The owned simulated pod initiates this southbound connection.
                listener = "unix:" + str(root / "adapter.sock")
                session = OvsSession("p" + listener)
                await database.manager_remote(listener)
                vault, store = SecretStore(root / "secrets"), Store(root / "journal")
                backend = BoundBackend(session, vault)
                engine = Engine(store, vault, {"pod-1": backend})
                # Failed parameter configuration ends that exchange. Every
                # negative trial therefore uses a fresh M1; none reopens it.
                for failure in (
                    "invalid_authentication",
                    "teardown",
                    "multiple_m2",
                    "unsupported_companion",
                ):
                    bridge = make_bridge(engine, backend, case)
                    m1, m2 = await start_exchange(bridge, registrar, frames)
                    extra = ()
                    if failure == "invalid_authentication":
                        m2 = m2[:-1] + bytes([m2[-1] ^ 1])
                    elif failure == "teardown":
                        m2 = await asyncio.to_thread(registrar_reply, registrar, m1, "teardown")
                    elif failure == "multiple_m2":
                        extra = (Tlv(0x11, m2),)
                    else:
                        extra = (Tlv(0xB5, b"unsupported"),)
                    checks[failure] = await reject(bridge, m2, frames, extra=extra)
                    bridge.close()
                assert not store.operations() and not list(vault.directory.glob("wsc-*"))
                assert backend.write_count == 0
                checks["negative_trials_created_operations"] = 0
                bridge = make_bridge(engine, backend, case)
                m1, m2 = await start_exchange(bridge, registrar, frames)
                op = await receive(bridge, m2, frames)
                op_id = op.operation_id
                for retry in (m2, await asyncio.to_thread(registrar_reply, registrar, m1)):
                    assert (await receive(bridge, retry, frames, mid=202)).operation_id == op_id
                changed = await asyncio.to_thread(registrar_reply, registrar, m1, "changed")
                checks["changed_duplicate"] = await reject(bridge, changed, frames, mid=203)
                assert len(store.operations()) == 1 and backend.write_count == 0
                if case == "lost-reply":
                    backend.drop_next_reply = True
                elif case == "identity-race":

                    async def change_identity():
                        admin = OvsSession(database.endpoint)
                        try:
                            result = await admin.transact(
                                [
                                    {
                                        "op": "update",
                                        "table": "Wifi_VIF_State",
                                        "where": [],
                                        "row": {"mac": "02:00:00:00:50:99"},
                                    }
                                ]
                            )
                            assert result == [{"count": 1}]
                        finally:
                            await admin.close()

                    backend.before_transaction = change_identity
                op = await engine.execute(op_id)
                expected = {
                    "configure": State.CONFIG_COMMITTED,
                    "lost-reply": State.INDETERMINATE,
                    "identity-race": State.OWNERSHIP_CONFLICT,
                }[case]
                assert op.state == expected
                checks["after_submit"] = op.state
                if case == "configure":
                    assert (await receive(bridge, m2, frames, mid=204)).operation_id == op_id
                    assert (await engine.execute(op_id)).state == State.CONFIG_COMMITTED
                checks["rows_before_manager_apply"] = await read_ssids(database.endpoint)
                assert checks["rows_before_manager_apply"]["Wifi_VIF_State"] == ["initial-network"]
                if case != "identity-race":
                    await engine.reconcile("pod-1")
                    assert store.get(op_id).state != State.OBSERVED_APPLIED
                    checks["config_commit_did_not_claim_application"] = True
                    await manager.command("apply")
                    await observe(engine, op_id)
                else:
                    assert checks["rows_before_manager_apply"]["Wifi_VIF_Config"] == [
                        "initial-network"
                    ]
                    checks["manager_after_race"] = await manager.command("observe")
                checks["transaction_attempts"] = backend.write_count
                assert backend.write_count == 1
            checks["rows_at_end"] = await read_ssids(database.endpoint)
            if case != "identity-race":
                assert checks["rows_at_end"] == {
                    "Wifi_VIF_Config": ["EMOSA-WSC-component"],
                    "Wifi_VIF_State": ["EMOSA-WSC-component"],
                }
            if case in {"lost-reply", "crash-after-commit"}:
                recovered = store.get(op_id)
                assert recovered.commit_evidence["attribution"] == "unknown"
                assert recovered.application_evidence["attribution"] == "current_condition_only"
            result = {
                "case": case,
                "passed": True,
                "checks": checks,
                "operation": public_operation(engine, op_id),
                "frames": [f.hex() for f in frames],
                "socket_io": False,
                "physical_pod": False,
                "radio_or_client_observed": False,
                "full_controller_onboarding": False,
            }
            assert len(store.operations()) == 1
            return result
        finally:
            if bridge:
                bridge.close()
            if store:
                store.close()
            if session:
                await session.close()
            if manager:
                await manager.close()
            await database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registrar",
        type=Path,
        required=True,
        help="pinned component-registrar built by scripts/build-wsc-registrar.py",
    )
    parser.add_argument("--output", type=Path, required=True, help="new evidence directory")
    args = parser.parse_args()
    executable = args.registrar.resolve(strict=True)
    provenance = json.loads((executable.parent / "provenance.json").read_text())
    if provenance["binary_sha256"] != hashlib.sha256(executable.read_bytes()).hexdigest():
        parser.error("registrar binary does not match its build provenance")
    args.output.mkdir(parents=True, exist_ok=False)

    async def run():
        return [await run_case(executable, case) for case in CASES]

    results = asyncio.run(run())
    for result in results:
        (args.output / (result["case"] + ".json")).write_text(json.dumps(result, indent=2) + "\n")
    summary = {
        "scope": "owned_simulation_wsc_component",
        "cases": len(results),
        "passed": all(r["passed"] for r in results),
        "registrar": provenance,
        "full_wire_gate": "blocked_P0",
        "physical_pod": False,
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
