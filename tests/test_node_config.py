# SPDX-License-Identifier: Apache-2.0
"""The pod's modules' settings (spec 4): the node-config scope's write, the modules' reports,
and the fleet's settings and file server."""

import asyncio
import copy
import hashlib
import logging
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime

import pytest

from emosa.agent.fleet import FileServer, Fleet, agent_config, module_settings
from emosa.agent.node_config import RESTART_GRACE, NodeConfig, events
from emosa.config import validate
from emosa.errors import EmosaError, Reason
from emosa.model import State
from emosa.opensync.node_config import MONITOR, NodeConfigBackend, NodeConfigIntent
from emosa.opensync.schema import Schema, reference_schema
from emosa.secrets import SecretStore
from emosa.store import Store

pytestmark = pytest.mark.unit

SERIAL = "PODNODECONFIG0001"
NODE = "00000000-0000-4000-8000-0000000007a1"
RADIO = "00000000-0000-4000-8000-0000000007a2"
SHA = "a" * 64
SETTINGS = {
    "hook": {
        "redirector": "tcp:192.0.2.1:6640",
        "update_url": f"http://192.0.2.1:6650/{SHA}",
        "update_sha256": SHA,
    }
}


class Pod:
    """The pod's database as the node-config scope sees it (the 6.6 reference schema): the
    agent's transactions applied to it, as ovsdb-server would."""

    def __init__(self, config=None, state=None):
        self.schema = Schema(reference_schema())
        self.sent, self.next = [], 0
        self.tables = {
            "AWLAN_Node": {NODE: {"serial_number": SERIAL}},
            "Wifi_Radio_Config": {RADIO: {"if_name": "phy0", "freq_band": "2.4G"}},
            "Node_Config": dict(config or {}),
            "Node_State": dict(state or {}),
        }

    def uuid(self):
        self.next += 1
        return f"00000000-0000-4000-8000-0000000008{self.next:02d}"

    async def snapshot(self):
        tables = copy.deepcopy(self.tables)
        for table, columns in MONITOR.items():
            tables[table] = {
                u: {k: v for k, v in r.items() if k in columns} for u, r in tables[table].items()
            }
        return {
            "tables": tables,
            "generation": 1,
            "revision": 1,
            "ready": True,
            "schema": self.schema,
        }

    def _rows(self, op):
        rows = self.tables[op["table"]]
        found = {}
        for uuid, row in rows.items():
            if all(
                (uuid == value[1]) if column == "_uuid" else row.get(column) == value
                for column, _, value in op["where"]
            ):
                found[uuid] = row
        return found

    async def transact(self, operations, transaction_id=None, generation=None, **_):
        self.sent.append(operations)
        results = []
        for op in operations:
            if op["op"] == "wait":
                rows = [{c: r.get(c) for c in op["columns"]} for r in self._rows(op).values()]
                if rows != op["rows"]:
                    return results + [{"error": "timed out"}]
                results.append({})
            elif op["op"] == "insert":
                self.tables[op["table"]][self.uuid()] = dict(op["row"])
                results.append({"uuid": ["uuid", "x"]})
            elif op["op"] == "update":
                found = self._rows(op)
                for row in found.values():
                    row.update(op["row"])
                results.append({"count": len(found)})
            elif op["op"] == "delete":
                found = self._rows(op)
                for uuid in found:
                    del self.tables[op["table"]][uuid]
                results.append({"count": len(found)})
        return results


def row(module, key, value):
    return {"module": module, "key": key, "value": value, "persist": True}


def scope(tmp_path, pod, modules=None, monotonic=None):
    intent = NodeConfigIntent("pod-1", modules or SETTINGS)
    backend = NodeConfigBackend("pod-1", pod, serial=SERIAL, intent=intent)
    return NodeConfig(
        "pod-1",
        backend,
        Store(tmp_path / "node-config"),
        SecretStore(tmp_path / "secrets"),
        run_id="t",
        monotonic=monotonic,
    )


