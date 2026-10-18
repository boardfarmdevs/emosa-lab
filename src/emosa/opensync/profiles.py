# SPDX-License-Identifier: Apache-2.0
"""Pod profiles: how one kind of OpenSync pod lays out what EMOSA manages.

A profile is data (``src/emosa/profiles/*.json``, schema
``schemas/pod-profile.schema.json``): the radio band and channel a cold pod's
BSS is created on, the fronthaul VIF and its row, the backhaul overrides, the
platform's extra VIF slots for multi-BSS, the Inet row of a created VIF, and
the backhaul station data plane option 1 moves onto the EasyMesh backhaul.
A new pod model needs a new profile, not new code.
"""

import hashlib
import json
import os
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from emosa.config import validate
from emosa.errors import EmosaError, Reason

DEFAULT = "opensync-lab-hwsim-6.6.1-v1"


@dataclass(frozen=True)
class PodProfile:
    id: str
    band: str
    channel: int
    ht_mode: str
    chipset: str
    fronthaul_if: str
    fronthaul_vif: dict
    backhaul_vif: dict
    extra_slots: tuple  # (if_name, role, vif_radio_idx), in assignment order
    inet: dict
    uplink_station: str | None = None  # the backhaul station option 1 moves (bootstrap-created)
    esp_be: bytes | None = None  # declared best-effort ESP for AP metrics (spec §3.8)
    # the backhaul station on each band ("2.4G", "5G", "6G"), for a move to another band (§8.3)
    uplink_stations: tuple = ()  # ((band, if_name), ...)
    # how a backhaul BSS of the M2 set is carried: "multi-ap" (§8.5) or "gre-parent" (§8.6),
    # and the parent AP's overrides of its VIF and Inet rows
    backhaul_mode: str = "multi-ap"
    gre_parent_vif: dict | None = None
    gre_parent_inet: dict | None = None
    # additional radios EMOSA binds and reports beyond the fronthaul's radio (spec 9, the second
    # radio of 8.6): ({"band", "ht_mode"?, "chipset"}, ...). EMOSA writes no channel/ht_mode on
    # them (they follow the station) and reports the observed channel.
    radios: tuple = ()
    # the radio each extra slot's VIF is on, when not the fronthaul's `radio` (spec 9):
    # ((if_name, band), ...). A parent AP b-ap-50 on the second radio is ("b-ap-50", "5G").
    slot_bands: tuple = ()
    # the pod's database generation (spec 3.10): "opensync-6.6", or "opensync-2.0", a legacy pod
    # with its WPA2-PSK in the security map and no multi_ap
    schema: str = "opensync-6.6"
    # a legacy pod's WAN bridge, the one its cm puts the uplink port in (spec 8.4)
    wan_bridge: str = "br-wan"
    # a legacy pod's radio plan (spec 3.10): "pod", left as its seed brings it back, or "emosa",
    # the APs the profile does not name disabled and the fronthaul radio on the profile's channel
    radio_plan: str = "pod"
    # with radio_plan "emosa": the radios the profile neither binds nor places, each with the
    # channel and width the plan turns it on at when it carries an enabled AP but cannot beacon
    # ({"band", "channel", "ht_mode"}, ...)
    idle_radios: tuple = ()
    # the first 12 hex of the profile file's SHA-256, its bytes as read ("" for one built in code)
    fingerprint: str = ""

    @property
    def mapping(self):
        """The mapping the AP and radio scopes plan under (spec §5): the profile, by its ID and
        its file's content, so an image whose profile changed is another mapping (no ownership
        conflict over what an earlier one wrote, a new ownership period)."""
        return f"{self.id}@{self.fingerprint}" if self.fingerprint else self.id

    @property
    def backhaul_row(self):
        return {**self.fronthaul_vif, **self.backhaul_vif}

    @property
    def legacy(self):
        return self.schema == "opensync-2.0"


