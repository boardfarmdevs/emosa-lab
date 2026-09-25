"""Evidence-bound synthetic radio capability mapping; no wire admission.

Hashes establish which operator-supplied claims were used, not their truth.
Physical qualification and complete EasyMesh profiles remain separate gates.
"""

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from emosa import capability_extensions
from emosa.config import validate
from emosa.easymesh_payloads import APRadioBasicCapabilities, BasicOperatingClass, encode_value
from emosa.errors import EmosaError
from emosa.opensync.topology import project as project_topology
from emosa.operating_classes import CENTER_CHANNEL_CLASSES, GLOBAL_OPERATING_CLASSES
from emosa.payload_description import describe
from emosa.topology_bindings import binding_digest

PROFILE_BYTES = 65_536
EVIDENCE_BYTES = 1_048_576


class InputUnavailable(Exception):
    pass


def require(condition, code):
    if not condition:
        raise InputUnavailable(code)


def _read(path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "input_not_regular_file")
            data = stream.read(limit + 1)
    except OSError:
        raise InputUnavailable("input_file_unavailable") from None
    require(len(data) <= limit, "input_byte_budget_exceeded")
    return data


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def load_inputs(reference):
    """Read pinned regular files on every request. Never echo paths or file contents."""
    require(reference is not None, "capability_input_not_configured")
    path = Path(reference["path"])
    data = _read(path, PROFILE_BYTES)
    require(hashlib.sha256(data).hexdigest() == reference["sha256"], "profile_digest_mismatch")
    try:
        profile = json.loads(data, object_pairs_hook=_object)
        validate("radio-capabilities", profile)
    except (ValueError, RecursionError, EmosaError):
        raise InputUnavailable("invalid_capability_contract") from None
    require(profile["source_kind"] == "synthetic_fixture", "physical_qualification_pending")
    evidence = profile["evidence"]
    require(len({e["id"] for e in evidence}) == len(evidence), "duplicate_evidence_id")
    for item in evidence:
        raw = _read(path.parent / item["file"], EVIDENCE_BYTES)
        require(hashlib.sha256(raw).hexdigest() == item["sha256"], "evidence_digest_mismatch")
    return profile


def unavailable(pod_id, binding, code):
    return {
        "schema_version": 1,
        "pod_id": pod_id,
        "interface": "local-diagnostic",
        "backend_mode": "ovsdb-sim",
        "scope": "AP Radio Basic Capabilities values from explicit synthetic inputs",
        "ready_scope": "basic_radio_capabilities_only",
        "extension_scope": (
            "Selected HT/VHT, Wi-Fi 6 companion and Device Inventory mapping; "
            "complete HE technology set pending, EHT unassessed"
        ),
        "binding_sha256": binding_digest(binding) if binding else None,
        "ready": False,
        "blockers": [code],
        "radios": [],
        "extensions": capability_extensions.unavailable(),
        "snapshot_fresh": False,
        "input_assurance": "operator-declared synthetic facts; digests are not attestation",
        "complete_ap_capability_report": False,
        "qualified_easymesh_profile": None,
        "easymesh_wire_state": "blocked_P0",
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
    }


