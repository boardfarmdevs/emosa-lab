import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_every_architecture_requirement_and_acceptance_has_a_traceability_row():
    architecture = Path("doc/minimal-easymesh-architecture-requirements.md").read_text()
    expected = set(re.findall(r"\*\*([A-Z]+-\d+)\.", architecture))
    expected.update(re.findall(r"\| ([AWENT]\d{2}):", architecture))
    rows = json.loads(Path("docs/traceability.json").read_text())["rows"]
    assert {row["id"] for row in rows} == expected
    assert len(rows) == len(expected)
    for row in rows:
        if row["status"] == "blocked":
            assert row["blocker"] and row["independent_work"]
        if row["status"] == "verified":
            assert row["mode"] in {"model", "ovsdb-sim", "opensync-native"} and row["test_refs"]
            if row["mode"] == "opensync-native":
                assert row["evidence_refs"]
                assert row["id"] in {
                    "CORE-01",
                    "CORE-02",
                    "CORE-03",
                    "CORE-05",
                    "N01",
                    "N02",
                    "N04",
                }
        for ref in row["test_refs"] + row["implementation_refs"]:
            assert Path(ref).exists(), (row["id"], ref)
