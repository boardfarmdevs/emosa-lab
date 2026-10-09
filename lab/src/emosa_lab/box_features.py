# SPDX-License-Identifier: Apache-2.0
"""The lab in a box for the features' faults and refusals (easymesh-labs alignment plan 8.4):
an M2 set of several BSSes, the uplink switch held, a moved upstream kept, the broker gone
and back, another manager's steering rows. Each runs with either agent, C or Python, in
the box of emosa_lab.box.
"""

import asyncio
import subprocess
import time

from emosa_lab.box import (
    ACK,
    AUTOCONFIG_SEARCH,
    AUTOCONFIG_WSC,
    BACKHAUL_PARENT,
    BACKHAUL_STATION,
    REGISTRAR_SSID,
    STATIONS,
    TARGET_PARENT,
    UPLINK,
    backhaul_on,
    backhaul_steering,
    backhaul_steering_request,
    next_message,
    now_ms,
    onboarded,
    pinned,
    pod_statistics,
    provision,
    publish,
    steering_answer,
    steering_request,
    steering_rows,
    telemetry_applied,
    telemetry_status,
    uplink_status,
    wait_rows,
)

BACKHAUL_SSID = "EMOSA-WSC-backhaul"  # deploy/wire/component-registrar.c, its backhaul mode
BACKHAUL_KEY = "OnlySimulationWscBackhaul2026!"  # a public simulation input, as the SSID


async def until_async(predicate, seconds=60, step=0.3):
    """predicate() (a coroutine) until it holds; its last value."""
    deadline = time.monotonic() + seconds
    value = await predicate()
    while not value and time.monotonic() < deadline:
        await asyncio.sleep(step)
        value = await predicate()
    return value


async def credentials(box):
    return list((await box.rows("Wifi_Credential_Config")).values())


async def join(box, station, ssid, parent):
    """cm and the supplicant: the station now on this SSID at this parent."""
    uuid, _ = await box.vif("Wifi_VIF_State", station)
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_VIF_State",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {"ssid": ssid, "parent": parent},
            }
        ]
    )


def uplink_operation(box):
    return uplink_status(box).get("operation") or {}


async def multi_bss(box):
    """spec 3.4, 8.3: an M2 set of a fronthaul and a backhaul BSS (with the Backhaul STA bit,
    as prplMesh sends it): both BSSes written as one guarded change and applied, the backhaul
    BSS on the profile's backhaul slot; its credentials serve the uplink switch (credentials
    m2): the station pinned to the configured parent with the set's backhaul SSID and key."""
    onboarding = await onboarded(box)
    vifs = list((await box.rows("Wifi_VIF_Config")).values())
    fronthaul = [v for v in vifs if v.get("ssid") == REGISTRAR_SSID]
    backhaul = [v for v in vifs if v.get("ssid") == BACKHAUL_SSID]

    async def switch_written():
        return [
            c
            for c in await credentials(box)
            if c.get("ssid") == BACKHAUL_SSID
            and str(c.get("bssid") or "").lower() == BACKHAUL_PARENT
        ]

    written = await until_async(switch_written, seconds=60)
    station = UPLINK["station"]
    if written:
        await join(box, station, BACKHAUL_SSID, BACKHAUL_PARENT)
    applied = await box.until(
        lambda: uplink_operation(box).get("state") == "OBSERVED_APPLIED", seconds=60
    )
    keys = [str(c.get("onboard_psk") or c.get("security") or "") for c in written or []]
    return box.result(
        passed=bool(onboarding.get("applied"))
        and len(fronthaul) == 1
        and len(backhaul) == 1
        and backhaul[0].get("multi_ap") == "backhaul_bss"
        and bool(written)
        and any(BACKHAUL_KEY in k for k in keys)
        and bool(applied),
        onboarded=bool(onboarding.get("applied")),
        backhaul_bss=[(v.get("if_name"), v.get("multi_ap")) for v in backhaul],
        switch_written=bool(written),
        switch_key_from_m2=any(BACKHAUL_KEY in k for k in keys),
        uplink_applied=bool(applied),
    )


# five BSSes, each with its own BSS index, as RDK sends them
RDK_SET = ("configure", "configure:3", "backhaul", "configure:4", "configure:5")
ONE_AP = "rpi-pod-mt7921u-6.6.1-v1"  # the Pis' MT7921U: no extra slots


