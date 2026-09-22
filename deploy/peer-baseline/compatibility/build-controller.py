"""Build an isolated native counter-capability candidate; never install in a peer.

Run on an x86_64 Ubuntu 22.04 HOST with g++, make, patch, git, strip and cmake.
The verified source and install archives contain the original 22 lab patches.
Only controller sources are rebuilt; all native shared libraries remain pinned.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARCHIVES = {
    "prplmesh-patched-source.tar.gz": (
        "ebc771d43279cdb6b1cf5e2c09eb51d66ef3a8afab6b97f4561439d2606ac549"
    ),
    "prpl-install-nl80211-6.0.0.tar.gz": (
        "a98a6903fb9db1494ece0c292dd85c7d4d8ff2f570b5b85768ed6f299f72fb1f"
    ),
    "prpl-runtime-deps-6.0.0.tar.gz": (
        "ad912dc63b7d2188096bd6c0a42978590c4a4b82b0590da1198514e122cec4d2"
    ),
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True, help="New private absolute directory")
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=3)
    args = parser.parse_args()
    if not args.build.is_absolute() or args.build.exists():
        parser.error("Build directory must be new and absolute")
    for name, expected in ARCHIVES.items():
        if digest(args.artifacts / name) != expected:
            raise SystemExit(f"Archive digest mismatch: {name}")
    os.umask(0o077)
    args.build.mkdir(parents=True, exist_ok=False)
    source, install = args.build / "source", args.build / "prpl-install-nl80211"
    source.mkdir()
    # These exact, locally reviewed archives have already passed their hash checks.
    with tarfile.open(args.artifacts / "prplmesh-patched-source.tar.gz") as archive:
        # The upstream source archive carries an unused parent-directory toolchain
        # symlink. The overlay uses the host compiler, not that external config.
        archive.extractall(
            source,
            members=[m for m in archive if m.name != "./external_toolchain.cfg"],
            filter="data",
        )
    with tarfile.open(args.artifacts / "prpl-install-nl80211-6.0.0.tar.gz") as archive:
        archive.extractall(
            args.build,
            members=[
                m
                for m in archive
                if m.name.startswith(("prpl-install-nl80211/include/", "prpl-install-nl80211/lib/"))
            ],
            filter="data",
        )
    with tarfile.open(args.artifacts / "prpl-runtime-deps-6.0.0.tar.gz") as archive:
        archive.extractall(args.build, filter="data")
    headers = args.build / "headers"
    headers.mkdir()
    lock = json.loads((HERE / "controller-headers.json").read_text())
    with (args.build / "build.log").open("w") as log:

        def run(*command, **kwargs):
            return subprocess.run(
                command, check=True, stdout=log, stderr=log, timeout=900, **kwargs
            )

        for row in lock["libraries"]:
            target = headers / row["name"]
            run("git", "init", "--quiet", str(target))
            run(
                "git",
                "-C",
                str(target),
                "fetch",
                "--quiet",
                "--depth=1",
                row["repository"],
                row["commit"],
            )
            run("git", "-C", str(target), "checkout", "--quiet", "--detach", "FETCH_HEAD")
            actual = subprocess.check_output(
                ["git", "-C", str(target), "rev-parse", "HEAD"], text=True
            )
            if actual.strip() != row["commit"]:
                raise RuntimeError("Dependency checkout differs from header lock")
        patch = HERE.parent / "patches/0004-controller-counter-capability.patch"
        with patch.open() as content:
            run("patch", "--batch", "--fuzz=0", "-p1", "-d", str(source), stdin=content)
        run(
            "cmake",
            "-S",
            str(HERE.parent / "controller-overlay"),
            "-B",
            str(args.build / "build"),
            "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
            f"-DPEER_SOURCE={source}",
            f"-DPEER_INSTALL={install}",
            f"-DHEADERS={headers}",
            f"-DRUNTIME={args.build}",
        )
        run("cmake", "--build", str(args.build / "build"), "--parallel", str(args.jobs))
        run("cmake", "--install", str(args.build / "build"), "--prefix", str(args.build / "stage"))
        candidate = args.build / "stage/bin/beerocks_controller"
        unstripped_sha = digest(candidate)
        run("strip", "--strip-debug", str(candidate))
        environment = os.environ | {
            "LD_LIBRARY_PATH": ":".join(
                str(p)
                for p in (
                    install / "lib",
                    args.build / "usr/lib/x86_64-linux-gnu",
                    args.build / "usr/lib",
                )
            )
        }
        regression = subprocess.check_output(
            [str(args.build / "build/emosa-counter-regression")],
            env=environment,
            text=True,
            timeout=30,
        )
        (args.build / "regression.json").write_text(regression)
        if json.loads(regression)["passed"] != 12:
            raise RuntimeError("Native counter regression did not pass")
    report = {
        "scope": "Native controller counter advertisement candidate; not admitted onboarding",
        "input_archives": ARCHIVES,
        "header_dependencies": lock,
        "patch_sha256": digest(patch),
        "candidate_sha256": digest(candidate),
        "unstripped_candidate_sha256": unstripped_sha,
        "source_sha256": {
            p: digest(source / p)
            for p in (
                "controller/src/beerocks/master/controller.cpp",
                "controller/src/beerocks/master/db/db.cpp",
            )
        },
        "recipe_sha256": {
            p.name: digest(p)
            for p in (
                Path(__file__),
                HERE / "counter-units.cpp",
                HERE / "controller-headers.json",
                HERE.parent / "controller-overlay/CMakeLists.txt",
            )
        },
        "tools": {
            name: subprocess.check_output([name, "--version"], text=True).splitlines()[0]
            for name in ("cmake", "g++", "git", "strip")
        },
        "counter_regression": json.loads(regression),
        "installed_in_peer": False,
    }
    (args.build / "candidate.json").write_text(json.dumps(report, indent=2) + "\n")
    shutil.copyfile(HERE / "controller-headers.json", args.build / "controller-headers.json")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
