# SPDX-License-Identifier: Apache-2.0
"""A legacy pod's station on the site's GTP BSS, as an EMOSA scope (spec 3.10).

A legacy pod's core makes no backhaul station: ``cm`` selects the ``mode=sta`` rows it finds, and
``wm`` gives a station only the credentials its own ``credential_configs`` links. Its database is
its vendor's seed again at every start of Open vSwitch: the operator's onboarding credentials,
unlinked, and no station. So EMOSA writes the station that reaches the site's GTP (spec 8.2), once
per start of the pod's OpenSync, while the station has no row; a row already there is left alone,
whoever wrote it (the pod, an operator, a start-up hook).

The write is the one that joined a 2.x pod to a GTP BSS by hand (13 October), one guarded
transaction: a credential (``ssid``, the ``security`` map, ``onboard_type=gre``), the station in
vconf form linked to it (``mode=sta``, ``enabled``, ``vif_radio_idx`` 0, the GTP's ``ssid`` and
``security``, ``parent`` the pin if any), its reference in its radio's ``vif_configs``, and its
Inet row enabled. Applied when, on the same start, the row is in that form and ``wm`` shows the
station (its ``Wifi_VIF_State`` row).
"""

import json
import re
from dataclasses import asdict, dataclass

from emosa.backends.base import Snapshot, SubmitResult
from emosa.clock import utc_now
from emosa.errors import EmosaError, Reason
from emosa.model import Observation
from emosa.opensync.mapping import check_results, failed_check, where_uuid
from emosa.opensync.stations import deletion
from emosa.opensync.uplink import BSSID, credential_key, lower_mac, start_instance

MODE = "opensync-2.0-gtp-station"
PROVENANCE = "opensync-wm:Wifi_VIF_Config+Wifi_VIF_State"
INTERFACE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
ENCRYPTION = "WPA-PSK"


@dataclass(frozen=True)
class GtpStationIntent:
    """Write ``station``'s row to join the site's GTP BSS ``ssid``, its passphrase the secret
    ``secret_ref``, pinned to ``bssid`` when one is given (none by default)."""

    pod_id: str
    station: str
    ssid: str
    secret_ref: str
    bssid: str | None = None

    def record(self):
        return asdict(self)

    def validate(self):
        if any(
            not isinstance(x, str) or not x for x in (self.pod_id, self.station, self.secret_ref)
        ):
            raise EmosaError(Reason.INVALID_INPUT, "explicit station and secret reference required")
        if not INTERFACE.match(self.station):
            raise EmosaError(Reason.INVALID_INPUT, "station must be an interface name")
        if (
            not isinstance(self.ssid, str)
            or not 1 <= len(self.ssid.encode("utf-8")) <= 32
            or "\x00" in self.ssid
        ):
            raise EmosaError(Reason.INVALID_INPUT, "SSID must contain 1–32 UTF-8 bytes, no NUL")
        if self.bssid is not None and not BSSID.match(self.bssid):
            raise EmosaError(Reason.INVALID_INPUT, "BSSID must be a lower-case MAC address")

    def target(self, vault):
        self.validate()
        return {
            "station": self.station,
            "ssid": self.ssid,
            "credential_fingerprint": vault.fingerprint(vault.resolve(self.secret_ref)),
            "bssid": self.bssid,
            "joined": True,
        }


def security_map(psk):
    """A legacy pod's WPA2-PSK (spec 3.10): the security map."""
    return ["map", [["encryption", ENCRYPTION], ["key", psk]]]


def station_rows(decoded, table, station):
    return [(u, r) for u, r in decoded.get(table, {}).items() if r.get("if_name") == station]


def all_stations(decoded, own):
    """Every station row of the pod: [{"uuid", "if_name", "own", "associated"}], by name; a
    station associated when a State row of its name has a parent (``wl bssid``)."""
    associated = {
        r.get("if_name")
        for r in decoded.get("Wifi_VIF_State", {}).values()
        if r.get("mode") == "sta" and lower_mac(r.get("parent"))
    }
    return sorted(
        (
            {
                "uuid": u,
                "if_name": r.get("if_name"),
                "own": u == own,
                "associated": r.get("if_name") in associated,
            }
            for u, r in decoded.get("Wifi_VIF_Config", {}).items()
            if r.get("mode") == "sta"
        ),
        key=lambda s: (s["if_name"] or "", s["uuid"]),
    )


