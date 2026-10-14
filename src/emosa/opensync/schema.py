# SPDX-License-Identifier: Apache-2.0
import copy
import hashlib
import json
from importlib.resources import files
from pathlib import Path

from ovs.db.data import Datum
from ovs.db.error import Error as OvsError
from ovs.db.schema import DbSchema

from emosa.errors import EmosaError, Reason

SCHEMA_SHA256 = "91d18cc4668ff6fc9c9be2234ddc7e70c84b9640f04e621a1ff90e3451c867a9"
TABLES = {
    "AWLAN_Node": ["serial_number", "model", "firmware_version"],
    "Wifi_Radio_Config": ["if_name", "freq_band", "enabled", "vif_configs"],
    "Wifi_Radio_State": [
        "if_name",
        "radio_config",
        "vif_states",
        "freq_band",
        "channel",
        "mac",
        "enabled",
        "country",
    ],
    "Wifi_VIF_Config": [
        "if_name",
        "mode",
        "ssid",
        "enabled",
        "wpa",
        "wpa_key_mgmt",
        "wpa_psks",
        "security",
        "rsn_pairwise_ccmp",
        "wpa_pairwise_tkip",
        "wpa_pairwise_ccmp",
        "wpa_oftags",
        "bridge",
        "multi_ap",
    ],
    "Wifi_VIF_State": [
        "if_name",
        "vif_config",
        "mode",
        "ssid",
        "enabled",
        "wpa",
        "wpa_key_mgmt",
        "wpa_psks",
        "security",
        "rsn_pairwise_ccmp",
        "wpa_pairwise_tkip",
        "wpa_pairwise_ccmp",
        "mac",
        "associated_clients",
        "multi_ap",
    ],
    "Wifi_Associated_Clients": ["mac", "state"],
}

# The pod's database generation (spec 3.10), from its profile: the reference it qualifies against,
# that file's hash, and the columns of the reference the generation's pods lack.
OPENSYNC_66, LEGACY = "opensync-6.6", "opensync-2.0"
LEGACY_SCHEMA_SHA256 = "0d2dd69e44dfe0d4561613625665041ab82beeaff6a4cbe1dfe73bd676475f21"
GENERATIONS = {
    OPENSYNC_66: ("opensync.ovsschema", SCHEMA_SHA256, {}),
    LEGACY: (
        "opensync-2.0.0.ovsschema",
        LEGACY_SCHEMA_SHA256,
        {"Wifi_VIF_Config": ("multi_ap",), "Wifi_VIF_State": ("multi_ap",)},
    ),
}
# the columns a legacy pod has not: its WPA2-PSK is the security map, its roles the profile's
WPA_COLUMNS = (
    "wpa",
    "wpa_key_mgmt",
    "wpa_psks",
    "rsn_pairwise_ccmp",
    "wpa_pairwise_tkip",
    "wpa_pairwise_ccmp",
    "wpa_oftags",
    "multi_ap",
)
# the values EMOSA writes in an enum column it requires: a pod's narrower enum keeps them
WRITTEN = {("Wifi_VIF_Config", "mode"): {"ap", "sta"}}


def reference_path(generation=OPENSYNC_66) -> Path:
    name = GENERATIONS[generation][0]
    local = Path(__file__).resolve().parents[3] / "tests/fixtures/opensync" / name
    return local if local.exists() else Path(str(files("emosa").joinpath(f"data/{name}")))


def reference_schema(generation=OPENSYNC_66):
    """The generation's reference as its pods have it: the pinned file without the columns they
    lack (the legacy pod's multi_ap)."""
    raw = json.loads(reference_path(generation).read_text())
    for table, columns in GENERATIONS[generation][2].items():
        for column in columns:
            del raw["tables"][table]["columns"][column]
    return raw


def required_tables(generation=OPENSYNC_66):
    """The columns EMOSA uses on a pod of the generation (spec 3.2, 3.10)."""
    if generation == LEGACY:
        return {t: [c for c in cols if c not in WPA_COLUMNS] for t, cols in TABLES.items()}
    return TABLES


def _enum_values(enum):
    return set(enum[1]) if isinstance(enum, list) and enum[:1] == ["set"] else {enum}


def narrowed(table, column, actual, reference):
    """True when the pod's type is the reference's with fewer enum values, keeping every value
    EMOSA writes there (a pod without ap_vlan)."""
    a, r = actual.to_json(), reference.to_json()
    if not (isinstance(a, dict) and isinstance(r, dict)):
        return False
    changed = False
    for part in ("key", "value"):
        pa, pr = a.get(part), r.get(part)
        if not (isinstance(pa, dict) and isinstance(pr, dict) and "enum" in pa and "enum" in pr):
            continue
        values, allowed = _enum_values(pa["enum"]), _enum_values(pr["enum"])
        if not values <= allowed or not WRITTEN.get((table, column), set()) <= values:
            return False
        changed |= values != allowed
        a[part] = {k: v for k, v in pa.items() if k != "enum"}
        r[part] = {k: v for k, v in pr.items() if k != "enum"}
    return changed and a == r


def canonical_hash(schema):
    return hashlib.sha256(
        json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Schema:
    def __init__(self, raw):
        try:
            self.raw = copy.deepcopy(raw)
            self.fingerprint = canonical_hash(raw)
            self.db = DbSchema.from_json(copy.deepcopy(raw))
        except (OvsError, TypeError, KeyError, ValueError) as exc:
            raise EmosaError(Reason.SCHEMA_MISMATCH, "invalid OVSDB schema") from exc

    def qualify_synthetic(self, *, tables=None, generation=OPENSYNC_66):
        if generation not in GENERATIONS:
            raise EmosaError(Reason.INVALID_INPUT, "unknown schema generation")
        reference = reference_path(generation).read_bytes()
        if hashlib.sha256(reference).hexdigest() != GENERATIONS[generation][1]:
            raise EmosaError(Reason.SCHEMA_MISMATCH, "pinned schema artifact hash mismatch")
        expected = DbSchema.from_json(reference_schema(generation))
        # the generations do not mix (spec 3.10): a pod with wpa_psks is no legacy pod, and one
        # without it no 6.6 pod
        vif = self.db.tables.get("Wifi_VIF_Config")
        if (vif is not None and "wpa_psks" not in vif.columns) != (generation == LEGACY):
            raise EmosaError(
                Reason.SCHEMA_MISMATCH,
                "the pod's schema is not the profile's generation",
                generation=generation,
            )
        # The server adds default isRoot fields and removes schema checksums.
        for table, columns in (required_tables(generation) | (tables or {})).items():
            for column in columns:
                try:
                    actual = self.db.tables[table].columns[column].type
                    ref = expected.tables[table].columns[column].type
                except KeyError as exc:
                    raise EmosaError(
                        Reason.SCHEMA_MISMATCH, "required column absent", table=table, column=column
                    ) from exc
                if actual != ref and not narrowed(table, column, actual, ref):
                    raise EmosaError(
                        Reason.SCHEMA_MISMATCH,
                        "required column type changed",
                        table=table,
                        column=column,
                    )

    def decode(self, table, column, value):
        if column in {"_uuid", "_version"}:
            return value[1]
        type_ = self.db.tables[table].columns[column].type
        datum = Datum.from_json(type_, value)
        if type_.is_map():
            return {str(k.value): v.value for k, v in datum.values.items()}
        values = [str(k.value) if type_.key.type.name == "uuid" else k.value for k in datum.values]
        if type_.n_max == 1:
            return values[0] if values else None
        return sorted(values)

    def row(self, table, raw):
        return {k: self.decode(table, k, v) for k, v in raw.items()}
