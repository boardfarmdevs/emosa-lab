# SPDX-License-Identifier: Apache-2.0
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
# container (sh -c "cat > FILE", or "cat > 'FILE.new' && mv ...") lands in
# $CAPTURE/<basename>, as does a file pushed (lxc file push ... SOURCE CT/PATH); a
# container's places (emosa_layout) are read from $FAKE_DEFAULT for its
# /etc/default/emosa (none: the kit's defaults); its LAN bridge has 10.0.0.1; no container
# is running; everything else succeeds.
FAKE_LXC = r"""#!/bin/bash
if [ "$1 $2" = "file push" ]; then
    cp "${@: -2:1}" "$CAPTURE/$(basename "${@: -1}")"; exit 0
fi
for a in "$@"; do
    case $a in
        *EMOSA_FLEET_CONFIG*)
            exec sh -c "${a//\/etc\/default\/emosa/${FAKE_DEFAULT:-/nonexistent}}" ;;
        "cat > '"*)
            f=${a#"cat > '"}; f=${f%%"'"*}
            cat > "$CAPTURE/$(basename "${f%.new}")"; exit 0 ;;
        "cat > "*) f=${a#cat > }; cat > "$CAPTURE/$(basename "$f")"; exit 0 ;;
    esac
done
case " $* " in
    *" ip -4 -o addr show "*) echo "5: brlan0    inet 10.0.0.1/24 scope global brlan0" ;;
    *" storage show bpi-lab "*)
        [ -n "${FAKE_BPI_LAB:-}" ] || exit 1
        printf 'name: bpi-lab\ndriver: %s\n' "$FAKE_BPI_LAB" ;;
    *" profile device get default root pool "*) echo default ;;
esac
exit 0
"""


def run(tmp_path, script, state=None, default=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "lxc").write_text(FAKE_LXC)
    (bin_dir / "lxc").chmod(0o755)
    capture = tmp_path / "capture"
    capture.mkdir(exist_ok=True)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        CAPTURE=str(capture),
        STATE=str(state or tmp_path / "state"),
    )
    if default:
        env["FAKE_DEFAULT"] = str(default)
    for name in ("EMOSA_MESSAGE_SET", "EMOSA_MULTI_BSS", "EMOSA_M2_SESSION", "EMOSA_POD_PROFILE"):
        env.pop(name, None)
    prologue = (
        "set -euo pipefail; LOG_TAG=test IMAGE=ubuntu:24.04 WAN_HOST=10.101.0.40; "
        f"source {LIBRARY}\n"
    )
    result = subprocess.run(
        ["bash", "-c", prologue + script], env=env, capture_output=True, text=True
    )
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
    _, capture = run(
        tmp_path,
        "fleet_config 6640 6651 6690 02:00:00:00:00:01 r1 true shared "
        '\'{"SERIAL": {"uplink": {"mode": "multi-ap"}}}\'',
    )
    fleet = json.loads((capture / "emosa-fleet.json").read_text())
    assert fleet["listen"] == "ptcp:6640:127.0.0.1"
    assert fleet["advertise"] == "10.101.0.40"
    assert fleet["ports"] == [6651, 6690]
    assert (fleet["message_set"], fleet["multi_bss"], fleet["m2_session"]) == ("r1", True, "shared")
    assert fleet["telemetry"] == {"mode": "off"}
    assert fleet["pods"] == {"SERIAL": {"uplink": {"mode": "multi-ap"}}}
    assert (fleet["state_root"], fleet["config_dir"]) == ("/var/lib/emosa", "/etc/emosa")
    assert "forward" not in fleet and "run_root" not in fleet


def test_rdk_fleet_in_the_gateway(tmp_path):
    # EMOSA in the RDK lab's gateway: its package's places, the ports and the pods'
    # broker on its LAN address, through its forwarder
    state = tmp_path / "state"
    state.mkdir()
    (state / "gateway").touch()
    (state / "telemetry").touch()
    default = tmp_path / "default-emosa"
    default.write_text(
        "EMOSA_FLEET_CONFIG=/nvram/emosa/fleet-config.json\n"
        "EMOSA_AGENT_CONFIG_DIR=/nvram/emosa/agents\n"
        "EMOSA_STATE_ROOT=/nvram/emosa/state\n"
        "EMOSA_RUN_ROOT=/run/emosa\n"
    )
    _, capture = run(
        tmp_path,
        "CTL=bpibroadband fleet_config 6640 6651 6690 02:00:00:00:00:01 r1 true shared",
        state,
        default,
    )
    fleet = json.loads((capture / "fleet-config.json").read_text())
    assert fleet["advertise"] == "10.0.0.1"
    assert fleet["forward"] is True
    places = (fleet["state_root"], fleet["config_dir"])
    assert places == ("/nvram/emosa/state", "/nvram/emosa/agents")
    assert fleet["run_root"] == "/run/emosa"
    assert fleet["telemetry"] == {"mode": "mqtt", "broker": "10.0.0.1", "port": 8883}


