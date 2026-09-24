# EMOSA Lab pages

A static, dependency-free page for newcomers. It has four parts:
- **How it works**: an animated walkthrough of the adapter in four situations;
- **What it does today**: current capabilities and what is not there yet;
- **Try it**: the lab demo, as copyable commands;
- **Learn more**: a short glossary and three links.

Keep it to what EMOSA does now. Put history and evidence in `doc/`, not here.

```sh
python3 scripts/build-site.py
python3 -m http.server 8000 --directory dist/site
# open http://localhost:8000/
```

- The walkthrough's diagram and steps are data in `app.js`: `NODES`, `LINKS` and
  `SCENES`. A step names the nodes its message travels through (`hops`), which
  must be joined by `LINKS`. It can show the translation it performs as a pair:
  `mesh` for the EasyMesh side, `sync` for the OpenSync OVSDB side.
- The build copies `index.html`, `style.css`, `app.js` and `icon.svg`. It fails
  if the page links to a repository path that does not exist on `main`.
- `.github/workflows/pages.yml` builds on pull requests and pushes, and deploys
  from `main` to <https://boardfarmdevs.github.io/emosa-lab/>. The repository's
  Pages source must be **GitHub Actions**.
