"""Bounded, non-provisioning native-controller compatibility measurement.

The only emitted procedure is a Profile-1 Autoconfiguration Search. A correlated
Response records observed fields, never grants admission or starts WSC. There is
no database, operation engine or credential dependency in this component.
"""

import time

from emosa.easymesh_payloads import Profile2APCapability
from emosa.errors import EmosaError
from emosa.wire.autoconfiguration import DiscoveryExchange
from emosa.wire.cmdu import MidSequence, Reassembler, decode_frame


class ControllerProbe:
    def __init__(self, binding, send_frame, *, mids=None, clock=time.monotonic):
        self.binding, self.send_frame, self.clock = binding, send_frame, clock
        self.exchange = DiscoveryExchange(
            binding,
            band=0,
            profile=1,
            profile2=Profile2APCapability(0, 0, 0, 0),
            mids=mids if mids is not None else MidSequence(65534),
            clock=clock,
        )
        self.assembly = Reassembler(
            clock=clock, max_contexts=2, max_fragments=8, max_message_bytes=8192, max_bytes=16384
        )
        self.state, self.next_send = "searching", clock()
        self.advertisement = None
        self.response_mid = None
        self.counts = {}
        self.tokens, self.refill = 32.0, clock()

    def _record(self, event):
        self.counts[event] = self.counts.get(event, 0) + 1
        return event

    def tick(self):
        if self.state != "searching":
            return
        self.assembly.expire()
        now = self.clock()
        if now >= self.exchange.deadline:
            self.state = "timeout"
            self.exchange.close()
        elif now >= self.next_send and self.exchange.transmissions < 3:
            try:
                for frame in self.exchange.request():
                    self.send_frame(frame)
                if self.clock() >= self.exchange.deadline:
                    raise TimeoutError("Search transmission exceeded probe deadline")
                self.next_send = self.clock() + 1
                self._record("search_sent")
            except (EmosaError, OSError):
                self.state = "send_failed"
                self.exchange.close()
                self._record("search_send_failed")

    def receive(self, frame, *, ingress, generation):
        if self.state != "searching":
            return self._record("inactive_input")
        now = self.clock()
        self.tokens = min(32.0, self.tokens + max(0, now - self.refill) * 16)
        self.refill = now
        if self.tokens < 1:
            return self._record("rate_limited")
        self.tokens -= 1
        try:
            fragment = decode_frame(frame)
            self.binding.check(fragment, ingress=ingress, generation=generation)
            if fragment.message_type != 8:
                return self._record("unsupported_message")
            message = self.assembly.feed(frame, ingress=ingress)
            if message is None:
                return self._record("incomplete")
            self.advertisement = self.exchange.receive(
                message, ingress=ingress, generation=generation
            )
            self.response_mid = message.mid
            self.state = "response_observed"
            self.exchange.close()
            return self._record("response_correlated")
        except EmosaError:
            return self._record("input_rejected")

    def close(self):
        self.exchange.close()
        if self.state == "searching":
            self.state = "closed"

    def status(self):
        response = self.advertisement
        return {
            "scope": "native_controller_discovery_probe_only",
            "state": self.state,
            "search_profile": 1,
            "sent_mids": sorted(self.exchange.sent_mids),
            "response_mid": self.response_mid,
            "response_profile": response.profile if response else None,
            "profile_correlated": response is not None,
            "controller_flags_hex": response.controller_flags.hex()
            if response and response.controller_flags is not None
            else None,
            "security_capability_hex": response.security_capability.hex()
            if response and response.security_capability is not None
            else None,
            "selected_response_issues": list(response.selected_response_issues)
            if response
            else None,
            "pending_requirements": list(response.pending_requirements) if response else None,
            "counts": dict(self.counts),
            "wsc_started": False,
            "operations_created": 0,
            "profile_qualified": False,
            "controller_onboarding_proven": False,
            "physical_pod_proven": False,
        }
