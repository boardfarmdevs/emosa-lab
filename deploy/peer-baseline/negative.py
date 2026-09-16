"""Negative controls on a provisioned, isolated native baseline."""

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

from node import KEY
from run import AGENT, WIFI, Attempt, ap, check_ok, guard, sta, values, write
from setup import ROOT, inside


def wrong_key(attempt):
    check_ok(sta(WIFI, "disable_network", "all", iface="wlan0"))
    try:
        check_ok(sta(WIFI, "set_network", "0", "psk", '"DeliberatelyWrong2026!"', iface="wlan0"))
        # A fresh log offset excludes earlier authentication failures.
        offset = int(inside(WIFI, "stat", "-c", "%s", str(ROOT / "client.log")).strip())
        check_ok(sta(WIFI, "enable_network", "0", iface="wlan0"))
        observations = []
        for _ in range(15):
            time.sleep(1)
            observation = values(sta(WIFI, "status", iface="wlan0"))
            observations.append(observation)
            write(attempt.directory / "wrong-key-status.json", observations)
            if observation.get("wpa_state") == "COMPLETED":
                raise RuntimeError("Wrong-key client unexpectedly authenticated")
        log = inside(
            WIFI,
            "python3",
            "-c",
            f"from pathlib import Path; "
            f"print(Path({str(ROOT / 'client.log')!r}).read_bytes()[{offset}:].decode())",
        )
        (attempt.directory / "wrong-key.log").write_text(log)
        if "WRONG_KEY" not in log:
            raise RuntimeError("No explicit wrong-key rejection was observed")
        stations = ap(AGENT, "all_sta")
        (attempt.directory / "agent-stations-after-wrong-key.txt").write_text(stations)
        if "[AUTHORIZED]" in stations:
            raise RuntimeError("Agent still reports an authorized fronthaul station")
    finally:
        check_ok(sta(WIFI, "disable_network", "all", iface="wlan0"))
        check_ok(sta(WIFI, "set_network", "0", "psk", json.dumps(KEY), iface="wlan0"))
        check_ok(sta(WIFI, "enable_network", "0", iface="wlan0"))
    attempt.clients(prepare=False)


def update_rejections(attempt):
    # Only the synthetic agent's hostapd file is modified; every edit is restored.
    path = "/var/run/hostapd-phy0.conf"
    original = inside(AGENT, "cat", path)
    before = values(ap(AGENT, "status"))
    fields = ("state", "freq", "channel", "bssid[0]", "bssid[1]", "ssid[0]", "ssid[1]")
    expected = {key: before[key] for key in fields}
    cases = {
        "malformed": original + "\nemosa_unknown_option=1\n",
        "channel-change": re.sub(r"(?m)^channel=6$", "channel=11", original),
        "bss-layout-change": original.replace("bss=wlan0.0", "bss=wlan0.1"),
        "bssid-change": original.replace("bssid=02:00:00:ec:02:01", "bssid=02:00:00:ec:02:05"),
        "bridge-change": original.replace("bridge=br-lan", "bridge=unqualified"),
    }
    outcomes = {}
    try:
        for label, modified in cases.items():
            if modified == original:
                raise RuntimeError("Negative mutation did not alter the selected configuration")
            inside(
                AGENT,
                "python3",
                "-c",
                f"from pathlib import Path; Path({path!r}).write_text({modified!r})",
            )
            reply = ap(AGENT, "raw", "UPDATE")
            current = values(ap(AGENT, "status"))
            observed = {key: current.get(key) for key in fields}
            outcomes[label] = {"reply": reply, "expected": expected, "observed": observed}
            write(attempt.directory / "update-rejections.json", outcomes)
            if reply != "FAIL" or observed != expected:
                raise RuntimeError("Unsupported UPDATE was accepted or changed live AP state")
    finally:
        inside(
            AGENT,
            "python3",
            "-c",
            f"from pathlib import Path; Path({path!r}).write_text({original!r})",
        )
    attempt.clients(prepare=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("wired", "wireless"), required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    guard()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,70}", args.label):
        raise SystemExit("Use a unique lowercase label")
    suite = ROOT / "runs" / args.label
    (suite / "harness").mkdir(parents=True, exist_ok=False)
    sources = sorted(Path(__file__).parent.glob("*.py"))
    for source in sources:
        (suite / "harness" / source.name).write_bytes(source.read_bytes())
    write(
        suite / "harness.json",
        {source.name: hashlib.sha256(source.read_bytes()).hexdigest() for source in sources},
    )
    attempt = Attempt(suite / "negative-controls", args.mode, "negative-controls")
    try:
        if not attempt.ready():
            raise RuntimeError("A provisioned baseline must pass before negative controls")
        attempt.captures()
        wrong_key(attempt)
        update_rejections(attempt)
        attempt.stability()
        attempt.result["status"] = "passed"
    except Exception as error:
        attempt.result["status"] = "failed"
        attempt.result["error"] = str(error)
    finally:
        try:
            attempt.collect()
        except Exception as error:
            attempt.result["status"] = "failed"
            attempt.result["collection_error"] = str(error)
        attempt.save()
    print(json.dumps(attempt.result), flush=True)
    raise SystemExit(0 if attempt.result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
