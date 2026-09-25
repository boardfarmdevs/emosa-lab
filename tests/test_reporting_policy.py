import json
import sqlite3
from dataclasses import replace

import pytest

from emosa.errors import EmosaError
from emosa.wire.cmdu import Reassembler, Tlv, fragment_message
from emosa.wire.coordinator import ReportSource
from emosa.wire.reporting_policy import (
    ReportingPolicyCoordinator,
    ReportingPolicyStore,
    decode_policy,
)
from emosa_lab.simulation.wire_reports import fixtures

pytestmark = pytest.mark.unit


class Rig:
    def __init__(self, directory):
        self.now = 0.0
        self.binding, self.caps, self.topology = fixtures()
        self.ruid = self.caps.radios[0].basic.ruid
        self.source = ReportSource(self.binding, "pod-1", "a" * 64, clock=lambda: self.now)
        self.path = directory / "reporting.sqlite"
        self.store = ReportingPolicyStore(self.path, boot_id="boot-1")
        self.sent = []
        self.coordinator = self.restart()
        self.publish()

    def restart(self):
        return ReportingPolicyCoordinator(
            self.source, self.sent.append, self.store, clock=lambda: self.now
        )

    def publish(self):
        self.source.publish((1, 1), self.caps, self.topology, observed_at=self.now, lifetime=2)

    def metrics(self, interval=60, flags=0xE0):
        return Tlv(0x8A, bytes((interval, 1)) + self.ruid + bytes((0, 0, 0, flags)))

    def request(self, tlvs=None, mid=23, **changes):
        message = Reassembler().feed(
            fragment_message(
                self.binding.local_al,
                self.binding.controller_al,
                0x8003,
                mid,
                tlvs if tlvs is not None else (self.metrics(),),
            )[0]
        )
        return self.coordinator.handle(replace(message, **changes), self.now)


@pytest.fixture
def rig(tmp_path):
    value = Rig(tmp_path)
    yield value
    value.coordinator.close()
    value.store.close()


def test_native_policy_stored_before_correlated_receipt_ack(rig):
    tlvs = (rig.metrics(), Tlv(0x89, b"\0\0\1" + rig.ruid + b"\0\0\0"), Tlv(0xDB, bytes(22)))

    def send(frame):
        with sqlite3.connect(rig.path) as reader:
            record = json.loads(reader.execute("SELECT value FROM reporting_policy").fetchone()[0])
        assert record["latest_mid"] == 23
        assert record["policy"]["metrics"]["interval_seconds"] == 60
        rig.sent.append(frame)

    rig.coordinator.send_frame = send
    assert rig.request(tlvs) == "policy_receipt_ack_sent"
    ack = Reassembler().feed(rig.sent[0])
    assert (ack.message_type, ack.mid, ack.tlvs) == (0x8000, 23, ())
    assert ack.source == rig.binding.local_al and ack.destination == rig.binding.controller_al
    status = rig.coordinator.status()
    assert not status["required_reporting_proven"] and not status["policy_application_proven"]
    policy = rig.store.read()["policy"]
    assert all(
        policy["metrics"]["radios"][0][k]
        for k in ("include_traffic", "include_link", "include_wifi6_status")
    )
    assert policy["qos"] == [{"mscs_disallowed": [], "scs_disallowed": []}]


def test_deadlines_survive_identical_new_mid_reconnect_and_process_restart(rig):
    rig.request()
    rig.now = 50
    rig.publish()
    rig.coordinator.close()
    rig.coordinator = rig.restart()
    rig.request(mid=24)
    assert rig.store.read()["next_due"] == 60
    rig.store.close()
    rig.store = ReportingPolicyStore(rig.path, boot_id="boot-1")
    rig.coordinator = rig.restart()
    rig.now = 181
    rig.publish()
    rig.coordinator.tick()
    result = rig.store.read()
    assert result["periods_due_without_report"] == 3 and result["next_due"] == 240
    assert result["receipt_count"] == 2
    assert len(rig.sent) == 2


def test_changed_interval_replaces_schedule_but_omitted_metrics_preserve_it(rig):
    rig.request()
    rig.now = 1
    rig.publish()
    rig.request((Tlv(0xDB, bytes(22)),), mid=24)
    assert rig.store.read()["next_due"] == 60
    rig.request((rig.metrics(10),), mid=25)
    assert rig.store.read()["next_due"] == 11
    rig.request((rig.metrics(0),), mid=26)
    assert rig.store.read()["next_due"] is None


