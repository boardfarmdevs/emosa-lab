"""EasyMesh 6.1 (default) versus R1 message sets for Profile-1 onboarding."""

import pytest

from emosa.errors import EmosaError
from emosa.wire.autoconfiguration import (
    EASYMESH_61,
    R1,
    ControllerAdvertisement,
    check_message_set,
    parse_response,
)
from emosa.wire.cmdu import Tlv
from emosa.wire.onboarding import non_dpp_admission
from test_autoconfiguration import (
    RESPONSE,
    assemble,
    exchange,
    fixed_entropy,  # noqa: F401 (fixture)
    message,
    receive,
    search,
)

pytestmark = pytest.mark.unit
R1_RESPONSE = tuple(t for t in RESPONSE if t.kind != 0xB3)


def kinds(frames):
    return [t.kind for t in assemble(frames).tlvs]


def test_default_search_is_easymesh_61_and_r1_search_has_no_profile_tlvs():
    assert kinds(search().request()) == [0x01, 0x0D, 0x0E, 0x80, 0x81, 0xB3, 0xB4]
    assert kinds(search(message_set=R1).request()) == [0x01, 0x0D, 0x0E, 0x80, 0x81]


def test_r1_response_without_profile_tlv_is_profile_1_only_in_r1():
    with pytest.raises(EmosaError):
        parse_response(message(R1_RESPONSE, kind=8))
    assert parse_response(message(R1_RESPONSE, kind=8), message_set=R1).profile == 1
    session = search(message_set=R1)
    sent = assemble(session.request())
    result = receive(session, message(R1_RESPONSE, kind=8, mid=sent.mid, destination=sent.source))
    assert result.profile == 1 and session.state == "received"


def test_r1_response_with_a_profile_tlv_is_still_checked():
    session = search(message_set=R1)
    sent = assemble(session.request())
    assert receive(session, message(RESPONSE, kind=8, mid=sent.mid)).profile == 1


def test_r1_m1_carries_only_basic_capabilities(fixed_entropy):  # noqa: F811
    assert kinds(exchange().request()) == [0x85, 0x11, 0xB4, 0xBE]
    assert kinds(exchange(message_set=R1).request()) == [0x85, 0x11]


def test_r1_admission_ignores_only_the_r3_controller_fields():
    bare = ControllerAdvertisement(0, 1, None, False)
    assert "controller_capability_absent" in non_dpp_admission(bare)
    assert non_dpp_admission(bare, message_set=R1) == ()
    wrong_band = ControllerAdvertisement(1, 1, None, False)
    assert non_dpp_admission(wrong_band, message_set=R1) == ("outside_profile1_24ghz_contract",)


def test_unknown_message_set_is_refused():
    assert check_message_set(EASYMESH_61) == EASYMESH_61
    with pytest.raises(EmosaError):
        check_message_set("easymesh-5.0")
    with pytest.raises(EmosaError):
        search(message_set="r2")


def test_r1_ignores_the_controllers_profile_value_but_61_requires_the_echo():
    higher = tuple(Tlv(0xB3, b"\3") if t.kind == 0xB3 else t for t in RESPONSE)
    strict = search()
    sent = assemble(strict.request())
    with pytest.raises(EmosaError):
        receive(strict, message(higher, kind=8, mid=sent.mid))
    r1 = search(message_set=R1)
    sent = assemble(r1.request())
    result = receive(r1, message(higher, kind=8, mid=sent.mid))
    assert result.profile == 3 and non_dpp_admission(result, message_set=R1) == ()
    assert "outside_profile1_24ghz_contract" in non_dpp_admission(result)


def test_r1_topology_response_has_no_profile_or_bss_configuration_report():
    from emosa.wire.reports import _topology
    from emosa_lab.simulation.wire_reports import fixtures

    binding, _, facts = fixtures()
    assert {0xB3, 0xB7} <= {t.kind for t in _topology(facts, binding)}
    r1 = {t.kind for t in _topology(facts, binding, R1)}
    assert not r1 & {0xB3, 0xB7} and {0x03, 0x83} <= r1
