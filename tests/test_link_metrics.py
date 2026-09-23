from dataclasses import replace

import pytest

from emosa.errors import EmosaError, Reason
from emosa.simulation.wire_reports import fixtures
from emosa.wire.cmdu import Reassembler, Tlv, fragment_message
from emosa.wire.coordinator import ReportSource
from emosa.wire.link_metrics import (
    LinkBinding,
    LinkMetricCoordinator,
    LinkMetricQuery,
    LinkMetrics,
    LinkMetricSource,
    RxLink,
    TxLink,
    decode_metrics,
    decode_query,
)
from emosa.wire.topology_values import LocalInterface, Neighbor, Neighbors1905

pytestmark = pytest.mark.unit
PEER_INTERFACE = bytes.fromhex("020000005002")
OTHER_NEIGHBOR = bytes.fromhex("020000005099")


class Rig:
    def __init__(self):
        self.now = 10.0
        self.binding, self.caps, self.topology = fixtures()
        self.reports = ReportSource(self.binding, "pod-1", "a" * 64, clock=lambda: self.now)
        self.refresh()
        self.source = LinkMetricSource(self.reports, clock=lambda: self.now)
        self.inventory = (
            LinkBinding(
                self.binding.controller_al, self.binding.local_al, PEER_INTERFACE, 1, False
            ),
        )
        self.tx = LinkMetrics(
            self.binding.local_al,
            self.binding.controller_al,
            (TxLink(self.binding.local_al, PEER_INTERFACE, 1, False, 3, 513, 940, 97, 65535),),
        )
        self.rx = LinkMetrics(
            self.binding.local_al,
            self.binding.controller_al,
            (RxLink(self.binding.local_al, PEER_INTERFACE, 1, 2, 1027, 255),),
        )
        self.sent = []
        self.coordinator = LinkMetricCoordinator(
            self.source, self.sent.append, clock=lambda: self.now
        )

    def refresh(self, revision=(1, 1), **kwargs):
        self.reports.publish(
            revision, self.caps, self.topology, observed_at=self.now, lifetime=2, **kwargs
        )

    def publish(self, **overrides):
        values = dict(
            context_token=self.reports.current().context_token,
            counter_epoch="boot-1/interface-1",
            interval_started=self.now - 1,
            observed_at=self.now,
            inventory=self.inventory,
            metrics=(self.tx, self.rx),
            inventory_complete=True,
        )
        values.update(overrides)
        return self.source.publish(**values)

    def query(self, neighbor=None, direction=2, **overrides):
        values = dict(
            destination=self.binding.local_al,
            source=self.binding.controller_al,
            message_type=5,
            mid=345,
            tlvs=(LinkMetricQuery(neighbor, direction).tlv(),),
        )
        values.update(overrides)
        assembled = Reassembler()
        for frame in fragment_message(**values):
            message = assembled.feed(frame, ingress=self.binding.ingress)
        return message

    def handle(self, message=None, **overrides):
        args = dict(ingress=self.binding.ingress, generation=self.binding.generation)
        args.update(overrides)
        return self.coordinator.handle(message or self.query(), self.now, **args)


@pytest.fixture
def rig():
    return Rig()


@pytest.mark.parametrize("direction", [0, 1, 2])
@pytest.mark.parametrize("neighbor", [None, OTHER_NEIGHBOR])
def test_query_conditional_layout(direction, neighbor):
    query = LinkMetricQuery(neighbor, direction)
    assert query.tlv().value == (b"\0" if neighbor is None else b"\1" + neighbor) + bytes(
        [direction]
    )
    assert decode_query((Tlv(222, b"vendor"), query.tlv())) == query


@pytest.mark.parametrize("value", [b"", b"\0", b"\0" * 8, b"\1\2", b"\1" + bytes(8)])
def test_wrong_query_lengths_cannot_shift_the_requested_direction(value):
    with pytest.raises(EmosaError):
        decode_query((Tlv(8, value),))


@pytest.mark.parametrize("value", [b"\2\0", b"\0\3", b"\1" + OTHER_NEIGHBOR + b"\xff"])
def test_reserved_query_enumerations_ignore_the_entire_tlv(value):
    with pytest.raises(EmosaError) as error:
        decode_query((Tlv(8, value),))
    assert error.value.code == Reason.UNSUPPORTED_OPERATION


