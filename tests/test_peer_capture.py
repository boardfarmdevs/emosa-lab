import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

from emosa.errors import EmosaError
from emosa_lab.evaluation import peer_capture as capture

pytestmark = pytest.mark.unit
FIXTURE = Path(__file__).parent / "fixtures" / "peer-capture"
PEERS = {"controller_al": "02:00:00:e0:00:01", "agent_al": "02:00:00:e0:00:02"}


def records():
    return capture.parse_tsv((FIXTURE / "wired.fields.tsv").read_text())


def test_actual_peer_observations_retain_profile_and_radio_scope_gaps():
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    assert (
        hashlib.sha256((FIXTURE / "wired.fields.tsv").read_bytes()).hexdigest()
        == (provenance["projected_fields_sha256"])
    )
    result = capture.review(records(), **PEERS)
    assert result["status"] == "review_required"
    assert [(f["frame"], f["rule"]) for f in result["findings"]] == [
        (2, "discovery_profile_response")
    ]
    assert result["findings"][0]["observed"] == {"search_frame": 1, "search": [2], "response": [1]}
    m2 = next(o for o in result["observations"] if "m2_payload_count" in o)
    assert m2["m2_payload_count"] == 2 and m2["other_wps_types"] == [12]
    assert not m2["single_bss_payload_count"]
    assert m2["complete_request_mapping"] == "not_qualified"
    assert result["unassessed_partial_or_malformed"] == [6]
    assert not result["controller_onboarding_by_emosa_proven"]
    assert not result["physical_pod_proven"] and not result["full_protocol_conformance"]


def test_tshark_versions_agree_on_retained_native_capture():
    newer = capture.parse_tsv((FIXTURE / "wired-4.2.fields.tsv").read_text())
    provenance = json.loads((FIXTURE / "provenance-4.2.json").read_text())
    assert (
        hashlib.sha256((FIXTURE / "wired-4.2.fields.tsv").read_bytes()).hexdigest()
        == (provenance["projected_fields_sha256"])
    )
    assert capture.review(newer, **PEERS) == capture.review(records(), **PEERS)


@pytest.mark.parametrize("condition", ["duplicate", "outside-window", "wrong-destination"])
def test_uncertain_search_response_correlation_is_not_assessed(condition):
    selected = records()[:2]
    if condition == "duplicate":
        selected.insert(1, copy.deepcopy(selected[0]))
    elif condition == "outside-window":
        selected[1]["time"] = selected[0]["time"] + capture.CORRELATION_SECONDS + 0.1
    else:
        selected[1]["destination"] = "02:00:00:e0:00:99"
    result = capture.review(selected, **PEERS)
    assert not result["findings"]
    assert result["observations"][0]["profile_pairing"] == "not_established"
    assert not result["full_protocol_conformance"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("truncated", True),
        ("malformed", True),
        ("last_fragment", 0),
        ("fragment", 2),
        ("mid", None),
    ],
)
def test_incomplete_decode_cannot_produce_a_positive_inclusion_assessment(field, value):
    row = records()[0]
    row[field] = value
    result = capture.review([row], **PEERS)
    assert result["status"] == "no_selected_messages"
    assert result["unassessed_partial_or_malformed"] == [1]


def test_missing_and_duplicate_required_fields_are_visible():
    selected = records()[:1]
    selected[0]["tlvs"].remove(0x81)
    selected[0]["tlvs"].append(0x80)
    result = capture.review(selected, **PEERS)
    assert {f["rule"]: f["observed"] for f in result["findings"]} == {
        "search_searched_service": 0,
        "search_supported_service": 2,
    }


@pytest.mark.parametrize("bad", ["missing-columns", "nan-time", "ambiguous-frame", "frame-budget"])
def test_invalid_or_excessive_dissector_projection_rejected(monkeypatch, bad):
    text = (FIXTURE / "wired.fields.tsv").read_text()
    if bad == "frame-budget":
        monkeypatch.setattr(capture, "MAX_FRAMES", 1)
    else:
        fields = text.splitlines()[0].split("\t")
        if bad == "missing-columns":
            fields.pop()
        elif bad == "nan-time":
            fields[1] = "nan"
        else:
            fields[0] = "1,2"
        text = "\t".join(fields) + "\n"
    with pytest.raises(EmosaError, match="invalid projected"):
        capture.parse_tsv(text)


def test_failed_tool_does_not_expose_payload_in_error(tmp_path):
    with pytest.raises(EmosaError) as error:
        capture.bounded_tool(
            [sys.executable, "-c", "import sys; sys.stderr.write('private-key'); sys.exit(1)"],
            tmp_path,
            "failed",
        )
    assert "private-key" not in str(error.value)


def test_tool_output_budget_applies_even_to_completed_process(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "MAX_OUTPUT", 32)
    with pytest.raises(EmosaError, match="budget"):
        capture.bounded_tool([sys.executable, "-c", "print('x' * 1024)"], tmp_path, "oversized")


def test_missing_dissector_retains_failure_without_copying_capture(tmp_path):
    source = tmp_path / "sensitive.pcap"
    source.write_bytes(b"unpublished capture")
    output = tmp_path / "review"
    with pytest.raises(EmosaError, match="unavailable"):
        capture.analyze(source, output, **PEERS, tshark="/no/installed/dissector")
    assert list(p.name for p in output.iterdir()) == ["failure.json"]
    assert json.loads((output / "failure.json").read_text())["status"] == "analysis_failed"
    assert output.stat().st_mode & 0o077 == 0