async def set_beyond_slots(box):
    """spec 3.4 (alignment plan 9.A1): a five-BSS M2 set, as RDK's controller sends it to every
    agent, to a radio whose profile has no extra slots (the Pis' MT7921U runs one AP): the set
    is taken, not refused; the primary fronthaul BSS is written and applied and the other four
    left out, no slot VIF and no backhaul BSS written, and only the primary's passphrase
    stored."""
    onboarding = await onboarded(box)
    vifs = list((await box.rows("Wifi_VIF_Config")).values())
    aps = [v for v in vifs if v.get("mode") == "ap"]
    fronthaul = [v for v in aps if v.get("ssid") == REGISTRAR_SSID]
    backhaul = [v for v in vifs if v.get("ssid") == BACKHAUL_SSID]
    secrets = box.directory / "state" / box.serial / "secrets"
    stored = sorted(p.name for p in secrets.glob("wsc-*")) if secrets.is_dir() else []
    return box.result(
        passed=bool(onboarding.get("applied"))
        and len(fronthaul) == 1
        and len(aps) == 1
        and not backhaul
        and len(stored) == 1,
        onboarded=bool(onboarding.get("applied")),
        failed=onboarding.get("failed"),
        access_points=[(v.get("if_name"), v.get("ssid")) for v in aps],
        backhaul_bss=bool(backhaul),
        stored_passphrases=len(stored),
    )


# a fronthaul, a second fronthaul with its own SSID and a backhaul BSS, each with its own index
ROLE_SET = ("configure", "changed:3", "backhaul")
SECOND_SSID = "Conflicting-WSC-request"  # the registrar's changed mode
ROLES = {
    REGISTRAR_SSID: "fronthaul_bss",
    SECOND_SSID: "fronthaul_bss",
    BACKHAUL_SSID: "backhaul_bss",
}


async def ap_roles(box):
    """Each AP VIF with an SSID of the controller's set: its name, SSID, and multi_ap in its
    Config and in its State."""
    states = {r.get("if_name"): r for r in (await box.rows("Wifi_VIF_State")).values()}
    return sorted(
        (
            v.get("if_name"),
            v.get("ssid"),
            v.get("multi_ap"),
            states.get(v.get("if_name"), {}).get("multi_ap"),
        )
        for v in (await box.rows("Wifi_VIF_Config")).values()
        if v.get("mode") == "ap" and v.get("ssid") in ROLES
    )


def roles_carried(roles, expected):
    """Every BSS of the set written once, its Config and State with the role its M2 gave it."""
    return sorted(ssid for _, ssid, _, _ in roles) == sorted(expected) and all(
        config == state == ROLES[ssid] for _, ssid, config, state in roles
    )


async def fronthaul_role(box):
    """spec 3.2, 3.4 (finding 21): every BSS the agent writes carries in multi_ap the role its
    M2's Multi-AP extension gives it, so the pod advertises it as a Multi-AP agent's AP does:
    the recorded pod's fronthaul (its multi_ap unset) updated to fronthaul_bss, a second
    fronthaul BSS created on a slot as fronthaul_bss, the backhaul BSS as backhaul_bss."""
    onboarding = await onboarded(box)
    roles = await ap_roles(box)
    return box.result(
        passed=bool(onboarding.get("applied")) and roles_carried(roles, ROLES),
        onboarded=bool(onboarding.get("applied")),
        failed=onboarding.get("failed"),
        roles=roles,
    )


async def fronthaul_role_cold(box):
    """spec 3.2, 3.4 (finding 21): the Pis' case: a pod whose bootstrap made its radio only
    (one AP, no slots) gets RDK's five-BSS set; the fronthaul the agent creates carries
    fronthaul_bss, and the pod applies it."""
    onboarding = await onboarded(box)
    roles = await ap_roles(box)
    return box.result(
        passed=bool(onboarding.get("applied")) and roles_carried(roles, [REGISTRAR_SSID]),
        onboarded=bool(onboarding.get("applied")),
        failed=onboarding.get("failed"),
        roles=roles,
    )


