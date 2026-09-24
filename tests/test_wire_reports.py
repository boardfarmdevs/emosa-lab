import json
from dataclasses import replace
from pathlib import Path

import pytest

from emosa.easymesh_payloads import (
    AKMSuiteCapabilities,
    APCapability,
    APRadioAdvancedCapabilities,
    AssociatedClient,
    AssociatedClients,
    BasicOperatingClass,
    BssClients,
    BssConfigurationReport,
    ConfiguredBss,
    ConfiguredRadio,
    SupportedCipherSuites,
    decode_value,
    encode_value,
)
from emosa.errors import EmosaError
from emosa.evaluation.payloads import inspect_value
from emosa.simulation.wire_reports import (
    AGENT,
    BSSID,
    CONTROLLER,
    RUID,
    SSID,
    fixtures,
    main,
    query_frames,
)
from emosa.wire.cmdu import MidSequence, Reassembler, Tlv
from emosa.wire.inspection import inspect_capture
from emosa.wire.reports import ReportStamp, early_report, topology_response
from emosa.wire.topology_values import (
    BridgingCapability,
    DeviceInformation,
    LocalInterface,
    Neighbor,
    Neighbors1905,
    Non1905Neighbors,
    decode_topology,
    encode_topology,
)

pytestmark = pytest.mark.unit
STAMP = ReportStamp("synthetic-pod-1/revision-1/inputs-1", 0, 2)


def assemble(frames):
    parser = Reassembler()
    result = None
    for frame in frames:
        result = parser.feed(frame)
    assert result is not None
    return result


def reply(facts=None, query=None, **kwargs):
    binding, _, default = fixtures()
    return topology_response(
        query or assemble(query_frames()),
        binding,
        facts or default,
        STAMP,
        **({"ingress": "fixture", "generation": 1, "received_at": 0, "clock": lambda: 0} | kwargs),
    )


def early(facts=None):
    binding, default, _ = fixtures()
    return early_report(binding, facts or default, STAMP, MidSequence(65535), clock=lambda: 0)


def test_security_selectors_are_counted_four_octet_values_and_empty_means_empty():
    assert (
        encode_value(AKMSuiteCapabilities((), (bytes.fromhex("000fac02"),))).hex() == "0001000fac02"
    )
    assert encode_value(SupportedCipherSuites((bytes.fromhex("000fac04"),))).hex() == "01000fac04"
    assert decode_value(0xCC, b"\0\0") == AKMSuiteCapabilities((), ())
    assert decode_value(0xED, b"\0") == SupportedCipherSuites(())
    vendor = bytes.fromhex("506f9a02")
    assert decode_value(0xED, b"\1" + vendor).selectors == (vendor,)
    assert inspect_value(0xCC, bytes.fromhex("0001000fac02"))["decoded"]["fronthaul_selectors"] == [
        "000fac02"
    ]


@pytest.mark.parametrize(
    "kind,value",
    [
        (0xCC, b""),
        (0xCC, b"\0"),
        (0xCC, b"\0\1abc"),
        (0xCC, b"\0\0x"),
        (0xED, b"\1abc"),
        (0xED, b"\0x"),
    ],
)
def test_malformed_security_counts_never_become_partial_capabilities(kind, value):
    with pytest.raises(EmosaError):
        decode_value(kind, value)


def test_configuration_flags_ignore_reserved_receive_bits_and_zero_them_on_transmit():
    payload = BssConfigurationReport((ConfiguredRadio(RUID, (ConfiguredBss(BSSID, 0x40, b"x"),)),))
    value = encode_value(payload)
    assert value == b"\1" + RUID + b"\1" + BSSID + b"\x40\0\1x"
    reserved = value[:14] + b"\x43\xff" + value[16:]
    assert decode_value(0xB7, reserved) == payload
    assert encode_value(decode_value(0xB7, reserved)) == value
    assert "ssid_hex" in inspect_value(0xB7, value)["decoded"]["radios"][0]["bsses"][0]
    with pytest.raises(EmosaError):
        encode_value(
            BssConfigurationReport((ConfiguredRadio(RUID, (ConfiguredBss(BSSID, 0x41, b"x"),)),))
        )


