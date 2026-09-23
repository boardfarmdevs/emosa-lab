import copy
import json
import runpy
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
AUDIT = runpy.run_path("scripts/check-native-ap-esp.py")
ROOT = Path("doc/evidence/ap-esp/native-ap-esp-new-01")


@pytest.mark.parametrize("index", range(16))
def test_actual_sparse_receipts_and_invalid_input_withholding(index):
    probe = json.loads((ROOT / "ap-esp-probe.json").read_text())
    packets = AUDIT["NATIVE"]["packets"](ROOT / "ethernet.pcap")
    previous = AUDIT["observed"](json.loads((ROOT / "controller-after.json").read_text()))
    for row in probe["cases"][: index + 1]:
        packet = next(p for p in packets if p["mid"] == 0xD000 + row["index"])
        objects = json.loads((ROOT / f"ap-esp-inventory-{row['index']:02}.json").read_text())
        previous = AUDIT["check_case"](row, packet, objects, previous)


@pytest.mark.parametrize(
    "case", ["wrong_category", "stale_receipt", "changed_byte", "changed_utilization", "wrong_peer"]
)
def test_sparse_audit_rejects_corrupted_evidence(case):
    row = json.loads((ROOT / "ap-esp-probe.json").read_text())["cases"][0]
    packet = next(
        p for p in AUDIT["NATIVE"]["packets"](ROOT / "ethernet.pcap") if p["mid"] == 0xD000
    )
    objects = json.loads((ROOT / "ap-esp-inventory-00.json").read_text())
    before = AUDIT["observed"](json.loads((ROOT / "controller-after.json").read_text()))
    if case == "wrong_category":
        for group in objects:
            for value in group.values():
                if isinstance(value, dict) and value.get("BSSID") == "02:00:00:ec:02:00":
                    value["EstServiceParametersVI"] = value["EstServiceParametersBE"]
    elif case == "stale_receipt":
        row["observations"][-1]["wall_started_ns"] = int((packet["time"] - 1) * 1e9)
    elif case == "changed_byte":
        packet["tlvs"][0] = (0x94, packet["tlvs"][0][1][:-1] + b"\xff")
    elif case == "changed_utilization":
        row["expected"]["Utilization"] = 0
    else:
        packet["source"] = "020000009999"
    with pytest.raises(AssertionError):
        AUDIT["check_case"](row, packet, objects, copy.copy(before))
