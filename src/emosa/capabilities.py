def capabilities(ready, ownership_conflict=False):
    return {
        "bss-set": {
            "status": "supported"
            if ready and not ownership_conflict
            else "temporarily_unavailable",
            "scope": "one existing enabled WPA2-PSK AP; SSID and designated key",
            "qualification": "synthetic-existing-bss-v1; simulation only",
        },
        **{
            name: {"status": "unsupported", "reason": "no qualified existing remote operation"}
            for name in ("steering", "channel-set", "vif-create", "vif-delete", "backhaul-set")
        },
        "detailed-metrics": {"status": "unknown", "reason": "no qualified telemetry source"},
    }
