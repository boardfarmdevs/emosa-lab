"""Complete Ethernet inputs plus independent hostap WSC payloads; no pod writes."""

from dataclasses import replace
from pathlib import Path

import pytest

from emosa import wsc_messages
from emosa.easymesh_payloads import (
    APRadioAdvancedCapabilities,
    APRadioBasicCapabilities,
    BasicOperatingClass,
    Profile2APCapability,
)
from emosa.errors import EmosaError, Reason
from emosa.wire.autoconfiguration import (
    CONFIGURATION_COMPANIONS,
    DiscoveryExchange,
    PeerBinding,
    WscExchange,
    parse_response,
    parse_search,
)
from emosa.wire.cmdu import MULTICAST, MidSequence, Reassembler, Tlv, fragment_message
from emosa.wsc import KeyPair
from emosa_lab.wire.inspection import describe, inspect_capture, packets
from test_wsc_messages import device, pair, raw, second_m2

pytestmark = pytest.mark.unit
LOCAL = device().al_mac
PEER = bytes.fromhex("020000000002")
INTERFACE = bytes.fromhex("020000000003")
RUID = bytes.fromhex("020000001001")
BINDING = PeerBinding("fixture", 4, LOCAL, PEER, (PEER, INTERFACE))
BASIC = APRadioBasicCapabilities(RUID, 2, (BasicOperatingClass(81, 20, ()),))
PROFILE2 = Profile2APCapability(0, 0, 0, 0)
ADVANCED = APRadioAdvancedCapabilities(RUID, 0)
RESPONSE = (Tlv(0x0F, b"\0"), Tlv(0x10, b"\0"), Tlv(0x80, b"\x01\0"), Tlv(0xB3, b"\x01"))


def message(tlvs, *, kind=9, mid=321, source=PEER, destination=LOCAL, relay=False):
    reassembly = Reassembler()
    result = None
    for frame in fragment_message(destination, source, kind, mid, tlvs, relay=relay):
        result = reassembly.feed(frame)
    return result


def assemble(frames):
    parser = Reassembler()
    result = None
    for frame in frames:
        result = parser.feed(frame)
    return result


def receive(exchange, msg, **kwargs):
    return exchange.receive(msg, **({"ingress": "fixture", "generation": 4} | kwargs))


def m2(value=None, **kwargs):
    return message((Tlv(0x82, RUID), Tlv(0x11, raw("m2") if value is None else value)), **kwargs)


@pytest.fixture
def fixed_entropy(monkeypatch):
    # Only tests install the public independent harness's deterministic material.
    monkeypatch.setattr(KeyPair, "generate", lambda: pair())
    monkeypatch.setattr(wsc_messages.secrets, "token_bytes", lambda n: bytes(range(n)))


def exchange(**kwargs):
    return WscExchange(
        BINDING, device(), BASIC, PROFILE2, ADVANCED, mids=MidSequence(65534), **kwargs
    )


def search(**kwargs):
    return DiscoveryExchange(
        BINDING, band=0, profile=1, profile2=PROFILE2, mids=MidSequence(65534), **kwargs
    )


def test_search_literal_vector_and_echoed_mid():
    session = search()
    frame = session.request()[0]
    assert frame.hex() == (
        "0180c2000013020000000001893a00000007ffff00c0"
        "0100060200000000010d0001000e00010080000201018100020100"
        "b3000101b4000400000000000000"
    )
    parsed = parse_search(assemble((frame,)))
    assert (parsed.al_mac, parsed.band, parsed.profile) == (LOCAL, 0, 1)
    assert assemble(session.request()).mid == 0
    result = receive(session, message(RESPONSE, kind=8, mid=65535, source=INTERFACE))
    assert "controller_capability_absent" in result.pending_requirements
    assert "security_capability_absent" in result.pending_requirements
    assert session.state == "received"
    with pytest.raises(EmosaError):
        session.request()


@pytest.mark.parametrize(
    "kind,value", [(0x0F, b"\1"), (0x10, b"\1"), (0x80, b"\1\1"), (0xB3, b"\2")]
)
def test_search_response_role_band_service_profile_must_match(kind, value):
    session = search()
    session.request()
    bad = tuple(Tlv(t.kind, value) if t.kind == kind else t for t in RESPONSE)
    with pytest.raises(EmosaError):
        receive(session, message(bad, kind=8, mid=65535))
    assert session.state == "waiting"


