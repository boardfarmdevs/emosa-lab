"""Pod profiles: how one kind of OpenSync pod lays out what EMOSA manages.

A profile is data (``src/emosa/profiles/*.json``, schema
``schemas/pod-profile.schema.json``): the radio band and channel a cold pod's
BSS is created on, the fronthaul VIF and its row, the backhaul overrides, the
platform's extra VIF slots for multi-BSS, the Inet row of a created VIF, and
the backhaul station data plane option 1 moves onto the EasyMesh backhaul.
A new pod model needs a new profile, not new code.
"""

import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from emosa.config import validate

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

    @property
    def backhaul_row(self):
        return {**self.fronthaul_vif, **self.backhaul_vif}


def load(ref=DEFAULT):
    """A bundled profile by ID, or a profile file by path."""
    if ref.endswith(".json") or "/" in ref:
        text = Path(ref).read_text()
    else:
        text = files("emosa").joinpath(f"profiles/{ref}.json").read_text()
    data = json.loads(text)
    validate("pod-profile", data)
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
    )
