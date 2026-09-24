"use strict";
// EMOSA Lab: the flow player. One message at a time moves through the adapter.

const SVG = "http://www.w3.org/2000/svg";
const $ = (s) => document.querySelector(s);

// Boxes in the 1000 x 400 diagram. zone = which world the box belongs to.
const ZONES = [
  { id: "mesh", x: 10, y: 20, w: 205, h: 360, label: "EasyMesh network", note: "talks IEEE 1905.1" },
  { id: "emosa", x: 240, y: 20, w: 362, h: 360, label: "EMOSA adapter", note: "1905.1 in · OVSDB out" },
  { id: "sync", x: 627, y: 20, w: 240, h: 360, label: "OpenSync pod · unchanged" },
  { id: "client", x: 885, y: 20, w: 105, h: 360, label: "Client" },
];
const NODES = {
  controller: { x: 25, y: 170, w: 175, h: 70, name: "EasyMesh controller", sub: "prplMesh · RDK" },
  fleet: { x: 420, y: 58, w: 165, h: 64, name: "Fleet", sub: "one agent per pod" },
  agent: { x: 258, y: 170, w: 142, h: 70, name: "Virtual agent", sub: "speaks EasyMesh" },
  translate: { x: 420, y: 170, w: 165, h: 70, name: "Translation", sub: "OVSDB ⇄ EasyMesh" },
  cm: { x: 645, y: 58, w: 204, h: 50, name: "Connection manager", sub: "cm" },
  identity: { x: 645, y: 124, w: 204, h: 50, name: "Identity", sub: "AWLAN_Node" },
  config: { x: 645, y: 186, w: 204, h: 50, name: "Desired config", sub: "Wifi_*_Config" },
  state: { x: 645, y: 248, w: 204, h: 50, name: "Actual state", sub: "Wifi_*_State" },
  radio: { x: 645, y: 312, w: 204, h: 56, name: "Wi-Fi manager + radio", sub: "owm" },
  client: { x: 895, y: 305, w: 85, h: 63, name: "Phone", sub: "Wi-Fi" },
};
// Links as polylines, drawn from the first node to the second.
const LINKS = [
  { a: "controller", b: "agent", pts: [[200, 205], [258, 205]] },
  { a: "agent", b: "translate", pts: [[400, 205], [420, 205]] },
  { a: "fleet", b: "agent", pts: [[420, 90], [329, 90], [329, 170]] },
  { a: "cm", b: "fleet", pts: [[645, 83], [585, 83]] },
  { a: "fleet", b: "identity", pts: [[585, 108], [645, 149]] },
  { a: "translate", b: "config", pts: [[585, 196], [645, 211]] },
  { a: "state", b: "translate", pts: [[645, 273], [585, 226]] },
  { a: "config", b: "radio", pts: [[849, 211], [864, 211], [864, 326], [849, 326]] },
  { a: "radio", b: "state", pts: [[747, 312], [747, 298]] },
  { a: "radio", b: "client", pts: [[849, 352], [895, 352]] },
];

