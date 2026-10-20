# SPDX-License-Identifier: Apache-2.0
"""Bounded non-DPP discovery → Early Report → authenticated WSC simulation.

This is an experimental lifecycle for the owned sole-radio fixture. It does not
qualify a complete EasyMesh profile or accept a physical backend. See the explicit
feature/interpretation contract in docs/reference/protocol/native-onboarding.md.
"""

import time
from collections import deque

from emosa.easymesh_payloads import DeviceInventory, encode_value
from emosa.errors import EmosaError, Reason
from emosa.wire.ap_metrics import APMetricCoordinator, APMetricSource
from emosa.wire.autoconfiguration import (
    EASYMESH_61,
    R1,
    SECURITY_ENVELOPES,
    DiscoveryExchange,
    check_message_set,
)
from emosa.wire.backhaul_steering import BackhaulSteeringCoordinator
from emosa.wire.channel import ChannelCoordinator, moved_preferences
from emosa.wire.cmdu import (
    MULTICAST,
    MidSequence,
    Reassembler,
    Tlv,
    decode_frame,
    fragment_message,
    parse_tlvs,
)
from emosa.wire.coordinator import ReportCoordinator
from emosa.wire.disassociation import DisassociationCoordinator, FinalSession
from emosa.wire.link_metrics import (
    BackhaulLinkMetrics,
    LinkMetricCoordinator,
    LinkMetricSource,
)
from emosa.wire.pod_metrics import PodMetricReporter
from emosa.wire.provisioning_session import ComponentProvisioningSession
from emosa.wire.reporting_policy import ReportingPolicyCoordinator
from emosa.wire.reports import (
    PreparedReport,
    capabilities_identity,
    capability_tlvs,
    operable_channels_moved,
)
from emosa.wire.steering import SteeringCoordinator
from emosa.wire.unassociated import UnassociatedCoordinator

R3_CONTROLLER_FIELDS = (
    "controller_capability_absent",
    "kib_mib_support_absent",
    "early_ap_capability_bit_absent_for_non_dpp_search",
)


class ClientReannouncement:
    """When a provisioned session announces every current client again (spec §2.5).

    Once, after the controller's first Topology Query answered since M2 was
    accepted: before that the controller may not know the BSS yet and can drop the
    announcements (seen with a restarted prplMesh). Counted in Topology Responses
    sent by the session.
    """

    def __init__(self):
        self.mark, self.done = None, False

    def provisioned(self, responses):
        """M2 accepted, with ``responses`` Topology Responses sent so far."""
        if self.mark is None:
            self.mark = responses

    def due(self, responses):
        """True once: the clients are to be announced again now."""
        if self.done or self.mark is None or responses <= self.mark:
            return False
        self.done = True
        return True


