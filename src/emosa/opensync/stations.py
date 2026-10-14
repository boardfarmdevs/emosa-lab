# SPDX-License-Identifier: Apache-2.0
"""Deleting a legacy pod's station rows (spec 3.10): the radio plan's seed stations, and the
station guard's unassociated ones.

A station row goes with what only it uses: its radio's ``vif_configs`` reference, the
credentials no other VIF links and its Inet row (when no other VIF has its name). Each row is
``wait``ed on as it is, so a station another writer changed meanwhile fails the transaction.
"""

from emosa.opensync.mapping import where_uuid


def optional(value):
    """An OVSDB optional's value in a wait row: the value, or the empty set."""
    return ["set", []] if value is None else value


def deletion(decoded, uuids):
    """The operations deleting the station rows ``uuids`` of the decoded pod, and the counts
    each must match: ([operation], [count or None])."""
    vifs = decoded.get("Wifi_VIF_Config", {})
    gone = {u for u in uuids if u in vifs}
    kept = [r for u, r in vifs.items() if u not in gone]
    kept_credentials = {c for r in kept for c in r.get("credential_configs") or []}
    kept_names = {r.get("if_name") for r in kept}
    names = {vifs[u].get("if_name") for u in gone}
    credentials = sorted(
        {
            c
            for u in gone
            for c in vifs[u].get("credential_configs") or []
            if c in decoded.get("Wifi_Credential_Config", {}) and c not in kept_credentials
        }
    )
    inets = sorted(
        u
        for u, r in decoded.get("Wifi_Inet_Config", {}).items()
        if r.get("if_name") in names - kept_names
    )
    radios = sorted(
        (u, v)
        for u, r in decoded.get("Wifi_Radio_Config", {}).items()
        for v in r.get("vif_configs") or []
        if v in gone
    )
    operations, counts = [], []
    for uuid in sorted(gone, key=lambda u: (vifs[u].get("if_name") or "", u)):
        row = vifs[uuid]
        operations.append(
            {
                "op": "wait",
                "table": "Wifi_VIF_Config",
                "where": where_uuid(uuid),
                "columns": ["if_name", "mode", "ssid"],
                "until": "==",
                "rows": [
                    {
                        "if_name": row.get("if_name"),
                        "mode": "sta",
                        "ssid": optional(row.get("ssid")),  # a credential list: none
                    }
                ],
                "timeout": 0,
            }
        )
        counts.append(None)
    for radio, uuid in radios:
        operations.append(
            {
                "op": "mutate",
                "table": "Wifi_Radio_Config",
                "where": where_uuid(radio),
                "mutations": [["vif_configs", "delete", ["set", [["uuid", uuid]]]]],
            }
        )
        counts.append(1)
    for table, rows in (
        ("Wifi_VIF_Config", sorted(gone, key=lambda u: (vifs[u].get("if_name") or "", u))),
        ("Wifi_Credential_Config", credentials),
        ("Wifi_Inet_Config", inets),
    ):
        for uuid in rows:
            operations.append({"op": "delete", "table": table, "where": where_uuid(uuid)})
            counts.append(1)
    return operations, counts
