"""Packet worker for the owned native-controller/hwsim onboarding experiment.

It accepts only a marked local run directory, no desired SSID/key or pod address.
The pod simulator initiates OVSDB; the independent radio manager publishes State.
"""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import time
from dataclasses import replace
from pathlib import Path

from emosa.easymesh_payloads import (
    APHTCapabilities,
    APOperationalBss,
    APRadioAdvancedCapabilities,
    APRadioBasicCapabilities,
    AssociatedClient,
    AssociatedClients,
    BasicOperatingClass,
    BssClients,
    BssConfigurationReport,
    ConfiguredBss,
    ConfiguredRadio,
    DeviceInventory,
    InventoryRadio,
    OperationalBss,
    OperationalRadio,
    Profile2APCapability,
)
from emosa.errors import EmosaError, Reason
from emosa.opensync.session import OvsSession
from emosa.reconcile import Engine
from emosa.secrets import SecretStore
from emosa.simulation.backhaul_accounting import BackhaulAccountingSource
from emosa.simulation.egress_accounting import EgressAccountingSource
from emosa.simulation.forwarding import ForwardingSource
from emosa.simulation.neighbor_binding import NeighborSource
from emosa.simulation.peer_metrics import PeerMetricPublisher
from emosa.simulation.radio import MONITOR
from emosa.simulation.shaped_backhaul import ShapedBackhaulSource
from emosa.simulation.station_telemetry import NODE_ID, TOPIC, LabMqtt
from emosa.simulation.virtual_capacity import VirtualCapacitySource
from emosa.simulation.wire_reports import fixtures
from emosa.simulation.wsc_provisioning import (
    SERIAL,
    TARGET,
    BoundBackend,
    make_bridge,
    public_operation,
)
from emosa.simulation.wsc_wire import RADIO_BSSID, RADIO_ROOT, write
from emosa.store import Store
from emosa.telemetry.stations import StationSource
from emosa.wire.autoconfiguration import PeerBinding
from emosa.wire.channel import ChannelPolicyStore, OperatingRadio
from emosa.wire.cmdu import MidSequence
from emosa.wire.coordinator import ReportSource
from emosa.wire.ethernet import EthernetEndpoint
from emosa.wire.onboarding import OnboardingRecovery, OnboardingSession
from emosa.wire.reporting_policy import ReportingPolicyStore
from emosa.wire.topology_values import (
    BridgingCapability,
    DeviceInformation,
    LocalInterface,
    Neighbor,
    Neighbors1905,
)

AGENT = bytes.fromhex("020000003001")
CONTROLLER = bytes.fromhex("020000e00001")
RADIO = bytes.fromhex(RADIO_BSSID.replace(":", ""))
OWNER = {"owner": "emosa-native-onboarding-v1", "backend": "ovsdb-sim"}