def test_search_ignores_unrelated_tlv_and_reserved_service_profile_values():
    session = search()
    session.request()
    values = tuple(
        Tlv(0xB3, b"\xff") if t.kind == 0xB3 else Tlv(0x80, b"\2\0\xa1") if t.kind == 0x80 else t
        for t in RESPONSE
    ) + (Tlv(0xEF, b"unrelated"), Tlv(0xDD, b"\xc0\xff"), Tlv(0xA9, b"\0\0\0"))
    result = receive(session, message(values, kind=8, mid=65535))
    assert result.profile == 255  # receiver-profile rule, never invented peer qualification
    assert result.controller_flags == b"\xc0\xff"
    assert "early_ap_capability_procedure_and_table117_review" in result.pending_requirements
    assert "profile_qualification" in result.pending_requirements


@pytest.mark.parametrize("missing", [0x01, 0x0D, 0x0E, 0x80, 0x81, 0xB3, 0xB4])
def test_search_required_fields_and_profile1_feature_tlv(missing):
    msg = assemble(search().request())
    with pytest.raises(EmosaError):
        parse_search(replace(msg, tlvs=tuple(t for t in msg.tlvs if t.kind != missing)))


@pytest.mark.parametrize("kind", [0x0F, 0x10, 0x80, 0xB3])
def test_response_missing_and_duplicate_required_fields(kind):
    for values in (
        tuple(t for t in RESPONSE if t.kind != kind),
        RESPONSE + tuple(t for t in RESPONSE if t.kind == kind),
    ):
        with pytest.raises(EmosaError):
            parse_response(message(values, kind=8))


def test_search_unsolicited_wrong_mid_and_deadline_are_rejected():
    now = [0.0]
    session = search(clock=lambda: now[0])
    with pytest.raises(EmosaError):
        receive(session, message(RESPONSE, kind=8, mid=65535))
    session.request()
    with pytest.raises(EmosaError):
        receive(session, message(RESPONSE, kind=8, mid=7))
    now[0] = 5.0
    with pytest.raises(EmosaError):
        receive(session, message(RESPONSE, kind=8, mid=65535))


def test_discovery_repeated_response_cannot_change_bound_advertisement():
    session = search()
    session.request()
    msg = message(RESPONSE, kind=8, mid=65535)
    first = receive(session, msg)
    assert receive(session, msg) == first
    with pytest.raises(EmosaError):
        receive(session, replace(msg, tlvs=msg.tlvs + (Tlv(0xDD, b"\xc0"),)))
    assert receive(session, msg) == first


def test_native_discovery_fields_are_independent_inputs_with_unqualified_profiles():
    parser = Reassembler()
    capture = Path("doc/evidence/peer-baseline/samples/wired/ethernet.pcap")
    decoded = []
    for _number, _timestamp, frame, truncated in packets(capture):
        assert not truncated
        msg = parser.feed(frame)
        if msg and msg.message_type in (7, 8):
            decoded.append(msg)
    agent = parse_search(decoded[0])
    controller = parse_response(decoded[1])
    assert agent.profile == 2 and controller.profile == 1
    assert decoded[0].mid == decoded[1].mid
    # Independently checked with tshark -x at frame 2, octets 46..49: dd000140.
    assert controller.controller_flags == b"\x40"
    assert "security_capability_absent" in controller.pending_requirements
    assert "kib_mib_support_absent" in controller.pending_requirements
    assert "early_ap_capability_procedure_and_table117_review" in controller.pending_requirements
    result = describe(decoded[1])
    assert result["operation_created"] is False
    assert result["procedure_validation"] == "selected_response_fields_only"
    report = inspect_capture(capture)
    assert not report["rejected"]
    pair = report["autoconfiguration_pairs"][0]
    assert (pair["search_frame"], pair["response_frame"]) == (1, 2)
    assert pair["band_matches"] and pair["profile_matches"] is False
    assert pair["onboarding_proven"] is False


