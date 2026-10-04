#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Put back the evidence files kept outside git, checked.

The large raw captures and recordings of the evidence records (pcap and jsonl files over
256 KB) live in a release of this repository, not in git. Their paths, sizes and SHA-256
are in docs/records/evidence/external.json. This downloads the release's archive once
(cached), checks it, and writes each missing or different file back in place:

    python3 scripts/fetch-evidence.py            fetch what is missing, then check all
    python3 scripts/fetch-evidence.py --check    only report; exit 1 if anything is missing

The tests that read these files stop with a pointer to this script until it has run.
"""

import argparse
import hashlib
import json
import os
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/records/evidence/external.json"
CACHE_HOME = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
CACHE = CACHE_HOME / "emosa-lab" / "evidence"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def differing(manifest):
    """The manifest entries whose file is missing, or not the recorded size and digest."""
    wrong = []
    for entry in manifest["files"]:
        path = ROOT / entry["path"]
        if (
            not path.is_file()
            or path.stat().st_size != entry["size"]
            or sha256(path) != entry["sha256"]
        ):
            wrong.append(entry)
    return wrong


def archive(manifest):
    """The release archive, from the cache or downloaded into it, its digest checked."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / f"{manifest['asset_sha256']}-{manifest['asset']}"
    if cached.is_file() and sha256(cached) == manifest["asset_sha256"]:
        return cached
    print(f"downloading {manifest['url']} ({manifest['asset_size'] / 2**20:.1f} MB)")
    with (
        tempfile.NamedTemporaryFile(dir=CACHE, delete=False) as partial,
        urllib.request.urlopen(manifest["url"], timeout=120) as response,
    ):
        for chunk in iter(lambda: response.read(1 << 20), b""):
            partial.write(chunk)
    if sha256(partial.name) != manifest["asset_sha256"]:
        os.unlink(partial.name)
        raise SystemExit(f"{manifest['asset']}: the download does not match its recorded digest")
    os.replace(partial.name, cached)
    return cached


def place(source, wanted):
    """Write the wanted files from the archive; nothing else in it is read."""
    by_path = {entry["path"]: entry for entry in wanted}
    with tarfile.open(source, "r:xz") as tar:
        for member in tar:
            entry = by_path.pop(member.name, None)
            if entry is None:
                continue
            if not member.isfile():
                raise SystemExit(f"{member.name}: not a regular file in the archive")
            target = ROOT / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as stream, open(target, "wb") as output:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    output.write(chunk)
    if by_path:
        raise SystemExit(f"not in the archive: {sorted(by_path)[:3]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="only report what is missing or differs"
    )
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    wrong = differing(manifest)
    if args.check or not wrong:
        total = len(manifest["files"])
        print(f"evidence outside git: {total - len(wrong)} of {total} in place")
        for entry in wrong[:5]:
            print(f"  missing or different: {entry['path']}")
        return 1 if wrong else 0
    place(archive(manifest), wrong)
    still = differing(manifest)
    if still:
        raise SystemExit(f"{len(still)} files still differ after fetching, e.g. {still[0]['path']}")
    print(
        f"evidence outside git: {len(wrong)} files put back; all {len(manifest['files'])} in place"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