class RadioReportSource:
    """Explicit fixture capabilities plus a fresh complete observed OVSDB graph."""

    def __init__(self, backend, binding, stations=None, *, forwarding_run=None, peer_metrics=False):
        self.backend = backend
        self.stations = stations
        self.last_revision = None
        self.revision = 0
        self.forwarding = ForwardingSource()
        self.egress = EgressAccountingSource(forwarding_run) if forwarding_run else None
        self.backhaul = BackhaulAccountingSource(forwarding_run) if forwarding_run else None
        self.virtual_capacity = VirtualCapacitySource(forwarding_run) if forwarding_run else None
        self.shaped_backhaul = ShapedBackhaulSource(forwarding_run) if forwarding_run else None
        self.neighbor = (
            NeighborSource(CONTROLLER, AGENT, forwarding_run) if forwarding_run else None
        )
        _, caps, self.template = fixtures()
        radio = replace(
            caps.radios[0],
            basic=APRadioBasicCapabilities(
                RADIO, 1, (BasicOperatingClass(81, 20, tuple(n for n in range(1, 14) if n != 6)),)
            ),
            ht=APHTCapabilities(RADIO, 0),
            advanced=APRadioAdvancedCapabilities(RADIO, 0),
        )
        self.capabilities = replace(
            # Profile-1 traffic counters are bytes (EasyMesh 6.1 Table 58).
            # Keep the accompanying Profile-2 counter declaration consistent.
            caps,
            radios=(radio,),
            profile2=Profile2APCapability(0, 0, 0, 0),
        )
        self.source = ReportSource(
            binding,
            "pod-1",
            hashlib.sha256(
                b"owned-hwsim:sole-HT20-channel6:PSK-CCMP:nonDPP:v1"
                + (b":observed-forwarding-identity:v1" if forwarding_run else b"")
            ).hexdigest(),
        )
        self.inventory = DeviceInventory(
            SERIAL.encode(),
            b"0.1.0",
            b"owned-hwsim-simulation",
            (InventoryRadio(RADIO, b"mac80211_hwsim"),),
        )
        self.peer_metrics = (
            PeerMetricPublisher(self.source, forwarding_run) if peer_metrics else None
        )

    async def refresh(self):
        started = time.monotonic()
        try:
            raw = await self.backend.session.snapshot()
            self.forwarding.refresh(raw)
            for accounting in (
                self.egress,
                self.backhaul,
                self.virtual_capacity,
                self.shaped_backhaul,
            ):
                if accounting is None:
                    continue
                sample = self.forwarding.sample
                if sample:
                    port = sample.interfaces["eth1"]
                    accounting.refresh(
                        sample.egress_observation,
                        generation=sample.generation,
                        ifindex=port["ifindex"],
                        address=port["mac"],
                        boot_id=sample.epoch[1],
                        netns_inode=sample.epoch[2],
                    )
                else:
                    accounting.invalidate()
            if self.neighbor:
                self.neighbor.refresh(self.forwarding.sample)
            self.backend._binding(raw)
            rows = {
                t: {k: raw["schema"].row(t, v) for k, v in values.items()}
                for t, values in raw["tables"].items()
            }
            vif = next(iter(rows["Wifi_VIF_State"].values()))
            radio = next(iter(rows["Wifi_Radio_State"].values()))
            if not (
                vif.get("enabled")
                and radio.get("enabled")
                and vif.get("mode") == "ap"
                and vif.get("wpa") is True
                and vif.get("wpa_key_mgmt") == ["wpa2-psk"]
                and vif.get("rsn_pairwise_ccmp") is True
                and vif.get("wpa_pairwise_tkip") is False
                and vif.get("wpa_pairwise_ccmp") is False
            ):
                raise EmosaError(Reason.NOT_READY, "radio State is unavailable")
            associated = rows.get("Wifi_Associated_Clients", {})
            membership = {
                bytes.fromhex(row["mac"].replace(":", ""))
                for row in associated.values()
                if row.get("state") == "active"
            }
            sample = self.stations.current() if self.stations else None
            # Old experiments without telemetry still support a proved empty
            # database inventory. A nonempty list always requires measured age.
            complete = (
                set(associated) == set(vif.get("associated_clients") or [])
                and len(membership) == len(associated)
                and (
                    (self.stations is None and not membership)
                    or (
                        sample is not None
                        and membership == {mac for mac, _ in sample.clients}
                        and (not membership or sample.ssid == vif["ssid"])
                    )
                )
            )
            clients = (
                tuple(AssociatedClient(mac, seconds) for mac, seconds in sample.clients)
                if complete and sample
                else ()
            )
            ssid = vif["ssid"].encode()
            interfaces = (
                LocalInterface(AGENT, 1, b""),
                LocalInterface(RADIO, 0x103, RADIO + b"\0\0\x06\0"),
            )
            bridges = BridgingCapability(((AGENT, RADIO),))
            neighbors = (Neighbors1905(AGENT, (Neighbor(CONTROLLER, False),)),)
            neighbor_binding = self.neighbor.current() if self.neighbor else None
            if self.neighbor:
                complete = complete and neighbor_binding is not None
                neighbors = ()
                forwarding_sample = self.forwarding.sample
                if forwarding_sample is not None:
                    # This retains the owned simulator's Ethernet media fixture.
                    # Only the MAC/bridge identities are newly measured here;
                    # no physical 1000BASE-T PHY or throughput is asserted.
                    interfaces = tuple(
                        LocalInterface(
                            bytes.fromhex(
                                forwarding_sample.interfaces[name]["mac"].replace(":", "")
                            ),
                            1,
                            b"",
                        )
                        for name in ("eth1", "eth2")
                    ) + (interfaces[1],)
                    if forwarding_sample.interfaces["wlan0"]["mac"] != RADIO_BSSID:
                        raise EmosaError(
                            Reason.NOT_READY, "observed forwarding AP identity changed"
                        )
                    bridges = BridgingCapability((tuple(i.mac for i in interfaces),))
                if neighbor_binding:
                    neighbors = (
                        Neighbors1905(
                            bytes.fromhex(neighbor_binding.local_interface.replace(":", "")),
                            (Neighbor(CONTROLLER, neighbor_binding.bridges_present),),
                        ),
                    )
            topology = replace(
                self.template,
                device=DeviceInformation(AGENT, interfaces),
                bridges=bridges,
                neighbors1905=neighbors,
                operational=APOperationalBss(
                    (OperationalRadio(RADIO, (OperationalBss(RADIO, ssid),)),)
                ),
                configuration=BssConfigurationReport(
                    (ConfiguredRadio(RADIO, (ConfiguredBss(RADIO, 0x40, ssid),)),)
                ),
                clients=AssociatedClients((BssClients(RADIO, clients),)),
                inventory_complete=complete,
            )
            revision = (
                raw["generation"],
                raw["revision"],
                sample.timestamp_ms if sample else None,
                complete,
                neighbor_binding,
            )
            if revision != self.last_revision:
                self.revision += 1
                self.last_revision = revision
            self.source.publish(
                (raw["generation"], self.revision),
                self.capabilities,
                topology,
                observed_at=started,
                lifetime=2,
                telemetry_valid_until=sample.valid_until if sample else None,
                topology_valid_until=neighbor_binding.valid_until_ns / 1e9
                if neighbor_binding
                else None,
                operating_radios=(OperatingRadio(RADIO, 81, radio["channel"], radio["tx_power"]),)
                if sample is not None and type(radio.get("tx_power")) is int
                else (),
            )
            if self.peer_metrics:
                self.peer_metrics.refresh(
                    self.forwarding.sample, neighbor_binding, self.shaped_backhaul.status()
                )
            return True
        except (EmosaError, ConnectionError, TimeoutError):
            self.forwarding.invalidate()
            if self.egress:
                self.egress.invalidate()
            if self.backhaul:
                self.backhaul.invalidate()
            if self.virtual_capacity:
                self.virtual_capacity.invalidate()
            if self.shaped_backhaul:
                self.shaped_backhaul.invalidate()
            if self.neighbor:
                self.neighbor.invalidate()
            self.source.invalidate()
            if self.peer_metrics:
                self.peer_metrics.invalidate()
            return False


