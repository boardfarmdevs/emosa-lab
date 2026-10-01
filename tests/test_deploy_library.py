"""The labs' VM scripts and the EMOSA steps they share (deploy/lib/emosa-vm.sh): both
scripts parse and source the library, and the fleet and telemetry configuration it
writes is what each lab asks for (lxc mocked: no VM)."""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "deploy/lib/emosa-vm.sh"
SCRIPTS = [ROOT / "deploy/opensync-lab/vm/lab.sh", ROOT / "deploy/rdk-lab/vm/lab.sh"]

# A stand-in for lxc: every container exists; what a step writes to a file in a
# container (sh -c "cat > FILE") lands in $CAPTURE/<basename>; everything else succeeds.
FAKE_LXC = """#!/bin/bash
for a in "$@"; do
    case $a in "cat > "*) f=${a#cat > }; cat > "$CAPTURE/$(basename "$f")"; exit 0 ;; esac
done
exit 0
"""


def run(tmp_path, script, state=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "lxc").write_text(FAKE_LXC)
    (bin_dir / "lxc").chmod(0o755)
    capture = tmp_path / "capture"
    capture.mkdir(exist_ok=True)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", CAPTURE=str(capture),
               STATE=str(state or tmp_path / "state"))
    for name in ("EMOSA_MESSAGE_SET", "EMOSA_MULTI_BSS", "EMOSA_M2_SESSION", "EMOSA_POD_PROFILE"):
        env.pop(name, None)
    prologue = f"set -euo pipefail; LOG_TAG=test IMAGE=ubuntu:24.04 WAN_HOST=10.101.0.40; source {LIBRARY}\n"
    result = subprocess.run(["bash", "-c", prologue + script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout, capture


def test_scripts_parse_and_source_the_library():
    for path in [LIBRARY, *SCRIPTS]:
        subprocess.run(["bash", "-n", str(path)], check=True)
    for path in SCRIPTS:
        text = path.read_text()
        assert "lib/emosa-vm.sh" in text
        for shared in ("log() {", "die() {", "cx() {", "provision() {", "telemetry_json() {"):
            assert shared not in text, f"{path.name} defines {shared} again"


def test_rdk_fleet(tmp_path):
    _, capture = run(tmp_path, 'fleet_config 6640 6651 6690 02:00:00:00:00:01 r1 true shared '
                               '\'{"SERIAL": {"uplink": {"mode": "multi-ap"}}}\'')
    fleet = json.loads((capture / "emosa-fleet.json").read_text())
    assert fleet["listen"] == "ptcp:6640:127.0.0.1"
    assert fleet["advertise"] == "10.101.0.40"
    assert fleet["ports"] == [6651, 6690]
    assert (fleet["message_set"], fleet["multi_bss"], fleet["m2_session"]) == ("r1", True, "shared")
    assert fleet["telemetry"] == {"mode": "off"}
    assert fleet["pods"] == {"SERIAL": {"uplink": {"mode": "multi-ap"}}}


def test_opensync_lab_fleet_without_pods(tmp_path):
    _, capture = run(tmp_path, "fleet_config 6650 6651 6690 02:00:00:e0:00:01 easymesh-6.1 false distinct")
    fleet = json.loads((capture / "emosa-fleet.json").read_text())
    assert (fleet["message_set"], fleet["multi_bss"], fleet["m2_session"]) == ("easymesh-6.1", False, "distinct")
    assert "pods" not in fleet
    assert fleet["profile"] == "opensync-lab-hwsim-6.6.1-v1"


def test_environment_overrides_a_labs_defaults(tmp_path):
    _, capture = run(tmp_path, "EMOSA_MULTI_BSS=false EMOSA_MESSAGE_SET=easymesh-6.1 "
                               "fleet_config 6640 6651 6690 02:00:00:00:00:01 r1 true shared")
    fleet = json.loads((capture / "emosa-fleet.json").read_text())
    assert (fleet["message_set"], fleet["multi_bss"]) == ("easymesh-6.1", False)


def test_telemetry_setting(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    out, _ = run(tmp_path, "telemetry_json", state)
    assert json.loads(out) == {"mode": "off"}
    (state / "telemetry").touch()
    out, _ = run(tmp_path, "telemetry_json", state)
    assert json.loads(out) == {"mode": "mqtt", "broker": "10.101.0.40", "port": 8883}
    out, _ = run(tmp_path, "TELEMETRY_OPTIONS='\"reporting_interval\": 5, \"survey\": true' telemetry_json", state)
    assert json.loads(out) == {"mode": "mqtt", "broker": "10.101.0.40", "port": 8883,
                               "reporting_interval": 5, "survey": True}


def test_agent_binaries(tmp_path):
    out, _ = run(tmp_path, "agent_binary python; agent_binary c; agent_binary rust || echo none")
    assert out.split() == ["/opt/emosa-adapter/venv/bin/emosa-agent", "/opt/emosa-adapter/bin/emosa-agent-c", "none"]
