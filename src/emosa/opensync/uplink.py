# SPDX-License-Identifier: Apache-2.0
"""The pod's uplink as an EMOSA scope: switch a backhaul station to EasyMesh (option 1).

Data plane option 1 (docs/concepts/data-plane.md §5): the pod's own backhaul
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
# A child of a GRE parent (spec 8.6) joins its parent AP as a 3-address station
# (onboard_type=gre, no wds), and cm builds the gretap: cm's uplink is then that
# gretap (if_type gre), which uplink_state reports as this kind. The credential is
# the same backhaul SSID/PSK as a Multi-AP join; only onboard_type and the applied
# State differ. One uplink scope carries both, by the move's mode.
GRE = "gre"
# The operator's "back to bootstrap" (spec 8.6): not a pin but a revert. EMOSA undoes only its own
# writes - it restores the station's snapshotted vconf columns (ssid, security, credential_configs,
# and multi_ap/wds if a Multi-AP move touched them) and deletes the credential it inserted - and
# leaves the pod's parent pin, so the station lands back on the bootstrap BSS the pod owns.
BOOTSTRAP = "bootstrap"
ONBOARD = {MULTI_AP: "multi_ap", GRE: "gre"}
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
    # the revert's snapshot (BOOTSTRAP mode): the station's pre-move vconf columns to put back
    restore: dict | None = None

    def record(self):
        data = asdict(self)
        if data.get("restore") is None:
            del data["restore"]  # a pin carries no snapshot; keep its record as it was
        return data

    def validate(self):
        if self.mode == BOOTSTRAP:
            # a revert to the bootstrap: no credential is written, so no SSID/secret; it carries
            # the bootstrap BSSID (what the station lands on) and the snapshot to restore
            if self.bssid is None or not BSSID.match(self.bssid):
                raise EmosaError(Reason.INVALID_INPUT, "a revert requires the bootstrap BSSID")
            if not self.restore or not self.restore.get("ssid"):
                raise EmosaError(Reason.INVALID_INPUT, "a revert requires the vconf snapshot")
            if not isinstance(self.station, str) or not self.station:
                raise EmosaError(Reason.INVALID_INPUT, "explicit station required")
            return
        if not 1 <= len(self.ssid.encode("utf-8")) <= 32 or "\x00" in self.ssid:
            raise EmosaError(Reason.INVALID_INPUT, "SSID must contain 1–32 UTF-8 bytes, no NUL")
        if self.mode not in (MULTI_AP, GRE):
            raise EmosaError(
                Reason.UNSUPPORTED_OPERATION, "only the Multi-AP or GRE-parent backhaul uplink"
            )
        if self.mode == GRE and self.bssid is None:
            # a GRE child joins by its pinned BSSID only (spec 8.6); never by SSID
            raise EmosaError(Reason.INVALID_INPUT, "a GRE uplink requires the parent AP's BSSID")
        if any(
            not isinstance(x, str) or not x for x in (self.pod_id, self.station, self.secret_ref)
        ):
            raise EmosaError(Reason.INVALID_INPUT, "explicit station and secret reference required")
        if self.bssid is not None and not BSSID.match(self.bssid):
            raise EmosaError(Reason.INVALID_INPUT, "BSSID must be a lower-case MAC address")

    def onboard_type(self):
        return ONBOARD[self.mode]

    def target(self, vault):
        self.validate()
        if self.mode == BOOTSTRAP:
            # the revert is confirmed by observation (the station back on the bootstrap BSS in
            # vconf mode), not by a credential fingerprint EMOSA cannot know (the pod's own PSK)
            return {"uplink": BOOTSTRAP, "station": self.station, "bssid": self.bssid}
        target = {
            "uplink": self.mode,
            "station": self.station,
            "ssid": self.ssid,
            "credential_fingerprint": vault.fingerprint(vault.resolve(self.secret_ref)),
        }
        if self.bssid is not None:
            target["bssid"] = self.bssid
        return target


def lower_mac(value):
    return value.lower() if isinstance(value, str) and value else None


def credential_uuids(configs):
    """The credential UUIDs of a ``Wifi_VIF_Config.credential_configs`` wire value.

    OVSDB renders a set of one as the bare reference ``["uuid", u]`` and a larger set as
    ``["set", [["uuid", u], ...]]``; accept both, and an empty or absent value as none."""
    if not isinstance(configs, list) or not configs:
        return []
    if configs[0] == "uuid":
        return [configs[1]]
    if configs[0] == "set":
        return [u for kind, u in configs[1] if kind == "uuid"]
    return []


def band_of_operating_class(operating_class):
    """The band of an IEEE 802.11 Annex E global operating class, or None."""
    if 81 <= operating_class <= 84:
        return "2.4G"
    if 115 <= operating_class <= 130:
        return "5G"
    if 131 <= operating_class <= 137:
        return "6G"
    return None


def own_bssids(decoded):
    """Every MAC address the pod's own VIFs use: none of them may be its upstream."""
    return {
        lower_mac(r.get("mac"))
        for table in ("Wifi_VIF_State", "Wifi_Radio_State")
        for r in decoded.get(table, {}).values()
        if lower_mac(r.get("mac"))
    }


