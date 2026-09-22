"""Owned OVSDB fixture feeding the report coordinator, never a physical profile.

The database initiates a local Unix-socket connection to a read-only monitor.
Capabilities/virtual adjacency are explicit fixture declarations; only current
BSS/radio/client facts come from the database. The fixture administrator and
separate simulated manager perform changes, never the report coordinator.
"""

import asyncio
import hashlib
import json
import time
from dataclasses import replace

from emosa.easymesh_payloads import (
    AssociatedClients,
    BssClients,
    BssConfigurationReport,
    ConfiguredBss,
    ConfiguredRadio,
    decode_value,
)
from emosa.errors import EmosaError, Reason
from emosa.opensync.mapping import check_results
from emosa.opensync.session import OvsSession
from emosa.opensync.topology import COLUMNS as TOPOLOGY_COLUMNS
from emosa.opensync.topology import project
from emosa.simulation.database import SimDatabase, SimManager
from emosa.simulation.wire_reports import AGENT, BSSID, RUID, fixtures
from emosa.wire.coordinator import ReportSource
from emosa.wire.topology_values import DeviceInformation, LocalInterface

BEFORE = "EMOSA-report-before"
AFTER = "EMOSA-report-after"
SERIAL = "EMOSA-REPORT-COORDINATOR-FIXTURE"
RADIO_MAC = "02:00:00:00:40:20"
SECURITY = ["wpa", "wpa_key_mgmt", "rsn_pairwise_ccmp", "wpa_pairwise_tkip", "wpa_pairwise_ccmp"]
COLUMNS = {t: list(cols) for t, cols in TOPOLOGY_COLUMNS.items()}
COLUMNS["Wifi_VIF_Config"] += ["multi_ap", "ssid", *SECURITY]
COLUMNS["Wifi_VIF_State"] += ["multi_ap", "associated_clients", *SECURITY]
COLUMNS["Wifi_Radio_State"] += ["ht_mode"]
COLUMNS["Wifi_Associated_Clients"] = ["mac", "state"]
BINDING = {
    "pod_id": "report-fixture",
    "al_mac": AGENT.hex(":"),
    "expected_serial": SERIAL,
    "topology": {
        "radios": [
            {
                "radio_id": "radio-1",
                "if_name": "lab-radio",
                "ruid": RUID.hex(":"),
                "expected_mac": RADIO_MAC,
                "interfaces": [
                    {
                        "interface_id": "bss-1",
                        "if_name": "lab-ap",
                        "mode": "ap",
                        "expected_mac": BSSID.hex(":"),
                    }
                ],
            }
        ]
    },
}


def _require(condition):
    if not condition:
        raise EmosaError(Reason.NOT_READY, "observed fixture exceeds report source contract")


