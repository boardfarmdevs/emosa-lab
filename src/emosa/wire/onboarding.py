"""Bounded non-DPP discovery → Early Report → authenticated WSC simulation.

This is an experimental lifecycle for the owned sole-radio fixture. It does not
qualify a complete EasyMesh profile or accept a physical backend. See the explicit
feature/interpretation contract in doc/protocol/native-onboarding.md.
"""

import time
from collections import deque

from emosa.easymesh_payloads import DeviceInventory, encode_value
from emosa.errors import EmosaError, Reason
from emosa.wire.autoconfiguration import SECURITY_ENVELOPES, DiscoveryExchange
from emosa.wire.cmdu import MULTICAST, MidSequence, Reassembler, Tlv, decode_frame, fragment_message
from emosa.wire.coordinator import ReportCoordinator
from emosa.wire.provisioning_session import ComponentProvisioningSession
from emosa.wire.reports import PreparedReport, capability_tlvs


def non_dpp_admission(advertisement):
    """Apply 6.1 §§6.1, 9.1, 13.1, 18 to the selected non-DPP experiment.

    Table 117's named Early bit takes precedence over its overlapping reserved
    range for this lab interpretation. A security capability can be absent for
    unsupported DPP; a present malformed/reserved value is still rejected.
    """
    issues = list(advertisement.selected_response_issues)
    if "security_capability_absent" in issues:
        issues.remove("security_capability_absent")
    if advertisement.profile != 1 or advertisement.band != 0:
        issues.append("outside_profile1_24ghz_contract")
    return tuple(issues)


