"""Offline inspection of selected TLV values, with no runtime or pod connection."""

import hashlib
import os
import stat
from pathlib import Path

from emosa.easymesh_payloads import (
    MAX_VALUE_BYTES,
    APOperationalBss,
    APRadioBasicCapabilities,
    MultiAPProfile,
    RadioIdentifier,
    SearchedServices,
    SupportedServices,
    decode_value,
)
from emosa.errors import EmosaError, Reason


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