class DatabaseReportFixture:
    def __init__(self, *, ingress="fixture", clock=time.monotonic):
        self.clock = clock
        self.database = SimDatabase()
        self.admin = OvsSession(self.database.endpoint)
        self.manager = SimManager(self.database.endpoint)
        self.peer, self.capabilities, self.template = fixtures(ingress=ingress)
        declaration = {
            "version": 1,
            "binding": BINDING,
            "radio": "2.4 GHz HT20, class81, explicit synthetic capability and virtual bridge",
            "security": "PSK/CCMP pure fronthaul; no associated clients",
        }
        digest = hashlib.sha256(json.dumps(declaration, sort_keys=True).encode()).hexdigest()
        self.source = ReportSource(self.peer, BINDING["pod_id"], digest, clock=clock)
        listener = "unix:" + str(self.database.directory / "report.sock")
        self.listener = listener
        self.session = OvsSession(
            "p" + listener, read_only=True, monitor_columns=COLUMNS, timeout=0.5
        )
        self.refreshes = 0

    async def start(self):
        await self.database.start()
        await self.database.seed(serial_number=SERIAL, sole_radio=True)
        rows = {
            "Wifi_VIF_Config": {"ssid": BEFORE},
            "Wifi_VIF_State": {"ssid": BEFORE, "mac": BSSID.hex(":"), "multi_ap": "none"},
            "Wifi_Radio_Config": {"freq_band": "2.4G"},
            "Wifi_Radio_State": {
                "freq_band": "2.4G",
                "channel": 6,
                "mac": RADIO_MAC,
                "ht_mode": "HT20",
            },
        }
        check_results(
            await self.admin.transact(
                [
                    {"op": "update", "table": table, "where": [], "row": row}
                    for table, row in rows.items()
                ]
            ),
            [1] * len(rows),
        )
        await self.manager.start()
        await self.database.manager_remote(self.listener)
        await self.wait_ready()
        return self

    async def refresh(self):
        started = self.clock()
        try:
            raw = await self.session.snapshot()
            raw["schema"].qualify_synthetic()
            _require(
                all(
                    set(cols) <= set(self.session.selected_tables.get(t, ()))
                    for t, cols in COLUMNS.items()
                )
            )
            observed = project(
                BINDING["pod_id"], raw, BINDING, state_provenance="independent-simulated-manager"
            )
            _require(observed["ready"] and observed["complete"])
            # An empty monitored table can be absent from OVSDB's initial map.
            # Selection/schema completeness was checked above; this is not a
            # default for an unsupported or unmonitored client table.
            _require(not raw["tables"].get("Wifi_Associated_Clients", {}))
            for table in ("Wifi_VIF_Config", "Wifi_VIF_State"):
                row = raw["schema"].row(table, next(iter(raw["tables"][table].values())))
                _require(
                    row.get("multi_ap") == "none"
                    and row.get("wpa") is True
                    and row.get("wpa_key_mgmt") == ["wpa2-psk"]
                    and row.get("rsn_pairwise_ccmp") is True
                    and row.get("wpa_pairwise_tkip") is False
                    and row.get("wpa_pairwise_ccmp") is False
                    and (table != "Wifi_VIF_State" or row.get("associated_clients") == [])
                )
            radio = observed["radios"][0]
            state = raw["schema"].row(
                "Wifi_Radio_State", next(iter(raw["tables"]["Wifi_Radio_State"].values()))
            )
            _require(
                radio["enabled"]
                and radio["freq_band"] == "2.4G"
                and type(radio["channel"]) is int
                and 1 <= radio["channel"] <= 13
                and state.get("ht_mode") == "HT20"
            )
            operational = decode_value(
                0x83, bytes.fromhex(observed["operational_bss_value"]["value_hex"])
            )
            _require(len(operational.radios) == 1 and len(operational.radios[0].bsses) == 1)
            bss = operational.radios[0].bsses[0]
            topology = replace(
                self.template,
                device=DeviceInformation(
                    AGENT,
                    (
                        LocalInterface(AGENT, 1, b""),
                        LocalInterface(BSSID, 0x103, BSSID + bytes((0, 0, radio["channel"], 0))),
                    ),
                ),
                operational=operational,
                configuration=BssConfigurationReport(
                    (ConfiguredRadio(RUID, (ConfiguredBss(BSSID, 0x40, bss.ssid),)),)
                ),
                clients=AssociatedClients((BssClients(BSSID, ()),)),
            )
            self.source.publish(
                (raw["generation"], raw["revision"]),
                self.capabilities,
                topology,
                observed_at=started,
            )
            self.refreshes += 1
            return True
        except (EmosaError, ConnectionError, TimeoutError):
            self.source.invalidate()
            return False

    async def wait_ready(self, *, ssid=None):
        end = self.clock() + 10
        while self.clock() < end:
            if await self.refresh():
                bss = self.source.current().topology.operational.radios[0].bsses[0]
                if ssid is None or bss.ssid == ssid.encode():
                    return
            await asyncio.sleep(0.05)
        raise EmosaError(Reason.NOT_READY, "owned report fixture did not become ready")

    async def change_config(self):
        check_results(
            await self.admin.transact(
                [{"op": "update", "table": "Wifi_VIF_Config", "where": [], "row": {"ssid": AFTER}}]
            ),
            [1],
        )

    async def close(self):
        self.source.invalidate()
        await self.session.close()
        await self.manager.close()
        await self.admin.close()
        await self.database.close()
