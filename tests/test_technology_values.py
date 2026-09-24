import pytest

from emosa.easymesh_payloads import (
    APHECapabilities,
    APHTCapabilities,
    APVHTCapabilities,
    DeviceInventory,
    InventoryRadio,
    decode_value,
    encode_value,
)
from emosa.errors import EmosaError
from emosa_lab.evaluation.payloads import describe

pytestmark = pytest.mark.unit
RUID = bytes.fromhex("020000014001")


@pytest.mark.parametrize(
    "payload,expected",
    [
        (APHTCapabilities(RUID, 0), "02000001400100"),
        (APHTCapabilities(RUID, 0x7E), "0200000140017e"),
        (APHTCapabilities(RUID, 0xFE), "020000014001fe"),
        # Eight asymmetric two-bit codes, in NSS 1..8 order: 0,1,2,3,0,1,2,3.
        (APVHTCapabilities(RUID, 0xE4E4, 0x1B1B, 0xFF, 0xF0), "020000014001e4e41b1bfff0"),
        (APVHTCapabilities(RUID, 0, 65535, 0, 0), "0200000140010000ffff0000"),
        (
            APHECapabilities(RUID, bytes.fromhex("12345678"), 0x24, 0x36),
            "02000001400104123456782436",
        ),
        (APHECapabilities(RUID, bytes(range(8)), 1, 0), "0200000140010800010203040506070100"),
        (APHECapabilities(RUID, bytes(range(8)), 2, 0), "0200000140010800010203040506070200"),
        (
            APHECapabilities(RUID, bytes(range(12)), 3, 0xFE),
            "0200000140010c000102030405060708090a0b03fe",
        ),
        (
            DeviceInventory(b"S\0", b"1.2", b"\xff", (InventoryRadio(RUID, b""),)),
            "02530003312e3201ff0102000001400100",
        ),
    ],
)
def test_source_derived_exact_values_and_every_truncation(payload, expected):
    value = bytes.fromhex(expected)
    assert encode_value(payload) == value
    assert decode_value(payload.kind, value) == payload
    for bad in [value[:n] for n in range(len(value))] + [value + b"\0"]:
        with pytest.raises(EmosaError):
            decode_value(payload.kind, bad)


def test_vht_uses_big_endian_map_words_and_low_bits_for_nss1():
    result = describe(decode_value(0x87, bytes.fromhex("020000014001e4e41b1bfff0")))
    assert result["tx_mcs_codes"] == [0, 1, 2, 3, 0, 1, 2, 3]
    assert result["rx_mcs_codes"] == [3, 2, 1, 0, 3, 2, 1, 0]
    assert result["max_tx_streams"] == result["max_rx_streams"] == 8
    assert all(
        result[n]
        for n in (
            "short_gi_80",
            "short_gi_160",
            "vht8080",
            "vht160",
            "su_beamformer",
            "mu_beamformer",
        )
    )


def test_he_bytes_remain_opaque_and_inconsistent_length_is_visible_not_repaired():
    payload = decode_value(0x88, RUID + bytes.fromhex("041234567803fe"))
    result = describe(payload)
    assert result["mcs_hex"] == "12345678"
    assert result["mcs_interpretation"] == "opaque_mapping_pending"
    assert not result["mcs_length_matches_width_flags"]
    assert "tx_mcs_codes" not in result and "rx_mcs_codes" not in result
    with pytest.raises(EmosaError):
        encode_value(payload)


@pytest.mark.parametrize(
    "kind,prefix,mask",
    [
        (0x86, RUID, 1),
        (0x87, RUID + b"\xff" * 4 + b"\0", 15),
        (0x88, RUID + b"\x04" + b"\0" * 5, 1),
    ],
)
def test_all_reserved_flag_patterns_preserved_but_never_emitted(kind, prefix, mask):
    for flags in range(256):
        value = prefix + bytes((flags,))
        payload = decode_value(kind, value)
        assert describe(payload)["reserved_bits"] == flags & mask
        if flags & mask:
            with pytest.raises(EmosaError):
                encode_value(payload)
        else:
            assert encode_value(payload) == value


@pytest.mark.parametrize("length", [0, 1, 3, 5, 6, 7, 9, 10, 11, 13, 255])
def test_he_rejects_lengths_not_composed_of_rx_tx_map_pairs(length):
    with pytest.raises(EmosaError):
        decode_value(0x88, RUID + bytes((length,)) + b"\0" * length + b"\0\0")


def test_inventory_preserves_opaque_octets_and_counts_bytes():
    value = DeviceInventory(
        b"\xff" * 64,
        "é".encode() * 32,
        b"",
        (InventoryRadio(RUID, b"\0" * 64), InventoryRadio(b"123456", b"B")),
    )
    assert decode_value(0xD4, encode_value(value)) == value
    assert describe(value)["serial_number_hex"] == "ff" * 64
    for bad in (b"\x41" + b"x" * 65, b"\0\0\0\0"):
        with pytest.raises(EmosaError):
            decode_value(0xD4, bad)


@pytest.mark.parametrize(
    "payload",
    [
        APHTCapabilities(RUID, True),
        APHTCapabilities(b"short", 0),
        APVHTCapabilities(RUID, True, 0, 0, 0),
        APVHTCapabilities(RUID, -1, 0, 0, 0),
        APVHTCapabilities(RUID, 65536, 0, 0, 0),
        APVHTCapabilities(RUID, 1.0, 0, 0, 0),
        APVHTCapabilities(RUID, 0, False, 0, 0),
        APVHTCapabilities(RUID, 0, 0, False, 0),
        APHECapabilities(RUID, bytearray(4), 0, 0),
        APHECapabilities(RUID, b"\0" * 4, 0, True),
        DeviceInventory("serial", b"", b"", (InventoryRadio(RUID, b""),)),
        DeviceInventory(b"x" * 65, b"", b"", (InventoryRadio(RUID, b""),)),
        DeviceInventory(b"", b"", b"", ()),
        DeviceInventory(b"", b"", b"", []),
        DeviceInventory(b"", b"", b"", (None,)),
        DeviceInventory(b"", b"", b"", (InventoryRadio(b"short", b""),)),
        DeviceInventory(b"", b"", b"", (InventoryRadio(RUID, b"x" * 65),)),
    ],
)
def test_invalid_outbound_objects_fail_closed(payload):
    with pytest.raises(EmosaError):
        encode_value(payload)
