"""Reproduce the exact Ubuntu kernel source subset used for counter review.

Downloads pinned public source archives (about 239 MB) into a reusable cache.
Extracts only selected source files and applies their distribution patch in a
new review directory. Does not build, install or alter a running kernel.
"""

import argparse
import gzip
import hashlib
import json
import re
import subprocess
import tarfile
import urllib.request
from pathlib import Path

BASE = "https://archive.ubuntu.com/ubuntu/pool/main/l/linux/"
INPUTS = {
    "linux_6.8.0-139.139.dsc": "40fb1a41e084f8f923cbea43fe2ffc491e7bd3c247672018f0aa97ef3e352558",
    "linux_6.8.0.orig.tar.gz": "26512115972bdf017a4ac826cc7d3e9b0ba397d4f85cd330e4e4ff54c78061c8",
    "linux_6.8.0-139.139.diff.gz": (
        "c18cf1db6c7ed0ad5cf7d651de117b415f17e05baaeefb13a4a2306e94e9ae57"
    ),
}
FILES = {
    "drivers/net/wireless/virtual/mac80211_hwsim.h": (
        "6c6d6b21e62670f575d46b572aae324fc186d2d7808e6c6cd313a97a7a170851"
    ),
    "include/net/mac80211.h": ("d6b9917f6f1b340b48e533ffdcaa585b1324098f390bfc3011788a29c4ac470a"),
    "net/mac80211/main.c": "6f1811cd0352c56aee4babece8402adf79419e69298ccf4316eb707df480aca2",
    "drivers/net/wireless/virtual/mac80211_hwsim.c": (
        "901123c8e88d0e90881e4f56492c674ade65a5d976ad552318667acfc1d4ef81"
    ),
    "include/uapi/linux/nl80211.h": (
        "9535f7ae11376801e3586ac1c237e706e296bb8885568baca08c967cb024b7f7"
    ),
    "net/mac80211/rx.c": "29d829200038202d4484f65895bb75938bbc49ea886f96f2942eb7ad52718a82",
    "net/mac80211/sta_info.c": "047f3ef6aeae812e5748326bedc2120e24cd4a4b3bb4ef835aa0922b711bb866",
    "net/mac80211/status.c": "5970acc09c31ccc47677ac68ac2616af3f662717346d6e7aad540d0d97a24cca",
    "net/mac80211/tx.c": "9ae7f3ece76ab8c532f8b47251e119c727f1504ef8156a9c6ba8dac98afcb4a6",
    "net/wireless/nl80211.c": "a03a4a81eefd24b6056a953a7975b7ce123af714885160a501e3002b7fa5a261",
}


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            result.update(chunk)
    return result.hexdigest()


def review(cache, output):
    if output.exists() or output.is_symlink():
        raise ValueError("choose a new source-review directory")
    cache.mkdir(parents=True, exist_ok=True)
    for name, expected in INPUTS.items():
        path = cache / name
        if not path.exists():
            with urllib.request.urlopen(BASE + name, timeout=45) as remote, path.open("xb") as out:
                for chunk in iter(lambda: remote.read(1048576), b""):
                    out.write(chunk)
        if path.is_symlink() or digest(path) != expected:
            raise ValueError("cached source differs from pinned input: " + name)
    output.mkdir(parents=True, exist_ok=False)
    selected = output / "source"
    found = set()
    with tarfile.open(cache / "linux_6.8.0.orig.tar.gz", "r|gz") as archive:
        for member in archive:
            if not member.name.startswith("linux-6.8/"):
                continue
            relative = member.name.removeprefix("linux-6.8/")
            if relative not in FILES:
                continue
            if not member.isfile() or relative in found:
                raise ValueError("unexpected selected archive member")
            path = selected / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.extractfile(member).read())
            found.add(relative)
    if found != set(FILES):
        raise ValueError("incomplete source archive")
    with gzip.open(cache / "linux_6.8.0-139.139.diff.gz", "rt") as stream:
        blocks = re.split(r"(?m)(?=^--- linux-6\.8\.0\.orig/)", stream.read())
    changes = []
    for block in blocks:
        if block.startswith("--- linux-6.8.0.orig/"):
            name = block.splitlines()[0].split("orig/", 1)[1].split("\t")[0]
            if name in FILES:
                changes.append(block)
    patch = output / "selected.patch"
    patch.write_text("".join(changes))
    subprocess.run(
        ["patch", "--batch", "--fuzz=0", "-p1", "-i", str(patch.resolve())],
        cwd=selected,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    for name, expected in FILES.items():
        if digest(selected / name) != expected:
            raise ValueError("patched source differs from reviewed source: " + name)
    result = {
        "package": "linux",
        "version": "6.8.0-139.139",
        "runtime_kernel": "6.8.0-139-generic",
        "source_base": BASE,
        "input_sha256": INPUTS,
        "patched_source_sha256": FILES,
        "scope": "Selected source reconstruction and digest check; no kernel build or installation",
        "source_revision_verified": True,
        "final_counter_source_qualified": False,
    }
    (output / "source-provenance.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--cache", type=Path, default=Path(".cache/upstream/linux-6.8.0-139.139"))
    args = parser.parse_args()
    print(json.dumps(review(args.cache, args.output), indent=2))
