"""Build the pinned, unmodified optional loss simulator in the owned VM.

No download, install, service start or radio change. Preserve the fresh output
directory, source tree and compiler log as provenance, including failed builds.
"""

import argparse
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

COMMIT = "717e5d7fcc23eecbc8e32bd897a8fd4b1e3ba640"


def build(source, output):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args])

    if git("rev-parse", "HEAD").decode().strip() != COMMIT:
        raise ValueError("checkout must be at the selected wmediumd commit")
    if git("status", "--porcelain", "--untracked-files=normal").strip():
        raise ValueError("source checkout must be clean; preserve local changes separately")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    archive = output / "source.tar"
    archive.write_bytes(git("archive", "--format=tar", COMMIT))
    with tarfile.open(archive) as bundle:
        bundle.extractall(output / "source", filter="data")
    sources = {
        str(p.relative_to(output / "source")): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((output / "source").rglob("*"))
        if p.is_file()
    }
    with (output / "build.log").open("x") as log:
        subprocess.run(
            ["make", "-C", str(output / "source/wmediumd"), "-j2"],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=180,
        )
    binary = output / "wmediumd"
    binary.write_bytes((output / "source/wmediumd/wmediumd").read_bytes())
    binary.chmod(0o700)
    manifest = {
        "upstream": "https://github.com/bcopeland/wmediumd",
        "commit": COMMIT,
        "patches": [],
        "source_sha256": sources,
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "compiler": subprocess.check_output(["cc", "--version"], text=True),
        "packages": subprocess.check_output(
            ["dpkg-query", "-W", "libconfig-dev", "libconfig9", "libnl-3-dev", "libnl-genl-3-dev"],
            text=True,
        ),
        "qualified_airtime_or_phy_model": False,
    }
    (output / "build.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.source.resolve(), args.output.resolve())
