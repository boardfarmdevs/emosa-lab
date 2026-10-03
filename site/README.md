# EMOSA Lab pages

A static, dependency-free page for newcomers. It has four parts:
- **What it is**: an animated walkthrough of the adapter in four situations;
- **Current state**: current capabilities and what is not there yet;
- **Run it**: the lab demo, as copyable commands;
- **Reference**: a short glossary and three links.

The labs' sites share these section names and a top bar that links them (the umbrella's
`pages/labs-bar.js`, served once from <https://mesh.vcpe.dev/labs-bar.js>).

Keep it to what EMOSA does now. Put history and evidence in `doc/`, not here.

```sh
pages/build && python3 ../easymesh-labs/pages/finish-site.py   # the umbrella, next to this one
python3 -m http.server 8000 --directory dist/site
# open http://localhost:8000/
```

- The walkthrough's diagram and steps are data in `app.js`: `NODES`, `LINKS` and
  `SCENES`. A step names the nodes its message travels through (`hops`), which
  must be joined by `LINKS`. It can show the translation it performs as a pair:
  `mesh` for the EasyMesh side, `sync` for the OpenSync OVSDB side.
- The build copies `index.html`, `style.css`, `app.js` and `icon.svg`. It fails
  if the page links to a repository path that does not exist on `main`.
- `.github/workflows/pages.yml` calls the labs' shared Pages workflow (in the
  umbrella), which runs `pages/build` and the shared `pages/finish-site.py` on pull
  requests and pushes, and deploys from `main` to <https://vcpe.dev/emosa-lab/>. The
  repository's Pages source must be **GitHub Actions**.
