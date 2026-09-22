"""Read-only Ethernet/PCAP inspection. Never print TLV values or decrypt WSC."""

import hashlib
import stat
import struct
from pathlib import Path

from emosa.errors import EmosaError, Reason
from emosa.wire.autoconfiguration import parse_response, parse_search
from emosa.wire.cmdu import ETHERTYPE, MULTICAST, Reassembler, invalid

NAMES = {
    0: "Topology Discovery",
    1: "Topology Notification",
    2: "Topology Query",
    3: "Topology Response",
    4: "Vendor Specific",
    5: "Link Metric Query",
    6: "Link Metric Response",
    7: "AP Autoconfiguration Search",
    8: "AP Autoconfiguration Response",
    9: "AP Autoconfiguration WSC",
    10: "AP Autoconfiguration Renew",
    0x8000: "1905 Ack",
    0x8001: "AP Capability Query",
    0x8002: "AP Capability Report",
}
MAX_CAPTURE = 64 * 1024 * 1024


def discovery(message):
    """Only the complete base Topology Discovery contract, sections 6.2/6.3.1.

    The two fixed-size base values aggregate by type under 6.2. Unknown TLVs
    are ignored. This is not the EasyMesh Search/Response or an inventory entry.
    """
    if message.message_type != 0 or message.destination != MULTICAST or message.relay:
        invalid("invalid Topology Discovery envelope")
    values = {}
    for kind in (1, 2):
        value = b"".join(t.value for t in message.tlvs if t.kind == kind)
        if len(value) != 6:
            invalid("missing or invalid discovery identity TLV")
        values[kind] = value.hex(":")
    return {"al_mac": values[1], "interface_mac": values[2]}


def describe(message):
    result = {
        "source": message.source.hex(":"),
        "destination": message.destination.hex(":"),
        "message_type": f"0x{message.message_type:04x}",
        "name": NAMES.get(message.message_type, "uninterpreted message type"),
        "mid": message.mid,
        "relay": message.relay,
        "fragments": message.fragments,
        "tlvs": [{"type": f"0x{t.kind:02x}", "length": len(t.value)} for t in message.tlvs],
        "procedure_validation": "not_performed",
        "operation_created": False,
    }
    if message.message_type == 0:
        result["discovery"] = discovery(message)
        result["procedure_validation"] = "base_topology_discovery_only"
    elif message.message_type == 7:
        search = parse_search(message)
        result["autoconfiguration"] = {
            "al_mac": search.al_mac.hex(":"),
            "band": search.band,
            "advertised_profile": search.profile,
        }
        result["procedure_validation"] = "selected_search_fields_only"
    elif message.message_type == 8:
        response = parse_response(message)
        result["autoconfiguration"] = {
            "band": response.band,
            "advertised_profile": response.profile,
            "pending_requirements": list(response.pending_requirements),
        }
        result["procedure_validation"] = "selected_response_fields_only"
    return result


def discovery_pairs(messages):
    """Read-only capture correlation; matching a MID/MAC never authenticates a peer."""
    searches, pairs = {}, []
    for message in messages:
        facts = message.get("autoconfiguration")
        if facts is None:
            continue
        if message["message_type"] == "0x0007":
            key = (facts["al_mac"], message["mid"])
            if key not in searches and len(searches) >= 4096:
                searches.pop(next(iter(searches)))
            searches[key] = message
        elif message["message_type"] == "0x0008":
            request = searches.get((message["destination"], message["mid"]))
            if request is None:
                continue  # Missing/out-of-window traffic is not invented.
            previous = request["autoconfiguration"]
            sent, received = previous["advertised_profile"], facts["advertised_profile"]
            effective = received if received in (1, 2, 3) else sent
            pairs.append(
                {
                    "search_frame": request["completed_at_frame"],
                    "response_frame": message["completed_at_frame"],
                    "mid": message["mid"],
                    "search_profile": sent,
                    "response_profile": received,
                    "profile_matches": effective == sent if sent in (1, 2, 3) else None,
                    "band_matches": previous["band"] == facts["band"],
                    "pending_requirements": facts["pending_requirements"],
                    "correlation": "capture_addresses_and_mid_only",
                    "onboarding_proven": False,
                }
            )
    return pairs


