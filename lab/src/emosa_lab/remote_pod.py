# SPDX-License-Identifier: Apache-2.0
"""A simulated OpenSync pod for a real EMOSA over the network.

The lab in a box's recorded pod (an OVSDB server from recorded rows of an OpenSync 6.6.1 hwsim
pod) is handed to a remote fleet's front port, as an operator's redirector does, and then
behaves as a pod's cm: when the fleet writes manager_addr, it dials the agent there instead.
Its managers copy each AP VIF's Config into its State, so the agent's writes are observed as
applied. Outbound TCP only, no radio and no data plane: the remote EMOSA's agent onboards it to
its controller as it would a real pod. Runs until stopped (SIGINT, SIGTERM) or --seconds.

  python -m emosa_lab.remote_pod --fleet HOST:PORT [--serial SERIAL] [--seconds N]

The fleet must admit the serial and give it the recorded pod's profile,
opensync-lab-hwsim-6.6.1-v1 (pods.SERIAL.profile when its default is another).
"""

import argparse
import asyncio
import contextlib
import json
import signal
import sys
import time
from pathlib import Path

from emosa.opensync.schema import reference_path
from emosa.opensync.session import OvsSession
from emosa_lab.box import POD_ROWS, apply_configuration, dial, recorded_pod
from emosa_lab.simulation.database import SimDatabase

RECORDED_SERIAL = "MVXPOD023F87E628DD"
PROFILE = "opensync-lab-hwsim-6.6.1-v1"


def log(text):
    print(time.strftime("%H:%M:%S ") + text, flush=True)


async def rows(db, table):
    """One table of the pod's database, every column, decoded."""
    schema = json.loads(Path(reference_path()).read_text())
    columns = sorted(schema["tables"][table]["columns"])
    session = OvsSession(db.endpoint, read_only=True, monitor_columns={table: columns})
    try:
        snapshot = await session.snapshot()
    finally:
        await session.close()
    return snapshot["tables"].get(table, {})


async def transact(db, operations):
    session = OvsSession(db.endpoint)
    try:
        return await session.transact(operations)
    finally:
        await session.close()


async def start_pod(serial, directory=None):
    """The recorded pod under another serial, in a disposable database."""
    _, operations = recorded_pod(POD_ROWS, {RECORDED_SERIAL: serial})
    db = SimDatabase(directory)
    await db.start()
    await transact(db, operations)
    return db


async def follow(db, remote):
    """As a pod's cm: the remote manager_addr names now, dialed in place of remote."""
    node = next(iter((await rows(db, "AWLAN_Node")).values()), {})
    wanted = node.get("manager_addr")
    if wanted and wanted != remote:
        await dial(db, remote, connect=False)
        await dial(db, wanted)
        log(f"manager_addr {wanted}: dialing it instead of {remote}")
        return wanted
    return remote


async def served(db):
    """The SSIDs the pod's AP VIFs serve (their State)."""
    return sorted(
        str(row.get("ssid"))
        for row in (await rows(db, "Wifi_VIF_State")).values()
        if row.get("mode") == "ap" and row.get("ssid")
    )


async def run(args):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    db = await start_pod(args.serial)
    try:
        remote = f"tcp:{args.fleet}"
        node = {"op": "update", "table": "AWLAN_Node", "where": [], "row": {"manager_addr": remote}}
        await transact(db, [node])
        await dial(db, remote)
        log(f"pod {args.serial} (profile {PROFILE}): handed to the fleet at {remote}")
        end = time.monotonic() + args.seconds if args.seconds else None
        last = None
        while not stop.is_set() and (end is None or time.monotonic() < end):
            remote = await follow(db, remote)
            await apply_configuration(db)
            ssids = await served(db)
            if ssids != last:
                log(f"serving {ssids or 'no SSID'}")
                last = ssids
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), args.interval)
        log(f"pod {args.serial}: stopped")
    finally:
        await db.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fleet", required=True, help="the fleet's front port, HOST:PORT")
    parser.add_argument("--serial", default="SIMPOD0000000001")
    parser.add_argument("--seconds", type=float, default=0, help="stop after; 0: until stopped")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between looks")
    args = parser.parse_args()
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