class GtpStationBackend:
    """The GTP station scope of one legacy pod, bound by serial: its station ``station`` on the
    radio of ``band``."""

    mode = MODE

    def __init__(self, pod_id, session, vault, *, serial, station, band):
        self.pod_id, self.session, self.vault = pod_id, session, vault
        self.expected_serial, self.station, self.band = serial, station, band
        self.instance = None
        self.last = None
        self.write_count = 0

    def _decode(self, raw):
        schema = raw["schema"]
        return {
            t: {u: schema.row(t, row) for u, row in rows.items()}
            for t, rows in raw["tables"].items()
        }

    def _binding(self, raw):
        decoded = self._decode(raw)
        nodes = decoded.get("AWLAN_Node", {})
        if len(nodes) != 1 or next(iter(nodes.values())).get("serial_number") != (
            self.expected_serial
        ):
            raise EmosaError(Reason.NOT_READY, "pod identity absent or not the bound serial")
        self.instance = start_instance(decoded)
        return decoded, next(iter(nodes))

    def _configured(self, decoded):
        """The station as the scope writes it: its row in vconf form with one linked credential
        (onboard_type gre) of the same SSID and passphrase, and wm's State of it. ``present``: the
        station has a row at all, whoever wrote it."""
        rows = station_rows(decoded, "Wifi_VIF_Config", self.station)
        vif = rows[0][1] if len(rows) == 1 else {}
        credentials = decoded.get("Wifi_Credential_Config", {})
        linked = [credentials[u] for u in vif.get("credential_configs") or [] if u in credentials]
        sole = linked[0] if len(linked) == 1 else {}
        key = credential_key(vif)
        written = (
            vif.get("mode") == "sta"
            and vif.get("enabled") is True
            and bool(vif.get("ssid"))
            and key is not None
            and sole.get("onboard_type") == "gre"
            and sole.get("ssid") == vif.get("ssid")
            and credential_key(sole) == key
        )
        states = [
            r
            for _, r in station_rows(decoded, "Wifi_VIF_State", self.station)
            if r.get("mode") == "sta"
        ]
        return {
            "station": self.station,
            "present": bool(rows),
            "ssid": vif.get("ssid") if written else None,
            "credential_fingerprint": self.vault.fingerprint(key) if written else None,
            "bssid": (lower_mac(vif.get("parent")) or None) if written else None,
            "joined": bool(states),
            # on a BSS (its State's parent), as the vendor healthcheck's `wl bssid` sees it
            "associated": any(lower_mac(r.get("parent")) for r in states),
            # every station row of the pod, the scope's own marked (the guard's, spec 3.10)
            "stations": all_stations(decoded, rows[0][0] if written else None),
        }

    async def remove_station(self, uuid):
        """spec 3.10, the station guard on a converted pod: another station row than the scope's
        own (the seed's, a hook's), not associated past the guard, deleted with what only it uses
        (emosa.opensync.stations). The number of station rows deleted."""
        raw = await self.session.snapshot()
        decoded, node_uuid = self._binding(raw)
        row = decoded.get("Wifi_VIF_Config", {}).get(uuid)
        if not row or row.get("mode") != "sta":
            raise EmosaError(Reason.NOT_READY, "no such station row")
        operations, counts = deletion(decoded, [uuid])
        operations = [
            {
                "op": "wait",
                "table": "AWLAN_Node",
                "where": where_uuid(node_uuid),
                "columns": ["serial_number"],
                "until": "==",
                "rows": [{"serial_number": self.expected_serial}],
                "timeout": 0,
            },
            *operations,
        ]
        results = await self.session.transact(operations, generation=raw["generation"])
        check_results(results, [None, *counts])
        return 1

    async def remove(self, intent):
        """spec 3.10, the station's guard: the station in the scope's form (the intent's SSID), not
        associated past the guard, deleted in one guarded transaction: its row, its linked
        credentials and its radio's reference (a disabled station would still fail the vendor
        healthcheck). The number of rows deleted."""
        self._check(intent)
        raw = await self.session.snapshot()
        decoded, node_uuid = self._binding(raw)
        rows = station_rows(decoded, "Wifi_VIF_Config", self.station)
        if len(rows) != 1:
            raise EmosaError(Reason.NOT_READY, "no single station row")
        uuid, vif = rows[0]
        if vif.get("mode") != "sta" or vif.get("ssid") != intent.ssid:
            raise EmosaError(Reason.OWNERSHIP_CONFLICT, "not the GTP station")
        credentials = sorted(
            u
            for u in vif.get("credential_configs") or []
            if u in decoded.get("Wifi_Credential_Config", {})
        )
        radios = sorted(
            u
            for u, r in decoded.get("Wifi_Radio_Config", {}).items()
            if uuid in (r.get("vif_configs") or [])
        )
        operations = [
            {
                "op": "wait",
                "table": "AWLAN_Node",
                "where": where_uuid(node_uuid),
                "columns": ["serial_number"],
                "until": "==",
                "rows": [{"serial_number": self.expected_serial}],
                "timeout": 0,
            },
            {
                "op": "wait",
                "table": "Wifi_VIF_Config",
                "where": where_uuid(uuid),
                "columns": ["if_name", "mode", "ssid"],
                "until": "==",
                "rows": [{"if_name": self.station, "mode": "sta", "ssid": intent.ssid}],
                "timeout": 0,
            },
        ]
        counts = [None, None]
        for radio in radios:
            operations.append(
                {
                    "op": "mutate",
                    "table": "Wifi_Radio_Config",
                    "where": where_uuid(radio),
                    "mutations": [["vif_configs", "delete", ["set", [["uuid", uuid]]]]],
                }
            )
            counts.append(1)
        operations.append({"op": "delete", "table": "Wifi_VIF_Config", "where": where_uuid(uuid)})
        counts.append(1)
        for credential in credentials:
            operations.append(
                {"op": "delete", "table": "Wifi_Credential_Config", "where": where_uuid(credential)}
            )
            counts.append(1)
        results = await self.session.transact(operations, generation=raw["generation"])
        check_results(results, counts)
        return 1 + len(credentials)

    async def snapshot(self):
        try:
            raw = await self.session.snapshot()
            decoded, _ = self._binding(raw)
            configured = self._configured(decoded)
            self.last = Snapshot(
                configured,
                Observation(
                    self.pod_id,
                    "gtp-station",
                    dict(configured),
                    "ovsdb",
                    self.mode,
                    raw["generation"],
                    utc_now(),
                    bool(raw["ready"]),
                    PROVENANCE,
                    revision=raw["revision"],
                ),
                bool(raw["ready"]),
                raw["generation"],
                raw["schema"].fingerprint,
            )
        except (EmosaError, ConnectionError, TimeoutError):
            if self.last is None:
                raise
            self.last.ready = False
            self.last.observed.fresh = False
        return self.last

    def _check(self, intent, decoded=None):
        """The intent is this pod's station; with the pod's rows: no station row, and the one
        radio of the station's band. (the radio's uuid, the station's Inet row as (uuid, row) or
        None)."""
        intent.target(self.vault)
        if intent.pod_id != self.pod_id or intent.station != self.station:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "request exceeds the bound pod")
        if decoded is None:
            return None
        if station_rows(decoded, "Wifi_VIF_Config", self.station):
            # the pod's, an operator's or a start-up hook's station: left alone
            raise EmosaError(Reason.OWNERSHIP_CONFLICT, "the station has a row")
        radios = [
            u
            for u, r in decoded.get("Wifi_Radio_Config", {}).items()
            if r.get("freq_band") == self.band
        ]
        if len(radios) != 1:
            raise EmosaError(Reason.NOT_READY, "no single radio of the station's band")
        inets = station_rows(decoded, "Wifi_Inet_Config", self.station)
        if len(inets) > 1:
            raise EmosaError(Reason.NOT_READY, "more than one Inet row of the station")
        return radios[0], (inets[0] if inets else None)

    async def plan(self, intent):
        self._check(intent)
        raw = await self.session.snapshot()
        decoded, _ = self._binding(raw)
        self._check(intent, decoded)
        if not raw["ready"]:
            raise EmosaError(Reason.NOT_READY, "the pod's database is not ready")
        return {
            "mapping": MODE,
            "action": "gtp-station",
            "station": intent.station,
            "ssid": intent.ssid,
            "pinned": intent.bssid is not None,
            "instance": self.instance,
            "fields": [
                "Wifi_Credential_Config",
                "Wifi_VIF_Config",
                "Wifi_Radio_Config.vif_configs",
                "Wifi_Inet_Config.enabled",
            ],
            "guard": "pod serial, no station row",
        }

    def transaction(self, intent, psk, node_uuid, radio_uuid, inet):
        """The guarded write (spec 3.10, the GTP station): its operations and the counts each
        must match."""
        station = {
            "if_name": self.station,
            "mode": "sta",
            "enabled": True,
            "vif_radio_idx": 0,
            "ssid": intent.ssid,
            "security": security_map(psk),
            "credential_configs": ["set", [["named-uuid", "gtp_credential"]]],
        }
        if intent.bssid is not None:
            station["parent"] = intent.bssid
        operations = [
            {
                "op": "wait",
                "table": "AWLAN_Node",
                "where": where_uuid(node_uuid),
                "columns": ["serial_number"],
                "until": "==",
                "rows": [{"serial_number": self.expected_serial}],
                "timeout": 0,
            },
            {
                "op": "wait",
                "table": "Wifi_VIF_Config",
                "where": [["if_name", "==", self.station]],
                "columns": ["if_name"],
                "until": "==",
                "rows": [],
                "timeout": 0,
            },
            {
                "op": "insert",
                "table": "Wifi_Credential_Config",
                "uuid-name": "gtp_credential",
                "row": {"ssid": intent.ssid, "security": security_map(psk), "onboard_type": "gre"},
            },
            {
                "op": "insert",
                "table": "Wifi_VIF_Config",
                "uuid-name": "gtp_station",
                "row": station,
            },
            {
                "op": "mutate",
                "table": "Wifi_Radio_Config",
                "where": where_uuid(radio_uuid),
                "mutations": [["vif_configs", "insert", ["set", [["named-uuid", "gtp_station"]]]]],
            },
        ]
        if inet is not None:
            operations.append(
                {
                    "op": "update",
                    "table": "Wifi_Inet_Config",
                    "where": where_uuid(inet[0]),
                    "row": {"enabled": True, "network": True},
                }
            )
        else:
            operations.append(
                {
                    "op": "insert",
                    "table": "Wifi_Inet_Config",
                    "row": {
                        "if_name": self.station,
                        "if_type": "vif",
                        "enabled": True,
                        "network": True,
                        "ip_assign_scheme": "dhcp",
                    },
                }
            )
        return operations, [None, None, None, None, 1, 1 if inet is not None else None]

    async def submit(self, intent, attempt):
        await self.plan(intent)
        raw = await self.session.snapshot()
        decoded, node_uuid = self._binding(raw)
        if raw["generation"] != attempt["session_generation"]:
            return SubmitResult("rejected", {}, Reason.NOT_READY)
        radio_uuid, inet = self._check(intent, decoded)
        psk = self.vault.resolve(intent.secret_ref)
        operations, counts = self.transaction(intent, psk, node_uuid, radio_uuid, inet)
        self.write_count += 1
        try:
            results = await self.session.transact(
                operations, attempt["transaction_id"], attempt["session_generation"]
            )
            check_results(results, counts)
        except (ConnectionError, TimeoutError):
            return SubmitResult("unknown", {"attribution": "unknown"}, Reason.OUTCOME_UNKNOWN)
        except EmosaError as exc:
            return failed_check(exc)
        return SubmitResult(
            "committed",
            {
                "attribution": "reply",
                "transaction_validated": True,
                "transaction_id": attempt["transaction_id"],
                "session_generation": raw["generation"],
                "action": "gtp-station",
                "instance": self.instance,
                "results": json.loads(json.dumps(results, default=str)),
            },
            None,
        )
