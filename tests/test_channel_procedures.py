import sqlite3
from dataclasses import replace

import pytest

from emosa.errors import EmosaError
from emosa.simulation.wire_reports import fixtures
from emosa.wire.channel import (
    ChannelCoordinator,
    ChannelPolicyStore,
    OperatingRadio,
    selected_policy,
)
from emosa.wire.cmdu import MidSequence, Reassembler, Tlv, fragment_message
from emosa.wire.coordinator import ReportSource

pytestmark = pytest.mark.unit


class Rig:
    def __init__(self, directory):
        self.now = 0.0
        self.binding, self.caps, self.topology = fixtures()
        radio = self.caps.radios[0]
        opclass = replace(
            radio.basic.operating_classes[0],
            non_operable_channels=tuple(c for c in range(1, 14) if c != 6),
        )
        self.caps = replace(
            self.caps,
            radios=(replace(radio, basic=replace(radio.basic, operating_classes=(opclass,))),),
        )
        self.radio = OperatingRadio(radio.basic.ruid, 81, 6, 17)
        self.source = ReportSource(self.binding, "pod-1", "a" * 64, clock=lambda: self.now)
        self.store = ChannelPolicyStore(directory / "policy.sqlite")
        self.sent = []
        self.coordinator = ChannelCoordinator(
            self.source, self.sent.append, self.store, MidSequence(65534), clock=lambda: self.now
        )
        self.publish()

    def publish(self, revision=(1, 1), radios=None):
        return self.source.publish(
            revision,
            self.caps,
            self.topology,
            observed_at=self.now,
            lifetime=2,
            operating_radios=(self.radio,) if radios is None else radios,
        )

    def request(self, kind=0x8006, mid=41, tlvs=(), received_at=None):
        frame = fragment_message(
            self.binding.local_al, self.binding.controller_al, kind, mid, tlvs
        )[0]
        return self.coordinator.handle(
            Reassembler().feed(frame), self.now if received_at is None else received_at
        )

    def ack(self, mid, *, source=None, tlvs=(), generation=1):
        frame = fragment_message(
            self.binding.local_al, source or self.binding.controller_al, 0x8000, mid, tlvs
        )[0]
        return self.coordinator.ack(frame, ingress="fixture", generation=generation)


@pytest.fixture
def rig(tmp_path):
    value = Rig(tmp_path)
    yield value
    value.coordinator.close()
    value.store.close()


def test_preference_omission_means_highest_only_for_advertised_operable_channels(rig):
    assert rig.request(0x8004) == "channel_preference_report_sent"
    packet = Reassembler().feed(rig.sent[0])
    assert (packet.message_type, packet.mid, packet.tlvs) == (0x8005, 41, ())
    assert rig.store.read()["status"] == "reboot_default"
    assert rig.request(0x8004) == "duplicate_channel_request"
    assert len(rig.sent) == 1


def test_empty_selection_persists_then_reports_measured_power_and_ack(rig, tmp_path):
    assert rig.request() == "channel_selection_accepted"
    response, report = (Reassembler().feed(f) for f in rig.sent)
    assert (response.message_type, response.mid) == (0x8007, 41)
    assert response.tlvs == (Tlv(0x8E, rig.radio.ruid + b"\0"),)
    assert (report.message_type, report.mid) == (0x8008, 65535)
    # 17 dBm is observed, not the advertised 20 dBm maximum or a requested limit.
    assert report.tlvs == (Tlv(0x8F, rig.radio.ruid + bytes.fromhex("01510611")),)
    with sqlite3.connect(tmp_path / "policy.sqlite") as another_reader:
        assert (
            '"mid": 41' in another_reader.execute("SELECT value FROM channel_policy").fetchone()[0]
        )
    rig.now = 0.3
    assert rig.ack(report.mid) == "operating_channel_acknowledged"
    assert len(rig.sent) == 2  # An arriving Ack does not trigger a needless retry.