def load(ref=DEFAULT):
    """A profile by ID, from the directory EMOSA_PROFILES names when set (as the C agent reads
    them) else the bundled ones, or a profile file by path."""
    if ref.endswith(".json") or "/" in ref:
        raw = Path(ref).read_bytes()
    elif os.environ.get("EMOSA_PROFILES"):
        raw = (Path(os.environ["EMOSA_PROFILES"]) / f"{ref}.json").read_bytes()
    else:
        raw = files("emosa").joinpath(f"profiles/{ref}.json").read_bytes()
    data = json.loads(raw.decode())
    validate("pod-profile", data)
    # a slot is on the fronthaul's radio or on one of the profile's further radios (spec 9),
    # whose scope writes it; a slot on a radio the profile does not bind would never be served
    bands = [r["band"] for r in data.get("radios", ())]
    if len(set(bands)) != len(bands) or data["radio"]["band"] in bands:
        raise EmosaError(Reason.INVALID_INPUT, f"pod profile {data['id']}: one radio per band")
    for slot in data["extra_slots"]:
        if slot.get("band", data["radio"]["band"]) not in (data["radio"]["band"], *bands):
            raise EmosaError(
                Reason.INVALID_INPUT, f"pod profile {data['id']}: {slot['if_name']} on no radio"
            )
    # a radio the profile places has a channel EMOSA maps, else it would never be reported (spec 9)
    from emosa.opensync.easymesh_view import operating_class

    for radio in data.get("radios", ()):
        if "channel" in radio and operating_class(radio["band"], radio["channel"]) is None:
            raise EmosaError(
                Reason.INVALID_INPUT, f"pod profile {data['id']}: {radio['band']} channel unmapped"
            )
    # a legacy pod has no Multi-AP backhaul BSS: its backhaul slots are GRE parent APs (spec 3.10)
    backhaul_slots = any(s["role"] == "backhaul" for s in data["extra_slots"])
    if (
        data.get("schema") == "opensync-2.0"
        and backhaul_slots
        and data.get("backhaul_mode") != "gre-parent"
    ):
        raise EmosaError(
            Reason.INVALID_INPUT, f"pod profile {data['id']}: a legacy pod's backhaul is gre-parent"
        )
    if data.get("radio_plan", "pod") != "pod" and data.get("schema") != "opensync-2.0":
        # a 6.6 pod's seed is its own bootstrap's, which EMOSA does not take over
        raise EmosaError(
            Reason.INVALID_INPUT, f"pod profile {data['id']}: radio_plan is a legacy pod's"
        )
    # an idle radio is one the profile neither binds nor places, once, on a channel EMOSA maps;
    # only the radio plan turns it on
    idle = [r["band"] for r in data.get("idle_radios", ())]
    if idle and data.get("radio_plan", "pod") != "emosa":
        raise EmosaError(
            Reason.INVALID_INPUT, f"pod profile {data['id']}: idle_radios need radio_plan emosa"
        )
    if len(set(idle)) != len(idle) or set(idle) & {data["radio"]["band"], *bands}:
        raise EmosaError(
            Reason.INVALID_INPUT, f"pod profile {data['id']}: an idle radio is a radio not bound"
        )
    for radio in data.get("idle_radios", ()):
        if operating_class(radio["band"], radio["channel"]) is None:
            raise EmosaError(
                Reason.INVALID_INPUT,
                f"pod profile {data['id']}: idle {radio['band']} channel unmapped",
            )
    return PodProfile(
        data["id"],
        data["radio"]["band"],
        data["radio"]["channel"],
        data["radio"]["ht_mode"],
        data["radio"]["chipset"],
        data["fronthaul"]["if_name"],
        data["fronthaul"]["vif"],
        data["backhaul_vif"],
        tuple((s["if_name"], s["role"], s["vif_radio_idx"]) for s in data["extra_slots"]),
        data["inet"],
        data.get("uplink", {}).get("station"),
        bytes.fromhex(data["ap_metrics"]["esp_be"]) if "ap_metrics" in data else None,
        tuple(sorted(data.get("uplink", {}).get("stations", {}).items())),
        data.get("backhaul_mode", "multi-ap"),
        data.get("gre_parent", {}).get("vif", {}),
        data.get("gre_parent", {}).get("inet", {}),
        tuple(data.get("radios", ())),
        tuple((s["if_name"], s["band"]) for s in data["extra_slots"] if "band" in s),
        data.get("schema", "opensync-6.6"),
        data.get("wan_bridge", "br-wan"),
        data.get("radio_plan", "pod"),
        tuple(dict(r) for r in data.get("idle_radios", ())),
        hashlib.sha256(raw).hexdigest()[:12],
    )