def test_an_intent_is_refused_unless_its_names_and_values_fit_the_tables():
    for bad in (
        {},
        {"Hook": {"a": "b"}},  # module names are lowercase
        {"hook": {}},
        {"hook": {"a-b": "c"}},  # a key is [a-z0-9_]
        {"hook": {"a": ""}},  # never an empty value
        {"hook": {"a": "x" * 1025}},
        {f"m{n}": {"a": "b"} for n in range(5)},
    ):
        with pytest.raises(EmosaError) as refused:
            NodeConfigIntent("pod-1", bad).validate()
        assert refused.value.code == Reason.INVALID_INPUT
    NodeConfigIntent("pod-1", SETTINGS).validate()


def test_the_settings_are_written_in_one_transaction_and_another_modules_rows_kept(tmp_path):
    pod = Pod(
        config={
            "00000000-0000-4000-8000-000000000701": row("hook", "redirector", "auto"),
            "00000000-0000-4000-8000-000000000702": row("hook", "stale", "x"),  # not ours now
            "00000000-0000-4000-8000-000000000703": row("hook", "redirector", "tcp:1:2"),  # twice
            "00000000-0000-4000-8000-000000000704": row("vendor", "level", "debug"),
        }
    )
    agent = scope(tmp_path, pod)
    asyncio.run(agent.tick())
    (ops,) = pod.sent
    assert [o["op"] for o in ops] == [
        "wait",  # the serial
        "wait",
        "delete",  # hook/redirector's second row
        "wait",
        "delete",  # hook/stale
        "wait",
        "update",  # hook/redirector
        "wait",
        "insert",  # hook/update_sha256
        "wait",
        "insert",  # hook/update_url
    ]
    config = {
        (r["module"], r["key"]): (r["value"], r["persist"])
        for r in pod.tables["Node_Config"].values()
    }
    assert config == {
        ("hook", "redirector"): ("tcp:192.0.2.1:6640", True),
        ("hook", "update_url"): (f"http://192.0.2.1:6650/{SHA}", True),
        ("hook", "update_sha256"): (SHA, True),
        ("vendor", "level"): ("debug", True),  # another module's: never touched
    }
    asyncio.run(agent.tick())
    status = agent.status()
    assert status["operation"]["state"] == State.OBSERVED_APPLIED
    assert status["modules"]["hook"]["settings_written"] is True and status["differs"] == []
    asyncio.run(agent.tick())
    assert len(pod.sent) == 1  # once per start


def test_a_row_another_writer_added_meanwhile_is_a_conflict(tmp_path):
    pod = Pod()
    backend = NodeConfigBackend(
        "pod-1", pod, serial=SERIAL, intent=NodeConfigIntent("pod-1", SETTINGS)
    )
    decoded, node = backend._binding(asyncio.run(pod.snapshot()))
    operations, counts = backend.transaction(backend.intent, decoded, node)
    pod.tables["Node_Config"]["00000000-0000-4000-8000-000000000799"] = row(
        "hook", "redirector", "auto"
    )
    results = asyncio.run(pod.transact(operations))
    assert results[-1] == {"error": "timed out"}  # the insert's guard: nothing was written


def test_events_are_read_in_order_and_unnumbered_ones_left_out():
    assert events("1 apply;2 emosa:connected; junk;3 update:ok") == [
        (1, "apply"),
        (2, "emosa:connected"),
        (3, "update:ok"),
    ]
    assert events(None) == []


def report(**state):
    return {
        f"00000000-0000-4000-8000-0000000009{n:02d}": {"module": "hook", "key": k, "value": v}
        for n, (k, v) in enumerate(state.items())
    }


def test_each_event_is_logged_once_per_boot(tmp_path, caplog):
    pod = Pod(state=report(boot_id="boot-1", events="1 apply;2 emosa"))
    agent = scope(tmp_path, pod)
    with caplog.at_level(logging.INFO, logger="emosa.agent.node_config"):
        asyncio.run(agent.tick())
        asyncio.run(agent.tick())
        pod.tables["Node_State"] = report(boot_id="boot-1", events="1 apply;2 emosa;3 update:ok")
        asyncio.run(agent.tick())
        pod.tables["Node_State"] = report(boot_id="boot-2", events="1 apply")
        asyncio.run(agent.tick())
    logged = [r.getMessage() for r in caplog.records if "event" in r.getMessage()]
    assert logged == [
        "module hook: event 1 apply",
        "module hook: event 2 emosa",
        "module hook: event 3 update:ok",
        "module hook: event 1 apply",  # its next boot's
    ]
    assert any("a new boot of the pod (boot-2)" in r.getMessage() for r in caplog.records)