def test_retries_get_new_mids_and_keep_original_deadline_across_telemetry_updates(rig):
    rig.request()
    for now, revision in ((0.3, 2), (0.6, 3), (0.9, 4)):
        rig.now = now
        rig.publish((1, revision))
        rig.coordinator.tick()
    reports = [Reassembler().feed(f) for f in rig.sent[1:]]
    assert [p.mid for p in reports] == [65535, 0, 1]
    rig.now = 1
    rig.coordinator.tick()
    assert rig.coordinator.pending is None
    assert rig.coordinator.counts["operating_channel_ack_timeout"] == 1
    assert rig.ack(1) is None


@pytest.mark.parametrize("fault", ["generation", "power", "missing", "invalidate"])
def test_report_retry_withdraws_changed_observation_or_context(rig, fault):
    rig.request()
    if fault == "generation":
        rig.publish((2, 1))
    elif fault == "power":
        rig.publish((1, 2), (replace(rig.radio, tx_power_dbm=15),))
    elif fault == "missing":
        rig.publish((1, 2), ())
    else:
        rig.source.invalidate()
    rig.now = 0.3
    rig.coordinator.tick()
    assert len(rig.sent) == 2 and rig.coordinator.pending is None
    assert rig.coordinator.counts["operating_report_source_lost"] == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": bytes.fromhex("02000000eeee")},
        {"generation": 2},
        {"tlvs": (Tlv(0xA3, b"error"),)},
    ],
)
def test_alien_or_error_ack_cannot_complete_report(rig, kwargs):
    rig.request()
    with pytest.raises(EmosaError):
        rig.ack(65535, **kwargs)
    assert rig.coordinator.pending is not None
    assert rig.ack(65534) is None
    assert rig.ack(65535) == "operating_channel_acknowledged"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"\x01",
        b"\x01\x51\x02\x06",
        b"\x00x",
        bytes.fromhex("01510106f0"),
        bytes.fromhex("01510106e6"),
        bytes.fromhex("02510106e0510106d0"),
    ],
)
def test_malformed_or_reserved_preferences_have_no_effect(rig, body):
    before = rig.store.read()
    with pytest.raises(EmosaError):
        rig.request(tlvs=(Tlv(0x8B, rig.radio.ruid + body),))
    assert rig.store.read() == before and rig.sent == []


def test_complete_scope_validation_before_any_acceptance_or_persistence(rig):
    before = rig.store.read()
    supported = Tlv(0x8B, rig.radio.ruid + bytes.fromhex("01510106e0"))
    for tlvs in (
        (supported, supported),
        (supported, Tlv(0xEB, b"unknown-actuation")),
        (Tlv(0x8D, rig.radio.ruid + b"\x10"),),  # Lower than observed 17; needs actuator.
        (Tlv(0x8B, rig.radio.ruid + bytes.fromhex("0151010600")),),
        (Tlv(0x8B, bytes.fromhex("02000000ffff00")),),
    ):
        with pytest.raises(EmosaError):
            rig.request(tlvs=tlvs)
        assert rig.store.read() == before and rig.sent == []
    assert (
        rig.request(tlvs=(supported, Tlv(0x8D, rig.radio.ruid + b"\x14")))
        == "channel_selection_accepted"
    )
    assert rig.store.read()["power_limit_dbm"] == 20
    assert rig.store.read()["preferences"][0]["preference"] == 14


def test_no_measured_radio_no_late_reply_no_disk_no_success(rig, monkeypatch):
    before = rig.store.read()
    rig.publish((1, 2), ())
    assert rig.request() == "channel_waiting_for_observation"
    rig.now = 1
    rig.coordinator.tick()
    assert not rig.coordinator.waiting
    rig.publish((1, 3))
    with pytest.raises(EmosaError):
        rig.request(received_at=-2)
    assert rig.sent == [] and rig.store.read() == before

    def fail(_):
        raise sqlite3.OperationalError("injected full disk")

    monkeypatch.setattr(rig.store, "save", fail)
    with pytest.raises(EmosaError):
        rig.request()
    assert rig.sent == []


