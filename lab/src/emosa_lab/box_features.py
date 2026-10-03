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
    REGISTRAR_SSID,
    STATIONS,
    TARGET_PARENT,
    UPLINK,
    backhaul_on,
    backhaul_steering,
    now_ms,
    onboarded,
    pinned,
    pod_statistics,
    provision,
    publish,
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
    "uplink-held": uplink_held,
    "uplink-foreign-change": uplink_foreign_change,
    "backhaul-steering-kept": backhaul_steering_kept,
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
    "uplink-held": {"backhaul": True, "uplink": {**UPLINK, "ssid": "emosa-lab-elsewhere"}},
    "uplink-foreign-change": {"backhaul": True},
    "backhaul-steering-kept": {"backhaul": True},
    "telemetry-broker-restart": {"telemetry": True},
}