def test_the_status_reads_the_modules_report_and_flags_what_it_means(tmp_path):
    now = [100.0]
    pod = Pod(
        state=report(
            state="error:conf",
            boot_id="boot-1",
            version="b" * 64,
            running="c" * 64,
            slot="none",
            redirector="tcp:192.0.2.1:6640",
            update="ok",
            update_sha256=SHA,
        )
    )
    agent = scope(tmp_path, pod, monotonic=lambda: now[0])
    asyncio.run(agent.tick())
    hook = agent.status()["modules"]["hook"]
    assert hook["settings_echoed"] == ["redirector", "update_sha256"]
    assert hook["state"] == "error:conf" and hook["slot"] == "none"
    assert hook["flags"] == ["state_error", "not_started_at_boot"]  # within the grace
    now[0] += RESTART_GRACE + 1
    asyncio.run(agent.tick())
    assert "restart_not_taken" in agent.status()["modules"]["hook"]["flags"]
    validate("agent-status", {"node_config": agent.status()} | REQUIRED_STATUS)
    for key, value in (("running", "b" * 64), ("update", "failed:sha256")):
        pod.tables["Node_State"] = report(
            **{
                "version": "b" * 64,
                "running": "c" * 64,
                "update": "ok",
                "update_sha256": SHA,
                key: value,
            }
        )
        asyncio.run(agent.tick())
    assert agent.status()["modules"]["hook"]["flags"] == ["update_failed"]
    pod.tables["Node_State"] = report(update="ok", update_sha256="d" * 64)  # an older request
    asyncio.run(agent.tick())
    assert agent.status()["modules"]["hook"]["flags"] == ["update_pending"]


REQUIRED_STATUS = {
    "pod_id": "pod-1",
    "profile": "p",
    "agent_al": "02:00:00:00:00:01",
    "controller_al": "02:00:00:00:00:02",
    "pod": None,
    "report_source_available": False,
    "session": None,
    "operations": [],
    "writes": 0,
    "worker_pid": 1,
    "updated": 0.0,
}


# -- the fleet -----------------------------------------------------------------------------


def fleet_config(tmp_path, **extra):
    return {
        "listen": "ptcp:6640:127.0.0.1",
        "advertise": "192.0.2.1",
        "ports": [6651, 6660],
        "controller_al": "02:00:00:00:00:02",
        "state_root": str(tmp_path / "state"),
        "config_dir": str(tmp_path / "config"),
        **extra,
    }


def test_the_fleet_gives_each_agent_its_modules_settings_with_the_update_url(tmp_path):
    fleet = fleet_config(
        tmp_path,
        files={"directory": str(tmp_path / "files")},
        modules={"hook": {"config": {"redirector": "auto"}}},
        pods={SERIAL: {"modules": {"hook": {"config": {"redirector": "auto"}, "update": SHA}}}},
    )
    validate("fleet-config", fleet)
    entry = {"pod_id": SERIAL, "port": 6651, "interface": "em1", "al_mac": "02:00:00:00:00:03"}
    config = validate("agent-config", agent_config(entry, fleet))
    assert config["modules"] == {
        "hook": {
            "redirector": "auto",
            "update_url": f"http://192.0.2.1:6650/{SHA}",
            "update_sha256": SHA,
        }
    }
    other = agent_config({**entry, "pod_id": "OTHER"}, fleet)
    assert other["modules"] == {"hook": {"redirector": "auto"}}  # the fleet's for every pod
    assert "modules" not in agent_config(entry, fleet_config(tmp_path))  # unchanged without
    assert module_settings({"hook": {}}, fleet) == {}


