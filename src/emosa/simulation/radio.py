"""Independent OVSDB-to-radio lab manager, with State derived from driver reads.

This is a simulation manager, not OpenSync firmware or an EasyMesh endpoint.
It deliberately imports no adapter, operation engine or application predicate.
"""

import copy
import re
from dataclasses import dataclass, field

from emosa.opensync.schema import TABLES

MONITOR = copy.deepcopy(TABLES)
MONITOR["Wifi_Radio_Config"] += ["channel"]


async def seed_radio_database(session):
    """Create a synthetic binding with no positive radio State before observation."""
    vif = {
        "if_name": "wlan0",
        "bridge": "br-lan",
        "mode": "ap",
        "enabled": True,
        "multi_ap": "none",
        "ssid": "emosa-radio-initial",
        "wpa": True,
        "wpa_key_mgmt": ["set", ["wpa2-psk"]],
        "wpa_psks": ["map", [["key", "RadioInitial2026!"]]],
        "rsn_pairwise_ccmp": True,
        "wpa_pairwise_tkip": False,
        "wpa_pairwise_ccmp": False,
        "security": ["map", []],
    }
    entries = [
        ("Wifi_VIF_Config", "vif", vif),
        (
            "Wifi_VIF_State",
            "vs",
            {"if_name": "wlan0", "enabled": False, "vif_config": ["named-uuid", "vif"]},
        ),
        (
            "Wifi_Radio_Config",
            "radio",
            {
                "if_name": "phy1",
                "freq_band": "2.4G",
                "enabled": True,
                "channel": 6,
                "vif_configs": ["set", [["named-uuid", "vif"]]],
            },
        ),
        (
            "Wifi_Radio_State",
            "rs",
            {
                "if_name": "phy1",
                "radio_config": ["named-uuid", "radio"],
                "freq_band": "2.4G",
                "enabled": False,
                "vif_states": ["set", [["named-uuid", "vs"]]],
            },
        ),
    ]
    ops = [
        {
            "op": "wait",
            "table": table,
            "where": [],
            "columns": ["if_name"],
            "until": "==",
            "rows": [],
            "timeout": 0,
        }
        for table, _, _ in entries
    ]
    ops += [
        {"op": "insert", "table": table, "uuid-name": name, "row": row}
        for table, name, row in entries
    ]
    result = await session.transact(ops)
    if len(result) != len(ops) or any(not isinstance(r, dict) or "error" in r for r in result):
        raise RuntimeError("radio database seed rejected")


@dataclass(frozen=True)
class RadioConfig:
    ssid: str
    passphrase: str = field(repr=False)


def desired_config(vif, radio):
    """The complete supported lab shape: one existing AP, one key, channel 6."""
    if (
        vif.get("if_name") != "wlan0"
        or vif.get("bridge") != "br-lan"
        or vif.get("mode") != "ap"
        or vif.get("enabled") is not True
        or vif.get("wpa") is not True
        or vif.get("wpa_key_mgmt") != ["wpa2-psk"]
        or vif.get("rsn_pairwise_ccmp") is not True
        or vif.get("wpa_pairwise_tkip") is not False
        or vif.get("wpa_pairwise_ccmp") is not False
        or vif.get("security")
        or vif.get("multi_ap") not in (None, "none")
        or set(vif.get("wpa_psks", {})) != {"key"}
        or radio.get("if_name") != "phy1"
        or radio.get("freq_band") != "2.4G"
        or radio.get("channel") != 6
        or radio.get("enabled") is not True
        or len(radio.get("vif_configs", [])) != 1
    ):
        raise ValueError("unsupported single-AP radio configuration")
    ssid, key = vif.get("ssid", ""), vif["wpa_psks"]["key"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}", ssid):
        raise ValueError("SSID outside the explicit lab text profile")
    if not 8 <= len(key) <= 63 or any(not 32 <= ord(c) <= 126 for c in key):
        raise ValueError("unsupported lab passphrase representation")
    return RadioConfig(ssid, key)


def observed_state(observation):
    """No Config values enter this function. Missing reads cannot imply success."""
    status, live, interface = (
        observation["status"],
        observation["config"],
        observation["interface"],
    )
    if (
        status.get("state") != "ENABLED"
        or status.get("ssid[0]") != live.get("ssid")
        or interface.get("ssid") != live.get("ssid")
        or interface.get("type") != "AP"
        or interface.get("channel") != 6
        or status.get("freq") != "2437"
        or live.get("bssid") != "02:00:00:ec:02:00"
        or interface.get("addr") != live.get("bssid")
        or live.get("wpa") != "2"
        or live.get("key_mgmt", "").strip() != "WPA-PSK"
        or live.get("rsn_pairwise_cipher", "").strip() != "CCMP"
        or live.get("group_cipher") != "CCMP"
        or not live.get("passphrase")
    ):
        raise ValueError("incomplete or inconsistent live radio observation")
    return {
        "ssid": live["ssid"],
        "mode": "ap",
        "enabled": True,
        "wpa": True,
        "wpa_key_mgmt": ["set", ["wpa2-psk"]],
        "wpa_psks": ["map", [["key", live["passphrase"]]]],
        "rsn_pairwise_ccmp": True,
        "wpa_pairwise_tkip": False,
        "wpa_pairwise_ccmp": False,
        "security": ["map", []],
        "mac": live["bssid"],
    }


def row_guard(table, row_id, row):
    return {
        "op": "wait",
        "table": table,
        "where": [["_uuid", "==", ["uuid", row_id]]],
        "columns": list(row),
        "until": "==",
        "rows": [row],
        "timeout": 0,
    }


