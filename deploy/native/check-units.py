#!/usr/bin/env python3
"""Run upstream units in separate processes and a private disposable database.

The recorded lab patch makes the upstream fork runner check child exit codes.
Keep fork isolation: prefixes can match several tests that share static state.
No OpenSync boot scripts or host-global database/socket paths are used.
"""

import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path


def main():
    if (
        socket.gethostname() != "opensync-native-r0"
        or subprocess.check_output(["systemd-detect-virt", "--container"], text=True).strip()
        != "lxc"
    ):
        raise SystemExit("Run only in the isolated opensync-native-r0 container")
    root = Path(os.environ.get("EMOSA_NATIVE_ROOT", "/opt/native"))
    if root not in (Path("/opt/native"), Path("/opt/native/reproduction")):
        raise SystemExit("Unexpected native build root")
    core = root / "device/core"
    build = core / "work/native-gcc-ubuntu24.04-x86_64"
    evidence = Path(tempfile.mkdtemp(prefix="units-", dir=root))
    env = {
        **os.environ,
        "LD_LIBRARY_PATH": str(build / "lib"),
        "PLUME_OVSDB_SOCK_PATH": str(evidence / "db.sock"),
        "OSW_DRV_TARGET_DISABLED": "1",
        "OSW_DRV_NL80211_DISABLE": "1",
    }
    owm = str(build / "bin/owm")
    subprocess.run(
        [
            str(root / "ovsdb/ovsdb-tool"),
            "create",
            str(evidence / "test.db"),
            str(core / "interfaces/opensync.ovsschema"),
        ],
        check=True,
    )
    names = subprocess.check_output([owm, "-l"], env=env, text=True).splitlines()
    names = [name.strip() for name in names if name.strip().startswith("osw_ut_func_")]
    prefixes = (
        "osw_ut_func_ow_ovsdb",
        "osw_ut_func_ow_conf",
        "osw_ut_func_osw_drv",
        "osw_ut_func_osw_confsync",
        "osw_ut_func_osw_types",
    )
    selected = [name for name in names if name.startswith(prefixes)]
    if not selected:
        raise SystemExit("No relevant upstream unit tests found")
    results = []
    with (evidence / "server.log").open("w") as log:
        server = subprocess.Popen(
            [
                str(root / "ovsdb/ovsdb-server"),
                str(evidence / "test.db"),
                "--remote=punix:" + env["PLUME_OVSDB_SOCK_PATH"],
                "--unixctl=" + str(evidence / "control.sock"),
                "--no-chdir",
            ],
            stdout=log,
            stderr=log,
        )
        try:
            end = time.monotonic() + 5
            while not (evidence / "db.sock").exists():
                if server.poll() is not None or time.monotonic() >= end:
                    raise RuntimeError("Disposable database did not start")
                time.sleep(0.01)
            for name in prefixes:
                started = time.monotonic()
                with (
                    (evidence / (name + ".log")).open("w") as output,
                    subprocess.Popen(
                        [owm, "-U", name],
                        env=env,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    ) as child,
                ):
                    try:
                        code = child.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                        code = "timeout"
                results.append(
                    {
                        "prefix": name,
                        "exit_code": code,
                        "selected_count": sum(n.startswith(name) for n in names),
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    }
                )
                print(name, code, flush=True)
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
    summary = {
        "schema_version": 1,
        "scope": "native upstream component units",
        "runner": "process per prefix; patched upstream fork runner checks child exit status",
        "results": results,
        "passed": all(r["exit_code"] == 0 for r in results),
    }
    (evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(evidence, flush=True)
    raise SystemExit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    main()