@pytest.mark.parametrize(
    "seconds,expected", [(0, 0), (1, 1), (65534, 65534), (65535, 65535), (90000, 65535)]
)
def test_client_age_is_big_endian_seconds_saturated_at_65535(seconds, expected):
    value = encode_value(
        AssociatedClients((BssClients(BSSID, (AssociatedClient(CONTROLLER, seconds),)),))
    )
    assert value == b"\1" + BSSID + b"\0\1" + CONTROLLER + expected.to_bytes(2, "big")
    assert decode_value(0x84, value).bsses[0].clients[0].association_seconds == expected
    assert inspect_value(0x84, value)["decoded"]["bsses"][0]["clients"][0]["age_saturated"] == (
        expected == 65535
    )


@pytest.mark.parametrize("seconds", [None, -1, True, 1.5])
def test_unknown_association_age_cannot_be_fabricated(seconds):
    with pytest.raises(EmosaError):
        encode_value(
            AssociatedClients((BssClients(BSSID, (AssociatedClient(CONTROLLER, seconds),)),))
        )


def test_literal_ieee_topology_values_and_reserved_neighbor_bits():
    info = DeviceInformation(AGENT, (LocalInterface(AGENT, 1, b""),))
    literal = AGENT + b"\1" + AGENT + b"\0\1\0"
    assert encode_topology(info) == literal
    assert decode_topology(3, literal) == info
    bridge = BridgingCapability(((AGENT, BSSID),))
    assert encode_topology(bridge) == b"\1\2" + AGENT + BSSID
    neighbors = Neighbors1905(AGENT, (Neighbor(CONTROLLER, True),))
    assert decode_topology(7, AGENT + CONTROLLER + b"\xff") == neighbors
    assert encode_topology(neighbors) == AGENT + CONTROLLER + b"\x80"
    assert decode_topology(6, AGENT + CONTROLLER) == Non1905Neighbors(AGENT, (CONTROLLER,))


@pytest.mark.parametrize(
    "kind,value",
    [
        (3, b""),
        (3, AGENT + b"\1"),
        (4, b"\1\2" + AGENT),
        (6, AGENT + b"\1"),
        (7, AGENT + CONTROLLER),
        (7, AGENT + CONTROLLER + b"\0\0"),
    ],
)
def test_partial_ieee_topology_records_are_rejected(kind, value):
    with pytest.raises(EmosaError):
        decode_topology(kind, value)


@pytest.mark.parametrize(
    "media,size", [(0, 1), (1, 10), (0x103, 0), (0x108, 10), (0x109, 10), (0x10A, 0)]
)
def test_media_lengths_follow_selected_ieee_and_easymesh_tables(media, size):
    value = AGENT + b"\1" + BSSID + media.to_bytes(2, "big") + bytes([size]) + bytes(size)
    with pytest.raises(EmosaError):
        decode_topology(3, value)


def test_wifi6_and_wifi7_media_use_zero_specific_bytes_and_legacy_reserved_role_is_ignored():
    for media in (0x108, 0x109):
        info = DeviceInformation(AGENT, (LocalInterface(BSSID, media, b""),))
        assert decode_topology(3, encode_topology(info)) == info
    raw = AGENT + b"\1" + BSSID + b"\1\3\x0a" + BSSID + b"\x0f\0\6\0"
    decoded = decode_topology(3, raw)
    with pytest.raises(EmosaError):
        encode_topology(decoded)  # Reserved receive bits cannot be sent back blindly.
    with pytest.raises(EmosaError):
        decode_topology(3, raw[:-4] + b"\x10\0\6\0")


def test_independent_native_report_fields_and_selected_edition_failure():
    cases = json.loads(Path("tests/fixtures/protocol/reports/native-values.json").read_text())
    assert len(cases) == 5
    for case in cases:
        kind, value = case["type"], bytes.fromhex(case["value_hex"])
        expected = case["decoded"]
        if kind == 3:
            assert any(
                i["media_type"] == 0x108 and i["media_length"] == 10 for i in expected["interfaces"]
            )
            with pytest.raises(EmosaError):
                decode_topology(kind, value)
        elif kind == 6:
            decoded = decode_topology(kind, value)
            assert decoded.local_interface.hex() == expected["local_interface"]
            assert [n.hex() for n in decoded.neighbors] == expected["neighbors"]
            assert encode_topology(decoded) == value
        elif kind == 0xCC:
            decoded = decode_value(kind, value)
            assert len(decoded.backhaul) == expected["backhaul_count"] == 0
            assert len(decoded.fronthaul) == expected["fronthaul_count"] == 0
        else:
            decoded = decode_value(kind, value)
            assert decoded.radios[0].ruid.hex() == expected["ruid"]
            assert [
                {"bssid": b.bssid.hex(), "flags": b.flags, "ssid_hex": b.ssid.hex()}
                for b in decoded.radios[0].bsses
            ] == expected["bsses"]
            assert encode_value(decoded) == value


