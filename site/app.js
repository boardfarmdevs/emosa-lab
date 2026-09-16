const $ = (selector) => document.querySelector(selector);
const repo = "https://github.com/boardfarmdevs/emosa-lab/blob/";
let snapshot;
const esc = (text) =>
  String(text ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const source = (path) => `${repo}${snapshot.revision}/${path}`;
const badge = (text, state = "pending") =>
  `<span class="badge ${esc(state)}">${esc(text)}</span>`;
const link = (path, title) =>
  `<a href="${esc(source(path))}">${esc(title)} ↗</a>`;

function showNode(id) {
  const item = snapshot.nodes[id];
  document
    .querySelectorAll("[data-node]")
    .forEach((button) =>
      button.setAttribute("aria-pressed", String(button.dataset.node === id)),
    );
  $("#node-detail").innerHTML =
    `<p class="eyebrow">Building block / ${esc(item.label)}</p><h3>${esc(item.title)}</h3>${badge(item.status, item.state)}<p>${esc(item.description)}</p><ul>${item.responsibilities.map((x) => `<li>${esc(x)}</li>`).join("")}</ul><hr><p><strong>Next evidence</strong><br>${esc(item.next)}</p>${link(item.path, "Inspect source & contract")}`;
}

function showMode(id) {
  const item = snapshot.modes[id];
  document
    .querySelectorAll("[data-mode]")
    .forEach((button) =>
      button.setAttribute("aria-pressed", String(button.dataset.mode === id)),
    );
  $("#mode-detail").innerHTML =
    `${badge(item.status, item.state)}<h3>${esc(item.title)}</h3><p>${esc(item.description)}</p><div class="mode-path">${esc(item.path)}</div><div class="scope-grid"><div><strong>What this establishes</strong><p>${esc(item.proves)}</p></div><div><strong>What remains outside its scope</strong><p>${esc(item.limits)}</p></div></div><p><strong>Prerequisites:</strong> ${esc(item.prerequisites)}</p><div class="command"><pre><code>${esc(item.command)}</code></pre><button class="copy-button" type="button">Copy</button></div><p>${esc(item.note)}</p>${link(item.reference, "Read the complete procedure")}`;
  $(".copy-button").addEventListener("click", async (event) => {
    try {
      await navigator.clipboard.writeText(item.command);
      event.target.textContent = "Copied";
    } catch {
      event.target.textContent = "Select & copy";
      const range = document.createRange();
      range.selectNodeContents($(".command code"));
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
  });
}

function runFacts(run) {
  return {
    "Component verdict": run.verdict,
    Interoperability: run.interoperability_verdict,
    "Backend / entry": `${run.manifest.backend_mode} / ${run.manifest.initiating_interface}`,
    "Operation state": run.operations[0]?.state ?? "No operation admitted",
    "Commit attribution":
      run.operations[0]?.commit_evidence?.attribution ?? "Not applicable",
    "Apply budget": `${run.manifest.scenario.deadlines.apply_seconds} s`,
  };
}

function renderComparison() {
  const container = $("#comparison");
  const a = snapshot.runs.find((x) => x.run_id === $("#run-select").value);
  const b = snapshot.runs.find((x) => x.run_id === $("#compare-select").value);
  container.hidden = !b;
  if (!b) return;
  const af = runFacts(a),
    bf = runFacts(b);
  container.innerHTML = `<table><caption>Recorded outcomes · ${esc(a.label)} versus ${esc(b.label)}</caption><thead><tr><th>Measure</th><th>Selected run</th><th>Comparison</th></tr></thead><tbody>${Object.keys(
    af,
  )
    .map(
      (k) =>
        `<tr><th>${esc(k)}</th><td>${esc(af[k])}</td><td>${esc(bf[k])}</td></tr>`,
    )
    .join(
      "",
    )}</tbody></table><p class="quiet">Source hashes: ${esc(a.manifest.source_tree_sha256.slice(0, 12))} / ${esc(b.manifest.source_tree_sha256.slice(0, 12))}. Runs can represent different builds and budgets; this comparison does not attribute causality.</p>`;
}

function showRun() {
  const run = snapshot.runs.find((x) => x.run_id === $("#run-select").value);
  const facts = runFacts(run);
  $("#run-summary").innerHTML =
    `<article class="run-card"><h3>${esc(run.label)} ${badge(run.verdict, run.verdict)}</h3><p>${esc(run.annotation)}</p><p class="quiet"><code>${esc(run.run_id)}</code> · ${esc(run.manifest.started_at)}</p><dl class="run-facts">${Object.entries(
      facts,
    )
      .map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`)
      .join(
        "",
      )}</dl><div class="artifact-links">${link(`docs/evidence/runs/${run.run_id}/run.json`, "Result JSON")}${link(`docs/evidence/runs/${run.run_id}/report.md`, "Full report")}${link(`docs/evidence/runs/${run.run_id}/artifact-manifest.json`, "Artifact hashes")}</div></article>`;
  showEvents();
  renderComparison();
}

function showEvents() {
  const run = snapshot.runs.find((x) => x.run_id === $("#run-select").value);
  const term = $("#event-filter").value.toLowerCase().trim();
  const events = run.events.filter((x) =>
    JSON.stringify(x).toLowerCase().includes(term),
  );
  const start = Date.parse(run.events[0]?.timestamp);
  $("#timeline").innerHTML = events
    .map(
      (event) =>
        `<li><span class="event-time">+${((Date.parse(event.timestamp) - start) / 1000).toFixed(3)}s</span><div class="event-body"><strong>${esc(event.phase)}</strong><small>${esc(event.reason ?? event.pod_id ?? "Run-level event")}</small><details><summary>Inspect recorded event</summary><pre>${esc(JSON.stringify(event, null, 2))}</pre></details></div></li>`,
    )
    .join("");
  $("#event-count").textContent =
    `${events.length} of ${run.events.length} events. Relative times use the recorded wall-clock timestamps; they are a reading aid, not new latency measurements.`;
}

function showReferences() {
  const term = $("#reference-filter").value.toLowerCase().trim();
  const refs = snapshot.references.filter((x) =>
    JSON.stringify(x).toLowerCase().includes(term),
  );
  $("#reference-list").innerHTML =
    refs
      .map(
        (x) =>
          `<a class="reference-card" href="${esc(source(x.path))}"><strong>${esc(x.title)} ↗</strong><p>${esc(x.description)}</p><small>${esc(x.category)}</small></a>`,
      )
      .join("") ||
    '<p class="quiet">No matching guides. Try a requirement ID in the table below.</p>';
  const rows = snapshot.traceability.rows.filter((x) =>
    JSON.stringify(x).toLowerCase().includes(term),
  );
  $("#trace-count").textContent =
    `· ${rows.length} of ${snapshot.traceability.rows.length} rows`;
  $("#trace-rows").innerHTML = rows
    .map(
      (row) =>
        `<tr><td>${esc(row.id)}</td><td>${esc(row.status)}<small>${esc(row.mode ?? "No qualified mode")}</small></td><td>${esc(row.scope)}${row.blocker ? `<p>${esc(row.blocker)}</p>` : ""}<div>${(row
          .evidence_refs?.length
          ? row.evidence_refs
          : row.implementation_refs
        )
          .slice(0, 2)
          .map((path) => link(path, path))
          .join("<br>")}</div></td></tr>`,
    )
    .join("");
}

async function init() {
  const response = await fetch("data.json");
  if (!response.ok)
    throw new Error(
      `Evidence snapshot could not be loaded (${response.status}).`,
    );
  snapshot = await response.json();
  $("#unit-count").textContent = snapshot.suites.unit;
  $("#ovsdb-count").textContent = snapshot.suites.ovsdb;
  document
    .querySelectorAll("[data-node]")
    .forEach((button) =>
      button.addEventListener("click", () => showNode(button.dataset.node)),
    );
  document
    .querySelectorAll("[data-mode]")
    .forEach((button) =>
      button.addEventListener("click", () => showMode(button.dataset.mode)),
    );
  const options = snapshot.runs
    .map(
      (run) =>
        `<option value="${esc(run.run_id)}">${esc(run.label)} · ${esc(run.verdict)}</option>`,
    )
    .join("");
  $("#run-select").innerHTML = options;
  $("#compare-select").innerHTML += options;
  $("#run-select").addEventListener("change", showRun);
  $("#compare-select").addEventListener("change", renderComparison);
  $("#event-filter").addEventListener("input", showEvents);
  $("#reference-filter").addEventListener("input", showReferences);
  $("#roadmap").innerHTML = snapshot.roadmap
    .map(
      (step) =>
        `<article class="proof-step"><div>${badge(step.status)}<h3>${esc(step.title)}</h3><p>${esc(step.description)}</p><p class="deliverable"><strong>Exit evidence:</strong> ${esc(step.evidence)}</p>${link(step.path, step.action)}</div></article>`,
    )
    .join("");
  $("#build-info").innerHTML =
    `Snapshot from ${link("docs/delivery.md", snapshot.revision.slice(0, 12))}. Evidence retains its original run dates and source hashes.`;
  showNode("engine");
  showMode("model");
  showRun();
  showReferences();
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting)
          document
            .querySelectorAll(".sidebar nav a")
            .forEach((a) =>
              a.classList.toggle("active", a.hash === `#${entry.target.id}`),
            );
      });
    },
    { rootMargin: "-15% 0px -65% 0px" },
  );
  document
    .querySelectorAll("main section")
    .forEach((section) => observer.observe(section));
}
init().catch((error) => {
  $("#load-error").hidden = false;
  $("#load-error").textContent =
    `${error.message} Serve the built site over HTTP, or use the repository guides linked below.`;
});
