"""Owned Ethernet WSC component exercise and synthetic hostap payload peer.

This starts explicitly at the M1 component boundary. Discovery/profile admission
is not bypassed in the service: no service activation or native controller is
involved. Shared filesystem barriers coordinate the laboratory, not IEEE messages.
"""

import asyncio
import hashlib
import json
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from emosa.model import State
from emosa.opensync.session import OvsSession
from emosa.reconcile import Engine
from emosa.secrets import SecretStore
from emosa.simulation.database import SimDatabase, SimManager
from emosa.simulation.wire_reports import pcap
from emosa.simulation.wsc_provisioning import (
    AGENT,
    CONTROLLER,
    TARGET,
    BoundBackend,
    make_bridge,
    public_operation,
    registrar_reply,
    seed,
)
from emosa.store import Store
from emosa.wire.autoconfiguration import PeerBinding
from emosa.wire.cmdu import MidSequence, Reassembler, Tlv, fragment_message
from emosa.wire.ethernet import EthernetEndpoint
from emosa.wire.provisioning_session import ComponentProvisioningSession

RADIO_ROOT = Path("/opt/emosa-radio-manager/runs")
RADIO_BSSID = "02:00:00:ec:02:00"
SSID = "EMOSA-WSC-component"


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def verify_registrar(executable):
    executable = Path(executable).resolve(strict=True)
    reference = json.loads((executable.parent / "provenance.json").read_text())
    if reference["binary_sha256"] != hashlib.sha256(executable.read_bytes()).hexdigest():
        raise ValueError("registrar binary differs from its build provenance")
    return reference


async def wait_file(path, *, seconds=30):
    end = time.monotonic() + seconds
    while not path.is_file():
        if time.monotonic() >= end:
            raise TimeoutError("owned packet experiment barrier timed out")
        await asyncio.sleep(0.02)


async def exchange_agent(interface, directory, captures, engine, backend, apply, *, target=TARGET):
    binding = PeerBinding(interface, 1, AGENT, CONTROLLER, (CONTROLLER,))
    mids = MidSequence(100)
    current = None
    with EthernetEndpoint(interface, AGENT, timeout=0.05) as endpoint:

        def new_session():
            bridge = make_bridge(
                engine,
                backend,
                "ethernet-wsc",
                binding=binding,
                target=target,
                deadline=120,
                mids=mids,
            )
            return ComponentProvisioningSession(bridge, endpoint.send)

        current = new_session()
        try:
            await current.start()
            (directory / "left.ready").touch()
            rejected_first = False
            complete = 0
            end = time.monotonic() + 35
            while time.monotonic() < end:
                current.tick()
                frame = await asyncio.to_thread(endpoint.receive)
                if frame is None:
                    continue
                captures.append(frame)
                result = await current.receive(frame, ingress=interface, generation=1)
                if not rejected_first:
                    assert result["status"] == "rejected" and result["reason"] == "INVALID_INPUT"
                    assert current.bridge.exchange.state == "closed"
                    assert not engine.store.operations() and backend.write_count == 0
                    assert not list(engine.vault.directory.glob("wsc-*"))
                    current.close()
                    current = new_session()
                    await current.start()
                    rejected_first = True
                    continue
                if result["status"] == "incomplete":
                    assert not engine.store.operations() and backend.write_count == 0
                    (directory / "partial-observed").touch()
                else:
                    assert result["status"] in {"operation", "rejected"}
                    complete += 1
                if complete == 5:
                    break
            assert rejected_first and complete == 5, "packet sequence did not complete"
            assert len(engine.store.operations()) == 1 and backend.write_count == 1
            operation = engine.store.operations()[0]
            snap = await backend.snapshot()
            assert snap.config["ssid"] == SSID and snap.observed.values["ssid"] != SSID
            assert operation.state in {State.CONFIG_COMMITTED, State.INDETERMINATE}
            before = {
                "operation": public_operation(engine, operation.operation_id),
                "config_ssid": snap.config["ssid"],
                "observed_ssid": snap.observed.values["ssid"],
                "packet_session": current.status(),
                "transaction_attempts": backend.write_count,
            }
            write(directory / "wire-ready.json", before)
            await apply()
            end = time.monotonic() + 35
            while time.monotonic() < end:
                await engine.reconcile("pod-1")
                operation = engine.store.get(operation.operation_id)
                if operation.state == State.OBSERVED_APPLIED:
                    break
                await asyncio.sleep(0.1)
            assert operation.state == State.OBSERVED_APPLIED
            assert len(operation.attempts) == backend.write_count == 1
            if before["operation"]["state"] == State.INDETERMINATE:
                assert operation.commit_evidence["attribution"] == "unknown"
                assert operation.application_evidence["attribution"] == "current_condition_only"
            return {
                "passed": True,
                "before_application": before,
                "operation": public_operation(engine, operation.operation_id),
                "packet_session": current.status(),
                "invalid_authentication_operations": 0,
                "incomplete_request_operations": 0,
                "transaction_attempts": backend.write_count,
            }
        finally:
            if current:
                current.close()


