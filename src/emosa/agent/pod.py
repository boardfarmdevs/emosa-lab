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
on the controller's bridge. The pod's physical path to the gateway (Wi-Fi
backhaul plus GRE, owned by OpenSync and the lab NOC) is not represented.
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

from emosa.errors import EmosaError, Reason
from emosa.opensync.easymesh_view import device_view, inventory, radio_capabilities, topology
from emosa.opensync.pod_profile import PROFILE, PodBackend, wpa2_psk
from emosa.opensync.schema import TABLES
from emosa.opensync.session import OvsSession
from emosa.reconcile import Engine
from emosa.secrets import SecretStore
from emosa.simulation.wire_reports import fixtures
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
MONITOR = {
    **TABLES,
    "AWLAN_Node": [*TABLES["AWLAN_Node"], "id"],
    "Wifi_Radio_State": [*TABLES["Wifi_Radio_State"], "tx_power"],
}


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

    def __init__(self, backend, binding, pod_id):
        self.backend, self.binding = backend, binding
        self.agent = binding.local_al
        self.revision, self.last = 0, None
        self.first_seen = {}  # station MAC -> monotonic time EMOSA first saw it active
        self.facts = None  # the pod's identity/radio/BSS facts behind the published reports
        _, self.caps_template, self.topology_template = fixtures()
        self.source = ReportSource(
            binding,
            pod_id,
            hashlib.sha256(
                f"{PROFILE}:sole-2.4G-fronthaul:PSK-CCMP:nonDPP:v1".encode()
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
                self.caps_template,
                radio,
                channel=channel,
                max_bss=self.backend.max_bss,
                max_eirp=max_eirp,
            )
            if self.capabilities is None:
                self.capabilities = capabilities
                self.inventory = inventory(device, radio)
            elif capabilities != self.capabilities:
                # A moved radio or replaced PHY is a different device to the controller.
                raise EmosaError(Reason.NOT_READY, "pod radio identity or channel changed")
            now = time.monotonic()
            active = {m for b in bsses for m in b.stations}
            self.first_seen = {m: self.first_seen.get(m, now) for m in active}
            report = topology(
                self.topology_template,
                agent_al=self.agent,
                controller_al=self.binding.controller_al,
                radio=radio,
                channel=channel,
                bsses=bsses,
                ages={m: now - t for m, t in self.first_seen.items()},
            )
            facts = (
                raw["generation"],
                raw["revision"],
                tuple((b.bssid, b.ssid, b.stations) for b in bsses),
                radio.tx_power,
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
            if self.facts is not None:
                log.info("pod source unavailable: %s", exc)
            self.facts = None
            self.source.invalidate()
            return False

    _clients = None


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
        if_name=config.get("vif", "home-ap-24"),
        multi_bss=config.get("multi_bss", False) is True,
    )
    engine = Engine(store, vault, {pod_id: backend})
    engine.recover()
    binding = PeerBinding(config["interface"], 1, agent, controller, (controller,))
    report = PodReportSource(backend, binding, pod_id)
    mids = MidSequence(secrets.randbelow(65536))
    run_id = config.get("run_id", pod_id)
    # EasyMesh message set toward this controller: easymesh-6.1 unless the
    # controller needs the R1 form (see emosa.wire.autoconfiguration).
    message_set = check_message_set(config.get("message_set", EASYMESH_61))
    lifecycle, last_status, last_facts, last_write = None, None, None, 0
    next_discovery, last_contact = 0, time.monotonic()
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
            "profile": PROFILE,
            "agent_al": agent.hex(":"),
            "controller_al": controller.hex(":"),
            "pod": report.facts,
            "report_source_available": snapshot is not None,
            "session": lifecycle.status() if lifecycle else None,
            "operations": ops,
            "writes": backend.write_count,
            "worker_pid": os.getpid(),
            "updated": time.time(),
        }

    try:
        with EthernetEndpoint(config["interface"], agent, timeout=0.05) as endpoint:

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
                )

            channels = ChannelPolicyStore(state_dir / "channel-policy.sqlite")
            reporting = ReportingPolicyStore(
                state_dir / "reporting-policy.sqlite",
                boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            )
            lifecycle = OnboardingRecovery(report.source, factory)
            while not stop.is_set():
                ready = await report.refresh()
                facts = {k: v for k, v in (report.facts or {}).items() if k != "ovsdb_revision"}
                if facts != last_facts:
                    log.info("pod: %s", json.dumps(facts or None, default=str))
                    last_facts = facts
                try:
                    await lifecycle.tick()
                except EmosaError as exc:
                    log.warning("session tick: %s", exc)
                now = time.monotonic()
                if now >= next_discovery:
                    for piece in topology_discovery(agent, mids):
                        endpoint.send(piece)
                    next_discovery = now + DISCOVERY_PERIOD
                if now - last_contact > CONTROLLER_TIMEOUT:
                    # Like a native agent's controller connectivity check: a silent
                    # controller is lost; onboard again when it answers a Search.
                    log.info(
                        "no message from the controller for %ds: fresh attempt", CONTROLLER_TIMEOUT
                    )
                    lifecycle.renew()
                    last_contact = now
                frame = await asyncio.to_thread(endpoint.receive)
                kind = from_controller(frame, controller) if frame is not None else None
                if kind is not None:
                    last_contact = time.monotonic()
                if kind == RENEW:
                    log.info("AP-Autoconfiguration Renew from the controller: fresh M1")
                    lifecycle.renew()
                elif frame is not None:
                    try:
                        result = await lifecycle.receive(
                            frame, ingress=config["interface"], generation=1
                        )
                        if result and result not in ("incomplete",):
                            log.debug("rx: %s", result)
                    except EmosaError as exc:
                        log.debug("rx rejected: %s", exc)
                if ready and store.operations():
                    # The WSC provisioning session executes its operation; the
                    # engine only needs fresh State to confirm application.
                    try:
                        await engine.reconcile(pod_id)
                    except (EmosaError, ConnectionError, TimeoutError) as exc:
                        log.warning("reconcile: %s", exc)
                        report.source.invalidate()
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
        for extra in (channels, reporting):
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

    async def run():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await serve(config, stop)

    asyncio.run(run())


if __name__ == "__main__":
    main()
