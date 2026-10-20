# SPDX-License-Identifier: Apache-2.0
"""spec 3.10, §5 (18 October): a legacy pod's start marker, EMOSA's Node_Config row that tells its
OpenSync's starts apart (its radio rows come back from its vendor's seed with the same UUIDs)."""

import asyncio
import hashlib
import json
import types
from importlib.resources import files

import pytest

from emosa.agent.pod import PodReportSource
from emosa.errors import EmosaError, Reason
from emosa.opensync import uplink
from emosa.opensync.node_config import NodeConfigIntent
from emosa.opensync.profiles import load as load_profile
from emosa.opensync.uplink import (
    START_KEY,
    START_MODULE,
    start_instance,
    start_marker,
    start_marker_error,
    start_marker_ops,
    start_pending,
)

pytestmark = pytest.mark.unit

RADIOS = {"b-radio": {}, "a-radio": {}}


def marker(token, module=START_MODULE, key=START_KEY):
    return {"m": {"module": module, "key": key, "value": token}}


def instance_of(*extra):
    return hashlib.sha256(json.dumps(["a-radio", "b-radio", *extra]).encode()).hexdigest()[:16]


@pytest.fixture
def marked(monkeypatch):
    monkeypatch.setattr(uplink, "_start_marker", True)
    monkeypatch.setattr(uplink, "_session_start", None)


def test_a_legacy_pods_instance_is_its_radio_rows_with_its_start_markers_token(marked):
    tables = {"Wifi_Radio_Config": RADIOS, "Node_Config": marker("0123456789abcdef")}
    assert start_instance(tables) == instance_of("0123456789abcdef")
    assert start_marker(tables) == "0123456789abcdef" and not start_pending(tables)
    # another start's marker, another instance; the radio rows alone tell nothing apart
    other = {"Wifi_Radio_Config": RADIOS, "Node_Config": marker("fedcba9876543210")}
    assert start_instance(other) != start_instance(tables)


def test_a_legacy_pod_without_its_start_marker_is_not_ready(marked):
    for node_config in ({}, marker("x", module="site-hook"), marker("x", key="other"), marker("")):
        tables = {"Wifi_Radio_Config": RADIOS, "Node_Config": node_config}
        assert start_pending(tables)
        with pytest.raises(EmosaError) as raised:
            start_instance(tables)
        assert raised.value.code == Reason.NOT_READY


def test_without_the_marker_the_instance_is_the_radio_rows_and_the_sessions_token(monkeypatch):
    monkeypatch.setattr(uplink, "_start_marker", False)
    monkeypatch.setattr(uplink, "_session_start", None)
    tables = {"Wifi_Radio_Config": RADIOS, "Node_Config": marker("0123456789abcdef")}
    assert start_instance(tables) == instance_of() and not start_pending(tables)
    monkeypatch.setattr(uplink, "_session_start", "1111222233334444")
    assert start_instance(tables) == instance_of("1111222233334444")


def test_the_start_marker_is_inserted_only_where_the_pod_has_none_and_never_persists():
    wait, insert = start_marker_ops("0123456789abcdef")
    assert wait["op"] == "wait" and wait["table"] == "Node_Config" and wait["rows"] == []
    assert wait["until"] == "==" and wait["timeout"] == 0
    assert wait["where"] == [["module", "==", "emosa"], ["key", "==", "start"]]
    assert insert == {
        "op": "insert",
        "table": "Node_Config",
        "row": {"module": "emosa", "key": "start", "value": "0123456789abcdef"},
    }


def test_no_configured_module_may_be_emosas_own():
    with pytest.raises(EmosaError) as raised:
        NodeConfigIntent("pod-1", {"emosa": {"start": "x"}}).validate()
    assert raised.value.code == Reason.INVALID_INPUT


class Session:
    def __init__(self, results):
        self.results, self.sent = results, []

    async def transact(self, ops):
        self.sent.append(ops)
        return self.results


def source(session):
    report = object.__new__(PodReportSource)
    report.backend = types.SimpleNamespace(
        profile=types.SimpleNamespace(legacy=True), session=session
    )
    report.session_starts, report.start_token, report.start_writes = 1, None, 0
    report.start_tried = report.start_pending_since = report.start_error = None
    return report


def raw(node_config, generation=1, tables=("Node_Config", "Wifi_Radio_Config")):
    schema = types.SimpleNamespace(db=types.SimpleNamespace(tables=dict.fromkeys(tables)))
    return {
        "ready": True,
        "schema": schema,
        "generation": generation,
        "tables": {"Wifi_Radio_Config": RADIOS, "Node_Config": node_config},
    }


