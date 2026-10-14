# SPDX-License-Identifier: Apache-2.0
"""EMOSA virtual EasyMesh agent for one real OpenSync pod.

The pod is unchanged. Its own ``cm`` dials EMOSA (the lab NOC's redirector hands
it over), EMOSA monitors the pod's OVSDB and represents the pod to the EasyMesh
controller as one agent with one radio and one fronthaul BSS:

- identity, radio MAC, BSSID, channel, SSID and associated stations come from
  the pod's own State tables (``owm`` publishes them from the driver);
- the controller's WSC M2 is the only source of a new SSID/key; it becomes one
  durable operation and one guarded OVSDB transaction (``pod_profile``);
- an operation is applied only when the pod's own State shows it.

Declared representation: the agent's 1905 interface is EMOSA's Ethernet port
on the controller's bridge. The pod's path to the gateway over OpenSync's GRE
(data plane option 2) is not an EasyMesh link and is not represented. When the
pod's backhaul station is on the EasyMesh backhaul (option 1), the agent reports
it: a non-AP STA interface on its parent BSSID, and Backhaul STA Radio
Capabilities. With ``uplink.mode = multi-ap`` the agent makes that switch
itself (``emosa.agent.uplink``).
Station association age is the time since EMOSA first observed the station
(OpenSync 6.6 has no association timestamp in OVSDB), a lower bound.

Run: ``python -m emosa.agent.pod CONFIG.json``. See deploy/opensync-lab/.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import secrets
import signal
import time
from dataclasses import replace
from pathlib import Path

from emosa.agent.gre_parent import GreTunnels
from emosa.agent.gtp_station import RETRY, UNASSOCIATED, GtpStation
from emosa.agent.peers import PeerDirectory, children, loops, mac_bytes, parent_bssids, upstream
from emosa.agent.probe_watch import ProbeWatch
from emosa.agent.radio_plan import RadioPlan
from emosa.agent.renew import CONTROLLER_TIMEOUT, M2_TIMEOUT, RenewRules
from emosa.agent.steering import ClientSteering
from emosa.agent.telemetry import MqttSubscriber, TelemetrySetup
from emosa.agent.uplink import HOLD_BACKOFF, HOLD_BACKOFF_CAP, UplinkSwitch, m2_backhaul
from emosa.agent.wired import WiredUplink
from emosa.config import validate
from emosa.errors import EmosaError, Reason
from emosa.model import ACTIVE, RadioIntent
from emosa.opensync.easymesh_view import (
    IEEE_802_11N_24,
    MEDIA,
    OPERATING_CLASSES,
    advertised_eirp,
    backhaul,
    backhaul_radios,
    bound_radios,
    device_view,
    inventory,
    operating_class,
    radios_capabilities,
    topology,
)
from emosa.opensync.gre_parent import MONITOR as GRE_PARENT_MONITOR
from emosa.opensync.gre_parent import GreParentBackend
from emosa.opensync.gtp_station import GtpStationBackend, GtpStationIntent
from emosa.opensync.pod_profile import PodBackend, RadioBackend, wpa2_psk
from emosa.opensync.probe_watch import MONITOR as WATCH_MONITOR
from emosa.opensync.probe_watch import WatchBackend
from emosa.opensync.profiles import DEFAULT as DEFAULT_PROFILE
from emosa.opensync.profiles import load as load_profile
from emosa.opensync.radio_plan import MONITOR as RADIO_PLAN_MONITOR
from emosa.opensync.radio_plan import RadioPlanBackend, RadioPlanIntent
from emosa.opensync.schema import TABLES
from emosa.opensync.session import OvsSession
from emosa.opensync.stats import PodStats
from emosa.opensync.steering import MONITOR as STEERING_MONITOR
from emosa.opensync.steering import SteeringBackend
from emosa.opensync.telemetry import MONITOR as TELEMETRY_MONITOR
from emosa.opensync.telemetry import TelemetryBackend, TelemetryIntent
from emosa.opensync.uplink import (
    BSSID,
    GRE,
    MULTI_AP,
    UplinkBackend,
    band_of_operating_class,
    lower_mac,
    new_session_start,
    start_instance,
    uplink_state,
)
from emosa.opensync.uplink import MONITOR as UPLINK_MONITOR
from emosa.opensync.wired import MONITOR as WIRED_MONITOR
from emosa.opensync.wired import WIRED, WiredBackend, WiredIntent
from emosa.reconcile import Engine
from emosa.secrets import SecretStore
from emosa.store import Store
from emosa.wire.autoconfiguration import (
    EASYMESH_61,
    PeerBinding,
    WscExchange,
    check_message_set,
)
from emosa.wire.channel import ChannelPolicyStore, OperatingRadio
from emosa.wire.cmdu import MULTICAST, MidSequence, Tlv, decode_frame, fragment_message
from emosa.wire.coordinator import ReportSource
from emosa.wire.ethernet import EthernetEndpoint
from emosa.wire.link_metrics import BackhaulPair
from emosa.wire.onboarding import OnboardingRecovery, OnboardingSession
from emosa.wire.operation_bridge import ComponentTarget, WscComponentBridge
from emosa.wire.reporting_policy import ReportingPolicyStore
from emosa.wire.reports import capabilities_identity
from emosa.wsc_messages import M1Device

log = logging.getLogger("emosa.agent")
RENEW = 0x000A  # AP-Autoconfiguration Renew
TOPOLOGY_QUERY = 0x0002
DISCOVERY_PERIOD = 60  # IEEE 1905.1 Topology Discovery interval
# When the agent asks for a fresh attempt on its own: emosa.agent.renew
RENEWALS = {
    "unserved": "provisioned, but the pod serves no BSS: fresh M1",
    "no_m2": f"no M2 for {M2_TIMEOUT}s after M1: fresh attempt",
    "no_topology_query": "provisioned, no Topology Query in topology_query_window: fresh attempt",
    "controller_silent": f"no message from the controller for {CONTROLLER_TIMEOUT}s: fresh attempt",
}
# The pod's State is re-read on this cadence, not on every received frame: the
# published report lives 1.5 s, which this refreshes three times over.
REFRESH_PERIOD = 0.5
# spec 2.5: a provisioned session is held this long while its pod's link is away, and kept when
# the same pod comes back within it with the same capabilities apart from their operable
# channels. OpenSync's CM opens the link again 15 to 18 s after a backhaul move; the window
# stays well inside a controller's own silence windows (RDK: a Topology Query every 15 s, and
# EMOSA's topology_query_window of 120 s in its lab), so a held agent never outlives the
# controller's tolerance.
RECONNECT_WINDOW = 60
# the band of a reported radio, by the operating class its capabilities advertise (spec 9)
BAND_OF_OPERATING_CLASS = {
    opclass: band for band, classes in OPERATING_CLASSES.items() for opclass, _ in classes
}
MONITOR = {
    **TABLES,
    "AWLAN_Node": [*TABLES["AWLAN_Node"], "id"],
    "Wifi_Radio_State": [*TABLES["Wifi_Radio_State"], "tx_power"],
}
for _scope in (
    UPLINK_MONITOR,
    TELEMETRY_MONITOR,
    STEERING_MONITOR,
    WATCH_MONITOR,
    WIRED_MONITOR,
    GRE_PARENT_MONITOR,
    RADIO_PLAN_MONITOR,
):
    for _table, _columns in _scope.items():  # the other scopes, each column once
        MONITOR[_table] = list(dict.fromkeys([*MONITOR.get(_table, []), *_columns]))


async def timed(label, awaitable, slow=0.5):
    """Await, and log a step of the agent loop that holds up the pod's state refresh."""
    start = time.monotonic()
    try:
        return await awaitable
    finally:
        spent = time.monotonic() - start
        if spent > slow:
            log.warning("slow %s: %.1f s", label, spent)


