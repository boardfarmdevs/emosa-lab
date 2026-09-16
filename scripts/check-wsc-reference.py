"""Rebuild synthetic WSC vectors with pinned upstream hostap, independently of EMOSA.

Needs a C compiler and OpenSSL development headers. No daemon or radio is started.
Without --archive, downloads the pinned official release into a temporary directory.
"""

import argparse
import hashlib
import json
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/protocol"


def check_fixture(name, source_root, work):
    fixture = FIXTURES / name
    provenance = json.loads((fixture / "provenance.json").read_text())
    for filename, digest in provenance["source_files"].items():
        if hashlib.sha256((source_root / filename).read_bytes()).hexdigest() != digest:
            raise SystemExit("hostap source file digest mismatch")
    for filename, key in (("reference.c", "harness_sha256"), ("vectors.json", "vectors_sha256")):
        if hashlib.sha256((fixture / filename).read_bytes()).hexdigest() != provenance[key]:
            raise SystemExit(f"{name} retained {filename} digest mismatch")
    executable = work / name
    command = [
        "cc",
        "-O2",
        "-ffunction-sections",
        "-fdata-sections",
        "-DCONFIG_WPS",
        "-DCONFIG_SHA256",
        "-DCONFIG_NO_STDOUT_DEBUG",
        *[f"-D{define}" for define in provenance.get("compile_defines", [])],
        "-I",
        str(source_root / "src"),
        "-I",
        str(source_root / "src/utils"),
        str(fixture / "reference.c"),
        *[str(source_root / filename) for filename in provenance["compiled_sources"]],
        "-Wl,--gc-sections",
        "-lcrypto",
        "-o",
        str(executable),
    ]
    subprocess.run(command, check=True, timeout=60)
    result = subprocess.run(
        [str(executable)], check=True, capture_output=True, text=True, timeout=30
    )
    cases = []
    for line in result.stdout.splitlines():
        key, value = line.split("=", 1)
        if key == "case":
            cases.append({"name": value})
        else:
            cases[-1][key] = value
    if cases != json.loads((fixture / "vectors.json").read_text())["cases"]:
        raise SystemExit("independent hostap output differs from retained vectors")
    print(f"Independent hostap {name}: {len(cases)} cases match retained expected bytes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="existing digest-matching hostapd-2.11.tar.gz")
    parser.add_argument("--fixture", choices=["all", "wsc", "wsc-messages"], default="all")
    args = parser.parse_args()
    names = ["wsc", "wsc-messages"] if args.fixture == "all" else [args.fixture]
    references = [json.loads((FIXTURES / name / "provenance.json").read_text()) for name in names]
    provenance = references[0]
    if any(ref["source_sha256"] != provenance["source_sha256"] for ref in references):
        raise SystemExit("selected fixtures require different hostap archives")
    with tempfile.TemporaryDirectory(prefix="emosa-wsc-reference-") as directory:
        work = Path(directory)
        archive = args.archive
        if archive is None:
            archive = work / "hostapd-2.11.tar.gz"
            with urllib.request.urlopen(provenance["source_url"], timeout=30) as response:
                archive.write_bytes(response.read(16 * 1024 * 1024))
        if hashlib.sha256(archive.read_bytes()).hexdigest() != provenance["source_sha256"]:
            raise SystemExit("hostap archive digest mismatch")
        with tarfile.open(archive) as source:
            source.extractall(work, filter="data")
        source_root = work / "hostapd-2.11"
        for name in names:
            check_fixture(name, source_root, work)


if __name__ == "__main__":
    main()
