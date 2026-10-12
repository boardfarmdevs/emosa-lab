# SPDX-License-Identifier: Apache-2.0
"""Spec 9: a pod's second radio is configured by its own M2, in its own scope.

One M2 configures one radio (EasyMesh 6.1 §7.1). The WSC bridge of a further radio's exchange
fits that radio's whole M2 set to the radio's slots (no primary fronthaul: the radio serves none)
and journals a RadioIntent in the radio's own scope.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from emosa.errors import EmosaError
from emosa.model import RadioIntent, State
from emosa.reconcile import Engine
from emosa.store import Store
from emosa.wire.autoconfiguration import ExchangeResult
from emosa.wire.cmdu import Tlv
from emosa.wire.operation_bridge import ComponentTarget, WscComponentBridge
from emosa.wsc_radio import ExistingBssCandidate
from test_pod_profile import parent_5g

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