def packets(path):
    """Bounded classic PCAP reader; PCAPNG, VLAN and FCS are not guessed."""
    path = Path(path)
    with path.open("rb") as stream:
        import os

        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CAPTURE:
            invalid("capture must be a regular file of at most 64 MiB")
        header = stream.read(24)
        formats = {
            b"\xd4\xc3\xb2\xa1": ("<", 1e6),
            b"\xa1\xb2\xc3\xd4": (">", 1e6),
            b"\x4d\x3c\xb2\xa1": ("<", 1e9),
            b"\xa1\xb2\x3c\x4d": (">", 1e9),
        }
        if len(header) != 24 or header[:4] not in formats:
            invalid("expected classic PCAP, not PCAPNG")
        endian, scale = formats[header[:4]]
        major, minor, _zone, _sig, snaplen, linktype = struct.unpack(endian + "HHIIII", header[4:])
        if (major, minor) != (2, 4) or linktype != 1 or not 22 <= snaplen <= MAX_CAPTURE:
            invalid("unsupported PCAP version, link type or snapshot length")
        consumed = 24
        for number in range(1, 100001):
            record = stream.read(16)
            if not record:
                return
            if len(record) != 16:
                invalid("truncated PCAP record header")
            seconds, fraction, size, original = struct.unpack(endian + "IIII", record)
            consumed += 16 + size
            if size > min(snaplen, 65535) or size > original or fraction >= scale:
                invalid("invalid PCAP record length or timestamp")
            if consumed > MAX_CAPTURE:
                invalid("capture exceeds read budget")
            data = stream.read(size)
            if len(data) != size:
                invalid("truncated PCAP record data")
            yield number, seconds + fraction / scale, data, size != original
        if stream.read(1):
            invalid("capture packet budget exhausted")


def inspect_capture(path):
    now = 0.0
    reassembler = Reassembler(clock=lambda: now)
    messages, rejected = [], []
    count = ignored = tagged = expired = 0
    for number, timestamp, frame, truncated in packets(path):
        count += 1
        now = max(now, timestamp)
        expired += reassembler.expire()
        if len(frame) < 14:
            rejected.append({"frame": number, "reason": "truncated Ethernet header"})
            continue
        ether_type = struct.unpack_from("!H", frame, 12)[0]
        if ether_type != ETHERTYPE:
            tagged += ether_type in (0x8100, 0x88A8)
            ignored += 1
            continue
        try:
            if truncated:
                invalid("capture truncated an IEEE 1905 frame")
            message = reassembler.feed(frame)
            if message is not None:
                messages.append({"completed_at_frame": number, **describe(message)})
        except EmosaError as exc:
            rejected.append({"frame": number, "code": exc.code, "reason": str(exc)})
    return {
        "schema_version": 1,
        "scope": "IEEE1905_envelope_and_base_discovery_inspection",
        "packets": count,
        "ignored_non_1905": ignored,
        "unsupported_tagged_frames": tagged,
        "messages": messages,
        "autoconfiguration_pairs": discovery_pairs(messages),
        "rejected": rejected,
        "expired_assemblies": expired,
        "incomplete_or_quarantined": len(reassembler.contexts),
        "buffered_bytes": reassembler.buffered,
        "operations_created": 0,
        "onboarding_proven": False,
        "physical_pod_proven": False,
    }


def read_frame(path):
    with Path(path).open("rb") as stream:
        data = stream.read(1515)
    if len(data) > 1514:
        invalid("frame file exceeds Ethernet budget")
    message = Reassembler().feed(data)
    if message is None:
        raise EmosaError(Reason.NOT_READY, "frame needs additional fragments; use a capture")
    return {"frame_sha256": hashlib.sha256(data).hexdigest(), **describe(message)}
