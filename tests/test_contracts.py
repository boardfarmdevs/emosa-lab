"""The adapter's external contracts: schemas, the spec examples, and what the code writes."""

import json
from pathlib import Path

import pytest

from emosa.agent.fleet import agent_config
from emosa.config import validate
from emosa.errors import EmosaError
from test_fleet import Pod, make_fleet

pytestmark = pytest.mark.unit

EXAMPLES = Path(__file__).resolve().parents[1] / "spec" / "examples"


@pytest.mark.parametrize(
    "schema", ["fleet-config", "agent-config", "fleet-registry", "agent-status"]
)
def test_the_spec_examples_are_valid(schema):
    validate(schema, json.loads((EXAMPLES / f"{schema}.json").read_text()))


def test_what_the_fleet_writes_meets_the_contracts(tmp_path):
    started = []
    fleet = make_fleet(tmp_path, started, message_set="r1", multi_bss=True, m2_session="shared")
    for serial in ("POD1", "POD2"):
        fleet._call = Pod(serial)
        fleet.handle(None, "peer")
    validate("fleet-registry", fleet.registry.agents)
    for entry in fleet.registry.agents.values():
        validate("agent-config", agent_config(entry, fleet.config))
        written = json.loads((tmp_path / "etc" / f"{entry['pod_id']}.json").read_text())
        validate("agent-config", written)


@pytest.mark.parametrize(
    "change",
    [
        {"message_set": "easymesh-5.0"},
        {"ports": [6651]},
        {"controller_al": "02:00:00:E0:00:01"},
        {"admit": "all"},
        {"unknown_key": 1},
    ],
)
def test_an_invalid_fleet_configuration_is_refused(tmp_path, change):
    with pytest.raises(EmosaError):
        make_fleet(tmp_path, [], **change)


def test_the_fleet_passes_every_agent_setting_its_agents_accept():
    # The fleet hands its settings to every agent: a key the agent contract
    # gained but the fleet contract did not makes the fleet refuse its own
    # configuration (the telemetry publish interval and survey did).
    root = Path(__file__).resolve().parents[1] / "schemas"
    fleet = json.loads((root / "fleet-config.schema.json").read_text())["properties"]
    agent = json.loads((root / "agent-config.schema.json").read_text())["properties"]
    for key in ("uplink", "telemetry"):
        assert fleet[key]["properties"] == agent[key]["properties"], key


def test_a_fleet_with_the_rdk_labs_telemetry_is_valid(tmp_path):
    telemetry = {
        "mode": "mqtt",
        "broker": "10.101.0.40",
        "port": 8883,
        "reporting_interval": 5,
        "sampling_interval": 5,
        "publish_interval": 5,
        "survey": True,
    }
    make_fleet(tmp_path, [], telemetry=telemetry)