const SCENES = {
  join: [
    { hops: ["cm", "fleet"], msg: "connect", text: "The operator's cloud hands the pod to EMOSA. The pod's own connection manager dials EMOSA's front port, just as it would dial its cloud. Nothing is installed on the pod." },
    { hops: ["fleet", "identity"], msg: "select AWLAN_Node", text: "The fleet asks the pod who it is: serial number, model and firmware." },
    { hops: ["identity", "fleet"], msg: "MVXPOD023F87E628DD", text: "The serial number becomes the agent's EasyMesh identity. A returning pod always gets the same agent back.", mesh: "AL MAC 02:72:f9:7f:07:85", sync: "AWLAN_Node.serial_number" },
    { hops: ["fleet", "agent"], msg: "start agent", text: "The fleet starts a virtual agent for this pod, with its own EasyMesh interface and its own port for the pod." },
    { hops: ["fleet", "identity"], msg: "manager_addr = agent port", text: "The fleet points the pod at its agent and ends the session. The pod reconnects, straight to its own agent this time." },
    { hops: ["state", "translate", "agent"], msg: "monitor", text: "From now on the agent follows the pod's tables live. Whatever the pod reports reaches the agent within half a second." },
    { hops: ["agent", "controller"], msg: "Topology Discovery", text: "The agent introduces itself on the EasyMesh network and repeats it every 60 seconds, as every EasyMesh agent does." },
  ],
  onboard: [
    { hops: ["agent", "controller"], msg: "AP-Autoconfig Search", text: "The agent looks for the controller that will configure its radio." },
    { hops: ["controller", "agent"], msg: "AP-Autoconfig Response", text: "The controller answers: it will configure this 2.4 GHz radio." },
    { hops: ["state", "translate", "agent", "controller"], msg: "WSC M1", text: "The agent describes the pod's radio in an M1 message, built from what the pod itself reports.", mesh: "M1 · radio 02:00:00:00:01:00 · 1 BSS", sync: "Wifi_Radio_State · mac · channel 6" },
    { hops: ["controller", "agent", "translate"], msg: "WSC M2", text: "The controller sends back the network: SSID and passphrase, encrypted for this agent only." },
    { hops: ["translate", "config"], msg: "guarded write", text: "EMOSA turns M2 into one guarded OVSDB write. The passphrase goes into a secret store and never into a log.", mesh: "M2 · SSID emosa-mesh · WPA2-PSK", sync: "Wifi_VIF_Config home-ap-24 · ssid · wpa_psks" },
    { hops: ["config", "radio"], msg: "apply", text: "The pod's own Wi-Fi manager applies the change to the radio. EMOSA never touches the radio itself." },
    { hops: ["radio", "state"], msg: "Wifi_VIF_State", text: "The pod publishes what it is actually running now." },
    { hops: ["state", "translate"], msg: "applied ✓", text: "Only now does EMOSA mark the change as done. A write is not proof; the pod's own state is." },
    { hops: ["client", "radio"], msg: "associate", text: "A phone joins emosa-mesh on the pod, using the credentials the controller chose." },
  ],
  topology: [
    { hops: ["client", "radio"], msg: "associate", text: "A phone joins the pod's Wi-Fi network." },
    { hops: ["radio", "state"], msg: "Wifi_Associated_Clients", text: "The pod records the new client in its state tables." },
    { hops: ["state", "translate"], msg: "state rows", text: "EMOSA reads the pod's radios, networks and clients from those tables, never from what was asked for.", mesh: "radio · BSS · associated client", sync: "Wifi_Radio_State · Wifi_VIF_State · Wifi_Associated_Clients" },
    { hops: ["translate", "agent", "controller"], msg: "Topology Notification", text: "The agent tells the controller about the new client right away.", mesh: "client association event · joined", sync: "Wifi_Associated_Clients · state=active" },
    { hops: ["controller", "agent"], msg: "Topology Query", text: "The controller asks for the full picture." },
    { hops: ["translate", "agent", "controller"], msg: "Topology Response", text: "The controller now shows the pod as one of its agents, with its network and its clients.", mesh: "BSS role · fronthaul 0x40 · backhaul 0x80", sync: "multi_ap · none · backhaul_bss" },
  ],
  recover: [
    { hops: ["controller", "agent"], msg: "AP-Autoconfig Renew", text: "The controller restarts, or changes the network, and asks every agent to onboard again." },
    { hops: ["state", "translate", "agent", "controller"], msg: "WSC M1", text: "The agent answers with a fresh M1, built again from the pod's state." },
    { hops: ["controller", "agent", "translate"], msg: "WSC M2", text: "The controller sends the network again." },
    { hops: ["translate", "config"], msg: "no write needed", text: "The pod already runs exactly this network, so EMOSA writes nothing. The same message twice never means two changes." },
    { hops: ["state", "translate"], msg: "connection lost", text: "Now the pod's connection drops: a network cut, or a reboot. The agent keeps its identity and waits for the pod." },
    { hops: ["state", "translate"], msg: "reconnect · reconcile", text: "When the pod is back, the agent reads its state again and finishes any change that was in flight, without doing anything twice." },
    { hops: ["agent", "controller"], msg: "Topology Discovery", text: "A restarted controller finds every agent again within one 60-second discovery round, and relearns their clients." },
  ],
};

const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
const el = (tag, attrs = {}, parent) => {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
};

