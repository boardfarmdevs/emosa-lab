"""Read-only native process/library and stop-policy observations in the owned node.

Run through the VM's ownership-checked harness. No process is signalled or
configuration changed. Capture only selected binary/library facts, never memory.
"""

import hashlib
import json
import os
import socket
import subprocess
import time
from pathlib import Path


def observe():
    if os.geteuid() != 0 or socket.gethostname() != "em-baseline-controller":
        raise RuntimeError("expected the owned native controller container")
    result = {"observed_at": time.time(), "units": {}}
    for name in ("controller", "agent"):
        unit = "emosa-baseline-" + name + ".service"
        fields = (
            "MainPID",
            "ActiveState",
            "KillSignal",
            "KillMode",
            "TimeoutStopUSec",
            "SendSIGKILL",
            "SuccessExitStatus",
            "Restart",
            "ExecStart",
        )
        raw = subprocess.check_output(
            ["systemctl", "show", unit, *["--property=" + p for p in fields]], text=True
        )
        state = dict(line.split("=", 1) for line in raw.splitlines())
        pid = int(state["MainPID"])
        if state["ActiveState"] != "active" or pid <= 1:
            raise RuntimeError("native process is not active")
        proc = Path("/proc") / str(pid)
        libraries = {}
        for line in (proc / "maps").read_text().splitlines():
            fields = line.split()
            if len(fields) != 6:
                continue
            path = Path(fields[5])
            if path.name not in ("libbpl.so.6.0.0", "libnbapi.so.6.0.0", "libamxrt.so.0.5.1"):
                continue
            stat = path.stat()
            device = f"{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}"
            if int(fields[4]) != stat.st_ino or fields[3] != device:
                raise RuntimeError("mapped library does not match current filesystem inode")
            libraries[path.name] = {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "inode": stat.st_ino,
                "device": device,
            }
        if len(libraries) != 3:
            raise RuntimeError("selected native lifetime libraries not all mapped")
        result["units"][name] = {
            "properties": state,
            "executable": str((proc / "exe").resolve()),
            "executable_sha256": hashlib.sha256((proc / "exe").read_bytes()).hexdigest(),
            "libraries": libraries,
        }
    return result


if __name__ == "__main__":
    print(json.dumps(observe(), indent=2))