async def wired_uplink(box):
    """spec 8.4 (alignment plan 9.A1): a wired pod, cm using its Ethernet port eth1 as the
    uplink, and the agent's uplink mode ethernet: the agent bridges eth1 into br-home
    (Connection_Manager_Uplink.bridge) in one guarded write, applied on the same start, and
    reports the uplink as ethernet, in use."""
    onboarding = await onboarded(box)

    async def bridged():
        rows = (await box.rows("Connection_Manager_Uplink")).values()
        return [r for r in rows if r.get("if_name") == "eth1" and r.get("bridge") == "br-home"]

    written = await until_async(bridged, seconds=60)

    def applied():
        operation = ((box.status() or {}).get("uplink") or {}).get("operation") or {}
        return operation.get("state") == "OBSERVED_APPLIED"

    done = await box.until(applied, seconds=60)
    uplink = (box.status() or {}).get("uplink") or {}
    return box.result(
        passed=bool(onboarding.get("applied"))
        and bool(written)
        and bool(done)
        and uplink.get("mode") == "ethernet"
        and uplink.get("in_use") == "eth1"
        and uplink.get("bridge") == "br-home",
        onboarded=bool(onboarding.get("applied")),
        bridged=bool(written),
        applied=bool(done),
        uplink={k: uplink.get(k) for k in ("mode", "port", "bridge", "in_use", "waiting")},
    )


async def uplink_held(box):
    """spec 8.3: a switch the pod never confirms (here its station never joins the
    configured SSID) times out after 90 s; EMOSA then holds the pod on option 2 and does not
    switch it again on its own."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    written = await until_async(lambda: _credential_for(box, box.uplink["ssid"]), seconds=60)
    held = await box.until(lambda: uplink_status(box).get("held"), seconds=150)
    operation = uplink_operation(box)
    switches = len(await credentials(box))
    await asyncio.sleep(15)
    again = len(await credentials(box))
    return box.result(
        passed=bool(written)
        and bool(held)
        and operation.get("state") == "TIMED_OUT"
        and again == switches,
        switch_written=bool(written),
        held=held,
        operation=operation.get("state"),
        switched_again=again != switches,
    )


async def _credential_for(box, ssid):
    return [c for c in await credentials(box) if c.get("ssid") == ssid]


SWITCH_DEADLINE = 95  # past an uplink switch's 90 s to be confirmed (spec 8.3)


async def _reswitch_in_use(box, vif_parent):
    """(a) of rdk-1004's lab.sh up stopping on pod-1's uplink APPLY_TIMEOUT (9 October): a
    Backhaul Steering Request to the BSS the station is already on, the station's
    Wifi_VIF_Config parent empty or another BSS's: answered success, and past the switch's
    deadline the uplink still applied, the pod not held."""
    if not await backhaul_on(box):
        return box.result(passed=False, failed="uplink switch not applied")
    uuid, _ = await box.vif("Wifi_VIF_Config", UPLINK["station"])
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_VIF_Config",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {"parent": vif_parent or ["set", []]},
            }
        ]
    )
    answered = len(box.sent(0x801A))
    mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, BACKHAUL_PARENT),)
    )
    ack = await box.until(lambda: box.controller.reply(ACK, mid), seconds=5)
    response = await next_message(box, 0x801A, answered, seconds=60)
    answer = steering_answer(response)
    await asyncio.sleep(SWITCH_DEADLINE)
    operation = uplink_operation(box)
    held = uplink_status(box).get("held")
    return box.result(
        passed=bool(ack)
        and answer == (BACKHAUL_STATION, BACKHAUL_PARENT, 0)
        and operation.get("state") == "OBSERVED_APPLIED"
        and not held,
        acked=bool(ack),
        answer=answer,
        operation=operation.get("state"),
        reason=operation.get("reason"),
        held=held,
    )


async def uplink_reswitch_parent_empty(box):
    return await _reswitch_in_use(box, None)


async def uplink_reswitch_parent_other(box):
    return await _reswitch_in_use(box, "02:00:00:00:19:09")


async def uplink_instance_change(box):
    """(b) of rdk-1004's lab.sh up stopping on pod-1's uplink APPLY_TIMEOUT (9 October): the
    pod's OpenSync starts again (a new database, a new instance) after EMOSA committed its
    uplink switch and before the station was confirmed on it. spec 8.3 takes that for the
    switch's failure (OpenSync restarts a pod to its bootstrap uplink when its router checks
    fail): the switch cannot be confirmed on the new start, times out (APPLY_TIMEOUT) and holds
    the pod on option 2, and EMOSA does not switch it again on its own. Both agents."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    written = await until_async(lambda: _credential_for(box, box.uplink["ssid"]), seconds=60)
    if not written:
        return box.result(passed=False, failed="uplink switch not written")
    first = uplink_operation(box).get("operation_id")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    await box.new_pod_database()
    again = await provision(box, searches, m1s)
    held = await box.until(lambda: uplink_status(box).get("held"), seconds=SWITCH_DEADLINE + 40)
    operation = uplink_operation(box)
    await asyncio.sleep(10)
    switched_again = bool(await _credential_for(box, box.uplink["ssid"]))
    return box.result(
        passed=bool(again)
        and bool(held)
        and operation.get("operation_id") == first
        and operation.get("state") == "TIMED_OUT"
        and operation.get("reason") == "APPLY_TIMEOUT"
        and not switched_again,
        provisioning_again=again,
        held=held,
        operation=operation.get("operation_id"),
        state=operation.get("state"),
        reason=operation.get("reason"),
        switched_again_on_the_new_start=switched_again,
    )


