import json
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from emosa.errors import EmosaError, Reason
from emosa.wire.cmdu import (
    END,
    MULTICAST,
    DuplicateWindow,
    MidSequence,
    Reassembler,
    Tlv,
    decode_frame,
    fragment_message,
    parse_tlvs,
)
from emosa_lab.evaluation.cli import main
from emosa_lab.wire.inspection import discovery, inspect_capture, packets

pytestmark = pytest.mark.unit
SOURCE = bytes.fromhex("020000003001")
OTHER = bytes.fromhex("020000003002")


def frames(tlvs=None, *, mid=0x1234, mtu=1500, source=SOURCE):
    return fragment_message(
        MULTICAST,
        source,
        0,
        mid,
        [Tlv(1, SOURCE), Tlv(2, SOURCE)] if tlvs is None else tlvs,
        mtu=mtu,
    )


def test_literal_normative_discovery_vector_and_padding():
    expected = bytes.fromhex(
        "0180c2000013020000003001893a0000000012340080010006020000003001020006020000003001000000"
    )
    assert frames() == (expected,)
    result = Reassembler().feed(expected + b"\xff" * 17)
    assert discovery(result) == {
        "al_mac": "02:00:00:00:30:01",
        "interface_mac": "02:00:00:00:30:01",
    }
    assert result.mid == 0x1234 and result.fragments == 1


def test_reserved_fields_are_ignored_but_version_is_a_reserved_value():
    frame = bytearray(frames()[0])
    frame[15] = 0xFF
    frame[21] |= 0x3F
    frame[23] |= 0xC0  # reserved high length bits
    assert Reassembler().feed(bytes(frame)) == Reassembler().feed(frames()[0])
    frame[14] = 1
    with pytest.raises(EmosaError) as exc:
        Reassembler().feed(bytes(frame))
    assert exc.value.code == Reason.UNSUPPORTED_OPERATION


@pytest.mark.parametrize("size", [0, 13, 14, 21, 1515])
def test_truncated_or_oversized_envelope(size):
    with pytest.raises(EmosaError):
        decode_frame(bytes(size))


@pytest.mark.parametrize(
    "payload,final",
    [
        (b"", True),
        (b"\x01", True),
        (b"\x01\x00", True),
        (b"\x01\x00\x06abc", True),
        (b"\x00\x00\x01x", True),
        (END, False),
        (b"", False),
    ],
)
def test_malformed_tlv_or_end_marker(payload, final):
    with pytest.raises(EmosaError):
        parse_tlvs(payload, final=final)


def test_unknown_tlvs_ignored_and_base_duplicates_aggregate():
    parts = [
        Tlv(2, SOURCE),
        Tlv(0xEF, b"private-not-printed"),
        Tlv(1, SOURCE[:3]),
        Tlv(1, SOURCE[3:]),
    ]
    message = Reassembler().feed(frames(parts)[0])
    assert discovery(message)["al_mac"] == SOURCE.hex(":")
    with pytest.raises(EmosaError):
        discovery(Reassembler().feed(frames([Tlv(1, SOURCE)] * 2 + [Tlv(2, SOURCE)])[0]))


def test_wsc_occurrences_preserved_for_message_specific_interpreter():
    tlvs = [Tlv(0x11, b"first"), Tlv(0x11, b"second")]
    message = Reassembler().feed(fragment_message(OTHER, SOURCE, 9, 1, tlvs)[0])
    assert message.tlvs == tuple(tlvs)


def test_reorder_exact_duplicate_no_partial_dispatch_and_separate_peers():
    sequence = frames([Tlv(0x11, b"a" * 800), Tlv(0x11, b"b" * 800)])
    assert len(sequence) == 2 and len(sequence[0]) <= 1514
    parser = Reassembler()
    assert parser.feed(sequence[1]) is None
    assert parser.feed(sequence[1]) is None
    assert parser.feed(replace(decode_frame(sequence[0]), source=OTHER).encode()) is None
    message = parser.feed(sequence[0])
    assert [t.value[:1] for t in message.tlvs] == [b"a", b"b"]
    assert message.fragments == 2
    assert parser.buffered == 803  # unrelated peer is still incomplete


def test_fragment_ids_and_last_flag_and_only_final_eom():
    sequence = frames([Tlv(1, b"a" * 10)] * 3, mtu=24)
    assert [decode_frame(frame).fid for frame in sequence] == [0, 1, 2]
    assert [decode_frame(frame).last for frame in sequence] == [False, False, True]
    assert all(decode_frame(f).payload[-3:] != END for f in sequence[:-1])
    assert decode_frame(sequence[-1]).payload.endswith(END)


def test_conflict_poisoned_until_original_deadline_and_no_refresh_by_duplicates():
    now = [0.0]
    parser = Reassembler(clock=lambda: now[0], timeout=2)
    first, second = frames([Tlv(1, b"a" * 800), Tlv(2, b"b" * 800)])
    assert parser.feed(first) is None
    now[0] = 1.9
    assert parser.feed(first) is None
    corrupted = replace(decode_frame(first), payload=Tlv(1, b"c" * 800).encode()).encode()
    with pytest.raises(EmosaError, match="conflicting duplicate"):
        parser.feed(corrupted)
    assert parser.buffered == 0
    with pytest.raises(EmosaError, match="quarantined"):
        parser.feed(second)
    now[0] = 2.0
    assert parser.expire() == 1
    assert parser.feed(first) is None
    assert parser.feed(second) is not None


