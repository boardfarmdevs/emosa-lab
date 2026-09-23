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
    ],
    "Wifi_Associated_Clients": ["mac", "state"],
}


def reference_path() -> Path:
    local = Path(__file__).resolve().parents[3] / "tests/fixtures/opensync/opensync.ovsschema"
    return (
        local if local.exists() else Path(str(files("emosa").joinpath("data/opensync.ovsschema")))
    )


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

    def qualify_synthetic(self, *, tables=None):
        reference = reference_path().read_bytes()
        if hashlib.sha256(reference).hexdigest() != SCHEMA_SHA256:
            raise EmosaError(Reason.SCHEMA_MISMATCH, "pinned schema artifact hash mismatch")
        expected = DbSchema.from_json(json.loads(reference))
        # The server adds default isRoot fields and removes schema checksums.
        for table, columns in (TABLES | (tables or {})).items():
            for column in columns:
                try:
                    actual = self.db.tables[table].columns[column].type
                    ref = expected.tables[table].columns[column].type
                except KeyError as exc:
                    raise EmosaError(
                        Reason.SCHEMA_MISMATCH, "required column absent", table=table, column=column
                    ) from exc
                if actual != ref:
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
