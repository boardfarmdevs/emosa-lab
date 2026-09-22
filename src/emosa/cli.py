import argparse
import asyncio
import json
import sys
from pathlib import Path

from emosa.app import serve
from emosa.config import load
from emosa.errors import EmosaError, Reason
from emosa.local_api import request
from emosa.secrets import redact


def output(value):
    print(json.dumps(redact(value), indent=2, ensure_ascii=False))


def error_exit(exc):
    output({"schema_version": 1, "error": exc.public()})
    if exc.details.get("wait_timeout"):
        return 3
    if exc.details.get("service_unavailable"):
        return 4
    if exc.code == Reason.INVALID_INPUT:
        return 2
    if exc.code in {
        Reason.UNSUPPORTED_OPERATION,
        Reason.MISSING_PREREQUISITE,
        Reason.OWNERSHIP_CONFLICT,
        Reason.PRECONDITION_FAILED,
        Reason.BUSY,
    }:
        return 5
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="emosa",
        description="EasyMesh to OpenSync Adapter; semantic diagnostics are component tests",
    )
    parser.add_argument("--socket", default="/run/emosa/control.sock")
    sub = parser.add_subparsers(dest="command", required=True)
    service = sub.add_parser("serve")
    service.add_argument("--config", required=True)
    qualify = sub.add_parser(
        "qualify-pod", help="collect a read-only draft; never enable pod writes"
    )
    qualify.add_argument("--connection", required=True, help="validated local connection JSON")
    qualify.add_argument("--output", required=True, help="new private evidence directory")
    for name in ("status", "pods", "agents", "quiesce"):
        sub.add_parser(name).add_argument("--json", action="store_true")
    pod = sub.add_parser("pod")
    pod.add_argument("pod_id")
    pod.add_argument(
        "view",
        choices=(
            "capabilities",
            "radios",
            "bsses",
            "clients",
            "radio-scope",
            "topology",
            "radio-capabilities",
        ),
    )
    pod.add_argument("--json", action="store_true")
    ownership = sub.add_parser("ownership")
    ownership.add_argument("action", choices=["status"])
    ownership.add_argument("--pod", required=True)
    for name in ("plan", "component-submit"):
        p = sub.add_parser(name)
        p.add_argument(
            "--intent-file",
            required=True,
            help="JSON intent with secret_ref, no plaintext credential",
        )
        if name == "component-submit":
            p.add_argument("--idempotency-key", required=True)
            p.add_argument("--run-id", required=True)
            p.add_argument("--apply-seconds", type=float, default=30)
            p.add_argument("--wait", type=float)
    op = sub.add_parser("operation")
    op.add_argument("action", choices=["show", "cancel", "wait"])
    op.add_argument("operation_id")
    op.add_argument("--timeout", type=float, default=10)
    op.add_argument("--json", action="store_true")
    events = sub.add_parser("events")
    events.add_argument("--run-id", required=True)
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        if args.command == "qualify-pod":
            from emosa.qualification import collect

            result = asyncio.run(collect(load("qualification", args.connection), args.output))
            output(result)
            return 5 if result["identifier_cross_check"] == "mismatch" else 0
        if args.command == "serve":
            asyncio.run(serve(load("config", args.config)))
            return 0
        params = {}
        method = args.command
        if method == "pod":
            method = {
                "capabilities": "capabilities",
                "radio-scope": "radio.scope",
                "topology": "topology",
                "radio-capabilities": "radio.capabilities",
            }.get(args.view, "inventory")
            params = {"pod_id": args.pod_id}
        elif method == "ownership":
            params = {"pod_id": args.pod}
        elif method in {"plan", "component-submit"}:
            try:
                intent = json.loads(Path(args.intent_file).read_text())
            except (ValueError, OSError) as exc:
                raise EmosaError(Reason.INVALID_INPUT, "cannot read intent JSON") from exc
            params = {"intent": intent}
            if method == "component-submit":
                method = "component.submit"
                params.update(
                    idempotency_key=args.idempotency_key,
                    run_id=args.run_id,
                    apply_seconds=args.apply_seconds,
                )
        elif method == "operation":
            method = "operation." + args.action
            params = {"operation_id": args.operation_id}
            if args.action == "wait":
                params["timeout"] = args.timeout
        elif method == "events":
            params = {"run_id": args.run_id, "after": args.after, "limit": args.limit}
        result = asyncio.run(request(args.socket, method, params))
        if args.command == "component-submit" and args.wait is not None:
            result = asyncio.run(
                request(
                    args.socket,
                    "operation.wait",
                    {"operation_id": result["operation_id"], "timeout": args.wait},
                )
            )
        if args.command == "pod" and args.view not in {
            "capabilities",
            "radio-scope",
            "topology",
            "radio-capabilities",
        }:
            result = {"pod_id": args.pod_id, "fresh": result["fresh"], args.view: result[args.view]}
        output(result)
        if (
            args.command == "pod"
            and args.view in {"topology", "radio-capabilities"}
            and not result["ready"]
        ):
            return 5
        if isinstance(result, dict) and result.get("state") in {
            "REJECTED",
            "OWNERSHIP_CONFLICT",
            "FAILED",
        }:
            return 5
        return 0
    except EmosaError as exc:
        return error_exit(exc)
    except KeyboardInterrupt:
        return 130


def controller_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="em-controller", description="EasyMesh controller endpoint (P0 blocked)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status").add_argument("--json", action="store_true")
    sub.add_parser("agents")
    service = sub.add_parser("serve")
    service.add_argument("--config", required=True)
    provision = sub.add_parser("provision")
    provision.add_argument("--agent", required=True)
    provision.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    if args.command == "status":
        output(
            {
                "schema_version": 1,
                "protocol_state": "blocked",
                "gate": "P0",
                "wire_procedures": [],
                "virtual_agents": [],
                "reason": "normative matrix and independent vectors missing",
            }
        )
        return 0
    return error_exit(
        EmosaError(
            Reason.MISSING_PREREQUISITE,
            "P0: selected normative matrix and vectors required; no wire exchange performed",
        )
    )


if __name__ == "__main__":
    sys.exit(main())
