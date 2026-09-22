"""Admission observations for the bounded native policy; no LXD calls."""

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def native(monkeypatch):
    root = Path("deploy/peer-baseline").resolve()
    modules = {}
    for name in ("setup", "node", "run"):
        spec = importlib.util.spec_from_file_location(name, root / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(__import__("sys").modules, name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    return modules["run"]


def attempt(native, tmp_path, policy):
    value = object.__new__(native.Attempt)
    value.directory = tmp_path
    value.policy = policy
    value.mode = "wired"
    value.fresh_after = None
    return value


@pytest.mark.parametrize(
    "policy,count,expected",
    [
        ("sole-fronthaul", 1, True),
        ("sole-fronthaul", 2, False),
        ("front-and-backhaul", 1, False),
        ("front-and-backhaul", 2, True),
    ],
)
def test_controller_inventory_requires_complete_selected_bss_set(
    native, tmp_path, monkeypatch, policy, count, expected
):
    device = "Device.WiFi.DataElements.Network.Device.2."
    radio = device + "Radio.1."
    data = {
        device: {"ID": native.AL},
        radio: {"ID": native.RUID, "BSSNumberOfEntries": count},
        radio + "BSS.1.": {
            "BSSID": native.RUID,
            "SSID": native.FRONTHAUL,
            "Enabled": True,
            "FronthaulUse": True,
            "BackhaulUse": False,
        },
        device + "MultiAPDevice.Backhaul.": {
            "BackhaulDeviceID": "02:00:00:e0:00:01",
            "LinkType": "Ethernet",
        },
    }
    if count == 2:
        data[radio + "BSS.2."] = {
            "BSSID": "02:00:00:ec:02:01",
            "SSID": native.BACKHAUL,
            "Enabled": True,
            "FronthaulUse": False,
            "BackhaulUse": True,
        }
    monkeypatch.setattr(native, "inside", lambda *args: json.dumps(data))
    assert attempt(native, tmp_path, policy).inventory() is expected


@pytest.mark.parametrize(
    "name,second,expected",
    [
        ("em-baseline-agent", False, True),
        ("em-baseline-agent", True, False),
        ("em-baseline-controller", False, False),
        ("em-baseline-controller", True, True),
    ],
)
def test_sole_policy_keeps_root_backhaul_and_rejects_extra_agent_bss(
    native, tmp_path, monkeypatch, name, second, expected
):
    status = "state=ENABLED\nssid[0]=" + native.FRONTHAUL + "\n"
    if second:
        status += "ssid[1]=" + native.BACKHAUL + "\n"
    monkeypatch.setattr(native, "ap", lambda *args: status)
    assert attempt(native, tmp_path, "sole-fronthaul").applied(name) is expected