async def worker(directory, *, duration=110, telemetry=False):
    directory = directory.resolve(strict=True)
    if (
        directory.parent != RADIO_ROOT
        or json.loads((directory / "native-owner.json").read_text()) != OWNER
    ):
        raise ValueError("expected an owned native onboarding run")
    session = OvsSession(
        "punix:" + str(directory / "database/native-pod.sock"), monitor_columns=MONITOR, timeout=1
    )
    vault, store = SecretStore(directory / "native-secrets"), Store(directory / "native-journal")
    backend = BoundBackend(
        session,
        vault,
        radio_mac=RADIO_BSSID,
        bssid=RADIO_BSSID,
        if_name="wlan0",
        radio_name="phy1",
        state_provenance="independent-hostapd-nl80211-manager:Wifi_VIF_State",
    )
    binding = PeerBinding("probe0", 1, AGENT, CONTROLLER, (CONTROLLER,))
    target = replace(TARGET, ruid=RADIO, bssid=RADIO)
    engine = Engine(store, vault, {"pod-1": backend})
    engine.recover()
    stations = StationSource(NODE_ID, TOPIC) if telemetry else None
    mqtt = LabMqtt(directory, stations) if telemetry else None
    facts = RadioReportSource(
        backend,
        binding,
        stations,
        forwarding_run=directory.name,
        peer_metrics=(directory / "peer-metrics-requested").exists(),
    )
    lifecycle = None
    channel_store = ChannelPolicyStore(directory / "channel-policy.sqlite") if telemetry else None
    reporting_policy_store = (
        ReportingPolicyStore(
            directory / "reporting-policy.sqlite",
            boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        )
        if telemetry
        else None
    )
    mids = MidSequence(secrets.randbelow(65536))

    def status():
        value = lifecycle.status()
        value["worker"] = {"pid": os.getpid(), "process_id": store.process_id}
        snapshot = facts.source.current()
        value["report_source"] = {
            "available": snapshot is not None,
            "context_token": snapshot.context_token if snapshot else None,
            "inventory_complete": snapshot.topology.inventory_complete if snapshot else False,
            "operating_radio_count": len(snapshot.operating_radios) if snapshot else 0,
        }
        value["forwarding_observation"] = facts.forwarding.status()
        value["observed_neighbor"] = facts.neighbor.status()
        value["egress_accounting"] = facts.egress.status()
        value["backhaul_accounting"] = facts.backhaul.status()
        value["virtual_capacity"] = facts.virtual_capacity.status()
        value["shaped_backhaul"] = facts.shaped_backhaul.status()
        value["peer_link_metrics"] = facts.peer_metrics.status() if facts.peer_metrics else None
        if stations:
            sample = stations.current()
            value["telemetry"] = {
                "accepted": stations.accepted,
                "rejected": stations.rejected,
                "generation": stations.generation,
                "timestamp_ms": sample.timestamp_ms if sample else None,
                "station_count": len(sample.clients) if sample else None,
            }
        return value

    try:
        previous_forwarding = None
        with EthernetEndpoint("probe0", AGENT, timeout=0.03) as endpoint:

            def factory(snapshot, mids):
                return make_bridge(
                    engine,
                    backend,
                    directory.name,
                    binding=binding,
                    target=target,
                    deadline=120,
                    mids=mids,
                    capabilities=snapshot.capabilities,
                )

            lifecycle = OnboardingRecovery(
                facts.source,
                lambda: OnboardingSession(
                    facts.source,
                    endpoint.send,
                    factory,
                    facts.inventory,
                    channel_store=channel_store,
                    reporting_policy_store=reporting_policy_store,
                    link_metric_source=facts.peer_metrics.source if facts.peer_metrics else None,
                    mids=mids,
                    reset_channel_policy=lifecycle.starts == 0,
                ),
            )
            end = time.monotonic() + duration
            while time.monotonic() < end and not (directory / "stop-worker").exists():
                if mqtt:
                    mqtt.poll()
                ready = await facts.refresh()
                forwarding = facts.forwarding.status()
                forwarding["observed_neighbor"] = facts.neighbor.status()
                forwarding["egress_accounting"] = facts.egress.status()
                forwarding["backhaul_accounting"] = facts.backhaul.status()
                forwarding["virtual_capacity"] = facts.virtual_capacity.status()
                forwarding["shaped_backhaul"] = facts.shaped_backhaul.status()
                forwarding["peer_link_metrics"] = (
                    facts.peer_metrics.status() if facts.peer_metrics else None
                )
                if forwarding != previous_forwarding:
                    with (directory / "forwarding-samples.jsonl").open("a") as observations:
                        observations.write(
                            json.dumps(
                                {
                                    "worker_pid": os.getpid(),
                                    "observed_ns": time.monotonic_ns(),
                                    **forwarding,
                                }
                            )
                            + "\n"
                        )
                    previous_forwarding = forwarding
                await lifecycle.tick()
                try:
                    frame = await asyncio.to_thread(endpoint.receive)
                    if frame is not None:
                        await lifecycle.receive(frame, ingress="probe0", generation=1)
                except EmosaError:
                    pass
                if ready and store.operations():
                    try:
                        await engine.reconcile("pod-1")
                        snapshot = await backend.snapshot()
                    except (EmosaError, ConnectionError, TimeoutError):
                        facts.source.invalidate()
                        await lifecycle.tick()
                        write(directory / "native-session.json", status())
                        continue
                    operations = [
                        public_operation(engine, op.operation_id) for op in store.operations()
                    ]
                    write(
                        directory / "native-operation.json",
                        {
                            "operation": operations[0],
                            "operations": operations,
                            "operation_count": len(operations),
                            "writes": backend.write_count,
                            "journal_write_attempts": sum(op["attempts"] for op in operations),
                            "process_id": store.process_id,
                            "config_ssid": snapshot.config["ssid"],
                            "observed_ssid": snapshot.observed.values.get("ssid"),
                        },
                    )
                write(directory / "native-session.json", status())
                if lifecycle.state == "incompatible":
                    raise RuntimeError("onboarding lifecycle stopped; inspect native-session.json")
    finally:
        if lifecycle:
            write(directory / "native-session.json", status())
            lifecycle.close()
        store.close()
        if channel_store:
            channel_store.close()
        if reporting_policy_store:
            reporting_policy_store.close()
        if mqtt:
            mqtt.close()
        await session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--duration", type=int, default=110)
    parser.add_argument("--telemetry", action="store_true")
    args = parser.parse_args()
    if not 30 <= args.duration <= 86400:
        parser.error("duration must be 30–86400 seconds")
    asyncio.run(worker(args.directory, duration=args.duration, telemetry=args.telemetry))
