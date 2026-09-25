"""VM-local LXD invocation; application containers never receive the admin socket."""

import subprocess
import uuid
from pathlib import Path

from emosa.errors import EmosaError, Reason


def endpoint_command(endpoint, arguments):
    if endpoint not in {"em-controller", "emosa"}:
        raise EmosaError(Reason.INVALID_INPUT, "unknown project endpoint container")
    return ["lxc", "exec", endpoint, "--", *arguments]


def invoke(arguments, *, scenario_path=None):
    arguments = list(arguments)
    if scenario_path is not None:
        path = Path(scenario_path).resolve()
        remote = "/var/lib/emosa/lab/scenarios/" + uuid.uuid4().hex + ".json"
        subprocess.run(
            endpoint_command("emosa", ["mkdir", "-p", "/var/lib/emosa/lab/scenarios"]), check=True
        )
        subprocess.run(["lxc", "file", "push", str(path), "emosa" + remote], check=True)
        arguments[arguments.index(str(scenario_path))] = remote
        # The controller's status is separate from pod readiness, even in a component run.
        subprocess.run(
            endpoint_command(
                "em-controller", ["/opt/emosa/.venv/bin/em-controller", "status", "--json"]
            ),
            check=True,
        )
    return subprocess.run(
        endpoint_command(
            "emosa",
            ["/opt/emosa/.venv/bin/emosa-lab", "--state-dir", "/var/lib/emosa/lab", *arguments],
        )
    ).returncode
