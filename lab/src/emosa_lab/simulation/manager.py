"""Independent simulated manager process, explicitly triggered at device boundaries.

Observes Config via real OVSDB. Only this test process publishes synthetic State.
It imports no semantic adapter, expected-state predicate or lifecycle code.
"""

import asyncio
import json
import sys

from emosa.opensync.session import OvsSession

APPLY_COLUMNS = [
    "ssid",
    "enabled",
    "mode",
    "wpa",
    "wpa_key_mgmt",
    "wpa_psks",
    "security",
    "rsn_pairwise_ccmp",
    "wpa_pairwise_tkip",
    "wpa_pairwise_ccmp",
]


async def serve(endpoint):
    session = OvsSession(endpoint)
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            try:
                action = json.loads(line)["action"]
                snap = await session.snapshot()
                tables = snap["tables"]
                schema = snap["schema"]
                config_id, config = next(
                    (u, r)
                    for u, r in tables["Wifi_VIF_Config"].items()
                    if schema.decode("Wifi_VIF_Config", "if_name", r["if_name"]) == "lab-ap"
                )
                changes = []
                if action in {"apply", "partial"}:
                    for state_id, row in tables["Wifi_VIF_State"].items():
                        ref = schema.decode("Wifi_VIF_State", "vif_config", row["vif_config"])
                        if ref == config_id:
                            columns = ["ssid"] if action == "partial" else APPLY_COLUMNS
                            changes.append(
                                {
                                    "op": "update",
                                    "table": "Wifi_VIF_State",
                                    "where": [["_uuid", "==", ["uuid", state_id]]],
                                    "row": {c: config[c] for c in columns},
                                }
                            )
                elif action == "compete":
                    changes.append(
                        {
                            "op": "update",
                            "table": "Wifi_VIF_Config",
                            "where": [["_uuid", "==", ["uuid", config_id]]],
                            "row": {"ssid": "competing-network"},
                        }
                    )
                elif action not in {"observe", "withhold", "reject"}:
                    raise ValueError("unknown simulated driver action")
                results = await session.transact(changes) if changes else []
                if any("error" in r or r.get("count") != 1 for r in results):
                    raise ValueError("manager transaction rejected")
                print(
                    json.dumps(
                        {
                            "action": action,
                            "config_consumed": True,
                            "state_writes": len(changes) if action != "compete" else 0,
                            "generation": snap["generation"],
                            "provenance": "independent-simulated-manager",
                        }
                    ),
                    flush=True,
                )
            except Exception as exc:
                print(json.dumps({"error": type(exc).__name__}), flush=True)
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(serve(sys.argv[1]))