def test_m1_contains_all_selected_companions_and_native_transcript(fixed_entropy):
    session = exchange()
    first, retry = assemble(session.request()), assemble(session.request())
    assert first.mid == 65535 and retry.mid == 0
    assert first.tlvs == retry.tlvs and not first.relay
    assert first.destination == PEER and first.source == LOCAL
    assert [t.kind for t in first.tlvs] == [0x85, 0x11, 0xB4, 0xBE]
    assert first.tlvs[1].value == raw("m1")
    result = receive(session, m2(mid=42))  # WSC does not use discovery's echo rule.
    assert result.ruid == RUID and not result.duplicate
    assert result.candidate.passphrase == "public-vector-passphrase"
    assert "public-vector" not in repr(result)
    with pytest.raises(EmosaError):
        session.request()


def test_duplicate_m2_new_mid_and_unrelated_tlv_produce_one_candidate(fixed_entropy):
    session = exchange()
    session.request()
    first = receive(session, m2())
    for mid in (321, 65535, 0):
        msg = m2(mid=mid, source=INTERFACE)
        msg = replace(msg, tlvs=msg.tlvs + (Tlv(0xEF, b"ignored-not-configuration"),))
        duplicate = receive(session, msg)
        assert duplicate.duplicate and duplicate.candidate is first.candidate
    with pytest.raises(EmosaError):
        receive(session, m2(second_m2()))
    assert receive(session, m2()).duplicate


def test_duplicate_cannot_hide_extra_radio_configuration(fixed_entropy):
    session = exchange()
    session.request()
    receive(session, m2())
    msg = m2()
    with pytest.raises(EmosaError):
        receive(session, replace(msg, tlvs=msg.tlvs + (Tlv(0xB6, b""),)))
    assert receive(session, m2()).duplicate


def test_authenticated_reencrypted_same_configuration_is_still_duplicate(fixed_entropy):
    session = exchange()
    session.request()
    first = receive(session, m2())
    rebuilt = second_m2(index=1)  # Fresh registrar nonce/IV; unchanged complete ConfigData.
    assert rebuilt != raw("m2")
    result = receive(session, m2(rebuilt, mid=999))
    assert result.duplicate and result.candidate is first.candidate
    assert receive(session, m2()).duplicate


def test_invalid_reencrypted_retry_cannot_replace_accepted_candidate(fixed_entropy):
    session = exchange()
    session.request()
    first = receive(session, m2())
    bad = second_m2(index=1)
    bad = bad[:-1] + bytes([bad[-1] ^ 1])
    with pytest.raises(EmosaError):
        receive(session, m2(bad, mid=999))
    assert session.state == "received"
    assert receive(session, m2()).candidate is first.candidate


@pytest.mark.parametrize("kind", [0xAB, 0xAC])
def test_dpp_security_envelope_requires_its_own_processing(fixed_entropy, kind):
    session = exchange()
    session.request()
    msg = m2()
    with pytest.raises(EmosaError) as error:
        receive(session, replace(msg, tlvs=msg.tlvs + (Tlv(kind, b""),)))
    assert error.value.code == Reason.UNSUPPORTED_OPERATION
    assert session._candidate is None


def test_fragmented_complete_request_is_not_dispatched_early(fixed_entropy):
    session = exchange()
    session.request()
    frames = fragment_message(
        LOCAL, PEER, 9, 321, (Tlv(0x82, RUID), Tlv(0xEF, bytes(1400)), Tlv(0x11, raw("m2")))
    )
    assert len(frames) == 2
    parser = Reassembler()
    assert parser.feed(frames[1]) is None
    assert session.state == "waiting" and session._candidate is None
    assert not receive(session, parser.feed(frames[0])).duplicate


@pytest.mark.parametrize(
    "mutation", ["ingress", "generation", "source", "destination", "ruid", "relay", "type"]
)
def test_wrong_peer_link_radio_or_envelope_cannot_consume_exchange(fixed_entropy, mutation):
    session = exchange()
    session.request()
    msg = m2()
    kwargs = {}
    if mutation in ("ingress", "generation"):
        kwargs[mutation] = "other" if mutation == "ingress" else 5
    elif mutation == "ruid":
        msg = replace(msg, tlvs=(Tlv(0x82, PEER), *msg.tlvs[1:]))
    else:
        field = "message_type" if mutation == "type" else mutation
        value = {"source": LOCAL, "destination": MULTICAST, "relay": True, "type": 8}[mutation]
        msg = replace(msg, **{field: value})
    with pytest.raises(EmosaError):
        receive(session, msg, **kwargs)
    assert session.state == "waiting"
    assert not receive(session, m2()).duplicate


