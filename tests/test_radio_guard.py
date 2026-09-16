"""Safety boundaries, without creating radios, namespaces or LXD resources."""

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def lab():
    spec = importlib.util.spec_from_file_location("radio_lab", Path("deploy/hwsim/lab.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("action", ["setup", "smoke", "cleanup"])
def test_mutations_refuse_non_dedicated_vm(lab, monkeypatch, action):
    monkeypatch.setattr(lab, "preflight", lambda: {"dedicated_vm": False})
    monkeypatch.setattr(lab, "command", lambda *a, **kw: pytest.fail("Command after failed guard"))
    with pytest.raises(RuntimeError, match="dedicated emosa-lab"):
        getattr(lab, action)()


def test_cleanup_checks_ownership_before_deletion(lab, monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    state.write_text('{"owner":"expected","containers":["em-radio-ap"]}')
    monkeypatch.setattr(lab, "STATE", state)
    monkeypatch.setattr(lab, "require_vm", lambda: None)
    calls = []

    def fake_lxc(*args):
        calls.append(args)
        if args[0] != "config":
            pytest.fail("Mutation before ownership was checked")
        return type("Result", (), {"stdout": "different-owner\n"})()

    monkeypatch.setattr(lab, "lxc", fake_lxc)
    with pytest.raises(RuntimeError, match="Ownership mismatch"):
        lab.cleanup()
    assert len(calls) == 1
