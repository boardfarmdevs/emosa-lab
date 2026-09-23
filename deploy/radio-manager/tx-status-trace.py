"""Passive TX-status observation on the exact owned Ubuntu hwsim kernel.

Owns one tracefs instance and one named probe. Does not change radio/packet
behavior, counters, kernel lockdown or other tracing sessions. Raw kernel
pointers stay in the private raw trace; the reviewed JSON projection omits them.
"""

import hashlib
import json
import os
import platform
import re
import time
from pathlib import Path

ROOT = Path("/sys/kernel/tracing")
BTF = {
    "/sys/kernel/btf/vmlinux": "6b316ce238ad8dc2686af1a0a7fd5e0d8f454e74ec82e6422194971e03aa2ea7",
    "/sys/kernel/btf/mac80211": "6731e6b80c80a9a5832e131c619d1c45e88495ff4d4673277bdba8a98067ebbe",
}
# Offsets reviewed using pahole against these exact runtime BTF files: status
# info=8, skb=16; skb len=112, data=208; info flags=0, rates=8 (4*3 bytes);
# ieee80211_hw.max_report_rates=126. Never apply these offsets to another BTF.
FRAME = "+208(+16($arg2))"
FIELDS = (
    "station=+0($arg2):x64 flags=+0(+8($arg2)):x32 "
    "mpdu_len=+112(+16($arg2)):u32 "
    f"header=+0({FRAME}):x8[24] rates=+8(+8($arg2)):x8[12] "
    "max_rates=+126($arg1):u8 "
    f"ra_lo=+4({FRAME}):x32 ra_hi=+8({FRAME}):x16 "
    f"ta_lo=+10({FRAME}):x32 ta_hi=+14({FRAME}):x16"
)
FILTER = (
    "mpdu_len >= 24 && ra_lo == 0x00000002 && ra_hi == 0x0002 "
    "&& ta_lo == 0xec000002 && ta_hi == 0x0002"
)


def write(path, text, *, append=False):
    # Python open(..., 'a') seeks to EOF and tracefs rejects that seek. Write
    # one command directly; never truncate the global probe definition file.
    fd = os.open(path, os.O_WRONLY | (os.O_APPEND if append else 0))
    try:
        data = text.encode()
        if os.write(fd, data) != len(data):
            raise RuntimeError("short tracefs write")
    finally:
        os.close(fd)


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def decode(raw, event):
    records = []
    pattern = re.compile(
        r"\s(\d+\.\d+): " + re.escape(event) + r": .*?"
        r" station=(0x[0-9a-f]+) flags=(0x[0-9a-f]+) mpdu_len=(\d+) "
        r"header=\{([^}]+)\} rates=\{([^}]+)\} max_rates=(\d+) "
        r"ra_lo=0x[0-9a-f]+ ra_hi=0x[0-9a-f]+ ta_lo=0x[0-9a-f]+ ta_hi=0x[0-9a-f]+$"
    )
    for line in raw.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = pattern.search(line)
        if not match:
            raise ValueError("unexpected trace record; retain private raw trace")
        timestamp, station, flags, length, header, rates, maximum = match.groups()
        header_bytes = bytes(int(v.strip(), 0) for v in header.split(","))
        rate_bytes = bytes(int(v.strip(), 0) for v in rates.split(","))
        if len(header_bytes) != 24 or len(rate_bytes) != 12:
            raise ValueError("unexpected traced header/rate width")
        records.append(
            {
                "monotonic_seconds": float(timestamp),
                "station_present": bool(int(station, 16)),
                "flags": int(flags, 16),
                "mpdu_length": int(length),
                "header_hex": header_bytes.hex(),
                "rates_hex": rate_bytes.hex(),
                "max_report_rates": int(maximum),
            }
        )
    return records


