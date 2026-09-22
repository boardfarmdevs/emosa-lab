"""IEEE 802.11-2024 Supported HE-MCS And NSS Set field, not a complete IE.

Sections 9.2.2 and 9.4.2.247.4, Figure 9-901, Tables 9-377/378: Rx then Tx,
little-endian 16-bit maps; optional pairs follow the explicit width support.
This representation must not be used as the reordered EasyMesh 0x88 field.
"""

from dataclasses import dataclass

from emosa.errors import EmosaError, Reason


def _invalid():
    return EmosaError(Reason.INVALID_INPUT, "invalid supported HE MCS field")


@dataclass(frozen=True)
class HEMCSPair:
    # Eight codes, for NSS 1 through 8. 0/1/2 mean MCS 0..7/9/11;
    # 3 means unsupported, not a maximum MCS of 3.
    rx: tuple[int, ...]
    tx: tuple[int, ...]


@dataclass(frozen=True)
class HESupportedMCS:
    up_to_80: HEMCSPair
    mhz160: HEMCSPair | None = None
    mhz80plus80: HEMCSPair | None = None


def _map_bytes(codes):
    if type(codes) is not tuple or len(codes) != 8:
        raise _invalid()
    if any(type(code) is not int or not 0 <= code <= 3 for code in codes):
        raise _invalid()
    return sum(code << (2 * n) for n, code in enumerate(codes)).to_bytes(2, "little")


def encode_he_mcs(value: HESupportedMCS) -> bytes:
    """Derive width presence from explicit pairs; make no radio support claims."""
    if type(value) is not HESupportedMCS or type(value.up_to_80) is not HEMCSPair:
        raise _invalid()
    result = b""
    for pair in (value.up_to_80, value.mhz160, value.mhz80plus80):
        if pair is None:
            continue
        if type(pair) is not HEMCSPair:
            raise _invalid()
        result += _map_bytes(pair.rx) + _map_bytes(pair.tx)
    return result


def decode_he_mcs(value: bytes, *, he160: bool, he8080: bool) -> HESupportedMCS:
    """Widths are required: an eight-octet field alone cannot identify its pair."""
    if type(he160) is not bool or type(he8080) is not bool or type(value) is not bytes:
        raise _invalid()
    if len(value) != 4 + 4 * he160 + 4 * he8080:
        raise _invalid()
    pairs = []
    for offset in range(0, len(value), 4):
        rx = int.from_bytes(value[offset : offset + 2], "little")
        tx = int.from_bytes(value[offset + 2 : offset + 4], "little")
        pairs.append(
            HEMCSPair(
                tuple((rx >> (2 * n)) & 3 for n in range(8)),
                tuple((tx >> (2 * n)) & 3 for n in range(8)),
            )
        )
    return HESupportedMCS(pairs[0], pairs[1] if he160 else None, pairs[-1] if he8080 else None)


def describe_he_mcs(value: HESupportedMCS) -> dict:
    encode_he_mcs(value)  # Validate programmatically constructed objects too.
    result = {}
    for name, pair in (
        ("up_to_80_mhz", value.up_to_80),
        ("160_mhz", value.mhz160),
        ("80_plus_80_mhz", value.mhz80plus80),
    ):
        result[name] = (
            {"rx_codes_nss_1_to_8": list(pair.rx), "tx_codes_nss_1_to_8": list(pair.tx)}
            if pair is not None
            else None
        )
    return result