@pytest.mark.parametrize("tlvs", [(), (Tlv(8, b"\0\2"),) * 2])
def test_query_requires_exactly_one_metric_tlv(tlvs):
    with pytest.raises(EmosaError):
        decode_query(tlvs)


def test_literal_tx_rx_layouts_and_amendment_unspecified_sentinels(rig):
    prefix = (
        rig.binding.local_al + rig.binding.controller_al + rig.binding.local_al + PEER_INTERFACE
    )
    assert rig.tx.tlv() == Tlv(9, prefix + bytes.fromhex("000100000000030000020103ac0061ffff"))
    assert rig.rx.tlv() == Tlv(10, prefix + bytes.fromhex("00010000000200000403ff"))
    assert decode_metrics(rig.tx.tlv()) == rig.tx
    assert decode_metrics(rig.rx.tlv()) == rig.rx
    wifi = replace(rig.tx.links[0], media_type=0x103)
    assert replace(rig.tx, links=(wifi,)).tlv().value[-2:] == b"\xff\xff"
    with pytest.raises(EmosaError):
        replace(wifi, phy_rate_mbps=65).encode()
    with pytest.raises(EmosaError):
        replace(rig.rx.links[0], rssi_db=42).encode()


@pytest.mark.parametrize(
    "field,value",
    [
        ("packet_errors", -1),
        ("transmitted_packets", 2**32),
        ("mac_throughput_mbps", 65536),
        ("availability_percent", 101),
        ("availability_percent", True),
        ("bridges_present", 1),
        ("media_type", 0x1234),
    ],
)
def test_unknown_invalid_or_overflowed_values_are_not_coerced(rig, field, value):
    with pytest.raises(EmosaError):
        replace(rig.tx.links[0], **{field: value}).encode()


@pytest.mark.parametrize("change", ["partial", "empty", "bridge_reserved", "duplicate"])
def test_incomplete_or_reserved_metric_tlv_rejected(rig, change):
    value = rig.tx.tlv().value
    if change == "partial":
        value = value[:-1]
    elif change == "empty":
        value = value[:12]
    elif change == "bridge_reserved":
        value = value[:26] + b"\2" + value[27:]
    else:
        value += value[12:]
    with pytest.raises(EmosaError):
        decode_metrics(Tlv(9, value))


@pytest.mark.parametrize("direction,kinds", [(0, [9]), (1, [10]), (2, [9, 10])])
def test_only_requested_directions_same_mid_without_ack(rig, direction, kinds):
    rig.publish()
    assert rig.handle(rig.query(direction=direction)) == "neighbor_link_metric_response_sent"
    message = Reassembler().feed(rig.sent[0])
    assert (message.message_type, message.mid, message.relay) == (6, 345, False)
    assert (
        message.source == rig.binding.local_al and message.destination == rig.binding.controller_al
    )
    assert [t.kind for t in message.tlvs] == kinds
    assert len(rig.sent) == 1


def test_missing_valid_neighbor_metrics_is_not_an_invalid_neighbor(rig):
    assert rig.handle() == "neighbor_measurement_unavailable"
    rig.publish(metrics=(rig.rx,))
    assert rig.handle() == "neighbor_measurement_unavailable"
    assert rig.sent == []
    assert rig.handle(rig.query(direction=1)) == "neighbor_link_metric_response_sent"
    rig.sent.clear()
    assert rig.handle(rig.query(OTHER_NEIGHBOR)) == "neighbor_link_metric_response_sent"
    assert Reassembler().feed(rig.sent[0]).tlvs == (Tlv(12, b"\0"),)


def test_empty_all_neighbor_response_requires_fresh_complete_empty_inventory(rig):
    rig.topology = replace(rig.topology, neighbors1905=())
    rig.refresh((1, 2))
    rig.publish(inventory=(), metrics=())
    rig.handle()
    assert Reassembler().feed(rig.sent[0]).tlvs == ()


