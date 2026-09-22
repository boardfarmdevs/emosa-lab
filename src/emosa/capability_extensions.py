"""Selected synthetic capability inputs, after the Basic input/topology gate.

No driver inference or physical qualification. HE support stays explicit and
blocks technology readiness until HE/Wi-Fi 6 mapping is implemented.
"""

import hashlib

from emosa.easymesh_payloads import (
    APHTCapabilities,
    APVHTCapabilities,
    DeviceInventory,
    InventoryRadio,
    encode_value,
)
from emosa.errors import EmosaError
from emosa.evaluation.payloads import describe


class Unavailable(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise Unavailable(reason)


def missing(reason):
    return {"ready": False, "blockers": [reason], "values": []}


def unavailable():
    return {
        "technology": missing("basic_capability_context_unavailable"),
        "device_inventory": missing("basic_capability_context_unavailable"),
    }


def _value(payload):
    raw = encode_value(payload)
    return {
        "type": f"0x{payload.kind:02x}",
        "value_hex": raw.hex(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "decoded": describe(payload),
    }


def _evidence(profile, identifier, scope):
    require(
        any(e["id"] == identifier and scope in e["covers"] for e in profile["evidence"]),
        "extension_evidence_scope_missing",
    )


def _inventory(rows, observed):
    require(
        len({r["radio_id"] for r in rows}) == len(rows)
        and {r["radio_id"] for r in rows} == {r["radio_id"] for r in observed},
        "extension_radio_inventory_mismatch",
    )


def _streams(value, maximum):
    require(type(value) is int and 1 <= value <= maximum, "invalid_stream_count")
    return value - 1


def _bits(facts, names):
    require(all(type(facts[n]) is bool for n in names), "invalid_feature_flag")
    return sum(int(facts[name]) << bit for name, bit in names.items())


def _mcs(codes, streams):
    require(
        len(codes) == 8 and all(type(c) is int and 0 <= c <= 3 for c in codes),
        "invalid_vht_mcs_codes",
    )
    require(
        max((n + 1 for n, c in enumerate(codes) if c != 3), default=0) == streams,
        "vht_mcs_stream_count_mismatch",
    )
    return sum(code << (2 * n) for n, code in enumerate(codes))


def _technology(profile, observed):
    rows = profile.get("extensions", {}).get("technology")
    require(rows is not None, "technology_input_not_configured")
    _inventory(rows, observed)
    values = []
    for row in sorted(rows, key=lambda r: r["radio_id"]):
        _evidence(profile, row["evidence_id"], "technology")
        basic = next(r for r in profile["radios"] if r["radio_id"] == row["radio_id"])
        classes = {op["operating_class"] for op in basic["operating_classes"]}
        ruid = bytes.fromhex(
            next(r["ruid"] for r in observed if r["radio_id"] == row["radio_id"]).replace(":", "")
        )
        require(
            all(row[key] is not None for key in ("ht", "vht", "he")), "technology_support_unknown"
        )
        require(row["he"] is False, "he_and_wifi6_mapping_pending")
        if row["ht"] is not False:
            h = row["ht"]
            flags = (_streams(h["max_tx_streams"], 4) << 6) | (
                _streams(h["max_rx_streams"], 4) << 4
            )
            flags |= _bits(h, {"short_gi_20": 3, "short_gi_40": 2, "ht40": 1})
            require(not h["short_gi_40"] or h["ht40"], "ht_width_flag_mismatch")
            require(not h["ht40"] or bool(classes & {83, 84, 116, 117}), "ht_width_class_missing")
            values.append(_value(APHTCapabilities(ruid, flags)))
        if row["vht"] is not False:
            v = row["vht"]
            require(128 in classes, "vht_80mhz_class_missing")
            require(not v["vht160"] and not v["vht8080"], "vht_wider_class_review_pending")
            flags = (_streams(v["max_tx_streams"], 8) << 5) | (
                _streams(v["max_rx_streams"], 8) << 2
            )
            flags |= _bits(v, {"short_gi_80": 1, "short_gi_160": 0})
            features = _bits(v, {"vht8080": 7, "vht160": 6, "su_beamformer": 5, "mu_beamformer": 4})
            require(not v["short_gi_160"] or v["vht160"] or v["vht8080"], "vht_width_flag_mismatch")
            values.append(
                _value(
                    APVHTCapabilities(
                        ruid,
                        _mcs(v["tx_mcs_codes"], v["max_tx_streams"]),
                        _mcs(v["rx_mcs_codes"], v["max_rx_streams"]),
                        flags,
                        features,
                    )
                )
            )
    return values


def _text(value):
    # Synthetic OVSDB/profile strings use explicit UTF-8; Table 99's codec
    # remains byte preserving and does not impose this as a normative charset.
    raw = value.encode("utf-8")
    require(1 <= len(raw) <= 64, "inventory_string_octet_limit")
    return raw


def _device(profile, observed, node):
    data = profile.get("extensions", {}).get("device_inventory")
    require(data is not None, "device_inventory_input_not_configured")
    _evidence(profile, data["evidence_id"], "device_inventory")
    _inventory(data["radios"], observed)
    require(data["serial_number"] == node.get("serial_number"), "inventory_serial_mismatch")
    require(data["software_version"] == node.get("firmware_version"), "inventory_firmware_mismatch")
    radios = tuple(
        InventoryRadio(
            bytes.fromhex(r["ruid"].replace(":", "")),
            _text(
                next(i["chipset_vendor"] for i in data["radios"] if i["radio_id"] == r["radio_id"])
            ),
        )
        for r in sorted(observed, key=lambda r: r["radio_id"])
    )
    return [
        _value(
            DeviceInventory(
                _text(data["serial_number"]),
                _text(data["software_version"]),
                _text(data["execution_env"]),
                radios,
            )
        )
    ]


def project(profile, observed, node):
    result = {}
    for name, build in (
        ("technology", lambda: _technology(profile, observed)),
        ("device_inventory", lambda: _device(profile, observed, node)),
    ):
        try:
            result[name] = {"ready": True, "blockers": [], "values": build()}
        except Unavailable as exc:
            result[name] = missing(str(exc))
        except (EmosaError, UnicodeError):
            result[name] = missing("invalid_extension_value")
    return result
