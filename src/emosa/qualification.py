"""Read-only collection of actual pod evidence; never enables a writable mapping."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from emosa.clock import utc_now
from emosa.config import validate
from emosa.errors import EmosaError, Reason
from emosa.evaluation.evidence import artifact, write_json
from emosa.opensync.radio_scope import assess
from emosa.opensync.session import OvsSession

# Deliberately omit security, wpa_psks, certificates, manager/cloud and credentials.
# Schema structure establishes candidate representations without reading Wi-Fi keys.
QUALIFICATION_COLUMNS = {
    "AWLAN_Node": [
        "model",
        "serial_number",
        "firmware_version",
        "platform_version",
        "sku",
        "revision",
        "id",
    ],
    "Wifi_Radio_Config": ["if_name", "freq_band", "enabled", "channel", "vif_configs"],
    "Wifi_Radio_State": [
        "if_name",
        "radio_config",
        "vif_states",
        "freq_band",
        "channel",
        "mac",
        "enabled",
    ],
    "Wifi_VIF_Config": [
        "if_name",
        "mode",
        "ssid",
        "enabled",
        "wpa",
        "wpa_key_mgmt",
        "pmf",
        "rsn_pairwise_ccmp",
        "wpa_pairwise_tkip",
        "wpa_pairwise_ccmp",
        "multi_ap",
    ],
    "Wifi_VIF_State": [
        "if_name",
        "vif_config",
        "mode",
        "ssid",
        "enabled",
        "wpa",
        "wpa_key_mgmt",
        "pmf",
        "rsn_pairwise_ccmp",
        "wpa_pairwise_tkip",
        "wpa_pairwise_ccmp",
        "mac",
    ],
}


def private_reference(directory, reference):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", reference):
        raise EmosaError(Reason.INVALID_INPUT, "invalid private file reference")
    directory = Path(directory)
    try:
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise EmosaError(Reason.INVALID_INPUT, "private directory must be owned with mode 0700")
        path = directory / reference
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise EmosaError(
                    Reason.INVALID_INPUT, "credential/trust reference must be a private owned file"
                )
            if info.st_size > 1024 * 1024:
                raise EmosaError(
                    Reason.INVALID_INPUT, "credential/trust reference exceeds size limit"
                )
        return path
    except OSError as exc:
        raise EmosaError(
            Reason.MISSING_PREREQUISITE, "private credential/trust reference unavailable"
        ) from exc


def qualification_session(config):
    validate("qualification", config)
    connection = config["connection"]
    endpoint, trust = connection["endpoint"], connection["trust"]
    direction = "listen" if endpoint.startswith(("ptcp:", "punix:", "pssl:")) else "dial"
    if connection["direction"] != direction:
        raise EmosaError(Reason.INVALID_INPUT, "endpoint direction contradicts configuration")
    kwargs = {}
    trust_evidence = {
        "kind": trust["kind"],
        "database_role": "pod database / EMOSA management client",
    }
    if trust["kind"] == "mutual-tls":
        required = {"certificate_ref", "private_key_ref", "ca_ref", "peer_certificate_sha256"}
        if not required.issubset(trust):
            raise EmosaError(
                Reason.INVALID_INPUT, "mutual TLS requires CA, client certificate/key and peer pin"
            )
        if not endpoint.startswith(("ssl:", "pssl:")):
            raise EmosaError(
                Reason.INVALID_INPUT,
                "mutual TLS requires ssl:HOST:PORT or pssl:PORT:IPv4",
            )
        kwargs["tls_files"] = {
            key: str(private_reference(config["secret_directory"], trust[ref]))
            for key, ref in (
                ("certificate", "certificate_ref"),
                ("private_key", "private_key_ref"),
                ("ca", "ca_ref"),
            )
        }
        kwargs["peer_certificate_sha256"] = trust["peer_certificate_sha256"]
        trust_evidence.update(
            certificate_chain="required",
            peer_certificate_pin="required",
            minimum_tls="1.2",
            hostname_matching="certificate pin binds peer identity",
        )
    elif trust["kind"] == "local-unix":
        if not endpoint.startswith(("unix:", "punix:")):
            raise EmosaError(Reason.INVALID_INPUT, "local-unix trust requires a Unix socket")
        parent = Path(endpoint.split(":", 1)[1]).parent
        if (
            not parent.is_dir()
            or parent.stat().st_mode & 0o077
            or parent.stat().st_uid != os.getuid()
        ):
            raise EmosaError(
                Reason.INVALID_INPUT, "Unix socket must be in an owned private directory"
            )
        trust_evidence["limitation"] = (
            "Local socket access alone does not authenticate a remote pod behind a tunnel."
        )
    else:
        if not (
            re.fullmatch(r"tcp:127\.0\.0\.1:[0-9]+", endpoint)
            or re.fullmatch(r"ptcp:[0-9]+:127\.0\.0\.1", endpoint)
        ):
            raise EmosaError(
                Reason.INVALID_INPUT, "existing tunnel endpoint must be explicit loopback"
            )
        if "evidence_ref" not in trust:
            raise EmosaError(
                Reason.INVALID_INPUT, "existing tunnel requires a private trust evidence reference"
            )
        evidence_path = private_reference(config["secret_directory"], trust["evidence_ref"])
        trust_evidence["tunnel_evidence_sha256"] = hashlib.sha256(
            evidence_path.read_bytes()
        ).hexdigest()
        trust_evidence["limitation"] = (
            "Tunnel setup and remote binding still require operator verification."
        )
    return OvsSession(
        endpoint,
        config["database"],
        read_only=True,
        monitor_columns=QUALIFICATION_COLUMNS,
        timeout=config.get("timeout_seconds", 10),
        **kwargs,
    ), trust_evidence


def draft_profile(config, snap, trust_evidence):
    schema, tables = snap["schema"], snap["tables"]
    rows = {
        table: {row_id: schema.row(table, row) for row_id, row in data.items()}
        for table, data in tables.items()
    }
    identities = list(rows.get("AWLAN_Node", {}).values())
    expected = config.get("expected_identifiers", {})
    identity_match = all(
        any(row.get(key) == value for row in identities) for key, value in expected.items()
    )
    vif_columns = schema.raw["tables"].get("Wifi_VIF_Config", {}).get("columns", {})
    modern = all(c in vif_columns for c in ("wpa", "wpa_key_mgmt", "wpa_psks"))
    legacy = "security" in vif_columns
    vif_configs = rows.get("Wifi_VIF_Config", {})
    vif_states = rows.get("Wifi_VIF_State", {})
    radios = []
    for radio_uuid, radio in rows.get("Wifi_Radio_Config", {}).items():
        radios.append(
            {
                "config_uuid": radio_uuid,
                "if_name": radio.get("if_name"),
                "freq_band": radio.get("freq_band"),
                "vif_config_refs": radio.get("vif_configs", []),
                "observed_states": [
                    r
                    for r in rows.get("Wifi_Radio_State", {}).values()
                    if r.get("radio_config") == radio_uuid
                ],
            }
        )
    vifs = [
        {
            "config_uuid": row_id,
            "config": row,
            "radio_config_refs": [
                r["config_uuid"] for r in radios if row_id in r["vif_config_refs"]
            ],
            "observed_states": [r for r in vif_states.values() if r.get("vif_config") == row_id],
        }
        for row_id, row in vif_configs.items()
    ]
    return {
        "schema_version": 1,
        "status": "draft_read_only",
        "writable": False,
        "pod_id": config["pod_id"],
        "source": "OpenSync",
        "management_transport": "OVSDB",
        "collected_at": utc_now(),
        "database": config["database"],
        "schema_origin": "retrieved_from_configured_endpoint",
        "physical_identity_verified": False,
        "connection_direction": config["connection"]["direction"],
        "configured_endpoint": config["connection"]["endpoint"],
        "trust": trust_evidence,
        "schema_fingerprint": schema.fingerprint,
        "schema_version_reported": schema.raw.get("version"),
        "session_generation": snap["generation"],
        "max_message_seen": snap["max_message_seen"],
        "reported_identifiers": identities,
        "identifier_cross_check": "pass"
        if expected and identity_match
        else "mismatch"
        if expected
        else "not_configured",
        "radios": radios,
        "vifs": vifs,
        "configuration_representations": {
            "modern_wpa_columns_present": modern,
            "legacy_security_column_present": legacy,
            "active_flags": [r for r in vif_configs.values()],
            "operation_support": "unqualified",
            "credential_values_collected": False,
        },
        "missing_observation_tables": [
            t for t in QUALIFICATION_COLUMNS if t not in schema.db.tables
        ],
        "radio_scope_candidates": [
            assess(
                rows,
                if_name=vif.get("if_name"),
                radio_name=radio.get("if_name"),
                ready=snap["ready"],
                credentials_available=False,
            )
            for radio in rows.get("Wifi_Radio_Config", {}).values()
            for uid, vif in vif_configs.items()
            if uid in radio.get("vif_configs", []) and vif.get("mode") == "ap"
        ],
        "remaining_checks": [
            "Confirm physical model/build and trusted session-to-pod binding.",
            "Select one existing AP radio/VIF; "
            "verify modern or legacy security semantics on that build.",
            "For EasyMesh WSC, qualify a sole existing BSS on its radio "
            "or map the complete radio request; "
            "one selected BSS on a shared radio is insufficient (EasyMesh 6.1 section 7.1).",
            "Verify current cloud/local writer controls "
            "and their persistence across reconnect/reboot.",
            "Record wired/wireless management path, "
            "shared BSS/radio dependencies and recovery method.",
            "Qualify a guarded mapping preserving unrelated fields and VIF lifecycle.",
            "Supply an independent client and qualified write scenario after P0/M0.",
            "Real EasyMesh messages → EMOSA adapter → unchanged pod "
            "→ independent physical behavior.",
        ],
    }


async def collect(config, output_directory):
    session, trust_evidence = qualification_session(config)
    directory = Path(output_directory)
    try:
        if directory.exists():
            raise EmosaError(Reason.INVALID_INPUT, "qualification output directory must be new")
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        snap = await session.snapshot()
        profile = draft_profile(config, snap, trust_evidence)
        # A schema contains column definitions, not row credentials. Redacting
        # names such as wpa_psks here would corrupt the collected schema/hash.
        (directory / "schema.json").write_text(json.dumps(snap["schema"].raw, indent=2) + "\n")
        write_json(directory / "draft-profile.json", profile)
        write_json(
            directory / "artifact-manifest.json",
            {
                "schema_version": 1,
                "mode": "read-only",
                "artifacts": [
                    artifact(directory / name, directory)
                    for name in ("schema.json", "draft-profile.json")
                ],
            },
        )
        return {
            "schema_version": 1,
            "status": "draft_read_only",
            "writable": False,
            "profile": str(directory / "draft-profile.json"),
            "schema_fingerprint": snap["schema"].fingerprint,
            "identifier_cross_check": profile["identifier_cross_check"],
        }
    except (OSError, ConnectionError, TimeoutError) as exc:
        raise EmosaError(
            Reason.NOT_READY, "read-only collection could not complete; check endpoint and trust"
        ) from exc
    finally:
        await session.close()
