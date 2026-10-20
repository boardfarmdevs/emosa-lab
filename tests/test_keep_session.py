# SPDX-License-Identifier: Apache-2.0
"""spec 2.5: a provisioned session kept across its pod's reconnection.

The pod's link to its agent drops on every backhaul move and comes back as a new database
generation. Back within the reconnection window, the same pod with the same capabilities apart
from their operable channels keeps its session: a Topology Notification, and a Channel Preference
Report when a radio's channel moved, instead of a new Search and M1. Anything else is a fresh
session, as before.
"""

import asyncio
from dataclasses import replace

import pytest

from emosa.easymesh_payloads import BasicOperatingClass
from emosa.wire.coordinator import ReportSource
from emosa.wire.reports import capabilities_identity, identity_difference
from test_autoconfiguration import BINDING
from test_autoconfiguration import fixed_entropy as fixed_entropy
from test_native_onboarding import lifecycle, receive, response
from test_provisioning_session import frames
from test_wsc_operation_bridge import rig as rig

pytestmark = pytest.mark.unit

WINDOW = 60


def message_types(sent, after=0):
    return [int.from_bytes(f[16:18], "big") for f in sent[after:]]


async def provisioned(rig, **options):
    """A provisioned session on a source whose context is the agent's lineage 0."""
    session, source, sent = lifecycle(rig, reconnect_window=WINDOW, **options)
    clock = rig[3]
    first = source.current()
    source.publish(
        (1, 2), first.capabilities, first.topology, observed_at=clock.monotonic(), lineage=0
    )
    await session.tick()
    await receive(session, response())
    await receive(session, frames()[0])
    assert session.state == "provisioning"
    return session, source, sent, clock


def republish(source, clock, revision, *, lineage=0, capabilities=None, held=frozenset()):
    """The pod's source published again: the facts last published (kept across invalidation);
    ``held`` the radios a station on them holds."""
    last_capabilities, last_topology = source._last_facts[:2]
    return source.publish(
        revision,
        capabilities or last_capabilities,
        last_topology,
        observed_at=clock.monotonic(),
        lineage=lineage,
        held_radios=held,
    )


def moved(capabilities, ruid_index=0, channel=11):
    """The same capabilities with radio ``ruid_index`` operable on ``channel`` instead."""
    radio = capabilities.radios[ruid_index]
    classes = tuple(
        replace(
            c,
            non_operable_channels=tuple(
                sorted(
                    (set(c.non_operable_channels) | set(range(1, 14)) - {channel}) - {channel}
                    if c.operating_class == 81
                    else c.non_operable_channels
                )
            ),
        )
        for c in radio.basic.operating_classes
    )
    radios = list(capabilities.radios)
    radios[ruid_index] = replace(radio, basic=replace(radio.basic, operating_classes=classes))
    return replace(capabilities, radios=tuple(radios))


def test_a_lineage_keeps_the_context_across_invalidation_and_a_new_generation():
    _, caps, topology = __import__(
        "emosa_lab.simulation.wire_reports", fromlist=["fixtures"]
    ).fixtures()
    topology = replace(topology, device=replace(topology.device, al_mac=BINDING.local_al))
    now = [0.0]
    source = ReportSource(BINDING, "pod-1", "b" * 64, clock=lambda: now[0])
    source.publish((1, 1), caps, topology, observed_at=0.0, lineage=3)
    context = source.current().context_token
    assert source.current().generation == 1
    source.invalidate()
    assert source.current() is None
    now[0] = 15.0
    source.publish((2, 1), caps, topology, observed_at=15.0, lineage=3)
    assert source.current().context_token == context and source.current().generation == 2
    source.publish((2, 2), caps, topology, observed_at=15.0, lineage=4)
    assert source.current().context_token != context
    # without a lineage the context is the connection's, as before
    plain = ReportSource(BINDING, "pod-1", "b" * 64, clock=lambda: now[0])
    plain.publish((1, 1), caps, topology, observed_at=15.0)
    before = plain.current().context_token
    plain.publish((2, 1), caps, topology, observed_at=15.0)
    assert plain.current().context_token != before


