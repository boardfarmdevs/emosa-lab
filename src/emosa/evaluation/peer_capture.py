"""Offline WFA inclusion review using an independent tshark dissector; no wire endpoint."""

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path

from emosa.errors import EmosaError, Reason
from emosa.evaluation.evidence import write_json

FIELDS = (
    "frame.number",
    "frame.time_epoch",
    "frame.len",
    "frame.cap_len",
    "eth.src",
    "eth.dst",
    "ieee1905.message_type",
    "ieee1905.message_id",
    "ieee1905.fragment_id",
    "ieee1905.last_fragment",
    "ieee1905.fragment.reassembled.length",
    "ieee1905.tlv_type",
    "ieee1905.multi_ap_version",
    "wps.message_type",
    "wps.mac_address",
    "ieee1905.radio_basic_cap.max_bss",
    "_ws.malformed",
)
MAX_CAPTURE = 64 * 1024 * 1024
MAX_OUTPUT = 16 * 1024 * 1024
MAX_FRAMES = 20000
CORRELATION_SECONDS = 5  # Local analysis window, never an IEEE protocol timer.


def bounded_tool(arguments, directory, label):
    """Bound time and disk output without buffering untrusted dissector output in RAM."""
    out, err = directory / (label + ".out"), directory / (label + ".err")
    with out.open("wb") as stdout, err.open("wb") as stderr:
        try:
            child = subprocess.Popen(arguments, stdout=stdout, stderr=stderr)
        except OSError as exc:
            raise EmosaError(Reason.MISSING_PREREQUISITE, "tshark executable unavailable") from exc
        try:
            deadline = time.monotonic() + 30
            while child.poll() is None:
                if (
                    time.monotonic() >= deadline
                    or out.stat().st_size + err.stat().st_size > MAX_OUTPUT
                ):
                    raise EmosaError(Reason.INVALID_INPUT, "dissector exceeded analysis budget")
                time.sleep(0.05)
            if out.stat().st_size + err.stat().st_size > MAX_OUTPUT:
                raise EmosaError(Reason.INVALID_INPUT, "dissector exceeded output budget")
            if child.returncode:
                raise EmosaError(
                    Reason.INVALID_INPUT, "dissector could not decode the capture/fields"
                )
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    return out.read_text()


def numbers(value):
    return [int(v, 16 if v.startswith("0x") else 10) for v in value.split(",") if v]


def single(data, key, *, optional=False):
    values = numbers(data[key])
    if optional and not values:
        return None
    if len(values) != 1 or values[0] < 0:
        raise ValueError("missing or ambiguous scalar field")
    return values[0]


def parse_tsv(text):
    records = []
    try:
        reader = csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
        for row in reader:
            if len(row) != len(FIELDS) or len(records) >= MAX_FRAMES:
                raise ValueError("invalid or excessive field rows")
            data = dict(zip(FIELDS, row, strict=True))
            # This is a projection of dissector output, not an IEEE parser.
            # tshark 3.6 renders this boolean as 0/1, whereas 4.2 uses False/True.
            data["ieee1905.last_fragment"] = {"False": "0", "True": "1"}.get(
                data["ieee1905.last_fragment"], data["ieee1905.last_fragment"]
            )
            timestamp = float(data["frame.time_epoch"])
            if not math.isfinite(timestamp):
                raise ValueError("invalid timestamp")
            records.append(
                {
                    "frame": single(data, "frame.number"),
                    "time": timestamp,
                    "source": data["eth.src"].lower(),
                    "destination": data["eth.dst"].lower(),
                    "message_type": single(data, "ieee1905.message_type", optional=True),
                    "mid": single(data, "ieee1905.message_id", optional=True),
                    "fragment": single(data, "ieee1905.fragment_id", optional=True),
                    "last_fragment": single(data, "ieee1905.last_fragment", optional=True),
                    "reassembled_bytes": single(
                        data, "ieee1905.fragment.reassembled.length", optional=True
                    ),
                    "tlvs": numbers(data["ieee1905.tlv_type"]),
                    "profiles": numbers(data["ieee1905.multi_ap_version"]),
                    "wps_types": numbers(data["wps.message_type"]),
                    "wps_macs": [v.lower() for v in data["wps.mac_address"].split(",") if v],
                    "max_bss": numbers(data["ieee1905.radio_basic_cap.max_bss"]),
                    "truncated": single(data, "frame.cap_len") != single(data, "frame.len"),
                    "malformed": bool(data["_ws.malformed"]),
                }
            )
    except (ValueError, OverflowError) as exc:
        raise EmosaError(Reason.INVALID_INPUT, "invalid projected dissector output") from exc
    return records


