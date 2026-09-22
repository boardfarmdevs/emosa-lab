import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import ovs.jsonrpc
import ovs.stream

from emosa.errors import EmosaError, Reason
from emosa.opensync.schema import reference_path
from emosa.opensync.session import OvsSession


def binary(name):
    override = os.environ.get("EMOSA_OVS_BIN")
    candidates = [Path(override) / name] if override else []
    candidates.append(
        Path(__file__).resolve().parents[3] / ".cache/upstream/openvswitch-4.0.0/ovsdb" / name
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise EmosaError(
        Reason.MISSING_PREREQUISITE, "real ovsdb-server/tool required; run scripts/build-ovsdb.sh"
    )


class SimDatabase:
    """Disposable private Unix-socket database, no host daemon or switch datapath."""

    def __init__(self, directory=None, *, tls_files=None):
        self.temporary = (
            tempfile.TemporaryDirectory(prefix="emosa-db-") if directory is None else None
        )
        self.directory = Path(self.temporary.name if self.temporary else directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.endpoint = "unix:" + str(self.directory / "db.sock")
        self.process = None
        self.log = None
        self.tls_files = tls_files

    async def start(self):
        server, tool = binary("ovsdb-server"), binary("ovsdb-tool")
        db = self.directory / "pod.db"
        if not db.exists():
            result = await asyncio.to_thread(
                subprocess.run,
                [tool, "create", str(db), str(reference_path())],
                capture_output=True,
                text=True,
            )
            if result.returncode:
                raise EmosaError(
                    Reason.SCHEMA_MISMATCH, "ovsdb-tool could not create reference database"
                )
        self.log = open(self.directory / "server.log", "ab")  # noqa: SIM115 - closed by stop()
        self.process = subprocess.Popen(
            [
                server,
                str(db),
                "--remote=p" + self.endpoint,
                "--unixctl=" + str(self.directory / "control.sock"),
                "--pidfile=" + str(self.directory / "server.pid"),
                "--no-chdir",
                *(
                    [
                        "--private-key=" + str(self.tls_files["private_key"]),
                        "--certificate=" + str(self.tls_files["certificate"]),
                        "--ca-cert=" + str(self.tls_files["ca"]),
                    ]
                    if self.tls_files
                    else []
                ),
            ],
            stdout=self.log,
            stderr=self.log,
        )
        end = asyncio.get_running_loop().time() + 5

        def accepting(name):
            # A pathname can be stale, and the database listener can appear
            # before unixctl. Both endpoints must actually accept connections.
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.05)
                    probe.connect(str(self.directory / name))
                return True
            except OSError:
                return False

        while True:
            if self.process.poll() is not None or asyncio.get_running_loop().time() >= end:
                raise EmosaError(Reason.NOT_READY, "disposable ovsdb-server did not start")
            if accepting("db.sock") and accepting("control.sock"):
                break
            await asyncio.sleep(0.01)
        return self

    async def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(self.process.wait), 5)
            except TimeoutError:
                self.process.kill()
                await asyncio.to_thread(self.process.wait)
        if self.log:
            self.log.close()

    async def close(self):
        await self.stop()
        if self.temporary:
            self.temporary.cleanup()

    async def manager_remote(self, endpoint, *, connect=True):
        """Make this disposable database initiate a connection to a private local listener."""
        if not endpoint.startswith("unix:/") and not (
            self.tls_files and re.fullmatch(r"ssl:127\.0\.0\.1:[0-9]+", endpoint)
        ):
            raise EmosaError(Reason.INVALID_INPUT, "simulation remote must be a local Unix socket")

        def command():
            error, stream = ovs.stream.Stream.open_block(
                ovs.stream.Stream.open("unix:" + str(self.directory / "control.sock")), 5000
            )
            if error:
                raise EmosaError(Reason.NOT_READY, "simulation admin socket unavailable")
            rpc = ovs.jsonrpc.Connection(stream)
            try:
                action = "add-remote" if connect else "remove-remote"
                error, reply = rpc.transact_block(
                    ovs.jsonrpc.Message.create_request("ovsdb-server/" + action, [endpoint])
                )
                if error or reply.error is not None:
                    raise EmosaError(Reason.NOT_READY, "simulation remote change failed")
            finally:
                rpc.close()

        await asyncio.to_thread(command)

    async def seed(self, *, serial_number=None, sole_radio=False):
        # Fixed, explicitly synthetic device records. No adapter predicate imports.
        ap = {
            "if_name": "lab-ap",
            "mode": "ap",
            "enabled": True,
            "ssid": "initial-network",
            "wpa": True,
            "wpa_key_mgmt": ["set", ["wpa2-psk"]],
            "wpa_psks": [
                "map",
                [["key", "initial-simulation-key"], ["guest", "preserved-guest-key"]],
            ],
            "rsn_pairwise_ccmp": True,
            "wpa_pairwise_tkip": False,
            "wpa_pairwise_ccmp": False,
            "security": ["map", []],
        }
        if sole_radio:
            ap["wpa_psks"] = ["map", [["key", "initial-simulation-key"]]]
        state = {**ap, "vif_config": ["named-uuid", "vif"], "mac": "02:00:00:00:10:01"}
        config = {
            **ap,
            "bridge": "preserved-lab-bridge",
            "wpa_oftags": ["map", [["guest", "preserved-tag"]]],
        }
        if sole_radio:
            config.update(multi_ap="none", wpa_oftags=["map", []])
        rows = [
            ("Wifi_VIF_Config", "vif", config),
            ("Wifi_VIF_State", "vifstate", state),
            (
                "Wifi_Radio_Config",
                "radio",
                {
                    "if_name": "lab-radio",
                    "freq_band": "5G",
                    "enabled": True,
                    "vif_configs": ["set", [["named-uuid", "vif"]]],
                },
            ),
            (
                "Wifi_Radio_State",
                "radiostate",
                {
                    "if_name": "lab-radio",
                    "freq_band": "5G",
                    "radio_config": ["named-uuid", "radio"],
                    "enabled": True,
                    "channel": 36,
                    "mac": "02:00:00:00:20:01",
                    "vif_states": ["set", [["named-uuid", "vifstate"]]],
                },
            ),
        ]
        if serial_number is not None:
            rows.append(
                (
                    "AWLAN_Node",
                    "node",
                    {
                        "serial_number": serial_number,
                        "model": "EMOSA synthetic extender",
                        "firmware_version": "simulation-only",
                    },
                )
            )
        session = OvsSession(self.endpoint)
        try:
            result = await session.transact(
                [
                    {"op": "insert", "table": table, "uuid-name": name, "row": row}
                    for table, name, row in rows
                ]
            )
            if any(not isinstance(r, dict) or "error" in r for r in result):
                raise EmosaError(Reason.INVALID_INPUT, "synthetic seed transaction rejected")
        finally:
            await session.close()


class SimManager:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.process = None

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "emosa.simulation.manager",
            self.endpoint,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self.command("observe")
        return self

    async def command(self, action):
        self.process.stdin.write((json.dumps({"action": action}) + "\n").encode())
        await self.process.stdin.drain()
        line = await asyncio.wait_for(self.process.stdout.readline(), 10)
        if not line:
            raise EmosaError(Reason.NOT_READY, "independent simulated manager exited")
        result = json.loads(line)
        if "error" in result:
            raise EmosaError(Reason.NOT_READY, "independent manager command failed")
        return result

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
