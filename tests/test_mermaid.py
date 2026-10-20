# SPDX-License-Identifier: Apache-2.0
"""The Markdown diagrams render on GitHub: Mermaid 12, which GitHub uses, reads an `@` outside a
quoted label as its node and edge ID syntax (`A1[emosa-agent@pod-1]` failed to parse in
spec/design.md, 20 October), so every `@` in a mermaid block is inside a quoted label."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
SKIP = {"node_modules", "build", "venv"}  # besides hidden directories (caches, .git)


def mermaid_lines():
    """(file, line number, line) of every line in a mermaid block of the repository's Markdown."""
    for path in sorted(ROOT.rglob("*.md")):
        parts = path.relative_to(ROOT).parts[:-1]
        if any(p.startswith(".") or p in SKIP for p in parts):
            continue
        name, inside = path.relative_to(ROOT), False
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("```mermaid"):
                inside = True
            elif inside and stripped.startswith("```"):
                inside = False
            elif inside and not stripped.startswith("%%"):
                yield name, number, line


def test_every_at_sign_in_a_diagram_is_in_a_quoted_label():
    lines = list(mermaid_lines())
    assert lines, "no mermaid blocks found"
    bare = [
        f"{name}:{number}: {line.strip()}"
        for name, number, line in lines
        if "@" in re.sub(r'"[^"]*"', "", line)
    ]
    assert not bare, "quote the label: " + "; ".join(bare)
