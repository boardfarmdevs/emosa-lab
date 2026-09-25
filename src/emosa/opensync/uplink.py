"""The pod's uplink as an EMOSA scope: switch a backhaul station to EasyMesh (option 1).

Data plane option 1 (doc/architecture/data-plane.md §5): the pod's own backhaul
station joins the gateway's Multi-AP backhaul BSS as a 4-address backhaul STA,
bridged into ``br-home``, with no GRE. OpenSync 6.6 expresses it in OVSDB
(``ow_ovsdb_cconf.c``, ``cm2_ovsdb.c``):

- the station's ``Wifi_VIF_Config`` in credential-list mode (``ssid`` empty),
  linked to one ``Wifi_Credential_Config`` with ``onboard_type=multi_ap``;
  ``owm`` builds a ``wpa_supplicant`` network with ``multi_ap_backhaul_sta=1``;
- applied when the station's ``Wifi_VIF_State`` shows ``multi_ap=backhaul_sta``
  and ``wds=true`` and ``cm`` uses the station itself as the uplink
  (``Connection_Manager_Uplink.is_used``), which means no GRE.

The write is one guarded transaction on one existing station row (the pod's
bootstrap creates it). No ``gre`` credential is kept beside the ``multi_ap``
one: osw aborts ``owm`` when it settles on a lower-priority network (§5.3).
OpenSync's own bootstrap restart is the fallback when the new uplink fails.

The credential is pinned to one upstream BSSID (``Wifi_Credential_Config.bssid``,
which owm passes to wpa_supplicant). Unpinned, the station joins any BSS with
the backhaul SSID, including the pod's own backhaul BSS: both are in
``br-home``, and the loop floods the pod's bridge until the host runs out of
memory (seen live, data-plane.md §5.6). A BSSID of the pod itself is refused.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from emosa.backends.base import Snapshot, SubmitResult
from emosa.clock import utc_now
from emosa.errors import EmosaError, Reason
from emosa.model import Observation
from emosa.opensync.mapping import check_results, guard, where_uuid

MODE = "opensync-6.6-uplink"
PROVENANCE = "opensync-cm:Connection_Manager_Uplink+owm:Wifi_VIF_State"
MULTI_AP = "multi-ap"
BSSID = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
STATION_GUARDS = ("if_name", "mode", "enabled", "ssid", "credential_configs", "multi_ap")
# What the switch monitors beyond the AP scope's tables (emosa.opensync.schema.TABLES).
MONITOR = {
    "Wifi_VIF_Config": ["credential_configs", "wds", "parent"],
    "Wifi_VIF_State": ["wds", "parent", "bridge"],
    "Wifi_Credential_Config": ["ssid", "security", "onboard_type", "priority", "enabled", "bssid"],
    "Connection_Manager_Uplink": ["if_name", "if_type", "is_used", "has_L2", "has_L3"],
}


@dataclass(frozen=True)
class UplinkIntent:
    """Move ``station`` onto the EasyMesh backhaul BSS ``bssid`` with ``ssid`` (option 1).

    ``bssid`` is required for a switch (``UplinkBackend`` refuses one without it);
    records journaled before it existed have none.
    """

    pod_id: str
    station: str
    ssid: str
    secret_ref: str
    mode: str = MULTI_AP
    bssid: str | None = None

    def record(self):
        return asdict(self)

    def validate(self):
        if not 1 <= len(self.ssid.encode("utf-8")) <= 32 or "\x00" in self.ssid:
            raise EmosaError(Reason.INVALID_INPUT, "SSID must contain 1–32 UTF-8 bytes, no NUL")
        if self.mode != MULTI_AP:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "only the Multi-AP backhaul uplink")
        if any(
            not isinstance(x, str) or not x for x in (self.pod_id, self.station, self.secret_ref)
        ):
            raise EmosaError(Reason.INVALID_INPUT, "explicit station and secret reference required")
        if self.bssid is not None and not BSSID.match(self.bssid):
            raise EmosaError(Reason.INVALID_INPUT, "BSSID must be a lower-case MAC address")

    def target(self, vault):
        self.validate()
        target = {
            "uplink": MULTI_AP,
            "station": self.station,
            "ssid": self.ssid,
            "credential_fingerprint": vault.fingerprint(vault.resolve(self.secret_ref)),
        }
        if self.bssid is not None:
            target["bssid"] = self.bssid
        return target


def lower_mac(value):
    return value.lower() if isinstance(value, str) and value else None


def own_bssids(decoded):
    """Every MAC address the pod's own VIFs use: none of them may be its upstream."""
    return {
        lower_mac(r.get("mac"))
        for table in ("Wifi_VIF_State", "Wifi_Radio_State")
        for r in decoded.get(table, {}).values()
        if lower_mac(r.get("mac"))
    }