function drawStage(svg) {
  for (const z of ZONES) {
    el("rect", { class: `zone ${z.id}`, x: z.x, y: z.y, width: z.w, height: z.h, rx: 12 }, svg);
    el("text", { class: `zone-label ${z.id}`, x: z.x + 12, y: z.y + 22 }, svg).textContent = z.label;
    if (z.note) el("text", { class: "zone-note", x: z.x + 12, y: z.y + z.h - 14 }, svg).textContent = z.note;
  }
  const links = {};
  for (const l of LINKS) {
    const path = el("polyline", { class: "link", points: l.pts.map((p) => p.join(",")).join(" ") }, svg);
    links[`${l.a}>${l.b}`] = { path, pts: l.pts };
    links[`${l.b}>${l.a}`] = { path, pts: [...l.pts].reverse() };
  }
  const nodes = {};
  for (const [id, n] of Object.entries(NODES)) {
    const g = el("g", { class: "node", "data-node": id }, svg);
    el("rect", { x: n.x, y: n.y, width: n.w, height: n.h, rx: 8 }, g);
    const cx = n.x + n.w / 2;
    const mid = n.y + n.h / 2;
    el("text", { class: "name", x: cx, y: mid - 2, "text-anchor": "middle" }, g).textContent = n.name;
    el("text", { class: "sub", x: cx, y: mid + 15, "text-anchor": "middle" }, g).textContent = n.sub;
    nodes[id] = g;
  }
  const label = el("g", { class: "wire-label" }, svg);
  const labelBox = el("rect", { rx: 6, height: 22 }, label);
  const labelText = el("text", { "text-anchor": "middle" }, label);
  const token = el("circle", { class: "token", r: 8, cx: -20, cy: -20 }, svg);
  return { links, nodes, token, label, labelBox, labelText };
}

function routeFor(stage, hops) {
  const pts = [];
  const paths = [];
  for (let i = 0; i < hops.length - 1; i++) {
    const link = stage.links[`${hops[i]}>${hops[i + 1]}`];
    if (!link) throw new Error(`no link ${hops[i]} > ${hops[i + 1]}`);
    paths.push(link.path);
    for (const p of link.pts) pts.push(p);
  }
  let total = 0;
  const segs = [];
  for (let i = 0; i < pts.length - 1; i++) {
    const [x1, y1] = pts[i];
    const [x2, y2] = pts[i + 1];
    const len = Math.hypot(x2 - x1, y2 - y1);
    segs.push({ x1, y1, x2, y2, len, start: total });
    total += len;
  }
  return { segs, total, paths };
}

function pointAt(route, d) {
  for (const s of route.segs) {
    if (d <= s.start + s.len || s === route.segs[route.segs.length - 1]) {
      const t = s.len ? Math.min(1, Math.max(0, (d - s.start) / s.len)) : 1;
      return [s.x1 + (s.x2 - s.x1) * t, s.y1 + (s.y2 - s.y1) * t];
    }
  }
  return [0, 0];
}

function placeToken(stage, [x, y]) {
  stage.token.setAttribute("cx", x);
  stage.token.setAttribute("cy", y);
  const w = stage.labelText.getComputedTextLength() + 16;
  const lx = Math.min(Math.max(x, w / 2 + 4), 1000 - w / 2 - 4);
  const ly = y < 50 ? y + 16 : y - 32;
  stage.labelBox.setAttribute("x", lx - w / 2);
  stage.labelBox.setAttribute("y", ly);
  stage.labelBox.setAttribute("width", w);
  stage.labelText.setAttribute("x", lx);
  stage.labelText.setAttribute("y", ly + 15.5);
}

