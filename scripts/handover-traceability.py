#!/usr/bin/env python3
"""docs/handover/traceability.md from docs/handover/traceability.json.

python3 scripts/handover-traceability.py           write it
python3 scripts/handover-traceability.py --check   exit 1 when it is out of date
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "handover" / "traceability.json"
TARGET = ROOT / "docs" / "handover" / "traceability.md"
COLUMNS = (
    ("vectors", "Vectors", "../../spec/conformance/{}"),
    ("box", "Box scenarios", None),
    ("python", "Python tests", "../../tests/{}"),
    ("units", "C units", None),
    ("fuzz", "Fuzz targets", "../../c/fuzz/{}.c"),
    ("lab", "Lab records", "../records/evidence/{}/README.md"),
)


def cell(kind, link, items):
    if not items:
        return ""
    if link:
        return ", ".join(f"[`{i}`]({link.format(i)})" for i in items)
    return ", ".join(f"`{i}`" for i in items)


def render():
    data = json.loads(SOURCE.read_text())
    lines = [
        "# Specification to tests",
        "",
        "Generated from [traceability.json](traceability.json) by",
        "`scripts/handover-traceability.py`; `tests/test_handover.py` checks the map against",
        "the specification's sections and the repository's tests. Each section of",
        "[spec/README.md](../../spec/README.md), and the normative sections of",
        "[spec/design.md](../../spec/design.md), with what checks it. Box scenarios are",
        "described in [spec/box-scenarios.md](../../spec/box-scenarios.md); the C units are",
        "functions of `c/tests/units.c`.",
        "",
        "| Section | " + " | ".join(title for _, title, _ in COLUMNS) + " |",
        "| --- |" + " --- |" * len(COLUMNS),
    ]
    for section, entry in data["sections"].items():
        cells = [cell(kind, link, entry.get(kind, [])) for kind, _, link in COLUMNS]
        name = section + (f" ({entry['note']})" if "note" in entry else "")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    text = render()
    if args.check:
        if not TARGET.exists() or TARGET.read_text() != text:
            print(f"{TARGET} is out of date: run {Path(__file__).name}", file=sys.stderr)
            sys.exit(1)
        return
    TARGET.write_text(text)


if __name__ == "__main__":
    main()
