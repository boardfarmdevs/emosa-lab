import hashlib
import json
import os
from pathlib import Path

import pytest

from emosa import easymesh_payloads as em
from emosa.errors import EmosaError, Reason
from emosa_lab.evaluation.cli import main
from emosa_lab.evaluation.payloads import describe, inspect_value, read_value

pytestmark = pytest.mark.unit
FIXTURE = Path(__file__).parent / "fixtures/protocol/easymesh"
NATIVE = json.loads((FIXTURE / "native-values.json").read_text())["cases"]
RUID = bytes.fromhex("020000ec0200")


def test_independent_native_fixture_provenance():
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    for path, key in (
        (FIXTURE / "native-values.json", "vectors_sha256"),
        (Path(provenance["source_capture"]), "capture_sha256"),
        (Path(provenance["extractor"]), "extractor_sha256"),
    ):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == provenance[key]


@pytest.mark.parametrize("case", NATIVE, ids=lambda c: f"native-frame-{c['frame']}-{c['type']}")
def test_decode_and_encode_against_independent_native_bytes_and_fields(case):
    value = bytes.fromhex(case["value_hex"])
    decoded = em.decode_value(int(case["type"], 16), value)
    assert {key: describe(decoded)[key] for key in case["decoded"]} == case["decoded"]
    if isinstance(decoded, em.SupportedServices) and 0xA1 in decoded.services:
        # This sender uses a reserved service code. We must ignore that role on
        # receipt and must not reproduce it when building a new outbound value.
        assert decoded.known_services == (1,)
        with pytest.raises(EmosaError):
            em.encode_value(decoded)
    else:
        assert em.encode_value(decoded) == value


# Hand-calculated value octets from the selected field tables, distinct from
# captured implementation examples. These deliberately include non-text SSIDs.
GOLDEN = [
    (em.SupportedServices(()), "00"),
    (em.SupportedServices((1,)), "0101"),
    (em.SupportedServices((0, 1)), "020001"),
    (em.SearchedServices(()), "00"),
    (em.SearchedServices((0,)), "0100"),
    (em.RadioIdentifier(RUID), "020000ec0200"),
    (em.MultiAPProfile(1), "01"),
    (em.MultiAPProfile(2), "02"),
    (em.MultiAPProfile(3), "03"),
    (em.APOperationalBss(()), "00"),
    (em.APOperationalBss((em.OperationalRadio(RUID, ()),)), "01020000ec020000"),
    (
        em.APOperationalBss(
            (
                em.OperationalRadio(
                    RUID,
                    (
                        em.OperationalBss(bytes.fromhex("020000ec0201"), b"\xff\0A"),
                        em.OperationalBss(bytes.fromhex("020000ec0202"), "é".encode()),
                    ),
                ),
                em.OperationalRadio(bytes.fromhex("020000ec0300"), ()),
            )
        ),
        "02020000ec020002020000ec020103ff0041020000ec020202c3a9020000ec030000",
    ),
]


@pytest.mark.parametrize("payload,hex_value", GOLDEN)
def test_selected_table_layouts_have_exact_expected_bytes(payload, hex_value):
    value = bytes.fromhex(hex_value)
    assert em.encode_value(payload) == value
    assert em.decode_value(payload.kind, value) == payload


@pytest.mark.parametrize("case", NATIVE, ids=lambda c: f"prefix-frame-{c['frame']}-{c['type']}")
def test_every_truncation_and_extra_octet_fails_closed(case):
    kind = int(case["type"], 16)
    value = bytes.fromhex(case["value_hex"])
    for bad in [value[:length] for length in range(len(value))] + [value + b"\0"]:
        with pytest.raises(EmosaError) as error:
            em.decode_value(kind, bad)
        assert error.value.code == Reason.INVALID_INPUT


def test_all_reserved_profiles_use_explicit_receiver_without_changing_raw_value():
    for value in range(256):
        payload = em.decode_value(0xB3, bytes((value,)))
        for receiver in (1, 2, 3):
            assert payload.effective_profile(receiver) == (
                value if value in (1, 2, 3) else receiver
            )
        assert payload.profile == value
        if value not in (1, 2, 3):
            with pytest.raises(EmosaError):
                em.encode_value(payload)
    for bad in (0, 4, True, None, "1"):
        with pytest.raises(EmosaError):
            em.MultiAPProfile(255).effective_profile(bad)


def test_reserved_services_are_ignored_for_roles_and_forbidden_for_senders():
    for kind, known in ((0x80, (0, 1)), (0x81, (0,))):
        for value in range(256):
            payload = em.decode_value(kind, bytes((1, value)))
            assert payload.services == (value,)
            assert payload.known_services == ((value,) if value in known else ())
            if value not in known:
                with pytest.raises(EmosaError):
                    em.encode_value(payload)


@pytest.mark.parametrize("length", [0, 32])
def test_ssid_octet_boundaries_are_preserved_without_unicode_assumptions(length):
    value = bytes.fromhex("01020000ec020001020000ec0201") + bytes((length,)) + b"\xff" * length
    payload = em.decode_value(0x83, value)
    assert payload.radios[0].bsses[0].ssid == b"\xff" * length
    assert em.encode_value(payload) == value


@pytest.mark.parametrize("offset,replacement", [(0, 0), (0, 2), (7, 0), (7, 2), (14, 33), (14, 2)])
def test_inconsistent_nested_counts_or_ssid_lengths_rejected(offset, replacement):
    value = bytearray.fromhex("01020000ec020001020000ec02010141")
    value[offset] = replacement
    with pytest.raises(EmosaError):
        em.decode_value(0x83, bytes(value))