async def uplink_foreign_change(box):
    """spec 8.3: once applied, another manager changes the switched station's credential on
    the same start of the pod: EMOSA holds the pod on option 2 and leaves the change alone."""
    if not await backhaul_on(box):
        return box.result(passed=False, failed="uplink switch not applied")
    other = "02:00:00:00:19:09"
    rows = await box.rows("Wifi_Credential_Config")
    changed = [u for u, r in rows.items() if str(r.get("bssid") or "").lower() == BACKHAUL_PARENT]
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_Credential_Config",
                "where": [["_uuid", "==", ["uuid", u]]],
                "row": {"bssid": other},
            }
            for u in changed
        ]
    )
    held = await box.until(lambda: uplink_status(box).get("held"), seconds=30)
    await asyncio.sleep(3)
    left_alone = await pinned(box, other) and not await pinned(box, BACKHAUL_PARENT)
    return box.result(
        passed=bool(changed) and bool(held) and left_alone,
        changed_rows=len(changed),
        held=held,
        change_left_alone=left_alone,
    )


async def backhaul_steering_kept(box):
    """spec 8.3: a moved upstream is kept: the agent started again on its state directory
    keeps the station on the target the controller moved it to, not the configured parent."""
    moved = await backhaul_steering(box)
    if not moved.get("passed"):
        return box.result(passed=False, failed="backhaul steering", steering=moved)
    before = await box.rows("Wifi_Credential_Config")
    box.process.terminate()
    box.process.wait(10)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.restart_agent()
    again = await provision(box, searches, m1s)
    await box.until(lambda: uplink_status(box).get("bssid"), seconds=30)
    await asyncio.sleep(5)
    kept = uplink_status(box).get("bssid")
    # nothing written again: the station stays pinned to the target, not switched back
    rewritten = await box.rows("Wifi_Credential_Config") != before
    return box.result(
        passed=again
        and str(kept or "").lower() == TARGET_PARENT
        and await pinned(box, TARGET_PARENT)
        and not rewritten,
        provisioning_again=again,
        upstream_after_restart=kept,
        credentials_rewritten=rewritten,
    )


GONE_PARENT = "02:00:00:00:19:03"  # a backhaul BSS of the controller's that is gone


async def _revert_station(box):
    """cm, its uplink failed: the station back on the pod's own credentials (none pinned to a
    BSSID), on the same start of the pod."""
    rows = await box.rows("Wifi_Credential_Config")
    own = [
        uuid
        for uuid, row in rows.items()
        if row.get("onboard_type") == "multi_ap" and not isinstance(row.get("bssid"), str)
    ]
    uuid, _ = await box.vif("Wifi_VIF_Config", UPLINK["station"])
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_VIF_Config",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {"credential_configs": ["set", [["uuid", u] for u in own]]},
            }
        ]
    )
    return bool(own)


