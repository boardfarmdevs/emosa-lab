"""Validate completed semantic radio runs and publish reviewed synthetic evidence."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

CASES = {
    "initial",
    "change",
    "withheld",
    "late-application",
    "lost-reply",
    "wrong-key",
    "unsupported-channel",
    "adapter-restart",
    "database-manager-restart",
    "backhaul-loss",
    "backhaul-recovery",
    "ap-unavailable",
    "ap-recovery",
}


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(value, reason):
    if not value:
        raise ValueError(reason)


def validate(path):
    result = read(path / "result.json")
    require(
        result["status"] == "passed" and result.get("elapsed_seconds", 0) > 0,
        "Incomplete result: " + path.name,
    )
    require(result.get("acceptance_version") == 1, "Preliminary acceptance")
    require(
        result["initiating_interface"] == "semantic"
        and not result["real_easymesh_exchange"]
        and not result["physical_pod"],
        "Unexpected scope",
    )
    cases = result["cases"]
    require(cases.keys() == CASES, "Incomplete case selection")
    require(cases["withheld"]["operation"]["state"] == "TIMED_OUT", "Withholding was not detected")
    late = cases["late-application"]["operation"]
    require(
        late["state"] == "TIMED_OUT" and late["late_resolution"] == "applied_after_deadline",
        "Late observation erased the timeout",
    )
    lost = cases["lost-reply"]["operation"]
    require(
        lost["state"] == "OBSERVED_APPLIED"
        and lost["commit_evidence"]["attribution"] == "unknown"
        and len(lost["attempts"]) == 1,
        "Lost-reply attribution/retry mismatch",
    )
    require(
        cases["wrong-key"]["fresh_wrong_key_event"] and not cases["wrong-key"]["authenticated"],
        "Wrong-key rejection missing",
    )
    require(
        cases["unsupported-channel"]["requested"] == 11
        and cases["unsupported-channel"]["observed"] == 6,
        "Unsupported channel was applied",
    )
    restart = cases["database-manager-restart"]
    require(restart["new_generation"] > restart["previous_generation"], "No new database session")
    require(
        cases["backhaul-loss"]["both_clients_detected_failure"]
        and cases["backhaul-loss"]["radio_state_enabled"],
        "Missing data-path negative control",
    )
    require(cases["ap-unavailable"]["state_enabled"] is False, "Stale positive AP State")
    packets = read(path / "packet-observations.json")
    require(packets["radio_pcap_sha256"] == sha(path / "radio.pcap"), "Capture hash differs")
    required_ssids = {
        "emosa-radio-initial",
        "emosa-radio-changed",
        "emosa-radio-withheld",
        "emosa-radio-lost-reply",
    }
    require(required_ssids <= packets["beacon_counts"].keys(), "Missing observed SSID")
    eapol = (path / "eapol.tsv").read_text()
    require(
        {"1", "2", "3", "4"} <= {line.split("\t")[-1] for line in eapol.splitlines()},
        "Incomplete independent WPA capture",
    )
    clients = {
        p.stem.removeprefix("clients-"): read(p) for p in sorted(path.glob("clients-*.json"))
    }
    require(
        len(clients) == 11 and len({v["nonce"] for v in clients.values()}) == 11,
        "Missing or reused client nonces",
    )
    for value in clients.values():
        require(
            set(value["observations"]) == {"em-baseline-wired", "em-baseline-wifi"},
            "Missing independent client",
        )
        for name, item in value["observations"].items():
            require(
                item["elapsed_seconds"] <= 30 and item["application"]["nonce"] == value["nonce"],
                "Client deadline or freshness failure",
            )
            expected = "192.0.2.21" if name.endswith("wifi") else "192.0.2.20"
            require(item["application"]["peer"] == expected, "Wrong application peer")
            if name.endswith("wifi"):
                require(
                    item["supplicant"]["bssid"] == "02:00:00:ec:02:00"
                    and item["supplicant"]["wpa_state"] == "COMPLETED",
                    "Wrong Wi-Fi path",
                )
    return {
        "result": result,
        "packets": packets,
        "clients": clients,
        "source_hashes": read(path / "source-hashes.json"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--selected", nargs="+", required=True)
    args = parser.parse_args()
    root, output = args.private_root, args.output
    selected = {name: validate(root / "runs" / name) for name in args.selected}
    history = [read(p) for p in sorted((root / "runs").glob("*/result.json"))]
    require(all(r["status"] != "running" for r in history), "An attempt is running")
    private_manifest = {
        str(p.relative_to(root)): {"sha256": sha(p), "size": p.stat().st_size}
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != "private-manifest.json"
    }
    private_file = root / "private-manifest.json"
    private_file.write_text(json.dumps(private_manifest, indent=2) + "\n")
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "semantic_ovsdb_hwsim_observed",
        "selected_runs": selected,
        "history": history,
        "runtime": read(root / "runtime.json"),
        "private_manifest_sha256": sha(private_file),
        "scope": {
            "real_ovsdb": True,
            "independent_radio_manager": True,
            "independent_clients": True,
            "easymesh_onboarding": False,
            "opensync_firmware": False,
            "physical_pod": False,
        },
        "limits": [
            "One synthetic sole-BSS profile, wired management, finite repetitions.",
            "Full hostapd restart interrupts Wi-Fi when applying configuration.",
            "Periodic observations do not establish a production State freshness lease.",
            "IEEE-dependent wire binding and physical qualification remain pending.",
        ],
    }
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    sample = output / "sample"
    sample.mkdir(exist_ok=True)
    source = root / "runs" / args.selected[-1]
    allowlist = [
        "result.json",
        "manager.jsonl",
        "operations.json",
        "packet-observations.json",
        "eapol.tsv",
        "radio.pcap",
        "wrong-key.log",
        "backhaul-loss.json",
        "source-hashes.json",
    ]
    allowlist += [p.name for p in sorted(source.glob("clients-*.json"))]
    for name in allowlist:
        shutil.copyfile(source / name, sample / name)
    (sample / "index.json").write_text(
        json.dumps(
            {
                "source_run": args.selected[-1],
                "scope": "Synthetic traffic only; private config, vault and database excluded.",
                "files": {name: sha(sample / name) for name in allowlist},
            },
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps({"selected": len(selected), "cases_each": len(CASES), "history": len(history)})
    )


if __name__ == "__main__":
    main()