def test_reboot_preserves_intent_and_explicitly_rebases_monotonic_schedule(rig):
    rig.request()
    rig.coordinator.close()
    rig.store.boot_id = "boot-2"
    rig.now = 5
    rig.publish()
    rig.coordinator = rig.restart()
    rig.coordinator.tick()
    assert rig.store.read()["schedule_rebases"] == 1
    assert rig.store.read()["next_due"] == 65


def test_same_mid_can_reack_but_cannot_change_policy(rig):
    rig.request()
    rig.request()
    assert len(rig.sent) == 2 and rig.store.read()["receipt_count"] == 1
    with pytest.raises(EmosaError, match="conflicting"):
        rig.request((rig.metrics(10),))
    assert rig.store.read()["policy"]["metrics"]["interval_seconds"] == 60


@pytest.mark.parametrize("failure", ["database", "stale", "controller", "relay", "identity"])
def test_no_ack_without_live_binding_and_durable_complete_receipt(rig, monkeypatch, failure):
    changes = {}
    if failure == "database":

        def fail(_):
            raise sqlite3.OperationalError("unavailable")

        monkeypatch.setattr(rig.store, "save", fail)
    elif failure == "stale":
        rig.now = 3
    elif failure == "controller":
        changes["source"] = bytes.fromhex("020000000099")
    elif failure == "relay":
        changes["relay"] = True
    else:
        rig.request()
        rig.coordinator.value["identity"]["ruid"] = "020000000099"
        rig.sent.clear()
    with pytest.raises(EmosaError):
        rig.request(mid=24, **changes)
    assert not rig.sent


def test_source_loss_stops_active_work_and_recovery_accounts_elapsed_periods(rig):
    rig.request()
    rig.source.invalidate()
    rig.now = 121
    rig.coordinator.tick()
    assert rig.store.read()["periods_due_without_report"] == 0
    rig.publish()
    rig.coordinator.tick()
    assert rig.store.read()["periods_due_without_report"] == 2


@pytest.mark.parametrize(
    "kind,value",
    [
        (0x8A, b"\x3c\x01"),
        (0x8A, b"\x3c\x00\x00"),
        (0x89, b"\x01"),
        (0x89, b"\x00\x00\x01"),
        (0xDB, bytes(21)),
        (0xDB, bytes(23)),
        (0x99, b""),
    ],
)
def test_bad_companion_cannot_partially_replace_policy(rig, kind, value):
    rig.request()
    before = rig.store.read()
    with pytest.raises(EmosaError):
        rig.request((rig.metrics(10), Tlv(kind, value)), mid=24)
    assert rig.store.read() == before and len(rig.sent) == 1


def test_reserved_bits_ignored_and_complete_counted_lists_retained(rig):
    decoded = decode_policy((rig.metrics(flags=0xFF),), rig.ruid)
    assert decoded == decode_policy((rig.metrics(),), rig.ruid)
    sta = bytes.fromhex("020000000077")
    qos = Tlv(0xDB, b"\1" + sta + b"\0" + bytes([255]) * 20)
    assert (
        decode_policy((qos, qos), rig.ruid)["qos"]
        == [{"mscs_disallowed": [sta.hex()], "scs_disallowed": []}] * 2
    )


def test_persistence_delay_cannot_send_late_ack(rig, monkeypatch):
    original = rig.store.save

    def slow(value):
        original(value)
        rig.now += 1.1
        rig.publish()

    monkeypatch.setattr(rig.store, "save", slow)
    with pytest.raises(EmosaError):
        rig.request()
    assert rig.store.read()["receipt_count"] == 1 and not rig.sent


def test_bounded_mid_tracking_expires_without_forgetting_reporting_schedule(rig):
    for mid in range(64):
        rig.request(mid=mid)
    with pytest.raises(EmosaError, match="budget"):
        rig.request(mid=65)
    rig.now = 6
    rig.publish()
    rig.request(mid=65)
    assert len(rig.coordinator.recent) == 1
    assert rig.store.read()["next_due"] == 60


@pytest.mark.parametrize("fault", ["radio", "rcpi", "duplicate_metrics", "duplicate_steering"])
def test_invalid_selected_fields_are_rejected_before_receipt(rig, fault):
    metrics = rig.metrics()
    if fault == "radio":
        tlvs = (Tlv(0x8A, metrics.value[:2] + bytes.fromhex("020000000088") + metrics.value[8:]),)
    elif fault == "rcpi":
        tlvs = (Tlv(0x8A, metrics.value[:8] + b"\xdd" + metrics.value[9:]),)
    elif fault == "duplicate_metrics":
        tlvs = (metrics, metrics)
    else:
        tlvs = (Tlv(0x89, bytes(3)),) * 2
    with pytest.raises(EmosaError):
        rig.request(tlvs)
    assert rig.store.read() is None and not rig.sent
