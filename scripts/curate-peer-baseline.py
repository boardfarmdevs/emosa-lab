"""Publish reviewed synthetic observations; reject incomplete/weak final matrices."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

CLIENTS = ("em-baseline-wired", "em-baseline-wifi")
KINDS = ("agent-restart", "controller-restart", "backhaul-loss")


def read(path):
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def client_view(data):
    return {
        k: data[k]
        for k in ("client", "interface", "supplicant", "ping", "application", "routes")
        if k in data
    }


def summarize(path, root):
    value = read(path / "result.json")
    keys = (
        "mode",
        "kind",
        "started_utc",
        "status",
        "error",
        "collection_error",
        "phases",
        "elapsed_seconds",
        "shutdown_clean",
        "policy_startup_barrier",
        "controller_restart_scope",
        "acceptance_version",
        "stability",
        "post_client_stability_seconds",
    )
    row = {"id": str(path.relative_to(root)), **{k: value[k] for k in keys if k in value}}
    row["acceptance_version"] = value.get("acceptance_version", 1)
    row["qualification"] = (
        "preliminary; native operational state and stability were not required"
        if row["acceptance_version"] < 2
        else "measured with operational/stability checks"
    )
    row["artifact_hashes"] = {p.name: digest(p) for p in sorted(path.iterdir()) if p.is_file()}
    for name in (
        "protocol-observation",
        "restart-shutdown",
        "helper-shutdown",
        "during-outage",
        "update-rejections",
        "inventory-identity",
        "runtime-reference",
        "em-baseline-agent-agent-state",
        "em-baseline-controller-agent-state",
    ):
        artifact = path / f"{name}.json"
        if artifact.exists():
            row[name] = read(artifact)
    row["clients"] = [
        client_view(read(path / f"{name}-observation.json"))
        for name in CLIENTS
        if (path / f"{name}-observation.json").exists()
    ]
    row["client_batches"] = [
        {"nonce": batch["nonce"], "clients": [client_view(c) for c in batch["clients"].values()]}
        for batch in (read(p) for p in sorted(path.glob("client-probes-*.json")))
    ]
    return row


def validate_selected(row, path, mode):
    label = row["id"]
    require(row["mode"] == mode and row["status"] == "passed", f"Unpassed selection: {label}")
    require(row["acceptance_version"] == 2, f"Weak acceptance cannot qualify {label}")
    require(all(p["seconds"] <= p["budget"] for p in row["phases"].values()), label)
    require(len(row["clients"]) == 2 and len(row["client_batches"]) >= 2, label)
    require(len({b["nonce"] for b in row["client_batches"]}) == len(row["client_batches"]), label)
    for batch in row["client_batches"]:
        require(len(batch["clients"]) == 2, f"Missing independent client in {label}")
        for client in batch["clients"]:
            require(client["application"]["nonce"] == batch["nonce"], label)
    stability = read(path / "post-client-stability.json")
    require(len(stability) >= 10 and stability[-1]["elapsed"] >= 30, label)
    require(all(s["ready"] for s in stability), f"Post-client regression in {label}")
    for name in ("em-baseline-agent", "em-baseline-controller"):
        state = row[f"{name}-agent-state"]["X_PRPLWARE-COM_Agent.Info."]
        require(state["CurrentState"] == "OPERATIONAL (15)", label)
    protocol = row.get("protocol-observation", {})
    require(bool(protocol), f"No independent protocol observations: {label}")
    if row["kind"] == "clean":
        require(all(protocol["handshake_frames"].values()), label)
        if mode == "wireless":
            require(protocol.get("wps_m1_to_m8_observed"), label)
    if mode == "wireless":
        require(protocol.get("four_address_frames", 0) > 0, label)
    if row["kind"] == "controller-restart":
        require(row.get("controller_restart_scope") == ["controller", "local-agent"], label)
        require(row.get("policy_startup_barrier"), label)
    if row["kind"] == "backhaul-loss":
        require(row["phases"]["recovery"].get("required_stable_seconds") == 30, label)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", type=Path, required=True, help="Copied VM runs directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wired-suite", required=True)
    parser.add_argument("--wireless-suite", required=True)
    parser.add_argument("--wired-negative", required=True)
    parser.add_argument("--wireless-negative", required=True)
    parser.add_argument(
        "--wired-outage-prefix", help="Three separately labeled settled outage runs"
    )
    args = parser.parse_args()
    root = args.private_root
    rows = [summarize(p.parent, root) for p in sorted(root.glob("*/*/result.json"))]
    require(not any(r["status"] == "running" for r in rows), "An attempt is still running")
    by_id = {r["id"]: r for r in rows}
    selected, negatives = {}, {}
    for mode, suite, negative in (
        ("wired", args.wired_suite, args.wired_negative),
        ("wireless", args.wireless_suite, args.wireless_negative),
    ):
        selected[mode] = [f"{suite}/clean-{i:02d}" for i in range(1, 6)] + [
            f"{suite}/{kind}-{i:02d}" for kind in KINDS for i in range(1, 4)
        ]
        if mode == "wired" and args.wired_outage_prefix:
            selected[mode][-3:] = [
                f"{args.wired_outage_prefix}{i:02d}/backhaul-loss-01" for i in range(1, 4)
            ]
        for label in selected[mode]:
            require(label in by_id, f"Missing selected attempt: {label}")
            validate_selected(by_id[label], root / label, mode)
        label = f"{negative}/negative-controls"
        require(label in by_id, f"Missing negative controls: {label}")
        row = by_id[label]
        require(row["status"] == "passed" and row["acceptance_version"] == 2, label)
        require(row.get("stability", {}).get("seconds", 0) >= 30, label)
        require("WRONG_KEY" in (root / label / "wrong-key.log").read_text(), label)
        require(len(row.get("update-rejections", {})) == 5, label)
        require(
            all(
                v["reply"] == "FAIL" and v["expected"] == v["observed"]
                for v in row["update-rejections"].values()
            ),
            label,
        )
        negatives[mode] = label
    counts = Counter((r["mode"], r["kind"], r["status"], r["acceptance_version"]) for r in rows)
    summary = {
        "schema_version": 2,
        "status": "bounded_native_onboarding_observed_with_recorded_defects",
        "date": "2026-09-16",
        "source_base_revision": "2fcd99132251a9395035f4e5c2910659945e8964",
        "profile": "deploy/peer-baseline/reference.json",
        "final_procedure_matrix": selected,
        "negative_controls": negatives,
        "counts": [
            {"mode": m, "kind": k, "status": s, "acceptance_version": v, "count": c}
            for (m, k, s, v), c in sorted(counts.items())
        ],
        "attempts": rows,
        "scope": {
            "native_prplmesh_controller_and_agent": True,
            "opensync_or_emosa_in_path": False,
            "physical_pod_accessed": False,
            "normative_conformance_claimed": False,
            "universal_onboarding_claimed": False,
        },
        "known_limits": [
            "Native SIGTERM shutdown repeatedly aborts; functional recovery is a separate result.",
            "Controller restart requires the transport barrier, local helper and BML policy.",
            "Version 1 passes did not establish operational state or stability and are excluded.",
            "An earlier wired outage failed post-client stability as topology inventory changed.",
            "Outage recovery requires 30 continuous healthy seconds inside its 120-second limit.",
            "The native HAL primary-BSS fix and hostap file-UPDATE patch are part of this profile.",
            "One patched prplMesh pair, 2.4 GHz/20 MHz/WPA2, finite repetitions and dwell.",
            "No cross-vendor, VM/container reboot, physical RF or full capability qualification.",
        ],
        "harnesses": {p.parent.name: read(p) for p in sorted(root.glob("*/harness.json"))},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n")
    print(
        json.dumps(
            {
                "selected": {k: len(v) for k, v in selected.items()},
                "retained_attempts": len(rows),
                "negative_controls": negatives,
            }
        )
    )


if __name__ == "__main__":
    main()
