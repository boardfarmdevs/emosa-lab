import copy
import json

import pytest

from emosa.agents import AgentDirectory
from emosa.config import load, validate
from emosa.errors import EmosaError
from emosa_lab.simulation.connecting_pod import configuration

pytestmark = pytest.mark.unit


def test_agent_directory_expires_and_retains_identity(monkeypatch, tmp_path):
    config = configuration(tmp_path, "unix:/private/simulation.sock")
    clock = [100.0]
    monkeypatch.setattr("emosa.agents.time.monotonic", lambda: clock[0])
    directory = AgentDirectory(config["pods"])
    assert directory.view()["agents"][0]["state"] == "pending"
    inventory = {"ready": True, "generation": 1, "bsses": [{"ssid": "old"}]}
    directory.observe("pod-1", inventory)
    inventory["bsses"][0]["ssid"] = "mutated"
    first = directory.view()
    assert first["agents"][0]["state"] == "ready"
    assert first["agents"][0]["inventory"]["bsses"][0]["ssid"] == "old"
    clock[0] += 2.01
    stale = directory.view()["agents"][0]
    assert stale["state"] == "unavailable" and not stale["inventory"]["ready"]
    assert stale["al_mac"] == first["agents"][0]["al_mac"]
    directory.observe("pod-1", {**inventory, "generation": 2})
    assert directory.view()["agents"][0]["fresh"]
    directory.invalidate("pod-1")
    assert not directory.view()["agents"][0]["fresh"]
    assert not first["controller_onboarding_proven"]
    assert first["easymesh_wire_state"] == "blocked_P0"
    assert directory.view(offset=1)["agents"] == []
    assert directory.view(offset=1)["total"] == 1


@pytest.mark.parametrize("field", ["al_mac", "expected_serial"])
def test_duplicate_virtual_agent_binding_rejected(tmp_path, field):
    config = configuration(tmp_path, "unix:/private/one.sock")
    other = copy.deepcopy(config["pods"][0])
    other.update(pod_id="pod-2", endpoint="punix:/private/two.sock")
    other["virtual_agent"] = {"al_mac": "02:00:00:00:30:02", "expected_serial": "second"}
    other["virtual_agent"][field] = config["pods"][0]["virtual_agent"][field]
    config["pods"].append(other)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(EmosaError, match="duplicate virtual agent"):
        load("config", path)


@pytest.mark.parametrize("address", ["03:00:00:00:00:01", "00:00:00:00:00:01", "not-a-mac"])
def test_virtual_agent_requires_explicit_lab_unicast_address(tmp_path, address):
    config = configuration(tmp_path, "unix:/private/one.sock")
    config["pods"][0]["virtual_agent"]["al_mac"] = address
    with pytest.raises(EmosaError):
        validate("config", config)


def test_model_cannot_claim_observed_agent(tmp_path):
    config = configuration(tmp_path, "unix:/private/one.sock")
    config["backend_mode"] = "model"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(EmosaError, match="requires ovsdb-sim"):
        load("config", path)


def test_agents_local_request_contract():
    validate(
        "local-api",
        {"schema_version": 1, "request_id": "one", "method": "agents", "params": {"limit": 1}},
    )


@pytest.mark.parametrize("fault", ["no-trust", "no-binding", "remote-write", "duplicate-pin"])
def test_tls_service_requires_local_unique_explicit_trust(tmp_path, fault):
    from emosa_lab.simulation.tls import create_pki, trust_config

    pki = create_pki(tmp_path / "secrets", 1)
    config = configuration(tmp_path, "unix:/private/one.sock")
    pod = config["pods"][0]
    pod.update(endpoint="pssl:6640:127.0.0.1", tls=trust_config(pki, 0))
    if fault == "no-trust":
        pod.pop("tls")
    elif fault == "no-binding":
        pod.pop("virtual_agent")
    elif fault == "remote-write":
        pod["endpoint"] = "pssl:6640:192.0.2.1"
    else:
        other = copy.deepcopy(pod)
        other.update(pod_id="pod-2", endpoint="pssl:6641:127.0.0.1")
        other["virtual_agent"] = {"al_mac": "02:00:00:00:40:02", "expected_serial": "second"}
        config["pods"].append(other)
    path = tmp_path / "adapter.json"
    path.write_text(json.dumps(config))
    with pytest.raises(EmosaError):
        load("config", path)


@pytest.mark.parametrize("fault", ["missing-binding", "model-mode"])
def test_sole_radio_scope_requires_bound_ovsdb_simulation(tmp_path, fault):
    config = configuration(tmp_path, "unix:/private/one.sock")
    config["pods"][0]["mapping_scope"] = "sole-fronthaul-radio"
    if fault == "missing-binding":
        config["pods"][0].pop("virtual_agent")
    else:
        config["backend_mode"] = "model"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(EmosaError, match="sole-radio scope requires"):
        load("config", path)
