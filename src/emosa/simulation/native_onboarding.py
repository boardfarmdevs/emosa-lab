"""Packet worker for the owned native-controller/hwsim onboarding experiment.

It accepts only a marked local run directory, no desired SSID/key or pod address.
The pod simulator initiates OVSDB; the independent radio manager publishes State.
"""

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path

from emosa.easymesh_payloads import (
    APHTCapabilities,
    APOperationalBss,
    APRadioAdvancedCapabilities,
    APRadioBasicCapabilities,
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
from emosa.wire.autoconfiguration import PeerBinding
from emosa.wire.coordinator import ReportSource
from emosa.wire.ethernet import EthernetEndpoint
from emosa.wire.onboarding import OnboardingSession
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

    def __init__(self, backend, binding):
        self.backend = backend
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
            caps, radios=(radio,), profile2=Profile2APCapability(0, 0, 0x40, 0)
        )
        self.source = ReportSource(
            binding,
            "pod-1",
            hashlib.sha256(b"owned-hwsim:sole-HT20-channel6:PSK-CCMP:nonDPP:v1").hexdigest(),
        )
        self.inventory = DeviceInventory(
            SERIAL.encode(),
            b"0.1.0",
            b"owned-hwsim-simulation",
            (InventoryRadio(RADIO, b"mac80211_hwsim"),),
        )

    async def refresh(self):
        started = time.monotonic()
        try:
            raw = await self.backend.session.snapshot()
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
            if associated or vif.get("associated_clients"):
                raise EmosaError(
                    Reason.NOT_READY, "station reporting requires further implementation"
                )
            ssid = vif["ssid"].encode()
            topology = replace(
                self.template,
                device=DeviceInformation(
                    AGENT,
                    (
                        LocalInterface(AGENT, 1, b""),
                        LocalInterface(RADIO, 0x103, RADIO + b"\0\0\x06\0"),
                    ),
                ),
                bridges=BridgingCapability(((AGENT, RADIO),)),
                neighbors1905=(Neighbors1905(AGENT, (Neighbor(CONTROLLER, False),)),),
                operational=APOperationalBss(
                    (OperationalRadio(RADIO, (OperationalBss(RADIO, ssid),)),)
                ),
                configuration=BssConfigurationReport(
                    (ConfiguredRadio(RADIO, (ConfiguredBss(RADIO, 0x40, ssid),)),)
                ),
                clients=AssociatedClients((BssClients(RADIO, ()),)),
            )
            self.source.publish(
                (raw["generation"], raw["revision"]),
                self.capabilities,
                topology,
                observed_at=started,
                lifetime=2,
            )
            return True
        except (EmosaError, ConnectionError, TimeoutError):
            self.source.invalidate()
            return False


async def worker(directory):
    directory = directory.resolve(strict=True)
    if (
        directory.parent != RADIO_ROOT
        or json.loads((directory / "native-owner.json").read_text()) != OWNER
    ):
        raise ValueError("expected an owned native onboarding run")
    session = OvsSession("punix:" + str(directory / "database/native-pod.sock"))
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
    facts = RadioReportSource(backend, binding)
    lifecycle = None
    try:
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

            lifecycle = OnboardingSession(facts.source, endpoint.send, factory, facts.inventory)
            end = time.monotonic() + 110
            while time.monotonic() < end and not (directory / "stop-worker").exists():
                await facts.refresh()
                await lifecycle.tick()
                try:
                    frame = await asyncio.to_thread(endpoint.receive)
                    if frame is not None:
                        await lifecycle.receive(frame, ingress="probe0", generation=1)
                except EmosaError:
                    pass
                if store.operations():
                    await engine.reconcile("pod-1")
                    snapshot = await backend.snapshot()
                    write(
                        directory / "native-operation.json",
                        {
                            "operation": public_operation(
                                engine, store.operations()[0].operation_id
                            ),
                            "operation_count": len(store.operations()),
                            "writes": backend.write_count,
                            "config_ssid": snapshot.config["ssid"],
                            "observed_ssid": snapshot.observed.values.get("ssid"),
                        },
                    )
                write(directory / "native-session.json", lifecycle.status())
                if lifecycle.state in ("failed", "source_lost", "incompatible"):
                    raise RuntimeError("onboarding lifecycle stopped; inspect native-session.json")
    finally:
        if lifecycle:
            write(directory / "native-session.json", lifecycle.status())
            lifecycle.close()
        store.close()
        await session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    asyncio.run(worker(parser.parse_args().directory))