@pytest.mark.parametrize("kind", sorted(CONFIGURATION_COMPANIONS))
def test_companion_configuration_is_never_silently_dropped(fixed_entropy, kind):
    session = exchange()
    session.request()
    msg = m2()
    with pytest.raises(EmosaError) as error:
        receive(session, replace(msg, tlvs=msg.tlvs + (Tlv(kind, b""),)))
    assert error.value.code == Reason.UNSUPPORTED_OPERATION
    assert session.state == "closed" and session._candidate is None


def test_an_ap_mld_configuration_with_zero_mlds_configures_nothing(fixed_entropy):
    session = exchange()
    session.request()
    msg = m2()
    result = receive(session, replace(msg, tlvs=msg.tlvs + (Tlv(0xE0, b"\0"),)))
    assert result.candidate.ssid and not result.duplicate
    other = exchange()
    other.request()
    with pytest.raises(EmosaError) as error:
        receive(other, replace(msg, tlvs=msg.tlvs + (Tlv(0xE0, b"\1" + bytes(20)),)))
    assert error.value.code == Reason.UNSUPPORTED_OPERATION


@pytest.mark.parametrize("case", ["auth", "teardown", "multiple", "m8", "empty"])
def test_failed_complete_request_cannot_be_retried_with_old_transcript(fixed_entropy, case):
    session = exchange()
    session.request()
    values = {
        "auth": (raw("m2")[:-1] + bytes([raw("m2")[-1] ^ 1]),),
        "teardown": (raw("teardown_m2"),),
        "multiple": (raw("m2"), second_m2()),
        "m8": (raw("m2"), bytes.fromhex("102200010c")),
        "empty": (),
    }[case]
    msg = message((Tlv(0x82, RUID), *(Tlv(0x11, value) for value in values)))
    with pytest.raises(EmosaError):
        receive(session, msg)
    assert session.state == "closed" and session._transcript is None
    with pytest.raises(EmosaError):
        receive(session, m2())


def test_fresh_exchange_rejects_old_m2_and_uses_fresh_entropy():
    session = exchange()
    first = assemble(session.request())
    other = assemble(exchange().request())
    assert first.tlvs[1].value != other.tlvs[1].value
    with pytest.raises(EmosaError):
        receive(session, m2())
    assert session.state == "closed"


def test_retries_duplicates_and_deadline_never_extend_lifetime(fixed_entropy):
    now = [0.0]
    session = exchange(clock=lambda: now[0], max_transmissions=2)
    session.request()
    now[0] = 4.0
    session.request()
    with pytest.raises(EmosaError):
        session.request()
    receive(session, m2())
    assert session.deadline == 5.0
    now[0] = 4.99
    assert receive(session, m2()).duplicate
    now[0] = 5.0
    with pytest.raises(EmosaError):
        receive(session, m2())
    assert session._candidate is None and session._transcript is None


def test_no_m2_before_m1_and_explicit_close_invalidates_material(fixed_entropy):
    session = exchange()
    with pytest.raises(EmosaError):
        receive(session, m2())
    session.request()
    session.close()
    with pytest.raises(EmosaError):
        receive(session, m2())


@pytest.mark.parametrize("budget", [0, -1, 61, float("nan"), float("inf"), True])
def test_invalid_time_budgets(budget):
    with pytest.raises(EmosaError):
        exchange(timeout=budget)


def test_explicit_identity_binding_rejects_alias_and_mismatched_m1():
    for changes in ({"local_al": PEER}, {"source_macs": (LOCAL,)}, {"generation": -1}):
        with pytest.raises(EmosaError):
            replace(BINDING, **changes)
    with pytest.raises(EmosaError):
        WscExchange(
            BINDING, replace(device(), al_mac=PEER), BASIC, PROFILE2, ADVANCED, mids=MidSequence(0)
        )
    with pytest.raises(EmosaError):
        WscExchange(
            BINDING, device(), BASIC, PROFILE2, replace(ADVANCED, ruid=PEER), mids=MidSequence(0)
        )
