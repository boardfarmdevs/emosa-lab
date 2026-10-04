#!/usr/bin/env python3
"""The bill of materials of EMOSA's C programs as an SPDX 2.3 document (JSON).

    c/packaging/sbom.py OUT.spdx.json [--revision REV] [--rdk-logger VERSION]

It lists every file of this repository that goes into the programs or their package
(the sources, the build file, the units and helper, the schemas and pod profiles), each
with its SHA-1 and SHA-256; the libraries the programs link, with the versions
pkg-config reports where they are built (PKG_CONFIG names another pkg-config); and the
programs the package's units run. EMOSA's own license is NOASSERTION until its owner
grants one. The document is deterministic: its time is SOURCE_DATE_EPOCH, else the
revision's commit time.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# what goes into the programs and their package (CMake EMOSA_INSTALL_DATA)
FILES = (
    "c/CMakeLists.txt",
    "c/src/*.c",
    "c/src/*.h",
    "c/src/*.in",
    "c/packaging/default-emosa",
    "c/packaging/systemd/*.in",
    "deploy/adapter/files/emosa-agent-link",
    "deploy/adapter/files/fleet.example.json",
    "deploy/adapter/files/gtp.example.json",
    "schemas/*.schema.json",
    "src/emosa/profiles/*.json",
)

# the libraries the programs link: (name, pkg-config module, where, SPDX license, purl)
LIBRARIES = (
    (
        "cJSON",
        "libcjson",
        "https://github.com/DaveGamble/cJSON",
        "MIT",
        "pkg:github/DaveGamble/cJSON@{v}",
    ),
    (
        "OpenSSL libcrypto",
        "libcrypto",
        "https://www.openssl.org/source/",
        None,
        "pkg:generic/openssl@{v}",
    ),
    (
        "SQLite",
        "sqlite3",
        "https://www.sqlite.org/download.html",
        "blessing",
        "pkg:generic/sqlite@{v}",
    ),
)

# the programs the package's units and helper run: (name, where, SPDX license)
RUNTIME = (
    ("systemd", "https://github.com/systemd/systemd", "LGPL-2.1-or-later"),
    ("iproute2", "https://git.kernel.org/pub/scm/network/iproute2/iproute2.git", "GPL-2.0-only"),
    ("bash", "https://www.gnu.org/software/bash/", "GPL-3.0-or-later"),
    ("dnsmasq", "https://thekelleys.org.uk/dnsmasq/doc.html", "GPL-2.0-only OR GPL-3.0-only"),
)


def git(*args):
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def release():
    text = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("pyproject.toml has no version")
    return match.group(1)


def pkg_config_version(module):
    try:
        result = subprocess.run(
            [os.environ.get("PKG_CONFIG", "pkg-config"), "--modversion", module],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def files():
    found = sorted({p for pattern in FILES for p in ROOT.glob(pattern) if p.is_file()})
    for path in found:
        data = path.read_bytes()
        yield (
            path.relative_to(ROOT).as_posix(),
            hashlib.sha1(data).hexdigest(),
            hashlib.sha256(data).hexdigest(),
        )


def spdx_id(text):
    return re.sub(r"[^A-Za-z0-9.-]", "-", text)


def document(revision, rdk_logger=None, created=None):
    version = release()
    listed = list(files())
    # SPDX 2.3 §7.9: SHA-1 of the sorted file SHA-1s
    code = hashlib.sha1("".join(sorted(sha1 for _, sha1, _ in listed)).encode()).hexdigest()
    package = "SPDXRef-Package-emosa-c"
    packages = [
        {
            "SPDXID": package,
            "name": "emosa-c",
            "versionInfo": f"{version}+{revision}",
            "supplier": "NOASSERTION",
            "downloadLocation": f"git+https://github.com/boardfarmdevs/emosa-lab.git@{revision}",
            "filesAnalyzed": True,
            "packageVerificationCode": {"packageVerificationCodeValue": code},
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "copyrightText": "NOASSERTION",
            "primaryPackagePurpose": "APPLICATION",
            "summary": "EMOSA: OpenSync pods as EasyMesh agents (the agent, the fleet, the GTP)",
        }
    ]
    relationships = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": package,
        }
    ]
    spdx_files = []
    for name, sha1, sha256 in listed:
        ident = "SPDXRef-File-" + spdx_id(name)
        spdx_files.append(
            {
                "SPDXID": ident,
                "fileName": "./" + name,
                "checksums": [
                    {"algorithm": "SHA1", "checksumValue": sha1},
                    {"algorithm": "SHA256", "checksumValue": sha256},
                ],
                "licenseConcluded": "NOASSERTION",
                "copyrightText": "NOASSERTION",
            }
        )
        relationships.append(
            {"spdxElementId": package, "relationshipType": "CONTAINS", "relatedSpdxElement": ident}
        )
    libraries = list(LIBRARIES)
    if rdk_logger:
        libraries.append(
            (
                "rdk-logger",
                None,
                "https://github.com/rdkcentral/rdk_logger",
                "Apache-2.0",
                "pkg:github/rdkcentral/rdk_logger@{v}",
            )
        )
    for name, module, where, license_id, purl in libraries:
        v = rdk_logger if module is None else pkg_config_version(module)
        if v == "NOASSERTION":  # linked, its version not known where it was built
            v = None
        if license_id is None:  # OpenSSL: Apache-2.0 from 3.0, its own license before
            license_id = "Apache-2.0" if v and int(v.split(".")[0]) >= 3 else "OpenSSL"
        ident = "SPDXRef-Package-" + spdx_id(name)
        entry = {
            "SPDXID": ident,
            "name": name,
            "versionInfo": v or "NOASSERTION",
            "supplier": "NOASSERTION",
            "downloadLocation": where,
            "filesAnalyzed": False,
            "licenseConcluded": license_id,
            "licenseDeclared": license_id,
            "copyrightText": "NOASSERTION",
            "primaryPackagePurpose": "LIBRARY",
        }
        if v:
            entry["externalRefs"] = [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": purl.format(v=v),
                }
            ]
        packages.append(entry)
        relationships.append(
            {
                "spdxElementId": package,
                "relationshipType": "DYNAMIC_LINK",
                "relatedSpdxElement": ident,
            }
        )
    for name, where, license_id in RUNTIME:
        ident = "SPDXRef-Package-" + spdx_id(name)
        packages.append(
            {
                "SPDXID": ident,
                "name": name,
                "versionInfo": "NOASSERTION",
                "supplier": "NOASSERTION",
                "downloadLocation": where,
                "filesAnalyzed": False,
                "licenseConcluded": license_id,
                "licenseDeclared": license_id,
                "copyrightText": "NOASSERTION",
                "primaryPackagePurpose": "APPLICATION",
            }
        )
        relationships.append(
            {
                "spdxElementId": ident,
                "relationshipType": "RUNTIME_DEPENDENCY_OF",
                "relatedSpdxElement": package,
            }
        )
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"emosa-c-{version}",
        "documentNamespace": f"https://github.com/boardfarmdevs/emosa-lab/spdx/emosa-c-{version}-{revision}",
        "creationInfo": {
            "created": created or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "creators": ["Tool: emosa-lab c/packaging/sbom.py"],
        },
        "packages": packages,
        "files": spdx_files,
        "relationships": relationships,
    }


def creation_time(revision):
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    committed = (
        git("log", "-1", "--format=%ct", revision.removesuffix("+dirty"))
        if revision != "unknown"
        else ""
    )
    if committed:
        return datetime.fromtimestamp(int(committed), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("--revision", help="the source revision (default: git describe)")
    ap.add_argument(
        "--rdk-logger",
        nargs="?",
        const="NOASSERTION",
        metavar="VERSION",
        help="the programs log through rdk-logger (of VERSION)",
    )
    args = ap.parse_args()
    revision = (
        args.revision
        or git("describe", "--always", "--abbrev=12", "--dirty=+dirty", "--match=v[0-9]*")
        or "unknown"
    )
    if not re.fullmatch(r"[0-9A-Za-z.+~-]+", revision):
        raise SystemExit(f"revision {revision!r} unusable")
    doc = document(revision, args.rdk_logger, creation_time(revision))
    args.out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(
        f"{args.out}: {len(doc['files'])} files, {len(doc['packages'])} packages", file=sys.stderr
    )


if __name__ == "__main__":
    main()
