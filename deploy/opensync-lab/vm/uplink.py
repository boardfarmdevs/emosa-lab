#!/usr/bin/env python3
"""Move one pod's uplink for the data plane experiments (doc/architecture/data-plane.md).

Inside the lab VM (root): python3 uplink.py POD MODE [--ssid S --key K ...]

  gtp        option 2: bhaul-sta-24 joins the pod-backhaul SSID (served by em-gtp)
             with a gre credential; the pod's cm builds its GRE to the GTP (.1)
  multi-ap   option 1: bhaul-sta-24 joins the Multi-AP backhaul BSS (em-gtp's
             emosa-lab-bh) with a sole multi_ap credential. --fallback adds the gre
             credential for the pod-backhaul SSID at a lower priority (an experiment:
             osw aborts owm when it stays on the lower-priority network)
  restore    back to the lab's original uplink: bhaul-sta-50 to mv3, bhaul-sta-24 off
  show       the pod's backhaul stations, credentials and uplink as it reports them

Credential-list mode throughout: the station's own ssid is empty, so OpenSync
(ow_ovsdb_cconf.c) builds one wpa_supplicant network per linked credential, by
priority. Rows are written with the pod's own ovsdb-client, in one transaction.
"""

import argparse
import json
import subprocess
import sys

STATION, ORIGINAL = "bhaul-sta-24", "bhaul-sta-50"
LAB_SSIDS = {"emosa-podbh", "emosa-lab-bh", "emosa-lab-bh-missing"}  # credentials this script owns
LINKS = r"""
for i in bhaul-sta-24 bhaul-sta-50; do
    echo "$i: $(iw dev $i link | head -2 | tr '\n' ' ')$(iw dev $i info | grep -o '4addr: on')"
done
echo "br-home ports: $(ip -br link show master br-home | cut -d' ' -f1 | tr '\n' ' ')"
ip -br addr show dev br-home
"""


def pod(name, *args, check=True):
    r = subprocess.run(["lxc", "exec", name, "--", *args], capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"{name}: {' '.join(args[:2])}: {r.stderr.strip()}")
    return r.stdout


def transact(name, *ops):
    out = pod(
        name,
        "ovsdb-client",
        "transact",
        "unix:/var/run/db.sock",
        json.dumps(["Open_vSwitch", *ops]),
    )
    result = json.loads(out)
    errors = [r for r in result if isinstance(r, dict) and r.get("error")]
    if errors:
        raise SystemExit(f"{name}: transaction failed: {errors}")
    return result


def select(name, table, where=(), columns=None):
    op = {"op": "select", "table": table, "where": list(where)}
    if columns:
        op["columns"] = columns
    return transact(name, op)[0]["rows"]


def credential(ssid, key, kind, priority):
    return {
        "ssid": ssid,
        "security": ["map", [["encryption", "WPA-PSK"], ["key", key]]],
        "onboard_type": kind,
        "priority": priority,
        "enabled": True,
    }


def apply(name, credentials, *, use_station):
    """One transaction: lab credentials replaced, bhaul-sta-24 in credential-list mode."""
    ops = [
        {"op": "delete", "table": "Wifi_Credential_Config", "where": [["ssid", "==", s]]}
        for s in LAB_SSIDS
    ]
    refs = []
    for i, row in enumerate(credentials):
        ops.append(
            {"op": "insert", "table": "Wifi_Credential_Config", "uuid-name": f"c{i}", "row": row}
        )
        refs.append(["named-uuid", f"c{i}"])
    vif = {
        "mode": "sta",
        "enabled": use_station,
        "ssid": "",
        "vif_radio_idx": 0,
        "credential_configs": ["set", refs],
    }
    if select(name, "Wifi_VIF_Config", [["if_name", "==", STATION]], ["_uuid"]):
        ops.append(
            {
                "op": "update",
                "table": "Wifi_VIF_Config",
                "where": [["if_name", "==", STATION]],
                "row": vif,
            }
        )
    else:
        ops.append(
            {
                "op": "insert",
                "table": "Wifi_VIF_Config",
                "uuid-name": "v",
                "row": {**vif, "if_name": STATION},
            }
        )
        radio = [["freq_band", "==", "2.4G"]]
        link = ["vif_configs", "insert", ["set", [["named-uuid", "v"]]]]
        ops.append(
            {"op": "mutate", "table": "Wifi_Radio_Config", "where": radio, "mutations": [link]}
        )
    if not select(name, "Wifi_Inet_Config", [["if_name", "==", STATION]], ["_uuid"]):
        inet = {
            "if_name": STATION,
            "if_type": "vif",
            "enabled": True,
            "network": True,
            "NAT": False,
            "ip_assign_scheme": "dhcp",
            "mtu": 1600,
            "dhcp_req": 1,
        }
        ops.append({"op": "insert", "table": "Wifi_Inet_Config", "row": inet})
    original = [["if_name", "==", ORIGINAL]]
    ops.append(
        {
            "op": "update",
            "table": "Wifi_VIF_Config",
            "where": original,
            "row": {"enabled": not use_station},
        }
    )
    transact(name, *ops)


def show(name):
    columns = ["if_name", "enabled", "ssid", "parent", "multi_ap", "wds", "bridge"]
    rows = select(name, "Wifi_VIF_State", [["mode", "==", "sta"]], columns)
    creds = select(
        name, "Wifi_Credential_Config", [], ["ssid", "onboard_type", "priority", "enabled"]
    )
    uplinks = select(
        name, "Connection_Manager_Uplink", [], ["if_name", "if_type", "is_used", "has_L2", "has_L3"]
    )
    links = pod(name, "sh", "-c", LINKS, check=False)
    return {
        "stations": rows,
        "credentials": creds,
        "uplinks": uplinks,
        "links": links.strip().splitlines(),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pod")
    ap.add_argument("mode", choices=("gtp", "multi-ap", "restore", "show"))
    ap.add_argument("--podbh-ssid", default="emosa-podbh")
    ap.add_argument("--podbh-key", default="EmosaPodBh2026!")
    ap.add_argument("--bh-ssid", default="emosa-lab-bh")
    ap.add_argument("--bh-key", default="EmosaLabBh2026!")
    ap.add_argument("--fallback", action="store_true", help="multi-ap: also the gre credential")
    args = ap.parse_args()
    gre = credential(args.podbh_ssid, args.podbh_key, "gre", 1)
    if args.mode == "gtp":
        apply(args.pod, [gre], use_station=True)
    elif args.mode == "multi-ap":
        creds = [credential(args.bh_ssid, args.bh_key, "multi_ap", 2)]
        apply(args.pod, creds + ([gre] if args.fallback else []), use_station=True)
    elif args.mode == "restore":
        apply(args.pod, [], use_station=False)
    json.dump(show(args.pod), sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