def project(pod_id, raw, binding, profile, reference, *, state_provenance, now=None):
    report = unavailable(pod_id, binding, "topology_binding_not_configured")
    topology = project_topology(pod_id, raw, binding, state_provenance=state_provenance)
    for field in ("snapshot_fresh", "generation", "revision", "observed_at", "schema_fingerprint"):
        report[field] = topology[field]
    try:
        require(topology["ready"], "observed_topology_unavailable")
        now = now or datetime.now(UTC)
        issued = datetime.fromisoformat(profile["issued_at"])
        expires = datetime.fromisoformat(profile["expires_at"])
        require(issued <= now < expires, "capability_input_outside_validity_window")
        require(profile["binding_sha256"] == binding_digest(binding), "capability_binding_mismatch")
        require(profile["pod_id"] == pod_id, "capability_pod_mismatch")
        require(
            profile["schema_fingerprint"] == raw["schema"].fingerprint,
            "capability_schema_mismatch",
        )
        require(
            profile["complete_operating_class_inventory"], "operating_class_inventory_incomplete"
        )
        node = next(iter(raw["tables"]["AWLAN_Node"].values()))
        device = raw["schema"].row(
            "AWLAN_Node", {k: node[k] for k in ("model", "firmware_version") if k in node}
        )
        require(device == profile["device"], "capability_device_mismatch")
        supplied = profile["radios"]
        require(
            len({r["radio_id"] for r in supplied}) == len(supplied)
            and {r["radio_id"] for r in supplied} == {r["radio_id"] for r in topology["radios"]},
            "capability_radio_inventory_mismatch",
        )
        rows = [
            raw["schema"].row(
                "Wifi_Radio_State", {k: v for k, v in row.items() if k in {"if_name", "country"}}
            )
            for row in raw["tables"]["Wifi_Radio_State"].values()
        ]
        radios = []
        for observed in topology["radios"]:
            facts = next(r for r in supplied if r["radio_id"] == observed["radio_id"])
            state = next(r for r in rows if r["if_name"] == observed["if_name"])
            require(state.get("country") == facts["country"], "regulatory_context_mismatch")
            require(facts["ruid"] == observed["ruid"], "capability_ruid_mismatch")
            require(
                facts["evidence_id"] in {e["id"] for e in profile["evidence"]},
                "capability_evidence_missing",
            )
            radios.append(_radio(facts, observed))
    except InputUnavailable as exc:
        report["blockers"] = [str(exc)]
        return report
    report.update(
        ready=True,
        blockers=[],
        radios=radios,
        profile_sha256=reference["sha256"],
        evidence=[{"id": e["id"], "sha256": e["sha256"]} for e in profile["evidence"]],
        issued_at=profile["issued_at"],
        expires_at=profile["expires_at"],
        extensions=capability_extensions.project(
            profile,
            topology["radios"],
            raw["schema"].row(
                "AWLAN_Node",
                {k: node[k] for k in ("serial_number", "firmware_version") if k in node},
            ),
        ),
    )
    return report


def _radio(facts, observed):
    ops = facts["operating_classes"]
    require(type(facts["max_bss"]) is int, "capability_integer_required")
    require(len({o["operating_class"] for o in ops}) == len(ops), "duplicate_operating_class")
    capable = []
    classes = []
    for op in sorted(ops, key=lambda o: o["operating_class"]):
        require(
            all(
                type(v) is int
                for v in [op["operating_class"], op["max_eirp_dbm"], *op["non_operable_channels"]]
            ),
            "capability_integer_required",
        )
        number = op["operating_class"]
        require(number in GLOBAL_OPERATING_CLASSES, "operating_class_not_audited")
        band, all_channels = GLOBAL_OPERATING_CLASSES[number]
        excluded = op["non_operable_channels"]
        require(set(excluded) <= set(all_channels), "non_operable_channel_outside_class")
        channels = set(all_channels) - set(excluded)
        require(bool(channels), "supported_class_has_no_operable_channels")
        if number not in CENTER_CHANNEL_CLASSES:
            capable.extend((band, c) for c in channels)
        classes.append(BasicOperatingClass(number, op["max_eirp_dbm"], tuple(sorted(excluded))))
    active_bsses = sum(v["mode"] == "ap" and v["enabled"] for v in observed["interfaces"])
    require(active_bsses <= facts["max_bss"], "observed_bsses_exceed_claimed_capacity")
    if observed["enabled"]:
        require(
            type(observed["channel"]) is int
            and (observed["freq_band"], observed["channel"]) in capable,
            "observed_channel_not_supported_by_input",
        )
    payload = APRadioBasicCapabilities(
        bytes.fromhex(facts["ruid"].replace(":", "")), facts["max_bss"], tuple(classes)
    )
    value = encode_value(payload)
    return {
        "radio_id": facts["radio_id"],
        "country": facts["country"],
        "evidence_id": facts["evidence_id"],
        "decoded": describe(payload),
        "value": {
            "type": "0x85",
            "value_hex": value.hex(),
            "sha256": hashlib.sha256(value).hexdigest(),
        },
    }