@pytest.mark.parametrize(
    "change", ["context", "incomplete", "omitted", "foreign_peer", "bridge", "interval", "future"]
)
def test_invalid_measurement_revokes_previously_usable_source(rig, change):
    rig.publish()
    rig.now += 0.1
    args = {}
    if change == "context":
        args["context_token"] = "other-control-session"
    elif change == "incomplete":
        args["inventory_complete"] = False
    elif change == "omitted":
        args["inventory"] = ()
    elif change == "foreign_peer":
        args["metrics"] = (replace(rig.tx, neighbor_al=OTHER_NEIGHBOR),)
    elif change == "bridge":
        args["inventory"] = (replace(rig.inventory[0], bridges_present=True),)
    elif change == "interval":
        args["interval_started"] = rig.now
    elif change == "future":
        args["observed_at"] = rig.now + 1
    with pytest.raises(EmosaError):
        rig.publish(**args)
    assert rig.source.current() is None
    assert rig.handle() == "neighbor_measurement_unavailable" and not rig.sent


def test_replay_watermark_survives_explicit_invalidation(rig):
    rig.publish()
    rig.source.invalidate()
    with pytest.raises(EmosaError):
        rig.publish()
    assert rig.source.current() is None


def test_client_telemetry_expiry_does_not_expire_independent_link_sample(rig):
    rig.refresh((1, 2), telemetry_valid_until=rig.now + 0.2)
    rig.publish()
    rig.now += 0.3
    assert not rig.reports.current().topology.inventory_complete
    assert rig.handle() == "neighbor_link_metric_response_sent"
    rig.now += 1.8
    assert rig.handle() == "neighbor_measurement_unavailable"


@pytest.mark.parametrize("change", ["generation", "bridge", "media"])
def test_control_generation_or_topology_change_withdraws_bound_metrics(rig, change):
    rig.publish()
    if change == "generation":
        rig.refresh((2, 1))
    elif change == "bridge":
        rig.topology = replace(
            rig.topology,
            neighbors1905=(
                Neighbors1905(rig.binding.local_al, (Neighbor(rig.binding.controller_al, True),)),
            ),
        )
        rig.refresh((1, 2))
    else:
        rig.topology = replace(
            rig.topology,
            device=replace(
                rig.topology.device,
                interfaces=(
                    LocalInterface(rig.binding.local_al, 0, b""),
                    *rig.topology.device.interfaces[1:],
                ),
            ),
        )
        rig.refresh((1, 2))
    assert rig.source.current() is None
    assert rig.handle() == "neighbor_measurement_unavailable" and not rig.sent


@pytest.mark.parametrize("args", [dict(ingress="other"), dict(generation=2)])
def test_wrong_ingress_or_generation_cannot_read_metrics(rig, args):
    rig.publish()
    with pytest.raises(EmosaError):
        rig.handle(**args)
    assert not rig.sent


def test_source_loss_during_send_is_not_success(rig):
    rig.publish()

    def lose_authority(frame):
        rig.sent.append(frame)
        rig.reports.invalidate()

    rig.coordinator.send_frame = lose_authority
    with pytest.raises(EmosaError):
        rig.handle()
    assert len(rig.sent) == 1
    assert not rig.coordinator.counts.get("response_sent")


def test_multiple_interface_pairs_and_neighbors_are_complete_and_selectable(rig):
    local2 = bytes.fromhex("020000005003")
    peer2 = bytes.fromhex("020000005004")
    rig.topology = replace(
        rig.topology,
        device=replace(
            rig.topology.device,
            interfaces=(*rig.topology.device.interfaces, LocalInterface(local2, 1, b"")),
        ),
        neighbors1905=(
            *rig.topology.neighbors1905,
            Neighbors1905(
                local2,
                (
                    Neighbor(rig.binding.controller_al, True),
                    Neighbor(OTHER_NEIGHBOR, True),
                ),
            ),
        ),
    )
    rig.refresh((1, 2))
    rig.inventory += (
        LinkBinding(rig.binding.controller_al, local2, peer2, 1, True),
        LinkBinding(OTHER_NEIGHBOR, local2, OTHER_NEIGHBOR, 1, True),
    )
    tx2 = replace(
        rig.tx.links[0], local_interface=local2, neighbor_interface=peer2, bridges_present=True
    )
    tx = replace(rig.tx, links=(*rig.tx.links, tx2))
    other = replace(
        rig.tx, neighbor_al=OTHER_NEIGHBOR, links=(replace(tx2, neighbor_interface=OTHER_NEIGHBOR),)
    )
    rig.publish(metrics=(tx, other))
    rig.handle(rig.query(direction=0))
    message = Reassembler().feed(rig.sent[-1])
    assert len(message.tlvs) == 2
    assert sorted(len(decode_metrics(t).links) for t in message.tlvs) == [1, 2]
    rig.handle(rig.query(OTHER_NEIGHBOR, direction=0))
    assert Reassembler().feed(rig.sent[-1]).tlvs == (other.tlv(),)


