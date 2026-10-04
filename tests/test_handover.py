# SPDX-License-Identifier: Apache-2.0
"""The handover documents (docs/handover) stay true to the repository: every section of the
specification has its entry in the traceability map, everything the map names exists,
every vector set and box scenario is in it, the generated page is current, and the
module guide names every C source file."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
HANDOVER = ROOT / "docs" / "handover"
MAP = json.loads((HANDOVER / "traceability.json").read_text())["sections"]
NORMATIVE_DESIGN = ("8. Timing (normative)", "9. Resource budgets (normative upper bounds)")


def spec_sections():
    """README's sections: each subsection, and each top section without subsections."""
    tops, subs = [], []
    for line in (ROOT / "spec" / "README.md").read_text().splitlines():
        if m := re.match(r"^## (\d+\. .+)$", line):
            tops.append(m.group(1))
        elif m := re.match(r"^### (\d+\.\d+ .+)$", line):
            subs.append(m.group(1))
    with_subs = {s.split(" ", 1)[0].split(".")[0] for s in subs}
    return [f"README {t}" for t in tops if t.split(".")[0] not in with_subs] + [
        f"README {s}" for s in subs
    ]


def box_scenarios():
    text = (ROOT / "spec" / "box-scenarios.md").read_text()
    return set(re.findall(r"^\| `([a-z0-9-]+)` \|", text, re.MULTILINE))


def test_every_section_mapped():
    expected = set(spec_sections()) | {f"design {s}" for s in NORMATIVE_DESIGN}
    assert set(MAP) == expected


def test_design_sections_exist():
    design = (ROOT / "spec" / "design.md").read_text()
    for section in NORMATIVE_DESIGN:
        assert f"## {section}" in design


def test_everything_named_exists():
    scenarios = box_scenarios()
    units = (ROOT / "c" / "tests" / "units.c").read_text()
    for section, entry in MAP.items():
        assert set(entry) <= {"vectors", "box", "python", "units", "fuzz", "lab", "note"}, section
        if "note" not in entry:
            assert any(
                entry.get(k) for k in ("vectors", "box", "python", "units", "fuzz", "lab")
            ), section
        for name in entry.get("vectors", []):
            assert (ROOT / "spec" / "conformance" / name).is_file(), (section, name)
        for name in entry.get("box", []):
            assert name in scenarios, (section, name)
        for name in entry.get("python", []):
            assert (ROOT / "tests" / name).is_file(), (section, name)
        for name in entry.get("units", []):
            assert re.search(rf"^static void {name}\(", units, re.MULTILINE), (section, name)
        for name in entry.get("fuzz", []):
            assert (ROOT / "c" / "fuzz" / f"{name}.c").is_file(), (section, name)
        for name in entry.get("lab", []):
            assert (ROOT / "docs" / "records" / "evidence" / name / "README.md").is_file(), name


def test_every_vector_set_and_scenario_traced():
    named_vectors = {v for e in MAP.values() for v in e.get("vectors", [])}
    assert named_vectors == {p.name for p in (ROOT / "spec" / "conformance").glob("*.json")}
    named_box = {b for e in MAP.values() for b in e.get("box", [])}
    assert named_box == box_scenarios()


def test_generated_page_current():
    spec = importlib.util.spec_from_file_location(
        "handover_traceability", ROOT / "scripts" / "handover-traceability.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (HANDOVER / "traceability.md").read_text() == module.render(), (
        "run: python3 scripts/handover-traceability.py"
    )


def test_module_guide_names_every_c_source():
    guide = (HANDOVER / "modules.md").read_text()
    for source in (ROOT / "c" / "src").glob("*.c"):
        assert f"`{source.name}`" in guide, source.name