def test_startup_requests_wait_for_fresh_observation_with_original_mids(rig):
    rig.publish((1, 2), ())
    before = rig.store.read()
    assert rig.request(0x8006, 41) == "channel_waiting_for_observation"
    assert rig.request(0x8004, 42) == "channel_waiting_for_observation"
    assert not rig.sent and rig.store.read() == before
    rig.now = 0.2
    rig.publish((1, 3))
    rig.coordinator.tick()
    replies = [Reassembler().feed(f) for f in rig.sent]
    assert [(p.message_type, p.mid) for p in replies] == [
        (0x8007, 41),
        (0x8008, 65535),
        (0x8005, 42),
    ]
    assert replies[1].tlvs == (rig.radio.tlv(),)
    assert rig.store.read()["status"] == "accepted_no_adjustment"
    assert not rig.coordinator.waiting


@pytest.mark.parametrize("fault", ["generation", "invalidate", "close", "deadline"])
def test_waiting_channel_request_cannot_outlive_authority_or_deadline(rig, fault):
    rig.publish((1, 2), ())
    before = rig.store.read()
    rig.request()
    rig.now = 0.99
    assert rig.request() == "duplicate_channel_request"
    if fault == "generation":
        rig.publish((2, 1))
    elif fault == "invalidate":
        rig.source.invalidate()
    elif fault == "close":
        rig.coordinator.close()
    else:
        rig.now = 1
        rig.publish((1, 3))
    rig.coordinator.tick()
    assert not rig.coordinator.waiting and not rig.sent
    assert rig.store.read() == before


def test_channel_observation_wait_has_fixed_budget_and_validates_before_effect(rig):
    rig.publish((1, 2), ())
    before = rig.store.read()
    for mid in range(4):
        assert rig.request(mid=mid, tlvs=(Tlv(0xEB, b"unknown"),)) == (
            "channel_waiting_for_observation"
        )
    assert rig.request(mid=5) == "channel_observation_wait_budget_exhausted"
    rig.now = 0.5
    rig.publish((1, 3))
    rig.coordinator.tick()
    assert not rig.sent and not rig.coordinator.waiting and rig.store.read() == before
    assert rig.coordinator.counts["queued_channel_rejected_UNSUPPORTED_OPERATION"] == 4


def test_observed_power_change_notifies_and_reboot_explicitly_resets_policy(rig):
    rig.request(tlvs=(Tlv(0x8D, rig.radio.ruid + b"\x14"),))
    rig.ack(65535)
    rig.publish((1, 2), (replace(rig.radio, tx_power_dbm=16),))
    rig.coordinator.tick()
    assert Reassembler().feed(rig.sent[-1]).tlvs[0].value[-1] == 16
    assert rig.coordinator.counts["autonomous_operating_change_reported"] == 1
    rig.coordinator.close()
    restarted = ChannelCoordinator(
        rig.source, rig.sent.append, rig.store, MidSequence(200), clock=lambda: rig.now
    )
    assert rig.store.read()["status"] == "reboot_default"
    assert rig.store.read()["power_limit_dbm"] is None
    restarted.close()


def test_transport_reconnect_retains_accepted_policy(rig):
    rig.request(tlvs=(Tlv(0x8D, rig.radio.ruid + b"\x14"),))
    accepted = rig.store.read()
    rig.coordinator.close()
    rig.publish((2, 1))
    reconnected = ChannelCoordinator(
        rig.source,
        rig.sent.append,
        rig.store,
        MidSequence(200),
        clock=lambda: rig.now,
        reset_policy=False,
    )
    assert rig.store.read() == accepted
    reconnected.close()


def test_signed_power_codec_and_all_class_preference(rig):
    assert replace(rig.radio, tx_power_dbm=-2).tlv().value[-1] == 254
    policy = selected_policy((Tlv(0x8B, rig.radio.ruid + bytes.fromhex("015100d0")),), rig.radio)
    assert policy["preferences"][0]["channels"] == []
    assert policy["preferences"][0]["preference"] == 13
