import json
import runpy
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("case", ["real_device", "neighbor_only", "duplicate_devices"])
def test_independent_inventory_check_requires_the_managed_device_not_a_neighbor_alias(
    tmp_path, case
):
    checker = runpy.run_path(str(Path("scripts/check-native-active.py")))
    device = "Device.WiFi.DataElements.Network.Device.2."
    neighbor = "Device.WiFi.DataElements.Network.Device.1.Interface.3.Neighbor.1."
    bss = device + "Radio.1.BSS.1."
    rows = {
        neighbor: {"ID": checker["AL"], "IsIEEE1905": False},
        device: {"ID": checker["AL"]},
        bss: {"BSSID": checker["BSSID"]},
        bss + "STA.1.": {"MACAddress": checker["STA"]},
    }
    if case == "neighbor_only":
        del rows[device]
    if case == "duplicate_devices":
        rows["Device.WiFi.DataElements.Network.Device.3."] = {"ID": checker["AL"]}
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps([{k: v} for k, v in rows.items()]))
    if case == "real_device":
        assert checker["stations"](path) == [{"MACAddress": checker["STA"]}]
    else:
        with pytest.raises(AssertionError):
            checker["stations"](path)
