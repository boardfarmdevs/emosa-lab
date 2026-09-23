"""Stage the radio experiment in the existing dedicated VM without host installs."""

import argparse
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TARGET = "/opt/emosa-radio-manager"


def command(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=60).stdout


def vm(*args):
    return command("lxc", "--force-local", "--project", "default", "exec", "emosa-lab", "--", *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wsc", action="store_true", help="include the built synthetic WSC peer")
    args = parser.parse_args()
    if args.wsc:
        reference = json.loads((REPO / ".cache/wsc-registrar/provenance.json").read_text())
        for key, path in (
            ("binary_sha256", REPO / ".cache/wsc-registrar/component-registrar"),
            ("harness_sha256", REPO / "deploy/wire/component-registrar.c"),
        ):
            if reference[key] != hashlib.sha256(path.read_bytes()).hexdigest():
                raise SystemExit("Build the current WSC registrar before staging")
    if vm("hostname").strip() != "emosa-lab" or vm("systemd-detect-virt", "--vm").strip() != "kvm":
        raise SystemExit("Expected the existing dedicated VM")
    # Refuse replacement during any active experiment, including a paused AP.
    code = """import pathlib,fcntl,json,subprocess
r=pathlib.Path('/opt/emosa-radio-manager')
locks=[]
for name in ('run.lock','manager.lock'):
 p=r/name
 if p.exists():
  f=p.open('r+');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(f)
for name in ('em-baseline-controller','em-baseline-agent','em-baseline-wired','em-baseline-wifi'):
 def call(*args):
  cmd=['lxc','--force-local','--project','default',*args]
  return subprocess.check_output(cmd,text=True).strip()
 if call('config','get',name,'user.emosa.baseline')!='emosa-standard-agent-baseline-v1':
  raise RuntimeError('Container ownership mismatch')
 if call('exec',name,'--','systemctl','list-units','--state=active,activating,deactivating',
         '--no-legend','--plain','emosa-baseline-*','emosa-radio-manager-*'):
  raise RuntimeError('A lab service is still active')
"""
    vm("python3", "-c", code)
    vm("test", "-x", "/opt/emosa/.venv/bin/python")
    for binary in ("ovsdb-server", "ovsdb-tool"):
        if "4.0.0" not in vm("/opt/native-" + binary, "--version"):
            raise SystemExit("Expected the retained Open vSwitch 4.0.0 tools")
    archive = REPO / ".cache/radio-manager-source.tar"
    archive.parent.mkdir(exist_ok=True)
    with tarfile.open(archive, "w") as bundle:
        for path in sorted((REPO / "src/emosa").rglob("*.py")):
            bundle.add(path, arcname="source/" + str(path.relative_to(REPO / "src")))
        for path in sorted((REPO / "deploy/radio-manager").glob("*.py")):
            bundle.add(path, arcname=path.name)
        if args.wsc:
            bundle.add(REPO / "deploy/wire/check-endpoint.py", arcname="wire-endpoint.py")
            for name in ("component-registrar", "provenance.json"):
                bundle.add(REPO / ".cache/wsc-registrar" / name, arcname="registrar/" + name)
            bundle.add(
                REPO / ".cache/wsc-registrar/hostapd-2.11/COPYING",
                arcname="registrar/COPYING.hostap",
            )
        bundle.add(REPO / "schemas", arcname="schemas")
        bundle.add(REPO / "tests/fixtures/opensync", arcname="tests/fixtures/opensync")
        bundle.add(REPO / "deploy/peer-baseline/reference.json", arcname="peer-reference.json")
        for path in sorted((REPO / "deploy/peer-baseline").glob("*.py")):
            bundle.add(path, arcname="peer-baseline/" + path.name)
        bundle.add(
            REPO / "deploy/peer-baseline/reference.json", arcname="peer-baseline/reference.json"
        )
        bundle.add(REPO / "doc/protocol/protocol-matrix.json", arcname="protocol-matrix.json")
    vm("mkdir", "-p", "-m", "700", TARGET)
    command(
        "lxc",
        "--force-local",
        "--project",
        "default",
        "file",
        "push",
        "--quiet",
        str(archive),
        "emosa-lab" + TARGET + "/source-input.tar",
    )
    # Check again and retain the locks through extraction, after the transfer.
    vm(
        "python3",
        "-c",
        code + "\nsubprocess.run(['tar','-xf',str(r/'source-input.tar'),'-C',str(r)],check=True)\n",
    )
    vm("mkdir", "-p", TARGET + "/ovsdb")
    for binary in ("ovsdb-server", "ovsdb-tool"):
        vm("ln", "-sfn", "/opt/native-" + binary, TARGET + "/ovsdb/" + binary)
    metadata = {
        "source_archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "base_commit": command("git", "-C", str(REPO), "rev-parse", "HEAD").strip(),
        "ovsdb_binaries": {
            name: vm("sha256sum", "/opt/native-" + name).split()[0]
            for name in ("ovsdb-server", "ovsdb-tool")
        },
    }
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
