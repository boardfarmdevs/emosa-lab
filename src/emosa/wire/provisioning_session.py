"""Bounded packet receiver for the owned WSC experiment, not service admission.

The caller constructs an explicitly bound component exchange. This does not
discover/authenticate an Ethernet link or grant a controller profile. Only the
owned simulation and hwsim harnesses attach it to a packet endpoint.
"""

import time
from collections import deque

from emosa.errors import EmosaError, Reason
from emosa.wire.cmdu import Reassembler, decode_frame


class ComponentProvisioningSession:
    def __init__(self, bridge, send_frame, *, clock=time.monotonic):
        self.bridge, self.send_frame, self.clock = bridge, send_frame, clock
        self.assembly = Reassembler(
            clock=clock, max_contexts=2, max_fragments=8, max_message_bytes=8192, max_bytes=16384
        )
        self.events = deque(maxlen=32)
        self.counts = {}
        self.receiving = self.closed = False
        self.tokens, self.last_token = 32.0, clock()

    def _record(self, status, **values):
        result = {"status": status, **values}
        self.counts[status] = self.counts.get(status, 0) + 1
        self.events.append(result)
        return result

    async def start(self):
        if self.closed:
            raise EmosaError(Reason.NOT_READY, "component packet session is closed")
        await self.bridge.start(self.send_frame)

    async def receive(self, frame, *, ingress, generation):
        if self.closed:
            return self._record("closed")
        if self.receiving:
            return self._record("busy")  # No unbounded per-packet waiter queue.
        now = self.clock()
        self.tokens = min(32.0, self.tokens + max(0, now - self.last_token) * 16)
        self.last_token = now
        if self.tokens < 1:
            return self._record("rate_limited")
        self.tokens -= 1
        self.receiving = True
        try:
            fragment = decode_frame(frame)
            # Validate the configured routing context before allocating any
            # reassembly state. MAC/context matching is not Ethernet link trust.
            self.bridge.exchange.binding.check(fragment, ingress=ingress, generation=generation)
            if fragment.message_type != 9:
                return self._record("unsupported_message")
            message = self.assembly.feed(frame, ingress=ingress)
            if message is None:
                return self._record("incomplete", mid=fragment.mid)
            operation = await self.bridge.receive(message, ingress=ingress, generation=generation)
            result = await self.bridge.engine.execute(operation.operation_id)
            return self._record(
                "operation", mid=message.mid, operation_id=result.operation_id, state=result.state
            )
        except EmosaError as exc:
            return self._record("rejected", reason=exc.code)
        finally:
            self.receiving = False

    def tick(self):
        # Fragment expiration cannot create an operation. The bridge checks its
        # own exchange lifetime and fresh source again before every handoff.
        return self.assembly.expire()

    def close(self):
        self.closed = True
        self.bridge.close()

    def status(self):
        return {
            "counts": dict(self.counts),
            "events": list(self.events),
            "closed": self.closed,
            "scope": "owned_simulation_wsc_component",
            "full_controller_onboarding": False,
        }