def test_complete_early_report_includes_all_selected_companions_and_mid_rollover():
    report = early()
    message = assemble(report.frames)
    assert message.message_type == 0x8043 and message.mid == 0 and not message.relay
    assert {t.kind for t in message.tlvs} == {0xA1, 0x85, 0x86, 0xBE, 0xCC, 0xB4, 0xED}
    assert next(t.value for t in message.tlvs if t.kind == 0xED) == bytes.fromhex("01000fac04")


@pytest.mark.parametrize("field", ["vht", "he", "eht", "ht"])
@pytest.mark.parametrize("value", [None, True])
def test_unknown_or_unsupported_radio_features_block_the_entire_early_report(field, value):
    _, caps, _ = fixtures()
    with pytest.raises(EmosaError):
        early(replace(caps, radios=(replace(caps.radios[0], **{field: value}),)))


def test_missing_inventories_and_unimplemented_capabilities_are_never_invented():
    _, caps, _ = fixtures()
    for changed in (
        replace(caps, inventory_complete=False),
        replace(caps, ap=APCapability(0x80)),
        replace(caps, akm=AKMSuiteCapabilities((), ())),
        replace(caps, radios=()),
    ):
        with pytest.raises(EmosaError):
            early(changed)
    radio = caps.radios[0]
    for changed in (
        replace(radio, advanced=APRadioAdvancedCapabilities(AGENT, 0)),
        replace(
            radio, basic=replace(radio.basic, operating_classes=(BasicOperatingClass(128, 20, ()),))
        ),
    ):
        with pytest.raises(EmosaError):
            early(replace(caps, radios=(changed,)))


def test_topology_reply_preserves_mid_and_complete_ieee_and_easymesh_inventory():
    report = reply()
    message = assemble(report.frames)
    assert message.mid == 65535 and message.message_type == 3
    assert {t.kind for t in message.tlvs} == {3, 4, 7, 0x80, 0x83, 0xB7, 0xB3}
    assert message.destination == CONTROLLER and message.source == AGENT
    assert SSID.decode() not in repr(report)


@pytest.mark.parametrize(
    "field",
    [
        "inventory_complete",
        "powered_off_interfaces_absent",
        "l2_neighbor_records_absent",
        "mld_backhaul_vbss_tid_policy_absent",
    ],
)
def test_unknown_conditional_topology_obligations_prevent_response(field):
    _, _, facts = fixtures()
    with pytest.raises(EmosaError):
        reply(replace(facts, **{field: None}))


def test_configuration_and_state_graph_cannot_disagree_in_response():
    _, _, facts = fixtures()
    for changed in (
        replace(facts, clients=AssociatedClients(())),
        replace(facts, device=replace(facts.device, al_mac=CONTROLLER)),
        replace(facts, bridges=BridgingCapability(((AGENT, CONTROLLER),))),
        replace(
            facts,
            configuration=BssConfigurationReport(
                (ConfiguredRadio(RUID, (ConfiguredBss(BSSID, 0x40, b"different"),)),)
            ),
        ),
        *(
            replace(
                facts,
                configuration=BssConfigurationReport(
                    (ConfiguredRadio(RUID, (ConfiguredBss(BSSID, flags, SSID),)),)
                ),
            )
            for flags in (0xC0, 0x20)  # combined fronthaul+backhaul, reserved bit
        ),
        replace(facts, device=replace(facts.device, interfaces=facts.device.interfaces[:1])),
    ):
        with pytest.raises(EmosaError):
            reply(changed)


def test_a_pure_backhaul_bss_is_reported():
    _, _, facts = fixtures()
    backhaul = replace(
        facts,
        configuration=BssConfigurationReport(
            (ConfiguredRadio(RUID, (ConfiguredBss(BSSID, 0x80, SSID),)),)
        ),
    )
    assert reply(backhaul) is not None


def test_nonempty_clients_require_ages_and_are_reported_on_the_correct_bss():
    _, _, facts = fixtures()
    client = bytes.fromhex("020000004020")
    facts = replace(
        facts, clients=AssociatedClients((BssClients(BSSID, (AssociatedClient(client, 17),)),))
    )
    msg = assemble(reply(facts).frames)
    assert (
        decode_value(0x84, next(t.value for t in msg.tlvs if t.kind == 0x84))
        .bsses[0]
        .clients[0]
        .association_seconds
        == 17
    )
    bad = replace(
        facts, clients=AssociatedClients((BssClients(CONTROLLER, (AssociatedClient(client, 17),)),))
    )
    with pytest.raises(EmosaError):
        reply(bad)