@pytest.mark.parametrize(
    "payload",
    [
        em.SupportedServices([1]),
        em.SupportedServices((True,)),
        em.SupportedServices((-1,)),
        em.SupportedServices((256,)),
        em.SupportedServices((1,) * 256),
        em.SearchedServices((1,)),
        em.RadioIdentifier(b"short"),
        em.RadioIdentifier("02:00:00:ec:02:00"),
        em.MultiAPProfile(True),
        em.MultiAPProfile(4),
        em.APOperationalBss((em.OperationalRadio(RUID, ()),) * 256),
        em.APOperationalBss((em.OperationalRadio(RUID, (em.OperationalBss(RUID, b"x"),) * 256),)),
        em.APOperationalBss((em.OperationalRadio(RUID, (em.OperationalBss(RUID, b"x" * 33),)),)),
        em.APOperationalBss(
            (em.OperationalRadio(RUID, (em.OperationalBss(RUID, "private-ssid"),)),)
        ),
        em.APOperationalBss((em.OperationalRadio(b"short", ()),)),
        em.APOperationalBss((em.OperationalRadio(RUID, (em.OperationalBss(b"short", b"x"),)),)),
        em.APOperationalBss((em.OperationalRadio(RUID, (None,)),)),
        em.APOperationalBss((None,)),
        None,
    ],
    ids=lambda value: type(value).__name__,
)
def test_invalid_outbound_objects_fail_without_leaking_values(payload):
    with pytest.raises(EmosaError) as error:
        em.encode_value(payload)
    assert error.value.code == Reason.INVALID_INPUT
    assert error.value.public() == {
        "code": "INVALID_INPUT",
        "message": "invalid EasyMesh TLV value",
        "details": {},
    }


def test_count_255_is_representable_without_claiming_unique_real_resources():
    payload = em.APOperationalBss((em.OperationalRadio(RUID, ()),) * 255)
    assert em.decode_value(0x83, em.encode_value(payload)) == payload
    radio = em.OperationalRadio(RUID, (em.OperationalBss(RUID, b"x"),) * 255)
    payload = em.APOperationalBss((radio,))
    assert em.decode_value(0x83, em.encode_value(payload)) == payload


def test_byte_budget_is_a_local_limit_not_a_normative_invalidity():
    radio = em.OperationalRadio(RUID, (em.OperationalBss(RUID, b"x" * 32),) * 255)
    for action in (
        lambda: em.encode_value(em.APOperationalBss((radio, radio))),
        lambda: em.decode_value(0x83, b"\0" * (em.MAX_VALUE_BYTES + 1)),
    ):
        with pytest.raises(EmosaError) as error:
            action()
        assert error.value.code == Reason.UNSUPPORTED_OPERATION
        assert "local byte budget" in str(error.value)


@pytest.mark.parametrize("kind,value", [(True, b"\0"), (-1, b"\0"), (256, b"\0"), (0x80, "00")])
def test_invalid_api_inputs_have_component_errors(kind, value):
    with pytest.raises(EmosaError) as error:
        em.decode_value(kind, value)
    assert error.value.code == Reason.INVALID_INPUT


def test_unsupported_type_is_not_silently_interpreted():
    with pytest.raises(EmosaError) as error:
        em.decode_value(0x89, b"\0")
    assert error.value.code == Reason.UNSUPPORTED_OPERATION


def test_offline_cli_reports_value_without_runtime_state_or_transport(tmp_path, capsys):
    value_file = tmp_path / "value.bin"
    value_file.write_bytes(bytes.fromhex("ff"))
    state = tmp_path / "state-not-created"
    assert (
        main(
            [
                "--state-dir",
                str(state),
                "payload",
                "--type",
                "0xb3",
                "--value-file",
                str(value_file),
                "--receiver-profile",
                "2",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["decoded"] == {
        "profile": 255,
        "reserved": True,
        "receiver_profile": 2,
        "effective_profile": 2,
    }
    assert result["sha256"] == hashlib.sha256(b"\xff").hexdigest()
    assert not result["wire_envelope_validated"]
    assert not result["controller_onboarding_by_emosa_proven"]
    assert not result["physical_pod_proven"]
    assert not state.exists()
    assert main(["payload", "--type", "0x80", "--value-hex", "0101"]) == 0
    assert json.loads(capsys.readouterr().out)["decoded"]["known_services"] == [1]


def test_cli_rejects_lxd_execution_invalid_hex_and_full_tlv(capsys):
    for args in (
        ["--execution", "lxd", "payload", "--type", "0x80", "--value-hex", "0101"],
        ["payload", "--type", "0x80", "--value-hex", "private-invalid-data"],
        ["payload", "--type", "0x80", "--value-hex", "8000020101"],
    ):
        assert main(args) != 0
        result = capsys.readouterr()
        assert json.loads(result.out)["error"]["code"] == "INVALID_INPUT"
        assert "private-invalid-data" not in result.out + result.err


def test_file_and_hex_inputs_are_bounded_and_special_files_do_not_block(tmp_path):
    large = tmp_path / "large.bin"
    large.write_bytes(b"\0" * (em.MAX_VALUE_BYTES + 1))
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    for path in (large, fifo, tmp_path, tmp_path / "missing"):
        with pytest.raises(EmosaError):
            read_value(value_file=path)
    with pytest.raises(EmosaError):
        read_value(value_hex="00" * (em.MAX_VALUE_BYTES + 1))
    assert read_value(value_hex="01 01") == b"\x01\x01"


def test_reserved_service_diagnostics_and_profile_context_are_explicit():
    result = inspect_value(0x80, bytes.fromhex("0201a1"))
    assert result["decoded"] == {
        "services": [1, 161],
        "known_services": [1],
        "reserved_services": [161],
    }
    with pytest.raises(EmosaError):
        inspect_value(0x80, b"\x01\x01", receiver_profile=1)
