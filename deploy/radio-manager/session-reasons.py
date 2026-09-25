"""Passive live hwsim reason + AP removal-event join in the owned VM.

Reads the already-created hwsim monitor and owned AP event log. No interface,
radio, pod configuration or kernel tracing is changed. Counters stay unqualified.
"""

import argparse
import asyncio
import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

from common import AP, ROOT, guard, write

from emosa_lab.simulation.session_reasons import ClockBounds, ReasonJoin, reason_frame


async def observe(directory, seconds):
    guard()
    if directory.parent != ROOT / "runs" or json.loads(
        (directory / "native-owner.json").read_text()
    ) != {"owner": "emosa-native-onboarding-v1", "backend": "ovsdb-sim"}:
        raise ValueError("expected owned native run")
    if Path("/sys/class/net/hwsim0/type").read_text().strip() != "803":
        raise ValueError("expected existing hwsim radiotap monitor")
    index = socket.if_nametoindex("hwsim0")
    state_path = directory / "reason-observer.json"
    state_path.open("x").close()
    events_path = ROOT / "station-events" / (directory.name + ".jsonl")
    epoch = str(uuid.uuid4())
    join = ReasonJoin(epoch)
    state = {
        "profile": "owned-hwsim-reason-join-v1",
        "collector_epoch": epoch,
        "run_label": directory.name,
        "interface": "hwsim0",
        "ifindex": index,
        "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "running": True,
        "ready": False,
        "errors": [],
        "joined": 0,
        "socket_packets": 0,
        "socket_drops": 0,
        "final_counter_source_qualified": False,
        "physical_pod_changed": False,
    }
    old = ""
    kernel_finished = False
    clock = ClockBounds()
    state["initial_clock_sample"] = clock.initial
    stopping = False

    def stop():
        nonlocal stopping
        stopping = True

    with (
        socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)) as channel,
        (directory / "session-reasons.jsonl").open("x") as log,
    ):
        channel.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        channel.setsockopt(socket.SOL_SOCKET, 35, 1)  # Linux SO_TIMESTAMPNS, 64-bit ABI
        channel.bind(("hwsim0", 3))
        channel.setblocking(False)

        def emit(value):
            log.write(json.dumps(value, sort_keys=True) + "\n")
            log.flush()

        def socket_health():
            packets, dropped = struct.unpack("=II", channel.getsockopt(263, 6, 8))
            state["socket_packets"] += packets
            state["socket_drops"] += dropped
            if dropped:
                raise ValueError("reason_packet_socket_loss")
            if socket.if_nametoindex("hwsim0") != index:
                raise ValueError("reason_monitor_identity_changed")

        async def kernel_stream():
            nonlocal old, kernel_finished
            while not kernel_finished and not stopping:
                child = await asyncio.create_subprocess_exec(
                    "lxc",
                    "--force-local",
                    "--project",
                    "default",
                    "exec",
                    AP,
                    "--",
                    "cat",
                    str(events_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    out, _ = await asyncio.wait_for(child.communicate(), 2)
                except BaseException:
                    if child.returncode is None:
                        child.kill()
                    await child.wait()
                    raise
                if child.returncode or len(out) > 1024 * 1024:
                    raise ValueError("kernel_event_read_failed_or_budget_exhausted")
                text = out.decode("utf-8")
                if not text.startswith(old):
                    raise ValueError("kernel_event_stream_replaced")
                # A writer may be between writes; consume complete lines only.
                complete = text[: text.rfind("\n") + 1]
                for line in complete[len(old) :].splitlines():
                    row = json.loads(line)
                    sample = {
                        name: row["received_" + name]
                        for name in ("wall_ns", "monotonic_ns", "monotonic_after_ns")
                    }
                    state["last_kernel_clock_sample"] = sample
                    clock.check(sample)
                    if row["event"] == "ready":
                        if old or state["ready"] or row["bssid"] != "02:00:00:ec:02:00":
                            raise ValueError("changed_station_collector_identity")
                        state["kernel_collector"] = row
                        state["ready"] = True
                        emit({"event": "ready", **state})
                    elif row["event"] == "finished":
                        if row["errors"]:
                            raise ValueError("kernel_station_observation_failed")
                        kernel_finished = True
                        state["kernel_finished"] = row
                    else:
                        if not state["ready"]:
                            raise ValueError("kernel_metadata_missing")
                        if not old:
                            raise ValueError("reason_collector_attached_after_station_creation")
                        if row["ifindex"] != state["kernel_collector"]["ifindex"]:
                            raise ValueError("kernel_interface_changed")
                        join.kernel(row)
                old = complete
                await asyncio.sleep(0.15)

        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop)
        task = asyncio.create_task(kernel_stream())
        deadline, heartbeat = time.monotonic() + seconds, 0.0
        try:
            while not stopping and time.monotonic() < deadline:
                if task.done():
                    task.result()
                for _ in range(512):
                    try:
                        data, ancillary, flags, address = channel.recvmsg(65536, 128)
                    except BlockingIOError:
                        break
                    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or address[0] != "hwsim0":
                        raise ValueError("truncated_or_foreign_monitor_packet")
                    timestamps = [v for level, kind, v in ancillary if (level, kind) == (1, 35)]
                    if len(timestamps) != 1 or len(timestamps[0]) != 16:
                        raise ValueError("missing_kernel_packet_timestamp")
                    sec, ns = struct.unpack("=qq", timestamps[0])
                    frame = reason_frame(data, sec * 1_000_000_000 + ns)
                    if frame is not None:
                        emit({"event": "reason_frame", **frame})
                    join.radio(frame, time.time_ns())
                sample = clock.sample()
                state["last_clock_sample"] = sample
                clock.check(sample)
                now, mono = sample["wall_ns"], sample["monotonic_after_ns"]
                socket_health()
                final = join.poll(now, mono)
                if final:
                    emit(final)
                    state["joined"] = join.joined
                if time.monotonic() - heartbeat >= 0.1:
                    state["heartbeat_monotonic_ns"] = mono
                    write(state_path, state)
                    heartbeat = time.monotonic()
                if kernel_finished and join.pending is None:
                    break
                await asyncio.sleep(0.01)
            if not kernel_finished:
                raise ValueError("reason_collector_interrupted_before_kernel_end")
            if join.pending is not None:
                raise ValueError("unfinished_final_reason_join")
        except Exception as error:
            state["errors"].append(type(error).__name__ + ":" + str(error))
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as error:
                message = type(error).__name__ + ":" + str(error)
                if message not in state["errors"]:
                    state["errors"].append(message)
            try:
                socket_health()
            except Exception as error:
                state["errors"].append(type(error).__name__ + ":" + str(error))
            state["running"] = False
            emit({"event": "finished", **state})
            write(state_path, state)
    if state["errors"]:
        raise RuntimeError("live reason observation failed")


class ReasonObserver:
    """Lifecycle used only by the ownership-checked native harness."""

    def __init__(self, directory, seconds):
        self.directory, self.seconds = directory, seconds
        self.process = self.log = None

    async def start(self):
        self.log = (self.directory / "reason-observer.log").open("xb")
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__)),
                str(self.directory),
                "--seconds",
                str(self.seconds),
            ],
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 10
        path = self.directory / "reason-observer.json"
        while time.monotonic() < deadline:
            self.check()
            if path.exists() and path.stat().st_size and json.loads(path.read_text())["ready"]:
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("live reason collector not ready")

    def check(self):
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError("live reason collector exited early; inspect retained log")

    async def stop(self):
        try:
            if self.process is not None:
                try:
                    code = await asyncio.to_thread(self.process.wait, 5)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        code = await asyncio.to_thread(self.process.wait, 5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        await asyncio.to_thread(self.process.wait, 5)
                        raise RuntimeError("live reason collector required SIGKILL") from None
                if code:
                    raise RuntimeError("live reason collector failed")
        finally:
            if self.log:
                self.log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--seconds", type=int, required=True)
    args = parser.parse_args()
    if not 30 <= args.seconds <= 7200:
        parser.error("seconds must be 30–7200")
    os.umask(0o077)
    asyncio.run(observe(args.directory.resolve(strict=True), args.seconds))