def test_the_identity_leaves_out_operable_channels_only():
    _, caps, _ = __import__("emosa_lab.simulation.wire_reports", fromlist=["fixtures"]).fixtures()
    assert capabilities_identity(moved(caps)) == capabilities_identity(caps)
    assert moved(caps) != caps
    radio = caps.radios[0]
    more = replace(radio, basic=replace(radio.basic, max_bss=radio.basic.max_bss + 1))
    assert capabilities_identity(replace(caps, radios=(more,))) != capabilities_identity(caps)
    eirp = replace(
        radio,
        basic=replace(
            radio.basic,
            operating_classes=tuple(
                replace(c, max_eirp_dbm=c.max_eirp_dbm + 1) for c in radio.basic.operating_classes
            ),
        ),
    )
    assert capabilities_identity(replace(caps, radios=(eirp,))) != capabilities_identity(caps)
    other = replace(radio, basic=replace(radio.basic, ruid=bytes(5) + b"\x09"))
    assert capabilities_identity(replace(caps, radios=(other,))) != capabilities_identity(caps)
    extra = replace(
        radio,
        basic=replace(
            radio.basic,
            operating_classes=(*radio.basic.operating_classes, BasicOperatingClass(83, 20, ())),
        ),
    )
    assert capabilities_identity(replace(caps, radios=(extra,))) != capabilities_identity(caps)


def test_a_capabilities_change_names_what_differs():
    # the log of a fresh session (capabilities_changed) says which field or TLV changed
    _, caps, _ = __import__("emosa_lab.simulation.wire_reports", fromlist=["fixtures"]).fixtures()
    radio = caps.radios[0]
    ruid = radio.basic.ruid.hex(":")
    assert identity_difference(caps, moved(caps)) == ""  # its operable channels only
    more = replace(radio, basic=replace(radio.basic, max_bss=radio.basic.max_bss + 1))
    assert identity_difference(caps, replace(caps, radios=(more,))) == (
        f"radio {ruid} max BSS {radio.basic.max_bss} -> {radio.basic.max_bss + 1}"
    )
    first = radio.basic.operating_classes[0]
    eirp = replace(
        radio,
        basic=replace(
            radio.basic,
            operating_classes=tuple(
                replace(c, max_eirp_dbm=c.max_eirp_dbm + 3) for c in radio.basic.operating_classes
            ),
        ),
    )
    assert identity_difference(caps, replace(caps, radios=(eirp,))) == (
        f"radio {ruid} class {first.operating_class} max EIRP "
        f"{first.max_eirp_dbm} -> {first.max_eirp_dbm + 3} dBm"
    )
    other = replace(radio, basic=replace(radio.basic, ruid=bytes(5) + b"\x09"))
    assert identity_difference(caps, replace(caps, radios=(other,))) == (
        f"radio {ruid} -> 00:00:00:00:00:09"
    )
    assert identity_difference(caps, replace(caps, radios=(radio, radio))) == "radios 1 -> 2"
    assert identity_difference(caps, replace(caps, radios=(replace(radio, vht=True),))) == (
        f"radio {ruid} VHT capabilities"
    )


def test_a_provisioned_session_is_kept_across_a_reconnection(rig):
    async def scenario():
        session, source, sent, clock = await provisioned(rig)
        searches, count = session.counts["search_sent"], len(sent)
        source.invalidate()  # the pod's link to its agent dropped (a backhaul move)
        clock.advance(15)
        await session.tick()
        assert session.state == "provisioning"
        assert session.counts["reconnection_held"] == 1
        republish(source, clock, (2, 1))  # back: a new database generation, the same pod
        await session.tick()
        assert session.state == "provisioning"
        assert session.counts["reconnection_kept"] == 1
        assert session.counts["search_sent"] == searches
        assert 0x0001 in message_types(sent, count), "a Topology Notification"
        assert 0x0007 not in message_types(sent, count) and 0x0002 not in message_types(sent, count)
        session.close()

    asyncio.run(scenario())


