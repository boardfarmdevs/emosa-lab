"""Pin and compare selected public BBF 2.17.0 definitions; no device access.

Full publisher XML stays in the caller's cache outside Git. Output contains
parameter paths, types and hashes, not redistributed specification prose.
"""

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import urlopen

PINS = {
    "usp": "4907bdbd5802a40b9d0e493d9c5c1884a25ab3b0437f725f71a39604eaefb5f7",
    "cwmp": "5c0f218337ec4831b3583c1ec7ed7b974dd5e57ef3281d01052f84337538a303",
}
DEVICE = "Device.WiFi.DataElements.Network.Device.{i}."
RADIO = DEVICE + "Radio.{i}."
BSS = RADIO + "BSS.{i}."
STA = BSS + "STA.{i}."
ASSOCIATED = "Device.WiFi.AccessPoint.{i}.AssociatedDevice.{i}.Stats."
COUNTERS = (
    "BytesSent",
    "BytesReceived",
    "PacketsSent",
    "PacketsReceived",
    "ErrorsSent",
    "ErrorsReceived",
    "RetransCount",
)
SELECTED = {
    DEVICE: ("CollectionInterval",),
    RADIO: ("Noise", "Utilization", "Transmit", "ReceiveSelf", "ReceiveOther"),
    BSS: (
        "UnicastBytesSent",
        "UnicastBytesReceived",
        "MulticastBytesSent",
        "MulticastBytesReceived",
        "BroadcastBytesSent",
        "BroadcastBytesReceived",
    ),
    STA: COUNTERS
    + (
        "LastDataDownlinkRate",
        "LastDataUplinkRate",
        "UtilizationReceive",
        "UtilizationTransmit",
        "EstMACDataRateDownlink",
        "EstMACDataRateUplink",
        "SignalStrength",
    ),
    ASSOCIATED: COUNTERS,
}


def canonical(node):
    """Ignore formatting and attribute order, preserve all definition content."""
    return [
        node.tag,
        dict(sorted(node.attrib.items())),
        " ".join((node.text or "").split()),
        [[canonical(child), " ".join((child.tail or "").split())] for child in node],
    ]


def digest(node):
    return hashlib.sha256(json.dumps(canonical(node), sort_keys=True).encode()).hexdigest()


def inspect(root):
    if [node.get("name") for node in root.findall("model")] != ["Device:2.17"]:
        raise ValueError("expected BBF Device:2.17 model")
    objects = {node.get("name"): node for node in root.iter("object")}
    result = {}
    for path, names in SELECTED.items():
        obj = objects[path]
        parameters = {node.get("name"): node for node in obj.findall("parameter")}
        for name in names:
            node = parameters[name]
            syntax = node.find("syntax")
            primitive = next(iter(syntax))
            units, bounds = primitive.find("units"), primitive.find("range")
            result[path + name] = {
                "definition_sha256": digest(node),
                "syntax_sha256": digest(syntax),
                "type": primitive.get("ref", primitive.tag),
                "units": units.get("value") if units is not None else None,
                "range": dict(bounds.attrib) if bounds is not None else None,
            }
    types = {
        node.get("name"): digest(node)
        for node in root.findall("dataType")
        if node.get("name") in ("StatsCounter32", "StatsCounter64")
    }
    if len(types) != 2:
        raise ValueError("missing statistics sentinel definitions")
    return {
        "parameters": result,
        "statistics_types": types,
        "associated_stats_object_description_sha256": digest(
            objects[ASSOCIATED].find("description")
        ),
    }


def compare(usp, cwmp):
    if usp != cwmp:
        raise ValueError("selected USP/CWMP definitions differ; review required")
    return usp


def review(cache, *, offline=False):
    records, definitions = {}, {}
    for side, expected in PINS.items():
        name = f"tr-181-2-17-0-{side}-full.xml"
        url = f"https://{side}-data-models.broadband-forum.org/{name}"
        target = cache / (side + "-" + name)
        if target.exists():
            data = target.read_bytes()
        elif offline:
            raise ValueError(f"missing offline source: {target.name}")
        else:
            with urlopen(url, timeout=45) as response:
                data = response.read(8 * 1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"source hash mismatch: {target.name}; no automatic edition update")
        root = ET.fromstring(data)
        if root.get("spec") != f"urn:broadband-forum-org:tr-181-2-17-0-{side}":
            raise ValueError("wrong BBF source identity")
        definitions[side] = inspect(root)
        records[side] = {"url": url, "sha256": expected, "bytes": len(data)}
        if not target.exists():
            cache.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(data)
    selected = compare(definitions["usp"], definitions["cwmp"])
    for path, parameter in selected["parameters"].items():
        anchor = "D.Device:2." + path.replace(".{i}", "")
        parameter["references"] = {
            side: f"https://{side}-data-models.broadband-forum.org/tr-181-2-17-0-{side}.html#{anchor}"
            for side in PINS
        }
    return {
        "scope": "BBF TR-181 2.17.0 selected definitions as served and pinned on 2026-09-23",
        "sources": records,
        "selected_parameters": len(selected["parameters"]),
        "usp_cwmp_selected_definitions_equal": True,
        **selected,
        "wfa_der3_equivalence_verified": False,
        "measurement_source_qualified": False,
        "sustained_operation_proven": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new JSON result path (never overwritten)")
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path.home() / ".local/share/emosa/specifications/bbf-tr181-2.17.0",
    )
    parser.add_argument(
        "--offline", action="store_true", help="require both pinned local XML files"
    )
    args = parser.parse_args()
    result = review(args.cache, offline=args.offline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print(f"Verified {result['selected_parameters']} matching BBF parameters: {args.output}")
