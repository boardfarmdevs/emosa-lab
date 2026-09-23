from dataclasses import replace

import pytest

from emosa.easymesh_payloads import AssociatedClient, AssociatedClients, BssClients
from emosa.errors import EmosaError
from emosa.simulation.wire_reports import fixtures
from emosa.telemetry.stations import Report
from emosa.wire.cmdu import MidSequence, Reassembler, Tlv, fragment_message
from emosa.wire.coordinator import ReportSource
from emosa.wire.disassociation import DisassociationCoordinator, FinalSession, TrafficCounters

pytestmark = pytest.mark.unit
STA = bytes.fromhex("020000000020")


class Rig:
    def __init__(self):
        self.now = 10.0
        self.binding, self.caps, topology = fixtures()
        self.bssid = topology.operational.radios[0].bsses[0].ap_mac
        self.topology = replace(
            topology,
            clients=AssociatedClients((BssClients(self.bssid, ()),)),
            inventory_complete=True,
        )
        self.source = ReportSource(self.binding, "pod-1", "a" * 64, clock=lambda: self.now)
        self.source.publish((1, 1), self.caps, self.topology, observed_at=self.now, lifetime=2)
        self.sent = []
        self.session = DisassociationCoordinator(
            self.source,
            self.sent.append,
            MidSequence(65534),
            clock=lambda: self.now,
        )
        self.event = FinalSession(
            self.source.current().context_token,
            "association-1",
            self.bssid,
            STA,
            self.now,
            3,
            TrafficCounters(1, 2, 3, 4, 5, 6, 7),
        )

    def publish(self, revision=(1, 2), topology=None):
        self.source.publish(
            revision, self.caps, topology or self.topology, observed_at=self.now, lifetime=2
        )

    def ack(
        self,
        mid,
        *,
        source=None,
        destination=None,
        ingress=None,
        generation=1,
        tlvs=(),
        relay=False,
    ):
        frame = fragment_message(
            destination or self.binding.local_al,
            source or self.binding.controller_al,
            0x8000,
            mid,
            tlvs,
            relay=relay,
        )[0]
        return self.session.ack(
            frame, ingress=ingress or self.binding.ingress, generation=generation
        )


@pytest.fixture
def rig():
    value = Rig()
    yield value
    value.session.close()


def test_literal_message_and_ack_no_ovsdb_side_effect(rig):
    assert rig.session.submit(rig.event) == "final_session_report_sent"
    packet = Reassembler().feed(rig.sent[0])
    assert (packet.message_type, packet.mid, packet.relay) == (0x8022, 65535, False)
    assert packet.tlvs == (
        Tlv(0x95, STA),
        Tlv(0xCA, bytes.fromhex("0003")),
        Tlv(0xA2, STA + bytes.fromhex("00000001000000020000000300000004000000050000000600000007")),
    )
    rig.now += 0.3
    assert rig.ack(65535) == "final_session_acknowledged"
    assert len(rig.sent) == 1 and not rig.session.pending
    assert rig.session.submit(rig.event) == "duplicate_final_session"
    with pytest.raises(EmosaError):
        rig.session.submit(replace(rig.event, reason=8))
    assert len(rig.sent) == 1


def test_counter_scaling_before_rollover_and_profile1_units():
    counters = TrafficCounters(2**42 + 2049, 2**32 + 1, 2**32 + 3, 4, 5, 6, 7)
    byte = counters.tlv(STA, profile=1, byte_units=0).value
    kib = counters.tlv(STA, profile=2, byte_units=1).value
    mib = counters.tlv(STA, profile=3, byte_units=2).value
    assert byte[6:18] == bytes.fromhex("000008010000000100000003")
    assert kib[6:18] == bytes.fromhex("000000020040000000000003")
    assert mib[6:14] == bytes.fromhex("0040000000001000")
    for profile, units in ((1, 1), (1, 2), (2, 3), (True, 0), (4, 0), (2, True)):
        with pytest.raises(EmosaError):
            counters.tlv(STA, profile=profile, byte_units=units)


def test_absent_proto2_counters_cannot_become_zeros():
    report = Report(nodeID="fixture")
    stats = (
        report.clients.add(band=0, channel=6).client_list.add(mac_address="02:00:00:00:00:20").stats
    )
    fields = (
        "tx_bytes",
        "rx_bytes",
        "tx_frames",
        "rx_frames",
        "tx_errors",
        "rx_errors",
        "tx_retries",
    )
    for field in fields:
        with pytest.raises(EmosaError):
            TrafficCounters.from_qualified_opensync(stats)
        setattr(stats, field, 0)
    assert TrafficCounters.from_qualified_opensync(stats) == TrafficCounters(0, 0, 0, 0, 0, 0, 0)
    for field in fields:
        copy = type(stats)()
        copy.CopyFrom(stats)
        copy.ClearField(field)
        with pytest.raises(EmosaError):
            TrafficCounters.from_qualified_opensync(copy)