@pytest.mark.parametrize(
    "change",
    [
        {"mid": 42, "relay": True},
        {"source": BSSID},
        {"destination": BSSID},
        {"message_type": 0x8001},
        {"tlvs": ()},
        {"tlvs": (Tlv(0xB3, b""),)},
        {"tlvs": (Tlv(0xB3, b"\1"), Tlv(0xAB, b""))},
    ],
)
def test_wrong_or_unprocessable_query_never_gets_a_report(change):
    with pytest.raises(EmosaError):
        reply(query=replace(assemble(query_frames()), **change))


@pytest.mark.parametrize("profile", [1, 2, 3, 255])
def test_topology_query_sender_profile_does_not_upgrade_our_response(profile):
    report = reply(query=replace(assemble(query_frames()), tlvs=(Tlv(0xB3, bytes([profile])),)))
    message = assemble(report.frames)
    assert next(t.value for t in message.tlvs if t.kind == 0xB3) == b"\1"


def test_response_budget_is_one_second_not_the_pod_apply_deadline():
    with pytest.raises(EmosaError):
        reply(clock=lambda: 1)
    with pytest.raises(EmosaError):
        reply(received_at=1, clock=lambda: 0)
    report = reply(clock=lambda: 0.99)
    sent = []
    with pytest.raises(EmosaError):
        report.send(sent.append, lambda: STAMP, clock=lambda: 1)
    assert sent == []


def test_stale_or_changed_source_is_rechecked_before_any_fragment_is_sent():
    report = reply()
    sent = []
    for changed, now in ((None, 0), (ReportStamp("different", 0, 2), 0), (STAMP, 2)):
        with pytest.raises(EmosaError):
            report.send(sent.append, lambda changed=changed: changed, clock=lambda now=now: now)
    assert sent == []


def test_slow_or_partially_sent_response_cannot_be_reported_as_on_time():
    report = reply()
    now = [0.0]
    sent = []

    def send(frame):
        sent.append(frame)
        now[0] = 1.0

    with pytest.raises(EmosaError):
        report.send(send, lambda: STAMP, clock=lambda: now[0])
    assert len(sent) == 1


def test_slow_source_revalidation_cannot_send_after_deadline():
    report = reply()
    now, sent = [0.0], []

    def current():
        now[0] = 1.0
        return STAMP

    with pytest.raises(EmosaError):
        report.send(sent.append, current, clock=lambda: now[0])
    assert sent == []


def test_fragment_delivery_stops_if_inventory_changes_after_first_fragment():
    _, _, facts = fixtures()
    clients = tuple(
        AssociatedClient(bytes.fromhex("020001") + i.to_bytes(3, "big"), i) for i in range(180)
    )
    facts = replace(facts, clients=AssociatedClients((BssClients(BSSID, clients),)))
    report = reply(facts)
    assert len(report.frames) > 1
    sent = []
    current = [STAMP]

    def send(frame):
        sent.append(frame)
        current[0] = ReportStamp("new-revision", 0, 2)

    with pytest.raises(EmosaError):
        report.send(send, lambda: current[0], clock=lambda: 0)
    assert len(sent) == 1


def test_offline_learning_command_and_native_diagnostic_boundaries(tmp_path):
    target = tmp_path / "reports"
    main(["--output", str(target)])
    result = json.loads((target / "result.json").read_text())
    assert result["passed"] and not result["socket_io"] and result["operations_created"] == 0
    report = inspect_capture(target / "synthetic-reports.pcap")
    assert len(report["messages"]) == 3 and not report["rejected"]
    for msg in report["messages"]:
        if "report_review" in msg:
            assert not msg["report_review"]["missing_required_tlvs"]
            assert not msg["report_review"]["invalid_or_unsupported_value_tlvs"]
    native = inspect_capture(Path("doc/evidence/peer-baseline/samples/wired/ethernet.pcap"))
    early = next(m for m in native["messages"] if m["message_type"] == "0x8043")["report_review"]
    topo = next(m for m in native["messages"] if m["message_type"] == "0x0003")["report_review"]
    assert "0xed" in early["missing_required_tlvs"]
    assert "0x03" in topo["invalid_or_unsupported_value_tlvs"]
    assert "0x04" in topo["missing_required_tlvs"]
    assert native["operations_created"] == 0 and not native["onboarding_proven"]
