"""The adapter (package emosa) stands alone: no lab code, only its declared dependencies."""

import ast
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "src" / "emosa"
# distribution name -> import name, for the adapter's declared runtime dependencies
IMPORT_NAMES = {"cryptography": "cryptography", "jsonschema": "jsonschema", "ovs": "ovs"}


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


def test_the_adapter_never_imports_lab_code():
    offenders = {
        f"{path.relative_to(ROOT)}: {name}"
        for path in ADAPTER.rglob("*.py")
        for name in imports(path)
        if name.split(".")[0] == "emosa_lab"
    }
    assert not offenders


def test_the_adapter_uses_only_its_declared_dependencies():
    declared = {
        requirement.split("==")[0]
        for requirement in tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
            "dependencies"
        ]
    }
    assert declared == set(IMPORT_NAMES)
    allowed = {"emosa", *IMPORT_NAMES.values(), *sys.stdlib_module_names}
    used = {name.split(".")[0] for path in ADAPTER.rglob("*.py") for name in imports(path)}
    assert used <= allowed, used - allowed