def mac(text):
    value = bytes.fromhex(text.replace(":", ""))
    if len(value) != 6:
        raise ValueError(f"not a MAC address: {text!r}")
    return value


def from_controller(frame, controller):
    """The message type of a frame sent by the bound controller, else None."""
    try:
        fragment = decode_frame(frame)
    except EmosaError:
        return None
    return fragment.message_type if fragment.source == controller else None


def topology_discovery(al, mids):
    """IEEE 1905.1 Topology Discovery: AL MAC TLV and the sending interface's MAC.

    Every 1905 device sends it on each interface every 60 s (not relayed), so
    neighbours, including a restarted controller, learn the agent. The agent's
    interface MAC is its AL MAC here (its own macvlan).
    """
    return fragment_message(MULTICAST, al, 0, mids.next(), (Tlv(1, al), Tlv(2, al)))


def write(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")
    os.replace(tmp, path)


def status_path(config, state_dir):
    """Where the agent writes its status (spec §6): <run_dir>/status.json when the
    configuration names a run directory (a RAM disk on a gateway), with
    <state_dir>/status.json a link to it, so readers keep one path and the state
    directory is not written once a second; else <state_dir>/status.json."""
    if not config.get("run_dir"):
        return state_dir / "status.json"
    run_dir = Path(config["run_dir"])
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target, link = run_dir / "status.json", state_dir / "status.json"
    if not (link.is_symlink() and os.readlink(link) == str(target)):
        tmp = state_dir / "status.json.link"
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(target)
        os.replace(tmp, link)
    return target


FRESHNESS = 240  # a measurement's lifetime without the agent's own telemetry settings (spec 3.6)


def direction(seen, errors, packets, value):
    """(packet errors, packets, rate or SNR) from a station's measurement, or None when any of
    the three is unknown (spec 3.6: an absent counter is unknown until measured)."""
    if not isinstance(seen, dict):
        return None
    found = (seen.get(errors), seen.get(packets), seen.get(value))
    return (
        found
        if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in found)
        else None
    )


def further_radios_changed(before, after):
    """Only the further radios of two capability reports differ (spec 9: a second radio's channel
    follows the pod's station, and it is reported once the pod brings it up): the fronthaul radio
    and the device's own capabilities are the same."""
    return (
        bool(before.radios)
        and bool(after.radios)
        and before.radios[0] == after.radios[0]
        and replace(before, radios=()) == replace(after, radios=())
    )