function init() {
  const svg = $("#stage");
  const stage = drawStage(svg);
  const state = { scene: "join", step: 0, playing: false, frame: 0, timer: 0 };
  const tabs = [...document.querySelectorAll("[data-scene]")];

  const stopMotion = () => {
    cancelAnimationFrame(state.frame);
    clearTimeout(state.timer);
  };

  function setPlaying(on) {
    state.playing = on;
    $("#play").textContent = on ? "Pause" : state.step === SCENES[state.scene].length - 1 ? "Replay" : "Play";
    if (!on) stopMotion();
  }

  function show(step, animate) {
    stopMotion();
    const steps = SCENES[state.scene];
    state.step = step;
    const s = steps[step];
    $("#step-count").textContent = `Step ${step + 1} of ${steps.length}`;
    $("#step-message").textContent = s.msg;
    $("#step-text").textContent = s.text;
    const tr = $("#step-translation");
    tr.hidden = !s.mesh;
    if (s.mesh) {
      $("#tr-mesh").textContent = s.mesh;
      $("#tr-sync").textContent = s.sync;
    }
    document.querySelectorAll("#steps button").forEach((b, i) => {
      if (i === step) b.setAttribute("aria-current", "step");
      else b.removeAttribute("aria-current");
    });
    for (const [id, g] of Object.entries(stage.nodes)) g.classList.toggle("active", s.hops.includes(id));
    const route = routeFor(stage, s.hops);
    svg.querySelectorAll(".link").forEach((p) => p.classList.toggle("active", route.paths.includes(p)));
    stage.labelText.textContent = s.msg;
    const done = () => {
      placeToken(stage, pointAt(route, route.total));
      if (state.playing) {
        state.timer = setTimeout(() => {
          if (state.step < steps.length - 1) show(state.step + 1, true);
          else setPlaying(false);
        }, 2600 + s.text.length * 18);
      }
    };
    if (!animate || reduced.matches) return done();
    const duration = Math.max(700, route.total * 3.2);
    const t0 = performance.now();
    const tick = (now) => {
      const t = Math.min(1, (now - t0) / duration);
      const eased = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
      placeToken(stage, pointAt(route, route.total * eased));
      if (t < 1) state.frame = requestAnimationFrame(tick);
      else done();
    };
    state.frame = requestAnimationFrame(tick);
  }

  function selectScene(name, autoplay) {
    state.scene = name;
    tabs.forEach((t) => {
      t.setAttribute("aria-selected", String(t.dataset.scene === name));
      t.tabIndex = t.dataset.scene === name ? 0 : -1;
    });
    const list = $("#steps");
    list.innerHTML = "";
    SCENES[name].forEach((s, i) => {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = s.msg;
      b.addEventListener("click", () => {
        setPlaying(false);
        show(i, true);
      });
      li.appendChild(b);
      list.appendChild(li);
    });
    setPlaying(Boolean(autoplay) && !reduced.matches);
    show(0, Boolean(autoplay));
  }

  tabs.forEach((t, i) => {
    t.addEventListener("click", () => selectScene(t.dataset.scene, true));
    t.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      next.focus();
      selectScene(next.dataset.scene, false);
    });
  });
  $("#play").addEventListener("click", () => {
    const last = SCENES[state.scene].length - 1;
    if (state.playing) return setPlaying(false);
    // Play from the current step (replay it with motion), or from the start at the end.
    state.started = true;
    setPlaying(true);
    show(state.step === last ? 0 : state.step, true);
  });
  $("#next").addEventListener("click", () => {
    setPlaying(false);
    show(Math.min(state.step + 1, SCENES[state.scene].length - 1), true);
  });
  $("#prev").addEventListener("click", () => {
    setPlaying(false);
    show(Math.max(state.step - 1, 0), true);
  });
  $("#player").addEventListener("keydown", (e) => {
    if (e.target.closest("[role=tab]")) return;
    if (e.key === "ArrowRight") $("#next").click();
    if (e.key === "ArrowLeft") $("#prev").click();
  });

  selectScene("join", false);
  // Play once when the walkthrough first comes into view.
  if ("IntersectionObserver" in window && !reduced.matches) {
    const seen = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        seen.disconnect();
        if (!state.started) {
          state.started = true;
          setPlaying(true);
          show(0, true);
        }
      }
    }, { threshold: 0.5 });
    seen.observe(svg);
  }
}

function copyButtons() {
  document.querySelectorAll(".cmd").forEach((box) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy";
    button.textContent = "Copy";
    button.addEventListener("click", async () => {
      const text = box.querySelector("code").textContent;
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "Copied";
      } catch {
        const range = document.createRange();
        range.selectNodeContents(box.querySelector("code"));
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        button.textContent = "Press Ctrl+C";
      }
      setTimeout(() => (button.textContent = "Copy"), 2000);
    });
    box.appendChild(button);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  init();
  copyButtons();
});
