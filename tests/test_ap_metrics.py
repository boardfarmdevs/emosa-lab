import sqlite3
import struct
from dataclasses import replace

import pytest

from emosa.easymesh_payloads import AssociatedClient, AssociatedClients, BssClients
from emosa.errors import EmosaError
from emosa.wire.ap_metrics import (
    APExtendedMetrics,
    APMetricBundle,
    APMetricCoordinator,
    APMetrics,
    APMetricSource,
    RadioMetrics,
    StationLinkMetrics,
    StationMetrics,
)
from emosa.wire.cmdu import MidSequence, Reassembler, Tlv, fragment_message
from emosa.wire.coordinator import ReportSource
from emosa.wire.disassociation import TrafficCounters
from emosa.wire.reporting_policy import ReportingPolicyCoordinator, ReportingPolicyStore
from emosa_lab.simulation.wire_reports import BSSID, RUID, fixtures

pytestmark = pytest.mark.unit
STA = bytes.fromhex("020000004020")


def bundle(now, *, stations=(STA,)):
    return APMetricBundle(
        APMetrics(BSSID, 123, len(stations), (("VI", b"\x02\x30\x40"), ("BE", b"\x01\x02\x03"))),
        APExtendedMetrics(BSSID, (0x01020304, 2, 3, 4, 5, 0xFFFFFFFF), 0),
        RadioMetrics(RUID, 160, 21, 22, 23),
        tuple(
            StationMetrics(
                sta,
                "association-" + sta.hex(),
                TrafficCounters(2**32 + 2049, 1002, 3, 4, 5, 6, 7),
                StationLinkMetrics(now - 0.125, 65, 32, 120, 65000, 32000, 101, 202),
                ((7, 255), (0, 1)),
            )
            for sta in stations
        ),
    )


class Rig:
    def __init__(self, path):
        self.now = 10.0
        self.revision = 0
        self.binding, self.caps, self.topology = fixtures()
        self.reports = ReportSource(self.binding, "pod-1", "a" * 64, clock=lambda: self.now)
        self.measurements = APMetricSource(self.reports, clock=lambda: self.now)
        self.sent = []
        self.admitted = True
        self.store = ReportingPolicyStore(path / "policy.sqlite", boot_id="same-boot")
        self.reporter = APMetricCoordinator(
            self.measurements,
            self.sent.append,
            MidSequence(100),
            admitted=lambda: self.admitted,
            policy=lambda: self.policy.value["policy"] if self.policy.value else {},
            clock=lambda: self.now,
        )
        self.policy = self.restart_policy()
        self.observe()

    def restart_policy(self):
        return ReportingPolicyCoordinator(
            self.reports,
            self.sent.append,
            self.store,
            reporter=self.reporter,
            clock=lambda: self.now,
        )

    def observe(self, stations=(STA,)):
        self.revision += 1
        self.topology = replace(
            self.topology,
            clients=AssociatedClients(
                (BssClients(BSSID, tuple(AssociatedClient(s, 4) for s in stations)),)
            ),
        )
        self.reports.publish(
            (1, self.revision),
            self.caps,
            self.topology,
            observed_at=self.now,
            lifetime=2,
        )

    def publish(self, value=None, **changes):
        args = dict(
            context=self.reports.current().context_token,
            counter_epoch="counter-epoch-1",
            observed_at=self.now,
            bundle=value or bundle(self.now),
            inventory_complete=True,
        )
        args.update(changes)
        return self.measurements.publish(**args)

    def request_policy(self, interval=60, flags=0xE0, mid=11):
        request = Reassembler().feed(
            fragment_message(
                self.binding.local_al,
                self.binding.controller_al,
                0x8003,
                mid,
                (Tlv(0x8A, bytes((interval, 1)) + RUID + bytes((0, 0, 0, flags))),),
            )[0]
        )
        return self.policy.handle(request, self.now)

    def query(self, *, radio=True, mid=71, changes=None, tlvs=None):
        request = Reassembler().feed(
            fragment_message(
                self.binding.local_al,
                self.binding.controller_al,
                0x800B,
                mid,
                tlvs
                if tlvs is not None
                else (Tlv(0x93, b"\1" + BSSID),) + ((Tlv(0x82, RUID),) if radio else ()),
            )[0]
        )
        return self.reporter.handle(
            replace(request, **(changes or {})),
            self.now,
            ingress="fixture",
            generation=1,
        )


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.policy.close()
    r.reporter.close()
    r.store.close()


