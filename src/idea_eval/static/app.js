"use strict";

// IDEA//DECK front-end. Everything that comes from the server or the web is rendered
// with textContent / createElement only; there is no innerHTML anywhere.

(() => {
  const $ = (id) => document.getElementById(id);
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const state = {
    me: null,
    mode: "cheap",
    runId: null,
    es: null,
    running: false,
    startedAt: 0,
    cost: 0,
    search: { total: 0, done: 0, failed: 0 },
  };

  const LABEL = {
    brief: "BRIEF", search: "RECON", gather: "SIFT", competitor_analyst: "RIVALS",
    business_analyst: "MONEY", evidence_check: "CHECK", judge: "JUDGE", strategist: "PLAN",
    report: "REPORT", sys: "SYS",
  };
  const VERDICT = { PURSUE: "c-mint", PIVOT: "c-amber", DROP: "c-red" };
  const CONFIDENCE = { high: "c-mint", medium: "c-amber", low: "c-red" };
  const FIT = { strong: "c-mint", moderate: "c-amber", weak: "c-red" };
  const EXISTS = {
    exact: ["YES · EXACT MATCH", "c-red"],
    close: ["YES · CLOSE MATCHES", "c-amber"],
    adjacent: ["PARTLY · ADJACENT", "c-mint"],
    none_found: ["NOTHING FOUND", "c-cyan"],
  };

  // ---------- DOM helpers ----------

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : String(v));
    }
    append(el, children);
    return el;
  }

  function append(el, children) {
    for (const c of children.flat(Infinity)) {
      if (c == null || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
  }

  function svg(tag, attrs) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
    return el;
  }

  function safeUrl(u) {
    try {
      const url = new URL(u);
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
    } catch {
      return null;
    }
  }

  function prettyModel(id) {
    const parts = String(id || "").replace(/^claude-/, "").split("-");
    return `${(parts[0] || "?").toUpperCase()} ${parts.slice(1).join(".")}`.trim();
  }

  function kilo(n) {
    const v = Number(n) || 0;
    return v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v);
  }

  function fmtDate(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
      d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  }

  // ---------- API ----------

  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", credentials: "same-origin", headers: {} };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    try {
      const res = await fetch(path, init);
      let data = null;
      try { data = await res.json(); } catch { /* empty body */ }
      return { ok: res.ok, status: res.status, data };
    } catch {
      return { ok: false, status: 0, data: { detail: "Network error: the deck is unreachable." } };
    }
  }

  function errText(r) {
    const d = r.data && r.data.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((x) => x.msg).join("; ");
    return `Error ${r.status}`;
  }

  // ---------- Screens ----------

  function show(screen) {
    for (const id of ["auth", "soon", "deck"]) $(id).hidden = id !== screen;
    $("strip").hidden = screen !== "deck";
    $("who").hidden = screen !== "deck";
  }

  const BOOT = [
    ["IDEA//DECK BIOS", "OK"],
    ["MEMORY CHECK", "OK"],
    ["LANGGRAPH CORE", "LINKED"],
    ["RECON UPLINK // TAVILY", "LINKED"],
    ["ANALYST CLUSTER // CLAUDE", "LINKED"],
    ["WHITELIST", "ENFORCED"],
  ];
  let bootToken = 0;

  async function boot() {
    const token = ++bootToken;
    const pre = $("boot");
    pre.replaceChildren();
    for (const [label, status] of BOOT) {
      if (token !== bootToken) return;
      pre.append(h("span", {}, `${label} `.padEnd(30, "."), " ",
        h("span", { class: status === "ENFORCED" ? "warn" : "ok" }, status), "\n"));
      if (!reduceMotion) await sleep(130);
    }
    pre.append(h("span", {}, "> IDENTIFY YOURSELF_"));
    $("email").focus();
  }

  function toAuth() {
    closeStream();
    state.me = null;
    history.replaceState(null, "", location.pathname);
    show("auth");
    boot();
  }

  async function enterDeck() {
    const r = await api("/api/me");
    if (!r.ok) return toAuth();
    state.me = r.data;
    $("operator").textContent = r.data.user.email;
    $("version").textContent = `v${r.data.version}`;
    renderQuota(r.data.quota);
    setMode(state.mode);
    show("deck");
    $("keys-btn").hidden = !r.data.user.is_admin;
    $("keys-panel").hidden = true;
    if (r.data.user.is_admin && !r.data.keys_ready) {
      openKeys("Add your Anthropic and Tavily API keys to start scanning.");
    }
    await loadHistory();
    const m = location.hash.match(/^#run=([a-f0-9]{32})$/);
    if (m) openRun(m[1]);
    else $("idea").focus();
  }

  function renderQuota(q) {
    $("strip-quota").textContent = q.limit == null ? "∞ UNLIMITED" : `${q.used}/${q.limit} THIS MONTH`;
  }

  async function refreshQuota() {
    const r = await api("/api/me");
    if (r.ok) renderQuota(r.data.quota);
  }

  // ---------- Login ----------

  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("login-msg");
    msg.textContent = "";
    $("login-btn").disabled = true;
    const r = await api("/api/login", {
      method: "POST",
      body: { email: $("email").value, password: $("password").value },
    });
    $("login-btn").disabled = false;
    $("password").value = "";
    if (r.ok && r.data.status === "ok") return enterDeck();
    if (r.ok && r.data.status === "coming_soon") return show("soon");
    if (r.status === 429) msg.textContent = "LOCKED // too many attempts, try again later";
    else if (r.status === 0) msg.textContent = errText(r);
    else msg.textContent = "ACCESS DENIED";
  });

  $("soon-back").addEventListener("click", () => toAuth());
  $("logout").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" });
    toAuth();
  });

  // ---------- API keys (admins) ----------

  const KEY_SOURCE = { app: "saved in app", env: "from server .env", missing: "not set" };
  const CHECK_STYLE = {
    ok: ["c-mint", "✓", "ok"], no_credits: ["c-amber", "⚠", "warn"],
    invalid: ["c-red", "✗", "bad"], error: ["c-red", "✗", "bad"], missing: ["c-dim", "·", ""],
  };

  function keyRow(name) {
    return $("keys-form").querySelector(`.key-row[data-key="${name}"]`);
  }

  function renderKeys(keys) {
    for (const [name, k] of Object.entries(keys || {})) {
      const row = keyRow(name);
      if (!row) continue;
      row.querySelector(".key-masked").textContent = k.masked || "—";
      row.querySelector(".key-source").textContent = KEY_SOURCE[k.source] || k.source;
      row.querySelector(".led").className = k.configured ? "led set" : "led";
    }
  }

  function renderChecks(checks) {
    for (const [name, c] of Object.entries(checks || {})) {
      const row = keyRow(name);
      if (!row) continue;
      const [cls, icon, led] = CHECK_STYLE[c.status] || ["c-red", "✗", "bad"];
      const result = row.querySelector(".key-result");
      result.className = `key-result ${cls}`;
      result.textContent = `${icon} ${c.message}`;
      if (led) row.querySelector(".led").className = `led ${led}`;
    }
  }

  async function openKeys(notice) {
    const intro = $("keys-intro");
    if (notice) {
      intro.textContent = notice;
      intro.classList.add("alert");
    }
    $("keys-panel").hidden = false;
    $("keys-panel").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    const r = await api("/api/admin/keys");
    if (r.ok) renderKeys(r.data.keys);
    const empty = [...$("keys-form").querySelectorAll("input")].find((i) => !i.value);
    if (empty) empty.focus();
  }

  async function submitKeys(path, method, body) {
    for (const b of [$("keys-save"), $("keys-test")]) b.disabled = true;
    for (const p of $("keys-form").querySelectorAll(".key-result")) {
      p.className = "key-result c-dim";
      p.textContent = "· testing…";
    }
    const r = await api(path, { method, body });
    for (const b of [$("keys-save"), $("keys-test")]) b.disabled = false;
    for (const p of $("keys-form").querySelectorAll(".key-result")) {
      if (p.textContent === "· testing…") p.textContent = "";
    }
    if (!r.ok) {
      if (r.status === 401) return toAuth();
      const first = $("keys-form").querySelector(".key-result");
      first.className = "key-result c-red";
      first.textContent = `✗ ${errText(r)}`;
      return;
    }
    renderKeys(r.data.keys);
    renderChecks(r.data.checks);
    const me = await api("/api/me");
    if (me.ok && me.data.keys_ready) {
      $("keys-intro").classList.remove("alert");
      $("keys-intro").textContent = "Keys are set. You can close this panel and run a scan.";
    }
  }

  $("keys-btn").addEventListener("click", () => {
    if ($("keys-panel").hidden) openKeys();
    else $("keys-panel").hidden = true;
  });
  $("keys-close").addEventListener("click", () => { $("keys-panel").hidden = true; });
  $("keys-test").addEventListener("click", () => submitKeys("/api/admin/keys/test", "POST"));
  $("keys-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {};
    for (const input of $("keys-form").querySelectorAll("input")) {
      if (input.value.trim()) body[input.id] = input.value.trim();
    }
    if (!Object.keys(body).length) return submitKeys("/api/admin/keys/test", "POST");
    await submitKeys("/api/admin/keys", "PUT", body);
    for (const input of $("keys-form").querySelectorAll("input")) input.value = "";
  });

  // ---------- Mode toggle ----------

  function setMode(mode) {
    state.mode = mode;
    const deep = mode === "deep";
    const models = (state.me && state.me.models) || { cheap: "claude-haiku-5-5", deep: "claude-sonnet-5-5" };
    $("mode").setAttribute("aria-checked", String(deep));
    document.body.classList.toggle("deep-on", deep);
    $("est").textContent = deep ? "~$0.13" : "~$0.02";
    $("est-model").textContent = deep
      ? `${prettyModel(models.deep)} judge + plan`
      : `all ${prettyModel(models.cheap)}`;
    $("strip-model").textContent = deep
      ? `${prettyModel(models.cheap)} + ${prettyModel(models.deep)}`
      : prettyModel(models.cheap);
    $("strip-model").classList.toggle("deep", deep);
    try { localStorage.setItem("itig-mode", mode); } catch { /* storage unavailable */ }
  }

  $("mode").addEventListener("click", () => setMode(state.mode === "deep" ? "cheap" : "deep"));

  // ---------- Runs ----------

  function runMsg(text) {
    $("run-msg").textContent = text || "";
  }

  $("idea").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("run-form").requestSubmit();
  });

  $("run-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const idea = $("idea").value.trim();
    if (idea.length < 8) return runMsg("Describe the idea in at least a few words.");
    runMsg("");
    $("go").disabled = true;
    const r = await api("/api/runs", {
      method: "POST",
      body: { idea, mode: state.mode, profile: $("profile").value.trim() },
    });
    if (!r.ok) {
      $("go").disabled = false;
      if (r.status === 401) return toAuth();
      return runMsg(errText(r));
    }
    $("report").hidden = true;
    startStream(r.data.id);
    loadHistory();
  });

  function stage(node) {
    return $("pipeline").querySelector(`li[data-node="${node}"]`);
  }

  function resetPipeline() {
    for (const li of $("pipeline").children) li.className = "";
    state.cost = 0;
    state.search = { total: 0, done: 0, failed: 0 };
    $("search-count").textContent = "web search";
    $("run-cost").textContent = "$0.0000";
    $("log").replaceChildren();
  }

  function setStatus(text) {
    $("run-status").textContent = text;
  }

  function logLine(node, text, cls) {
    const log = $("log");
    const t = new Date().toLocaleTimeString([], { hour12: false });
    log.append(h("span", { class: "line" },
      h("span", { class: "t" }, `${t} `),
      h("span", { class: "n" }, `${(LABEL[node] || node).padEnd(7)} `),
      h("span", { class: cls || "" }, text)));
    log.scrollTop = log.scrollHeight;
  }

  function closeStream() {
    if (state.es) state.es.close();
    state.es = null;
    state.running = false;
  }

  function startStream(runId) {
    closeStream();
    state.runId = runId;
    state.running = true;
    state.startedAt = Date.now();
    history.replaceState(null, "", `#run=${runId}`);
    highlightHistory();
    resetPipeline();
    setStatus("CONNECTING");
    $("go").disabled = true;
    const es = new EventSource(`/api/runs/${runId}/events`);
    state.es = es;
    // The server replays every event on (re)connect, so start from a clean slate.
    es.onopen = () => resetPipeline();
    es.onmessage = (m) => {
      let ev;
      try { ev = JSON.parse(m.data); } catch { return; }
      onEvent(ev);
    };
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED) fail("Connection to the deck was lost.");
      else setStatus("RECONNECTING");
    };
  }

  function onEvent(ev) {
    switch (ev.type) {
      case "status":
        setStatus("RUNNING");
        logLine("sys", "pipeline engaged", "ok");
        break;
      case "log":
        logLine("sys", ev.msg, "warn");
        break;
      case "usage":
        state.cost += Number(ev.cost_usd || 0);
        $("run-cost").textContent = `$${state.cost.toFixed(4)}`;
        break;
      case "node":
        onNode(ev);
        break;
      case "done":
        finish(ev);
        break;
      case "error":
        fail(ev.msg);
        break;
      default:
        break;
    }
  }

  function onNode(ev) {
    const li = stage(ev.node);
    if (!li) return;
    if (ev.node === "search") {
      if (ev.status === "start") {
        // A new search after analysis means a follow-up research round.
        if (stage("gather").classList.contains("done")) {
          for (const n of ["gather", "competitor_analyst", "business_analyst", "evidence_check"]) {
            stage(n).className = "";
          }
        }
        state.search.total += 1;
        li.className = "active";
        logLine("search", `› ${ev.detail}`);
      } else {
        state.search.done += 1;
        if (ev.status === "error") state.search.failed += 1;
        logLine("search", `${ev.status === "error" ? "✗" : "✓"} ${ev.detail}`, ev.status === "error" ? "err" : "ok");
        if (state.search.done >= state.search.total) {
          li.className = state.search.failed === state.search.total ? "error" : "done";
        }
      }
      $("search-count").textContent = `${state.search.done}/${state.search.total} queries`;
      return;
    }
    if (ev.status === "start") {
      li.className = "active";
      logLine(ev.node, `› ${ev.detail}`);
    } else if (ev.status === "done") {
      li.className = "done";
      logLine(ev.node, `✓ ${ev.detail}`, "ok");
    } else {
      li.className = "error";
      logLine(ev.node, `✗ ${ev.detail}`, "err");
    }
  }

  function finish(ev) {
    closeStream();
    setStatus("COMPLETE");
    for (const li of $("pipeline").children) if (li.className !== "error") li.className = "done";
    logLine("sys", "report ready", "ok");
    $("go").disabled = false;
    renderReport(ev.result, ev.run_id || state.runId);
    loadHistory();
    refreshQuota();
  }

  function fail(msg) {
    closeStream();
    setStatus("FAILED");
    for (const li of $("pipeline").children) if (li.className === "active") li.className = "error";
    logLine("sys", msg || "Run failed.", "err");
    if (state.me && state.me.user.is_admin && /anthropic|tavily|key|credit/i.test(msg || "")) {
      logLine("sys", "→ open KEYS (top bar) to test or replace your API keys", "warn");
    }
    $("go").disabled = false;
    loadHistory();
  }

  async function openRun(id) {
    const r = await api(`/api/runs/${id}`);
    if (!r.ok) {
      if (r.status === 401) return toAuth();
      return runMsg("That scan could not be found.");
    }
    const run = r.data;
    runMsg("");
    $("idea").value = run.idea;
    if (run.status === "running") return startStream(id);
    closeStream();
    state.runId = id;
    history.replaceState(null, "", `#run=${id}`);
    highlightHistory();
    resetPipeline();
    $("go").disabled = false;
    if (run.status === "done" && run.result) {
      for (const li of $("pipeline").children) li.className = "done";
      setStatus("ARCHIVED");
      $("run-cost").textContent = `$${Number(run.cost_usd || 0).toFixed(4)}`;
      logLine("sys", `loaded from archive · ${fmtDate(run.created_at)}`, "ok");
      renderReport(run.result, id);
    } else {
      setStatus("FAILED");
      logLine("sys", run.error || "Run failed.", "err");
      $("report").hidden = true;
    }
  }

  // ---------- History ----------

  async function loadHistory() {
    const r = await api("/api/runs");
    if (!r.ok) return;
    const runs = r.data.runs;
    $("archive-count").textContent = String(runs.length);
    $("history-empty").hidden = runs.length > 0;
    $("history").replaceChildren(...runs.map((run) => {
      const status = run.status === "done"
        ? h("span", { class: VERDICT[run.verdict] || "" }, `${run.score} ${run.verdict}`)
        : h("span", { class: run.status === "failed" ? "c-red" : "c-amber" }, run.status.toUpperCase());
      return h("li", {},
        h("button", { type: "button", "data-id": run.id, onclick: () => openRun(run.id) },
          h("span", { class: "h-title" }, run.title || run.idea),
          h("span", { class: "h-meta" }, status, h("span", {}, run.mode === "deep" ? "DEEP" : "ECO"),
            h("span", {}, fmtDate(run.created_at)))));
    }));
    highlightHistory();
  }

  function highlightHistory() {
    for (const b of $("history").querySelectorAll("button")) {
      b.setAttribute("aria-current", String(b.dataset.id === state.runId));
    }
  }

  // ---------- Report ----------

  function panel(title, code, ...children) {
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("span", {}, title), h("span", { class: "code" }, code || "")),
      children);
  }

  function jumpToSource(id) {
    const el = document.getElementById(`src-${id}`);
    if (!el) return;
    el.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" });
    el.classList.remove("flash");
    void el.offsetWidth;
    el.classList.add("flash");
  }

  function extraChips(text, ids) {
    const cited = new Set(String(text ?? "").match(/S\d+/g) || []);
    return (ids || []).filter((id) => !cited.has(id)).map(chip);
  }

  function chip(id) {
    if (!/^S\d+$/.test(id)) return null;
    return h("button", { type: "button", class: "cite", onclick: () => jumpToSource(id) }, id);
  }

  // Turn "[S1]" or "[S1, S3]" inside model text into clickable source chips.
  function cite(text) {
    const out = [];
    const re = /\[(S\d+(?:\s*,\s*S\d+)*)\]/g;
    const s = String(text ?? "");
    let last = 0;
    let m;
    while ((m = re.exec(s))) {
      if (m.index > last) out.push(s.slice(last, m.index));
      for (const id of m[1].split(/\s*,\s*/)) out.push(chip(id));
      last = re.lastIndex;
    }
    if (last < s.length) out.push(s.slice(last));
    return out;
  }

  function list(items, cls) {
    const arr = Array.isArray(items) ? items : [];
    return h("ul", { class: cls || "" },
      arr.length ? arr.map((i) => h("li", {}, cite(i))) : h("li", { class: "dim" }, "none found"));
  }

  function segs(score) {
    const n = Math.max(0, Math.min(5, Number(score) || 0));
    const lvl = n <= 2 ? "s-low" : n === 3 ? "s-mid" : "s-high";
    const wrap = h("span", { class: "segs", role: "img", "aria-label": `${n} out of 5` });
    for (let i = 0; i < 5; i += 1) wrap.append(h("span", { class: i < n ? `seg on ${lvl}` : "seg" }));
    return wrap;
  }

  function gauge(value, cls) {
    const r = 54;
    const c = 2 * Math.PI * r;
    const pct = Math.max(0, Math.min(100, Number(value) || 0));
    const g = svg("svg", { class: "gauge", viewBox: "0 0 132 132", role: "img", "aria-label": `Score ${pct} out of 100` });
    g.append(svg("circle", { cx: 66, cy: 66, r, fill: "none", class: "track", "stroke-width": 8 }));
    for (let i = 0; i < 40; i += 1) {
      const a = (i / 40) * 2 * Math.PI;
      g.append(svg("line", {
        x1: 66 + Math.cos(a) * 62, y1: 66 + Math.sin(a) * 62,
        x2: 66 + Math.cos(a) * (i % 5 ? 64 : 66), y2: 66 + Math.sin(a) * (i % 5 ? 64 : 66),
        stroke: "#27414b", "stroke-width": 1,
      }));
    }
    const arc = svg("circle", {
      cx: 66, cy: 66, r, fill: "none", class: `arc ${cls}`, "stroke-width": 8,
      "stroke-dasharray": c.toFixed(2), transform: "rotate(-90 66 66)",
    });
    arc.style.strokeDashoffset = String(c);
    g.append(arc);
    const num = svg("text", { x: 66, y: 74, "text-anchor": "middle", "font-size": 36, "font-weight": 700 });
    num.textContent = String(pct);
    const sub = svg("text", { x: 66, y: 92, "text-anchor": "middle", "font-size": 10, fill: "#6c878e", "letter-spacing": 2 });
    sub.textContent = "/ 100";
    g.append(num, sub);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      arc.style.strokeDashoffset = String(c * (1 - pct / 100));
    }));
    return g;
  }

  function scoreRow(c) {
    return h("div", { class: "score-row" },
      h("div", { class: "label" }, c.label, h("small", {}, `${Math.round(c.weight * 100)}% weight · ${c.score}/5`)),
      segs(c.score),
      h("div", { class: "why" }, cite(c.justification), " ", extraChips(c.justification, c.source_ids)));
  }

  function rival(c) {
    const url = safeUrl(c.url);
    const name = url
      ? h("a", { class: "rival-name", href: url, target: "_blank", rel: "noopener noreferrer" }, c.name)
      : h("span", { class: "rival-name" }, c.name);
    return h("div", { class: "rival" },
      h("div", { class: "rival-head" }, name, h("span", { title: `similarity ${c.similarity}/5` }, segs(c.similarity))),
      h("div", {}, cite(c.description)),
      h("div", { class: "rival-facts" },
        h("span", {}, "PRICING ", h("b", {}, cite(c.pricing))),
        h("span", {}, "TRACTION ", h("b", {}, cite(c.traction))),
        h("span", {}, extraChips(`${c.description} ${c.pricing} ${c.traction}`, c.source_ids))));
  }

  function renderReport(r, runId) {
    const { brief: b, competitors: comp, business: biz, plan, scorecard: card, score, usage } = r;
    const vc = VERDICT[score.verdict] || "c-dim";
    const [existsLabel, existsCls] = EXISTS[comp.existence] || [comp.existence, "c-dim"];
    const models = [...new Set((usage.by_node || []).map((u) => prettyModel(u.model)))].join(" + ");

    const telemetry = h("dl", { class: "meta" },
      h("dt", {}, "MODE"), h("dd", {}, r.mode === "deep" ? "DEEP" : "ECO"),
      h("dt", {}, "MODELS"), h("dd", {}, models || "n/a"),
      h("dt", {}, "LLM CALLS"), h("dd", {}, usage.llm_calls),
      h("dt", {}, "TOKENS IN/OUT"), h("dd", {}, `${kilo(usage.input_tokens)} / ${kilo(usage.output_tokens)}`),
      h("dt", {}, "SEARCH CREDITS"), h("dd", {}, usage.search_credits),
      h("dt", {}, "RESEARCH ROUNDS"), h("dd", {}, r.research_rounds),
      h("dt", {}, "SOURCES"), h("dd", {}, r.sources.length),
      h("dt", {}, "COST"), h("dd", { class: "c-mint" }, `$${Number(usage.cost_usd).toFixed(4)}`));

    const report = $("report");
    report.replaceChildren();
    append(report, [
      r.warnings && r.warnings.length
        ? h("div", { class: "warnbar", role: "status" }, "⚠ ", r.warnings.join(" · "))
        : null,
      h("div", { class: "grid-3" },
        panel("MOD-03 // VERDICT", `confidence ${score.confidence}`,
          h("div", { class: "verdict" },
            gauge(score.overall, vc),
            h("div", {},
              h("h2", {}, b.title),
              h("p", { class: "one-liner" }, b.one_liner),
              h("span", { class: `tag ${vc}` }, score.verdict),
              h("span", { class: `tag ${CONFIDENCE[score.confidence] || "c-dim"}` }, `CONF ${score.confidence.toUpperCase()}`))),
          h("p", {}, cite(card.bottom_line))),
        panel("MOD-04 // DOES IT EXIST?", `${comp.competitors.length} rivals`,
          h("div", { class: `exists-tag ${existsCls}` }, existsLabel),
          h("p", {}, cite(comp.existence_summary)),
          h("h3", {}, "Target customer"), h("p", {}, b.target_customer),
          h("h3", {}, "Category"), h("p", {}, b.category)),
        panel("MOD-05 // TELEMETRY", new Date(r.generated_at).toLocaleDateString(),
          telemetry,
          h("div", { class: "report-actions" },
            h("a", { class: "btn btn-ghost", href: `/api/runs/${runId}/report.md`, download: "" }, "DOWNLOAD .MD"),
            h("button", { class: "btn btn-ghost", type: "button", onclick: newScan }, "NEW SCAN")))),
      panel("MOD-06 // SCORECARD", "fixed weights · math done in code",
        card.criteria.map(scoreRow),
        h("h3", {}, "Why it could fail"), list(card.premortem, "risk")),
      h("div", { class: "grid-2" },
        panel("MOD-07 // RIVALS", "most similar first",
          h("div", { class: "rivals" }, comp.competitors.length
            ? comp.competitors.map(rival)
            : h("p", { class: "dim" }, "No competitors found in the sources. That means either a gap, or no market.")),
          h("h3", {}, "Gaps incumbents leave open"), list(comp.gaps),
          h("h3", {}, "Demand signals"), list(comp.demand_signals)),
        panel("MOD-08 // MONEY", "how comparables earn",
          h("p", {}, cite(biz.summary)),
          h("div", { class: "models" }, biz.comparable_models.map((m) => h("div", { class: "model" },
            h("b", {}, m.model), " ",
            h("span", { class: `tag ${FIT[m.fit] || "c-dim"}` }, `${String(m.fit).toUpperCase()} FIT`),
            h("div", {}, cite(m.how_it_works)),
            m.seen_at.length ? h("div", { class: "dim" }, `seen at: ${m.seen_at.join(", ")}`) : null))),
          h("h3", {}, "Recommended model"), h("p", {}, cite(biz.recommended_model)),
          h("h3", {}, "Unit economics (rough)"), h("p", {}, cite(biz.unit_economics)),
          h("h3", {}, "Price points seen"), list(biz.price_points),
          h("h3", {}, "Market signals"), list(biz.market_signals))),
      panel("MOD-09 // EXECUTION", "how to actually do it",
        h("h3", {}, "Positioning / wedge"), h("p", {}, cite(plan.positioning)),
        h("div", { class: "callout" }, h("strong", {}, "VALIDATION TEST // THIS WEEK"), cite(plan.validation_test)),
        h("div", { class: "grid-2" },
          h("div", {}, h("h3", {}, "MVP scope"), list(plan.mvp_scope)),
          h("div", {}, h("h3", {}, "Don't build yet"), list(plan.not_to_build))),
        h("div", { class: "grid-2" },
          h("div", {}, h("h3", {}, "First 10 customers"), list(plan.first_customers)),
          h("div", {}, h("h3", {}, "Pricing hypothesis"), h("p", {}, cite(plan.pricing_hypothesis)),
            h("h3", {}, "Metrics to watch"), list(plan.key_metrics))),
        h("h3", {}, "30 / 60 / 90"),
        h("div", { class: "roadmap" }, plan.roadmap.map((p) => h("div", {}, h("h4", {}, p.window), list(p.goals))))),
      panel("MOD-10 // SOURCES", `${r.sources.length} cited`,
        h("ol", { class: "sources" }, r.sources.map((s) => {
          const url = safeUrl(s.url);
          const id = /^S\d+$/.test(s.id) ? s.id : "";
          return h("li", { id: id ? `src-${id}` : null },
            h("span", { class: "sid" }, `[${s.id}]`),
            url ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer", title: url }, s.title || url)
              : h("span", {}, s.title));
        }))),
    ]);
    report.hidden = false;
    report.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
  }

  function newScan() {
    closeStream();
    state.runId = null;
    history.replaceState(null, "", location.pathname);
    highlightHistory();
    resetPipeline();
    setStatus("IDLE");
    $("go").disabled = false;
    $("report").hidden = true;
    $("idea").value = "";
    window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
    $("idea").focus();
  }

  // ---------- Clock & init ----------

  function tick() {
    const now = new Date();
    $("clock").textContent = now.toLocaleTimeString([], { hour12: false });
    if (state.running && state.startedAt) {
      const s = Math.floor((Date.now() - state.startedAt) / 1000);
      const status = $("run-status").textContent;
      if (status.startsWith("RUNNING")) {
        setStatus(`RUNNING ${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`);
      }
    }
  }

  async function init() {
    tick();
    setInterval(tick, 1000);
    try {
      if (localStorage.getItem("itig-mode") === "deep") state.mode = "deep";
    } catch { /* storage unavailable */ }
    const r = await api("/api/me");
    if (r.ok) enterDeck();
    else toAuth();
  }

  init();
})();