def review(records, *, controller_al, agent_al):
    """Review selected source-backed conditions; absence of findings is not conformance."""
    findings, assessed, partial, searches = [], [], [], []
    observations = []
    selected = [r for r in records if r["source"] in {controller_al, agent_al}]

    def finding(row, rule, condition, observed, source):
        findings.append(
            {
                "frame": row["frame"],
                "rule": rule,
                "condition": condition,
                "observed": observed,
                "source": source,
            }
        )

    def one(row, tlv, label, source):
        count = Counter(row["tlvs"])[tlv]
        if count != 1:
            finding(row, label, "exactly one in this selected procedure case", count, source)

    for row in selected:
        complete = (
            row["last_fragment"] == 1
            and row["fragment"] is not None
            and row["mid"] is not None
            and row["message_type"] is not None
            and (row["fragment"] == 0 or bool(row["reassembled_bytes"]))
        )
        if row["truncated"] or row["malformed"] or not complete:
            partial.append(row["frame"])
            continue
        kind = row["message_type"]

        if kind == 0x0007 and row["source"] == agent_al:
            one(row, 0x80, "search_supported_service", "EasyMesh 6.1 §6.1 / §17.1.1")
            one(row, 0x81, "search_searched_service", "EasyMesh 6.1 §6.1 / §17.1.1")
            one(row, 0xB3, "search_profile", "EasyMesh 6.1 §6.1 / §17.1.1")
            searches.append(row)
            if row["profiles"] == [1]:
                one(row, 0xB4, "profile1_search_capabilities", "EasyMesh 6.1 §6.1")
        elif kind == 0x0008 and row["source"] == controller_al:
            one(row, 0x80, "response_supported_service", "EasyMesh 6.1 §6.1 / §17.1.2")
            one(row, 0xB3, "response_profile", "EasyMesh 6.1 §6.1 / §17.1.2")
            candidates = [
                s
                for s in searches
                if s["mid"] == row["mid"]
                and s["mid"] is not None
                and row["destination"] == agent_al
                and 0 <= row["time"] - s["time"] <= CORRELATION_SECONDS
            ]
            if len(candidates) == 1:
                search = candidates[0]
                if (
                    len(search["profiles"]) == 1
                    and search["profiles"][0] in {1, 2, 3}
                    and row["profiles"] != search["profiles"]
                ):
                    finding(
                        row,
                        "discovery_profile_response",
                        "response profile follows the correlated agent Search profile",
                        {
                            "search_frame": search["frame"],
                            "search": search["profiles"],
                            "response": row["profiles"],
                        },
                        "EasyMesh 6.1 §6.1 p.63",
                    )
            else:
                observations.append(
                    {
                        "frame": row["frame"],
                        "profile_pairing": "not_established",
                        "candidate_count": len(candidates),
                    }
                )
        elif kind == 0x0002:
            one(row, 0xB3, "topology_query_profile", "EasyMesh 6.1 §6.2 / §17.1.38 / §18")
        elif kind == 0x0003 and row["source"] == agent_al:
            one(row, 0x80, "topology_response_service", "EasyMesh 6.1 §6.2")
            one(row, 0x83, "topology_operational_bss", "EasyMesh 6.1 §17.1.4")
            one(row, 0xB3, "topology_response_profile", "EasyMesh 6.1 §17.1.4 / §18")
        elif kind == 0x0009 and row["source"] == agent_al and 4 in row["wps_types"]:
            for tlv, label in (
                (0x85, "M1_radio_basic"),
                (0xB4, "M1_profile2_capability"),
                (0xBE, "M1_radio_advanced"),
            ):
                one(row, tlv, label, "EasyMesh 6.1 §7.1 / §17.1.3")
            if row["wps_types"] != [4] or row["wps_macs"] != [agent_al]:
                finding(
                    row,
                    "M1_count_and_mac",
                    "one M1 with enrollee MAC equal to agent AL",
                    {"wps_types": row["wps_types"], "wps_macs": row["wps_macs"]},
                    "EasyMesh 6.1 §7.1 p.67 / §17.1.3",
                )
            observations.append(
                {
                    "frame": row["frame"],
                    "advertised_max_bss": row["max_bss"],
                    "actual_radio_capabilities_verified": False,
                }
            )
        elif kind == 0x0009 and row["source"] == controller_al and 5 in row["wps_types"]:
            one(row, 0x82, "M2_radio_identifier", "EasyMesh 6.1 §7.1 / §17.1.3")
            observations.append(
                {
                    "frame": row["frame"],
                    "m2_payload_count": row["wps_types"].count(5),
                    "other_wps_types": [x for x in row["wps_types"] if x != 5],
                    "single_bss_payload_count": row["wps_types"] == [5],
                    "encrypted_roles_and_credentials": "not_inspected",
                    "complete_request_mapping": "not_qualified",
                }
            )
        else:
            continue
        assessed.append(row["frame"])
    return {
        "schema_version": 1,
        "scope": "offline selected WFA inclusion review of native peer traffic",
        "status": "no_selected_messages"
        if not assessed
        else "review_required"
        if findings
        else "selected_checks_observed",
        "full_protocol_conformance": False,
        "controller_onboarding_by_emosa_proven": False,
        "physical_pod_proven": False,
        "controller_al": controller_al,
        "agent_al": agent_al,
        "decoded_frames": len(records),
        "selected_peer_frames": len(selected),
        "assessed_frames": assessed,
        "unassessed_partial_or_malformed": partial,
        "findings": findings,
        "observations": observations,
        "correlation_policy": {
            "same_mid_and_selected_peers": True,
            "window_seconds": CORRELATION_SECONDS,
            "normative_timer": False,
            "ambiguous_pairs_assessed": False,
        },
        "limitations": [
            "Dissector output is an independent implementation observation, not a specification.",
            "Only the listed WFA inclusion conditions are checked; "
            "IEEE base/amendment remain pending.",
            "No exchange authentication, decryption, replay, physical scope "
            "or write authorization.",
            "Reassembled messages rely on the recorded tshark implementation.",
            "No live traffic is generated, replayed or forwarded.",
            "Selected lab peers use their AL as the Ethernet source; relayed/other links "
            "require separate identity qualification.",
        ],
    }


