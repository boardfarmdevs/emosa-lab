# SPDX-License-Identifier: Apache-2.0
"""Spec 9: a pod's second radio is configured by its own M2, in its own scope.

One M2 configures one radio (EasyMesh 6.1 §7.1). The WSC bridge of a further radio's exchange
fits that radio's whole M2 set to the radio's slots (no primary fronthaul: the radio serves none)
and journals a RadioIntent in the radio's own scope.
"""

import asyncio
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from emosa.easymesh_payloads import (
    APOperationalBss,
    APRadioAdvancedCapabilities,
    APRadioBasicCapabilities,
    BasicOperatingClass,
    BssConfigurationReport,
    ConfiguredRadio,
    OperationalRadio,
    encode_value,
)
from emosa.errors import EmosaError
from emosa.model import RadioIntent, State
from emosa.reconcile import Engine
from emosa.secrets import SecretStore
from emosa.store import Store
from emosa.wire.autoconfiguration import ExchangeResult, WscExchange
from emosa.wire.channel import CLASS_CHANNELS
from emosa.wire.cmdu import MidSequence, Tlv, decode_frame
from emosa.wire.operation_bridge import ComponentTarget, ScopeContext, WscComponentBridge
from emosa.wsc_radio import ExistingBssCandidate
from emosa_lab.backends.mock import ModelBackend
from test_autoconfiguration import BINDING, PROFILE2, RUID, assemble, m2, message
from test_autoconfiguration import fixed_entropy as fixed_entropy
from test_native_onboarding import lifecycle, receive, response
from test_pod_profile import parent_5g
from test_provisioning_session import frames
from test_wsc_messages import device, raw
from test_wsc_operation_bridge import rig as rig

pytestmark = pytest.mark.unit

CONTROLLER, AGENT = bytes.fromhex("020000000001"), bytes.fromhex("020000000002")
RUID_5G = bytes.fromhex("020000001b00")  # the 5 GHz radio of test_pod_profile's fixture
M2_SET = SimpleNamespace(mid=42, tlvs=(Tlv(0x11, b"an authenticated M2"),))


def bss(ssid, key):
    return ExistingBssCandidate(ssid, key, None)


# RDK's M2 set for a 5 GHz radio: the first fronthaul BSS, then the others in received order
PRIVATE_5G = bss("private_5g", "PrivateKey-2026")
RDK_5G = (
    ("fronthaul", bss("iot_5g", "IotKey-2026")),
    ("backhaul", bss("mesh_backhaul", "BackhaulKey-2026")),
    ("fronthaul", bss("lnf_radius", "LnfKey-2026")),
)


class StubExchange:
    """One radio's authenticated M2 set, without the WSC crypto (test_autoconfiguration has it):
    what the bridge makes of the set is the point here."""

    def __init__(self, additional=RDK_5G, *, multi_bss=True):
        self.binding = SimpleNamespace(
            controller_al=CONTROLLER,
            local_al=AGENT,
            ingress="emlan",
            generation=1,
            check=lambda *args, **kwargs: None,
        )
        self.basic = SimpleNamespace(ruid=RUID_5G, max_bss=1)
        self.multi_bss = multi_bss
        self.exchange_id = uuid.uuid4().hex
        self.m1_sha256 = "0" * 64
        self.result = ExchangeResult(False, RUID_5G, PRIVATE_5G, additional)
        self.closed = False

    def _live(self):
        pass

    def request(self):
        return ()

    def receive(self, message, **_):
        return self.result

    def close(self):
        self.closed = True


def radio_scope(tmp_path, **stub):
    """The 5 GHz radio's scope of a GRE parent (b-ap-50), its engine, and its exchange."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    _, radio, vault = parent_5g(tmp_path)
    engine = Engine(Store(tmp_path / "radio-5G"), vault, {"pod-1": radio}, intent_type=RadioIntent)
    return engine, radio, StubExchange(**stub)


TARGET = ComponentTarget("pod-1", "radio-5G", "radio-5G", RUID_5G, RUID_5G)


def handed_off(engine, radio, exchange):
    bridge = WscComponentBridge(engine, exchange, TARGET, radio.context, run_id="r")

    async def scenario():
        await bridge.start(lambda frame: None)
        return await bridge.receive(M2_SET, ingress="emlan", generation=1)

    return asyncio.run(scenario())


def test_a_radio_scope_takes_its_slots_bsses_from_its_radios_set(tmp_path):
    """The 5 GHz set's backhaul BSS fills b-ap-50; its fronthaul BSSes, the first included, have
    no slot on that radio and are left out, their keys not kept (spec 3.4)."""
    engine, radio, exchange = radio_scope(tmp_path)
    op = handed_off(engine, radio, exchange)
    ref = f"wsc-{exchange.exchange_id}"
    assert op.intent == {
        "pod_id": "pod-1",
        "radio_id": "radio-5G",
        "additional": [{"role": "backhaul", "ssid": "mesh_backhaul", "secret_ref": ref + "-1"}],
    }
    assert engine.vault.resolve(ref + "-1") == "BackhaulKey-2026"
    with pytest.raises(EmosaError):  # no primary BSS: the private key is not kept
        engine.vault.resolve(ref)
    receipt = engine.store.wsc_receipt(op.operation_id)
    assert (receipt["radio_id"], receipt["bss_id"], receipt["ruid"]) == (
        "radio-5G",
        "radio-5G",
        RUID_5G.hex(),
    )
    result = asyncio.run(engine.execute(op.operation_id))
    assert result.state == State.CONFIG_COMMITTED  # b-ap-50 on the 5 GHz radio
    assert result.commit_evidence["action"] == "slots"


def test_a_radio_set_without_a_backhaul_bss_maps_none_of_its_slots(tmp_path):
    """Withdrawal by band (spec 8.6): the controller leaves the backhaul BSS out of this band, so
    the radio's intent maps nothing and its scope removes b-ap-50."""
    fronthauls = tuple(pair for pair in RDK_5G if pair[0] == "fronthaul")
    engine, radio, exchange = radio_scope(tmp_path, additional=fronthauls)
    op = handed_off(engine, radio, exchange)
    assert op.intent["additional"] == []


