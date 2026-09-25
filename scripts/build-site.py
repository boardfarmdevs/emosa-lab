#!/usr/bin/env python3
"""Build the EMOSA Lab pages: four static files, and every repository link checked."""

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
OUTPUT = ROOT / "dist" / "site"
ASSETS = ("index.html", "style.css", "app.js", "icon.svg")
REPO_LINK = re.compile(r"https://github\.com/boardfarmdevs/emosa-lab/(?:blob|tree)/main/([^\"#?]+)")


def build():
    page = (SITE / "index.html").read_text()
    for path in REPO_LINK.findall(page):
        if not (ROOT / path).exists():
            raise ValueError(f"index.html links to a missing repository path: {path}")
    if OUTPUT.is_symlink():
        raise ValueError("Build output must not be a symlink")
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)
    # Explicit assets only: nothing else from the repository is published.
    for name in ASSETS:
        shutil.copyfile(SITE / name, OUTPUT / name)
    (OUTPUT / ".nojekyll").touch()
    print(f"Built {OUTPUT}: {', '.join(ASSETS)}")


if __name__ == "__main__":
    build()