def non_dpp_admission(advertisement, *, message_set=EASYMESH_61):
    """Apply 6.1 §§6.1, 9.1, 13.1, 18 to the selected non-DPP experiment.

    Table 117's named Early bit takes precedence over its overlapping reserved
    range for this lab interpretation. A security capability can be absent for
    unsupported DPP; a present malformed/reserved value is still rejected.
    """
    issues = list(advertisement.selected_response_issues)
    if "security_capability_absent" in issues:
        issues.remove("security_capability_absent")
    if message_set == R1:
        # EasyMesh R1 has no Controller Capability TLV and no Early AP Capability
        # Report; their absence is not an admission gap in that message set.
        issues = [i for i in issues if i not in R3_CONTROLLER_FIELDS]
    # An R1 agent does not interpret the Response's Profile TLV (see
    # DiscoveryExchange.receive); EasyMesh 6.1 requires the echoed Profile 1.
    if (message_set != R1 and advertisement.profile != 1) or advertisement.band != 0:
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
        self,
        source,
        send_frame,
        bridge_factory,
        inventory,
        *,
        mids=None,
        clock=time.monotonic,
        channel_store=None,
        reporting_policy_store=None,
        link_metric_source=None,
        ap_metric_source=None,
        reset_channel_policy=True,
        message_set=EASYMESH_61,
        steering_executor=None,
        pod_metrics=None,
        probes=None,
        watch=None,
        backhaul_steering_executor=None,
        backhaul_steering_outcome=None,
        backhaul_steering_state=None,
        backhaul_pairs=None,
        backhaul_steering_associated=None,
        radio_bridge_factory=None,
        reconnect_window=None,
    ):
        self.message_set = check_message_set(message_set)
        # spec 2.5: a provisioned session held while its pod's link is away, at most this many
        # seconds, and kept when the source comes back in the same lineage with the same
        # capabilities apart from their operable channels (None: the source lost at once)
        self.reconnect_window = reconnect_window
        self.generation = None  # the pod source's database generation the session is on
        self.away_since = None  # when a provisioned session's source went away
        self.notify_due = False  # a Topology Notification for a kept reconnection
        self.channel_moves = ()  # radios whose operable channel moved: their preferences due
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
        self.last_clients = set()
        self.reannounce = ClientReannouncement()  # every client again, once, after M2
        self.next_search = 0
        self.counts, self.events = {}, deque(maxlen=64)
        self.tokens, self.token_time = 32.0, clock()
        self.issues = ()
        self.channel_store = channel_store
        self.reporting_policy_store = reporting_policy_store
        self.reporting_policy = None
        self.reset_channel_policy = reset_channel_policy
        self.channels = None
        self.disassociations = None
        self.departures = {}
        if link_metric_source is not None and link_metric_source.reports is not source:
            raise EmosaError(Reason.INVALID_INPUT, "link measurements use another control source")
        self.link_metric_source = (
            link_metric_source
            if link_metric_source is not None
            else LinkMetricSource(source, clock=clock)
        )
        self.link_metrics = None
        # the pod's Wi-Fi backhaul neighbors' link metrics (spec 8.5), when it has any
        self.backhaul_metrics = (
            BackhaulLinkMetrics(
                self.binding.local_al, self.binding.controller_al, backhaul_pairs, send_frame
            )
            if backhaul_pairs is not None
            else None
        )
        if ap_metric_source is not None and ap_metric_source.reports is not source:
            raise EmosaError(Reason.INVALID_INPUT, "AP measurements use another control source")
        self.ap_metric_source = (
            ap_metric_source
            if ap_metric_source is not None
            else APMetricSource(source, clock=clock)
        )
        self.ap_metrics = None
        # AP metrics from the pod's statistics (spec §3.8): {"stats", "esp_be", "freshness"}
        self.pod_metrics = pod_metrics
        self.probes = probes  # the pod's probe requests (PodStats), with telemetry
        self.unassociated = None
        self.watch = watch  # the pod's probe watch (ProbeWatch.ask), with telemetry
        # Client steering mandates go to the pod through this (emosa.agent.steering).
        self.steering_executor = steering_executor
        self.steering = None
        # Backhaul Steering moves go to the pod's uplink scope (emosa.agent.uplink): an
        # agent whose pod has a Multi-AP backhaul station; without one they are refused.
        self.backhaul_steering_executor = backhaul_steering_executor
        self.backhaul_steering_outcome = backhaul_steering_outcome
        # the backhaul STA the pod is on now (bytes), which a successful move's answer names
        self.backhaul_steering_associated = backhaul_steering_associated
        self.backhaul_steering_state = backhaul_steering_state  # the agent's, across sessions
        self.backhaul_steering = None
        # Further radios (spec 9): one M1 and one M2 per radio (EasyMesh 6.1 §7.1), each into its
        # radio's own scope. radio_bridge_factory(snapshot, mids, index) makes the exchange of
        # capabilities.radios[index]; without it the session takes one radio only. Their M1s go
        # one at a time once the fronthaul is provisioned; an M2 goes to the exchange of the radio
        # its AP Radio Identifier names.
        self.radio_bridge_factory = radio_bridge_factory
        self.radio_sessions = {}  # RUID -> ComponentProvisioningSession, further radios
        self.radio_awaiting = None  # the further radio whose M1 is out and M2 not yet taken
        self.next_radio = 1
        self.wsc_routes = {}  # (source, MID) of a fragmented M2 -> its exchange's session

    def _record(self, event):
        self.counts[event] = self.counts.get(event, 0) + 1
        self.events.append(event)
        return event

    def _lost(self, reason=None):
        self.close()
        self.state = "source_lost"
        self._record("source_lost")
        if reason:
            self._record(reason)
        return None

    def _snapshot(self):
        snapshot = self.source.current() if self.source.binding == self.binding else None
        if self.context is None:
            return snapshot
        # spec 2.5: only a provisioned session is held through its pod's reconnection and kept
        keeps = self.state == "provisioning" and self.reconnect_window is not None
        if snapshot is None:
            if not keeps or self.source.binding != self.binding:
                return self._lost()
            if self.away_since is None:
                self.away_since = self.clock()
                self._record("reconnection_held")
            if self.clock() < self.away_since + self.reconnect_window:
                return None
            return self._lost("reconnection_window_passed")
        if snapshot.context_token != self.context:
            return self._lost()
        if snapshot.capabilities != self.capabilities:
            if not keeps or capabilities_identity(snapshot.capabilities) != capabilities_identity(
                self.capabilities
            ):
                return self._lost()
            # a radio's channel moved with the pod's uplink: its new operable channel reported
            # (a Channel Preference Report, and the Operating Channel Report that follows it)
            moved = operable_channels_moved(self.capabilities, snapshot.capabilities)
            self.capabilities = snapshot.capabilities
            self.channel_moves = tuple(dict.fromkeys((*self.channel_moves, *moved)))
            self._record("channel_move_kept")
        if snapshot.generation != self.generation or self.away_since is not None:
            if not keeps:
                return self._lost()
            self.generation, self.away_since, self.notify_due = snapshot.generation, None, True
            self._record("reconnection_kept")
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
            if len(snapshot.capabilities.radios) != 1 and self.radio_bridge_factory is None:
                raise EmosaError(Reason.UNSUPPORTED_OPERATION, "one radio required")
            self.context, self.capabilities = snapshot.context_token, snapshot.capabilities
            self.generation = snapshot.generation
            self.discovery = DiscoveryExchange(
                self.binding,
                band=0,
                profile=1,
                profile2=self.capabilities.profile2,
                mids=self.mids,
                clock=self.clock,
                message_set=self.message_set,
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
        if self.channels:
            self.channels.tick()
        if self.reporting_policy:
            self.reporting_policy.tick()
        if self.link_metrics and self.state == "provisioning":
            self.link_metrics.tick()
        if self.backhaul_steering:
            self.backhaul_steering.tick()
        if self.disassociations:
            self.disassociations.tick()
        self.departures = {key: at for key, at in self.departures.items() if self.clock() < at + 2}
        if self.provisioning:
            self.provisioning.tick()
            for session in self.radio_sessions.values():
                session.tick()
            if self.state == "provisioning":
                await self._next_radio(snapshot)
                if self.state != "provisioning":
                    return
            if self.channel_moves:
                # spec 2.5: a radio's channel moved in a kept session: an unsolicited Channel
                # Preference Report (its M1's capabilities named the old channel), its new channel
                # the only operable one while a station on it holds it; the Operating Channel
                # Report follows from the channel procedures
                for frame in fragment_message(
                    self.binding.controller_al,
                    self.binding.local_al,
                    0x8005,
                    self.mids.next(),
                    moved_preferences(self.capabilities, self.channel_moves, snapshot.held_radios),
                ):
                    snapshot.stamp.check(self._stamp(), self.clock())
                    self.send_frame(frame)
                self.channel_moves = ()
                self._record("channel_preference_report_unsolicited")
                if self.channels:
                    self.channels.moved()
            # Notify only observed topology changes, never desired Config changes, and once
            # after a kept reconnection (spec 2.5: the pod's uplink may have moved).
            if snapshot.topology.operational != self.last_topology or self.notify_due:
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
                self.notify_due = False
                self._record("observed_topology_notification")
            if self.reannounce.due(self.reports.counts.get("topology_response_sent", 0)):
                self.last_clients = set()
                self._record("clients_reannounced")
            if snapshot.topology.inventory_complete:
                clients = {
                    (b.bssid, c.mac) for b in snapshot.topology.clients.bsses for c in b.clients
                }
                for bssid, mac in sorted(clients ^ self.last_clients):
                    joined = (bssid, mac) in clients
                    for frame in fragment_message(
                        MULTICAST,
                        self.binding.local_al,
                        1,
                        self.mids.next(),
                        (
                            Tlv(1, self.binding.local_al),
                            Tlv(0x92, mac + bssid + bytes([0x80 if joined else 0])),
                        ),
                        relay=True,
                    ):
                        snapshot.stamp.check(self._stamp(), self.clock())
                        self.send_frame(frame)
                        snapshot.stamp.check(self._stamp(), self.clock())
                    self._record(
                        "client_join_notification" if joined else "client_leave_notification"
                    )
                    if not joined:
                        # EasyMesh 6.1 §6.3 also requires final statistics/reason.
                        # Membership alone cannot supply these. Retain the gap.
                        self._record("disassociation_statistics_unavailable")
                        if len(self.departures) < 64:
                            self.departures[(bssid, mac)] = self.clock()
                    if joined:
                        self.departures.pop((bssid, mac), None)
                        self.last_clients.add((bssid, mac))
                    else:
                        self.last_clients.discard((bssid, mac))

    def _check_bridge(self, bridge, radio):
        """An exchange's M1 must carry exactly what the reports say of its radio."""
        if (
            bridge.exchange.binding != self.binding
            or bridge.exchange.message_set != self.message_set
            or bridge.exchange.basic != radio.basic
            or bridge.exchange.profile2 != self.capabilities.profile2
            or bridge.exchange.advanced != radio.advanced
        ):
            bridge.close()
            raise EmosaError(Reason.NOT_READY, "WSC/report capabilities disagree")

    def _admitted(self, bridge):
        """Every step of the exchange also needs this session's report source still current."""
        original_context = bridge.current_context

        async def admitted_context():
            context = await original_context()
            if self._snapshot() is None:
                raise EmosaError(Reason.NOT_READY, "discovery report source lost")
            return context

        bridge.current_context = admitted_context

    async def _next_radio(self, snapshot):
        """Spec 9: the next further radio's own M1, once the radio before it has taken its M2
        (one exchange awaits an M2 at a time). A failed start ends the attempt, as a failed
        admission does: the recovery makes a fresh one (an unchanged M2 is then a no-op)."""
        radios = self.capabilities.radios
        if (
            self.radio_bridge_factory is None
            or self.radio_awaiting is not None
            or self.next_radio >= len(radios)
        ):
            return
        index, radio = self.next_radio, radios[self.next_radio]
        self.next_radio += 1
        try:
            bridge = self.radio_bridge_factory(snapshot, self.mids, index)
            self._check_bridge(bridge, radio)
            self._admitted(bridge)
            session = ComponentProvisioningSession(bridge, self.send_frame, clock=self.clock)
            self.radio_sessions[radio.basic.ruid] = self.radio_awaiting = session
            await session.start()
            self._record("radio_m1_sent")
        except (EmosaError, OSError):
            self.close()
            self.state = "failed"
            self._record("radio_m1_failed")

    def _wsc_session(self, fragment):
        """The exchange a WSC frame is for (spec 9): the radio its M2's AP Radio Identifier names
        (in the first fragment, which a TLV-boundary fragmentation keeps whole), its later
        fragments with it; else the fronthaul's, whose exchange checks the radio again."""
        key = (fragment.source, fragment.mid)
        if fragment.fid:
            session = self.wsc_routes.get(key, self.provisioning)
        else:
            try:
                tlvs = parse_tlvs(fragment.payload, final=fragment.last)
            except EmosaError:
                tlvs = ()
            ruid = next((t.value for t in tlvs if t.kind == 0x82), None)
            session = self.radio_sessions.get(ruid, self.provisioning)
            if not fragment.last:
                # a few M2s in flight at most: the oldest route goes first
                self.wsc_routes = dict(list(self.wsc_routes.items())[-3:]) | {key: session}
        if fragment.last:
            self.wsc_routes.pop(key, None)
        return session

    @property
    def awaiting_m2(self):
        """An M1 is out, of the fronthaul or of a further radio, and its M2 not yet taken."""
        return self.state == "awaiting_m2" or (
            self.state == "provisioning" and self.radio_awaiting is not None
        )

    async def receive(self, frame, *, ingress, generation):
        if self.state in ("closed", "source_lost", "failed", "incompatible"):
            return self._record("inactive_input")
        now = self.clock()
        try:
            fragment = decode_frame(frame)
            self.binding.check(fragment, ingress=ingress, generation=generation)
        except EmosaError:
            # Not from this agent's controller to this agent (e.g. the other agents'
            # 1905 traffic on a shared LAN), or malformed: dropped without spending
            # the rate budget, and without touching the session (an admission in
            # progress must not fail because of someone else's frame).
            return self._record("foreign_frame")
        self.tokens = min(32, self.tokens + max(0, now - self.token_time) * 16)
        self.token_time = now
        if self.tokens < 1:
            return self._record("rate_limited")
        self.tokens -= 1
        try:
            snapshot = self._snapshot()
            if snapshot is None:
                return self._record("source_unavailable")
            if fragment.message_type == 9:
                if self.provisioning is None:
                    return self._record("wsc_before_admission")
                provisioning = self._wsc_session(fragment)
                result = await provisioning.receive(frame, ingress=ingress, generation=generation)
                if provisioning is not self.provisioning:
                    if provisioning is self.radio_awaiting and (
                        result["status"] == "operation"
                        or result.get("reason")
                        in (Reason.INVALID_INPUT, Reason.UNSUPPORTED_OPERATION)
                    ):
                        # taken, or refused for good (spec 3.4): the next radio's M1 may go; a
                        # refused set leaves the radio as it is, a fresh M1 would get the same
                        self.radio_awaiting = None
                    return self._record("radio_wsc_" + result["status"])
                if result["status"] == "operation":
                    self.state = "provisioning"
                    if self.reports:
                        self.reannounce.provisioned(
                            self.reports.counts.get("topology_response_sent", 0)
                        )
                return self._record("wsc_" + result["status"])
            if fragment.message_type in (2, 0x8000) and self.reports:
                if fragment.message_type == 0x8000 and self.disassociations:
                    result = self.disassociations.ack(frame, ingress=ingress, generation=generation)
                    if result:
                        return self._record(result)
                if fragment.message_type == 0x8000 and self.channels:
                    result = self.channels.ack(frame, ingress=ingress, generation=generation)
                    if result:
                        return self._record(result)
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
                self.issues = non_dpp_admission(advertisement, message_set=self.message_set)
                if self.issues:
                    self.state = "incompatible"
                    return self._record("response_incompatible")
                self.state = "admitting"
                self.reports = ReportCoordinator(
                    self.source,
                    self.send_frame,
                    mids=self.mids,
                    clock=self.clock,
                    message_set=self.message_set,
                )
                self.disassociations = DisassociationCoordinator(
                    self.source, self.send_frame, self.mids, profile=1, clock=self.clock
                )
                self.link_metrics = LinkMetricCoordinator(
                    self.link_metric_source, self.send_frame, clock=self.clock
                )
                policy = lambda: (  # noqa: E731
                    self.reporting_policy.value["policy"]
                    if self.reporting_policy and self.reporting_policy.value
                    else {}
                )
                if self.pod_metrics is not None:
                    self.ap_metrics = PodMetricReporter(
                        self.source,
                        self.pod_metrics["stats"],
                        self.send_frame,
                        self.mids,
                        admitted=lambda: self.state == "provisioning",
                        policy=policy,
                        esp_be=self.pod_metrics["esp_be"],
                        freshness=self.pod_metrics["freshness"],
                        clock=self.clock,
                    )
                else:
                    self.ap_metrics = APMetricCoordinator(
                        self.ap_metric_source,
                        self.send_frame,
                        self.mids,
                        admitted=lambda: self.state == "provisioning",
                        policy=policy,
                        clock=self.clock,
                    )
                if self.channel_store is not None:
                    self.channels = ChannelCoordinator(
                        self.source,
                        self.send_frame,
                        self.channel_store,
                        self.mids,
                        clock=self.clock,
                        reset_policy=self.reset_channel_policy,
                    )
                self.unassociated = UnassociatedCoordinator(
                    self.source,
                    self.send_frame,
                    self.mids,
                    probes=self.probes,
                    watch=self.watch,
                    clock=self.clock,
                )
                if self.steering_executor is not None:
                    self.steering = SteeringCoordinator(
                        self.source,
                        self.send_frame,
                        self.steering_executor,
                        self.mids,
                        clock=self.clock,
                    )
                if self.backhaul_steering_executor is not None:
                    self.backhaul_steering = BackhaulSteeringCoordinator(
                        self.source,
                        self.send_frame,
                        self.backhaul_steering_executor,
                        self.backhaul_steering_outcome,
                        clock=self.clock,
                        shared=self.backhaul_steering_state,
                        associated=self.backhaul_steering_associated,
                    )
                if self.reporting_policy_store is not None:
                    self.reporting_policy = ReportingPolicyCoordinator(
                        self.source,
                        self.send_frame,
                        self.reporting_policy_store,
                        reporter=self.ap_metrics,
                        clock=self.clock,
                    )
                if self.message_set != R1:
                    self.reports.notify_early()
                bridge = self.factory(snapshot, self.mids)
                self._check_bridge(bridge, self.capabilities.radios[0])
                self.provisioning = ComponentProvisioningSession(
                    bridge, self.send_frame, clock=self.clock
                )
                self._admitted(bridge)
                self._snapshot()
                if self.state == "source_lost":
                    return self._record("source_unavailable")
                await self.provisioning.start()
                self.state = "awaiting_m2"
                return self._record("early_then_m1_sent")
            if self.channels:
                result = self.channels.handle(message, now)
                if result:
                    return self._record(result)
            if self.reporting_policy:
                result = self.reporting_policy.handle(message, now)
                if result:
                    return self._record(result)
            if self.backhaul_metrics and self.state == "provisioning":
                result = self.backhaul_metrics.handle(message)
                if result:
                    return self._record(result)
            if self.link_metrics and self.state == "provisioning":
                result = self.link_metrics.handle(
                    message, now, ingress=ingress, generation=generation
                )
                if result:
                    return self._record(result)
            if self.ap_metrics and self.state == "provisioning":
                result = self.ap_metrics.handle(
                    message, now, ingress=ingress, generation=generation
                )
                if result:
                    return self._record(result)
            if self.steering and self.state == "provisioning":
                result = self.steering.handle(message, now)
                if result:
                    return self._record(result)
            if self.backhaul_steering and self.provisioning:
                # as the refusal below: the executor decides whether the pod can move now
                result = self.backhaul_steering.handle(message, now)
                if result:
                    return self._record(result)
            if self.unassociated and self.state == "provisioning":
                result = self.unassociated.handle(message, now)
                if result:
                    return self._record(result)
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
            if message.message_type == 0x8009 and self.provisioning:
                infos = [t for t in message.tlvs if t.kind == 0x90]
                if len(infos) != 1 or len(infos[0].value) != 12:
                    raise EmosaError(Reason.INVALID_INPUT, "one complete Client Info TLV required")
                if not snapshot.topology.inventory_complete:
                    raise EmosaError(Reason.NOT_READY, "client inventory unavailable")
                info = infos[0]
                mac = info.value[6:]
                associated = any(
                    c.mac == mac for b in snapshot.topology.clients.bsses for c in b.clients
                )
                # §9.2: 02 = not associated; 03 = associated but frame unavailable.
                tlvs = (info, Tlv(0x91, b"\x01"), Tlv(0xA3, bytes([3 if associated else 2]) + mac))
                response = PreparedReport(
                    0x800A,
                    message.mid,
                    snapshot.stamp,
                    min(now + 1, snapshot.stamp.valid_until),
                    fragment_message(
                        self.binding.controller_al, self.binding.local_al, 0x800A, message.mid, tlvs
                    ),
                )
                response.send(self.send_frame, self._stamp, clock=self.clock)
                return self._record("client_capability_unavailable_report")
            if message.message_type == 0x8027 and self.provisioning:
                # Backhaul STA Capability Report: one Backhaul STA Radio Capabilities TLV
                # (RUID, MAC-included flag, STA MAC when known) for each radio of the pod
                # with a backhaul station, enabled or not, the one in use first (EasyMesh
                # 6.1, 9.3; spec 8.3): the bands the controller may move the pod to.
                tlvs = tuple(
                    Tlv(0xCB, ruid + (b"\x80" + sta if sta else b"\x00"))
                    for ruid, sta in snapshot.topology.backhaul_radios
                )
                response = PreparedReport(
                    0x8028,
                    message.mid,
                    snapshot.stamp,
                    min(now + 1, snapshot.stamp.valid_until),
                    fragment_message(
                        self.binding.controller_al, self.binding.local_al, 0x8028, message.mid, tlvs
                    ),
                )
                response.send(self.send_frame, self._stamp, clock=self.clock)
                return self._record("backhaul_sta_capability_report_sent")
            if message.message_type == 0x8019 and self.provisioning:
                # Without an uplink scope (no Multi-AP backhaul station) Backhaul
                # Steering is refused: 1905 ACK, then a Backhaul Steering Response
                # with result code 0x01 (failure) and, as with every failure, an
                # Error Code TLV (0x06, the station).
                requests = [t for t in message.tlvs if t.kind == 0x9E]
                if len(requests) != 1 or len(requests[0].value) != 14:
                    raise EmosaError(Reason.INVALID_INPUT, "one Backhaul Steering Request TLV")
                request = requests[0].value
                failure = (Tlv(0x9F, request[:12] + b"\x01"), Tlv(0xA3, b"\x06" + request[:6]))
                for kind, tlvs in ((0x8000, ()), (0x801A, failure)):
                    for piece in fragment_message(
                        self.binding.controller_al, self.binding.local_al, kind, message.mid, tlvs
                    ):
                        self.send_frame(piece)
                return self._record("backhaul_steering_refused")
            return self._record(f"unsupported_message_{message.message_type:04x}")
        except (EmosaError, OSError) as exc:
            # A failed admission/handoff never restarts implicitly.
            if self.state == "admitting":
                self.close()
                self.state = "failed"
            return self._record("rejected_" + getattr(exc, "code", "send"))

    def report_final_session(self, event):
        """Internal qualified-publisher handoff, never a substitute polling sample.

        A caller must bind the publisher's association identifier and finality;
        this lifecycle additionally requires a recently observed actual leave.
        The owned hwsim publisher does not yet provide these final records.
        """
        if type(event) is not FinalSession:
            raise EmosaError(Reason.INVALID_INPUT, "qualified final-session record required")
        departed = self.departures.get((event.bssid, event.station))
        if (
            self.state != "provisioning"
            or self._snapshot() is None
            or departed is None
            or not 0 <= self.clock() - departed < 2
            or event.observed_at < departed - 2
        ):
            raise EmosaError(Reason.NOT_READY, "no fresh admitted client departure")
        return self._record(self.disassociations.submit(event))

    def close(self):
        for component in (
            self.discovery,
            self.reports,
            self.provisioning,
            self.channels,
            self.reporting_policy,
            self.disassociations,
            self.link_metrics,
            self.ap_metrics,
            self.steering,
            self.backhaul_steering,
            self.unassociated,
            *self.radio_sessions.values(),
        ):
            if component:
                component.close()
        self.radio_awaiting = None
        self.departures.clear()
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
            # spec 9: each further radio's own exchange, in the order their M1s went
            "radios": [
                {
                    "ruid": ruid.hex(":"),
                    "awaiting_m2": session is self.radio_awaiting,
                    **{k: v for k, v in session.status().items() if k in ("counts", "closed")},
                }
                for ruid, session in self.radio_sessions.items()
            ],
            "full_profile_qualified": False,
            "physical_pod_proven": False,
            "reporting_policy": self.reporting_policy.status() if self.reporting_policy else None,
            "steering": self.steering.status() if self.steering else None,
            "backhaul_steering": self.backhaul_steering.status()
            if self.backhaul_steering
            else None,
            "unassociated_metrics": self.unassociated.status() if self.unassociated else None,
            "ap_metrics": (
                self.ap_metrics.status()
                if self.pod_metrics is not None
                else {
                    "counts": dict(self.ap_metrics.counts),
                    "measurement_available": self.ap_metric_source.current() is not None,
                }
            )
            if self.ap_metrics
            else None,
            "neighbor_link_metrics": {
                "counts": dict(self.link_metrics.counts),
                "measurement_available": self.link_metric_source.current() is not None,
            }
            if self.link_metrics
            else None,
            "channels": {
                "counts": dict(self.channels.counts),
                "operating_ack_pending": self.channels.pending is not None,
                "last_decline": self.channels.last_decline,
            }
            if self.channels
            else None,
            "disassociations": {
                "counts": dict(self.disassociations.counts),
                "pending": len(self.disassociations.pending),
            }
            if self.disassociations
            else None,
        }


