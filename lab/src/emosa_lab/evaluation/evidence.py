import hashlib
import html
import json
import platform
import subprocess
from importlib.metadata import version
from pathlib import Path

from emosa import __version__
from emosa.secrets import redact


def write_json(path, value):
    Path(path).write_text(json.dumps(redact(value), indent=2, ensure_ascii=False) + "\n")


def artifact(path, base):
    path = Path(path)
    data = path.read_bytes()
    return {
        "path": str(path.relative_to(base)),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def build_manifest():
    root = Path(__file__).resolve().parents[4]  # repository root (lab/src/emosa_lab/evaluation)
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
        )
        revision_text = revision.stdout.strip() or "unknown"
    except OSError:
        revision_text = "unknown"
    digest = hashlib.sha256()
    source_checkout = (root / "pyproject.toml").is_file()
    bases = (
        [root / d for d in ("src", "lab/src", "schemas", "scenarios")]
        if source_checkout
        else [Path(__file__).resolve().parents[1]]
    )
    for directory in bases:
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".json"}:
                digest.update(
                    str(path.relative_to(root if source_checkout else directory)).encode()
                )
                digest.update(path.read_bytes())
    lock = root / "uv.lock"
    return {
        "software_version": __version__,
        "git_revision": revision_text,
        "source_tree_sha256": digest.hexdigest(),
        "source_hash_scope": "checkout code/contracts/scenarios"
        if source_checkout
        else "installed package code/contracts",
        "dependencies": {name: version(name) for name in ("ovs", "jsonschema", "cryptography")},
        "python": platform.python_version(),
        "platform": platform.platform(),
        "uv_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None,
    }


def markdown_report(result, events):
    lines = [
        f"# EMOSA run {result['run_id']}",
        "",
        f"Experiment execution: **{result['execution_status']}**; "
        f"component verdict: **{result['verdict']}**.",
        f"Interoperability: **{result['interoperability_verdict']}**.",
        "",
        f"Interface: `{result['manifest']['initiating_interface']}`; "
        f"backend: `{result['manifest']['backend_mode']}`.",
        "",
        "## Checks",
        "",
        "| Check | Verdict | Expected | Observed |",
        "| --- | --- | --- | --- |",
    ]
    for check in result["checks"]:
        cells = [
            str(check[k]).replace("|", "\\|").replace("\n", " ")
            for k in ("id", "verdict", "expected", "observed")
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Timeline",
        "",
        "| Sequence | UTC | Pod | Phase | Reason |",
        "| --- | --- | --- | --- | --- |",
    ]
    for event in events:
        lines.append(
            f"| {event['sequence']} | {event['timestamp']} | {event['pod_id'] or '—'} | "
            f"{event['phase']} | {event['reason'] or '—'} |"
        )
    lines += [
        "",
        "## Limits",
        "",
        *["- " + item for item in result["limitations"]],
        "",
        "## Evidence",
        "",
        *[f"- `{a['path']}` — SHA-256 `{a['sha256']}`" for a in result["artifacts"]],
        "",
    ]
    return "\n".join(lines)


def html_report(result, events):
    rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(check[k]))}</td>"
            for k in ("id", "verdict", "expected", "observed")
        )
        + "</tr>"
        for check in result["checks"]
    )
    timeline = "".join(
        f"<details><summary>{e['sequence']} · {html.escape(e['timestamp'])} · "
        f"{html.escape(e['phase'])} · {html.escape(e['pod_id'] or 'run')}</summary>"
        f"<pre>{html.escape(json.dumps(e, indent=2))}</pre></details>"
        for e in events
    )
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width'>"
        "<title>EMOSA experiment</title><style>body{font:16px system-ui;"
        "max-width:1100px;margin:3rem auto;padding:0 1rem;"
        "background:#101a22;color:#e3edf3}table{border-collapse:collapse;width:100%}"
        "td,th{padding:.6rem;border-bottom:1px solid #456}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere}"
        "details{padding:.6rem;border-left:3px solid #68b9af;margin:.5rem 0}"
        "summary{cursor:pointer}a{color:#83c9ed}</style>"
        f"<h1>EMOSA experiment {html.escape(result['scenario_id'])}</h1>"
        f"<p>Run {html.escape(result['run_id'])}</p>"
        f"<p>Execution: <b>{result['execution_status']}</b> · "
        f"component verdict: <b>{result['verdict']}</b> · "
        f"interoperability: <b>{result['interoperability_verdict']}</b></p>"
        f"<p>{html.escape(result['manifest']['initiating_interface'])} / "
        f"{html.escape(result['manifest']['backend_mode'])}</p>"
        "<h2>Checks</h2><table><tr><th>Check</th><th>Verdict</th><th>Expected</th><th>Observed</th></tr>"
        + rows
        + "</table>"
        "<h2>Timeline</h2>"
        + timeline
        + "<h2>Scope and evidence</h2><pre>"
        + html.escape(json.dumps(redact(result), indent=2))
        + "</pre></html>"
    )


def compare(a, b):
    def differences(left, right, prefix=""):
        found = []
        for key in sorted(set(left) | set(right)):
            path = f"{prefix}.{key}" if prefix else key
            x, y = left.get(key), right.get(key)
            if isinstance(x, dict) and isinstance(y, dict):
                found.extend(differences(x, y, path))
            elif x != y:
                found.append({"field": path, "run_a": x, "run_b": y})
        return found

    return {
        "schema_version": 1,
        "run_a": a["run_id"],
        "run_b": b["run_id"],
        "differences": differences(a["manifest"], b["manifest"]),
        "verdicts": {"run_a": a["verdict"], "run_b": b["verdict"]},
        "checks": {"run_a": a["checks"], "run_b": b["checks"]},
        "timings": {"run_a": a["timings"], "run_b": b["timings"]},
        "evidence_gaps": {"run_a": a["limitations"], "run_b": b["limitations"]},
        "causality": "Differences in builds, backend, firmware or topology are uncontrolled; "
        "no causal attribution.",
    }
