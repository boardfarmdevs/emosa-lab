"""Restricted discovery-to-report lifecycle; never a configuration authority.

Only the owned simulator supplies facts today. The Table 117 ambiguity and
complete profile audit still block automatic Early Report and M1 initiation.
Matching selected Response fields permits read-only topology in this component,
not controller authentication, agent admission or physical-pod qualification.
"""

import secrets
import time
from collections import deque

from emosa.errors import EmosaError
from emosa.wire.autoconfiguration import DiscoveryExchange
from emosa.wire.cmdu import MidSequence, Reassembler, decode_frame
from emosa.wire.coordinator import ReportCoordinator
from emosa.wire.reports import early_report


class DiscoveryReportSession:
    """One explicit trusted-link binding, one bounded attempt per source context.

    tick() starts discovery when fresh facts appear. Three Search transmissions,
    one second apart, share a fixed five-second local budget. A failed attempt
    stays stopped until restart() or a changed source context; there is no
    endless registrar search. Source loss withdraws correlation immediately.
    """

    def __init__(self, source, send_frame, *, mids=None, clock=time.monotonic):
        self.source, self.binding, self.send_frame = source, source.binding, send_frame
        self.clock = clock
        self.mids = mids if mids is not None else MidSequence(secrets.randbits(16))
        self.assembly = Reassembler(clock=clock)
        self.state = "waiting_source"
        self.discovery = self.reports = self.context = self.advertisement = None
        self.next_search = 0.0
        self.events, self.counts = deque(maxlen=64), {}

    def _record(self, event):
        self.counts[event] = self.counts.get(event, 0) + 1
        self.events.append(event)
        return event

    def _drop(self, state):
        if self.discovery is not None:
            self.discovery.close()
        if self.reports is not None:
            self.reports.close()
        self.discovery = self.reports = self.advertisement = None
        self.assembly = Reassembler(clock=self.clock)
        self.state = state

    def _current(self):
        if self.source.binding != self.binding:
            return None
        return self.source.current()

    def _synchronize(self):
        snapshot = self._current()
        context = (snapshot.context_token, snapshot.capabilities) if snapshot else None
        if context != self.context:
            self._drop("waiting_source")
            self.context = context
            self._record("source_context_changed")
        return snapshot

    def restart(self):
        """Explicit local retry; never resets MID sequence or the rate limit."""
        if self.state != "closed":
            self._drop("waiting_source")
            self._record("restart_requested")

    def _transmit(self, snapshot):
        try:
            frames = self.discovery.request()
            for frame in frames:
                current = self._current()
                snapshot.stamp.check(current.stamp if current else None, self.clock())
                self.send_frame(frame)
                current = self._current()
                snapshot.stamp.check(current.stamp if current else None, self.clock())
            self.next_search = self.clock() + 1
            self._record("search_sent")
        except (EmosaError, OSError):
            # A partial/late send must not leave a response-eligible MID behind.
            self._drop("search_failed")
            self.next_search = self.clock() + 1
            self._record("search_send_failed")

    def tick(self):
        if self.state == "closed":
            return
        snapshot = self._synchronize()
        self.assembly.expire()
        if snapshot is None:
            return
        if self.state == "waiting_source" and self.clock() >= self.next_search:
            # The supported report builder is explicitly limited to 2.4 GHz.
            # Validate that scope without sending or consuming a session MID.
            try:
                early_report(
                    self.binding,
                    snapshot.capabilities,
                    snapshot.stamp,
                    MidSequence(0),
                    clock=self.clock,
                )
            except EmosaError:
                self.state = "source_incompatible"
                self._record("source_incompatible")
                return
            self.discovery = DiscoveryExchange(
                self.binding,
                band=0,
                profile=1,
                profile2=snapshot.capabilities.profile2,
                mids=self.mids,
                clock=self.clock,
            )
            self.state = "discovering"
            self._transmit(snapshot)
        elif self.state == "discovering":
            if self.clock() >= self.discovery.deadline:
                self._drop("discovery_timeout")
                self._record("discovery_timeout")
            elif self.clock() >= self.next_search and self.discovery.transmissions < 3:
                self._transmit(snapshot)
        elif self.reports is not None:
            self.reports.tick()

    def receive(self, frame, *, ingress, generation):
        if self.state == "closed":
            return self._record("closed_input")
        try:
            fragment = decode_frame(frame)
            self.binding.check(fragment, ingress=ingress, generation=generation)
            # Receiving a packet never starts discovery or triggers a retry.
            self._synchronize()
            if fragment.message_type == 9:
                return self._record("wsc_admission_blocked")
            if fragment.message_type == 2 and self.reports is not None:
                return self.reports.receive(frame, ingress=ingress, generation=generation)
            if fragment.message_type != 8 or self.state != "discovering":
                return self._record("message_not_admitted")
            message = self.assembly.feed(frame, ingress=ingress)
            if message is None:
                return "incomplete"
            advertisement = self.discovery.receive(message, ingress=ingress, generation=generation)
            self.advertisement = advertisement
            if advertisement.selected_response_issues:
                self.state = "discovery_incompatible"
                return self._record("discovery_incompatible")
            self.reports = ReportCoordinator(
                self.source, self.send_frame, mids=self.mids, clock=self.clock
            )
            self.state = "discovered_read_only"
            return self._record("controller_correlated")
        except EmosaError:
            return self._record("input_rejected")

    def close(self):
        self._drop("closed")

    def status(self):
        if self.state != "closed":
            self._synchronize()
        return {
            "scope": "restricted_discovery_to_read_only_topology",
            "state": self.state,
            "source_available": self._current() is not None,
            "selected_response_issues": list(self.advertisement.selected_response_issues)
            if self.advertisement
            else [],
            "response_observed": self.advertisement is not None,
            "counts": dict(self.counts),
            "events": list(self.events),
            "reports": self.reports.status() if self.reports else None,
            "automatic_early_report": "blocked_table117_review",
            "wsc_admission": "blocked_full_profile_and_durable_operation_integration",
            "operations_created": 0,
            "controller_onboarding_proven": False,
            "physical_pod_proven": False,
        }
