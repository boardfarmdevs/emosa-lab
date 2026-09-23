"""Read-only Linux observation framing, presence and corruption boundaries."""

import runpy
import socket
import struct
from pathlib import Path

import pytest

observer = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "deploy/radio-manager/station-events.py")
)
pytestmark = pytest.mark.unit


def nla(kind, data):
    size = len(data) + 4
    return struct.pack("=HH", size, kind) + data + bytes((-size) % 4)


def scalar(kind, fmt, value):
    return nla(kind, struct.pack("=" + fmt, value))


def station(info=b"", *, command=20, interface=7, extras=b""):
    return (
        struct.pack("=BBH", command, 1, 0)
        + scalar(3, "I", interface)
        + nla(6, bytes.fromhex("020000000200"))
        + nla(21 | 0x8000, info)
        + extras
    )


def message(payload, *, kind=42, sequence=0):
    size = 16 + len(payload)
    return struct.pack("=IHHII", size, kind, 0, sequence, 0) + payload + bytes((-size) % 4)


def test_final_kernel_fields_preserve_presence_and_wide_counters():
    event = observer["station_event"](
        station(
            scalar(23, "Q", 2**40 + 5)
            + scalar(24, "Q", 0)
            + scalar(10, "I", 17)
            + scalar(28, "Q", 0)
            + scalar(42, "Q", 123456789)
            + scalar(7, "b", -50),
            extras=scalar(46, "I", 31),
        ),
        7,
    )
    assert event["event"] == "del_station"
    assert event["station"] == "02:00:00:00:02:00"
    assert event["kernel_generation"] == 31
    assert event["observed_fields"] == {
        "rx_bytes64": 2**40 + 5,
        "tx_bytes64": 0,
        "tx_packets": 17,
        "rx_drop_misc": 0,
        "assoc_at_boottime_ns": 123456789,
        "signal_dbm": -50,
    }
    assert "tx_failed" not in event["observed_fields"]
    assert "reason_code" not in event
    assert bytes.fromhex(event["raw_station_info"]["24"]) == bytes(8)


def test_empty_final_statistics_stay_empty_and_foreign_interface_is_ignored():
    event = observer["station_event"](station(), 7)
    assert event["observed_fields"] == {}
    assert observer["station_event"](station(interface=8), 7) is None
    assert observer["station_event"](station(command=1), 7) is None
    event = observer["station_event"](station(command=19, extras=scalar(54, "H", 3)), 7)
    assert event["event"] == "new_station" and event["reason_code"] == 3


@pytest.mark.parametrize(
    "data",
    [
        b"\x01",
        struct.pack("=HH", 3, 1),
        struct.pack("=HH", 5, 1) + b"x",
        nla(1, b"x") * 2,
        nla(1 | 0x4000, b"x"),
        struct.pack("=HH", 20, 1),
    ],
)
def test_malformed_or_ambiguous_attributes_are_rejected(data):
    with pytest.raises(ValueError):
        observer["attributes"](data)


def test_wrong_counter_size_is_not_zero_or_a_partial_integer():
    with pytest.raises(ValueError):
        observer["station_event"](station(nla(23, b"\0" * 4)), 7)


@pytest.mark.parametrize(
    "data",
    [b"x", message(b"", kind=4), message(b"", kind=2), message(struct.pack("=i", -1), kind=2)],
)
def test_kernel_error_overrun_and_truncation_are_not_usable_observations(data):
    with pytest.raises(ValueError):
        list(observer["messages"](data))


def test_batched_messages_and_ack():
    packet = station()
    result = list(observer["messages"](message(packet) + message(struct.pack("=i", 0), kind=2)))
    assert len(result) == 1 and result[0][4] == packet


class Socket:
    def __init__(self, reply, flags=0, address=(0, 0)):
        self.reply, self.flags, self.address = reply, flags, address
        self.sent = []

    def sendto(self, data, address):
        self.sent.append((data, address))

    def recvmsg(self, _size):
        return self.reply, [], self.flags, self.address


@pytest.mark.parametrize(
    "flags,address", [(socket.MSG_TRUNC, (0, 0)), (socket.MSG_CTRUNC, (0, 0)), (0, (31, 0))]
)
def test_truncated_or_userspace_sender_rejected(flags, address):
    with pytest.raises(ValueError):
        observer["receive"](Socket(message(station()), flags, address))


def test_family_lookup_is_a_read_only_controller_request():
    groups = nla(1, nla(1, b"mlme\0") + scalar(2, "I", 9))
    response = struct.pack("=BBH", 1, 2, 0) + scalar(1, "H", 42) + nla(7, groups)
    channel = Socket(message(response, kind=16, sequence=1))
    assert observer["resolve"](channel) == (42, 9)
    request, destination = channel.sent[0]
    assert destination == (0, 0)
    assert struct.unpack_from("=IHHII", request)[1:4] == (16, 1, 1)
    assert request[16:20] == b"\x03\x01\0\0"  # CTRL_CMD_GETFAMILY only
    assert observer["attributes"](request[20:]) == {2: b"nl80211\0"}
