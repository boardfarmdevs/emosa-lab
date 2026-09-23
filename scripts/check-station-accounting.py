"""Audit exact-runtime kernel accounting against loss-checked radio captures.

This tests the observed Linux counter meanings, not an EasyMesh conversion.
The bounded corpus has one HT20/CCMP client, no fragmentation/A-MSDU/retries
and no recorded transmit/receive errors. Do not generalize it to other cases.
"""

import argparse
import json
import runpy
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).parent
HEALTH = runpy.run_path(str(SCRIPTS / "check-capture-health.py"))["check"]
STA, AP = "02:00:00:00:02:00", "02:00:00:ec:02:00"


def flag(value, *, optional=False):
    # tshark 3.6 prints numeric booleans; 4.2 prints True/False. Reject
    # unfamiliar values rather than silently treating protection/retries as off.
    if optional and value == "":
        return False
    assert value in ("1", "0", "True", "False"), "unexpected tshark boolean"
    return value in ("1", "True")


def frames(directory, tshark):
    fields = (
        "frame.number",
        "frame.time_epoch",
        "frame.len",
        "radiotap.length",
        "wlan.ta",
        "wlan.ra",
        "wlan.fc.type_subtype",
        "wlan.fc.protected",
        "wlan.fc.retry",
        "wlan.frag",
        "wlan.fc.frag",
        "wlan.qos.amsdupresent",
    )
    raw = subprocess.check_output(
        [
            tshark,
            "-n",
            "-r",
            str(directory / "radio.pcap"),
            "-Y",
            f"wlan.ta == {STA} || wlan.ra == {STA} || wlan.ta == {AP} || wlan.ra == {AP}",
            "-T",
            "fields",
            *[v for f in fields for v in ("-e", f)],
        ],
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    output = []
    for line in raw.splitlines():
        values = line.split("\t")
        assert len(values) == len(fields)
        number, timestamp, size, header, ta, ra, kind, protected, retry, fragment, more, amsdu = (
            values
        )
        output.append(
            {
                "frame": int(number),
                "time": float(timestamp),
                "mpdu_bytes": int(size) - int(header),
                "ta": ta,
                "ra": ra,
                "kind": int(kind, 0),
                "protected": flag(protected),
                "retry": flag(retry),
                "fragment": int(fragment or "0"),
                "more_fragments": flag(more),
                "amsdu": flag(amsdu, optional=True),
            }
        )
    return output


def check(directory, tshark):
    health = HEALTH(directory)
    radio = frames(directory, tshark)
    events = [json.loads(s) for s in (directory / "station-events.jsonl").read_text().splitlines()]
    assert events[0]["kernel"] == "6.8.0-139-generic", "review this kernel's accounting first"
    assert events[0]["event"] == "ready" and events[-1]["event"] == "finished"
    assert not events[-1]["errors"]
    results = []
    start = None
    for event in events[1:-1]:
        assert event["station"] == STA
        if event["event"] == "new_station":
            start = event["received_wall_ns"] / 1e9
            continue
        assert start is not None and event["event"] == "del_station"
        stop = event["received_wall_ns"] / 1e9
        # Multicast delivery can follow the first transmitted authentication
        # reply. Include 100 ms of pre-event capture, then locate actual frames;
        # do not pretend event receipt is the exact first-packet timestamp.
        window = [f for f in radio if start - 0.1 <= f["time"] < stop]
        tx = [f for f in window if f["ta"] == AP and f["ra"] == STA and f["kind"] != 5]
        rx = [f for f in window if f["ta"] == STA and f["ra"] == AP and f["kind"] != 11]
        assert sum(f["kind"] == 11 for f in tx) == 1  # actual authentication reply
        assert sum(f["kind"] == 0 for f in rx) == 1  # association request
        assert sum(f["kind"] == 12 for f in rx) == 1  # deauthentication
        assert all(
            not f["fragment"] and not f["more_fragments"] and not f["amsdu"] and not f["retry"]
            for f in tx + rx
        )
        assert all(f["kind"] == 0x28 for f in tx + rx if f["protected"])
        observed = event["observed_fields"]
        assert observed["tx_failed"] == observed["tx_retries"] == observed["rx_drop_misc"] == 0
        protected_tx = sum(f["protected"] for f in tx)
        management_rx = sum(f["kind"] < 16 for f in rx)
        assert management_rx == 4
        actual_tx_bytes = sum(f["mpdu_bytes"] for f in tx)
        actual_rx_bytes = sum(f["mpdu_bytes"] for f in rx)
        assert observed["tx_packets"] == len(tx)
        assert observed["tx_bytes64"] == actual_tx_bytes - 16 * protected_tx
        assert observed["rx_bytes64"] == actual_rx_bytes
        assert observed["rx_packets"] == len(rx) + management_rx
        # In this sole-client hwsim trace, each unicast TX has its own following
        # ACK addressed to this AP, before its next unicast TX. This corroborates
        # the kernel's zero-failure observation; no missing ACK is presumed.
        acknowledgments = [f for f in window if f["kind"] == 0x1D and f["ra"] == AP]
        used = set()
        for index, packet in enumerate(tx):
            next_frame = tx[index + 1]["frame"] if index + 1 < len(tx) else float("inf")
            matches = [
                f
                for f in acknowledgments
                if packet["frame"] < f["frame"] < next_frame
                # hwsim synthetic ACK skb timestamps can slightly precede the
                # MPDU timestamp despite following it in capture record order.
                # This is an identity/accounting check, not an RF timing test.
                and abs(f["time"] - packet["time"]) < 0.01
            ]
            assert len(matches) == 1 and matches[0]["frame"] not in used
            used.add(matches[0]["frame"])
        results.append(
            {
                "observed_lifetime": event["observed_lifetime"],
                "tx_packets": len(tx),
                "tx_acknowledgments": len(used),
                "tx_mpdu_bytes": actual_tx_bytes,
                "kernel_tx_bytes": observed["tx_bytes64"],
                "protected_tx_packets": protected_tx,
                "tx_ccmp_bytes_not_in_kernel_counter": 16 * protected_tx,
                "rx_packets": len(rx),
                "kernel_rx_packets": observed["rx_packets"],
                "rx_management_counted_twice": management_rx,
                "rx_mpdu_bytes": actual_rx_bytes,
            }
        )
        start = None
    assert len(results) == events[-1]["counts"]["del_station"] >= 3
    return {
        "kernel_accounting_checks_passed": True,
        "scope": "exact captured normal-traffic counter accounting, not EasyMesh qualification",
        "kernel": events[0]["kernel"],
        "capture_health": health,
        "sessions": results,
        "raw_counter_passthrough_valid": False,
        "final_counter_source_qualified": False,
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
        "remaining": [
            "Selected Wi-Fi Data Elements definitions and qualified online conversion",
            "Failed/retried/queued traffic, different frame forms and source-loss behavior",
            "AP airtime/ESP and neighbor measurements; full reporting and 15-minute acceptance",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--tshark", default="tshark")
    args = parser.parse_args()
    print(json.dumps(check(args.directory, args.tshark), indent=2))
