import asyncio
import socket
import ssl

import pytest

from emosa.opensync.session import OvsSession
from emosa.simulation.database import SimDatabase
from emosa.simulation.tls import create_pki, unused_port


@pytest.mark.ovsdb
def test_incomplete_handshakes_are_bounded_and_expire_without_displacing_pod(tmp_path):
    pki = create_pki(tmp_path / "secrets", 1)
    port = unused_port()

    async def scenario():
        db = SimDatabase(tls_files=pki["pods"][0])
        session = OvsSession(
            f"pssl:{port}:127.0.0.1",
            tls_files=pki["server"],
            peer_certificate_sha256=pki["pins"][0],
            timeout=10,
        )
        clients = []
        try:
            await db.start()
            await db.seed()
            first = asyncio.create_task(session.snapshot())
            await db.manager_remote(f"ssl:127.0.0.1:{port}")
            generation = (await first)["generation"]
            for _ in range(7):
                clients.append(socket.create_connection(("127.0.0.1", port), timeout=1))
                assert (await session.snapshot())["generation"] == generation
            listener = session.rpc.pstream
            assert listener.statistics()["pending"] == 4
            assert listener.statistics()["overflow"] == 3
            end = asyncio.get_running_loop().time() + 5
            while listener.statistics()["pending"]:
                assert (await session.snapshot())["generation"] == generation
                assert asyncio.get_running_loop().time() < end
                await asyncio.sleep(0.05)
            assert listener.statistics()["expired"] == 4
        finally:
            for client in clients:
                client.close()
            await session.close()
            await db.close()

    asyncio.run(scenario())


@pytest.mark.ovsdb
def test_read_only_qualification_accepts_pod_initiated_tls_without_writes(tmp_path):
    import json

    from emosa.qualification import collect
    from emosa.simulation.tls import trust_config

    pki = create_pki(tmp_path / "secrets", 1)
    port = unused_port()
    config = {
        "schema_version": 1,
        "pod_id": "synthetic-tls-qualification",
        "database": "Open_vSwitch",
        "secret_directory": str(tmp_path / "secrets"),
        "read_only": True,
        "timeout_seconds": 10,
        "connection": {
            "direction": "listen",
            "endpoint": f"pssl:{port}:127.0.0.1",
            "trust": {"kind": "mutual-tls", **trust_config(pki, 0)},
        },
    }

    async def scenario():
        db = SimDatabase(tls_files=pki["pods"][0])
        admin = OvsSession(db.endpoint)
        try:
            await db.start()
            await db.seed()
            before = (await admin.snapshot())["tables"]
            task = asyncio.create_task(collect(config, tmp_path / "collection"))
            await db.manager_remote(f"ssl:127.0.0.1:{port}")
            assert (await task)["status"] == "draft_read_only"
            assert (await admin.snapshot())["tables"] == before
            profile = json.loads((tmp_path / "collection/draft-profile.json").read_text())
            assert not profile["configuration_representations"]["credential_values_collected"]
            assert "initial-simulation-key" not in json.dumps(profile)
        finally:
            await admin.close()
            await db.close()

    asyncio.run(scenario())


@pytest.mark.ovsdb
def test_real_database_initiates_authenticated_tls_and_reconnects(tmp_path):
    pki = create_pki(tmp_path / "secrets", 1)
    port = unused_port()

    async def scenario():
        db = SimDatabase(tls_files=pki["pods"][0])
        session = OvsSession(
            f"pssl:{port}:127.0.0.1",
            tls_files=pki["server"],
            peer_certificate_sha256=pki["pins"][0],
            timeout=10,
        )
        try:
            await db.start()
            await db.seed(serial_number="TLS-SYNTHETIC-1")
            first = asyncio.create_task(session.snapshot())
            await db.manager_remote(f"ssl:127.0.0.1:{port}")
            snapshot = await first
            assert snapshot["ready"]
            assert snapshot["tables"]["AWLAN_Node"]
            generation = snapshot["generation"]
            await session.reconnect()
            assert (await session.snapshot())["generation"] > generation
        finally:
            await session.close()
            await db.close()

    asyncio.run(scenario())


@pytest.mark.ovsdb
def test_untrusted_and_wrong_pin_clients_never_replace_valid_session(tmp_path):
    pki = create_pki(tmp_path / "secrets", 2)
    other = create_pki(tmp_path / "untrusted", 1)
    port = unused_port()

    async def scenario():
        db = SimDatabase(tls_files=pki["pods"][0])
        session = OvsSession(
            f"pssl:{port}:127.0.0.1",
            tls_files=pki["server"],
            peer_certificate_sha256=pki["pins"][0],
            timeout=10,
        )
        try:
            await db.start()
            await db.seed()
            first = asyncio.create_task(session.snapshot())
            await db.manager_remote(f"ssl:127.0.0.1:{port}")
            generation = (await first)["generation"]

            def attack(files):
                context = ssl.create_default_context(cafile=pki["server"]["ca"])
                if files:
                    context.load_cert_chain(files["certificate"], files["private_key"])
                try:
                    with (
                        socket.create_connection(("127.0.0.1", port), timeout=2) as sock,
                        context.wrap_socket(sock, server_hostname="127.0.0.1") as client,
                    ):
                        client.sendall(b"{}\n")
                        return client.recv(1)
                except ssl.SSLCertVerificationError:
                    # Rejecting the test server's certificate is a fixture failure,
                    # not evidence that the listener rejected this client.
                    raise
                except (OSError, ssl.SSLError):
                    return b""

            for files in (None, other["pods"][0], pki["pods"][1]):
                task = asyncio.create_task(asyncio.to_thread(attack, files))
                while not task.done():
                    assert (await session.snapshot())["generation"] == generation
                    await asyncio.sleep(0.01)
                assert await task == b""
                await session.snapshot()
            assert session.rpc.pstream.statistics()["rejected"] >= 3
            assert session.rpc.pstream.statistics()["pin_rejected"] == 1
        finally:
            await session.close()
            await db.close()

    asyncio.run(scenario())