@pytest.mark.parametrize(
    "change",
    [
        {"relay": True},
        {"fid": 64},
        {"fid": 0, "last": True},
    ],
)
def test_mixed_fragment_headers_cannot_dispatch(change):
    first, last = frames([Tlv(1, b"a" * 800), Tlv(2, b"b" * 800)])
    parser = Reassembler()
    assert parser.feed(last) is None
    with pytest.raises(EmosaError):
        parser.feed(replace(decode_frame(first), **change).encode())


def test_bytes_contexts_and_tlv_budgets_do_not_evict_other_assemblies():
    first, last = frames([Tlv(1, b"a" * 800), Tlv(2, b"b" * 800)])
    parser = Reassembler(max_contexts=1)
    assert parser.feed(first) is None
    with pytest.raises(EmosaError) as exc:
        parser.feed(replace(decode_frame(first), mid=3).encode())
    assert exc.value.code == Reason.BUSY
    assert parser.feed(last).fragments == 2
    parser = Reassembler(max_message_bytes=1000, max_bytes=1000)
    assert parser.feed(first) is None
    with pytest.raises(EmosaError, match="byte budget"):
        parser.feed(last)
    assert parser.buffered == 0
    with pytest.raises(EmosaError, match="TLV count"):
        parse_tlvs(Tlv(1, b"").encode() * 257 + END, final=True)


def test_missing_fragment_does_not_complete_and_expires():
    now = [0]
    parser = Reassembler(clock=lambda: now[0])
    first = replace(decode_frame(frames()[0]), fid=2).encode()
    assert parser.feed(first) is None
    now[0] = 5
    assert parser.expire() == 1 and parser.buffered == 0


def test_sender_refuses_unqualified_octet_fragmentation():
    with pytest.raises(EmosaError, match="TLV too large"):
        frames([Tlv(0x11, b"a" * 1490)])
    with pytest.raises(EmosaError):
        Tlv(1, b"a" * 16384).encode()


def test_mid_wrap_and_al_based_duplicate_window_boundaries():
    seq = MidSequence(65534)
    assert [seq.next(), seq.next(), seq.next()] == [65535, 0, 1]
    now = [0]
    window = DuplicateWindow(clock=lambda: now[0], timeout=3, capacity=2)
    assert not window.seen(SOURCE, 0)
    assert window.seen(SOURCE, 0)
    assert not window.seen(OTHER, 0)
    with pytest.raises(EmosaError) as exc:
        window.seen(SOURCE, 1)
    assert exc.value.code == Reason.BUSY
    now[0] = 3
    assert not window.seen(SOURCE, 0)


def write_pcap(path, data, *, endian="<", linktype=1):
    path.write_bytes(
        struct.pack(endian + "IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype)
        + b"".join(
            struct.pack(endian + "IIII", 1, i, len(p), len(p)) + p for i, p in enumerate(data)
        )
    )


@pytest.mark.parametrize("endian", ["<", ">"])
def test_pcap_cli_keeps_values_private_and_makes_no_operations(tmp_path, capsys, endian):
    path = tmp_path / "test.pcap"
    write_pcap(path, frames() + frames([Tlv(0x11, b"private-passphrase")]), endian=endian)
    # Second envelope uses Discovery type but lacks its required identity fields.
    assert main(["wire-inspect", "--capture", str(path)]) == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert len(result["messages"]) == 1 and len(result["rejected"]) == 1
    assert "private-passphrase" not in output
    assert result["operations_created"] == 0 and not result["onboarding_proven"]


@pytest.mark.parametrize("payload", [b"", b"\x0a\x0d\x0d\x0a" + bytes(24)])
def test_bad_capture_formats(tmp_path, payload):
    path = tmp_path / "bad.pcap"
    path.write_bytes(payload)
    with pytest.raises(EmosaError):
        list(packets(path))


def test_retained_native_capture_has_real_headers_and_discovery():
    path = Path("doc/evidence/peer-baseline/samples/wired/ethernet.pcap")
    result = inspect_capture(path)
    found = next(m for m in result["messages"] if m["completed_at_frame"] == 4)
    assert found["message_type"] == "0x0000"
    assert found["discovery"]["al_mac"] == "02:00:00:e0:00:02"
    assert found["discovery"]["interface_mac"] == "00:16:3e:0d:9a:ca"
    assert not result["onboarding_proven"]


def test_all_native_headers_and_completed_tlv_boundaries_match_independent_tshark():
    capture = Path("doc/evidence/peer-baseline/samples/wired/ethernet.pcap")
    references = json.loads(
        Path("tests/fixtures/protocol/ieee1905/native-envelope.json").read_text()
    )
    decoder = Reassembler()
    messages = {m["completed_at_frame"]: m for m in inspect_capture(capture)["messages"]}
    for (number, _time, data, _truncated), expected in zip(
        packets(capture), references, strict=True
    ):
        frame = decode_frame(data)
        assert number == expected["frame"]
        assert (
            frame.version,
            frame.message_type,
            frame.mid,
            frame.fid,
            frame.last,
            frame.relay,
        ) == (
            expected["message_version"],
            expected["message_type"],
            expected["message_id"],
            expected["fragment_id"],
            expected["last_fragment"],
            expected["relay_indicator"],
        )
        assert frame.source.hex(":") == expected["src"]
        assert frame.destination.hex(":") == expected["dst"]
        result = decoder.feed(data)
        if result is not None:
            assert messages[number]["tlvs"] == expected["tlvs"]
    assert len(references) == 59 and len(messages) == 58
