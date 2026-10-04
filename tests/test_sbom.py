# SPDX-License-Identifier: Apache-2.0
"""The bill of materials of the C programs (c/packaging/sbom.py) is a complete SPDX 2.3
document: every source and data file of the package with its checksums, the libraries the
programs link, the programs the units run, and every relationship between known elements."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sbom", ROOT / "c" / "packaging" / "sbom.py")
sbom = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sbom)


@pytest.fixture(scope="module")
def doc():
    return sbom.document("0123456789ab", rdk_logger="2.4.0", created="2026-10-03T00:00:00Z")


def test_document_fields(doc):
    assert doc["spdxVersion"] == "SPDX-2.3"
    assert doc["dataLicense"] == "CC0-1.0"
    assert doc["SPDXID"] == "SPDXRef-DOCUMENT"
    assert doc["documentNamespace"].endswith("-0123456789ab")
    assert doc["creationInfo"]["created"] == "2026-10-03T00:00:00Z"
    json.dumps(doc)  # serialisable as it stands


def test_every_source_and_data_file_with_checksums(doc):
    names = {f["fileName"] for f in doc["files"]}
    for path in [
        *ROOT.glob("c/src/*.c"),
        *ROOT.glob("c/src/*.h"),
        *ROOT.glob("schemas/*.schema.json"),
    ]:
        assert "./" + path.relative_to(ROOT).as_posix() in names
    for name in ("./c/CMakeLists.txt", "./deploy/adapter/files/emosa-agent-link"):
        assert name in names
    for f in doc["files"]:
        data = (ROOT / f["fileName"][2:]).read_bytes()
        sums = {c["algorithm"]: c["checksumValue"] for c in f["checksums"]}
        assert sums == {
            "SHA1": hashlib.sha1(data).hexdigest(),
            "SHA256": hashlib.sha256(data).hexdigest(),
        }


def test_verification_code(doc):
    main = next(p for p in doc["packages"] if p["name"] == "emosa-c")
    sha1s = sorted(
        next(c["checksumValue"] for c in f["checksums"] if c["algorithm"] == "SHA1")
        for f in doc["files"]
    )
    expected = hashlib.sha1("".join(sha1s).encode()).hexdigest()
    assert main["packageVerificationCode"]["packageVerificationCodeValue"] == expected
    assert main["versionInfo"].endswith("+0123456789ab")
    assert main["licenseDeclared"] == "Apache-2.0"
    files = {f["fileName"]: f for f in doc["files"]}
    assert files["./LICENSE"]["licenseConcluded"] == "Apache-2.0"
    assert files["./c/src/agent.c"]["licenseInfoInFiles"] == ["Apache-2.0"]  # it says so itself
    assert files["./schemas/agent-config.schema.json"]["licenseInfoInFiles"] == ["NONE"]


def test_libraries_and_runtime(doc):
    by_name = {p["name"]: p for p in doc["packages"]}
    assert by_name["cJSON"]["licenseDeclared"] == "MIT"
    assert by_name["SQLite"]["licenseDeclared"] == "blessing"
    assert by_name["OpenSSL libcrypto"]["licenseDeclared"] in ("Apache-2.0", "OpenSSL")
    assert by_name["rdk-logger"]["versionInfo"] == "2.4.0"
    for name in ("systemd", "iproute2", "bash", "dnsmasq"):
        assert name in by_name
    kinds = {(r["relatedSpdxElement"], r["relationshipType"]) for r in doc["relationships"]}
    assert ("SPDXRef-Package-rdk-logger", "DYNAMIC_LINK") in kinds
    runtime = {
        r["spdxElementId"]
        for r in doc["relationships"]
        if r["relationshipType"] == "RUNTIME_DEPENDENCY_OF"
    }
    assert runtime == {"SPDXRef-Package-" + n for n in ("systemd", "iproute2", "bash", "dnsmasq")}


def test_relationships_name_known_elements(doc):
    ids = (
        {"SPDXRef-DOCUMENT"}
        | {p["SPDXID"] for p in doc["packages"]}
        | {f["SPDXID"] for f in doc["files"]}
    )
    assert len(ids) == 1 + len(doc["packages"]) + len(doc["files"])  # unique
    for r in doc["relationships"]:
        assert r["spdxElementId"] in ids and r["relatedSpdxElement"] in ids
    contained = {
        r["relatedSpdxElement"] for r in doc["relationships"] if r["relationshipType"] == "CONTAINS"
    }
    assert contained == {f["SPDXID"] for f in doc["files"]}