async def database_agent(interface, directory, captures, *, lost_reply):
    database, manager, session, store = SimDatabase(), None, None, None
    with tempfile.TemporaryDirectory(prefix="emosa-wsc-packet-") as private:
        root = Path(private)
        try:
            await database.start()
            await seed(database)
            manager = await SimManager(database.endpoint).start()
            listener = "unix:" + str(root / "adapter.sock")
            session = OvsSession("p" + listener)
            await database.manager_remote(listener)
            vault, store = SecretStore(root / "secrets"), Store(root / "journal")
            backend = BoundBackend(session, vault)
            backend.drop_next_reply = lost_reply
            engine = Engine(store, vault, {"pod-1": backend})
            return await exchange_agent(
                interface, directory, captures, engine, backend, lambda: manager.command("apply")
            )
        finally:
            if store:
                store.close()
            if session:
                await session.close()
            if manager:
                await manager.close()
            await database.close()


async def radio_agent(interface, directory, captures, radio_directory, *, lost_reply):
    # This private mode is called only by the ownership-checked hwsim runner.
    # It derives the endpoint inside that runner's existing owned directory;
    # no arbitrary Unix/TCP/physical endpoint is accepted.
    root = Path(radio_directory).resolve(strict=True)
    if root.parent != RADIO_ROOT or json.loads((root / "wire-owner.json").read_text()) != {
        "owner": "emosa-owned-hwsim-wsc-v1",
        "backend": "ovsdb-sim",
    }:
        raise ValueError("expected an owned hwsim provisioning run")
    session = OvsSession("punix:" + str(root / "database/wire-pod.sock"))
    vault, store = SecretStore(root / "wire-secrets"), Store(root / "wire-journal")
    backend = BoundBackend(
        session,
        vault,
        radio_mac=RADIO_BSSID,
        bssid=RADIO_BSSID,
        if_name="wlan0",
        radio_name="phy1",
        state_provenance="independent-hostapd-nl80211-manager:Wifi_VIF_State",
    )
    backend.drop_next_reply = lost_reply
    try:
        engine = Engine(store, vault, {"pod-1": backend})
        target = replace(TARGET, bssid=bytes.fromhex(RADIO_BSSID.replace(":", "")))
        return await exchange_agent(
            interface,
            directory,
            captures,
            engine,
            backend,
            lambda: wait_file(directory / "allow-application", seconds=55),
            target=target,
        )
    finally:
        store.close()
        await session.close()


async def peer(interface, directory, captures, registrar):
    verify_registrar(registrar)
    requests = []
    with EthernetEndpoint(interface, CONTROLLER, timeout=0.05) as endpoint:
        (directory / "right.ready").touch()
        decoder = Reassembler()
        end = time.monotonic() + 30
        while time.monotonic() < end and len(requests) < 2:
            frame = await asyncio.to_thread(endpoint.receive)
            if frame is None:
                continue
            captures.append(frame)
            message = decoder.feed(frame, ingress=interface)
            if message is None:
                continue
            assert message.source == AGENT and message.destination == CONTROLLER
            assert message.message_type == 9
            m1 = next(t.value for t in message.tlvs if t.kind == 0x11)
            ruid = next(t.value[:6] for t in message.tlvs if t.kind == 0x85)
            requests.append(hashlib.sha256(m1).hexdigest())
            m2 = await asyncio.to_thread(registrar_reply, registrar, m1)

            def frames(payload, mid, *, radio=ruid, extra=()):
                return fragment_message(
                    AGENT, CONTROLLER, 9, mid, (Tlv(0x82, radio), Tlv(0x11, payload), *extra)
                )

            if len(requests) == 1:
                for frame in frames(m2[:-1] + bytes([m2[-1] ^ 1]), 501):
                    endpoint.send(frame)
                continue
            for frame in frames(m2, 502, radio=bytes.fromhex("02000000ffff")):
                endpoint.send(frame)
            valid = frames(m2, 503, extra=(Tlv(0xEF, bytes(1024)),))
            assert len(valid) == 2
            endpoint.send(valid[1])  # Out-of-order arrival; no operation yet.
            await wait_file(directory / "partial-observed", seconds=5)
            endpoint.send(valid[0])
            for frame in frames(m2, 504):
                endpoint.send(frame)
            rebuilt = await asyncio.to_thread(registrar_reply, registrar, m1)
            assert rebuilt != m2
            for frame in frames(rebuilt, 505):
                endpoint.send(frame)
            changed = await asyncio.to_thread(registrar_reply, registrar, m1, "changed")
            for frame in frames(changed, 506):
                endpoint.send(frame)
        assert len(requests) == 2 and requests[0] != requests[1]
        return {
            "passed": True,
            "m1_sha256": requests,
            "m2_mids_sent": [501, 502, 503, 504, 505, 506],
            "out_of_order_fragments": True,
            "fresh_reencrypted_retry": True,
            "registrar": verify_registrar(registrar),
        }


async def worker(side, interface, directory, registrar, *, lost_reply=False, radio_directory=None):
    directory, captures = Path(directory), []
    try:
        if side == "right":
            result = await peer(interface, directory, captures, registrar)
        elif radio_directory is None:
            result = await database_agent(interface, directory, captures, lost_reply=lost_reply)
        else:
            result = await radio_agent(
                interface, directory, captures, radio_directory, lost_reply=lost_reply
            )
        result.update(
            scope="owned_WSC_provisioning_over_AF_PACKET",
            socket_io=True,
            native_controller_used=False,
            full_controller_onboarding=False,
            physical_pod_proven=False,
            radio_used=radio_directory is not None,
            lost_reply_injected=lost_reply,
            frames_received=len(captures),
        )
        write(directory / (side + ".json"), result)
    finally:
        (directory / (side + ".pcap")).write_bytes(pcap(captures))
