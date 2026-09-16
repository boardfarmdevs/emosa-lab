# EMOSA Lab interactive field guide

The site is a static, dependency-free explorer of the system architecture, lab
modes, retained run timelines/comparisons, requirement traceability and next
acceptance experiments. It has no connection to a running lab and exposes no
control API. All command examples are copied as text for local use.

```sh
python3 scripts/build-site.py
python3 -m http.server 8000 --directory dist/site
# Open http://localhost:8000/
```

`site/content.json` supplies editorial descriptions. The builder reads the actual
curated run JSON, XML suite outcomes and traceability table, checks retained
artifact hashes, and produces `dist/site/data.json`. Source links point to the
build commit, while each experiment keeps its original source hash and run date.
Local changes in a preview can differ from that commit until committed.

Only five named static assets and the derived data snapshot are published. No
local `.lab`, `.cache`, SQLite, private connection manifest, credential or new
physical capture directory is copied. New backend evidence requires an explicit
review and corresponding updates to the guide's acceptance claims.

## GitHub Pages

`.github/workflows/pages.yml` builds the site on pull requests and pushes. Only
`main` pushes or manual dispatches on `main` deploy to the `github-pages`
environment. The repository's Pages source must be **GitHub Actions**:

Repository Settings → Pages → Build and deployment → Source → GitHub Actions.

Then the workflow publishes at <https://boardfarmdevs.github.io/emosa-lab/>. Site
publication is complete only after the deployment succeeds and the URL serves
the new guide; a pushed workflow alone does not establish that.

The build validates hashes and JavaScript syntax. Manual/browser checks should
exercise diagram selection, all four lab modes, command copy, evidence comparison,
event/reference filters, keyboard navigation and narrow layouts. The page uses
relative asset URLs so a repository subpath works without a custom domain.

See [GitHub's custom Pages workflow documentation](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages).
