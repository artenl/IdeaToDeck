"use strict";

// IDEA//DECK front-end. Everything that comes from the server or the web is rendered
// with textContent / createElement only; there is no innerHTML anywhere.
// Interface strings live in i18n.js; t("key", {params}) picks the current language.

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
    statusKey: "idle",
    loginMsgKey: "",
    runMsg: null, // {key, params} or {text}
    keys: null,
    checks: null,
    keysIntro: "default",
    report: null, // {result, runId}
  };

  // ---------- Language ----------

  const DICT = window.IDECK_I18N || { en: {} };
  const LANGS = ["en", "fr"];

  function detectLang() {
    try {
      const saved = localStorage.getItem("itig-lang");
      if (LANGS.includes(saved)) return saved;
    } catch { /* storage unavailable */ }
    return (navigator.language || "").toLowerCase().startsWith("fr") ? "fr" : "en";
  }

  let lang = detectLang();

  function has(key) {
    return key in (DICT[lang] || {}) || key in DICT.en;
  }

  function t(key, params) {
    let s = (DICT[lang] && DICT[lang][key]) ?? DICT.en[key] ?? key;
    if (params) s = s.replace(/\{(\w+)\}/g, (m, k) => (params[k] ?? m));
    return s;
  }

  // Use a translation when one exists, otherwise the server's own (English) text.
  function tOr(key, params, fallback) {
    return has(key) ? t(key, params) : fallback;
  }

  function locale() {
    return lang === "fr" ? "fr-FR" : "en-GB";
  }

  function applyStatic() {
    document.documentElement.lang = lang;
    for (const el of document.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
    for (const el of document.querySelectorAll("[data-i18n-ph]")) el.placeholder = t(el.dataset.i18nPh);
    for (const el of document.querySelectorAll("[data-i18n-aria]")) {
      el.setAttribute("aria-label", t(el.dataset.i18nAria));
    }
    $("soon-title").dataset.text = t("soon.title");
    for (const b of document.querySelectorAll(".lang-switch button")) {
      b.setAttribute("aria-pressed", String(b.dataset.lang === lang));
    }
  }

  function setLang(next) {
    if (!LANGS.includes(next) || next === lang) return;
    lang = next;
    try { localStorage.setItem("itig-lang", lang); } catch { /* storage unavailable */ }
    applyStatic();
    // Re-render everything currently on screen in the new language.
    renderStatus();
    renderSearchCount();
    renderLoginMsg();
    renderRunMsg();
    renderKeysIntro();
    if (state.keys) renderKeys(state.keys);
    if (state.checks) renderChecks(state.checks);
    if (!$("auth").hidden) boot(false);
    if (state.me) {
      renderQuota(state.me.quota);
      setMode(state.mode);
      loadHistory();
    }
    if (state.report && !$("report").hidden) {
      renderReport(state.report.result, state.report.runId, false);
    }
  }

  for (const b of document.querySelectorAll(".lang-switch button")) {
    b.addEventListener("click", () => setLang(b.dataset.lang));
  }

  // ---------- Constants ----------

  const VERDICT = { PURSUE: "c-mint", PIVOT: "c-amber", DROP: "c-red" };
  const CONFIDENCE = { high: "c-mint", medium: "c-amber", low: "c-red" };
  const FIT = { strong: "c-mint", moderate: "c-amber", weak: "c-red" };
  const EXISTS = { exact: "c-red", close: "c-amber", adjacent: "c-mint", none_found: "c-cyan" };
  const KEY_PROBLEMS = new Set(["auth", "no_credits", "permission", "model_not_found"]);

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
    return d.toLocaleDateString(locale(), { month: "short", day: "numeric" }) + " " +
      d.toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit", hour12: false });
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
      return { ok: false, status: 0, data: null };
    }
  }

  // API errors carry {code, message}; translate the code, fall back to the message.
  function errText(r) {
    if (r.status === 0) return t("api.network");
    const d = r.data && r.data.detail;
    if (d && typeof d === "object" && !Array.isArray(d) && d.code) {
      return tOr(`api.${d.code}`, null, d.message || t("api.http", { status: r.status }));
    }
    if (Array.isArray(d)) return t("api.validation");
    if (typeof d === "string") return d;
    return t("api.http", { status: r.status });
  }

  // ---------- Screens ----------

  function show(screen) {
    for (const id of ["auth", "soon", "deck"]) $(id).hidden = id !== screen;
    $("strip").hidden = screen !== "deck";
    $("who").hidden = screen !== "deck";
  }

  const BOOT = [
    ["boot.bios", "boot.ok"],
    ["boot.memory", "boot.ok"],
    ["boot.core", "boot.linked"],
    ["boot.recon", "boot.linked"],
    ["boot.analysts", "boot.linked"],
    ["boot.whitelist", "boot.enforced"],
  ];
  let bootToken = 0;

  async function boot(animate = true) {
    const token = ++bootToken;
    const pre = $("boot");
    pre.replaceChildren();
    for (const [label, status] of BOOT) {
      if (token !== bootToken) return;
      pre.append(h("span", {}, `${t(label)} `.padEnd(30, "."), " ",
        h("span", { class: status === "boot.enforced" ? "warn" : "ok" }, t(status)), "\n"));
      if (animate && !reduceMotion) await sleep(130);
    }
    pre.append(h("span", {}, t("boot.prompt")));
    if (animate) $("email").focus();
  }

  function toAuth() {
    closeStream();
    state.me = null;
    state.report = null;
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
    setStatus("idle");
    show("deck");
    $("keys-btn").hidden = !r.data.user.is_admin;
    $("keys-panel").hidden = true;
    if (r.data.user.is_admin && !r.data.keys_ready) openKeys("first");
    await loadHistory();
    const m = location.hash.match(/^#run=([a-f0-9]{32})$/);
    if (m) openRun(m[1]);
    else $("idea").focus();
  }

  function renderQuota(q) {
    $("strip-quota").textContent = q.limit == null
      ? t("quota.unlimited")
      : t("quota.month", { used: q.used, limit: q.limit });
  }

  async function refreshQuota() {
    const r = await api("/api/me");
    if (r.ok) {
      state.me = r.data;
      renderQuota(r.data.quota);
    }
  }

  // ---------- Login ----------

  function renderLoginMsg() {
    $("login-msg").textContent = state.loginMsgKey ? t(state.loginMsgKey) : "";
  }

  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    state.loginMsgKey = "";
    renderLoginMsg();
    $("login-btn").disabled = true;
    const r = await api("/api/login", {
      method: "POST",
      body: { email: $("email").value, password: $("password").value },
    });
    $("login-btn").disabled = false;
    $("password").value = "";
    if (r.ok && r.data.status === "ok") return enterDeck();
    if (r.ok && r.data.status === "coming_soon") return show("soon");
    if (r.status === 429) state.loginMsgKey = "auth.locked";
    else if (r.status === 0) state.loginMsgKey = "api.network";
    else state.loginMsgKey = "auth.denied";
    renderLoginMsg();
  });

  $("soon-back").addEventListener("click", () => toAuth());
  $("logout").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" });
    toAuth();
  });

  // ---------- API keys (admins) ----------

  const CHECK_STYLE = {
    ok: ["c-mint", "✓", "ok"], no_credits: ["c-amber", "⚠", "warn"],
    invalid: ["c-red", "✗", "bad"], error: ["c-red", "✗", "bad"], missing: ["c-dim", "·", ""],
  };

  function keyRow(name) {
    return $("keys-form").querySelector(`.key-row[data-key="${name}"]`);
  }

  function renderKeysIntro() {
    const intro = $("keys-intro");
    const key = { first: "keys.first", ready: "keys.ready" }[state.keysIntro] || "keys.intro";
    intro.textContent = t(key);
    intro.classList.toggle("alert", state.keysIntro === "first");
  }

  function renderKeys(keys) {
    state.keys = keys;
    for (const [name, k] of Object.entries(keys || {})) {
      const row = keyRow(name);
      if (!row) continue;
      row.querySelector(".key-masked").textContent = k.masked || "—";
      row.querySelector(".key-source").textContent = tOr(`keys.src.${k.source}`, null, k.source);
      row.querySelector(".led").className = k.configured ? "led set" : "led";
    }
  }

  function renderChecks(checks) {
    state.checks = checks;
    for (const [name, c] of Object.entries(checks || {})) {
      const row = keyRow(name);
      if (!row) continue;
      const [cls, icon, led] = CHECK_STYLE[c.status] || ["c-red", "✗", "bad"];
      const result = row.querySelector(".key-result");
      result.className = `key-result ${cls}`;
      result.textContent = `${icon} ${tOr(`check.${name}.${c.status}`, c, c.message)}`;
      if (led) row.querySelector(".led").className = `led ${led}`;
    }
  }

  async function openKeys(intro) {
    if (intro) state.keysIntro = intro;
    renderKeysIntro();
    $("keys-panel").hidden = false;
    $("keys-panel").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    const r = await api("/api/admin/keys");
    if (r.ok) renderKeys(r.data.keys);
    const empty = [...$("keys-form").querySelectorAll("input")].find((i) => !i.value);
    if (empty) empty.focus();
  }

  async function submitKeys(path, method, body) {
    for (const b of [$("keys-save"), $("keys-test")]) b.disabled = true;
    state.checks = null;
    for (const p of $("keys-form").querySelectorAll(".key-result")) {
      p.className = "key-result c-dim";
      p.textContent = `· ${t("keys.testing")}`;
    }
    const r = await api(path, { method, body });
    for (const b of [$("keys-save"), $("keys-test")]) b.disabled = false;
    for (const p of $("keys-form").querySelectorAll(".key-result")) {
      if (p.classList.contains("c-dim")) p.textContent = "";
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
      state.keysIntro = "ready";
      renderKeysIntro();
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
      ? t("mode.deep_models", { model: prettyModel(models.deep) })
      : t("mode.all", { model: prettyModel(models.cheap) });
    $("strip-model").textContent = deep
      ? `${prettyModel(models.cheap)} + ${prettyModel(models.deep)}`
      : prettyModel(models.cheap);
    $("strip-model").classList.toggle("deep", deep);
    try { localStorage.setItem("itig-mode", mode); } catch { /* storage unavailable */ }
  }

  $("mode").addEventListener("click", () => setMode(state.mode === "deep" ? "cheap" : "deep"));

  // ---------- Runs ----------

  function renderRunMsg() {
    const m = state.runMsg;
    $("run-msg").textContent = !m ? "" : m.key ? t(m.key, m.params) : m.text;
  }

  function runMsg(msg) {
    state.runMsg = msg || null;
    renderRunMsg();
  }

  $("idea").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("run-form").requestSubmit();
  });

  $("run-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const idea = $("idea").value.trim();
    if (idea.length < 8) return runMsg({ key: "input.too_short" });
    runMsg(null);
    $("go").disabled = true;
    const r = await api("/api/runs", {
      method: "POST",
      body: { idea, mode: state.mode, profile: $("profile").value.trim(), lang },
    });
    if (!r.ok) {
      $("go").disabled = false;
      if (r.status === 401) return toAuth();
      return runMsg({ text: errText(r) });
    }
    $("report").hidden = true;
    state.report = null;
    startStream(r.data.id);
    loadHistory();
  });

  function stage(node) {
    return $("pipeline").querySelector(`li[data-node="${node}"]`);
  }

  function renderSearchCount() {
    const s = state.search;
    $("search-count").textContent = s.total ? t("search.count", s) : t("search.idle");
  }

  function resetPipeline() {
    for (const li of $("pipeline").children) li.className = "";
    state.cost = 0;
    state.search = { total: 0, done: 0, failed: 0 };
    renderSearchCount();
    $("run-cost").textContent = "$0.0000";
    $("log").replaceChildren();
  }

  function renderStatus() {
    let text = t(`status.${state.statusKey}`);
    if (state.statusKey === "running" && state.running && state.startedAt) {
      const s = Math.floor((Date.now() - state.startedAt) / 1000);
      text += ` ${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
    }
    $("run-status").textContent = text;
  }

  function setStatus(key) {
    state.statusKey = key;
    renderStatus();
  }

  function logLine(node, text, cls) {
    const log = $("log");
    const time = new Date().toLocaleTimeString(locale(), { hour12: false });
    log.append(h("span", { class: "line" },
      h("span", { class: "t" }, `${time} `),
      h("span", { class: "n" }, `${tOr(`node.${node}`, null, node).padEnd(9)} `),
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
    setStatus("connecting");
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
      if (es.readyState === EventSource.CLOSED) fail(t("log.lost"));
      else setStatus("reconnecting");
    };
  }

  function onEvent(ev) {
    switch (ev.type) {
      case "status":
        setStatus("running");
        logLine("sys", t("log.engaged"), "ok");
        break;
      case "log":
        logLine("sys", ev.code ? tOr(`log.${ev.code}`, null, ev.msg) : ev.msg, "warn");
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
        fail(ev.code ? tOr(`err.${ev.code}`, { detail: ev.detail || "" }, ev.msg) : ev.msg,
          KEY_PROBLEMS.has(ev.code));
        break;
      default:
        break;
    }
  }

  // Server events carry structured `data`; build the log text in the current language.
  function nodeText(ev) {
    const d = ev.data || {};
    if (ev.status === "start") {
      return ev.node === "search" ? d.query || ev.detail : tOr(`start.${ev.node}`, null, ev.detail);
    }
    if (ev.status === "error" || !Object.keys(d).length) return ev.detail;
    switch (ev.node) {
      case "evidence_check":
        return d.round ? t("done.evidence_check.more", d) : t("done.evidence_check.enough");
      case "judge":
        return t("done.judge", { score: d.score, verdict: t(`verdict.${d.verdict}`) });
      case "competitor_analyst":
        return t("done.competitor_analyst", {
          existence: t(`exists.${d.existence}`).toLowerCase(), competitors: d.competitors,
        });
      case "report":
        return t("done.report", { score: d.score, cost: Number(d.cost || 0).toFixed(4) });
      default:
        return tOr(`done.${ev.node}`, d, ev.detail);
    }
  }

  function onNode(ev) {
    const li = stage(ev.node);
    if (!li) return;
    const text = nodeText(ev);
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
        logLine("search", `› ${text}`);
      } else {
        state.search.done += 1;
        if (ev.status === "error") state.search.failed += 1;
        logLine("search", `${ev.status === "error" ? "✗" : "✓"} ${text}`, ev.status === "error" ? "err" : "ok");
        if (state.search.done >= state.search.total) {
          li.className = state.search.failed === state.search.total ? "error" : "done";
        }
      }
      renderSearchCount();
      return;
    }
    if (ev.status === "start") {
      li.className = "active";
      logLine(ev.node, `› ${text}`);
    } else if (ev.status === "done") {
      li.className = "done";
      logLine(ev.node, `✓ ${text}`, "ok");
    } else {
      li.className = "error";
      logLine(ev.node, `✗ ${text}`, "err");
    }
  }

  function finish(ev) {
    closeStream();
    setStatus("complete");
    for (const li of $("pipeline").children) if (li.className !== "error") li.className = "done";
    logLine("sys", t("log.ready"), "ok");
    $("go").disabled = false;
    renderReport(ev.result, ev.run_id || state.runId);
    loadHistory();
    refreshQuota();
  }

  function fail(msg, keyProblem = false) {
    closeStream();
    setStatus("failed");
    for (const li of $("pipeline").children) if (li.className === "active") li.className = "error";
    logLine("sys", msg || t("log.failed"), "err");
    if (keyProblem && state.me && state.me.user.is_admin) logLine("sys", t("log.keys_hint"), "warn");
    $("go").disabled = false;
    loadHistory();
  }

  async function openRun(id) {
    const r = await api(`/api/runs/${id}`);
    if (!r.ok) {
      if (r.status === 401) return toAuth();
      return runMsg({ key: "run.not_found" });
    }
    const run = r.data;
    runMsg(null);
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
      setStatus("archived");
      $("run-cost").textContent = `$${Number(run.cost_usd || 0).toFixed(4)}`;
      logLine("sys", t("log.archive", { date: fmtDate(run.created_at) }), "ok");
      renderReport(run.result, id);
    } else {
      setStatus("failed");
      logLine("sys", run.error || t("log.failed"), "err");
      $("report").hidden = true;
      state.report = null;
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
        ? h("span", { class: VERDICT[run.verdict] || "" }, `${run.score} ${tOr(`verdict.${run.verdict}`, null, run.verdict)}`)
        : h("span", { class: run.status === "failed" ? "c-red" : "c-amber" },
          tOr(`hist.${run.status}`, null, run.status.toUpperCase()));
      return h("li", {},
        h("button", { type: "button", "data-id": run.id, onclick: () => openRun(run.id) },
          h("span", { class: "h-title" }, run.title || run.idea),
          h("span", { class: "h-meta" }, status, h("span", {}, t(`mode.short.${run.mode === "deep" ? "deep" : "cheap"}`)),
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
      arr.length ? arr.map((i) => h("li", {}, cite(i))) : h("li", { class: "dim" }, t("rep.none")));
  }

  function segs(score) {
    const n = Math.max(0, Math.min(5, Number(score) || 0));
    const lvl = n <= 2 ? "s-low" : n === 3 ? "s-mid" : "s-high";
    const wrap = h("span", { class: "segs", role: "img", "aria-label": t("rep.segs_aria", { n }) });
    for (let i = 0; i < 5; i += 1) wrap.append(h("span", { class: i < n ? `seg on ${lvl}` : "seg" }));
    return wrap;
  }

  function gauge(value, cls, animate) {
    const r = 54;
    const c = 2 * Math.PI * r;
    const pct = Math.max(0, Math.min(100, Number(value) || 0));
    const g = svg("svg", { class: "gauge", viewBox: "0 0 132 132", role: "img", "aria-label": t("rep.score_aria", { n: pct }) });
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
    const target = String(c * (1 - pct / 100));
    arc.style.strokeDashoffset = animate ? String(c) : target;
    g.append(arc);
    const num = svg("text", { x: 66, y: 74, "text-anchor": "middle", "font-size": 36, "font-weight": 700 });
    num.textContent = String(pct);
    const sub = svg("text", { x: 66, y: 92, "text-anchor": "middle", "font-size": 10, fill: "#6c878e", "letter-spacing": 2 });
    sub.textContent = "/ 100";
    g.append(num, sub);
    if (animate) {
      requestAnimationFrame(() => requestAnimationFrame(() => { arc.style.strokeDashoffset = target; }));
    }
    return g;
  }

  function scoreRow(c) {
    return h("div", { class: "score-row" },
      h("div", { class: "label" }, tOr(`crit.${c.key}`, null, c.label),
        h("small", {}, t("rep.weight", { w: Math.round(c.weight * 100), s: c.score }))),
      segs(c.score),
      h("div", { class: "why" }, cite(c.justification), " ", extraChips(c.justification, c.source_ids)));
  }

  function rival(c) {
    const url = safeUrl(c.url);
    const name = url
      ? h("a", { class: "rival-name", href: url, target: "_blank", rel: "noopener noreferrer" }, c.name)
      : h("span", { class: "rival-name" }, c.name);
    return h("div", { class: "rival" },
      h("div", { class: "rival-head" }, name, h("span", { title: t("rep.similarity", { n: c.similarity }) }, segs(c.similarity))),
      h("div", {}, cite(c.description)),
      h("div", { class: "rival-facts" },
        h("span", {}, `${t("rep.pricing")} `, h("b", {}, cite(c.pricing))),
        h("span", {}, `${t("rep.traction")} `, h("b", {}, cite(c.traction))),
        h("span", {}, extraChips(`${c.description} ${c.pricing} ${c.traction}`, c.source_ids))));
  }

  function renderReport(r, runId, scroll = true) {
    state.report = { result: r, runId };
    const { brief: b, competitors: comp, business: biz, plan, scorecard: card, score, usage } = r;
    const vc = VERDICT[score.verdict] || "c-dim";
    const level = tOr(`level.${score.confidence}`, null, score.confidence);
    const models = [...new Set((usage.by_node || []).map((u) => prettyModel(u.model)))].join(" + ");

    const telemetry = h("dl", { class: "meta" },
      h("dt", {}, t("tel.mode")), h("dd", {}, t(`mode.short.${r.mode === "deep" ? "deep" : "cheap"}`)),
      h("dt", {}, t("tel.lang")), h("dd", {}, tOr(`langname.${r.lang || "en"}`, null, r.lang || "en")),
      h("dt", {}, t("tel.models")), h("dd", {}, models || "n/a"),
      h("dt", {}, t("tel.calls")), h("dd", {}, usage.llm_calls),
      h("dt", {}, t("tel.tokens")), h("dd", {}, `${kilo(usage.input_tokens)} / ${kilo(usage.output_tokens)}`),
      h("dt", {}, t("tel.credits")), h("dd", {}, usage.search_credits),
      h("dt", {}, t("tel.rounds")), h("dd", {}, r.research_rounds),
      h("dt", {}, t("tel.sources")), h("dd", {}, r.sources.length),
      h("dt", {}, t("tel.cost")), h("dd", { class: "c-mint" }, `$${Number(usage.cost_usd).toFixed(4)}`));

    const report = $("report");
    report.replaceChildren();
    append(report, [
      r.warnings && r.warnings.length
        ? h("div", { class: "warnbar", role: "status" }, "⚠ ", r.warnings.join(" · "))
        : null,
      h("div", { class: "grid-3" },
        panel(t("rep.verdict"), t("rep.confidence", { level: level.toLowerCase() }),
          h("div", { class: "verdict" },
            gauge(score.overall, vc, scroll),
            h("div", {},
              h("h2", {}, b.title),
              h("p", { class: "one-liner" }, b.one_liner),
              h("span", { class: `tag ${vc}` }, tOr(`verdict.${score.verdict}`, null, score.verdict)),
              h("span", { class: `tag ${CONFIDENCE[score.confidence] || "c-dim"}` }, t("rep.conf_tag", { level })))),
          h("p", {}, cite(card.bottom_line))),
        panel(t("rep.exists"), t("rep.rivals_count", { n: comp.competitors.length }),
          h("div", { class: `exists-tag ${EXISTS[comp.existence] || "c-dim"}` },
            tOr(`exists.${comp.existence}`, null, comp.existence)),
          h("p", {}, cite(comp.existence_summary)),
          h("h3", {}, t("rep.target")), h("p", {}, b.target_customer),
          h("h3", {}, t("rep.category")), h("p", {}, b.category)),
        panel(t("rep.telemetry"), new Date(r.generated_at).toLocaleDateString(locale()),
          telemetry,
          h("div", { class: "report-actions" },
            h("a", { class: "btn btn-ghost", href: `/api/runs/${runId}/report.md`, download: "" }, t("rep.download")),
            h("button", { class: "btn btn-ghost", type: "button", onclick: newScan }, t("rep.new"))))),
      panel(t("rep.scorecard"), t("rep.scorecard_code"),
        card.criteria.map(scoreRow),
        h("h3", {}, t("rep.premortem")), list(card.premortem, "risk")),
      h("div", { class: "grid-2" },
        panel(t("rep.rivals"), t("rep.rivals_code"),
          h("div", { class: "rivals" }, comp.competitors.length
            ? comp.competitors.map(rival)
            : h("p", { class: "dim" }, t("rep.no_rivals"))),
          h("h3", {}, t("rep.gaps")), list(comp.gaps),
          h("h3", {}, t("rep.demand")), list(comp.demand_signals)),
        panel(t("rep.money"), t("rep.money_code"),
          h("p", {}, cite(biz.summary)),
          h("div", { class: "models" }, biz.comparable_models.map((m) => h("div", { class: "model" },
            h("b", {}, m.model), " ",
            h("span", { class: `tag ${FIT[m.fit] || "c-dim"}` }, t("rep.fit", { fit: tOr(`fit.${m.fit}`, null, m.fit) })),
            h("div", {}, cite(m.how_it_works)),
            m.seen_at.length ? h("div", { class: "dim" }, t("rep.seen_at", { names: m.seen_at.join(", ") })) : null))),
          h("h3", {}, t("rep.recommended")), h("p", {}, cite(biz.recommended_model)),
          h("h3", {}, t("rep.unit")), h("p", {}, cite(biz.unit_economics)),
          h("h3", {}, t("rep.prices")), list(biz.price_points),
          h("h3", {}, t("rep.market")), list(biz.market_signals))),
      panel(t("rep.exec"), t("rep.exec_code"),
        h("h3", {}, t("rep.positioning")), h("p", {}, cite(plan.positioning)),
        h("div", { class: "callout" }, h("strong", {}, t("rep.validation")), cite(plan.validation_test)),
        h("div", { class: "grid-2" },
          h("div", {}, h("h3", {}, t("rep.mvp")), list(plan.mvp_scope)),
          h("div", {}, h("h3", {}, t("rep.dont")), list(plan.not_to_build))),
        h("div", { class: "grid-2" },
          h("div", {}, h("h3", {}, t("rep.customers")), list(plan.first_customers)),
          h("div", {}, h("h3", {}, t("rep.pricing_h")), h("p", {}, cite(plan.pricing_hypothesis)),
            h("h3", {}, t("rep.metrics")), list(plan.key_metrics))),
        h("h3", {}, t("rep.roadmap")),
        h("div", { class: "roadmap" }, plan.roadmap.map((p) => h("div", {}, h("h4", {}, p.window), list(p.goals))))),
      panel(t("rep.sources"), t("rep.sources_code", { n: r.sources.length }),
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
    if (scroll) report.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
  }

  function newScan() {
    closeStream();
    state.runId = null;
    state.report = null;
    history.replaceState(null, "", location.pathname);
    highlightHistory();
    resetPipeline();
    setStatus("idle");
    $("go").disabled = false;
    $("report").hidden = true;
    $("idea").value = "";
    window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
    $("idea").focus();
  }

  // ---------- Clock & init ----------

  function tick() {
    $("clock").textContent = new Date().toLocaleTimeString(locale(), { hour12: false });
    if (state.running && state.statusKey === "running") renderStatus();
  }

  async function init() {
    applyStatic();
    renderStatus();
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