def test_opensync_lab_fleet_without_pods(tmp_path):
    _, capture = run(
        tmp_path, "fleet_config 6650 6651 6690 02:00:00:e0:00:01 easymesh-6.1 false distinct"
    )
    fleet = json.loads((capture / "emosa-fleet.json").read_text())
    chosen = (fleet["message_set"], fleet["multi_bss"], fleet["m2_session"])
    assert chosen == ("easymesh-6.1", False, "distinct")
    assert "pods" not in fleet
    assert fleet["profile"] == "opensync-lab-hwsim-6.6.1-v1"


def test_environment_overrides_a_labs_defaults(tmp_path):
    _, capture = run(
        tmp_path,
        "EMOSA_MULTI_BSS=false EMOSA_MESSAGE_SET=easymesh-6.1 "
        "fleet_config 6640 6651 6690 02:00:00:00:00:01 r1 true shared",
    )
    fleet = json.loads((capture / "emosa-fleet.json").read_text())
    assert (fleet["message_set"], fleet["multi_bss"]) == ("easymesh-6.1", False)


def test_pod_journal_bounded(tmp_path):
    """A pod's journald drop-in goes in before its start (lab-storage W3): 128 MiB by default,
    EMOSA_POD_JOURNAL_MAX another bound."""
    _, capture = run(tmp_path, "pod_journal_cap pod-1")
    conf = (capture / "50-lab.conf").read_text()
    assert conf.splitlines() == [
        "[Journal]",
        "SystemMaxUse=128M",
        "SystemMaxFileSize=16M",
        "RuntimeMaxUse=32M",
    ]
    _, capture = run(tmp_path, "POD_JOURNAL_MAX=64M; pod_journal_cap pod-2")
    assert "SystemMaxUse=64M" in (capture / "50-lab.conf").read_text()


def test_pod_pool(tmp_path):
    """A new pod goes in a copy-on-write pool when the VM has one (lab-storage W8): the RDK
    lab's btrfs bpi-lab; else the default profile's pool; EMOSA_POD_POOL another."""
    out, _ = run(tmp_path, "FAKE_BPI_LAB=btrfs pod_pool")
    assert out.strip() == "bpi-lab"
    out, _ = run(tmp_path, "FAKE_BPI_LAB=dir pod_pool")
    assert out.strip() == "default"
    out, _ = run(tmp_path, "pod_pool")
    assert out.strip() == "default"
    out, _ = run(tmp_path, "EMOSA_POD_POOL=pods FAKE_BPI_LAB=btrfs pod_pool")
    assert out.strip() == "pods"


def test_telemetry_setting(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    out, _ = run(tmp_path, "telemetry_json", state)
    assert json.loads(out) == {"mode": "off"}
    (state / "telemetry").touch()
    out, _ = run(tmp_path, "telemetry_json", state)
    assert json.loads(out) == {"mode": "mqtt", "broker": "10.101.0.40", "port": 8883}
    options = 'TELEMETRY_OPTIONS=\'"reporting_interval": 5, "survey": true\''
    out, _ = run(tmp_path, f"{options} telemetry_json", state)
    assert json.loads(out) == {
        "mode": "mqtt",
        "broker": "10.101.0.40",
        "port": 8883,
        "reporting_interval": 5,
        "survey": True,
    }


def test_agent_binaries(tmp_path):
    out, _ = run(tmp_path, "agent_binary python; agent_binary c; agent_binary rust || echo none")
    assert out.split() == [
        "/opt/emosa-adapter/venv/bin/emosa-agent",
        "/opt/emosa-adapter/bin/emosa-agent-c",
        "none",
    ]
