import argparse
import asyncio
import html
import json
import sys
import time
from pathlib import Path

from emosa.cli import error_exit, output
from emosa.config import load
from emosa.errors import EmosaError, Reason
from emosa.evaluation.evidence import compare
from emosa.evaluation.lxd import invoke
from emosa.evaluation.payloads import inspect_value, read_value
from emosa.evaluation.profile_audit import audit, markdown, read_features
from emosa.evaluation.runner import all_events, run
from emosa.store import Store
from emosa.wire.inspection import inspect_capture, read_frame


def read_run(root, run_id):
    if not run_id.startswith("run-") or any(
        c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in run_id
    ):
        raise EmosaError(Reason.INVALID_INPUT, "invalid run identifier")
    path = root / "runs" / run_id
    if not path.is_dir():
        raise EmosaError(Reason.NOT_FOUND, "run not found")
    try:
        store = Store(path / "state", read_only=True)
        return path, store
    except OSError as exc:
        raise EmosaError(Reason.NOT_READY, "run journal unavailable") from exc


def main(argv=None):
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="emosa-lab", description="Controlled experiments with explicit evidence level"
    )
    parser.add_argument("--state-dir", type=Path, default=Path(".lab"))
    parser.add_argument("--execution", choices=["local", "lxd"], default="local")
    sub = parser.add_subparsers(dest="command", required=True)
    execute = sub.add_parser("run")
    execute.add_argument("scenario")
    execute.add_argument(
        "--backend",
        choices=["model", "ovsdb-sim", "opensync-native", "hardware"],
        default="ovsdb-sim",
    )
    execute.add_argument("--ssid")
    execute.add_argument("--seed", type=int)
    execute.add_argument("--repeat", type=int)
    execute.add_argument("--apply-seconds", type=float)
    execute.add_argument("--target", help="qualified hardware profile (M0 remains required)")
    for name in ("report", "watch", "inspect"):
        p = sub.add_parser(name)
        p.add_argument("run_id")
        if name == "report":
            p.add_argument("--format", choices=["json", "html", "markdown"], default="json")
        elif name == "inspect":
            p.add_argument("--operation", required=True)
        else:
            p.add_argument("--once", action="store_true")
            p.add_argument("--timeout", type=float, default=60)
    comparison = sub.add_parser("compare")
    comparison.add_argument("run_a")
    comparison.add_argument("run_b")
    comparison.add_argument("--format", choices=["json", "html", "markdown"], default="json")
    payload = sub.add_parser("payload", help="inspect one EasyMesh TLV value offline")
    payload.add_argument("--type", dest="kind", type=lambda value: int(value, 0), required=True)
    value = payload.add_mutually_exclusive_group(required=True)
    value.add_argument("--value-hex", help="value octets only, without a TLV header or frame")
    value.add_argument("--value-file", type=Path, help="regular file containing raw value octets")
    payload.add_argument("--receiver-profile", type=int, choices=(1, 2, 3))
    profile = sub.add_parser(
        "profile-audit", help="inspect Profile-1 requirements without qualifying a profile"
    )
    profile.add_argument("--features", type=Path, help="optional unqualified planning conditions")
    profile.add_argument("--format", choices=("json", "markdown"), default="json")
    wire = sub.add_parser("wire-inspect", help="inspect IEEE 1905 Ethernet bytes without sending")
    wire_input = wire.add_mutually_exclusive_group(required=True)
    wire_input.add_argument("--capture", type=Path, help="classic Ethernet PCAP (not PCAPNG)")
    wire_input.add_argument("--frame", type=Path, help="one Ethernet frame without FCS")
    args = parser.parse_args(argv)
    try:
        if args.command == "wire-inspect":
            if args.execution != "local":
                raise EmosaError(Reason.INVALID_INPUT, "offline wire inspection runs locally")
            try:
                result = inspect_capture(args.capture) if args.capture else read_frame(args.frame)
            except OSError as exc:
                raise EmosaError(Reason.NOT_READY, "wire input file unavailable") from exc
            output(result)
            return (
                1
                if (
                    result.get("rejected")
                    or result.get("expired_assemblies")
                    or result.get("incomplete_or_quarantined")
                    or result.get("unsupported_tagged_frames")
                )
                else 0
            )
        if args.command == "profile-audit":
            if args.execution != "local":
                raise EmosaError(Reason.INVALID_INPUT, "offline profile audit runs locally")
            report = audit(read_features(args.features) if args.features is not None else None)
            if args.format == "json":
                output(report)
            else:
                print(markdown(report), end="")
            return 5  # Requirements remain incomplete; this is not a conformance certifier.
        if args.command == "payload":
            if args.execution != "local":
                raise EmosaError(Reason.INVALID_INPUT, "offline payload inspection runs locally")
            output(
                inspect_value(
                    args.kind,
                    read_value(value_hex=args.value_hex, value_file=args.value_file),
                    receiver_profile=args.receiver_profile,
                )
            )
            return 0
        if args.execution == "lxd":
            for flag in ("--execution", "--state-dir"):
                if flag in raw_arguments:
                    index = raw_arguments.index(flag)
                    del raw_arguments[index : index + 2]
            return invoke(
                raw_arguments, scenario_path=args.scenario if args.command == "run" else None
            )
        if args.command == "run":
            scenario = load("scenario", args.scenario)
            if args.ssid is not None:
                scenario["intent"]["ssid"] = args.ssid
            if args.seed is not None:
                scenario["seed"] = args.seed
            if args.repeat is not None:
                scenario["repetitions"] = args.repeat
            if args.apply_seconds is not None:
                scenario["deadlines"]["apply_seconds"] = args.apply_seconds
            results = []
            if not 1 <= scenario["repetitions"] <= 100:
                raise EmosaError(Reason.INVALID_INPUT, "repetitions must be 1–100")
            for trial in range(1, scenario["repetitions"] + 1):
                result = asyncio.run(
                    run(
                        scenario,
                        backend=args.backend,
                        root=args.state_dir,
                        trial=trial,
                        announce=lambda identifier: print(
                            f"Started {identifier}", file=sys.stderr, flush=True
                        ),
                    )
                )
                results.append(result)
                output(
                    {
                        "run_id": result["run_id"],
                        "execution_status": result["execution_status"],
                        "verdict": result["verdict"],
                        "interoperability_verdict": result["interoperability_verdict"],
                        "artifacts": str(args.state_dir / "runs" / result["run_id"]),
                    }
                )
            if any(r["verdict"] == "blocked" for r in results):
                return 5
            return 0 if all(r["verdict"] == "pass" for r in results) else 1
        if args.command == "compare":
            _, left = read_run(args.state_dir, args.run_a)
            try:
                a = left.run(args.run_a)
            finally:
                left.close()
            _, right = read_run(args.state_dir, args.run_b)
            try:
                comparison = compare(a, right.run(args.run_b))
            finally:
                right.close()
            if args.format == "json":
                output(comparison)
            elif args.format == "html":
                print(
                    "<!doctype html><html lang='en'><meta charset='utf-8'>"
                    "<title>EMOSA run comparison</title>"
                    "<h1>EMOSA run comparison</h1><pre>"
                    + html.escape(json.dumps(comparison, indent=2))
                    + "</pre></html>"
                )
            else:
                print(
                    "# EMOSA comparison\n\n```json\n" + json.dumps(comparison, indent=2) + "\n```"
                )
            return 0
        path, store = read_run(args.state_dir, args.run_id)
        try:
            if args.command == "report":
                if args.format == "json":
                    output(store.run(args.run_id))
                else:
                    filename = "report.html" if args.format == "html" else "report.md"
                    if not (path / filename).exists():
                        raise EmosaError(
                            Reason.NOT_READY, "report available when run finishes; use watch"
                        )
                    print((path / filename).read_text())
            elif args.command == "inspect":
                operation = store.get(args.operation)
                if operation.run_id != args.run_id:
                    raise EmosaError(Reason.NOT_FOUND, "operation does not belong to run")
                output(
                    {
                        "operation": operation.to_dict(),
                        "timeline": [
                            e
                            for e in all_events(store, args.run_id)
                            if e["operation_id"] == args.operation
                            or e["pod_id"] == operation.pod_id
                        ],
                        "artifacts": store.run(args.run_id)["artifacts"],
                    }
                )
            else:
                after = 0
                end = time.monotonic() + min(max(args.timeout, 0), 3600)
                while True:
                    result = store.run(args.run_id)
                    events = store.events(args.run_id, after, 500)
                    for event in events:
                        output(event)
                        after = event["sequence"]
                    output(
                        {
                            "run_id": args.run_id,
                            "backend": result["manifest"]["backend_mode"],
                            "interface": result["manifest"]["initiating_interface"],
                            "execution_status": result["execution_status"],
                            "operations": [op.to_dict() for op in store.operations(args.run_id)],
                            "limitations": result["limitations"],
                        }
                    )
                    if (
                        args.once
                        or result["execution_status"] != "running"
                        or time.monotonic() >= end
                    ):
                        break
                    time.sleep(0.25)
            return 0
        finally:
            store.close()
    except EmosaError as exc:
        return error_exit(exc)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
