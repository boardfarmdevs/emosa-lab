import hashlib
from pathlib import Path

import pytest

from emosa_lab.simulation.station_telemetry import NODE_ID, TOPIC, encode_stations
from emosa_lab.telemetry.stations import PROTO_SHA256, Report, StationSource

pytestmark = pytest.mark.unit
MAC = "02:00:00:00:02:00"


def payload(timestamp=1000000, seconds=123):
    return encode_stations(
        {
            "complete": True,
            "timestamp_ms": timestamp,
            "clients": [{"mac": MAC, "connected_seconds": seconds}],
        },
        "test",
    )


def source():
    return StationSource(NODE_ID, TOPIC, clock=lambda: 10, wall=lambda: 1000.25)


def test_pinned_descriptor_and_actual_association_offset_not_reporting_duration():
    assert (
        hashlib.sha256(
            Path("lab/src/emosa_lab/telemetry/data/opensync_stats.proto").read_bytes()
        ).hexdigest()
        == PROTO_SHA256
    )
    value = Report.FromString(payload())
    value.clients[0].client_list[0].duration_ms = 7
    receiver = source()
    assert receiver.receive(TOPIC, value.SerializeToString())
    sample = receiver.current()
    assert sample.clients == ((bytes.fromhex("020000000200"), 123),)
    assert sample.observed_at == 9.75 and sample.valid_until == 11.75
    assert receiver.accepted == 1


def test_duplicate_retained_old_and_out_of_order_do_not_renew_or_replace():
    receiver = source()
    assert receiver.receive(TOPIC, payload())
    sample = receiver.current()
    for topic, data, retained in (
        (TOPIC, payload(), False),
        (TOPIC, payload(1000010), True),
        (TOPIC, payload(999999), False),
        (TOPIC, payload(998000), False),
        (TOPIC, payload(1000251), False),
        ("unrelated", payload(1000010), False),
        (TOPIC, b"\xff", False),
        (TOPIC, b"x" * 65537, False),
    ):
        assert not receiver.receive(topic, data, retained=retained)
        assert receiver.current() is sample
    receiver.clock = lambda: 11.75
    assert receiver.current() is None
    receiver.disconnect()
    assert not receiver.receive(TOPIC, payload())
    receiver.clock = lambda: 10
    assert receiver.receive(TOPIC, payload(1000010))
    assert receiver.current().valid_until == pytest.approx(11.76)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: setattr(r, "nodeID", "different"),
        lambda r: r.ClearField("nodeID"),
        lambda r: r.clients.add(band=0, channel=6),
        lambda r: setattr(r.clients[0], "channel", 11),
        lambda r: r.clients[0].ClearField("timestamp_ms"),
        lambda r: r.clients[0].client_list[0].ClearField("connect_offset_ms"),
        lambda r: setattr(r.clients[0].client_list[0], "connected", False),
        lambda r: setattr(r.clients[0].client_list[0], "mac_address", "ff:ff:ff:ff:ff:ff"),
        lambda r: r.clients[0].client_list.append(r.clients[0].client_list[0]),
        lambda r: setattr(r.clients[0].client_list[0], "mld_address", MAC),
    ],
)
def test_ambiguous_wrong_scope_or_missing_source_fields_rejected(mutation):
    value = Report.FromString(payload())
    mutation(value)
    receiver = source()
    assert not receiver.receive(TOPIC, value.SerializePartialToString())
    assert receiver.current() is None


def test_saturation_and_positive_empty_snapshot():
    receiver = source()
    assert receiver.receive(TOPIC, payload(seconds=70000))
    assert receiver.current().clients[0][1] == 65535
    assert receiver.receive(
        TOPIC, encode_stations({"complete": True, "timestamp_ms": 1000010, "clients": []}, "test")
    )
    assert receiver.current().clients == ()
    with pytest.raises(ValueError, match="incomplete"):
        encode_stations({"complete": False}, "test")