class OnboardingSession:
    """One attempt with no implicit retry after source loss or invalid M2.

    The factory creates a fresh WSC transcript only after a live Response and a
    completely transmitted Early Report. An Early Ack is tracked independently:
    §5.2.2 requires transmission before M1, not waiting for an Ack. Unsupported
    control requests are recorded and are never acknowledged as implemented.
    """

    def __init__(
        self, source, send_frame, bridge_factory, inventory, *, mids=None, clock=time.monotonic
    ):
        if type(inventory) is not DeviceInventory:
            raise EmosaError(Reason.INVALID_INPUT, "explicit device inventory required")
        encode_value(inventory)
        self.source, self.binding = source, source.binding
        self.send_frame, self.factory, self.inventory = send_frame, bridge_factory, inventory
        self.clock = clock
        self.mids = mids if mids is not None else MidSequence(100)
        self.assembly = Reassembler(
            clock=clock, max_contexts=2, max_fragments=8, max_message_bytes=8192, max_bytes=16384
        )
        self.state = "waiting_source"
        self.discovery = self.reports = self.provisioning = None
        self.context = self.capabilities = self.last_topology = None
        self.next_search = 0
        self.counts, self.events = {}, deque(maxlen=64)
        self.tokens, self.token_time = 32.0, clock()
        self.issues = ()

    def _record(self, event):
        self.counts[event] = self.counts.get(event, 0) + 1
        self.events.append(event)
        return event

    def _snapshot(self):
        snapshot = self.source.current() if self.source.binding == self.binding else None
        if self.context is not None and (
            snapshot is None
            or snapshot.context_token != self.context
            or snapshot.capabilities != self.capabilities
        ):
            self.close()
            self.state = "source_lost"
            self._record("source_lost")
            return None
        return snapshot

    def _stamp(self):
        snapshot = self._snapshot()
        return snapshot.stamp if snapshot else None

    async def tick(self):
        if self.state in ("closed", "source_lost", "failed", "incompatible"):
            return
        snapshot = self._snapshot()
        self.assembly.expire()
        if snapshot is None:
            return
        if self.state == "waiting_source":
            capability_tlvs(snapshot.capabilities)
            if len(snapshot.capabilities.radios) != 1:
                raise EmosaError(Reason.UNSUPPORTED_OPERATION, "one radio required")
            self.context, self.capabilities = snapshot.context_token, snapshot.capabilities
            self.discovery = DiscoveryExchange(
                self.binding,
                band=0,
                profile=1,
                profile2=self.capabilities.profile2,
                mids=self.mids,
                clock=self.clock,
            )
            self.state = "discovering"
        if self.state == "discovering":
            if self.clock() >= self.discovery.deadline:
                self.state = "failed"
                self._record("discovery_timeout")
            elif self.clock() >= self.next_search and self.discovery.transmissions < 3:
                try:
                    for frame in self.discovery.request():
                        snapshot.stamp.check(self._stamp(), self.clock())
                        self.send_frame(frame)
                        snapshot.stamp.check(self._stamp(), self.clock())
                    self.next_search = self.clock() + 1
                    self._record("search_sent")
                except (EmosaError, OSError):
                    self.close()
                    self.state = "failed"
                    self._record("search_send_failed")
        if self.reports:
            self.reports.tick()
        if self.provisioning:
            self.provisioning.tick()
            # Notify only observed topology changes, never desired Config changes.
            if snapshot.topology.operational != self.last_topology:
                for frame in fragment_message(
                    MULTICAST,
                    self.binding.local_al,
                    1,
                    self.mids.next(),
                    (Tlv(1, self.binding.local_al),),
                    relay=True,
                ):
                    snapshot.stamp.check(self._stamp(), self.clock())
                    self.send_frame(frame)
                self.last_topology = snapshot.topology.operational
                self._record("observed_topology_notification")

    async def receive(self, frame, *, ingress, generation):
        if self.state in ("closed", "source_lost", "failed", "incompatible"):
            return self._record("inactive_input")
        now = self.clock()
        self.tokens = min(32, self.tokens + max(0, now - self.token_time) * 16)
        self.token_time = now
        if self.tokens < 1:
            return self._record("rate_limited")
        self.tokens -= 1
        try:
            fragment = decode_frame(frame)
            self.binding.check(fragment, ingress=ingress, generation=generation)
            snapshot = self._snapshot()
            if snapshot is None:
                return self._record("source_unavailable")
            if fragment.message_type == 9:
                if self.provisioning is None:
                    return self._record("wsc_before_admission")
                result = await self.provisioning.receive(
                    frame, ingress=ingress, generation=generation
                )
                if result["status"] == "operation":
                    self.state = "provisioning"
                return self._record("wsc_" + result["status"])
            if fragment.message_type in (2, 0x8000) and self.reports:
                return self.reports.receive(frame, ingress=ingress, generation=generation)
            message = self.assembly.feed(frame, ingress=ingress)
            if message is None:
                return "incomplete"
            if message.relay or any(t.kind in SECURITY_ENVELOPES for t in message.tlvs):
                raise EmosaError(Reason.UNSUPPORTED_OPERATION, "unsupported input envelope")
            if message.message_type == 8 and self.state == "discovering":
                advertisement = self.discovery.receive(
                    message, ingress=ingress, generation=generation
                )
                self.issues = non_dpp_admission(advertisement)
                if self.issues:
                    self.state = "incompatible"
                    return self._record("response_incompatible")
                self.state = "admitting"
                self.reports = ReportCoordinator(
                    self.source, self.send_frame, mids=self.mids, clock=self.clock
                )
                self.reports.notify_early()
                bridge = self.factory(snapshot, self.mids)
                radio = self.capabilities.radios[0]
                if (
                    bridge.exchange.binding != self.binding
                    or bridge.exchange.basic != radio.basic
                    or bridge.exchange.profile2 != self.capabilities.profile2
                    or bridge.exchange.advanced != radio.advanced
                ):
                    bridge.close()
                    raise EmosaError(Reason.NOT_READY, "WSC/report capabilities disagree")
                self.provisioning = ComponentProvisioningSession(
                    bridge, self.send_frame, clock=self.clock
                )
                original_context = bridge.current_context

                async def admitted_context():
                    context = await original_context()
                    if self._snapshot() is None:
                        raise EmosaError(Reason.NOT_READY, "discovery report source lost")
                    return context

                bridge.current_context = admitted_context
                self._snapshot()
                if self.state == "source_lost":
                    return self._record("source_unavailable")
                await self.provisioning.start()
                self.state = "awaiting_m2"
                return self._record("early_then_m1_sent")
            if message.message_type == 0x8001 and self.provisioning:
                tlvs = [t for t in capability_tlvs(snapshot.capabilities) if t.kind != 0xED]
                tlvs.append(Tlv(self.inventory.kind, encode_value(self.inventory)))
                response = PreparedReport(
                    0x8002,
                    message.mid,
                    snapshot.stamp,
                    min(now + 1, snapshot.stamp.valid_until),
                    fragment_message(
                        self.binding.controller_al, self.binding.local_al, 0x8002, message.mid, tlvs
                    ),
                )
                response.send(self.send_frame, self._stamp, clock=self.clock)
                return self._record("ap_capability_report_sent")
            return self._record(f"unsupported_message_{message.message_type:04x}")
        except (EmosaError, OSError) as exc:
            # A failed admission/handoff never restarts implicitly.
            if self.state == "admitting":
                self.close()
                self.state = "failed"
            return self._record("rejected_" + getattr(exc, "code", "send"))

    def close(self):
        for component in (self.discovery, self.reports, self.provisioning):
            if component:
                component.close()
        self.state = "closed"

    def status(self):
        # Helper status describes the helper's own authority, not admission by
        # this parent lifecycle. Expose observations without contradictory gates.
        reports = self.reports.status() if self.reports else None
        wsc = self.provisioning.status() if self.provisioning else None
        return {
            "scope": "owned_non_dpp_sole_radio_onboarding",
            "state": self.state,
            "counts": dict(self.counts),
            "events": list(self.events),
            "admission_issues": list(self.issues),
            "reports": {k: reports[k] for k in ("counts", "events", "early_pending")}
            if reports
            else None,
            "wsc": {k: wsc[k] for k in ("counts", "events", "closed")} if wsc else None,
            "full_profile_qualified": False,
            "physical_pod_proven": False,
        }