def messages(frames):
    assembly = Reassembler()
    return [p for f in frames if (p := assembly.feed(f)) is not None]


def test_complete_query_preserves_all_policy_companions_and_literal_fields(rig):
    rig.request_policy()
    rig.publish()
    assert rig.query() == "ap_metric_query_answered"
    response = messages(rig.sent)[-1]
    assert (response.message_type, response.mid) == (0x800C, 71)
    assert [t.kind for t in response.tlvs] == [0x94, 0xC7, 0xC6, 0xA2, 0x96, 0xC8, 0xB0]
    v = {t.kind: t.value for t in response.tlvs}
    # Sparse BE+VI fields must be contiguous and in Table 45 order.
    assert v[0x94] == BSSID + bytes.fromhex("7b000190010203023040")
    assert v[0xC7] == BSSID + bytes.fromhex("0102030400000002000000030000000400000005ffffffff")
    assert v[0xC6] == RUID + bytes((160, 21, 22, 23))
    assert v[0xA2] == STA + struct.pack("!7I", 2049, 1002, 3, 4, 5, 6, 7)
    assert v[0x96] == STA + b"\1" + BSSID + struct.pack("!3IB", 125, 65, 32, 120)
    assert v[0xC8] == STA + b"\1" + BSSID + struct.pack("!4I", 65000, 32000, 101, 202)
    assert v[0xB0] == STA + bytes.fromhex("0207ff0001")


@pytest.mark.parametrize("field", ["traffic", "link", "wifi6_queues"])
def test_one_missing_requested_companion_withholds_whole_response(rig, field):
    rig.request_policy()
    value = bundle(rig.now)
    rig.publish(replace(value, stations=(replace(value.stations[0], **{field: None}),)))
    before = list(rig.sent)
    assert rig.query() == "ap_measurements_unavailable"
    assert rig.sent == before


def test_no_policy_no_sta_companions_but_still_require_ap_extended_metrics(rig):
    rig.publish()
    rig.query(radio=False)
    assert [t.kind for t in messages(rig.sent)[0].tlvs] == [0x94, 0xC7]


def test_explicit_empty_queue_inventory_is_not_missing_or_zero_filled(rig):
    rig.request_policy()
    value = bundle(rig.now)
    rig.publish(replace(value, stations=(replace(value.stations[0], wifi6_queues=()),)))
    rig.query()
    assert messages(rig.sent)[-1].tlvs[-1] == Tlv(0xB0, STA + b"\0")


@pytest.mark.parametrize(
    "fault",
    [
        "membership",
        "count",
        "bssid",
        "radio",
        "units",
        "duplicate",
        "missing_be",
        "future",
        "expired",
        "incomplete",
        "context",
        "reserved_rcpi",
        "duplicate_tid",
    ],
)
def test_invalid_source_withdraws_previous_sample(rig, fault):
    rig.publish()
    rig.now += 0.1
    value, changes = bundle(rig.now), {}
    if fault == "membership":
        value = replace(value, stations=())
    elif fault == "count":
        value = replace(value, ap=replace(value.ap, station_count=2))
    elif fault == "bssid":
        value = replace(value, extended=replace(value.extended, bssid=STA))
    elif fault == "radio":
        value = replace(value, radio=replace(value.radio, ruid=STA))
    elif fault == "units":
        value = replace(value, extended=replace(value.extended, byte_units=1))
    elif fault == "duplicate":
        value = replace(value, stations=value.stations * 2)
    elif fault == "missing_be":
        value = replace(value, ap=replace(value.ap, esp=(("VI", b"\2\0\0"),)))
    elif fault == "future":
        changes["observed_at"] = rig.now + 1
    elif fault == "expired":
        changes["observed_at"] = rig.now - 3
    elif fault == "incomplete":
        changes["inventory_complete"] = False
    elif fault == "context":
        changes["context"] = "other-context"
    elif fault == "reserved_rcpi":
        value = replace(
            value,
            stations=(replace(value.stations[0], link=replace(value.stations[0].link, rcpi=255)),),
        )
    else:
        value = replace(
            value, stations=(replace(value.stations[0], wifi6_queues=((1, 0), (1, 0))),)
        )
    with pytest.raises(EmosaError):
        rig.publish(value, **changes)
    assert rig.measurements.current() is None


def test_duplicate_cannot_refresh_lease_or_rebind_to_new_control_revision(rig):
    value = bundle(rig.now)
    old = rig.publish(value)
    rig.now += 0.5
    assert rig.publish(value, observed_at=10) == old
    rig.observe()
    assert rig.measurements.current() is None
    with pytest.raises(EmosaError, match="replayed"):
        rig.publish(value, observed_at=10)
    rig.publish()
    rig.reports.invalidate()
    assert rig.measurements.current() is None


