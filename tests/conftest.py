import json
from pathlib import Path

import pytest

EXTERNAL = Path(__file__).resolve().parents[1] / "docs/records/evidence/external.json"


def pytest_sessionstart(session):
    # Large raw evidence files live in a release, not in git; some tests replay them.
    manifest = json.loads(EXTERNAL.read_text())
    root = EXTERNAL.parents[3]
    missing = [
        entry["path"]
        for entry in manifest["files"]
        if not (root / entry["path"]).is_file()
        or (root / entry["path"]).stat().st_size != entry["size"]
    ]
    if missing:
        pytest.exit(
            f"{len(missing)} evidence files kept outside git are missing (e.g. {missing[0]}); "
            "put them back once with: python3 scripts/fetch-evidence.py",
            returncode=4,
        )


def pytest_collection_modifyitems(config, items):
    # Deliberately selected gated suites execute and fail with an explicit reason.
    # Ordinary CI excludes them; no hardware network is ever auto-discovered.
    selected = config.option.markexpr
    if not any(name in selected for name in ("wire", "hardware", "external")):
        for item in items:
            if any(name in item.keywords for name in ("wire", "hardware", "external")):
                item.add_marker(pytest.mark.skip(reason="select gated suite explicitly"))