RUID_B = bytes.fromhex("020000001b00")
BASIC_B = APRadioBasicCapabilities(RUID_B, 1, (BasicOperatingClass(115, 20, ()),))
ADVANCED_B = APRadioAdvancedCapabilities(RUID_B, 0)


def two_radios(rig, tmp_path):
    """test_native_onboarding's lifecycle with a second radio B, whose exchange goes into an
    engine of its own (its radio's scope)."""
    bridge, _, _, clock = rig
    vault = SecretStore(tmp_path / "secrets-b")
    backend_b = ModelBackend("pod-1", vault, clock)
    engine_b = Engine(Store(tmp_path / "state-b"), vault, {"pod-1": backend_b}, clock)
    exchange_b = WscExchange(
        BINDING, device(), BASIC_B, PROFILE2, ADVANCED_B, mids=MidSequence(1), clock=clock.monotonic
    )
    exchange_b.capabilities = (Tlv(0x85, encode_value(BASIC_B)), *exchange_b.capabilities[1:])

    async def context():
        return ScopeContext(backend_b.generation, "synthetic-model-v1", "bound-fixture")

    made = []

    def radio_bridge(snapshot, mids, index):
        assert index == 1 and snapshot.capabilities.radios[1].basic == BASIC_B
        target = ComponentTarget("pod-1", "radio-1", "bss-1", RUID_B, bytes.fromhex("020000001b01"))
        made.append(WscComponentBridge(engine_b, exchange_b, target, context, run_id="wsc-b"))
        return made[-1]

    session, source, sent = lifecycle(rig, radio_bridge_factory=radio_bridge)
    current = source.current()
    radio_b = replace(current.capabilities.radios[0], basic=BASIC_B, advanced=ADVANCED_B)
    caps = replace(current.capabilities, radios=(*current.capabilities.radios, radio_b))
    # radio B carries no BSS yet: an Operational and a Configured radio with none
    topology = current.topology
    topology = replace(
        topology,
        operational=APOperationalBss((*topology.operational.radios, OperationalRadio(RUID_B, ()))),
        configuration=BssConfigurationReport(
            (*topology.configuration.radios, ConfiguredRadio(RUID_B, ()))
        ),
    )
    source.publish((1, 2), caps, topology, observed_at=clock.monotonic())
    return session, sent, engine_b, made


def m2_b(value=None, **kwargs):
    return message((Tlv(0x82, RUID_B), Tlv(0x11, raw("m2") if value is None else value)), **kwargs)


def m1_ruids(sent):
    """The radio of each M1 sent (its AP Radio Basic Capabilities), in order."""
    m1s = {}
    for frame in sent:
        fragment = decode_frame(frame)
        if fragment.message_type == 9:
            m1s.setdefault(fragment.mid, []).append(frame)
    return [next(t.value[:6] for t in assemble(f).tlvs if t.kind == 0x85) for f in m1s.values()]


