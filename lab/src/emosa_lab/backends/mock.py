"""Synthetic Config and device state are separate stores with explicit device steps."""

from copy import deepcopy

from emosa.backends.base import Snapshot, SubmitResult
from emosa.errors import EmosaError, Reason
from emosa.model import Intent, Observation


class ModelBackend:
    mode = "model"

    def __init__(self, pod_id, vault, clock, *, fault="none"):
        self.pod_id, self.vault, self.clock = pod_id, vault, clock
        self.fault = fault
        self.config = {
            "ssid": "initial-network",
            "enabled": True,
            "mode": "ap",
            "security_mode": "wpa2-psk",
            "credential_fingerprint": "unknown",
        }
        self.observed = deepcopy(self.config)
        self.ready = True
        self.generation = 1
        self.revision = 1
        self.writes = 0
        self.pending = None

    async def snapshot(self):
        return Snapshot(
            deepcopy(self.config),
            Observation(
                self.pod_id,
                "bss-1",
                deepcopy(self.observed),
                "simulation",
                self.mode,
                self.generation,
                self.clock.utc(),
                self.ready,
                "synthetic-device-model",
                revision=self.revision,
            ),
            self.ready,
            self.generation,
            "synthetic-model-v1",
        )

    async def plan(self, intent: Intent):
        if not self.ready:
            raise EmosaError(Reason.NOT_READY, "model disconnected")
        if (intent.pod_id, intent.radio_id, intent.bss_id) != (self.pod_id, "radio-1", "bss-1"):
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "outside designated existing BSS")
        intent.target(self.vault)
        return {
            "mapping": "synthetic-existing-bss-v1",
            "fields": ["ssid", "wpa_psks[key]"],
            "radio_id": intent.radio_id,
            "bss_id": intent.bss_id,
            "ssid": intent.ssid,
            "secret_ref": intent.secret_ref,
            "guard": "current managed fields and references",
        }

    async def submit(self, intent, attempt):
        await self.plan(intent)
        if self.fault == "guard-conflict":
            return SubmitResult("conflict", {}, "PRECONDITION_FAILED")
        if self.fault == "reject":
            return SubmitResult("rejected", {}, "PRECONDITION_FAILED")
        self.writes += 1
        self.config.update(intent.target(self.vault))
        self.pending = deepcopy(self.config)
        if self.fault == "lost-reply":
            self.ready = False
            return SubmitResult("unknown", {"attribution": "unknown"}, "OUTCOME_UNKNOWN")
        return SubmitResult(
            "committed",
            {
                "attribution": "reply",
                "transaction_validated": True,
                "transaction_id": attempt["transaction_id"],
            },
        )

    def device_step(self, *, partial=False):
        if self.pending and self.fault not in {"withhold", "reject"}:
            if partial:
                self.observed["ssid"] = self.pending["ssid"]
            else:
                self.observed = self.pending
                self.pending = None
            self.revision += 1

    def reconnect(self):
        self.generation += 1
        self.ready = True

    def competing_writer(self):
        self.config["ssid"] = "competing-network"

    async def close(self):
        self.ready = False
