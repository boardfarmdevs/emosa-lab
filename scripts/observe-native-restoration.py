"""Read-only post-trial baseline and idle observation from the owned VM."""

import argparse
import json
import re
import subprocess
from pathlib import Path

OBSERVE = """
import hashlib, json, runpy, sys, time
from pathlib import Path
sys.path.insert(0, '/opt/emosa-baseline')
trial = runpy.run_path('/opt/emosa-baseline/controller-trial.py')
trial['idle']()
candidate = runpy.run_path('/opt/emosa-baseline/compatibility/controller-candidate.py')
state = Path('/opt/emosa-baseline/controller-candidates') / sys.argv[1]
before = state / 'reference-before.json'
result = json.loads((state / 'result.json').read_text())
assert result['baseline_restored']
root = Path('/opt/prpl-install-nl80211')
paths = [root / 'bin/beerocks_controller', root / 'bin/beerocks_agent',
         Path('/opt/emosa-baseline/prplmesh.reference.json')]
paths += [root / 'lib' / name for name in result.get('runtime_libraries', {})]
observed = {str(p): candidate['inside'](candidate['CONTROLLER'], 'sha256sum', str(p)).split()[0]
            for p in paths}
baseline = json.loads(before.read_text())
for name in ('beerocks_controller', 'beerocks_agent'):
    assert observed[str(root / 'bin' / name)] == baseline['binaries'][name]
for name, hashes in result.get('runtime_libraries', {}).items():
    assert observed[str(root / 'lib' / name)] == hashes['baseline_sha256']
reference_hash = hashlib.sha256(before.read_bytes()).hexdigest()
assert observed['/opt/emosa-baseline/prplmesh.reference.json'] == reference_hash
current_reference = Path('/opt/emosa-baseline/prplmesh.reference.json').read_bytes()
assert hashlib.sha256(current_reference).hexdigest() == reference_hash
print(json.dumps({'observed_at': time.time(), 'owned_idle_check_passed': True,
                  'actual_restored_files': observed,
                  'reference_before_sha256': reference_hash}, indent=2))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", args.label):
        parser.error("expected an existing owned trial label")
    if args.output.exists() or args.output.is_symlink():
        parser.error("preserve existing observations; select a new output path")
    observed = subprocess.check_output(
        [
            "lxc",
            "exec",
            "emosa-lab",
            "--",
            "env",
            "PYTHONPATH=/opt/emosa-radio-manager/source",
            "/opt/emosa/.venv/bin/python",
            "-c",
            OBSERVE,
            args.label,
        ],
        text=True,
        timeout=60,
    )
    value = json.loads(observed)
    with args.output.open("x") as output:
        output.write(json.dumps(value, indent=2) + "\n")
    print(f"Observed idle lab and restored baseline: {args.output}")


if __name__ == "__main__":
    main()