def test_a_hold_past_the_window_loses_the_source(rig):
    async def scenario():
        session, source, _sent, clock = await provisioned(rig)
        source.invalidate()
        await session.tick()
        clock.advance(WINDOW - 1)
        await session.tick()
        assert session.state == "provisioning"
        clock.advance(1)
        await session.tick()
        assert session.state == "source_lost"
        session.close()

    asyncio.run(scenario())


def test_a_new_lineage_is_a_fresh_session(rig):
    async def scenario():
        session, source, _sent, clock = await provisioned(rig)
        source.invalidate()
        clock.advance(10)
        await session.tick()
        republish(source, clock, (2, 1), lineage=1)  # the agent judged it another source
        await session.tick()
        assert session.state == "source_lost"
        session.close()

    asyncio.run(scenario())


def test_a_session_not_yet_provisioned_loses_its_source_at_once(rig):
    async def scenario():
        session, source, _sent = lifecycle(rig, reconnect_window=WINDOW)
        clock = rig[3]
        first = source.current()
        source.publish(
            (1, 2), first.capabilities, first.topology, observed_at=clock.monotonic(), lineage=0
        )
        await session.tick()
        assert session.state == "discovering"
        source.invalidate()
        await session.tick()
        assert session.state == "source_lost"
        session.close()
        # and a new generation of the same lineage before M2 is a new source too
        session, source, _sent = lifecycle(rig, reconnect_window=WINDOW)
        first = source.current()
        source.publish(
            (1, 2), first.capabilities, first.topology, observed_at=clock.monotonic(), lineage=0
        )
        await session.tick()
        await receive(session, response())
        assert session.state == "awaiting_m2"
        republish(source, clock, (2, 1))
        await session.tick()
        assert session.state == "source_lost"
        session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("held", [True, False])
def test_a_moved_channel_is_kept_with_a_channel_preference_report(rig, held):
    """Spec 2.5: a radio held by a station on it is reported with its new channel the only
    operable one (a Channel Preference TLV); one with no station on it with its normal
    preferences (none, as a Channel Preference Query is answered)."""

    async def scenario():
        session, source, sent, clock = await provisioned(rig)
        searches, count = session.counts["search_sent"], len(sent)
        before = source.current().capabilities
        source.invalidate()
        clock.advance(15)
        await session.tick()
        ruid = before.radios[0].basic.ruid
        republish(
            source,
            clock,
            (2, 1),
            capabilities=moved(before),
            held=frozenset({ruid}) if held else frozenset(),
        )
        await session.tick()
        assert session.state == "provisioning"
        assert session.capabilities == moved(before)
        assert session.counts["channel_move_kept"] == 1
        assert session.counts["search_sent"] == searches
        types = message_types(sent, count)
        assert 0x0001 in types, "a Topology Notification"
        assert 0x8005 in types, "an unsolicited Channel Preference Report"
        report = next(f for f in sent[count:] if int.from_bytes(f[16:18], "big") == 0x8005)
        if held:
            assert report[22] == 0x8B, "its first TLV is a Channel Preference TLV"
            assert report[25:31] == ruid
        else:
            assert report[22] == 0x00, "no Channel Preference TLV: the End of Message"
        session.close()

    asyncio.run(scenario())


def test_another_capability_set_is_a_fresh_session(rig):
    async def scenario():
        session, source, _sent, clock = await provisioned(rig)
        before = source.current().capabilities
        radio = before.radios[0]
        bigger = replace(
            before,
            radios=(replace(radio, basic=replace(radio.basic, max_bss=radio.basic.max_bss + 1)),),
        )
        source.invalidate()
        clock.advance(15)
        await session.tick()
        republish(source, clock, (2, 1), capabilities=bigger)
        await session.tick()
        assert session.state == "source_lost"
        session.close()

    asyncio.run(scenario())


def test_without_a_window_a_lost_source_ends_the_session_at_once(rig):
    async def scenario():
        session, source, _sent = lifecycle(rig)
        await session.tick()
        await receive(session, response())
        await receive(session, frames()[0])
        assert session.state == "provisioning"
        source.invalidate()
        await session.tick()
        assert session.state == "source_lost"
        session.close()

    asyncio.run(scenario())