def credential_key(row):
    security = row.get("security") or {}
    return security.get("key") if security.get("encryption") == "WPA-PSK" else None


def uplink_state(decoded, station):
    """How the pod reaches its gateway, from ``cm``'s and ``owm``'s State.

    ``kind`` is ``multi-ap`` only when ``station`` is a connected Multi-AP
    backhaul STA (4-address) and ``cm`` uses it, and nothing else, as its uplink;
    otherwise the ``if_type`` of the uplink ``cm`` uses (``gre``, ``eth``), or None.
    """
    used = [
        r for r in decoded.get("Connection_Manager_Uplink", {}).values() if r.get("is_used") is True
    ]
    states = [
        r
        for r in decoded.get("Wifi_VIF_State", {}).values()
        if r.get("if_name") == station and r.get("mode") == "sta"
    ]
    state = states[0] if len(states) == 1 else {}
    wds_sta = (
        state.get("enabled") is True
        and state.get("multi_ap") == "backhaul_sta"
        and state.get("wds") is True
    )
    if wds_sta and len(used) == 1 and used[0].get("if_name") == station:
        kind = MULTI_AP
    else:
        kind = used[0].get("if_type") if len(used) == 1 else None
    return {
        "kind": kind,
        "in_use": used[0].get("if_name") if len(used) == 1 else None,
        "station": station,
        "ssid": state.get("ssid") or None,
        "parent": lower_mac(state.get("parent")),
        "mac": state.get("mac") or None,
        "state": state,
    }