@pytest.mark.parametrize(
    "change", ["counter_epoch_missing", "peer_pair", "partial_direction", "media", "bridge"]
)
def test_publisher_cannot_substitute_unattributed_or_partial_link_values(rig, change):
    args = {}
    if change == "counter_epoch_missing":
        args["counter_epoch"] = ""
    elif change == "peer_pair":
        args["metrics"] = (
            replace(rig.tx, links=(replace(rig.tx.links[0], neighbor_interface=OTHER_NEIGHBOR),)),
        )
    elif change == "partial_direction":
        args["inventory"] = (
            *rig.inventory,
            replace(rig.inventory[0], neighbor_interface=OTHER_NEIGHBOR),
        )
    else:
        fields = {"media_type": 0} if change == "media" else {"bridges_present": True}
        args["metrics"] = (replace(rig.tx, links=(replace(rig.tx.links[0], **fields),)),)
    with pytest.raises(EmosaError):
        rig.publish(**args)
    assert rig.source.current() is None


@pytest.mark.parametrize("change", ["late", "future", "closed", "relay", "foreign"])
def test_response_requires_live_timely_bound_unicast_request(rig, change):
    rig.publish()
    message = rig.query()
    received_at = rig.now
    if change == "late":
        received_at -= 1
    elif change == "future":
        received_at += 1
    elif change == "closed":
        rig.coordinator.close()
    elif change == "relay":
        message = rig.query(relay=True)
    else:
        message = rig.query(source=OTHER_NEIGHBOR)
    with pytest.raises(EmosaError):
        rig.coordinator.handle(
            message, received_at, ingress=rig.binding.ingress, generation=rig.binding.generation
        )
    assert not rig.sent


def test_early_query_waits_for_observed_interval_with_original_mid(rig):
    assert rig.handle() == "neighbor_measurement_unavailable"
    rig.now += 0.141  # Native failure: complete interval published 140 ms after query.
    rig.publish()
    rig.coordinator.tick()
    reply = Reassembler().feed(rig.sent[0])
    assert reply.message_type == 6 and reply.mid == 345
    assert not rig.coordinator.waiting
    rig.coordinator.tick()
    assert len(rig.sent) == 1


@pytest.mark.parametrize(
    "loss", ["deadline", "duplicate_deadline", "context", "topology", "close", "source"]
)
def test_early_query_cannot_cross_deadline_or_authority(rig, loss):
    rig.handle()
    rig.now += 0.5
    if loss == "duplicate_deadline":
        rig.handle()  # Must not move the deadline to 11.5.
    if loss in ("deadline", "duplicate_deadline"):
        rig.now = 11
    elif loss == "context":
        rig.refresh((2, 1))
    elif loss == "topology":
        rig.topology = replace(rig.topology, neighbors1905=())
        rig.refresh((1, 2))
    elif loss == "close":
        rig.coordinator.close()
    elif loss == "source":
        rig.reports.invalidate()
    if loss not in ("source", "topology"):
        rig.publish()
    rig.coordinator.tick()
    assert not rig.sent and not rig.coordinator.waiting


def test_waiting_queries_have_bounded_storage_and_partial_send_is_not_retried(rig):
    for mid in range(10):
        rig.handle(rig.query(mid=mid))
    assert len(rig.coordinator.waiting) == 4
    rig.now += 0.1
    rig.publish()

    def fail(frame):
        rig.sent.append(frame)
        raise OSError("uncertain send")

    rig.coordinator.send_frame = fail
    rig.coordinator.tick()
    assert len(rig.sent) == 4 and not rig.coordinator.waiting
    rig.coordinator.tick()
    assert len(rig.sent) == 4


def test_duplicate_pending_query_with_uncertain_send_is_not_retried(rig):
    rig.handle()
    rig.now += 0.1
    rig.publish()

    def fail(frame):
        rig.sent.append(frame)
        raise OSError("uncertain duplicate-triggered send")

    rig.coordinator.send_frame = fail
    with pytest.raises(OSError):
        rig.handle()
    assert len(rig.sent) == 1 and not rig.coordinator.waiting
    rig.coordinator.tick()
    assert len(rig.sent) == 1