def test_a_modules_update_needs_the_fleets_files(tmp_path):
    config = fleet_config(tmp_path, modules={"hook": {"update": SHA}})
    with pytest.raises(EmosaError) as refused:
        Fleet(config)
    assert refused.value.code == Reason.INVALID_INPUT
    with pytest.raises(EmosaError):  # the fleet's own names are not a module's settings
        validate(
            "fleet-config",
            fleet_config(tmp_path, modules={"hook": {"config": {"update_url": "x"}}}),
        )


def test_the_file_server_serves_only_files_named_by_their_sha256_with_a_date(tmp_path, caplog):
    files = tmp_path / "files"
    files.mkdir()
    content = b"#!/bin/sh\necho hook\n"
    good = hashlib.sha256(content).hexdigest()
    (files / good).write_bytes(content)
    (files / ("0" * 64)).write_bytes(b"not its name")
    (files / "hook.sh").write_bytes(content)
    server = FileServer(files, "127.0.0.1", 0)
    with caplog.at_level(logging.INFO, logger="emosa.fleet"):
        server.start()
        base = f"http://127.0.0.1:{server.address[1]}"
        try:
            with urllib.request.urlopen(urllib.request.Request(base + "/", method="HEAD")) as r:
                assert r.status == 200 and r.read() == b""
                date = r.headers["Date"]
            assert date.endswith(" GMT") and parsedate_to_datetime(date).year >= 2026
            with urllib.request.urlopen(f"{base}/{good}") as r:
                assert r.read() == content
            with urllib.request.urlopen(
                urllib.request.Request(f"{base}/{good}", method="HEAD")
            ) as r:
                assert r.headers["Content-Length"] == str(len(content)) and r.read() == b""
            for path in ("0" * 64, "hook.sh", "b" * 64, f"../files/{good}"):
                with pytest.raises(urllib.error.HTTPError) as missing:
                    urllib.request.urlopen(f"{base}/{path}")
                assert missing.value.code == 404 and missing.value.headers["Date"]
        finally:
            server.stop()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert f"files: 127.0.0.1 GET /{good}: 200" in text
    assert "its content is not its name" in text and "files: 1 served" in text


def test_without_a_synchronized_clock_no_time_is_served(tmp_path, caplog):
    # spec 4, the pods' time: a server without a reliable clock sends no Date (RFC 7231); the
    # pods keep their own saved clock (the router's bench never syncs, 14 October)
    files = tmp_path / "files"
    files.mkdir()
    content = b"#!/bin/sh\n"
    sha = hashlib.sha256(content).hexdigest()
    (files / sha).write_bytes(content)
    trusted = [False]
    server = FileServer(files, "127.0.0.1", 0, lambda: trusted[0])
    with caplog.at_level(logging.WARNING, logger="emosa.fleet"):
        server.start()
        base = f"http://127.0.0.1:{server.address[1]}"
        try:
            with pytest.raises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(urllib.request.Request(base + "/", method="HEAD"))
            assert refused.value.code == 503 and refused.value.headers["Retry-After"] == "60"
            assert refused.value.headers["Date"] is None
            with urllib.request.urlopen(f"{base}/{sha}") as r:  # updates keep working
                assert r.read() == content and r.headers["Date"] is None
            trusted[0], server.checked = True, None  # synchronized, checked again
            with urllib.request.urlopen(urllib.request.Request(base + "/", method="HEAD")) as r:
                assert r.status == 200 and r.headers["Date"].endswith(" GMT")
        finally:
            server.stop()
    text = caplog.text
    assert "not synchronized: no time served" in text and "synchronized: the time served" in text


def test_the_clock_check_takes_the_command_when_the_fleet_names_one():
    from emosa.agent.fleet import clock_synchronized

    assert clock_synchronized({"time_synced_command": "true"}, environ={}) is True
    assert clock_synchronized({"time_synced_command": "false"}, environ={}) is False
    forced = {"EMOSA_FILES_CLOCK": "unsynchronized"}
    assert clock_synchronized({"time_synced_command": "true"}, environ=forced) is False