class UplinkBackend:
    """The uplink scope of one pod: one backhaul station, bound by serial."""

    mode = MODE

    def __init__(self, pod_id, session, vault, *, serial, station):
        self.pod_id, self.session, self.vault = pod_id, session, vault
        self.expected_serial, self.station = serial, station
        self.instance = None  # which start of the pod's OpenSync (its radio rows)
        self.facts = None  # uplink_state of the last read, without the raw State row
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
        # One start of the pod's OpenSync from the next: its start scripts create the
        # radio rows anew (new UUIDs), which then live as long as that start does.
        # AWLAN_Node comes from the database template and keeps its UUID (seen live).
        radios = sorted(decoded.get("Wifi_Radio_Config", {}))
        if not radios:
            raise EmosaError(Reason.NOT_READY, "the pod's radios are not configured yet")
        self.instance = hashlib.sha256(json.dumps(radios).encode()).hexdigest()[:16]
        rows = [
            (u, r)
            for u, r in decoded.get("Wifi_VIF_Config", {}).items()
            if r.get("if_name") == self.station
        ]
        if len(rows) > 1:
            raise EmosaError(Reason.NOT_READY, "backhaul station ambiguous")
        vif_uuid, vif = rows[0] if rows else (None, {})
        if vif_uuid is not None and vif.get("mode") != "sta":
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "the bound uplink VIF is not a station")
        return decoded, vif_uuid, vif

    def _configured(self, decoded, vif):
        """The station's configured uplink: Multi-AP credential-list mode, or not."""
        creds = decoded.get("Wifi_Credential_Config", {})
        linked = [creds[u] for u in vif.get("credential_configs") or [] if u in creds]
        sole = linked[0] if len(linked) == 1 else {}
        key = credential_key(sole)
        multi_ap = (
            vif.get("enabled") is True
            and not vif.get("ssid")
            and sole.get("onboard_type") == "multi_ap"
            and sole.get("enabled") is True
            and key is not None
        )
        return {
            "uplink": MULTI_AP if multi_ap else "other",
            "station": self.station,
            "ssid": sole.get("ssid") if multi_ap else vif.get("ssid"),
            "credential_fingerprint": self.vault.fingerprint(key) if multi_ap else None,
            "bssid": lower_mac(sole.get("bssid")) if multi_ap else None,
        }, (sole if multi_ap else None)

    async def snapshot(self):
        try:
            raw = await self.session.snapshot()
            decoded, vif_uuid, vif = self._binding(raw)
            configured, credential = self._configured(decoded, vif)
            state = uplink_state(decoded, self.station)
            self.facts = {k: v for k, v in state.items() if k != "state"}
            # The key a station connected with is not in its State: the observed
            # credential is the configured one whose SSID the station is on.
            observed = {
                "uplink": state["kind"],
                "station": self.station,
                "ssid": state["ssid"],
                "credential_fingerprint": (
                    configured["credential_fingerprint"]
                    if credential and state["ssid"] == credential.get("ssid")
                    else None
                ),
                "bssid": state["parent"],
            }
            fresh = bool(raw["ready"] and state["state"])
            self.last = Snapshot(
                configured,
                Observation(
                    self.pod_id,
                    self.station,
                    observed,
                    "ovsdb",
                    self.mode,
                    raw["generation"],
                    utc_now(),
                    fresh,
                    PROVENANCE,
                    revision=raw["revision"],
                ),
                bool(raw["ready"] and vif_uuid is not None),
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
        intent.target(self.vault)
        if (intent.pod_id, intent.station) != (self.pod_id, self.station):
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "request exceeds the bound station")
        if intent.bssid is None:
            raise EmosaError(Reason.INVALID_INPUT, "the upstream BSSID is required")
        if decoded is not None and intent.bssid in own_bssids(decoded):
            # joining itself would bridge the pod's backhaul BSS into its own br-home
            raise EmosaError(Reason.INVALID_INPUT, "the upstream BSSID is one of the pod's own")

    async def plan(self, intent):
        self._check(intent)
        raw = await self.session.snapshot()
        decoded, vif_uuid, _ = self._binding(raw)
        self._check(intent, decoded)
        if not raw["ready"] or vif_uuid is None:
            raise EmosaError(Reason.NOT_READY, "the pod's backhaul station row is required")
        state = uplink_state(decoded, self.station)
        if state["kind"] is None:
            # Start only from a working uplink: the switch moves the path EMOSA
            # itself uses, and OpenSync's restart returns the pod to this one.
            raise EmosaError(Reason.NOT_READY, "no working uplink to switch from")
        return {
            "mapping": MODE,
            "action": "multi-ap-uplink",
            "station": self.station,
            "ssid": intent.ssid,
            "bssid": intent.bssid,
            "secret_ref": intent.secret_ref,
            "from": {"kind": state["kind"], "in_use": state["in_use"]},
            "instance": self.instance,
            "fields": ["Wifi_Credential_Config", "Wifi_VIF_Config.credential_configs/ssid"],
            "guard": "pod serial and the station's current row",
        }

    async def submit(self, intent, attempt):
        await self.plan(intent)
        raw = await self.session.snapshot()
        _, vif_uuid, _ = self._binding(raw)
        if raw["generation"] != attempt["session_generation"]:
            return SubmitResult("rejected", {}, Reason.NOT_READY)
        node_uuid = next(iter(raw["tables"]["AWLAN_Node"]))
        vif = raw["tables"]["Wifi_VIF_Config"][vif_uuid]
        key = self.vault.resolve(intent.secret_ref)
        transaction = [
            {
                "op": "wait",
                "table": "AWLAN_Node",
                "where": [],
                "columns": ["_uuid", "serial_number"],
                "until": "==",
                "rows": [{"_uuid": ["uuid", node_uuid], "serial_number": self.expected_serial}],
                "timeout": 0,
            },
            guard("Wifi_VIF_Config", vif_uuid, vif, [c for c in STATION_GUARDS if c in vif]),
            {
                "op": "insert",
                "table": "Wifi_Credential_Config",
                "uuid-name": "backhaul",
                "row": {
                    "ssid": intent.ssid,
                    "security": ["map", [["encryption", "WPA-PSK"], ["key", key]]],
                    "onboard_type": "multi_ap",
                    "priority": 1,
                    "enabled": True,
                    "bssid": intent.bssid,
                },
            },
            {
                # Credential-list mode: the station's own SSID and security empty, one
                # linked multi_ap credential. owm then sets multi_ap, 4-address and
                # br-home itself; the bootstrap credential stays, unlinked.
                "op": "update",
                "table": "Wifi_VIF_Config",
                "where": where_uuid(vif_uuid),
                "row": {
                    "enabled": True,
                    "ssid": "",
                    "security": ["map", []],
                    "multi_ap": ["set", []],
                    "wds": ["set", []],
                    "credential_configs": ["set", [["named-uuid", "backhaul"]]],
                },
            },
        ]
        counts = [None, None, None, 1]
        self.write_count += 1
        try:
            results = await self.session.transact(
                transaction, attempt["transaction_id"], attempt["session_generation"]
            )
            check_results(results, counts)
        except (ConnectionError, TimeoutError):
            return SubmitResult("unknown", {"attribution": "unknown"}, Reason.OUTCOME_UNKNOWN)
        except EmosaError as exc:
            if exc.code == Reason.PRECONDITION_FAILED:
                return SubmitResult("conflict", {}, exc.code)
            return SubmitResult(
                "unknown" if exc.code == Reason.OUTCOME_UNKNOWN else "rejected", {}, exc.code
            )
        return SubmitResult(
            "committed",
            {
                "attribution": "reply",
                "transaction_validated": True,
                "transaction_id": attempt["transaction_id"],
                "session_generation": raw["generation"],
                "action": "multi-ap-uplink",
                "instance": self.instance,
                "results": json.loads(json.dumps(results, default=str)),
            },
        )

    async def close(self):
        pass