@pytest.mark.parametrize("value", [None, -1, 1.5, True, 2**64])
def test_counters_require_explicit_unsigned_values(value):
    with pytest.raises(EmosaError):
        TrafficCounters(1, 2, 3, 4, 5, value, 7)


def test_fixed_retry_deadline_survives_new_observation_revisions(rig):
    rig.session.submit(rig.event)
    for t, revision in ((10.3, 2), (10.6, 3), (10.9, 4)):
        rig.now = t
        rig.publish((1, revision))
        rig.session.tick()
    assert [Reassembler().feed(f).mid for f in rig.sent] == [65535, 0, 1]
    rig.now = 11
    rig.session.tick()
    assert not rig.session.pending
    assert rig.session.counts["final_session_ack_timeout"] == 1
    assert rig.ack(0) is None


@pytest.mark.parametrize(
    "change", ["generation", "invalidate", "incomplete", "rejoin", "bss_removed"]
)
def test_source_change_withdraws_final_statistics(rig, change):
    rig.session.submit(rig.event)
    rig.now = 10.3
    if change == "invalidate":
        rig.source.invalidate()
    elif change == "generation":
        rig.publish((2, 1))
    elif change == "incomplete":
        rig.publish(topology=replace(rig.topology, inventory_complete=False))
    elif change == "rejoin":
        rig.publish(
            topology=replace(
                rig.topology,
                clients=AssociatedClients((BssClients(rig.bssid, (AssociatedClient(STA, 0),)),)),
            )
        )
    else:
        other = bytes.fromhex("020000009999")
        radios = tuple(
            replace(r, bsses=tuple(replace(b, ap_mac=other) for b in r.bsses))
            for r in rig.topology.operational.radios
        )
        rig.publish(
            topology=replace(
                rig.topology, operational=replace(rig.topology.operational, radios=radios)
            )
        )
    rig.session.tick()
    assert len(rig.sent) == 1 and not rig.session.pending


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": bytes.fromhex("02000000ffff")},
        {"generation": 9},
        {"ingress": "wrong"},
        {"tlvs": (Tlv(0xA3, b"\x03" + STA),)},
        {"tlvs": (Tlv(0xAB, b"bad"),)},
        {"relay": True},
    ],
)
def test_unbound_or_error_ack_does_not_complete_report(rig, kwargs):
    rig.session.submit(rig.event)
    with pytest.raises(EmosaError):
        rig.ack(65535, **kwargs)
    assert rig.session.pending


def test_stale_future_foreign_context_or_inconsistent_units_never_send(rig):
    for event in (
        replace(rig.event, observed_at=8),
        replace(rig.event, observed_at=10.1),
        replace(rig.event, context="old"),
        replace(rig.event, bssid=bytes.fromhex("020000009999")),
    ):
        with pytest.raises(EmosaError):
            rig.session.submit(event)
    rig.caps = replace(rig.caps, profile2=replace(rig.caps.profile2, flags=0x40))
    rig.publish()
    with pytest.raises(EmosaError):
        rig.session.submit(rig.event)
    assert not rig.sent and not rig.session.pending


def test_pending_memory_bound_and_distinct_associations(rig):
    for n in range(16):
        event = replace(
            rig.event,
            session=f"association-{n}",
            station=bytes.fromhex("0200000000") + bytes([n + 1]),
        )
        rig.session.submit(event)
    assert len(rig.session.pending) == 16
    assert (
        rig.session.submit(replace(rig.event, session="extra")) == "final_session_budget_exhausted"
    )
    assert len(rig.sent) == 16
    rig.session.close()
    with pytest.raises(EmosaError):
        rig.session.submit(rig.event)


@pytest.mark.parametrize("reason", [0, 40, 45, 70, 72, 65535, True, None])
def test_reserved_or_missing_reasons_are_not_transmitted(rig, reason):
    with pytest.raises(EmosaError):
        replace(rig.event, reason=reason)
    assert rig.sent == []


def test_changed_advertised_units_cannot_rescale_pending_report(rig):
    rig.session.profile = 2
    rig.session.submit(rig.event)
    rig.caps = replace(rig.caps, profile2=replace(rig.caps.profile2, flags=0x40))
    rig.publish()
    rig.now = 10.3
    rig.session.tick()
    assert not rig.session.pending and len(rig.sent) == 1


def test_failed_send_does_not_claim_report_or_leave_retry_queue(rig):
    def fail(_):
        raise OSError("injected packet socket failure")

    rig.session.send_frame = fail
    with pytest.raises(OSError):
        rig.session.submit(rig.event)
    assert not rig.session.pending and rig.sent == []
    assert rig.session.counts == {"final_session_send_failed": 1}
