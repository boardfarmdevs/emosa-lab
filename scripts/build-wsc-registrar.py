"""Build the owned synthetic payload peer using pinned unmodified hostap sources."""

import argparse
import hashlib
import json
import subprocess
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / ".cache/wsc-registrar")
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    reference = json.loads(
        (ROOT / "tests/fixtures/protocol/wsc-messages/provenance.json").read_text()
    )
    archive = args.archive or out / "hostapd-2.11.tar.gz"
    if not archive.exists():
        with urllib.request.urlopen(reference["source_url"], timeout=30) as response:
            archive.write_bytes(response.read(16 * 1024 * 1024))
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == reference["source_sha256"]
    with tarfile.open(archive) as source:
        source.extractall(out, filter="data")
    source = out / "hostapd-2.11"
    for name, digest in reference["source_files"].items():
        assert hashlib.sha256((source / name).read_bytes()).hexdigest() == digest, name
    harness = ROOT / "deploy/wire/component-registrar.c"
    executable = out / "component-registrar"
    command = [
        "cc",
        "-O2",
        "-ffunction-sections",
        "-fdata-sections",
        "-DCONFIG_WPS",
        "-DCONFIG_SHA256",
        "-DCONFIG_NO_STDOUT_DEBUG",
        "-DCONFIG_WPS_STRICT",
        "-I",
        str(source / "src"),
        "-I",
        str(source / "src/utils"),
        str(harness),
        *[str(source / name) for name in reference["compiled_sources"]],
        "-Wl,--gc-sections",
        "-lcrypto",
        "-o",
        str(executable),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    (out / "build.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise SystemExit("Registrar build failed; inspect " + str(out / "build.log"))
    provenance = {
        "scope": "synthetic WSC payload peer, not an EasyMesh controller",
        "source_url": reference["source_url"],
        "source_sha256": reference["source_sha256"],
        "harness_sha256": hashlib.sha256(harness.read_bytes()).hexdigest(),
        "binary_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "compiler": subprocess.check_output(["cc", "--version"], text=True).splitlines()[0],
        "openssl": subprocess.check_output(["openssl", "version"], text=True).strip(),
        "source_files": reference["source_files"],
        "fresh_entropy": True,
        "credential_is_public_fixture": True,
    }
    (out / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(executable)


if __name__ == "__main__":
    main()