class PodReportSource:
    """What the agent reports about its pod: the bound BSS (and managed slot BSSes).

    The translation itself is :mod:`emosa.opensync.easymesh_view`; this class
    chooses what the agent represents and keeps the report source current.
    """

    def __init__(self, backend, binding, pod_id, stations=(), directory=None):
        self.backend, self.binding = backend, binding
        # the pod's backhaul stations: the one cm uses as a Multi-AP uplink is its EasyMesh
        # backhaul (option 1); each is a backhaul STA radio of the pod (spec 8.3)
        self.stations = tuple(s for s in stations if s)
        self.directory = directory  # the fleet's other agents (spec 8.5), or None
        self.links = ()  # the pod's 1905 neighbors on its Wi-Fi backhaul: (interface, AL)
        self.uplink, self.backhaul_stations = None, {}  # as of the last refresh
        self.backhaul_media = {}  # each backhaul BSS's 1905 media type (its radio's band)
        self.agent = binding.local_al
        self.revision, self.last = 0, None
        self.first_seen = {}  # station MAC -> monotonic time EMOSA first saw it active
        self.facts = None  # the pod's identity/radio/BSS facts behind the published reports
        self.source = ReportSource(
            binding,
            pod_id,
            hashlib.sha256(
                f"{backend.profile.id}:sole-2.4G-fronthaul:PSK-CCMP:nonDPP:v1".encode()
            ).hexdigest(),
        )
        self.capabilities = self.inventory = None
        self.capabilities_generation = None  # the pod source the capabilities are from
        self.further_channels = {}  # RUID -> channel of each further radio reported (spec 9)
        # spec 2.5: the sources that are one pod source to the session (a reconnection of the
        # same pod with the same capabilities apart from their operable channels, back within
        # RECONNECT_WINDOW); a new one for any other source
        self.lineage = 0
        self.identity = None  # (serial, node ID) of the pod last published
        self.instance = None  # the start of the pod's OpenSync last published (its radio rows)
        self.published_at = None  # when it was last published
        self.reconnections = {"continued": 0, "fresh": {}}
        # spec 3.10: a legacy pod's starts as its agent counts them (its sessions), this one first
        self.session_starts = 1 if backend.profile.legacy else None

    def new_start(self, why):
        """spec 3.10: a legacy pod's new start, counted by its agent's sessions (its radio rows
        come back from its vendor's seed with the same UUIDs): a new ownership period, and the
        writes made once per start written again where the pod lacks them (a re-claim)."""
        new_session_start()
        self.session_starts += 1
        log.info("a legacy pod's new start (%s): its configuration claimed again", why)

    def _fresh_reason(self, device, instance, capabilities, now):
        """Why a new database generation is another pod source (spec 2.5), or None: it
        continues the last one. Only the link to the pod comes back in a continuation: the
        same start of its OpenSync (its database and the configuration in it), not a restart,
        whose database comes from its template (the engine writes a configuration only for an
        M2, so a restarted pod needs the fresh session's)."""
        if (device.serial, device.node_id) != self.identity:
            return "another_pod"
        if instance is None or instance != self.instance:
            return "pod_restarted"
        if self.published_at is None or now - self.published_at > RECONNECT_WINDOW:
            return "window_passed"
        if capabilities_identity(capabilities) != capabilities_identity(self.capabilities):
            return "capabilities_changed"
        return None

    @staticmethod
    def _start(rows):
        """Which start of the pod's OpenSync (its radio rows), or None before it has them."""
        try:
            return start_instance(rows)
        except EmosaError:
            return None

    def _new_lineage(self, reason):
        self.lineage += 1
        fresh = self.reconnections["fresh"]
        fresh[reason] = fresh.get(reason, 0) + 1
        log.info("a new pod source (%s): a fresh session", reason.replace("_", " "))
        if self.backend.profile.legacy:
            self.new_start("a fresh session")

    def _represented(self, radio, bound):
        """The BSSes the agent represents: the bound one, then managed slot VIFs.

        A bound BSS the pod does not operate as a qualified WPA2-PSK AP is not
        represented (as on a cold pod): the radio stays reachable so the
        controller can configure it.
        """
        primary = radio.bss(self.backend.if_name) if bound else None
        if primary is not None and not (
            radio.enabled and wpa2_psk(primary.vif) and radio.channel is not None
        ):
            primary = None
        # every managed slot, whichever radio's scope writes it (spec 9): those on this radio
        extras = [radio.bss(name) for name, _, _ in self.backend.pod_slots]
        return primary, tuple(b for b in (primary, *extras) if b is not None)

    def _backhaul_links(self, uplink, bsses, media):
        """The pod's 1905 neighbors on its Wi-Fi backhaul (spec 8.5): its parent pod's agent on
        its backhaul station, and each child pod's agent on the backhaul BSS it is on, on any
        of the pod's radios (``media``: each BSS's 1905 media type, by its radio's band)."""
        if self.directory is None:
            return ()
        peers = self.directory.peers()
        links = []
        if uplink is not None:
            parent = upstream(peers, uplink.station.parent)
            if parent is not None:
                links.append((uplink.station.mac, parent.al))
        backhaul = {b.bssid: set(b.stations) for b in bsses if b.role == "backhaul"}
        self.uplink, self.backhaul_stations = uplink, backhaul
        self.backhaul_media = {bssid: media[bssid] for bssid in backhaul}
        links += [(bssid, child.al) for bssid, child in children(peers, backhaul)]
        return tuple(links)

    def backhaul_pairs(self, stats, freshness, now=None):
        """The Wi-Fi backhaul neighbors and their links' measurements (spec 8.5): a child's
        from this pod's own measurement of its station, the parent's from the parent's
        measurement of this pod's station (its directions swapped), each only while current."""
        if self.directory is None:
            return ()
        now = time.time() if now is None else now
        peers = self.directory.peers()
        pairs = []
        uplink = self.uplink
        if uplink is not None:
            parent = upstream(peers, uplink.station.parent)
            if parent is not None:
                seen = parent.measured.get(uplink.station.mac.hex(":"))
                if not isinstance(seen, dict) or now - (seen.get("measured_at") or 0) > freshness:
                    seen = None
                pairs.append(
                    BackhaulPair(
                        parent.al,
                        uplink.station.mac,
                        uplink.station.parent,
                        MEDIA[uplink.band],
                        direction(seen, "rx_errors", "rx_frames", "rx_rate_mbps"),
                        direction(seen, "tx_errors", "tx_frames", "snr_db"),
                    )
                )
        current = stats.current() if stats is not None else {}
        for bssid, child in children(peers, self.backhaul_stations):
            seen = current.get(child.station.hex(":"))
            seen = seen.public() if seen is not None else None
            pairs.append(
                BackhaulPair(
                    child.al,
                    bssid,
                    child.station,
                    self.backhaul_media.get(bssid, IEEE_802_11N_24),
                    direction(seen, "tx_errors", "tx_frames", "tx_rate_mbps"),
                    direction(seen, "rx_errors", "rx_frames", "snr_db"),
                )
            )
        return tuple(pairs)

    async def refresh(self):
        started = time.monotonic()
        try:
            raw = await self.backend.session.snapshot()
            _, _, _, state, rows = self.backend._binding(raw)
            ident = self.backend.identity
            if not raw["ready"] or not ident:
                raise EmosaError(Reason.NOT_READY, "bound radio State unavailable")
            device = device_view(rows, parent_aps=self.backend.parent_aps)
            radio = device.radio(mac(ident["radio_mac"]))
            if radio is None:
                raise EmosaError(Reason.NOT_READY, "bound radio absent from the view")
            primary, bsses = self._represented(radio, bool(state))
            # A cold pod has the radio but no BSS yet: advertise the radio on the
            # channel the profile would create the BSS on, and no BSS.
            channel = radio.channel if primary else self.backend.channel
            # The capabilities are fixed for one pod source (spec 2.4): a new database
            # generation (the pod recreated, maybe with another radio) is a new source with
            # a new session, and its capabilities are taken again.
            fresh = self.capabilities is None or raw["generation"] != self.capabilities_generation
            # spec 9: report the fronthaul radio and each of the profile's additional radios (the
            # second radio of 8.6), each operable only on its own channel.
            bound = bound_radios(
                device.radios,
                radio,
                channel,
                additional=self.backend.profile.radios,
                slots=self.backend.pod_slots,
                slot_bands=self.backend.profile.slot_bands,
                held={} if fresh else self.further_channels,
            )
            self.further_channels = {r.ruid: ch for r, ch, _ in bound[1:]}
            # each radio's advertised maximum EIRP so far: a source keeps its own (spec 2.5)
            previous = (
                {
                    r.basic.ruid: r.basic.operating_classes[0].max_eirp_dbm
                    for r in self.capabilities.radios
                }
                if self.capabilities is not None
                else {}
            )
            if fresh and self.capabilities is not None:
                # spec 2.5: the same pod back within the reconnection window with the same
                # capabilities apart from their operable channels (its maxima kept) continues the
                # source: the session is kept. Anything else is a new source, a fresh session.
                continued = radios_capabilities(
                    tuple(
                        (r, ch, mbss, advertised_eirp(previous.get(r.ruid), r.tx_power))
                        for r, ch, mbss in bound
                    )
                )
                why = self._fresh_reason(device, self._start(rows), continued, started)
                if why is None:
                    fresh = False
                    self.capabilities_generation = raw["generation"]
                    self.reconnections["continued"] += 1
                else:
                    self._new_lineage(why)
            stored = previous if not fresh else {}

            def _eirp(r):
                return advertised_eirp(stored.get(r.ruid), r.tx_power)

            capabilities = radios_capabilities(
                tuple((r, ch, mbss, _eirp(r)) for r, ch, mbss in bound)
            )
            # the same radios as advertised so far: different only when a radio's power rose
            kept = radios_capabilities(
                tuple((r, ch, mbss, stored.get(r.ruid, _eirp(r))) for r, ch, mbss in bound)
            )
            max_eirp = _eirp(radio)  # the fronthaul radio's, for the Operating Channel Report below
            if fresh:
                if self.capabilities is not None and capabilities != self.capabilities:
                    log.info("a new pod source with other radio capabilities: taken for it")
                self.capabilities = capabilities
                self.capabilities_generation = raw["generation"]
                self.inventory = inventory(device, radio, self.backend.profile.chipset.encode())
            elif capabilities != self.capabilities:
                if further_radios_changed(self.capabilities, capabilities):
                    # spec 9: a further radio's channel follows the pod's station (and the radio
                    # appears once the pod brings it up): its capabilities are taken again. A
                    # channel move keeps a provisioned session (spec 2.5: its new operable channel
                    # reported); a radio that came or went starts it afresh with new M1s.
                    log.info("a further radio moved channel or came and went: capabilities taken")
                    self.capabilities = capabilities
                elif kept == self.capabilities:
                    # spec 2.5: a radio reports more power than the maximum EIRP it advertised (the
                    # source began before the pod reported any): its capabilities are taken again
                    # with the higher maximum, and the session that has the old ones starts afresh
                    log.info(
                        "a radio reports more power than it advertised: capabilities taken (%s)",
                        ", ".join(
                            f"{r.ruid.hex(':')} {stored.get(r.ruid)} -> {_eirp(r)} dBm"
                            for r, _, _ in bound
                            if stored.get(r.ruid) != _eirp(r)
                        ),
                    )
                    self.capabilities = capabilities
                else:
                    # spec 2.5: the fronthaul radio's channel moved (its pod's uplink moved it, the
                    # radio shared with its station) or another radio is in its place: another pod
                    # source, a fresh session with these capabilities, never a refusal (that would
                    # make a channel move an outage). Spec 9's 2.4 GHz contract is channel 6 only
                    # today: the fronthaul's move is not kept with its reports yet.
                    self._new_lineage(
                        "fronthaul_moved"
                        if capabilities_identity(capabilities)
                        == capabilities_identity(self.capabilities)
                        else "capabilities_changed"
                    )
                    self.capabilities = capabilities
                    self.inventory = inventory(device, radio, self.backend.profile.chipset.encode())
            uplink = None
            for station in self.stations:
                # a wireless backhaul station: a Multi-AP (4-address) uplink, or a 3-address
                # station under a GRE tunnel (a GRE child, or the bootstrap/GTP path). The medium
                # is 802.11 either way, so the pod reports it as its Wi-Fi backhaul (spec 8.6).
                if uplink_state(rows, station)["kind"] in (MULTI_AP, GRE):
                    uplink = backhaul(device, station)
                    if uplink is not None:
                        break
            radios_with_station = backhaul_radios(
                rows, self.stations, uplink.station.if_name if uplink else None
            )
            # spec 9: each further bound radio is its own Operational/Configured radio, with the
            # EMOSA BSSes it carries (a parent's b-ap-50 on 5 GHz) or none (a child's, its station
            # only), as the capabilities list every bound radio
            extra_radios = tuple((r, self._represented(r, True)[1], ch) for r, ch, _ in bound[1:])
            # every BSS the agent represents, on any radio: their stations, the child pods on them
            every = bsses + tuple(b for _, bs, _ in extra_radios for b in bs)
            media = {
                b.bssid: MEDIA[r.band]
                for r, bs, _ in ((radio, bsses, channel), *extra_radios)
                for b in bs
            }
            now = time.monotonic()
            active = {m for b in every for m in b.stations}
            self.first_seen = {m: self.first_seen.get(m, now) for m in active}
            self.links = self._backhaul_links(uplink, every, media)
            report = topology(
                agent_al=self.agent,
                controller_al=self.binding.controller_al,
                radio=radio,
                channel=channel,
                bsses=bsses,
                ages={m: now - t for m, t in self.first_seen.items()},
                uplink=uplink,
                associated_at=self.first_seen,
                peers=self.links,
                backhaul_radios=radios_with_station,
                extra_radios=extra_radios,
            )
            facts = (
                raw["generation"],
                raw["revision"],
                tuple((b.bssid, b.ssid, b.stations) for b in every),
                radio.tx_power,
                uplink,
                self.links,
                radios_with_station,
                # spec 9: a further radio's channel and power (its capabilities and its
                # Operating Channel Report follow them)
                tuple((r.ruid, ch, r.tx_power) for r, ch, _ in bound),
                self.capabilities,
            )
            if facts != self.last:
                self.revision += 1
                self.last = facts
            self.facts = {
                "serial": device.serial,
                "node_id": device.node_id,
                "firmware": device.firmware,
                "radio": ident["radio_if_name"],
                "ruid": ident["radio_mac"],
                "bssid": ident["bssid"],
                "channel": channel,
                # spec 9: every radio EMOSA binds and reports (the fronthaul first), for the
                # status and the lab proofs - a second radio appears once the pod brings it up
                "radios": [
                    {"ruid": r.ruid.hex(":"), "band": r.band, "channel": ch} for r, ch, _ in bound
                ],
                "ssid": primary.ssid if primary else None,
                "bsses": [
                    {"role": b.role, "bssid": b.bssid.hex(":"), "ssid": b.ssid} for b in every
                ],
                "stations": sorted(m.hex(":") for m in active),
                "backhaul": None
                if uplink is None
                else {
                    "station": uplink.station.if_name,
                    "mac": uplink.station.mac.hex(":"),
                    "parent": uplink.station.parent.hex(":"),
                    "band": uplink.band,
                    "channel": uplink.channel,
                },
                "backhaul_neighbors": [
                    {"interface": local.hex(":"), "al": al.hex(":")} for local, al in self.links
                ],
                "ovsdb_generation": raw["generation"],
                "ovsdb_revision": raw["revision"],
            }
            # Facts may change only with a new source revision: the published ages
            # are taken when membership changes, and a Topology Response ages them
            # from the association times as it is sent.
            if self._clients is not None and self._clients[0] == self.revision:
                report = replace(report, clients=self._clients[1])
            self._clients = (self.revision, report.clients)

            def operated(r, opclass, ch, eirp):
                """A radio's operating parameters at its reported power, within the power it
                advertises; with no power reported (OpenSync leaves Wifi_Radio_State.tx_power
                unset when the radio's VIFs disagree) at its advertised maximum EIRP, a bound
                (spec 9: refused, a controller renews the agent every minute)."""
                if r.tx_power is None:
                    return (OperatingRadio(r.ruid, opclass, ch, eirp, bound=True),)
                return (
                    (OperatingRadio(r.ruid, opclass, ch, r.tx_power),) if r.tx_power <= eirp else ()
                )

            # Operating parameters (channel procedures, Operating Channel Report) only while
            # the BSS operates on the one supported channel.
            operating = operated(radio, 81, channel, max_eirp) if primary and channel == 6 else ()
            # spec 9: each further radio on its own observed channel (it follows the pod's
            # station)
            for r, ch, _ in bound[1:]:
                opclass = operating_class(r.band, ch)
                if opclass is not None:
                    operating += operated(r, opclass, ch, _eirp(r))
            self.source.publish(
                (raw["generation"], self.revision),
                self.capabilities,
                report,
                observed_at=started,
                lifetime=1.5,  # (t + 2) - t can exceed 2 in floating point
                operating_radios=operating,
                lineage=self.lineage,
            )
            self.identity, self.published_at = (device.serial, device.node_id), started
            self.instance = self._start(rows)
            self.refused = None
            return True
        except (EmosaError, ConnectionError, TimeoutError) as exc:
            # Once per cause: a pod that stays away must not flood the log, but a
            # new reason (a replaced radio after a reconnect) must show.
            if self.facts is not None or str(exc) != self._unavailable:
                log.info("pod source unavailable: %s", exc)
            self._unavailable = str(exc)
            # spec 3.10: a pod whose schema is not the profile's generation is refused, and said so
            mismatch = isinstance(exc, EmosaError) and exc.code == Reason.SCHEMA_MISMATCH
            self.refused = Reason.SCHEMA_MISMATCH.value if mismatch else None
            self.facts = None
            self.source.invalidate()
            return False

    _clients = None
    _unavailable = None
    refused = None  # SCHEMA_MISMATCH while the pod's schema is not the profile's generation