def start_instance(decoded):
    """Which start of the pod's OpenSync: a hash of its radio rows' UUIDs.

    Its start scripts create the radio rows anew (new UUIDs), which then live as
    long as that start does. AWLAN_Node comes from the database template and
    keeps its UUID (seen live), so it cannot tell two starts apart.
    """
    radios = sorted(decoded.get("Wifi_Radio_Config", {}))
    if not radios:
        raise EmosaError(Reason.NOT_READY, "the pod's radios are not configured yet")
    return hashlib.sha256(json.dumps(radios).encode()).hexdigest()[:16]


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
    """The uplink scope of one pod: its backhaul stations, bound by serial.

    ``station`` is the one the pod bootstraps on; ``stations`` its backhaul station on each
    band ((band, if_name) pairs, the profile's), which a move to a BSS on that band uses
    (spec 8.3). A switch enables the station it names and disables the others, by updates
    only: a band whose station the pod has no row for takes no switch. ``fixed_band`` is the
    band of the radio that carries the pod's BSSes: a station on it stays on that radio's
    channel, which the controller sets for the BSSes, not a move.
    """

    mode = MODE

    def __init__(
        self, pod_id, session, vault, *, serial, station, stations=(), fixed_band=None, loop=None
    ):
        self.pod_id, self.session, self.vault = pod_id, session, vault
        self.expected_serial, self.station = serial, station
        self.stations = dict(stations)
        self.bound = set(self.stations.values()) | {station}
        self.fixed_band = fixed_band
        self.present = set()  # the bound stations the pod has a row for, at the last read
        self.radios = {}  # bound station -> its radio's (freq_band, channel), at the last read
        self.active = station  # the station the uplink is on, at the last read
        # spec 8.5: (upstream BSSID, the pod's own MACs) -> True when that upstream is a pod
        # whose own upstream chain reaches this one
        self.loop = loop
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
        self.instance = start_instance(decoded)
        rows = {}  # bound station -> (its Wifi_VIF_Config UUID, row)
        for u, r in decoded.get("Wifi_VIF_Config", {}).items():
            name = r.get("if_name")
            if name not in self.bound:
                continue
            if name in rows:
                raise EmosaError(Reason.NOT_READY, "backhaul station ambiguous")
            if r.get("mode") != "sta":
                raise EmosaError(
                    Reason.UNSUPPORTED_OPERATION, "the bound uplink VIF is not a station"
                )
            rows[name] = (u, r)
        self.present = set(rows)
        # each station's radio: its band, and the channel it operates on (its State's)
        channels = {
            r.get("if_name"): r.get("channel") for r in decoded.get("Wifi_Radio_State", {}).values()
        }
        self.radios = {
            name: (radio.get("freq_band"), channels.get(radio.get("if_name")))
            for radio in decoded.get("Wifi_Radio_Config", {}).values()
            for name, (u, _) in rows.items()
            if u in (radio.get("vif_configs") or [])
        }
        return decoded, rows

    def station_for(self, band):
        """The pod's backhaul station on ``band`` it has a row for, at the last read, or None."""
        station = self.stations.get(band)
        return station if station in self.present else None

    def fixed_channel(self, station):
        """The channel ``station`` cannot leave: its radio's, when that radio carries the
        pod's BSSes (``fixed_band``); None for a station on a radio of its own."""
        band, channel = self.radios.get(station, (None, None))
        if band is not None and band == self.fixed_band and type(channel) is int:
            return channel
        return None

    def _active(self, decoded, rows):
        """The station the uplink is on: the one in Multi-AP credential-list mode, else the
        one ``cm`` uses, else the one the pod bootstraps on."""
        written = [n for n, (_, r) in rows.items() if self._configured(decoded, r, n)[1]]
        if len(written) == 1:
            return written[0]
        used = [
            r.get("if_name")
            for r in decoded.get("Connection_Manager_Uplink", {}).values()
            if r.get("is_used") is True and r.get("if_name") in rows
        ]
        return used[0] if len(used) == 1 else self.station

    def _configured(self, decoded, vif, station):
        """The station's configured uplink: credential-list mode (Multi-AP or GRE), or not.

        Both a Multi-AP (``onboard_type=multi_ap``, 4-address) and a GRE-parent child
        (``onboard_type=gre``, 3-address, spec 8.6) station are one linked credential in
        credential-list mode (the VIF's own ``ssid`` empty)."""
        creds = decoded.get("Wifi_Credential_Config", {})
        linked = [creds[u] for u in vif.get("credential_configs") or [] if u in creds]
        sole = linked[0] if len(linked) == 1 else {}
        key = credential_key(sole)
        listed = (
            vif.get("enabled") is True
            and not vif.get("ssid")
            and sole.get("enabled") is True
            and key is not None
        )
        kind = {"multi_ap": MULTI_AP, "gre": GRE}.get(sole.get("onboard_type")) if listed else None
        return {
            "uplink": kind or "other",
            "station": station,
            "ssid": sole.get("ssid") if kind else vif.get("ssid"),
            "credential_fingerprint": self.vault.fingerprint(key) if kind else None,
            "bssid": lower_mac(sole.get("bssid")) if kind else None,
        }, (sole if kind else None)

    async def snapshot(self):
        try:
            raw = await self.session.snapshot()
            decoded, rows = self._binding(raw)
            self.active = self._active(decoded, rows)
            vif_uuid, vif = rows.get(self.active, (None, {}))
            configured, credential = self._configured(decoded, vif, self.active)
            state = uplink_state(decoded, self.active)
            self.facts = {k: v for k, v in state.items() if k != "state"}
            # The key a station connected with is not in its State: the observed
            # credential is the configured one whose SSID the station is on.
            observed = {
                "uplink": state["kind"],
                "station": self.active,
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
                    self.active,
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
        if intent.pod_id != self.pod_id or intent.station not in self.bound:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "request exceeds the bound stations")
        if intent.bssid is None:
            raise EmosaError(Reason.INVALID_INPUT, "the upstream BSSID is required")
        if decoded is not None and intent.bssid in own_bssids(decoded):
            # joining itself would bridge the pod's backhaul BSS into its own br-home
            raise EmosaError(Reason.INVALID_INPUT, "the upstream BSSID is one of the pod's own")
        if (
            decoded is not None
            and self.loop is not None
            and self.loop(intent.bssid, own_bssids(decoded))
        ):
            # a pod downstream of this one: its br-home would be bridged into its own
            raise EmosaError(
                Reason.INVALID_INPUT, "the upstream BSSID is a pod downstream of this one"
            )

    @staticmethod
    def _others_on(rows, station):
        """The pod's other backhaul stations that are enabled: the switch disables them."""
        return sorted(n for n, (_, r) in rows.items() if n != station and r.get("enabled") is True)

    async def plan(self, intent):
        self._check(intent)
        raw = await self.session.snapshot()
        decoded, rows = self._binding(raw)
        self._check(intent, decoded)
        if not raw["ready"] or intent.station not in rows:
            raise EmosaError(Reason.NOT_READY, "the pod's backhaul station row is required")
        state = uplink_state(decoded, self._active(decoded, rows))
        if state["kind"] is None:
            # Start only from a working uplink: the switch moves the path EMOSA
            # itself uses, and OpenSync's restart returns the pod to this one.
            raise EmosaError(Reason.NOT_READY, "no working uplink to switch from")
        return {
            "mapping": MODE,
            "action": "multi-ap-uplink",
            "station": intent.station,
            "ssid": intent.ssid,
            "bssid": intent.bssid,
            "secret_ref": intent.secret_ref,
            "from": {"kind": state["kind"], "in_use": state["in_use"]},
            "disables": self._others_on(rows, intent.station),
            "instance": self.instance,
            "fields": [
                "Wifi_Credential_Config",
                "Wifi_VIF_Config.credential_configs/ssid",
                "Wifi_VIF_Config.enabled of the other backhaul stations",
            ],
            "guard": "pod serial and the stations' current rows",
        }

    async def submit(self, intent, attempt):
        await self.plan(intent)
        raw = await self.session.snapshot()
        _, rows = self._binding(raw)
        if raw["generation"] != attempt["session_generation"] or intent.station not in rows:
            return SubmitResult("rejected", {}, Reason.NOT_READY)
        node_uuid = next(iter(raw["tables"]["AWLAN_Node"]))
        vif_uuid = rows[intent.station][0]
        vif = raw["tables"]["Wifi_VIF_Config"][vif_uuid]
        if intent.mode == BOOTSTRAP:
            return await self._submit_revert(intent, attempt, raw, node_uuid, vif_uuid, vif)
        key = self.vault.resolve(intent.secret_ref)
        # Credential-list mode: the station's own SSID and security empty, one linked credential.
        # ssid "" is the marker that the station is under an EMOSA move (spec 8.6 ownership). On
        # onboard_type=multi_ap owm sets multi_ap, 4-address and br-home itself; on onboard_type=gre
        # (a GRE parent's child, spec 8.6) it is a 3-address station and cm builds the gretap. The
        # pod's bootstrap credential stays, unlinked, and its parent pin stands (ignored in this
        # mode). A GRE move leaves multi_ap/wds as the pod set them (none/false), within EMOSA's
        # columns; only a Multi-AP move clears them so owm can set backhaul_sta and wds.
        vif_row = {
            "enabled": True,
            "ssid": "",
            "security": ["map", []],
            "credential_configs": ["set", [["named-uuid", "backhaul"]]],
        }
        if intent.mode == MULTI_AP:
            vif_row["multi_ap"] = ["set", []]
            vif_row["wds"] = ["set", []]
        # Snapshot the pod columns this move overwrites, for the revert to put back (vconf mode),
        # but only when moving FROM the bootstrap (the pre-move station is in vconf mode, ssid set):
        # a move from one parent to another leaves the earlier snapshot in place, so the revert
        # always restores the bootstrap, never an EMOSA move state.
        restore = None
        if vif.get("ssid"):
            restore = {k: vif.get(k) for k in vif_row if k != "enabled" and k in vif}
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
                    "onboard_type": intent.onboard_type(),
                    "priority": 1,
                    "enabled": True,
                    "bssid": intent.bssid,
                },
            },
            {
                "op": "update",
                "table": "Wifi_VIF_Config",
                "where": where_uuid(vif_uuid),
                "row": vif_row,
            },
        ]
        counts = [None, None, None, 1]
        # Only a Multi-AP move disables the pod's other backhaul stations: two 4-address uplinks
        # bridge br-home into the network twice, with no STP (spec 8.3). A GRE move disables none
        # (cm bridges only the in-use uplink's gretap), so the pod's fallback station stays the
        # pod's (spec 8.6 ownership).
        others = self._others_on(rows, intent.station) if intent.mode == MULTI_AP else []
        for name in others:
            other_uuid = rows[name][0]
            other = raw["tables"]["Wifi_VIF_Config"][other_uuid]
            transaction += [
                guard(
                    "Wifi_VIF_Config", other_uuid, other, [c for c in STATION_GUARDS if c in other]
                ),
                {
                    "op": "update",
                    "table": "Wifi_VIF_Config",
                    "where": where_uuid(other_uuid),
                    "row": {"enabled": False},
                },
            ]
            counts += [None, 1]
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
                "restore": restore,
            },
        )

    async def _submit_revert(self, intent, attempt, raw, node_uuid, vif_uuid, vif):
        """The revert (spec 8.6): put the station's snapshotted vconf columns back and delete the
        credential EMOSA inserted, within EMOSA's own columns. The pod's parent pin stands (vconf
        mode honours it), so the station lands on the bootstrap BSS; the pod's bootstrap credential,
        still present, is re-linked by the restored credential_configs. The one credential EMOSA
        deletes is the one linked now that the snapshot does not keep."""
        keep = set(credential_uuids(intent.restore.get("credential_configs")))
        drop = [u for u in credential_uuids(vif.get("credential_configs")) if u not in keep]
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
                "op": "update",
                "table": "Wifi_VIF_Config",
                "where": where_uuid(vif_uuid),
                "row": dict(intent.restore),
            },
        ]
        counts = [None, None, 1]
        # The update unlinks EMOSA's credential (credential_configs back to the pod's), so it is
        # unreferenced in the transaction's final state and the delete succeeds.
        for uuid in drop:
            transaction.append(
                {"op": "delete", "table": "Wifi_Credential_Config", "where": where_uuid(uuid)}
            )
            counts.append(1)
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
                "action": "revert",
                "instance": self.instance,
                "results": json.loads(json.dumps(results, default=str)),
            },
        )

    async def close(self):
        pass
