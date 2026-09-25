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

from emosa.agent.steering import ClientSteering
from emosa.agent.telemetry import MqttSubscriber, TelemetrySetup
from emosa.agent.uplink import UplinkSwitch, m2_backhaul
from emosa.config import validate
from emosa.errors import EmosaError, Reason
from emosa.model import ACTIVE
from emosa.opensync.easymesh_view import (
    backhaul,
    device_view,
    inventory,
    radio_capabilities,
    topology,
)
from emosa.opensync.pod_profile import PodBackend, wpa2_psk
from emosa.opensync.profiles import DEFAULT as DEFAULT_PROFILE
from emosa.opensync.profiles import load as load_profile
from emosa.opensync.schema import TABLES
from emosa.opensync.session import OvsSession
from emosa.opensync.stats import PodStats
from emosa.opensync.steering import MONITOR as STEERING_MONITOR
from emosa.opensync.steering import SteeringBackend
from emosa.opensync.telemetry import MONITOR as TELEMETRY_MONITOR
from emosa.opensync.telemetry import TelemetryBackend, TelemetryIntent
from emosa.opensync.uplink import BSSID, MULTI_AP, UplinkBackend, uplink_state
from emosa.opensync.uplink import MONITOR as UPLINK_MONITOR
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
from emosa.wire.onboarding import OnboardingRecovery, OnboardingSession
from emosa.wire.operation_bridge import ComponentTarget, WscComponentBridge
from emosa.wire.reporting_policy import ReportingPolicyStore
from emosa.wsc_messages import M1Device

log = logging.getLogger("emosa.agent")
RENEW = 0x000A  # AP-Autoconfiguration Renew
DISCOVERY_PERIOD = 60  # IEEE 1905.1 Topology Discovery interval
CONTROLLER_TIMEOUT = 130  # no CMDU from the controller for about two discovery periods
# Provisioned, yet the pod does not serve the controller's BSS and nothing is being
# applied (its configuration was lost, e.g. a write lost to an uplink move): ask
# for the configuration again with a fresh M1 after this long.
UNSERVED_RENEW = 60
# M1 sent and no M2: a controller that restarted meanwhile has forgotten the M1, and
# its other queries keep CONTROLLER_TIMEOUT from firing. Search again after this long.
M2_TIMEOUT = 30
# The pod's State is re-read on this cadence, not on every received frame: the
# published report lives 1.5 s, which this refreshes three times over.
REFRESH_PERIOD = 0.5
MONITOR = {
    **TABLES,
    "AWLAN_Node": [*TABLES["AWLAN_Node"], "id"],
    "Wifi_Radio_State": [*TABLES["Wifi_Radio_State"], "tx_power"],
}
for _scope in (UPLINK_MONITOR, TELEMETRY_MONITOR, STEERING_MONITOR):  # the other scopes
    for _table, _columns in _scope.items():
        MONITOR[_table] = [*MONITOR.get(_table, []), *_columns]


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


