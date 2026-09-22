"""Restricted authenticated-WSC handoff, currently used only by owned simulation.

This module does not grant profile or controller admission. The normal service
and full wire scenario remain closed. No semantic fallback is used: operations
from this component retain their distinct wsc-component origin and atomic receipt.
"""

from dataclasses import dataclass

from emosa.errors import EmosaError, Reason
from emosa.model import Intent
from emosa.wire.cmdu import mac


@dataclass(frozen=True)
class ComponentTarget:
    pod_id: str
    radio_id: str
    bss_id: str
    ruid: bytes
    bssid: bytes

    def __post_init__(self):
        for identifier in (self.pod_id, self.radio_id, self.bss_id):
            if not isinstance(identifier, str) or not 1 <= len(identifier) <= 128:
                raise EmosaError(Reason.INVALID_INPUT, "invalid component resource identity")
        for address in (self.ruid, self.bssid):
            mac(address)
            if address == bytes(6) or address[0] & 1:
                raise EmosaError(Reason.INVALID_INPUT, "component target must be unicast")


@dataclass(frozen=True)
class ScopeContext:
    """Opaque source binding, not a user-set qualification flag or credential."""

    generation: int
    schema_fingerprint: str
    binding_token: str


class WscComponentBridge:
    """One live WSC exchange into at most one durable component operation.

    current_context must verify the entire supported source scope. The owned
    OVSDB fixture additionally pins identity/UUID/generation inside its backend
    and guards the graph atomically at commit. A model callback alone cannot
    qualify a device. There is no public serve method or physical endpoint option.
    """

    def __init__(self, engine, exchange, target, current_context, *, run_id, deadline=30):
        backend = engine.backends.get(target.pod_id)
        if (
            backend is None
            or backend.mode not in ("model", "ovsdb-sim")
            or exchange.basic.ruid != target.ruid
            or exchange.basic.max_bss != 1
        ):
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "component requires one simulated BSS")
        self.engine, self.exchange, self.target = engine, exchange, target
        self.backend, self.current_context = backend, current_context
        self.run_id, self.deadline = run_id, deadline
        self.process_id = engine.store.process_id
        self.context = self.operation_id = None
        self.receiving = False

    async def _bound(self):
        if self.engine.store.process_id != self.process_id:
            raise EmosaError(Reason.NOT_READY, "component belongs to a previous adapter process")
        try:
            current = await self.current_context()
        except BaseException:
            # Even a transient gap destroys this exchange's source authority;
            # later recovery must not silently revive an old M1 transcript.
            self.exchange.close()
            raise
        self.exchange._live()
        if (
            type(current) is not ScopeContext
            or type(current.generation) is not int
            or current.generation < 1
            or not current.schema_fingerprint
            or not current.binding_token
            or (self.context is not None and current != self.context)
        ):
            self.exchange.close()
            raise EmosaError(Reason.NOT_READY, "component source binding changed")
        return current

    async def start(self, send_frame):
        self.context = await self._bound()
        try:
            for frame in self.exchange.request():
                await self._bound()
                send_frame(frame)
                await self._bound()
        except BaseException:
            self.exchange.close()
            raise

    async def receive(self, message, *, ingress, generation):
        if self.receiving:
            raise EmosaError(Reason.BUSY, "one WSC component handoff at a time")
        self.receiving = True
        ref = None
        created = authenticated = False
        try:
            self.exchange.binding.check(message, ingress=ingress, generation=generation)
            if self.context is None:
                raise EmosaError(Reason.NOT_READY, "component has not sent M1")
            await self._bound()
            result = self.exchange.receive(message, ingress=ingress, generation=generation)
            authenticated = True
            if self.operation_id is not None:
                # WscExchange has authenticated and checked whole-configuration
                # equality, including re-encrypted retries and changed MIDs.
                return self.engine.store.get(self.operation_id)
            # Invalid/unsupported packets get here only after complete-message,
            # crypto, encrypted-role and sole-M2 scope checks have all passed.
            ref = "wsc-" + self.exchange.exchange_id
            self.engine.vault.persist_received(ref, result.candidate.passphrase)
            created = True
            intent = Intent(
                self.target.pod_id,
                self.target.radio_id,
                self.target.bss_id,
                result.candidate.ssid,
                ref,
            )
            await self.backend.plan(intent)
            await self._bound()
            peer = self.exchange.binding
            receipt = {
                "scope": "owned_simulation_wsc_component",
                "process_id": self.process_id,
                "exchange_id": self.exchange.exchange_id,
                "m1_sha256": self.exchange.m1_sha256,
                # Keyed, private journal correlation; do not export this value.
                "request_fingerprint": self.engine.vault.fingerprint(
                    [t.value.hex() for t in message.tlvs if t.kind == 0x11]
                ),
                "controller_al": peer.controller_al.hex(),
                "agent_al": peer.local_al.hex(),
                "ruid": self.target.ruid.hex(),
                "bssid": self.target.bssid.hex(),
                "ingress": peer.ingress,
                "link_generation": peer.generation,
                "database_generation": self.context.generation,
                "schema_fingerprint": self.context.schema_fingerprint,
                "first_mid": message.mid,
                "pod_id": self.target.pod_id,
                "radio_id": self.target.radio_id,
                "bss_id": self.target.bss_id,
            }
            operation = self.engine._request(
                intent,
                source="wsc-component:" + peer.controller_al.hex(),
                key=self.exchange.exchange_id,
                run_id=self.run_id,
                deadline=self.deadline,
                initiating_interface="wsc-component",
                wsc_receipt=receipt,
            )
            self.operation_id = operation.operation_id
            self.engine.wsc_guards[operation.operation_id] = self._bound
            return operation
        except BaseException:
            if created and self.operation_id is None:
                # Never remove a credential if SQLite actually committed its
                # operation before an exception was delivered to this caller.
                existing = self.engine.store.lookup(
                    "wsc-component:" + self.exchange.binding.controller_al.hex(),
                    self.target.pod_id,
                    self.exchange.exchange_id,
                )
                if existing is None:
                    (self.engine.vault.directory / ref).unlink(missing_ok=True)
                else:
                    self.operation_id = existing.operation_id
            if authenticated and self.operation_id is None:
                self.exchange.close()
            raise
        finally:
            self.receiving = False

    def close(self):
        self.exchange.close()
        if self.operation_id is not None:
            self.engine.wsc_guards.pop(self.operation_id, None)