async def backhaul_kept_gone(box):
    """spec 8.3: a controller's move is kept for the pod's later starts; when the kept
    target's BSS is gone at a start, its switch times out after 90 s. The agent then holds
    nothing and writes nothing until cm has the station off the failed credential (a switch
    written meanwhile is reverted with it), and switches to the configured upstream then, on
    the same start (finding 18)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    tried = await until_async(lambda: pinned(box, GONE_PARENT), seconds=60)

    def timed_out():
        return (uplink_status(box).get("operation") or {}).get("state") == "TIMED_OUT"

    failed = await box.until(timed_out, seconds=150)
    await asyncio.sleep(5)
    status = uplink_status(box)
    waited = status.get("waiting") == "the pod returning to its bootstrap uplink" and not (
        await pinned(box, BACKHAUL_PARENT)
    )
    reverted = await _revert_station(box)

    def back():
        status = uplink_status(box)
        operation = status.get("operation") or {}
        return status.get("bssid") == BACKHAUL_PARENT and operation.get("state") == (
            "OBSERVED_APPLIED"
        )

    applied = await box.until(back, seconds=60)
    status = uplink_status(box)
    kept = (box.directory / "state" / box.serial / "uplink" / "target.json").exists()
    return box.result(
        passed=bool(tried)
        and bool(failed)
        and waited
        and reverted
        and bool(applied)
        and not status.get("held")
        and not kept
        and await pinned(box, BACKHAUL_PARENT),
        tried_kept_target=bool(tried),
        kept_switch_timed_out=bool(failed),
        waited_for_the_revert=waited,
        configured_applied=bool(applied),
        held=status.get("held"),
        kept_target_left=kept,
    )


async def telemetry_broker_restart(box):
    """spec 3.6: the broker gone and back: the agent notices, connects again with its
    back-off, subscribes to the pod's topic again, and takes the pod's next report."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    if not await telemetry_applied(box):
        return box.result(passed=False, failed="telemetry not applied")
    await box.until(lambda: telemetry_status(box).get("subscribed"), seconds=30)
    _, clients = pod_statistics(now_ms())
    await publish(box, clients)
    first = await box.until(lambda: telemetry_status(box).get("reports_accepted"), seconds=20)
    box.broker.terminate()
    box.broker.wait(5)
    lost = await box.until(lambda: telemetry_status(box).get("subscribed") is False, seconds=60)
    box.broker = subprocess.Popen(
        ["mosquitto", "-c", str(box.directory / "broker.conf")],
        stdout=(box.directory / "broker.log").open("a"),
        stderr=subprocess.STDOUT,
    )
    back = await box.until(lambda: telemetry_status(box).get("subscribed"), seconds=60)
    _, clients = pod_statistics(now_ms())
    await publish(box, clients)
    second = await box.until(
        lambda: (telemetry_status(box).get("reports_accepted") or 0) >= 2, seconds=20
    )
    return box.result(
        passed=bool(first) and bool(lost) and bool(back) and bool(second),
        first_report=bool(first),
        noticed_the_loss=bool(lost),
        subscribed_again=bool(back),
        next_report=bool(second),
        reports_accepted=telemetry_status(box).get("reports_accepted"),
    )


async def client_rows(box, station):
    rows = (await box.rows("Band_Steering_Clients")).values()
    return [dict(r) for r in rows if r.get("mac") == station]


async def steering_conflict(box):
    """spec 3.7: another manager already steers the station (its client row is not one of
    EMOSA's): a mandate for it opens no window, and the other manager's row is left as it
    was."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    station = STATIONS[0]
    # another manager's row: the schema's required kick_type and reject_detection, its
    # own preferences, and none of EMOSA's watch marker
    foreign = {
        "mac": station,
        "kick_type": "btm_deauth",
        "reject_detection": "none",
        "pref_5g": "always",
    }
    inserted = await box.transact(
        [{"op": "insert", "table": "Band_Steering_Clients", "row": foreign}]
    )
    if not inserted or any("error" in r for r in inserted):
        return box.result(passed=False, failed="the other manager's row", insert=inserted)
    before = await client_rows(box, station)
    ack = await box.ask(0x8014, (steering_request(station),), ACK)
    await asyncio.sleep(5)
    clients, neighbors, groups = await steering_rows(box)
    after = await client_rows(box, station)
    status = (box.status() or {}).get("steering") or {}
    return box.result(
        passed=bool(ack) and not neighbors and not groups and after == before,
        acked=bool(ack),
        window_rows={"neighbors": len(neighbors), "groups": len(groups)},
        foreign_row_unchanged=after == before,
        steering_counts=status.get("counts"),
        steering_history=(status.get("history") or [])[-2:],
    )


async def steering_window_expired(box):
    """spec 3.7: a window the pod's owm never takes (its station row never steering) ends
    at its deadline: the rows the window inserted are deleted, and the outcome recorded."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    ack = await box.ask(0x8014, (steering_request(STATIONS[0]),), ACK)
    opened = await wait_rows(box, lambda c, n, g: c and n and g)
    closed = await wait_rows(box, lambda c, n, g: not c and not n, seconds=90)

    def outcome():  # the status file is rewritten at most once a second
        return ((box.status() or {}).get("steering") or {}).get("history")

    history = await box.until(outcome, seconds=10) or []
    status = (box.status() or {}).get("steering") or {}
    return box.result(
        passed=bool(ack) and bool(opened) and bool(closed) and bool(history),
        acked=bool(ack),
        opened=bool(opened),
        closed=bool(closed),
        outcome=history[-1].get("outcome") if history else None,
        steering_counts=status.get("counts"),
    )