def make_bridge(
    engine,
    backend,
    report,
    run_id,
    target,
    mids,
    capabilities,
    message_set=EASYMESH_61,
    m2_session="distinct",
    radio_index=0,
):
    node = report.facts
    uuid = hashlib.sha256(("emosa-agent:" + node["serial"]).encode()).digest()[:16]
    # spec 9: a WSC exchange per bound radio; radio_index picks it, rf_band follows its band
    radio = capabilities.radios[radio_index]
    rf_band = 1 if radio.basic.operating_classes[0].operating_class == 81 else 2  # 1=2.4, 2=5 GHz
    device = M1Device(
        uuid=uuid,
        al_mac=report.binding.local_al,
        authentication_types=0x20,  # WPA2-PSK
        encryption_types=8,  # AES
        connection_types=1,
        configuration_methods=0x0280,
        wps_state=2,
        manufacturer=b"OpenSync via EMOSA",
        model_name=b"OpenSync pod",
        model_number=(node["firmware"] or "6.6").encode()[:32],
        serial_number=node["serial"].encode()[:32],
        primary_device_type=bytes.fromhex("00060050f2040001"),
        device_name=node["serial"].encode()[:32],
        rf_band=rf_band,
        association_state=0,
        device_password_id=4,
        configuration_error=0,
        os_version=1,
    )
    exchange = WscExchange(
        report.binding,
        device,
        radio.basic,
        capabilities.profile2,
        radio.advanced,
        mids=mids,
        timeout=30,
        message_set=message_set,
        # an M2 set is taken whatever the profile's slots: a radio with none applies its
        # primary BSS (spec 3.4), as the C agent does from the same setting
        multi_bss=backend.multi_bss,
        m2_session=m2_session,
    )
    return WscComponentBridge(
        engine, exchange, target, backend.context, run_id=run_id, deadline=120
    )