class TxStatusTrace:
    def __init__(self, directory):
        self.directory = directory
        self.name = "tx_" + directory.name.replace("-", "_")
        if not re.fullmatch(r"tx_[a-z0-9_]{1,24}", self.name):
            raise ValueError("use a bounded native-run label")
        self.instance = ROOT / "instances" / ("emosa-" + directory.name)
        self.event = "emosa/" + self.name
        self.owns_probe = self.owns_instance = self.enabled = False
        self.provenance = {}

    def start(self):
        if platform.release() != "6.8.0-139-generic" or platform.machine() != "x86_64":
            raise RuntimeError("trace offsets require the reviewed exact kernel and architecture")
        actual = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in BTF}
        if actual != BTF:
            raise RuntimeError("runtime BTF differs; review layouts before tracing")
        if (ROOT / "events" / self.event).exists() or self.instance.exists():
            raise RuntimeError("trace resources already exist; never adopt another owner's trace")
        self.provenance = {
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "btf_sha256": actual,
            "probe": "p:" + self.event + " ieee80211_tx_status_ext " + FIELDS,
            "filter": FILTER,
            "clock": "mono",
            "buffer_kib_per_cpu": 8192,
            "lockdown": Path("/sys/kernel/security/lockdown").read_text().strip(),
            "wall_ns_before_start": time.time_ns(),
            "monotonic_ns_before_start": time.monotonic_ns(),
            "counter_conversion_qualified": False,
        }
        save(self.directory / "tx-status-provenance.json", self.provenance)
        write(ROOT / "kprobe_events", self.provenance["probe"] + "\n", append=True)
        self.owns_probe = True
        self.instance.mkdir()
        self.owns_instance = True
        write(self.instance / "tracing_on", "0\n")
        write(self.instance / "current_tracer", "nop\n")
        write(self.instance / "trace_clock", "mono\n")
        write(self.instance / "buffer_size_kb", "8192\n")
        event = self.instance / "events" / self.event
        write(event / "filter", FILTER + "\n")
        self.provenance["event_format"] = (event / "format").read_text()
        write(event / "enable", "1\n")
        write(self.instance / "tracing_on", "1\n")
        self.enabled = True
        save(self.directory / "tx-status-provenance.json", self.provenance)

    def stop(self):
        if not self.enabled:
            return
        write(self.instance / "tracing_on", "0\n")
        write(self.instance / "events" / self.event / "enable", "0\n")
        self.enabled = False
        stats = {
            p.parent.name: p.read_text() for p in sorted(self.instance.glob("per_cpu/cpu*/stats"))
        }
        raw = (self.instance / "trace").read_text()
        # The raw trace is private. Do not add it to collect-native-review.py.
        (self.directory / "tx-status.raw").write_text(raw)
        self.provenance.update(
            wall_ns_after_stop=time.time_ns(),
            monotonic_ns_after_stop=time.monotonic_ns(),
            per_cpu_statistics=stats,
            raw_trace_sha256=hashlib.sha256(raw.encode()).hexdigest(),
        )
        save(self.directory / "tx-status-provenance.json", self.provenance)
        records = decode(raw, self.name)
        (self.directory / "tx-status.jsonl").write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
        )
        for text in stats.values():
            for field in ("overrun", "commit overrun", "dropped events"):
                values = re.findall(r"^" + field + r":\s*(\d+)\s*$", text, re.M)
                if values != ["0"]:
                    raise RuntimeError("lost or unaccounted trace events")
        entries = re.findall(r"^entries:\s*(\d+)\s*$", "\n".join(stats.values()), re.M)
        if not entries or sum(map(int, entries)) != len(records):
            raise RuntimeError("trace record count differs from per-CPU accounting")

    def remove(self):
        if self.owns_instance:
            write(self.instance / "tracing_on", "0\n")
            write(self.instance / "events" / self.event / "enable", "0\n")
            self.instance.rmdir()
            self.owns_instance = False
        if self.owns_probe:
            write(ROOT / "kprobe_events", "-:" + self.event + "\n", append=True)
            self.owns_probe = False
