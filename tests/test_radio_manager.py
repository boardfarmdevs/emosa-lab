import asyncio
import importlib.util
from pathlib import Path

import pytest

from emosa.opensync.session import OvsSession
from emosa.simulation.database import SimDatabase
from emosa.simulation.radio import (
    MONITOR,
    RadioConfig,
    RadioManager,
    observed_state,
    seed_radio_database,
)


def observation(ssid="driver-ssid", key="driver-observed-key"):
    return {
        "status": {"state": "ENABLED", "ssid[0]": ssid, "freq": "2437"},
        "config": {
            "ssid": ssid,
            "passphrase": key,
            "bssid": "02:00:00:ec:02:00",
            "wpa": "2",
            "key_mgmt": "WPA-PSK ",
            "rsn_pairwise_cipher": "CCMP ",
            "group_cipher": "CCMP",
        },
        "interface": {"ssid": ssid, "addr": "02:00:00:ec:02:00", "type": "AP", "channel": 6},
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "table,key,value",
    [
        ("status", "state", "DISABLED"),
        ("interface", "ssid", "different"),
        ("interface", "type", "managed"),
        ("interface", "channel", 11),
        ("config", "key_mgmt", "SAE"),
        ("config", "passphrase", ""),
    ],
)
def test_incomplete_or_disagreeing_radio_reads_cannot_publish_success(table, key, value):
    actual = observation()
    actual[table][key] = value
    with pytest.raises(ValueError):
        observed_state(actual)


@pytest.mark.unit
def test_observation_contains_driver_values_and_config_repr_hides_key():
    value = observed_state(observation())
    assert value["ssid"] == "driver-ssid"
    assert value["wpa_psks"] == ["map", [["key", "driver-observed-key"]]]
    assert "secret-key" not in repr(RadioConfig("ssid", "secret-key"))


@pytest.mark.unit
@pytest.mark.parametrize("name", ["common", "node"])
def test_privileged_helpers_refuse_other_hosts_before_commands(name, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "radio_guard_" + name, Path("deploy/radio-manager") / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.socket, "gethostname", lambda: "unrelated-host")
    monkeypatch.setattr(
        module,
        "run" if name == "common" else "command",
        lambda *a, **kw: pytest.fail("Command before host guard"),
    )
    with pytest.raises(RuntimeError):
        module.guard()


@pytest.mark.ovsdb
def test_manager_never_echoes_requested_state_and_rejects_unsupported_scope(tmp_path):
    class Driver:
        def __init__(self):
            self.calls = []
            self.unavailable = False
            self.on_observe = None

        async def apply(self, config):
            self.calls.append(config)

        async def observe(self):
            if self.on_observe:
                await self.on_observe()
            if self.unavailable:
                raise RuntimeError("AP absent")
            return observation()  # Deliberately differs from every requested Config.

    async def scenario():
        db = await SimDatabase(tmp_path / "db").start()
        session = OvsSession(db.endpoint, monitor_columns=MONITOR)
        driver = Driver()
        manager = RadioManager(session, driver)
        try:
            await seed_radio_database(session)
            event = await manager.cycle()
            assert event["driver_apply"] == "completed"
            snap = await session.snapshot()
            _, state = next(iter(snap["tables"]["Wifi_VIF_State"].items()))
            assert state["ssid"] == "driver-ssid"
            assert dict(state["wpa_psks"][1])["key"] == "driver-observed-key"
            vif = next(iter(snap["tables"]["Wifi_VIF_Config"]))
            await session.transact(
                [
                    {
                        "op": "update",
                        "table": "Wifi_VIF_Config",
                        "where": [["_uuid", "==", ["uuid", vif]]],
                        "row": {"ssid": "new-request"},
                    }
                ]
            )
            await manager.cycle(withhold=True)
            assert len(driver.calls) == 1
            await session.transact(
                [
                    {
                        "op": "update",
                        "table": "Wifi_VIF_Config",
                        "where": [["_uuid", "==", ["uuid", vif]]],
                        "row": {
                            "wpa_psks": [
                                "map",
                                [["key", "valid-passphrase"], ["other", "extra-key"]],
                            ]
                        },
                    }
                ]
            )
            event = await manager.cycle()
            assert event["configuration"] == "unsupported" and len(driver.calls) == 1
            driver.unavailable = True
            event = await manager.cycle()
            assert event["radio"] == "unavailable"
            state = next(iter((await session.snapshot())["tables"]["Wifi_VIF_State"].values()))
            assert state["enabled"] is False and state["wpa_psks"] == ["map", []]
            driver.unavailable = False

            async def compete_during_read():
                await session.transact(
                    [
                        {
                            "op": "update",
                            "table": "Wifi_VIF_Config",
                            "where": [["_uuid", "==", ["uuid", vif]]],
                            "row": {"ssid": "raced"},
                        }
                    ]
                )

            driver.on_observe = compete_during_read
            event = await manager.cycle()
            assert event["publication"] == "conflict"
            state = next(iter((await session.snapshot())["tables"]["Wifi_VIF_State"].values()))
            assert state["enabled"] is False  # A raced read cannot republish positive State.
        finally:
            await session.close()
            await db.close()

    asyncio.run(scenario())
