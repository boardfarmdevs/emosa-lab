#!/usr/bin/env python3
"""Owned clean VM/container reproduction. Run on the development LXD host."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER = "secure-fleet-reproduction"
VM_IMAGE = "805f57b68c5eac9ef90fc2d0c435e01ad15ba98d57054b83ad83dac2ebc2f1e5"
BASE_IMAGE = "cc6b6e557372769f5b0d0945d61f200360818a408f9d0dddadbbab584d2fb414"
BUILD = "emosa-runtime-build"
RUN = "emosa-runtime-run"
ALIAS = "emosa-secure-runtime"


def command(*args, capture=False):
    return subprocess.run(
        list(map(str, args)), check=True, text=True, stdout=subprocess.PIPE if capture else None
    ).stdout


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("preflight", "create", "build", "publish", "run", "cleanup")
    )
    parser.add_argument("--vm", default="emosa-reliability")
    parser.add_argument("--image-alias", default=ALIAS)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--base-export", type=Path)
    parser.add_argument("--soak-cycles", type=int, default=12)
    args = parser.parse_args()
    if not re.fullmatch(r"emosa-reliability(?:-[a-z0-9-]+)?", args.vm):
        parser.error("VM name must start with emosa-reliability")
    if not re.fullmatch(r"emosa-secure-runtime(?:-[a-z0-9-]+)?", args.image_alias):
        parser.error("Image alias must start with emosa-secure-runtime")
    if not 1 <= args.soak_cycles <= 100:
        parser.error("soak cycles must be 1..100")
    os.umask(0o077)
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    instances = json.loads(command("lxc", "list", "--format=json", capture=True))
    existing = next((i for i in instances if i["name"] == args.vm), None)
    if existing and existing["config"].get("user.emosa-purpose") != OWNER:
        raise SystemExit("Refusing an instance without this experiment's ownership marker")

    def outer(*cmd, capture=False):
        return command("lxc", "exec", args.vm, "--", *cmd, capture=capture)

    def inner(*cmd, capture=False):
        return outer("/snap/bin/lxc", *cmd, capture=capture)

    def pull(source, target):
        outer("mkdir", "-p", "/root/emosa-export")
        inner("file", "pull", source, "/root/emosa-export/" + target)
        command("lxc", "file", "pull", f"{args.vm}/root/emosa-export/{target}", directory / target)

    if args.action == "preflight":
        info = {
            "vm": args.vm,
            "exists": existing is not None,
            "base_vm_image": VM_IMAGE,
            "base_container_image": BASE_IMAGE,
            "host_free_bytes": shutil.disk_usage(directory).free,
            "lxd_version": command("lxc", "version", capture=True),
            "storage": command("lxc", "storage", "info", "default", capture=True),
            "required": (
                "KVM; 4 GiB available RAM; 14 GiB VM disk; >=10 GiB pool and host export headroom"
            ),
            "touches_radio_lab": False,
        }
        (directory / "preflight.json").write_text(json.dumps(info, indent=2) + "\n")
        print(json.dumps(info, indent=2))
    elif args.action == "create":
        if existing:
            raise SystemExit("Refusing to replace existing VM")
        if args.base_export is None:
            parser.error(
                "create requires --base-export pointing to the retained split container export"
            )
        base = args.base_export.resolve()
        if (
            digest(base) != "3b437480022ddaa2860043d6b6ef41189c989de72d9308513ef26f1a5c5a1229"
            or digest(Path(str(base) + ".root"))
            != "49348feddff03cdbaac97f24497b872752be64cdddff44ebac793c649c6b02bd"
        ):
            raise SystemExit("Base export digest mismatch")
        command(
            "lxc",
            "launch",
            VM_IMAGE,
            args.vm,
            "--vm",
            "-c",
            "limits.cpu=2",
            "-c",
            "limits.memory=4GiB",
            "-c",
            f"user.emosa-purpose={OWNER}",
            "-d",
            "root,size=14GiB",
        )
        for attempt in range(60):
            try:
                outer("true")
                break
            except subprocess.CalledProcessError:
                if attempt == 59:
                    raise
                time.sleep(2)
        outer("cloud-init", "status", "--wait")
        outer("snap", "install", "lxd", "--revision=40585", "--channel=5.21/stable")
        outer("snap", "refresh", "--hold=24h", "lxd")
        outer("/snap/bin/lxd", "init", "--auto", "--storage-backend=dir")
        command("lxc", "file", "push", base, f"{args.vm}/root/ubuntu24-container")
        command(
            "lxc", "file", "push", str(base) + ".root", f"{args.vm}/root/ubuntu24-container.root"
        )
        inner("image", "import", "/root/ubuntu24-container", "/root/ubuntu24-container.root")
    else:
        if not existing or existing["status"] != "Running":
            raise SystemExit("Owned VM must be running; inspect preflight first")
        # A completed `lxc start` can precede VM-agent/nested-daemon readiness.
        # Only retry this read; never repeat a mutation after an uncertain result.
        for attempt in range(60):
            try:
                containers = json.loads(inner("list", "--format=json", capture=True))
                break
            except subprocess.CalledProcessError:
                if attempt == 59:
                    raise
                time.sleep(2)
        for name in (BUILD, RUN):
            found = next((c for c in containers if c["name"] == name), None)
            if found and found["config"].get("user.emosa-purpose") != "secure-fleet-runtime":
                raise SystemExit("Refusing unowned inner container: " + name)
        if args.action == "build":
            if any(c["name"] == BUILD for c in containers):
                raise SystemExit(
                    "Build container already exists; use a fresh owned build container"
                )
            bundle = directory / "bundle"
            bundle.mkdir(exist_ok=False)
            command("uv", "build", "--wheel", "--out-dir", bundle, ROOT)
            command(
                "uv",
                "export",
                "--frozen",
                "--no-emit-project",
                "--all-groups",
                "--project",
                ROOT,
                "--output-file",
                bundle / "requirements.txt",
                "--quiet",
            )
            shutil.copy2(shutil.which("uv"), bundle / "uv")
            for path in (
                "scripts/build-ovsdb.sh",
                "tests/test_tls_listener.py",
                "deploy/reliability/prepare-runtime.sh",
            ):
                target = bundle / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / path, target)
            archive = ROOT / ".cache/upstream/openvswitch-4.0.0.tar.gz"
            target = bundle / ".cache/upstream" / archive.name
            target.parent.mkdir(parents=True)
            shutil.copy2(archive, target)
            (bundle / "SHA256SUMS").write_text(
                "".join(
                    f"{digest(p)}  {p.relative_to(bundle)}\n"
                    for p in sorted(bundle.rglob("*"))
                    if p.is_file()
                )
            )
            with tarfile.open(directory / "bundle.tar", "w") as tar:
                tar.add(bundle, arcname="emosa-bundle")
            inner(
                "launch",
                BASE_IMAGE,
                BUILD,
                "-c",
                "user.emosa-purpose=secure-fleet-runtime",
                "-c",
                "limits.memory=3GiB",
            )
            command(
                "lxc", "file", "push", directory / "bundle.tar", f"{args.vm}/root/emosa-bundle.tar"
            )
            inner("file", "push", "/root/emosa-bundle.tar", f"{BUILD}/root/emosa-bundle.tar")
            inner("exec", BUILD, "--", "tar", "-xf", "/root/emosa-bundle.tar", "-C", "/root")
            inner(
                "exec",
                BUILD,
                "--",
                "bash",
                "/root/emosa-bundle/deploy/reliability/prepare-runtime.sh",
            )
        elif args.action == "publish":
            inner("exec", BUILD, "--", "test", "-f", "/opt/emosa-evidence/ovs-sha256.txt")
            inner("exec", BUILD, "--", "test", "!", "-e", "/home/emosa/runs")
            inner("stop", BUILD)
            inner("publish", BUILD, "--alias", args.image_alias)
            info = json.loads(
                inner("image", "list", args.image_alias, "--format=json", capture=True)
            )[0]
            stem = "runtime-" + info["fingerprint"]
            outer("mkdir", "-p", "/root/emosa-export")
            inner("image", "export", args.image_alias, "/root/emosa-export/" + stem)
            names = outer(
                "find",
                "/root/emosa-export",
                "-maxdepth",
                "1",
                "-name",
                stem + "*",
                "-type",
                "f",
                capture=True,
            ).splitlines()
            exports = []
            for name in names:
                path = directory / Path(name).name
                command("lxc", "file", "pull", args.vm + name, path)
                exports.append(
                    {"filename": path.name, "sha256": digest(path), "size": path.stat().st_size}
                )
            (directory / "runtime-image.json").write_text(
                json.dumps(
                    {
                        "image": info,
                        "exports": exports,
                        "published_before_experiments": True,
                        "contains_pod_credentials": False,
                    },
                    indent=2,
                )
                + "\n"
            )
        elif args.action == "run":
            if any(c["name"] == RUN for c in containers):
                raise SystemExit(
                    "Run container exists; retain evidence then use cleanup before rerunning"
                )
            inner(
                "launch",
                args.image_alias,
                RUN,
                "-c",
                "user.emosa-purpose=secure-fleet-runtime",
                "-c",
                "limits.memory=3GiB",
            )
            uid = inner("exec", RUN, "--", "id", "-u", "emosa", capture=True).strip()
            gid = inner("exec", RUN, "--", "id", "-g", "emosa", capture=True).strip()
            identity = inner("exec", RUN, "--user", uid, "--group", gid, "--", "id", capture=True)
            (directory / "runtime-identity.txt").write_text(identity)

            def execute(*cmd):
                inner(
                    "exec",
                    RUN,
                    "--user",
                    uid,
                    "--group",
                    gid,
                    "--env",
                    "EMOSA_OVS_BIN=/opt/emosa-ovs",
                    "--cwd",
                    "/home/emosa",
                    "--",
                    *cmd,
                )

            execute(
                "/opt/emosa-runtime/bin/python",
                "-I",
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-c",
                "/opt/emosa-evidence/pytest.ini",
                "/opt/emosa-tests/test_tls_listener.py",
                "-q",
                "--junitxml=/home/emosa/tls-results.xml",
            )
            pull(f"{RUN}/home/emosa/tls-results.xml", "tls-results.xml")
            for count in (4, 8, 16, 32):
                execute(
                    "/opt/emosa-runtime/bin/python",
                    "-I",
                    "-m",
                    "emosa.simulation.reliability",
                    "--directory",
                    f"/home/emosa/runs/fleet-{count}",
                    "--pods",
                    str(count),
                    "--cycles",
                    "1",
                )
                pull(f"{RUN}/home/emosa/runs/fleet-{count}/report.json", f"fleet-{count}.json")
            execute(
                "/opt/emosa-runtime/bin/python",
                "-I",
                "-m",
                "emosa.simulation.reliability",
                "--directory",
                "/home/emosa/runs/soak",
                "--pods",
                "4",
                "--cycles",
                str(args.soak_cycles),
                "--interval",
                "5",
            )
            pull(f"{RUN}/home/emosa/runs/soak/report.json", "soak.json")
            for name in (
                "packages.tsv",
                "python-packages.txt",
                "ovs-sha256.txt",
                "python-version.txt",
                "SHA256SUMS",
            ):
                pull(f"{RUN}/opt/emosa-evidence/{name}", name)
            (directory / "lxd-version.txt").write_text(outer("snap", "list", "lxd", capture=True))
        elif args.action == "cleanup":
            # Keep build container/image/export for repeatability; delete only the owned run.
            if any(c["name"] == RUN for c in containers):
                inner("delete", RUN, "--force")
            command("lxc", "stop", args.vm)
            print("Run container removed; VM stopped; build container/image and exports retained.")


if __name__ == "__main__":
    main()