def telemetry_intent(pod_id, serial, telemetry_config):
    """The statistics publishing an agent configuration asks for, or None (mode off)."""
    if telemetry_config.get("mode", "off") == "off":
        return None
    if telemetry_config["mode"] != "mqtt" or not telemetry_config.get("broker"):
        raise EmosaError(Reason.INVALID_INPUT, "telemetry: mode mqtt needs a broker")
    intent = TelemetryIntent(
        pod_id,
        telemetry_config["broker"],
        telemetry_config.get("port", 8883),
        telemetry_config.get("topic") or f"emosa/stats/{serial}",
        telemetry_config.get("radio_type", "2.4G"),
        telemetry_config.get("reporting_interval", 10),
        telemetry_config.get("sampling_interval", 5),
        telemetry_config.get("publish_interval"),
        telemetry_config.get("survey", False),
    )
    intent.validate()
    return intent


def uplink_bssid(uplink_config):
    """The upstream backhaul BSS a multi-ap uplink is pinned to, in lower case."""
    bssid = (uplink_config.get("bssid") or "").lower()
    if not BSSID.match(bssid):
        raise EmosaError(
            Reason.INVALID_INPUT, "uplink: bssid (the upstream backhaul BSS) is required"
        )
    return bssid


async def serve(config, stop):
    state_dir = Path(config["state_dir"])
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    status_file = status_path(config, state_dir)
    pod_id = config["pod_id"]
    agent, controller = mac(config["al_mac"]), mac(config["controller_al"])
    profile = load_profile(config.get("profile", DEFAULT_PROFILE))
    if profile.legacy:
        new_session_start()  # spec 3.10: the pod's starts counted by its agent's sessions
    session = OvsSession(
        config["ovsdb"], monitor_columns=MONITOR, timeout=2, schema_generation=profile.schema
    )
    vault, store = SecretStore(state_dir / "secrets"), Store(state_dir / "journal")
    backend = PodBackend(
        pod_id,
        session,
        vault,
        serial=config["serial"],
        profile=profile,
        multi_bss=config.get("multi_bss", False) is True,
        underlay=config.get("underlay"),
    )
    engine = Engine(store, vault, {pod_id: backend})
    engine.recover()
    # spec 9: each of the profile's further radios is its own scope, configured by its own M2:
    # band -> (backend, engine), each with its own journal
    radio_scopes = {}
    for extra in backend.profile.radios:
        radio_backend = RadioBackend(
            pod_id,
            session,
            vault,
            serial=config["serial"],
            band=extra["band"],
            profile=backend.profile,
            multi_bss=backend.multi_bss,
            underlay=config.get("underlay"),
        )
        radio_engine = Engine(
            Store(state_dir / f"radio-{extra['band']}"),
            vault,
            {pod_id: radio_backend},
            intent_type=RadioIntent,
        )
        radio_engine.recover()
        radio_scopes[extra["band"]] = (radio_backend, radio_engine)

    def writing():
        """A write of the controller's configuration in flight, on any of the pod's radios."""
        return any(
            op.state in ACTIVE
            for scope in (engine, *(e for _, e in radio_scopes.values()))
            for op in scope.store.operations()
        )

    # the fleet's other agents, beside this one's run directory (spec 8.5)
    directory = PeerDirectory(config["run_dir"], pod_id) if config.get("run_dir") else None
    uplink_config = config.get("uplink", {"mode": "off"})
    if profile.legacy and uplink_config["mode"] not in ("off", WIRED, GRE):
        # spec 3.10: a legacy pod has no Multi-AP station (Wifi_Credential_Config without bssid,
        # priority or enabled); a GRE child's pin is its station's parent, and its Ethernet uplink
        # is bridged by the patch pair (spec 8.4)
        raise EmosaError(
            Reason.INVALID_INPUT, f"uplink: {uplink_config['mode']} is not served on a legacy pod"
        )
    station = uplink_config.get("station") or backend.profile.uplink_station
    switch = uplink_store = None
    if uplink_config["mode"] in (MULTI_AP, GRE):
        if station is None:
            raise EmosaError(Reason.INVALID_INPUT, "uplink: no station (profile or configuration)")
        # A GRE child (spec 8.6) has no configured Multi-AP upstream: it stays on its bootstrap
        # (the GTP) until the controller steers it onto a parent AP, by that AP's BSSID. A
        # Multi-AP pod has its configured upstream BSSID from the start.
        bssid = None if uplink_config["mode"] == GRE else uplink_bssid(uplink_config)
        if uplink_config.get("credentials", "m2") == "config":
            if not uplink_config.get("ssid") or not uplink_config.get("secret_ref"):
                raise EmosaError(
                    Reason.INVALID_INPUT, "uplink: config credentials need ssid and secret_ref"
                )
            fixed = (uplink_config["ssid"], uplink_config["secret_ref"])
            credentials = lambda: fixed  # noqa: E731
        else:
            # the backhaul BSS of the fronthaul's set, else of a further radio's (spec 9)
            radio_stores = tuple(e.store for _, e in radio_scopes.values())
            credentials = lambda: m2_backhaul(store, *radio_stores)  # noqa: E731
        uplink_store = Store(state_dir / "uplink")
        switch = UplinkSwitch(
            pod_id,
            UplinkBackend(
                pod_id,
                session,
                vault,
                serial=config["serial"],
                station=station,
                stations=backend.profile.uplink_stations,
                fixed_band=backend.profile.band,
                legacy=profile.legacy,
                loop=None
                if directory is None
                else lambda target, own: loops(
                    directory.peers(),
                    agent,
                    {mac_bytes(b) for b in own},
                    mac_bytes(target),
                ),
            ),
            uplink_store,
            vault,
            credentials,
            bssid=bssid,
            run_id=config.get("run_id", pod_id),
            # the pod serves the controller's fronthaul, and no write is in flight on any radio
            settled=lambda: (report.facts or {}).get("ssid") is not None and not writing(),
            # the fleet's GRE parent APs (spec 8.6): a Backhaul Steering target among them is a
            # 3-address GRE move, read from the peers' statuses (no 1905 frame names them)
            parents=(lambda: frozenset())
            if directory is None
            else lambda: parent_bssids(directory.peers()),
            hold_backoff=uplink_config.get("hold_backoff", HOLD_BACKOFF),
            hold_backoff_cap=uplink_config.get("hold_backoff_cap", HOLD_BACKOFF_CAP),
        )
    wired = None
    if uplink_config["mode"] == WIRED:  # a wired pod: its uplink port into br-home (spec §8.4)
        port = uplink_config.get("port", "eth1")
        bridge = backend.profile.fronthaul_vif.get("bridge")
        if not bridge:
            raise EmosaError(Reason.INVALID_INPUT, "uplink: the profile's fronthaul has no bridge")
        wired = WiredUplink(
            pod_id,
            WiredBackend(
                pod_id,
                session,
                serial=config["serial"],
                port=port,
                legacy=profile.legacy,
                wan_bridge=profile.wan_bridge,
                home_bridge=bridge,
            ),
            Store(state_dir / "wired-uplink"),
            vault,
            WiredIntent(pod_id, port, bridge),
            run_id=config.get("run_id", pod_id),
        )
    gtp_station = None
    bootstrap = uplink_config.get("bootstrap")
    if bootstrap:
        # spec 3.10, the GTP station: a legacy pod's core makes none, and its seed has none
        if not profile.legacy:
            raise EmosaError(
                Reason.INVALID_INPUT, "uplink: bootstrap is a legacy pod's (spec 3.10)"
            )
        gtp_intent = GtpStationIntent(
            pod_id,
            station,
            bootstrap["ssid"],
            bootstrap["secret_ref"],
            lower_mac(bootstrap.get("bssid")),
        )
        gtp_intent.validate()
        # the station's guard (spec 3.10): unassociated this long, removed; written again after
        # the back-off (the box shortens both)
        unassociated = bootstrap.get("unassociated_seconds", UNASSOCIATED)
        retry = bootstrap.get("retry_seconds", list(RETRY))
        if (
            not isinstance(unassociated, int)
            or unassociated < 1
            or not isinstance(retry, list)
            or not 1 <= len(retry) <= 3
            or not all(isinstance(s, int) and s >= 1 for s in retry)
        ):
            raise EmosaError(
                Reason.INVALID_INPUT,
                "uplink: bootstrap's unassociated_seconds and retry_seconds are positive seconds",
            )
        bands = [b for b, s in backend.profile.uplink_stations if s == station]
        gtp_station = GtpStation(
            pod_id,
            GtpStationBackend(
                pod_id,
                session,
                vault,
                serial=config["serial"],
                station=station,
                band=bands[0] if bands else backend.profile.band,
            ),
            Store(state_dir / "gtp-station"),
            vault,
            gtp_intent,
            run_id=config.get("run_id", pod_id),
            unassociated=unassociated,
            retry=retry,
        )
    radio_plan = None
    if profile.radio_plan == "emosa":
        # spec 3.10, the radio plan: the APs the profile does not name off, its radio on its channel
        radio_plan = RadioPlan(
            pod_id,
            RadioPlanBackend(
                pod_id,
                session,
                vault,
                serial=config["serial"],
                intent=RadioPlanIntent(
                    pod_id,
                    tuple(sorted({profile.fronthaul_if, *(n for n, _, _ in profile.extra_slots)})),
                    profile.band,
                    profile.channel,
                    profile.ht_mode,
                ),
            ),
            Store(state_dir / "radio-plan"),
            vault,
            run_id=config.get("run_id", pod_id),
        )
    tunnels = None
    # a former GRE parent (its profile no longer one, its tunnels' journal kept): the tunnels left
    # on its former parent AP, its backhaul slot, are removed (spec 8.6, a parent demoted)
    former = sorted(name for name, role, _ in backend.pod_slots if role == "backhaul")
    demoted = not backend.parent_aps and bool(former) and (state_dir / "gre-parent").is_dir()
    if (backend.parent_aps and config.get("underlay")) or demoted:
        # a GRE parent (spec 8.6): a tunnel per child of its parent AP, in the fronthaul's bridge
        bridge = backend.profile.fronthaul_vif.get("bridge")
        if not bridge:
            raise EmosaError(
                Reason.INVALID_INPUT, "gre parent: the profile's fronthaul has no bridge"
            )
        tunnels = GreTunnels(
            pod_id,
            GreParentBackend(
                pod_id,
                session,
                serial=config["serial"],
                ap=former[0] if demoted else min(backend.parent_aps),
                bridge=bridge,
                # a former parent only removes: no child address is checked against it
                underlay=config.get("underlay") or "169.254.0.0/16",
                withdrawn=demoted,
            ),
            Store(state_dir / "gre-parent"),
            vault,
            run_id=config.get("run_id", pod_id),
        )
    backhaul_steering_state = {}  # a Backhaul Steering move under way outlives a session
    telemetry = stats = subscriber = telemetry_store = watch = None
    telemetry_config = config.get("telemetry", {"mode": "off"})
    wanted = telemetry_intent(pod_id, config["serial"], telemetry_config)
    if wanted is not None:
        telemetry_store = Store(state_dir / "telemetry")
        telemetry = TelemetrySetup(
            pod_id,
            TelemetryBackend(
                pod_id,
                session,
                serial=config["serial"],
                radio_type=wanted.radio_type,
                survey=wanted.survey,
            ),
            telemetry_store,
            vault,
            wanted,
            run_id=config.get("run_id", pod_id),
        )
        stats = PodStats(wanted.topic, interval=wanted.reporting_interval)
        # the stations the controller asks about, watched for their probe requests (§3.9); not on
        # a legacy pod, whose Band_Steering_Clients lacks the watch's columns (spec 3.10)
        watch = None
        if not profile.legacy:
            watch = ProbeWatch(
                pod_id,
                WatchBackend(pod_id, session, serial=config["serial"]),
                Store(state_dir / "probe-watch"),
                vault,
                if_name=backend.profile.fronthaul_if,
                band=wanted.radio_type,
                run_id=config.get("run_id", pod_id),
            )
        host, _, port = telemetry_config.get("subscribe", "127.0.0.1:1883").rpartition(":")
        subscriber = MqttSubscriber(
            host,
            int(port),
            wanted.topic,
            lambda topic, payload, retained: stats.receive(topic, payload, retained=retained),
            client_id=f"emosa-agent-{pod_id}",
        )
        subscriber.start(asyncio.get_running_loop())
    # Client steering mandates from the controller, carried out by owm (on unless "off"); off on a
    # legacy pod, whose Band_Steering_Clients lacks steering's columns (spec 3.10)
    steering = steering_store = None
    if config.get("steering", {}).get("mode", "owm") != "off" and not profile.legacy:
        steering_store = Store(state_dir / "steering")
        steering = ClientSteering(
            pod_id,
            SteeringBackend(pod_id, session, serial=config["serial"]),
            steering_store,
            vault,
            run_id=config.get("run_id", pod_id),
        )
    # AP metrics from the pod's statistics (spec §3.8): telemetry with the survey,
    # and the profile's declared best-effort ESP
    pod_metrics = None
    if stats is not None and wanted.survey and backend.profile.esp_be is not None:
        pod_metrics = {
            "stats": stats,
            "esp_be": backend.profile.esp_be,
            # a report is current for three reporting periods after its publication
            "freshness": 3 * wanted.reporting_interval + (wanted.publish_interval or 60),
        }
    binding = PeerBinding(config["interface"], 1, agent, controller, (controller,))
    # every backhaul station of the pod, the one it bootstraps on first (spec 8.3)
    stations = tuple(dict.fromkeys((station, *(s for _, s in backend.profile.uplink_stations))))
    report = PodReportSource(backend, binding, pod_id, stations, directory)
    mids = MidSequence(secrets.randbelow(65536))
    run_id = config.get("run_id", pod_id)
    # EasyMesh message set toward this controller: easymesh-6.1 unless the
    # controller needs the R1 form (see emosa.wire.autoconfiguration).
    message_set = check_message_set(config.get("message_set", EASYMESH_61))
    lifecycle, last_status, last_facts, last_write = None, None, None, 0
    next_discovery = 0
    last_kept = None  # (the session's kept reconnections, the session) last checked for drift
    renewals = RenewRules(
        time.monotonic(), topology_query_window=config.get("topology_query_window")
    )
    next_refresh, ready = 0, False
    channels = reporting = None

    def operation_status(op):
        return {
            "operation_id": op.operation_id,
            "state": op.state,
            "reason": op.reason,
            "ssid": op.intent.get("ssid"),
            "attempts": len(op.attempts),
            "application_evidence": op.application_evidence,
        }

    def status():
        snapshot = report.source.current()
        ops = [operation_status(op) for op in store.operations()]
        return {
            "pod_id": pod_id,
            "profile": backend.profile.id,
            "agent_al": agent.hex(":"),
            "controller_al": controller.hex(":"),
            "pod": report.facts,
            "report_source_available": snapshot is not None,
            "pod_refused": report.refused,
            "session": lifecycle.status() if lifecycle else None,
            # spec 2.5: new database generations that continued the pod source (the session
            # kept when it was provisioned), and those that were another source, by reason
            "reconnections": report.reconnections,
            # spec 3.10: a legacy pod's starts, counted by its agent's sessions
            **({"session_starts": report.session_starts} if profile.legacy else {}),
            "operations": ops,
            # spec 9: each further radio's scope, by band: its operations, each the BSSes of the
            # radio's own M2 set its slots map
            "radio_operations": {
                band: [
                    {**operation_status(op), "bsses": [b["ssid"] for b in op.intent["additional"]]}
                    for op in radio_engine.store.operations()
                ]
                for band, (_, radio_engine) in radio_scopes.items()
            },
            "uplink": (
                switch.status()
                if switch
                else wired.status()
                if wired
                else {"station": station, "mode": "off"}
            ),
            "telemetry": (
                {"mode": "mqtt", **telemetry.status(), "subscribed": subscriber.connected}
                | stats.status()
                if telemetry
                else {"mode": "off"}
            ),
            "steering": {"mode": "owm", **steering.status()} if steering else {"mode": "off"},
            "probe_watch": watch.status() if watch else None,
            "gre_parent": tunnels.status() if tunnels else None,
            "gtp_station": gtp_station.status() if gtp_station else None,
            "radio_plan": radio_plan.status() if radio_plan else None,
            "writes": backend.write_count,
            "worker_pid": os.getpid(),
            "updated": time.time(),
        }

    try:
        # A frame returns at once; the timeout only paces idle wakeups (timers are >= 1 s).
        # EMOSA_NETNS (/etc/default/emosa): the agents' interfaces in a namespace of their own
        with EthernetEndpoint(
            config["interface"], agent, timeout=0.2, netns=os.environ.get("EMOSA_NETNS")
        ) as endpoint:

            def radio_bridge(snap, m, index):
                """The exchange of a further radio (spec 9), into that radio's own scope: its
                target is the radio (no BSS of its own), its RUID the radio's."""
                radio = snap.capabilities.radios[index]
                opclass = radio.basic.operating_classes[0].operating_class
                radio_backend, radio_engine = radio_scopes[BAND_OF_OPERATING_CLASS[opclass]]
                ruid = radio.basic.ruid
                target = ComponentTarget(
                    pod_id, radio_backend.radio_id, radio_backend.radio_id, ruid, ruid
                )
                return make_bridge(
                    radio_engine,
                    radio_backend,
                    report,
                    run_id,
                    target,
                    m,
                    snap.capabilities,
                    message_set,
                    config.get("m2_session", "distinct"),
                    radio_index=index,
                )

            def factory():
                # Called by the recovery loop only while the source is current.
                # Before a cold pod's BSS exists the target records the radio MAC;
                # the BSSID reported afterwards comes from the pod's own State.
                ident = backend.identity
                target = ComponentTarget(
                    pod_id,
                    "radio-1",
                    "bss-1",
                    mac(ident["radio_mac"]),
                    mac(ident["bssid"] or ident["radio_mac"]),
                )
                return OnboardingSession(
                    report.source,
                    endpoint.send,
                    lambda snap, m: make_bridge(
                        engine,
                        backend,
                        report,
                        run_id,
                        target,
                        m,
                        snap.capabilities,
                        message_set,
                        config.get("m2_session", "distinct"),
                    ),
                    report.inventory,
                    mids=mids,
                    message_set=message_set,
                    channel_store=channels,
                    reporting_policy_store=reporting,
                    reset_channel_policy=lifecycle.starts == 0,
                    steering_executor=steering.start if steering else None,
                    pod_metrics=pod_metrics,
                    probes=stats,
                    watch=watch.ask if watch else None,
                    backhaul_steering_executor=None
                    if switch is None
                    else lambda bssid, operating_class, channel: switch.steer(
                        bssid, band_of_operating_class(operating_class), channel
                    ),
                    backhaul_steering_outcome=switch.steering_outcome if switch else None,
                    backhaul_steering_state=backhaul_steering_state,
                    # a successful move's answer names the backhaul STA the pod is on now
                    backhaul_steering_associated=None
                    if switch is None
                    else lambda: (
                        mac_bytes(switch.backend.facts["mac"])
                        if (switch.backend.facts or {}).get("mac")
                        else None
                    ),
                    backhaul_pairs=None
                    if directory is None
                    else lambda: report.backhaul_pairs(
                        pod_metrics["stats"] if pod_metrics else None,
                        pod_metrics["freshness"] if pod_metrics else FRESHNESS,
                    ),
                    radio_bridge_factory=radio_bridge if radio_scopes else None,
                    reconnect_window=RECONNECT_WINDOW,
                )

            channels = ChannelPolicyStore(state_dir / "channel-policy.sqlite")
            reporting = ReportingPolicyStore(
                state_dir / "reporting-policy.sqlite",
                boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            )
            lifecycle = OnboardingRecovery(report.source, factory)
            while not stop.is_set():
                refreshed = time.monotonic() >= next_refresh
                if refreshed:
                    late = time.monotonic() - next_refresh
                    if next_refresh and late > REFRESH_PERIOD:
                        # the published report lives 1.5 s: a late refresh risks its lease
                        log.warning("pod state refresh %.1f s late", late)
                    started = time.monotonic()
                    ready = await timed("refresh", report.refresh())
                    # the cadence counts from the start, as the lease does: a slow
                    # refresh is followed at once, not 0.5 s after it ends
                    next_refresh = started + REFRESH_PERIOD
                facts = {k: v for k, v in (report.facts or {}).items() if k != "ovsdb_revision"}
                if facts != last_facts:
                    log.info("pod: %s", json.dumps(facts or None, default=str))
                    last_facts = facts
                try:
                    await timed("session tick", lifecycle.tick())
                except EmosaError as exc:
                    log.warning("session tick: %s", exc)
                # spec 2.5: a kept reconnection whose pod no longer has the configuration (its rows
                # missing or changed under the same start of its OpenSync): the session ends at
                # once, and the fresh session's M2 writes it, rather than the unserved rule's 60 s
                session = lifecycle.session
                kept = (
                    (session.counts.get("reconnection_kept", 0), id(session)) if session else None
                )
                if kept and kept != last_kept and kept[0] and refreshed and ready:
                    last_kept = kept
                    engines = [engine, *(e for _, e in radio_scopes.values())]
                    if any([await e.drifted(pod_id) for e in engines if e.store.operations()]):
                        log.info(
                            "the pod's configuration is gone after its reconnection: "
                            "onboarding again"
                        )
                        report.reconnections["config_lost"] = (
                            report.reconnections.get("config_lost", 0) + 1
                        )
                        if profile.legacy:  # spec 3.10: a re-seed within the window
                            report.new_start("its configuration lost")
                        lifecycle.renew()
                now = time.monotonic()
                if now >= next_discovery:
                    for piece in topology_discovery(agent, mids):
                        endpoint.send(piece)
                    next_discovery = now + DISCOVERY_PERIOD
                unserved = (
                    lifecycle.session is not None
                    and lifecycle.session.state == "provisioning"
                    and report.facts is not None
                    and report.facts.get("ssid") is None
                    and not any(op.state in ACTIVE for op in store.operations())
                )
                # an M1 out without its M2: the fronthaul's, or a further radio's (spec 9)
                awaiting = lifecycle.session is not None and lifecycle.session.awaiting_m2
                provisioning = (
                    lifecycle.session is not None and lifecycle.session.state == "provisioning"
                )
                for reason in renewals.check(
                    now, unserved=unserved, awaiting=awaiting, provisioning=provisioning
                ):
                    log.info(RENEWALS[reason])
                    lifecycle.renew()
                frame = await timed("receive", asyncio.to_thread(endpoint.receive))
                kind = from_controller(frame, controller) if frame is not None else None
                if kind is not None:
                    renewals.contact(time.monotonic())
                if kind == TOPOLOGY_QUERY:
                    renewals.topology_query(time.monotonic())
                if kind == RENEW:
                    log.info("AP-Autoconfiguration Renew from the controller: fresh M1")
                    lifecycle.renew()
                elif frame is not None:
                    try:
                        result = await timed(
                            "frame",
                            lifecycle.receive(frame, ingress=config["interface"], generation=1),
                        )
                        if result and result not in ("incomplete",):
                            log.debug("rx: %s", result)
                    except EmosaError as exc:
                        log.debug("rx rejected: %s", exc)
                if refreshed and ready and store.operations():
                    # The WSC provisioning session executes its operation; the
                    # engine only needs fresh State to confirm application.
                    try:
                        await timed("reconcile", engine.reconcile(pod_id))
                    except (EmosaError, ConnectionError, TimeoutError) as exc:
                        log.warning("reconcile: %s", exc)
                        report.source.invalidate()
                for band, (_, radio_engine) in radio_scopes.items():
                    if refreshed and ready and radio_engine.store.operations():
                        try:
                            await timed(f"reconcile {band}", radio_engine.reconcile(pod_id))
                        except (EmosaError, ConnectionError, TimeoutError) as exc:
                            log.warning("reconcile %s: %s", band, exc)
                if refreshed and switch:
                    # Also while the pod is away: an unconfirmed switch times out.
                    try:
                        await timed("uplink", switch.tick())
                    except (EmosaError, ConnectionError, TimeoutError) as exc:
                        log.warning("uplink: %s", exc)
                if refreshed and wired:
                    try:
                        await timed("wired uplink", wired.tick())
                    except (EmosaError, ConnectionError, TimeoutError) as exc:
                        log.warning("wired uplink: %s", exc)
                if refreshed and gtp_station:
                    try:
                        await timed("gtp station", gtp_station.tick())
                    except (EmosaError, ConnectionError, TimeoutError) as exc:
                        log.warning("gtp station: %s", exc)
                if refreshed and radio_plan:
                    try:
                        await timed("radio plan", radio_plan.tick())
                    except (EmosaError, ConnectionError, TimeoutError) as exc:
                        log.warning("radio plan: %s", exc)
                if refreshed and tunnels:
                    try:
                        await timed("gre parent", tunnels.tick())
                    except (EmosaError, ConnectionError, TimeoutError) as exc:
                        log.warning("gre parent: %s", exc)
                if refreshed and telemetry:
                    try:
                        await timed("telemetry", telemetry.tick())
                    except (EmosaError, ConnectionError, TimeoutError) as exc:
                        log.warning("telemetry: %s", exc)
                if refreshed and steering:
                    try:
                        await timed("steering", steering.tick())
                    except (EmosaError, ConnectionError, TimeoutError) as exc:
                        log.warning("steering: %s", exc)
                if refreshed and watch:
                    try:
                        await timed("probe_watch", watch.tick())
                    except (EmosaError, ConnectionError, TimeoutError) as exc:
                        log.warning("probe watch: %s", exc)
                if not refreshed and frame is None:
                    continue  # nothing new to report: status only on the refresh cadence
                value = status()
                summary = json.dumps(
                    [
                        (value["session"] or {}).get("state"),
                        value["report_source_available"],
                        [(o["state"], o["ssid"]) for o in value["operations"]],
                    ]
                )
                if summary != last_status:
                    log.info("state: %s", summary)
                    last_status = summary
                    last_write = 0
                if time.monotonic() - last_write >= 1:
                    write(status_file, value)
                    last_write = time.monotonic()
    finally:
        if lifecycle:
            write(status_file, status())
            lifecycle.close()
        store.close()
        if subscriber is not None:
            subscriber.stop()
        for _, radio_engine in radio_scopes.values():
            radio_engine.store.close()
        for extra in (channels, reporting, uplink_store, telemetry_store, steering_store):
            if extra is not None:
                extra.close()
        await session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "config", type=Path, help="agent JSON (pod_id, serial, ovsdb, interface, ...)"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = json.loads(args.config.read_text())
    validate("agent-config", config)

    async def run():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await serve(config, stop)

    asyncio.run(run())


if __name__ == "__main__":
    main()
