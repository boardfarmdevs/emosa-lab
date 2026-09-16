#!/usr/bin/env python3
"""Build a public, allowlisted snapshot; never read local lab runs or credentials."""

import hashlib
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dist" / "site"


def read_json(path):
    return json.loads((ROOT / path).read_text())


def build():
    content = read_json("site/content.json")
    # Validate preserved evidence before publishing any derived claims.
    evidence = ROOT / "doc/evidence"
    artifacts = read_json("doc/evidence/manifest.json")["artifacts"]
    for artifact in artifacts:
        path = (evidence / artifact["path"]).resolve()
        if not path.is_relative_to(evidence.resolve()) or not path.is_file():
            raise ValueError(f"Invalid evidence reference: {artifact['path']}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise ValueError(f"Evidence hash mismatch: {artifact['path']}")
    content["revision"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    content["suites"] = {}
    suite_evidence = content.pop(
        "suite_evidence", {suite: f"{suite}-results.xml" for suite in ("unit", "ovsdb")}
    )
    for suite in ("unit", "ovsdb"):
        reference = suite_evidence[suite]
        if reference not in {artifact["path"] for artifact in artifacts}:
            raise ValueError(f"Suite evidence is not allowlisted: {reference}")
        root = ET.parse(evidence / reference).getroot()
        cases = root.findall(".//testcase")
        content["suites"][suite] = sum(
            not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
            for case in cases
        )
    content["runs"] = []
    for item in content.pop("run_catalog"):
        # Only committed, curated simulation evidence is eligible for this public site.
        path = Path("doc/evidence/runs") / item["id"]
        run = read_json(path / "run.json")
        if run["manifest"]["backend_mode"] not in {"model", "ovsdb-sim"}:
            raise ValueError("Public run catalog requires explicit review for new backend modes")
        if run["interoperability_verdict"] not in {"not_evaluated", "blocked"}:
            raise ValueError("Update the guide's physical-proof claims before publishing this run")
        run.update(label=item["label"], annotation=item["annotation"])
        run["events"] = read_json(path / "events.json")
        content["runs"].append(run)
    content["traceability"] = read_json("doc/project/traceability.json")
    for item in content["references"]:
        if not (ROOT / item["path"]).is_file():
            raise ValueError(f"Missing manual reference: {item['path']}")
    if OUTPUT.is_symlink():
        raise ValueError("Build output must not be a symlink")
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)
    # Explicit assets only. No repository-wide copy and no .lab/.cache artifacts.
    for name in ("index.html", "style.css", "app.js", "icon.svg", "architecture.svg"):
        shutil.copyfile(ROOT / "site" / name, OUTPUT / name)
    (OUTPUT / "data.json").write_text(json.dumps(content, ensure_ascii=False) + "\n")
    (OUTPUT / ".nojekyll").touch()
    print(f"Built {OUTPUT}: {len(content['runs'])} retained runs; evidence hashes verified")


if __name__ == "__main__":
    build()