@pytest.mark.parametrize(
    "failure", ["controller", "relay", "admission", "missing", "scope", "expired"]
)
def test_query_without_valid_authority_has_no_report(rig, failure):
    if failure != "missing":
        rig.publish()
    changes = {}
    if failure == "controller":
        changes["source"] = STA
    elif failure == "relay":
        changes["relay"] = True
    elif failure == "admission":
        rig.admitted = False
    elif failure == "expired":
        rig.now += 2
    if failure in ("controller", "relay", "scope"):
        with pytest.raises(EmosaError):
            rig.query(
                changes=changes, tlvs=(Tlv(0x93, b"\1" + STA),) if failure == "scope" else None
            )
    else:
        assert rig.query() == "ap_measurements_unavailable"
    assert not rig.sent


def test_periodic_reservation_before_send_and_same_policy_preserves_deadline(rig):
    rig.request_policy()
    rig.now = 70
    rig.observe()
    rig.publish()
    original = rig.reporter.send_frame

    def send(frame):
        saved = rig.store.read()
        assert saved["next_due"] == 130 and saved["periods_due_without_report"] == 1
        assert saved["latest_report_attempt"]["status"] == "reserved_outcome_unknown"
        original(frame)

    rig.reporter.send_frame = send
    rig.policy.tick()
    saved = rig.store.read()
    assert saved["reports_transmitted"] == 1 and saved["periods_due_without_report"] == 0
    assert saved["last_unfulfilled_due"] is None
    assert messages(rig.sent)[-1].message_type == 0x800C
    rig.request_policy(mid=12)
    assert rig.store.read()["next_due"] == 130
    assert not rig.policy.status()["required_reporting_proven"]


@pytest.mark.parametrize("failure", ["missing", "partial_io", "after_send_crash", "expired_due"])
def test_periodic_failure_is_durable_and_does_not_replay_after_restart(rig, failure, monkeypatch):
    rig.request_policy()
    rig.now = 70 if failure != "expired_due" else 71.01
    stations = tuple(bytes.fromhex("0200000050") + bytes((x,)) for x in range(1, 33))
    rig.observe(stations)
    if failure != "missing":
        rig.publish(bundle(rig.now, stations=stations))
    if failure == "partial_io":

        def fail(frame):
            rig.sent.append(frame)
            raise OSError("injected incomplete send")

        rig.reporter.send_frame = fail
    if failure == "after_send_crash":
        original = rig.store.save

        def crash(value):
            if value.get("reports_transmitted"):
                raise sqlite3.OperationalError("injected post-send persistence loss")
            original(value)

        monkeypatch.setattr(rig.store, "save", crash)
        with pytest.raises(EmosaError):
            rig.policy.tick()
    else:
        rig.policy.tick()
    saved = rig.store.read()
    assert saved["periods_due_without_report"] == 1 and saved["next_due"] == 130
    assert saved.get("reports_transmitted", 0) == 0
    sent = len(rig.sent)
    rig.policy.close()
    rig.policy = rig.restart_policy()
    rig.policy.tick()
    assert len(rig.sent) == sent


def test_delayed_restart_sends_at_most_one_current_report_and_counts_old_periods(rig):
    rig.request_policy()
    rig.now = 190.2
    rig.observe()
    rig.publish()
    rig.policy = rig.restart_policy()
    rig.policy.tick()
    saved = rig.store.read()
    assert saved["next_due"] == 250 and saved["periods_due_without_report"] == 2
    assert saved["reports_transmitted"] == 1 and saved["last_unfulfilled_due"] == 130
    assert len(messages(rig.sent)) == 2  # Receipt Ack plus one current report.


def test_source_change_during_fragmented_send_stops_later_fragments(rig):
    rig.request_policy()
    stations = tuple(bytes.fromhex("0200000050") + bytes((x,)) for x in range(1, 33))
    rig.now += 0.1
    rig.observe(stations)
    rig.publish(bundle(rig.now, stations=stations))
    rig.sent.clear()

    def send(frame):
        rig.sent.append(frame)
        rig.reports.invalidate()

    rig.reporter.send_frame = send
    assert rig.query() == "ap_measurements_unavailable"
    assert len(rig.sent) == 1 and not messages(rig.sent)
    assert rig.reporter.counts.get("ap_metric_report_transmitted", 0) == 0
