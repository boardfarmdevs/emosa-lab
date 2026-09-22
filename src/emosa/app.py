import asyncio
import contextlib
import signal
from dataclasses import asdict
from pathlib import Path

from emosa import __version__
from emosa.agents import AgentDirectory, validate_bindings
from emosa.backends.mock import ModelBackend
from emosa.capabilities import capabilities
from emosa.clock import Clock
from emosa.errors import EmosaError, Reason
from emosa.local_api import LocalServer
from emosa.model import Intent
from emosa.opensync.mapping import OpenSyncBackend
from emosa.opensync.session import OvsSession
from emosa.opensync.tls_listener import server_context
from emosa.qualification import private_reference
from emosa.reconcile import Engine
from emosa.secrets import SecretStore
from emosa.store import Store


class Application:
    def __init__(self, config):
        validate_bindings(config)
        self.config = config
        # Validate/load every context before acquiring the journal lock or workers.
        tls_options = {}
        for pod in config["pods"]:
            if "tls" in pod:
                trust = pod["tls"]
                files = {
                    key: str(private_reference(config["secret_directory"], trust[ref]))
                    for key, ref in (
                        ("certificate", "certificate_ref"),
                        ("private_key", "private_key_ref"),
                        ("ca", "ca_ref"),
                    )
                }
                server_context(files)
                tls_options[pod["pod_id"]] = {
                    "tls_files": files,
                    "peer_certificate_sha256": trust["peer_certificate_sha256"],
                }
        self.agents = AgentDirectory(config["pods"], config["backend_mode"])
        self.store = Store(Path(config["state_directory"]))
        try:
            topology_bindings = self.store.pin_topologies(config["pods"])
        except BaseException:
            self.store.close()
            raise
        self.vault = SecretStore(Path(config["secret_directory"]))
        self.clock = Clock()
        self.backends = {}
        for pod in config["pods"]:
            pod_id = pod["pod_id"]
            if config["backend_mode"] == "model":
                self.backends[pod_id] = ModelBackend(pod_id, self.vault, self.clock)
            elif config["backend_mode"] == "ovsdb-sim":
                self.backends[pod_id] = OpenSyncBackend(
                    pod_id,
                    OvsSession(
                        pod["endpoint"],
                        pod["database"],
                        request_limit=config.get("limits", {}).get("ovsdb_requests", 16),
                        **tls_options.get(pod_id, {}),
                    ),
                    self.vault,
                    if_name=pod["if_name"],
                    radio_name=pod["radio_name"],
                    bss_id=pod["bss_id"],
                    radio_id=pod["radio_id"],
                    expected_serial=pod.get("virtual_agent", {}).get("expected_serial"),
                    mapping_scope=pod.get("mapping_scope", "existing-bss"),
                    topology_binding=topology_bindings.get(pod_id),
                    radio_capabilities=pod.get("radio_capabilities"),
                    state_provenance=pod.get(
                        "state_provenance", "independent-simulated-manager:Wifi_VIF_State"
                    ),
                )
            else:
                raise EmosaError(Reason.MISSING_PREREQUISITE, "backend qualification pending")
        self.engine = Engine(self.store, self.vault, self.backends, self.clock)
        self.engine.recover()
        self.tasks = set()
        self.cache = {
            pod: {"pod_id": pod, "ready": False, "reason": "NOT_READY"} for pod in self.backends
        }
        self.inventories = {}
        self.server = LocalServer(
            config["socket_path"],
            self.handle,
            frame_limit=config.get("limits", {}).get("local_frame_bytes", 65536),
            client_limit=config.get("limits", {}).get("local_clients", 16),
        )

    async def refresh(self, pod_id):
        while True:
            try:
                snap = await self.engine.reconcile(pod_id)
                conflict = self.store.ownership(pod_id)
                self.cache[pod_id] = {
                    "pod_id": pod_id,
                    "ready": snap.ready,
                    "schema_fingerprint": snap.schema_fingerprint,
                    "ownership": "conflict" if conflict else "simulation_scope",
                    "generation": snap.generation,
                    "observation": asdict(snap.observed),
                    "capabilities": capabilities(
                        snap.ready
                        and self.config["write_mode"] == "managed-fields"
                        and not self.engine.quiesced,
                        bool(conflict),
                    ),
                }
                backend = self.backends[pod_id]
                if hasattr(backend, "inventory"):
                    self.inventories[pod_id] = await backend.inventory()
                    inventory = self.inventories[pod_id]
                    if (
                        snap.ready
                        and inventory["ready"]
                        and inventory["generation"] == snap.generation
                    ):
                        self.agents.observe(pod_id, inventory)
                    else:
                        self.agents.invalidate(pod_id)
                else:
                    self.inventories[pod_id] = {
                        "pod_id": pod_id,
                        "source": "OpenSync",
                        "backend_mode": "model",
                        "radios": [],
                        "bsses": [asdict(snap.observed)],
                        "clients": [],
                    }
            except (EmosaError, ConnectionError, TimeoutError) as exc:
                self.agents.invalidate(
                    pod_id, exc.code if isinstance(exc, EmosaError) else "NOT_READY"
                )
                self.cache[pod_id]["ready"] = False
                self.cache[pod_id]["reason"] = (
                    exc.code if isinstance(exc, EmosaError) else "NOT_READY"
                )
                if "observation" in self.cache[pod_id]:
                    self.cache[pod_id]["observation"]["fresh"] = False
            await asyncio.sleep(0.05)

    def _task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def start(self):
        await self.server.start()
        for pod_id in self.backends:
            self._task(self.refresh(pod_id))
        return self

    async def handle(self, method, params):
        if method == "status":
            return {
                "schema_version": 1,
                "software_version": __version__,
                "backend_mode": self.config["backend_mode"],
                "management_backend": "opensync_ovsdb",
                "protocol": {"state": "blocked", "reason": "P0 normative matrix missing"},
                "quiesced": self.engine.quiesced,
                "write_mode": self.config["write_mode"],
                "pods": list(self.cache.values()),
            }
        if method == "pods":
            offset, limit = params.get("offset", 0), params.get("limit", 100)
            return {"pods": list(self.cache.values())[offset : offset + limit], "offset": offset}
        if method == "agents":
            return self.agents.view(offset=params.get("offset", 0), limit=params.get("limit", 100))
        if method in {
            "inventory",
            "capabilities",
            "ownership",
            "radio.scope",
            "topology",
            "radio.capabilities",
        }:
            pod_id = params["pod_id"]
            if pod_id not in self.backends:
                raise EmosaError(Reason.NOT_FOUND, "unknown configured pod")
            if method == "radio.capabilities":
                backend = self.backends[pod_id]
                if not hasattr(backend, "radio_capabilities"):
                    raise EmosaError(
                        Reason.UNSUPPORTED_OPERATION, "radio capabilities require OVSDB inventory"
                    )
                return {
                    **await backend.radio_capabilities(),
                    "adapter_instance_id": self.agents.instance_id,
                }
            if method == "topology":
                backend = self.backends[pod_id]
                if not hasattr(backend, "topology"):
                    raise EmosaError(
                        Reason.UNSUPPORTED_OPERATION, "topology requires OVSDB inventory"
                    )
                return {
                    **await backend.topology(),
                    "adapter_instance_id": self.agents.instance_id,
                }
            if method == "radio.scope":
                backend = self.backends[pod_id]
                if not hasattr(backend, "radio_scope"):
                    raise EmosaError(
                        Reason.UNSUPPORTED_OPERATION, "radio scope requires OVSDB inventory"
                    )
                return await backend.radio_scope()
            if method == "ownership":
                return {
                    "pod_id": pod_id,
                    "conflict": self.store.ownership(pod_id),
                    "scope": "simulation only",
                    "write_mode": self.config["write_mode"],
                }
            if method == "capabilities":
                return capabilities(
                    self.cache[pod_id]["ready"]
                    and self.config["write_mode"] == "managed-fields"
                    and not self.engine.quiesced,
                    bool(self.store.ownership(pod_id)),
                )
            if pod_id not in self.inventories:
                raise EmosaError(Reason.NOT_READY, "inventory synchronization pending")
            return {**self.inventories[pod_id], "fresh": self.cache[pod_id]["ready"]}
        if method == "events":
            return {
                "events": self.store.events(
                    params["run_id"], params.get("after", 0), params.get("limit", 100)
                )
            }
        if method == "plan":
            return await self.engine.plan(Intent(**params["intent"]))
        if method == "operation.show":
            return self.store.get(params["operation_id"]).to_dict()
        if method == "operation.wait":
            return (await self.engine.wait(params["operation_id"], params["timeout"])).to_dict()
        if method == "quiesce":
            self.engine.quiesced = True
            return {"quiesced": True, "pod_configuration_restored": False}
        if self.config["write_mode"] != "managed-fields":
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "service is read-only")
        if method == "operation.cancel":
            return self.engine.cancel(params["operation_id"]).to_dict()
        if method == "component.submit":
            op = self.engine.request(
                Intent(**params["intent"]),
                source=self.config["request_source"],
                key=params["idempotency_key"],
                run_id=params["run_id"],
                deadline=params.get("apply_seconds", 30),
            )
            self._task(self.engine.execute(op.operation_id))
            return op.to_dict()
        raise EmosaError(Reason.UNSUPPORTED_OPERATION, "unknown local method")

    async def close(self):
        self.engine.quiesced = True
        await self.server.close()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(*(backend.close() for backend in self.backends.values()))
        self.store.close()


async def serve(config):
    app = Application(config)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await app.start()
        await stop.wait()
    finally:
        await app.close()