class PodReportSource:
    """What the agent reports about its pod: the bound BSS (and managed slot BSSes).

    The translation itself is :mod:`emosa.opensync.easymesh_view`; this class
    chooses what the agent represents and keeps the report source current.
    """

    def __init__(self, backend, binding, pod_id, station=None):
        self.backend, self.binding = backend, binding
        self.station = station  # the pod's backhaul station, reported when it is option 1
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
        extras = [radio.bss(name) for name, _, _ in self.backend.slots]
        return primary, tuple(b for b in (primary, *extras) if b is not None)

    async def refresh(self):
        started = time.monotonic()
        try:
            raw = await self.backend.session.snapshot()
            _, _, _, state, rows = self.backend._binding(raw)
            ident = self.backend.identity
            if not raw["ready"] or not ident:
                raise EmosaError(Reason.NOT_READY, "bound radio State unavailable")
            device = device_view(rows)
            radio = device.radio(mac(ident["radio_mac"]))
            if radio is None:
                raise EmosaError(Reason.NOT_READY, "bound radio absent from the view")
            primary, bsses = self._represented(radio, bool(state))
            # A cold pod has the radio but no BSS yet: advertise the radio on the
            # channel the profile would create the BSS on, and no BSS.
            channel = radio.channel if primary else self.backend.channel
            max_eirp = radio.tx_power if radio.tx_power and 0 < radio.tx_power <= 127 else 20
            if self.capabilities is not None:
                max_eirp = self.capabilities.radios[0].basic.operating_classes[0].max_eirp_dbm
            capabilities = radio_capabilities(
                radio,
                channel=channel,
                max_bss=self.backend.max_bss,
                max_eirp=max_eirp,
            )
            if self.capabilities is None:
                self.capabilities = capabilities
                self.inventory = inventory(device, radio, self.backend.profile.chipset.encode())
            elif capabilities != self.capabilities:
                # A moved radio or replaced PHY is a different device to the controller.
                raise EmosaError(Reason.NOT_READY, "pod radio identity or channel changed")
            uplink = None
            if self.station and uplink_state(rows, self.station)["kind"] == MULTI_AP:
                uplink = backhaul(device, self.station)
            now = time.monotonic()
            active = {m for b in bsses for m in b.stations}
            self.first_seen = {m: self.first_seen.get(m, now) for m in active}
            report = topology(
                agent_al=self.agent,
                controller_al=self.binding.controller_al,
                radio=radio,
                channel=channel,
                bsses=bsses,
                ages={m: now - t for m, t in self.first_seen.items()},
                uplink=uplink,
            )
            facts = (
                raw["generation"],
                raw["revision"],
                tuple((b.bssid, b.ssid, b.stations) for b in bsses),
                radio.tx_power,
                uplink,
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
                "ssid": primary.ssid if primary else None,
                "bsses": [
                    {"role": b.role, "bssid": b.bssid.hex(":"), "ssid": b.ssid} for b in bsses
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
                "ovsdb_generation": raw["generation"],
                "ovsdb_revision": raw["revision"],
            }
            # Station ages advance every refresh, but facts may change only with
            # a new source revision: ages are taken when membership changes.
            if self._clients is not None and self._clients[0] == self.revision:
                report = replace(report, clients=self._clients[1])
            self._clients = (self.revision, report.clients)
            # Measured operating parameters (channel procedures, Operating Channel
            # Report) only while the BSS operates on the one supported channel.
            operating = (
                (OperatingRadio(radio.ruid, 81, channel, radio.tx_power),)
                if primary
                and channel == 6
                and radio.tx_power is not None
                and radio.tx_power <= max_eirp
                else ()
            )
            self.source.publish(
                (raw["generation"], self.revision),
                self.capabilities,
                report,
                observed_at=started,
                lifetime=1.5,  # (t + 2) - t can exceed 2 in floating point
                operating_radios=operating,
            )
            return True
        except (EmosaError, ConnectionError, TimeoutError) as exc:
            # Once per cause: a pod that stays away must not flood the log, but a
            # new reason (a replaced radio after a reconnect) must show.
            if self.facts is not None or str(exc) != self._unavailable:
                log.info("pod source unavailable: %s", exc)
            self._unavailable = str(exc)
            self.facts = None
            self.source.invalidate()
            return False

    _clients = None
    _unavailable = None


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
):
    node = report.facts
    uuid = hashlib.sha256(("emosa-agent:" + node["serial"]).encode()).digest()[:16]
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
        rf_band=1,
        association_state=0,
        device_password_id=4,
        configuration_error=0,
        os_version=1,
    )
    radio = capabilities.radios[0]
    exchange = WscExchange(
        report.binding,
        device,
        radio.basic,
        capabilities.profile2,
        radio.advanced,
        mids=mids,
        timeout=30,
        message_set=message_set,
        multi_bss=bool(backend.slots),
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
    pod_id = config["pod_id"]
    agent, controller = mac(config["al_mac"]), mac(config["controller_al"])
    session = OvsSession(config["ovsdb"], monitor_columns=MONITOR, timeout=2)
    vault, store = SecretStore(state_dir / "secrets"), Store(state_dir / "journal")
    backend = PodBackend(
        pod_id,
        session,
        vault,
        serial=config["serial"],
        profile=load_profile(config.get("profile", DEFAULT_PROFILE)),
        multi_bss=config.get("multi_bss", False) is True,
    )
    engine = Engine(store, vault, {pod_id: backend})
    engine.recover()
    uplink_config = config.get("uplink", {"mode": "off"})
    station = uplink_config.get("station") or backend.profile.uplink_station
    switch = uplink_store = None
    if uplink_config["mode"] == MULTI_AP:
        if station is None:
            raise EmosaError(Reason.INVALID_INPUT, "uplink: no station (profile or configuration)")
        bssid = uplink_bssid(uplink_config)
        if uplink_config.get("credentials", "m2") == "config":
            if not uplink_config.get("ssid") or not uplink_config.get("secret_ref"):
                raise EmosaError(
                    Reason.INVALID_INPUT, "uplink: config credentials need ssid and secret_ref"
                )
            fixed = (uplink_config["ssid"], uplink_config["secret_ref"])
            credentials = lambda: fixed  # noqa: E731
        else:
            credentials = lambda: m2_backhaul(store)  # noqa: E731
        uplink_store = Store(state_dir / "uplink")
        switch = UplinkSwitch(
            pod_id,
            UplinkBackend(pod_id, session, vault, serial=config["serial"], station=station),
            uplink_store,
            vault,
            credentials,
            bssid=bssid,
            run_id=config.get("run_id", pod_id),
            # the pod serves the controller's fronthaul, and no fronthaul write is in flight
            settled=lambda: (
                (report.facts or {}).get("ssid") is not None
                and not any(op.state in ACTIVE for op in store.operations())
            ),
        )
    telemetry = stats = subscriber = telemetry_store = None
    telemetry_config = config.get("telemetry", {"mode": "off"})
    wanted = telemetry_intent(pod_id, config["serial"], telemetry_config)
    if wanted is not None:
        telemetry_store = Store(state_dir / "telemetry")
        telemetry = TelemetrySetup(
            pod_id,
            TelemetryBackend(
                pod_id, session, serial=config["serial"], radio_type=wanted.radio_type
            ),
            telemetry_store,
            vault,
            wanted,
            run_id=config.get("run_id", pod_id),
        )
        stats = PodStats(wanted.topic, interval=wanted.reporting_interval)
        host, _, port = telemetry_config.get("subscribe", "127.0.0.1:1883").rpartition(":")
        subscriber = MqttSubscriber(
            host,
            int(port),
            wanted.topic,
            lambda topic, payload, retained: stats.receive(topic, payload, retained=retained),
            client_id=f"emosa-agent-{pod_id}",
        )
        subscriber.start(asyncio.get_running_loop())
    # Client steering mandates from the controller, carried out by owm (on unless "off").
    steering = steering_store = None
    if config.get("steering", {}).get("mode", "owm") != "off":
        steering_store = Store(state_dir / "steering")
        steering = ClientSteering(
            pod_id,
            SteeringBackend(pod_id, session, serial=config["serial"]),
            steering_store,
            vault,
            run_id=config.get("run_id", pod_id),
        )
    binding = PeerBinding(config["interface"], 1, agent, controller, (controller,))
    report = PodReportSource(backend, binding, pod_id, station)
    mids = MidSequence(secrets.randbelow(65536))
    run_id = config.get("run_id", pod_id)
    # EasyMesh message set toward this controller: easymesh-6.1 unless the
    # controller needs the R1 form (see emosa.wire.autoconfiguration).
    message_set = check_message_set(config.get("message_set", EASYMESH_61))
    lifecycle, last_status, last_facts, last_write = None, None, None, 0
    next_discovery, last_contact = 0, time.monotonic()
    unserved_since = awaiting_since = None
    next_refresh, ready = 0, False
    channels = reporting = None

    def status():
        snapshot = report.source.current()
        ops = [
            {
                "operation_id": op.operation_id,
                "state": op.state,
                "reason": op.reason,
                "ssid": op.intent.get("ssid"),
                "attempts": len(op.attempts),
                "application_evidence": op.application_evidence,
            }
            for op in store.operations()
        ]
        return {
            "pod_id": pod_id,
            "profile": backend.profile.id,
            "agent_al": agent.hex(":"),
            "controller_al": controller.hex(":"),
            "pod": report.facts,
            "report_source_available": snapshot is not None,
            "session": lifecycle.status() if lifecycle else None,
            "operations": ops,
            "uplink": switch.status() if switch else {"station": station, "mode": "off"},
            "telemetry": (
                {"mode": "mqtt", **telemetry.status(), "subscribed": subscriber.connected}
                | stats.status()
                if telemetry
                else {"mode": "off"}
            ),
            "steering": {"mode": "owm", **steering.status()} if steering else {"mode": "off"},
            "writes": backend.write_count,
            "worker_pid": os.getpid(),
            "updated": time.time(),
        }

    try:
        # A frame returns at once; the timeout only paces idle wakeups (timers are >= 1 s).
        with EthernetEndpoint(config["interface"], agent, timeout=0.2) as endpoint:

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
                    ready = await timed("refresh", report.refresh())
                    next_refresh = time.monotonic() + REFRESH_PERIOD
                facts = {k: v for k, v in (report.facts or {}).items() if k != "ovsdb_revision"}
                if facts != last_facts:
                    log.info("pod: %s", json.dumps(facts or None, default=str))
                    last_facts = facts
                try:
                    await timed("session tick", lifecycle.tick())
                except EmosaError as exc:
                    log.warning("session tick: %s", exc)
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
                unserved_since = (unserved_since or now) if unserved else None
                if unserved_since is not None and now - unserved_since > UNSERVED_RENEW:
                    log.info("provisioned, but the pod serves no BSS: fresh M1")
                    lifecycle.renew()
                    unserved_since = None
                awaiting = (
                    lifecycle.session is not None and lifecycle.session.state == "awaiting_m2"
                )
                awaiting_since = (awaiting_since or now) if awaiting else None
                if awaiting_since is not None and now - awaiting_since > M2_TIMEOUT:
                    log.info("no M2 for %ds after M1: fresh attempt", M2_TIMEOUT)
                    lifecycle.renew()
                    awaiting_since = None
                if now - last_contact > CONTROLLER_TIMEOUT:
                    # Like a native agent's controller connectivity check: a silent
                    # controller is lost; onboard again when it answers a Search.
                    log.info(
                        "no message from the controller for %ds: fresh attempt", CONTROLLER_TIMEOUT
                    )
                    lifecycle.renew()
                    last_contact = now
                frame = await timed("receive", asyncio.to_thread(endpoint.receive))
                kind = from_controller(frame, controller) if frame is not None else None
                if kind is not None:
                    last_contact = time.monotonic()
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
                if refreshed and switch:
                    # Also while the pod is away: an unconfirmed switch times out.
                    try:
                        await timed("uplink", switch.tick())
                    except (EmosaError, ConnectionError, TimeoutError) as exc:
                        log.warning("uplink: %s", exc)
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
                    write(state_dir / "status.json", value)
                    last_write = time.monotonic()
    finally:
        if lifecycle:
            write(state_dir / "status.json", status())
            lifecycle.close()
        store.close()
        if subscriber is not None:
            subscriber.stop()
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