async def steering_restart(box):
    """spec 3.7, design 6: the agent ends while a window is open; started again on its
    journal it closes the window it finds left over, deleting exactly its rows."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    ack = await box.ask(0x8014, (steering_request(STATIONS[0]),), ACK)
    opened = await wait_rows(box, lambda c, n, g: c and n and g)
    box.process.kill()  # no chance to close anything itself
    box.process.wait(10)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.restart_agent()
    again = await provision(box, searches, m1s)
    closed = await wait_rows(box, lambda c, n, g: not c and not n, seconds=60)
    status = (box.status() or {}).get("steering") or {}
    history = status.get("history") or []
    return box.result(
        passed=bool(ack) and bool(opened) and again and bool(closed),
        opened=bool(opened),
        provisioning_again=again,
        closed=bool(closed),
        outcome=history[-1].get("outcome") if history else None,
        steering_counts=status.get("counts"),
    )


SCENARIOS = {
    "multi-bss": multi_bss,
    "set-beyond-slots": set_beyond_slots,
    "fronthaul-role": fronthaul_role,
    "fronthaul-role-cold": fronthaul_role_cold,
    "wired-uplink": wired_uplink,
    "uplink-held": uplink_held,
    "uplink-foreign-change": uplink_foreign_change,
    "uplink-reswitch-parent-empty": uplink_reswitch_parent_empty,
    "uplink-reswitch-parent-other": uplink_reswitch_parent_other,
    "uplink-instance-change": uplink_instance_change,
    "backhaul-steering-kept": backhaul_steering_kept,
    "backhaul-kept-gone": backhaul_kept_gone,
    "telemetry-broker-restart": telemetry_broker_restart,
    "steering-conflict": steering_conflict,
    "steering-window-expired": steering_window_expired,
    "steering-restart": steering_restart,
}
M2_UPLINK = {
    "mode": "multi-ap",
    "bssid": BACKHAUL_PARENT,
    "credentials": "m2",
    "station": UPLINK["station"],
}
OPTIONS = {
    "multi-bss": {"backhaul": True, "multi_bss": True, "uplink": M2_UPLINK},
    "set-beyond-slots": {"multi_bss": True, "profile": ONE_AP, "m2_modes": RDK_SET},
    "fronthaul-role": {"multi_bss": True, "m2_modes": ROLE_SET},
    "fronthaul-role-cold": {
        "multi_bss": True,
        "profile": ONE_AP,
        "m2_modes": RDK_SET,
        "cold": True,
    },
    "wired-uplink": {"wired": True},
    "uplink-held": {"backhaul": True, "uplink": {**UPLINK, "ssid": "emosa-lab-elsewhere"}},
    "uplink-foreign-change": {"backhaul": True},
    "uplink-reswitch-parent-empty": {"backhaul": True},
    "uplink-reswitch-parent-other": {"backhaul": True},
    "uplink-instance-change": {
        "backhaul": True,
        "uplink": {**UPLINK, "ssid": "emosa-lab-elsewhere"},
    },
    "backhaul-steering-kept": {"backhaul": True},
    "backhaul-kept-gone": {"backhaul": True, "kept_target": GONE_PARENT},
    "telemetry-broker-restart": {"telemetry": True},
}
