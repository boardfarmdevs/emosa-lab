"""Copy an explicit synthetic native-run allowlist from the owned VM for review.

Read-only in the VM. Creates a new private local directory; does not publish it
or collect credentials, operation journals, native configurations or full logs.
Run the independent checker and review these files before adding them to Git.
"""

import argparse
import io
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path

FIXED = (
    "result.json",
    "source-hashes.json",
    "withheld-operation.json",
    "native-operation.json",
    "native-session.json",
    "controller-before.json",
    "controller-after.json",
    "manager.jsonl",
    "mqtt-provenance.json",
    "radio.pcap",
    "ethernet.pcap",
    "active-samples.json",
    "recovery-checks.json",
    "client-outages.json",
    "station-events.jsonl",
    "radio-capture.log",
    "ethernet-capture.log",
    "netlink.pcap",
    "netlink-capture.log",
    "medium.cfg",
    "medium-build.json",
    "medium.log",
)
PATTERNS = (
    "clients-*.json",
    "active-inventory-*.json",
    "detached-inventory-*.json",
    "detached-session-*.json",
    "em-baseline-*-continuous-ping.log",
)


def collect(label, output, candidate="candidate-onboarding-01"):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", label):
        raise ValueError("expected an existing run label of 1–24 lowercase letters/digits/hyphens")
    if output.exists() or output.is_symlink():
        raise ValueError("preserve existing evidence; choose a new review directory")
    if not re.fullmatch(r"candidate-[a-z0-9-]{1,64}", candidate):
        raise ValueError("expected a candidate directory name under /opt/emosa-baseline")
    # Constants and label are separate argv values, never interpolated shell code.
    code = """
import json, sys, tarfile
from pathlib import Path
label, fixed, patterns = sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3])
root = Path('/opt/emosa-radio-manager/runs') / label
assert json.loads((root / 'native-owner.json').read_text()) == {
    'owner': 'emosa-native-onboarding-v1', 'backend': 'ovsdb-sim'}
paths = {name: root / name for name in fixed if (root / name).is_file()}
for pattern in patterns:
    paths.update({p.name: p for p in root.glob(pattern)})
base = Path('/opt/emosa-baseline')
paths['candidate.json'] = base / sys.argv[4] / 'candidate.json'
paths['candidate-trial.json'] = base / 'controller-candidates' / label / 'result.json'
assert all(p.is_file() and not p.is_symlink() for p in paths.values())
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
    for name, path in sorted(paths.items()):
        archive.add(path, arcname=name, recursive=False)
"""
    data = subprocess.check_output(
        [
            "lxc",
            "exec",
            "emosa-lab",
            "--",
            "python3",
            "-c",
            code,
            label,
            json.dumps(FIXED),
            json.dumps(PATTERNS),
            candidate,
        ],
        timeout=60,
    )
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = archive.getmembers()
        if any(
            not m.isfile() or Path(m.name).name != m.name or m.size > 256 * 1024 * 1024
            for m in members
        ):
            raise ValueError("unexpected review archive member")
        if len({m.name for m in members}) != len(members):
            raise ValueError("duplicate review archive name")
        os.umask(0o077)
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        for member in members:
            with (output / member.name).open("xb") as target:
                target.write(archive.extractfile(member).read())
    print(f"Collected {len(members)} allowlisted files into {output}; review still required")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label")
    parser.add_argument("output", type=Path)
    parser.add_argument("--candidate", default="candidate-onboarding-01")
    args = parser.parse_args()
    collect(args.label, args.output, args.candidate)