def test_each_radio_sends_its_own_m1_after_the_one_before_it_is_provisioned(rig, tmp_path):
    """Spec 9 / EasyMesh 6.1 §7.1: one M1 per radio. Radio B's M1 goes only once the
    fronthaul's M2 is taken, and radio B's M2 goes to radio B's exchange (its scope), by the
    AP Radio Identifier it names; the fronthaul's M2s still go to the fronthaul's."""

    async def scenario():
        _, engine, backend, _ = rig
        session, sent, engine_b, made = two_radios(rig, tmp_path)
        await session.tick()
        assert await receive(session, response()) == "early_then_m1_sent"
        await session.tick()
        assert not made and session.awaiting_m2  # B waits for the fronthaul's M2
        assert m1_ruids(sent) == [RUID]
        assert await receive(session, frames()[0]) == "wsc_operation"
        assert session.state == "provisioning" and not session.awaiting_m2
        await session.tick()
        assert made and m1_ruids(sent) == [RUID, RUID_B] and session.awaiting_m2
        assert session.status()["radios"] == [
            {"ruid": RUID_B.hex(":"), "awaiting_m2": True, "counts": {}, "closed": False}
        ]
        await session.tick()  # one M1 per radio
        assert m1_ruids(sent) == [RUID, RUID_B]
        # a fragmented M2 for B: its later fragment follows the first to B's exchange
        first, second = frames(m2_b(), fragmented=True)
        assert await receive(session, first) == "radio_wsc_incomplete"
        assert await receive(session, second) == "radio_wsc_operation"
        assert not session.awaiting_m2 and session.state == "provisioning"
        assert len(engine_b.store.operations()) == 1
        assert (
            engine_b.store.wsc_receipt(engine_b.store.operations()[0].operation_id)["ruid"]
            == RUID_B.hex()
        )
        # the fronthaul's M2 again (a retry) is still the fronthaul's
        assert await receive(session, frames(m2(mid=987))[0]) == "wsc_operation"
        assert len(engine.store.operations()) == backend.writes == 1
        assert len(engine_b.store.operations()) == 1
        session.close()
        assert session.status()["radios"][0]["closed"] is True

    asyncio.run(scenario())


def test_a_refused_m2_for_a_further_radio_ends_its_wait(rig, tmp_path):
    """An M2 for radio B that cannot be applied (here: no M1 transcript it authenticates against,
    INVALID_INPUT) leaves B as it is and stops the wait: a fresh M1 would get the same M2."""

    async def scenario():
        session, sent, engine_b, _ = two_radios(rig, tmp_path)
        await session.tick()
        await receive(session, response())
        await receive(session, frames()[0])
        await session.tick()
        assert session.awaiting_m2
        assert await receive(session, frames(m2_b(value=raw("m2")[:-8]))[0]) == "radio_wsc_rejected"
        assert not session.awaiting_m2 and not engine_b.store.operations()
        session.close()

    asyncio.run(scenario())


def test_a_session_without_a_radio_factory_takes_one_radio_only(rig, tmp_path):
    session, _, _, _ = two_radios(rig, tmp_path)
    session.radio_bridge_factory = None
    with pytest.raises(EmosaError):
        asyncio.run(session.tick())


def test_a_radio_scope_takes_only_its_radios_m2_set(tmp_path):
    """A radio scope needs a multi-BSS exchange (its set fitted to its slots) and a target
    naming its own radio as the resource."""
    engine, radio, exchange = radio_scope(tmp_path / "single", multi_bss=False)
    with pytest.raises(EmosaError):
        WscComponentBridge(engine, exchange, TARGET, radio.context, run_id="r")
    engine, radio, exchange = radio_scope(tmp_path / "other")
    for wrong in (
        ComponentTarget("pod-1", "radio-1", "bss-1", RUID_5G, RUID_5G),
        ComponentTarget("pod-1", "radio-5G", "bss-1", RUID_5G, RUID_5G),
    ):
        with pytest.raises(EmosaError):
            WscComponentBridge(engine, exchange, wrong, radio.context, run_id="r")


def test_a_second_radios_channel_change_retakes_its_capabilities_once(tmp_path):
    """Spec 9's channel rule (V54: radio B on 149): the pod's station roams and the second radio
    moves with it, from 36 to 149 by way of no channel. The report source takes the radio's new
    capabilities under the same pod source (holding 36 in between, so the move changes them
    once); a moved fronthaul radio is still a different device, refused until the source
    changes."""
    from emosa.agent.pod import PodReportSource, further_radios_changed
    from test_pod_profile import RADIO_STATE, RADIO_STATE_5G

    fronthaul, _, _ = parent_5g(tmp_path)
    tables = fronthaul.session.tables
    tables["Wifi_Radio_State"][RADIO_STATE_5G]["tx_power"] = 20
    report = PodReportSource(fronthaul, BINDING, "pod-1")

    def radio_b():
        """Radio B's (class, channel) in the Operating Channel Report, and the classes its
        capabilities make it operable in."""
        snapshot = report.source.current()
        basic = snapshot.capabilities.radios[1].basic
        (operating,) = [
            (r.operating_class, r.channel)
            for r in snapshot.operating_radios
            if r.ruid == basic.ruid
        ]
        operable = {
            c.operating_class
            for c in basic.operating_classes
            if set(CLASS_CHANNELS[c.operating_class]) - set(c.non_operable_channels)
        }
        return operating, operable

    assert asyncio.run(report.refresh())
    assert radio_b() == ((115, 36), {115})
    first = report.capabilities
    tables["Wifi_Radio_State"][RADIO_STATE_5G]["channel"] = ["set", []]  # between two channels
    assert asyncio.run(report.refresh())
    assert report.capabilities == first and radio_b() == ((115, 36), {115})
    tables["Wifi_Radio_State"][RADIO_STATE_5G]["channel"] = 149
    assert asyncio.run(report.refresh())
    assert further_radios_changed(first, report.capabilities)
    assert radio_b() == ((125, 149), {124, 125})
    tables["Wifi_Radio_State"][RADIO_STATE]["channel"] = 11
    assert not asyncio.run(report.refresh()) and report.source.current() is None