class OnboardingRecovery:
    """Fresh protocol attempts after source loss; never restore a WSC transcript.

    The factory must create a new OnboardingSession on the same qualified source.
    Link/pod binding is still checked by that source. Incompatible admission is
    terminal; repeated transient failures back off with bounded retained state.
    """

    def __init__(self, source, factory, *, clock=time.monotonic):
        self.source, self.factory, self.clock = source, factory, clock
        self.session = None
        self.starts = self.failures = 0
        self.next_start = 0
        self.closed = False
        self.history = deque(maxlen=32)

    @property
    def state(self):
        if self.closed:
            return "closed"
        return (
            self.session.state
            if self.session
            else "recovering"
            if self.starts
            else "waiting_source"
        )

    async def tick(self):
        if self.closed:
            return
        if self.session is None:
            if self.clock() < self.next_start or self.source.current() is None:
                return
            session = self.factory()
            if session.source is not self.source:
                session.close()
                raise EmosaError(Reason.INVALID_INPUT, "recovery factory changed source")
            self.session = session
            self.starts += 1
            self.history.append(
                {"event": "attempt_started", "attempt": self.starts, "at": self.clock()}
            )
        await self.session.tick()
        if self.session.state == "provisioning":
            self.failures = 0
        if self.session.state in ("source_lost", "failed"):
            self.history.append(
                {
                    "event": self.session.state,
                    "attempt": self.starts,
                    "at": self.clock(),
                    "session": self.session.status(),
                }
            )
            self.session.close()
            self.session = None
            self.next_start = self.clock() + min(30, 2 ** min(self.failures, 5))
            self.failures += 1

    async def receive(self, frame, *, ingress, generation):
        if self.session is None or self.closed:
            return "recovery_waiting_for_fresh_source"
        return await self.session.receive(frame, ingress=ingress, generation=generation)

    def renew(self):
        """AP-Autoconfiguration Renew (EasyMesh 6.1 §7.1): the controller asks for M1 again.

        The current attempt ends; the next tick starts a fresh one on the live
        source (discovery, Early Report, new M1). No WSC transcript is reused.
        """
        if self.closed:
            return
        self.history.append({"event": "renew", "attempt": self.starts, "at": self.clock()})
        if self.session:
            self.session.close()
            self.session = None
        self.failures, self.next_start = 0, 0

    def close(self):
        if self.session:
            self.session.close()
        self.closed = True

    def status(self):
        value = (
            self.session.status()
            if self.session
            else {"state": self.state, "counts": {}, "events": []}
        )
        return {
            **value,
            "recovery": {
                "attempts_started": self.starts,
                "next_start": self.next_start,
                "history": list(self.history),
            },
        }
