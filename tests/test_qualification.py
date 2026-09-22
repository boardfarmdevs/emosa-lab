import asyncio
import hashlib
import json
import os
import ssl
import subprocess

import pytest

from emosa.errors import EmosaError
from emosa.opensync.schema import Schema
from emosa.opensync.session import OvsSession
from emosa.qualification import collect
from emosa.simulation.database import SimDatabase


def config_for(endpoint, secret_directory, trust=None):
    return {
        "schema_version": 1,
        "pod_id": "unqualified-pod",
        "database": "Open_vSwitch",
        "connection": {
            "direction": "dial",
            "endpoint": endpoint,
            "trust": trust or {"kind": "local-unix"},
        },
        "secret_directory": str(secret_directory),
        "read_only": True,
    }


@pytest.mark.ovsdb
def test_read_only_collects_draft_without_credentials_or_writes(tmp_path):
    async def scenario():
        db = await SimDatabase().start()
        admin = OvsSession(db.endpoint)
        readonly = OvsSession(db.endpoint, read_only=True)
        try:
            await db.seed()
            before = (await admin.snapshot())["tables"]
            result = await collect(config_for(db.endpoint, tmp_path), tmp_path / "collection")
            assert result["status"] == "draft_read_only" and not result["writable"]
            profile = json.loads((tmp_path / "collection/draft-profile.json").read_text())
            saved_schema = Schema(json.loads((tmp_path / "collection/schema.json").read_text()))
            assert saved_schema.fingerprint == profile["schema_fingerprint"]
            assert saved_schema.db.tables["Wifi_VIF_Config"].columns["wpa_psks"].type.is_map()
            assert profile["vifs"][0]["config"]["if_name"] == "lab-ap"
            assert profile["radios"][0]["vif_config_refs"] == [profile["vifs"][0]["config_uuid"]]
            assert profile["configuration_representations"]["modern_wpa_columns_present"]
            assert not profile["configuration_representations"]["credential_values_collected"]
            assert profile["radio_scope_candidates"][0]["credential_layout"] == "not_collected"
            assert not profile["radio_scope_candidates"][0]["wire_admission"]
            assert "initial-simulation-key" not in json.dumps(profile)
            assert "preserved-guest-key" not in json.dumps(profile)
            assert (await admin.snapshot())["tables"] == before
            with pytest.raises(EmosaError, match="read-only"):
                await readonly.transact([{"op": "delete", "table": "Wifi_VIF_Config", "where": []}])
            assert (await admin.snapshot())["tables"] == before
        finally:
            await readonly.close()
            await admin.close()
            await db.close()

    asyncio.run(scenario())


@pytest.mark.ovsdb
def test_mutual_tls_collection_and_peer_pin(tmp_path):
    private = tmp_path / "secrets"
    private.mkdir(mode=0o700)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(private / "key.pem"),
            "-out",
            str(private / "cert.pem"),
            "-subj",
            "/CN=emosa-qualification-test",
        ],
        check=True,
        capture_output=True,
    )
    for path in private.iterdir():
        os.chmod(path, 0o600)
    certificate = ssl.PEM_cert_to_DER_cert((private / "cert.pem").read_text())
    pin = hashlib.sha256(certificate).hexdigest()

    async def scenario():
        db = await SimDatabase().start()
        connections = set()

        async def proxy(reader, writer):
            task = asyncio.current_task()
            connections.add(task)
            remote_writer = None
            try:
                remote_reader, remote_writer = await asyncio.open_unix_connection(
                    str(db.directory / "db.sock")
                )

                async def copy(source, target):
                    while data := await source.read(65536):
                        target.write(data)
                        await target.drain()
                    target.close()

                await asyncio.gather(copy(reader, remote_writer), copy(remote_reader, writer))
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                writer.close()
                if remote_writer:
                    remote_writer.close()
                connections.discard(task)

        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(private / "cert.pem", private / "key.pem")
        context.load_verify_locations(private / "cert.pem")
        context.verify_mode = ssl.CERT_REQUIRED
        server = await asyncio.start_server(proxy, "127.0.0.1", 0, ssl=context)
        port = server.sockets[0].getsockname()[1]
        trust = {
            "kind": "mutual-tls",
            "certificate_ref": "cert.pem",
            "private_key_ref": "key.pem",
            "ca_ref": "cert.pem",
            "peer_certificate_sha256": pin,
        }
        config = config_for(f"ssl:127.0.0.1:{port}", private, trust)
        try:
            await db.seed()
            assert (await collect(config, tmp_path / "valid"))["status"] == "draft_read_only"
            config["connection"]["trust"]["peer_certificate_sha256"] = "0" * 64
            with pytest.raises(EmosaError, match="pin mismatch"):
                await collect(config, tmp_path / "wrong-peer")
            assert not (tmp_path / "wrong-peer/schema.json").exists()
        finally:
            server.close()
            await server.wait_closed()
            for task in tuple(connections):
                task.cancel()
            await asyncio.gather(*connections, return_exceptions=True)
            await db.close()

    asyncio.run(scenario())


@pytest.mark.unit
def test_qualification_rejects_plaintext_remote_and_unsafe_secrets(tmp_path):
    from emosa.qualification import private_reference, qualification_session

    with pytest.raises(EmosaError):
        qualification_session(
            config_for("tcp:192.0.2.5:6640", tmp_path, {"kind": "existing-tunnel"})
        )
    with pytest.raises(EmosaError):
        private_reference(tmp_path, "../credentials")
    (tmp_path / "public.pem").write_text("not-a-secret")
    os.chmod(tmp_path / "public.pem", 0o644)
    with pytest.raises(EmosaError, match="private"):
        private_reference(tmp_path, "public.pem")