def station_updates(snap, station_read):
    """One atomic membership snapshot, preserving existing station UUIDs."""
    table = "Wifi_Associated_Clients"
    previous = snap["tables"].get(table, {})
    decoded = {u: snap["schema"].row(table, row) for u, row in previous.items()}
    current = {row["mac"]: u for u, row in decoded.items()}
    desired = {row["mac"] for row in station_read["clients"]}
    if len(current) != len(decoded) or len(desired) != len(station_read["clients"]):
        raise ValueError("duplicate station identity")
    ops = [
        {
            "op": "wait",
            "table": table,
            "where": [],
            "columns": ["_uuid", "mac", "state"],
            "until": "==",
            "timeout": 0,
            "rows": [
                {"_uuid": ["uuid", u], "mac": r["mac"], "state": r["state"]}
                for u, r in decoded.items()
            ],
        }
    ]
    references = []
    for index, mac in enumerate(sorted(desired)):
        if mac in current:
            u = current[mac]
            if decoded[u]["state"] != "active":
                ops.append(
                    {
                        "op": "update",
                        "table": table,
                        "where": [["_uuid", "==", ["uuid", u]]],
                        "row": {"state": "active"},
                    }
                )
            references.append(["uuid", u])
        else:
            name = f"station{index}"
            ops.append(
                {
                    "op": "insert",
                    "table": table,
                    "uuid-name": name,
                    "row": {"mac": mac, "state": "active"},
                }
            )
            references.append(["named-uuid", name])
    for mac in current.keys() - desired:
        ops.append(
            {"op": "delete", "table": table, "where": [["_uuid", "==", ["uuid", current[mac]]]]}
        )
    return ops, ["set", references]


class RadioManager:
    def __init__(self, session, driver):
        self.session, self.driver = session, driver
        self.applied = None

    async def cycle(self, *, withhold=False):
        snap = await self.session.snapshot()
        tables, schema = snap["tables"], snap["schema"]
        # Dedicated disposable DB only. Extra radios or VIFs cannot be ignored.
        names = ("Wifi_Radio_Config", "Wifi_VIF_Config", "Wifi_Radio_State", "Wifi_VIF_State")
        if any(len(tables.get(t, {})) != 1 for t in names):
            raise ValueError("ambiguous radio-manager database scope")
        rows = {t: next(iter(tables[t].items())) for t in names}
        decoded = {t: schema.row(t, row) for t, (_, row) in rows.items()}
        rc, vc, rs, vs = (rows[t][0] for t in names)
        if (
            decoded["Wifi_Radio_Config"]["vif_configs"] != [vc]
            or decoded["Wifi_Radio_State"].get("radio_config") != rc
            or decoded["Wifi_Radio_State"].get("vif_states") != [vs]
            or decoded["Wifi_VIF_State"].get("vif_config") != vc
            or decoded["Wifi_VIF_State"].get("if_name") != "wlan0"
        ):
            raise ValueError("inconsistent radio-manager references")
        outcome = {"generation": snap["generation"], "revision": snap["revision"]}
        try:
            config = desired_config(decoded["Wifi_VIF_Config"], decoded["Wifi_Radio_Config"])
        except ValueError:
            config = None
            outcome["configuration"] = "unsupported"
        if config is not None:
            outcome["configuration"] = "withheld" if withhold else "accepted"
            identity = (snap["generation"], config)
            if not withhold and self.applied != identity:
                try:
                    await self.driver.apply(config)
                    self.applied = identity
                    outcome["driver_apply"] = "completed"
                except (OSError, RuntimeError, TimeoutError):
                    self.applied = None
                    outcome["driver_apply"] = "failed"
        try:
            observed = await self.driver.observe()
            state = observed_state(observed)
            outcome["radio"] = "observed"
            outcome["observation"] = {
                "ssid": state["ssid"],
                "bssid": state["mac"],
                "channel": observed["interface"]["channel"],
                "sources": ["hostapd STATUS", "hostapd GET_CONFIG", "nl80211 iw dev info"],
            }
        except (OSError, RuntimeError, TimeoutError, ValueError, KeyError):
            # Withdraw positive state when the AP cannot be independently read.
            state = {"enabled": False, "wpa_psks": ["map", []]}
            outcome["radio"] = "unavailable"
        ops = [row_guard(t, rows[t][0], rows[t][1]) for t in names]
        if outcome["radio"] == "observed" and observed.get("stations", {}).get("complete") is True:
            station_ops, references = station_updates(snap, observed["stations"])
            ops += station_ops
            state["associated_clients"] = references
            outcome["stations"] = observed["stations"]
        ops += [
            {
                "op": "update",
                "table": "Wifi_VIF_State",
                "where": [["_uuid", "==", ["uuid", vs]]],
                "row": state,
            }
        ]
        radio_state = {"enabled": False}
        if outcome["radio"] == "observed":
            radio_state = {"channel": 6, "freq_band": "2.4G", "mac": state["mac"], "enabled": True}
        ops.append(
            {
                "op": "update",
                "table": "Wifi_Radio_State",
                "where": [["_uuid", "==", ["uuid", rs]]],
                "row": radio_state,
            }
        )
        results = await self.session.transact(ops, generation=snap["generation"])
        if len(results) != len(ops) or any(
            not isinstance(r, dict) or "error" in r for r in results
        ):
            outcome["publication"] = "conflict"
        elif any(
            r.get("count") != 1
            for op, r in zip(ops, results, strict=True)
            if op["op"] in ("update", "delete")
        ):
            raise RuntimeError("unexpected State publication count")
        else:
            outcome["publication"] = "observed-state"
        return outcome