def analyze(capture, output, *, controller_al, agent_al, tshark="tshark"):
    for value in (controller_al, agent_al):
        if not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", value):
            raise EmosaError(Reason.INVALID_INPUT, "explicit lowercase peer AL addresses required")
    if controller_al == agent_al:
        raise EmosaError(Reason.INVALID_INPUT, "controller and agent identities must differ")
    capture, output = Path(capture).resolve(), Path(output).resolve()
    if not capture.is_file() or capture.stat().st_size > MAX_CAPTURE:
        raise EmosaError(Reason.INVALID_INPUT, "capture absent or exceeds 64 MiB admission budget")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    try:
        with tempfile.TemporaryDirectory(prefix="emosa-peer-review-") as temporary:
            directory = Path(temporary)
            snapshot = directory / "input.pcap"
            digest = hashlib.sha256()
            size = 0
            with capture.open("rb") as source, snapshot.open("wb") as target:
                before = os.fstat(source.fileno())
                while chunk := source.read(65536):
                    size += len(chunk)
                    if size > MAX_CAPTURE:
                        raise EmosaError(
                            Reason.INVALID_INPUT, "capture grew beyond admission budget"
                        )
                    target.write(chunk)
                    digest.update(chunk)
                after = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise EmosaError(Reason.INVALID_INPUT, "capture changed during snapshot")
            executable = shutil.which(tshark)
            if executable is None:
                raise EmosaError(Reason.MISSING_PREREQUISITE, "tshark executable unavailable")
            version = bounded_tool([executable, "--version"], directory, "version").splitlines()[0]
            command = [
                executable,
                "-n",
                "-2",
                "-r",
                str(snapshot),
                "-Y",
                "ieee1905",
                "-T",
                "fields",
                "-E",
                "occurrence=a",
            ]
            for name in FIELDS:
                command += ["-e", name]
            projected = bounded_tool(command, directory, "fields")
            report = review(parse_tsv(projected), controller_al=controller_al, agent_al=agent_al)
            report["capture"] = {"sha256": digest.hexdigest(), "bytes": size}
            report["dissector"] = {
                "version": version,
                "executable_sha256": hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
                "fields": FIELDS,
            }
            (output / "frames.tsv").write_text(projected)
            report["projected_fields_sha256"] = hashlib.sha256(projected.encode()).hexdigest()
            write_json(output / "review.json", report)
            return report
    except BaseException as exc:
        write_json(
            output / "failure.json",
            {"status": "analysis_failed", "error": type(exc).__name__, "conformance_proven": False},
        )
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="new private review directory")
    parser.add_argument("--controller-al", required=True)
    parser.add_argument("--agent-al", required=True)
    parser.add_argument("--tshark", default="tshark")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = analyze(
            args.capture,
            args.output,
            controller_al=args.controller_al,
            agent_al=args.agent_al,
            tshark=args.tshark,
        )
    except (EmosaError, OSError) as exc:
        print(json.dumps({"status": "analysis_failed", "error": type(exc).__name__}))
        raise SystemExit(2) from None
    print(
        json.dumps(
            {
                "status": result["status"],
                "findings": len(result["findings"]),
                "report": str(args.output / "review.json"),
                "conformance_proven": False,
            }
        )
    )


if __name__ == "__main__":
    main()
