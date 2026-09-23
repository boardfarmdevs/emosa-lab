import asyncio
import importlib.util
import time
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
        "interface": {
            "ssid": ssid,
            "addr": "02:00:00:ec:02:00",
            "type": "AP",
            "channel": 6,
            "tx_power_dbm": 17,
        },
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


@pytest.mark.unit
@pytest.mark.parametrize(
    "ending, count, complete",
    [(b"", 0, True), (b"FAIL\n", 0, True), (b"", 1, False), (b"broken", 0, False)],
)
def test_hostap_empty_enumeration_requires_confirming_status(monkeypatch, ending, count, complete):
    spec = importlib.util.spec_from_file_location("station_reader", "deploy/radio-manager/node.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Control:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def settimeout(self, *_):
            pass

        def bind(self, *_):
            pass

        def connect(self, *_):
            pass

        def send(self, command):
            self.command = command

        def recv(self, *_):
            if self.command == b"STATUS":
                return f"state=ENABLED\nnum_sta[0]={count}\n".encode()
            assert self.command == b"STA-FIRST"
            return ending

    monkeypatch.setattr(module.socket, "socket", lambda *_: Control())
    actual = module.stations()
    assert actual["complete"] is complete
    if complete:
        assert actual["clients"] == []


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


@pytest.mark.ovsdb
def test_station_membership_age_and_binding_with_real_ovsdb(tmp_path):
    from emosa.secrets import SecretStore
    from emosa.simulation.native_onboarding import AGENT, CONTROLLER, RadioReportSource
    from emosa.simulation.station_telemetry import NODE_ID, TOPIC, encode_stations
    from emosa.simulation.wsc_provisioning import BoundBackend
    from emosa.telemetry.stations import StationSource
    from emosa.wire.autoconfiguration import PeerBinding

    class Driver:
        clients = []

        async def apply(self, config):
            pass

        async def observe(self):
            return observation() | {
                "stations": {
                    "complete": True,
                    "timestamp_ms": int(time.time() * 1000),
                    "clients": self.clients,
                }
            }

    async def scenario():
        db = await SimDatabase(tmp_path / "db").start()
        session = OvsSession(db.endpoint, monitor_columns=MONITOR)
        try:
            await seed_radio_database(session)
            await session.transact(
                [{"op": "insert", "table": "AWLAN_Node", "row": {"serial_number": NODE_ID}}]
            )
            driver = Driver()
            manager = RadioManager(session, driver)
            backend = BoundBackend(
                session,
                SecretStore(tmp_path / "secrets"),
                radio_mac="02:00:00:ec:02:00",
                bssid="02:00:00:ec:02:00",
                if_name="wlan0",
                radio_name="phy1",
            )
            telemetry = StationSource(NODE_ID, TOPIC)
            source = RadioReportSource(
                backend, PeerBinding("probe0", 1, AGENT, CONTROLLER, (CONTROLLER,)), telemetry
            )

            async def cycle():
                event = await manager.cycle()
                assert event["publication"] == "observed-state"
                assert telemetry.receive(TOPIC, encode_stations(event["stations"], "driver-ssid"))
                assert await source.refresh()
                return await session.snapshot(), source.source.current()

            _, initial = await cycle()
            assert initial.topology.inventory_complete
            assert initial.operating_radios[0].tx_power_dbm == 17
            anchor = backend.anchor.binding_token
            driver.clients = [{"mac": "02:00:00:00:02:00", "connected_seconds": 37}]
            raw, current = await cycle()
            assert current.context_token == initial.context_token
            assert backend.anchor.binding_token == anchor
            client = current.topology.clients.bsses[0].clients[0]
            assert client.mac == bytes.fromhex("020000000200") and client.association_seconds == 37
            station_uuid = next(iter(raw["tables"]["Wifi_Associated_Clients"]))
            driver.clients[0]["connected_seconds"] = 38
            await asyncio.sleep(0.003)
            raw, current = await cycle()
            assert list(raw["tables"]["Wifi_Associated_Clients"]) == [station_uuid]
            assert current.topology.clients.bsses[0].clients[0].association_seconds == 38
            telemetry.disconnect()
            assert await source.refresh()
            assert not source.source.current().topology.inventory_complete
            assert source.source.current().operating_radios == ()
            # OVSDB membership alone does not establish association time.
            assert (
                next(iter(raw["tables"]["Wifi_Associated_Clients"].values()))["mac"]
                == "02:00:00:00:02:00"
            )
            driver.clients = []
            await asyncio.sleep(0.003)
            raw, current = await cycle()
            assert current.topology.inventory_complete
            assert current.topology.clients.bsses[0].clients == ()
            assert not raw["tables"].get("Wifi_Associated_Clients")
            assert backend.anchor.binding_token == anchor
        finally:
            await session.close()
            await db.close()

    asyncio.run(scenario())