def test_an_agent_reads_the_pods_marker_back_and_counts_a_new_one_as_a_new_start(marked):
    session = Session([{}, {"uuid": ["uuid", "u"]}])
    report = source(session)
    asyncio.run(report.mark_start(raw(marker("0123456789abcdef"))))
    # an EMOSA restart's first read: its own start, not a new one, nothing written
    assert report.start_token == "0123456789abcdef" and report.session_starts == 1
    asyncio.run(report.mark_start(raw(marker("0123456789abcdef"))))
    assert report.session_starts == 1 and not session.sent
    asyncio.run(report.mark_start(raw(marker("fedcba9876543210"), generation=2)))
    assert report.session_starts == 2 and report.start_token == "fedcba9876543210"


def test_an_agent_writes_a_marker_where_the_pod_has_none_once_per_retry(marked, monkeypatch):
    session = Session([{}, {"uuid": ["uuid", "u"]}])
    report = source(session)
    clock = [100.0]
    monkeypatch.setattr("emosa.agent.pod.time.monotonic", lambda: clock[0])
    asyncio.run(report.mark_start(raw({})))
    assert len(session.sent) == 1 and report.start_writes == 1
    (wait, insert) = session.sent[0]
    assert wait["op"] == "wait" and len(insert["row"]["value"]) == 16
    clock[0] += 1  # its echo not in yet: not written again so soon
    asyncio.run(report.mark_start(raw({})))
    assert len(session.sent) == 1
    asyncio.run(report.mark_start(raw({}, generation=2)))  # another database: at once
    assert len(session.sent) == 2


def test_a_marker_not_taken_is_logged_and_tried_again_later(marked, monkeypatch, caplog):
    session = Session([{}, {"error": "constraint violation", "details": "too many rows"}])
    report = source(session)
    clock = [100.0]
    monkeypatch.setattr("emosa.agent.pod.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("emosa.agent.pod.time.time", lambda: 1_760_000_000.0 + clock[0])
    asyncio.run(report.mark_start(raw({})))
    assert report.start_writes == 0 and "start marker not written" in caplog.text
    # the status says why, and since when the pod has been unmarked (not ready)
    assert report.start_error == "INVALID_INPUT: constraint violation"
    assert report.start_pending_since == 1_760_000_100.0
    clock[0] += 6
    asyncio.run(report.mark_start(raw({})))
    assert len(session.sent) == 2 and report.start_pending_since == 1_760_000_100.0
    # taken at last: the marker read back, the pod no longer pending and its last error cleared
    asyncio.run(report.mark_start(raw(marker("0123456789abcdef"))))
    assert report.start_pending_since is None and report.start_error is None


def test_a_marker_refused_at_commit_is_a_refusal_with_the_databases_error(marked, monkeypatch):
    # RFC 7047: a refusal at commit (a constraint violation) has one result more than the
    # operations: nothing written, as definite as an operation's error; its details (which can
    # quote a value written) are not kept
    session = Session(
        [{}, {"uuid": ["uuid", "u"]}, {"error": "constraint violation", "details": "secret"}]
    )
    report = source(session)
    monkeypatch.setattr("emosa.agent.pod.time.monotonic", lambda: 100.0)
    asyncio.run(report.mark_start(raw({})))
    assert report.start_writes == 0
    assert report.start_error == "INVALID_INPUT: constraint violation"


def test_a_marker_writes_failure_is_its_reason_and_the_databases_error():
    assert start_marker_error(Reason.PRECONDITION_FAILED, [{"error": "timed out"}, {}]) == (
        "PRECONDITION_FAILED: timed out"
    )
    assert start_marker_error(Reason.OUTCOME_UNKNOWN) == "OUTCOME_UNKNOWN"
    assert start_marker_error(Reason.INVALID_INPUT, [{}, {"error": "x" * 100}]) == (
        "INVALID_INPUT: " + "x" * 64
    )


def test_a_profiles_mapping_is_its_id_and_its_files_content(tmp_path):
    # spec §5: an image whose profile changed plans under another mapping, so what the earlier one
    # wrote is no ownership conflict across an EMOSA restart on the same start (the box's
    # legacy-parent-unhide)
    name = "bcm-pod-2.4.3-gre-v1"
    bundled = files("emosa").joinpath(f"profiles/{name}.json").read_bytes()
    profile = load_profile(name)
    assert profile.mapping == f"{name}@{hashlib.sha256(bundled).hexdigest()[:12]}"
    changed = json.loads(bundled)
    changed["gre_parent"]["vif"] = {}
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(changed, indent=2) + "\n")
    other = load_profile(str(path))
    assert other.id == profile.id and other.mapping != profile.mapping
    assert other.mapping.startswith(f"{name}@") and len(other.mapping) == len(name) + 13


def test_a_legacy_schema_without_node_config_counts_its_starts_by_sessions(marked):
    report = source(Session([]))
    asyncio.run(report.mark_start(raw({}, tables=("Wifi_Radio_Config",))))
    assert not uplink.start_marker_used() and not report.backend.session.sent
