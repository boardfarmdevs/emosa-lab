import asyncio
import copy
import random
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from emosa.clock import Clock, ManualClock, utc_now
from emosa.config import validate
from emosa.errors import EmosaError, Reason
from emosa.model import Intent, State
from emosa.opensync.mapping import OpenSyncBackend
from emosa.opensync.session import OvsSession
from emosa.reconcile import Engine
from emosa.secrets import SecretStore, redact
from emosa.store import Store
from emosa_lab.backends.mock import ModelBackend
from emosa_lab.evaluation.evidence import (
    artifact,
    build_manifest,
    html_report,
    markdown_report,
    write_json,
)
from emosa_lab.simulation.database import SimDatabase, SimManager


def prerequisites(scenario, backend):
    missing = []
    if scenario.get("initiating_interface", "easymesh-wire") == "easymesh-wire":
        missing.append(
            {
                "gate": "P0",
                "status": "blocked",
                "reason": "exact normative matrix, independent vectors "
                "and wire implementation required",
            }
        )
    if backend == "hardware":
        missing.append(
            {
                "gate": "M0",
                "status": "blocked",
                "reason": "actual pod/schema/trust/ownership/client qualification missing",
            }
        )
    if backend == "opensync-native":
        missing.append(
            {
                "gate": "R0",
                "status": "blocked",
                "reason": "native manager/dummy-driver backend not qualified",
            }
        )
    required = scenario["required_backend"]
    if required != backend and not (
        required == "any-simulation" and backend in {"model", "ovsdb-sim"}
    ):
        missing.append(
            {
                "gate": "backend",
                "status": "blocked",
                "reason": "selected backend incompatible with scenario",
            }
        )
    for gate in scenario["prerequisites"]:
        if gate in {"P0", "M0", "X1", "R0"} and not any(m["gate"] == gate for m in missing):
            missing.append(
                {
                    "gate": gate,
                    "status": "blocked",
                    "reason": "required qualification evidence unavailable",
                }
            )
        elif gate not in {"P0", "M0", "X1", "R0", "synthetic-existing-bss-v1"}:
            missing.append(
                {
                    "gate": gate,
                    "status": "blocked",
                    "reason": "unknown prerequisite cannot be assumed satisfied",
                }
            )
    if scenario["intent"]["pod_id"] not in scenario["target_allowlist"]:
        raise EmosaError(Reason.INVALID_INPUT, "intent pod is outside scenario allowlist")
    if backend in {"model", "ovsdb-sim"} and scenario["cleanup"] != "dispose-simulation":
        raise EmosaError(Reason.INVALID_INPUT, "simulation requires explicit disposable cleanup")
    points = {
        "none": "after-config-commit",
        "lost-reply": "transaction-reply",
        "guard-conflict": "before-guard",
        "withhold": "after-config-commit",
        "reject": "after-config-commit",
        "partial": "after-config-commit",
        "compete": "after-config-commit",
        "restart-controller": "after-config-commit",
        "restart-server": "after-config-commit",
    }
    if scenario["fault"]["point"] != points[scenario["fault"]["action"]]:
        raise EmosaError(
            Reason.UNSUPPORTED_OPERATION, "fault action is not implemented at the selected boundary"
        )
    for required in {"pcap", "independent-client"} & set(scenario["evidence_required"]):
        missing.append(
            {
                "gate": "evidence:" + required,
                "status": "blocked",
                "reason": "required evidence cannot be produced by the available component backend",
            }
        )
    return missing


def all_events(store, run_id):
    events, after = [], 0
    while batch := store.events(run_id, after, 500):
        events.extend(batch)
        after = batch[-1]["sequence"]
    return events


