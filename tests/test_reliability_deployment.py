import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def driver(monkeypatch, tmp_path, instances, inner=(), action="cleanup"):
    path = Path(__file__).resolve().parents[1] / "deploy/reliability/lab.py"
    spec = importlib.util.spec_from_file_location("reliability_lab", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def command(*args, capture=False):
        calls.append(args)
        if args == ("lxc", "list", "--format=json"):
            return json.dumps(instances)
        if args[-2:] == ("list", "--format=json"):
            return json.dumps(inner)
        raise AssertionError("Unexpected mutation: " + str(args))

    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(sys, "argv", [str(path), action, "--directory", str(tmp_path / "output")])
    return module, calls


def test_cleanup_refuses_unowned_outer_instance(monkeypatch, tmp_path):
    module, calls = driver(monkeypatch, tmp_path, [{"name": "emosa-reliability", "config": {}}])
    with pytest.raises(SystemExit, match="ownership marker"):
        module.main()
    assert len(calls) == 1


@pytest.mark.parametrize("container", ["emosa-runtime-build", "emosa-runtime-run"])
def test_cleanup_refuses_unowned_inner_instance(monkeypatch, tmp_path, container):
    module, calls = driver(
        monkeypatch,
        tmp_path,
        [
            {
                "name": "emosa-reliability",
                "status": "Running",
                "config": {"user.emosa-purpose": "secure-fleet-reproduction"},
            }
        ],
        [{"name": container, "config": {}}],
    )
    with pytest.raises(SystemExit, match="unowned inner"):
        module.main()
    assert len(calls) == 2


def test_create_refuses_to_replace_owned_vm(monkeypatch, tmp_path):
    module, calls = driver(
        monkeypatch,
        tmp_path,
        [
            {
                "name": "emosa-reliability",
                "config": {"user.emosa-purpose": "secure-fleet-reproduction"},
            }
        ],
        action="create",
    )
    with pytest.raises(SystemExit, match="replace existing"):
        module.main()
    assert len(calls) == 1
