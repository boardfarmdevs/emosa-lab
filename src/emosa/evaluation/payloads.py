"""Offline inspection of selected TLV values, with no runtime or pod connection."""

import hashlib
import os
import stat
from pathlib import Path

from emosa.easymesh_payloads import (
    ADVANCED_FEATURE_BITS,
    AP_FEATURE_BITS,
    MAX_VALUE_BYTES,
    PROFILE2_FEATURE_BITS,
    APCapability,
    APHECapabilities,
    APHTCapabilities,
    APOperationalBss,
    APRadioAdvancedCapabilities,
    APRadioBasicCapabilities,
    APVHTCapabilities,
    APWifi6Capabilities,
    DeviceInventory,
    MultiAPProfile,
    Profile2APCapability,
    RadioIdentifier,
    SearchedServices,
    SupportedServices,
    decode_value,
)
from emosa.errors import EmosaError, Reason
from emosa.he_mcs import describe_he_mcs, encode_he_mcs


def read_value(*, value_hex: str | None = None, value_file: Path | None = None) -> bytes:
    """Bound both text decoding and file reads; never include input bytes in errors."""
    if (value_hex is None) == (value_file is None):
        raise EmosaError(Reason.INVALID_INPUT, "provide one value input")
    if value_hex is not None:
        if len(value_hex) > MAX_VALUE_BYTES * 2:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "hex input exceeds local byte budget")
        try:
            return bytes.fromhex(value_hex)
        except ValueError:
            raise EmosaError(Reason.INVALID_INPUT, "invalid hexadecimal value input") from None
    try:
        # Restrict the CLI to regular files, not FIFOs or devices that may block.
        descriptor = os.open(value_file, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError
            value = stream.read(MAX_VALUE_BYTES + 1)
    except (OSError, ValueError):
        raise EmosaError(Reason.INVALID_INPUT, "value file is not readable") from None
    if len(value) > MAX_VALUE_BYTES:
        raise EmosaError(Reason.UNSUPPORTED_OPERATION, "value file exceeds local byte budget")
    return value


def describe(payload):
    if isinstance(payload, APWifi6Capabilities):
        return {
            "ruid": payload.ruid.hex(":"),
            "mcs_interpretation": "ieee_80211_2024_figure_9_901_rx_then_tx_little_endian_maps",
            "roles": [
                {
                    "role": role.role,
                    "known_role": {0: "ap", 1: "non_ap_sta"}.get(role.role),
                    "he160": role.mcs.mhz160 is not None,
                    "he8080": role.mcs.mhz80plus80 is not None,
                    "mcs_length": len(encode_he_mcs(role.mcs)),
                    "mcs_hex": encode_he_mcs(role.mcs).hex(),
                    "mcs": describe_he_mcs(role.mcs),
                    "beamforming_flags": role.beamforming_flags,
                    "max_dl_mu_mimo_tx_users": role.mu_mimo_users >> 4,
                    "max_ul_mu_mimo_rx_users": role.mu_mimo_users & 15,
                    "max_dl_ofdma_tx_users": role.max_dl_ofdma_tx,
                    "max_ul_ofdma_rx_users": role.max_ul_ofdma_rx,
                    "user_limits_apply_to_ap_role": role.role == 0,
                    "feature_flags": role.feature_flags,
                    "features": {
                        name: bool(role.beamforming_flags & (1 << bit))
                        for name, bit in {
                            "su_beamformer": 7,
                            "su_beamformee": 6,
                            "mu_beamformer": 5,
                            "beamformee_sts_le_80": 4,
                            "beamformee_sts_gt_80": 3,
                            "ul_mu_mimo": 2,
                            "ul_ofdma": 1,
                            "dl_ofdma": 0,
                        }.items()
                    }
                    | {
                        name: bool(role.feature_flags & (1 << bit))
                        for name, bit in {
                            "rts": 7,
                            "mu_rts": 6,
                            "multi_bssid": 5,
                            "mu_edca": 4,
                            "twt_requester": 3,
                            "twt_responder": 2,
                            "spatial_reuse": 1,
                            "anticipated_channel_usage": 0,
                        }.items()
                    },
                }
                for role in payload.roles
            ],
        }
    if isinstance(payload, DeviceInventory):
        return {
            "serial_number_hex": payload.serial_number.hex(),
            "software_version_hex": payload.software_version.hex(),
            "execution_env_hex": payload.execution_env.hex(),
            "radios": [
                {"ruid": r.ruid.hex(":"), "chipset_vendor_hex": r.chipset_vendor.hex()}
                for r in payload.radios
            ],
        }
    if isinstance(payload, APHTCapabilities):
        return {
            "ruid": payload.ruid.hex(":"),
            "flags": payload.flags,
            "max_tx_streams": (payload.flags >> 6) + 1,
            "max_rx_streams": ((payload.flags >> 4) & 3) + 1,
            "short_gi_20": bool(payload.flags & 8),
            "short_gi_40": bool(payload.flags & 4),
            "ht40": bool(payload.flags & 2),
            "reserved_bits": payload.flags & 1,
        }
    if isinstance(payload, (APVHTCapabilities, APHECapabilities)):
        he = isinstance(payload, APHECapabilities)
        result = {
            "ruid": payload.ruid.hex(":"),
            "stream_flags": payload.stream_flags,
            "feature_flags": payload.feature_flags,
            "max_tx_streams": (payload.stream_flags >> 5) + 1,
            "max_rx_streams": ((payload.stream_flags >> 2) & 7) + 1,
            "reserved_bits": payload.feature_flags & (1 if he else 15),
        }
        if he:
            result.update(
                mcs_hex=payload.mcs.hex(),
                mcs_interpretation="opaque_mapping_pending",
                mcs_length_matches_width_flags=len(payload.mcs)
                == (4 + 4 * bool(payload.stream_flags & 1) + 4 * bool(payload.stream_flags & 2)),
            )
            result.update(
                he8080=bool(payload.stream_flags & 2), he160=bool(payload.stream_flags & 1)
            )
            bits = {
                "su_beamformer": 7,
                "mu_beamformer": 6,
                "ul_mu_mimo": 5,
                "ul_mu_mimo_ofdma": 4,
                "dl_mu_mimo_ofdma": 3,
                "ul_ofdma": 2,
                "dl_ofdma": 1,
            }
        else:
            result.update(
                tx_mcs=payload.tx_mcs,
                rx_mcs=payload.rx_mcs,
                tx_mcs_codes=[(payload.tx_mcs >> (2 * n)) & 3 for n in range(8)],
                rx_mcs_codes=[(payload.rx_mcs >> (2 * n)) & 3 for n in range(8)],
                short_gi_80=bool(payload.stream_flags & 2),
                short_gi_160=bool(payload.stream_flags & 1),
            )
            bits = {"vht8080": 7, "vht160": 6, "su_beamformer": 5, "mu_beamformer": 4}
        result.update(
            {name: bool(payload.feature_flags & (1 << bit)) for name, bit in bits.items()}
        )
        return result
    if isinstance(payload, (APCapability, Profile2APCapability, APRadioAdvancedCapabilities)):
        bits = (
            AP_FEATURE_BITS
            if isinstance(payload, APCapability)
            else PROFILE2_FEATURE_BITS
            if isinstance(payload, Profile2APCapability)
            else ADVANCED_FEATURE_BITS
        )
        result = {
            "flags": payload.flags,
            "features": {name: bool(payload.flags & (1 << bit)) for name, bit in bits.items()},
            "reserved_bits": payload.flags
            & (1 if isinstance(payload, APRadioAdvancedCapabilities) else 7),
        }
        if isinstance(payload, APRadioAdvancedCapabilities):
            result["ruid"] = payload.ruid.hex(":")
        if isinstance(payload, Profile2APCapability):
            units = payload.byte_counter_units
            result.update(
                max_prioritization_rules=payload.max_prioritization_rules,
                max_vids=payload.max_vids,
                reserved_octet=payload.reserved_octet,
                byte_counter_units=units,
                counter_unit={0: "bytes", 1: "KiB", 2: "MiB"}.get(units),
                bytes_per_counter_unit={0: 1, 1: 1024, 2: 1048576}.get(units),
            )
        return result
    if isinstance(payload, APRadioBasicCapabilities):
        return {
            "ruid": payload.ruid.hex(":"),
            "max_bss": payload.max_bss,
            "operating_classes": [
                {
                    "operating_class": op.operating_class,
                    "max_eirp_dbm": op.max_eirp_dbm,
                    "non_operable_channels": list(op.non_operable_channels),
                }
                for op in payload.operating_classes
            ],
        }
    if isinstance(payload, (SupportedServices, SearchedServices)):
        return {
            "services": list(payload.services),
            "known_services": list(payload.known_services),
            "reserved_services": [v for v in payload.services if v not in payload.known_services],
        }
    if isinstance(payload, RadioIdentifier):
        return {"ruid": payload.ruid.hex(":")}
    if isinstance(payload, MultiAPProfile):
        return {"profile": payload.profile, "reserved": payload.profile not in (1, 2, 3)}
    if isinstance(payload, APOperationalBss):
        return {
            "radios": [
                {
                    "ruid": radio.ruid.hex(":"),
                    "bsses": [
                        {"ap_mac": bss.ap_mac.hex(":"), "ssid_hex": bss.ssid.hex()}
                        for bss in radio.bsses
                    ],
                }
                for radio in payload.radios
            ]
        }
    raise EmosaError(Reason.INVALID_INPUT, "unrecognized payload object")


def inspect_value(kind: int, value: bytes, *, receiver_profile: int | None = None) -> dict:
    payload = decode_value(kind, value)
    decoded = describe(payload)
    if receiver_profile is not None:
        if not isinstance(payload, MultiAPProfile):
            raise EmosaError(Reason.INVALID_INPUT, "receiver profile applies only to type 0xb3")
        decoded["effective_profile"] = payload.effective_profile(receiver_profile)
        decoded["receiver_profile"] = receiver_profile
    return {
        "evidence_level": "standalone_easymesh_tlv_value",
        "type": f"0x{kind:02x}",
        "byte_length": len(value),
        "sha256": hashlib.sha256(value).hexdigest(),
        "decoded": decoded,
        "wire_envelope_validated": False,
        "controller_onboarding_by_emosa_proven": False,
        "physical_pod_proven": False,
    }