async def run(scenario, *, backend="ovsdb-sim", root=Path(".lab"), announce=None, trial=1):
    scenario = copy.deepcopy(scenario)
    scenario.setdefault("initiating_interface", "easymesh-wire")
    validate("scenario", scenario)
    missing = prerequisites(scenario, backend)
    run_id = "run-" + uuid.uuid4().hex[:16]
    directory = Path(root) / "runs" / run_id
    directory.mkdir(parents=True, mode=0o700)
    store = Store(directory / "state")
    result = {
        "schema_version": 1,
        "run_id": run_id,
        "scenario_id": scenario["id"],
        "execution_status": "running",
        "verdict": "inconclusive",
        "interoperability_verdict": "blocked" if missing else "not_evaluated",
        "manifest": {
            **build_manifest(),
            "scenario": scenario,
            "backend_mode": backend,
            "initiating_interface": scenario["initiating_interface"],
            "trial": trial,
            "started_at": utc_now(),
            "mapping_version": "synthetic-existing-bss-v1",
            "schema_fingerprints": {},
            "pod_identities": scenario["target_allowlist"],
            "controller": "component-runner"
            if scenario["initiating_interface"] == "semantic"
            else "not_started",
            "virtual_agent": None,
            "topology": scenario["topology"],
            "resource_budget": {
                "writes_per_pod": 1,
                "queued_modifying_requests": 0,
                "ovsdb_requests_per_session": 16,
                "monitor_rows": 10000,
            },
        },
        "checks": [],
        "coverage": {
            "selected": scenario["requirements"],
            "eligible": 0,
            "attempted": 0,
            "passing": 0,
            "omitted": ["all wire, RF, hardware and external-peer acceptance"],
        },
        "operations": [],
        "timings": {
            "clock": "single-process-monotonic",
            "samples": [],
            "client_recovery": None,
            "management_interruption": None,
        },
        "prerequisites": missing,
        "cleanup": {"status": "pending"},
        "artifacts": [],
        "limitations": [
            "Semantic component testing does not evaluate "
            "EasyMesh provisioning or interoperability.",
            "No physical radio or independent Wi-Fi client evidence.",
            "Upstream reference schema and synthetic identities do not qualify a pod build.",
            "Lost-reply injection discards a real response "
            "at adapter ingress before interpretation.",
            "Simulation event triggers are controlled; OS scheduling is not deterministic.",
        ],
        "seamlessness": {
            dimension: "not_evaluated"
            for dimension in (
                "controller_workflow",
                "pod_compatibility",
                "semantic_accuracy",
                "operational_continuity",
                "timing_and_observability",
            )
        },
    }
    store.save_run(result)
    write_json(directory / "inputs.json", scenario)
    if announce:
        announce(run_id)
    backends, databases, managers = {}, {}, {}
    observations = []
    started = time.monotonic()
    clock = ManualClock() if backend == "model" else Clock()
    if backend == "model":
        result["timings"]["clock"] = "deterministic-model-clock; UTC origin 2026-01-01"
    operation_started = {}
    try:
        async with asyncio.timeout(scenario["deadlines"]["overall_seconds"]):
            if missing:
                result["execution_status"], result["verdict"] = "blocked", "blocked"
                store.event(run_id, "BLOCKED", {"reason": "MISSING_PREREQUISITE", "gates": missing})
            else:
                vault = SecretStore(directory / "secrets")
                rng = random.Random(scenario["seed"])
                # Synthetic credentials are private files; the seed is not used for security.
                import secrets

                vault.write_simulated(
                    scenario["intent"]["secret_ref"], "sim-" + secrets.token_hex(12)
                )
                for pod_id in scenario["target_allowlist"]:
                    if backend == "model":
                        backends[pod_id] = ModelBackend(pod_id, vault, clock)
                    else:
                        db = SimDatabase()
                        databases[pod_id] = db
                        await db.start()
                        await db.seed()
                        manager = SimManager(db.endpoint)
                        managers[pod_id] = manager
                        await manager.start()
                        backends[pod_id] = OpenSyncBackend(pod_id, OvsSession(db.endpoint), vault)
                engine = Engine(store, vault, backends, clock)
                fault = scenario["fault"]["action"]
                affected_pod = scenario["intent"]["pod_id"]
                for pod_id, target in backends.items():
                    snap = await target.snapshot()
                    result["manifest"]["schema_fingerprints"][pod_id] = snap.schema_fingerprint
                    observations.append({"phase": "initial", **asdict(snap.observed)})
                    store.event(
                        run_id,
                        "READY",
                        {
                            "generation": snap.generation,
                            "backend_mode": backend,
                            "fresh": snap.observed.fresh,
                            "schema_fingerprint": snap.schema_fingerprint,
                        },
                        pod_id,
                    )
                    if pod_id == affected_pod:
                        if backend == "model":
                            target.fault = (
                                fault if fault in {"lost-reply", "guard-conflict"} else "none"
                            )
                        else:
                            target.drop_next_reply = fault == "lost-reply"
                            if fault == "guard-conflict":
                                target.before_transaction = lambda: managers[affected_pod].command(
                                    "compete"
                                )
                    intent = replace(Intent(**scenario["intent"]), pod_id=pod_id)
                    op = engine.request(
                        intent,
                        source="component-runner",
                        key="initial-change",
                        run_id=run_id,
                        deadline=scenario["deadlines"]["apply_seconds"],
                    )
                    operation_started[op.operation_id] = clock.monotonic()
                    await engine.execute(op.operation_id)
                if any(p.state == State.OBSERVED_APPLIED for p in store.operations()):
                    raise AssertionError("application reported before independent manager action")
                store.event(
                    run_id,
                    "BEFORE_DEVICE_APPLICATION",
                    {"fault": scenario["fault"], "seed_sample": rng.randrange(1000000)},
                )
                if fault == "restart-controller":
                    store.close()
                    store = Store(directory / "state")
                    engine = Engine(store, vault, backends, clock)
                    engine.recover()
                    store.event(run_id, "CONTROLLER_RESTART", {"journal_reloaded": True})
                if fault == "restart-server":
                    if backend == "model":
                        backends[affected_pod].ready = False
                        backends[affected_pod].reconnect()
                    else:
                        await managers[affected_pod].close()
                        await databases[affected_pod].stop()
                        await backends[affected_pod].snapshot()
                        await databases[affected_pod].start()
                        managers[affected_pod] = await SimManager(
                            databases[affected_pod].endpoint
                        ).start()
                duration = scenario["fault"]["duration_seconds"]
                if backend == "model":
                    clock.advance(duration)
                elif duration:
                    await asyncio.sleep(duration)
                for pod_id, target in backends.items():
                    action = fault if pod_id == affected_pod else "none"
                    if backend == "model":
                        if action == "compete":
                            target.competing_writer()
                        elif action not in {"withhold", "reject", "guard-conflict"}:
                            target.device_step(partial=action == "partial")
                        if not target.ready:
                            target.reconnect()
                    else:
                        if action == "compete":
                            await managers[pod_id].command("compete")
                        elif action not in {"withhold", "reject", "guard-conflict"}:
                            event = await managers[pod_id].command(
                                "partial" if action == "partial" else "apply"
                            )
                            store.event(run_id, "SIMULATED_MANAGER_FEEDBACK", event, pod_id)
                        elif action in {"withhold", "reject"}:
                            store.event(
                                run_id,
                                "SIMULATED_MANAGER_FEEDBACK",
                                await managers[pod_id].command(action),
                                pod_id,
                            )
                overall = started + scenario["deadlines"]["overall_seconds"]
                while True:
                    await asyncio.gather(*(engine.reconcile(pod_id) for pod_id in backends))
                    pending = [
                        op
                        for op in store.operations()
                        if op.state in {State.CONFIG_COMMITTED, State.INDETERMINATE}
                        and not op.deadline_elapsed
                    ]
                    if not pending or time.monotonic() >= overall:
                        break
                    if backend == "model":
                        clock.advance(0.05)
                    else:
                        await asyncio.sleep(0.01)
                for target in backends.values():
                    snap = await target.snapshot()
                    observations.append({"phase": "final", **asdict(snap.observed)})
                for op in store.operations():
                    expected = (
                        scenario["expected"]["state"]
                        if op.pod_id == affected_pod
                        else "OBSERVED_APPLIED"
                    )
                    expected_attribution = (
                        scenario["expected"]["commit_attribution"]
                        if op.pod_id == affected_pod
                        else "any"
                    )
                    passed = op.state == expected and (
                        expected_attribution == "any"
                        or op.commit_evidence["attribution"] == expected_attribution
                    )
                    if expected == "OBSERVED_APPLIED" and op.deadline_elapsed:
                        passed = False
                    result["checks"].append(
                        {
                            "id": op.pod_id + ":operation",
                            "verdict": "pass" if passed else "fail",
                            "expected": expected,
                            "observed": op.state,
                            "evidence_refs": ["events.json", "observations.json"],
                        }
                    )
                    result["timings"]["samples"].append(
                        {
                            "operation_id": op.operation_id,
                            "request_to_final_observation_seconds": clock.monotonic()
                            - operation_started[op.operation_id],
                            "request_to_commit_seconds": op.timings.get("CONFIG_COMMITTED", {}).get(
                                "seconds_since_request"
                            ),
                            "request_to_application_seconds": op.timings.get(
                                "OBSERVED_APPLIED", {}
                            ).get("seconds_since_request"),
                            "operation_phase_clocks": op.timings,
                            "independent_client_seconds": None,
                        }
                    )
                result["operations"] = [redact(p.to_dict()) for p in store.operations()]
                result["verdict"] = (
                    "pass" if all(c["verdict"] == "pass" for c in result["checks"]) else "fail"
                )
                result["execution_status"] = "completed"
                result["coverage"].update(
                    eligible=len(result["checks"]),
                    attempted=len(result["checks"]),
                    passing=sum(c["verdict"] == "pass" for c in result["checks"]),
                )
    except (EmosaError, OSError, TimeoutError) as exc:
        result["execution_status"] = (
            "blocked"
            if isinstance(exc, EmosaError) and exc.code == Reason.MISSING_PREREQUISITE
            else "failed"
        )
        result["verdict"] = "blocked" if result["execution_status"] == "blocked" else "fail"
        reason = (
            exc.public()
            if isinstance(exc, EmosaError)
            else {"code": "NOT_READY", "message": type(exc).__name__}
        )
        result["prerequisites"].append(reason)
        store.event(run_id, "ERROR", {"reason": reason})
    except (asyncio.CancelledError, KeyboardInterrupt):
        result["execution_status"], result["verdict"] = "interrupted", "inconclusive"
        result["limitations"].append("Run interrupted; operation journal retained for inspection.")
    except Exception as exc:
        result["execution_status"], result["verdict"] = "failed", "fail"
        result["limitations"].append("Runner error: " + type(exc).__name__)
        store.event(run_id, "RUNNER_ERROR", {"error_type": type(exc).__name__})
    finally:
        cleanup_errors = []
        for resource in [*managers.values(), *backends.values(), *databases.values()]:
            try:
                await resource.close()
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        result["cleanup"] = {
            "status": "pending_recovery" if cleanup_errors else "completed",
            "policy": scenario["cleanup"],
            "errors": cleanup_errors,
            "physical_pod_changes": False,
        }
        if cleanup_errors:
            result["execution_status"], result["verdict"] = "failed", "inconclusive"
        result["operations"] = [redact(p.to_dict()) for p in store.operations()]
        events = all_events(store, run_id)
        write_json(directory / "events.json", events)
        write_json(directory / "observations.json", observations)
        result["artifacts"] = [
            artifact(directory / name, directory)
            for name in ("inputs.json", "events.json", "observations.json")
        ]
        result["manifest"]["finished_at"] = utc_now()
        result["timings"]["runner_wall_seconds"] = time.monotonic() - started
        validate("run-result", result)
        store.save_run(result)
        write_json(directory / "run.json", result)
        (directory / "report.md").write_text(markdown_report(result, events))
        (directory / "report.html").write_text(html_report(result, events))
        write_json(
            directory / "artifact-manifest.json",
            {
                "schema_version": 1,
                "run_id": run_id,
                "artifacts": [
                    artifact(p, directory) for p in sorted(directory.iterdir()) if p.is_file()
                ],
            },
        )
        store.close()
    return result
