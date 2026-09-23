import runpy
import subprocess
from pathlib import Path

import pytest

audit = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/check-station-accounting.py")
)
pytestmark = pytest.mark.unit


@pytest.mark.parametrize(("yes", "no"), [("1", "0"), ("True", "False")])
def test_tshark_versions_preserve_protection_and_rejected_frame_flags(monkeypatch, yes, no):
    # Captured field formats from local 3.6 and Ubuntu 24.04's 4.2. Preserve
    # true retry/fragment/aggregation flags so the accounting gate sees them.
    lines = [
        "\t".join(
            ["1", "1.0", "100", "20", audit["AP"], audit["STA"], "0x0028", yes, yes, "0", yes, yes]
        ),
        "\t".join(["2", "1.1", "34", "20", "", audit["AP"], "0x001d", no, no, "", no, ""]),
    ]
    monkeypatch.setattr(subprocess, "check_output", lambda *_a, **_k: "\n".join(lines))
    frames = audit["frames"](Path("unused"), "tshark")
    for key in ("protected", "retry", "more_fragments", "amsdu"):
        assert frames[0][key] is True
        assert frames[1][key] is False


def test_unknown_boolean_cannot_silently_disable_accounting_guard():
    with pytest.raises(AssertionError, match="unexpected tshark boolean"):
        audit["flag"]("unrecognized")
