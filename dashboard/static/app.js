// dashboard/static/app.js — shell: tab loader, api() helper, status/badges, toast/modal.
// Same-origin only. No CDN. Token via login modal -> X-Api-Token header;
// sessionStorage by default, localStorage when "remember me" is checked.
"use strict";
window.Tabs = {};
const App = {
  tab: "overview",
  statusTimer: null,
  mailTimer: null,
  mailBackoff: 15000,
  token() { return sessionStorage.getItem("asha_token") || localStorage.getItem("asha_token") || ""; },
  remembered() { return !!localStorage.getItem("asha_token"); },
  setToken() { this.login(); },  // legacy entry point (tokenbar button)
  login() {
    if (this._loginOpen) return;
    this._loginOpen = true;
    const cur = this.token(), rem = this.remembered();
    this.modal(`<h3>Login — API token</h3>
      <div class="muted">Sent as <code>X-Api-Token</code> header on every request. Stored only in this browser, never on the server.</div>
      <div style="margin:8px 0"><label>Token<br><input id="login-token" type="password" value="${this.esc(cur)}" style="width:100%" autocomplete="current-password"></label></div>
      <div style="margin:8px 0"><label><input id="login-remember" type="checkbox" ${rem ? "checked" : ""}> Remember me (stay logged in on this browser)</label></div>
      <div style="margin:8px 0"><button class="btn" onclick="App.doLogin()">Login</button>
      <button class="btn ghost" onclick="App.logout()">Clear</button>
      <button class="btn ghost" onclick="App.closeModal()">Cancel</button></div>
      <pre id="login-out" class="muted"></pre>`);
  },
  async doLogin() {
    const t = document.getElementById("login-token").value;
    const rem = document.getElementById("login-remember").checked;
    const out = document.getElementById("login-out");
    if (!t) { out.textContent = "Enter a token first."; return; }
    let res;
    try {
      res = await fetch("/api/status?db=all", {headers: {"X-Api-Token": t}});
    } catch (e) { out.textContent = "Error: " + e.message; return; }
    if (res.status === 401) { out.textContent = "Wrong token (401). Try again."; return; }
    if (!res.ok) { out.textContent = "Error: " + res.statusText; return; }
    if (rem) { localStorage.setItem("asha_token", t); sessionStorage.removeItem("asha_token"); }
    else { sessionStorage.setItem("asha_token", t); localStorage.removeItem("asha_token"); }
    this.closeModal();
    document.getElementById("tokenbar").style.display = "none";
    this.updateAuthBtn();
    this.refresh();
  },
  logout() {
    sessionStorage.removeItem("asha_token"); localStorage.removeItem("asha_token");
    this.closeModal();
    document.getElementById("tokenbar").style.display = "block";
    this.updateAuthBtn();
    this.refresh(true);
  },
  authBtn() { if (this.token()) this.logout(); else this.login(); },
  updateAuthBtn() {
    const b = document.getElementById("authbtn");
    if (b) b.textContent = this.token() ? "Logout" : "Login";
  },
  async api(url, opts = {}) {
    opts.headers = Object.assign({}, opts.headers || {});
    const t = this.token();
    if (t) opts.headers["X-Api-Token"] = t;
    const res = await fetch(url, opts);
    if (res.status === 401) {
      document.getElementById("tokenbar").style.display = "block";
      this.updateAuthBtn();
      if (!this.token()) this.login();  // locked out: offer the login form
      throw new Error("unauthorized (401) — log in");
    }
    if (!res.ok) {
      let msg = res.statusText;
      try { const j = await res.json(); msg = j.error || j.message || JSON.stringify(j); } catch (e) {}
      throw new Error(msg);
    }
    return res.json();
  },
  esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  },
  ago(ts) {
    if (!ts) return "never";
    const s = Math.floor(Date.now() / 1000 - ts);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    const d = Math.floor(s / 86400);
    return d + "d ago";
  },
  fdate(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    const p = (x) => String(x).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  },
  when(ts) {
    if (!ts) return "never";
    return `${this.ago(ts)} <span class="muted">(${this.fdate(ts)})</span>`;
  },
  attTag(a) {
    if (a === "review_ready") return `<span class="tag amber">review ready</span>`;
    if (a === "core_verified") return `<span class="tag green">verified</span>`;
    if (a === "agent_private") return `<span class="tag grey">private</span>`;
    return a ? `<span class="tag grey">${this.esc(a)}</span>` : `<span class="muted">—</span>`;
  },
  metaTable(meta) {
    const o = meta || {};
    const keys = Object.keys(o);
    if (!keys.length) return `<span class="muted">No extra data.</span>`;
    const names = {attention_state: "Attention", agent_id: "Owner agent",
      agent_ids: "Owner agents", agent_scoped: "Agent-scoped",
      promoted_from: "Promoted from", promoted_from_agent: "Promoted by",
      promoted_at: "Promoted", original_agents_node_id: "Was agent note",
      contradiction_flag: "Flagged clash", contradiction_pair: "Clashes with",
      contradictions_detected: "Clashes found", clock_node: "Clock note"};
    const fmtVal = (v) => {
      if (v && typeof v === "object") {
        if (v.agent_id && v.node_id) return `note ${this.esc(v.node_id)} of ${this.esc(v.agent_id)}`;
        return this.esc(JSON.stringify(v));
      }
      if (typeof v === "number" && v > 946684800 && v < 4102444800) return this.when(v);
      return this.esc(v === true ? "yes" : v === false ? "no" : v ?? "—");
    };
    return `<table class="plain">` + keys.sort().map(k =>
      `<tr><td class="muted">${this.esc(names[k] || k)}</td><td>${fmtVal(o[k])}</td></tr>`).join("") + `</table>`;
  },
  nodeDetail(n) {
    const meta = n.metadata || {};
    const att = meta.attention_state;
    return `<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
      <span class="tag blue">${this.esc(n.node_type)}</span>
      <span class="tag grey">${this.esc(n.layer || "working")}</span>${this.attTag(att)}
      ${n.agent_id ? `<span class="muted">by ${this.esc(n.agent_id)}</span>` : ""}</div>
      <h4 style="margin:8px 0 4px">${this.esc(n.label || "(no label)")}</h4>
      <pre style="white-space:pre-wrap;max-height:220px;overflow:auto;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px">${this.esc(n.content || "")}</pre>
      <table class="plain">
      <tr><td class="muted">Added</td><td>${this.when(n.created_at)}</td></tr>
      <tr><td class="muted">Changed</td><td>${this.when(n.updated_at)}</td></tr>
      <tr><td class="muted">Read</td><td><b>${n.access_count || 0}×</b>${n.last_access ? `, last ${this.when(n.last_access)}` : ", never"}</td></tr>
      <tr><td class="muted">Trust / weight</td><td>${n.trust_level ?? "?"} / ${n.importance ?? "?"}</td></tr>
      <tr><td class="muted">Source</td><td>${this.esc(n.source || "—")}</td></tr>
      <tr><td class="muted">ID</td><td style="font-size:11px">${this.esc(n.node_id)}</td></tr>
      </table>
      <h4 style="margin:8px 0 4px">Extra data</h4>${this.metaTable(meta)}`;
  },
  toast(msg) {
    const el = document.getElementById("toast");
    el.textContent = msg; el.style.display = "block";
    clearTimeout(this._tt); this._tt = setTimeout(() => el.style.display = "none", 2600);
  },
  modal(html) {
    document.getElementById("modal-body").innerHTML = html;
    document.getElementById("modal").style.display = "flex";
  },
  closeModal() {
    const m=document.querySelector("#modal>div");
    if(m){m.style.maxWidth="";m.style.width="";}
    document.getElementById("modal").style.display = "none"; this._loginOpen = false;
  },
  tabs: [
    ["overview", "Overview"], ["maintenance", "Maintenance"], ["graduate", "Graduate"],
    ["observer", "Observer"], ["core_helper", "Core Helper"], ["contradicts", "Contradicts"], ["ephemeral", "Ephemeral"],
    ["graph", "Graph"], ["manager", "Manager"], ["system", "System"],
    ["statistics", "Statistics"], ["config", "Config"], ["mail", "Mail"],
  ],
  init() {
    const nav = document.getElementById("tabs");
    nav.innerHTML = "";
    for (const [key, label] of this.tabs) {
      const b = document.createElement("button");
      b.textContent = label; b.dataset.tab = key;
      b.innerHTML = label + `<span class="badge" id="b-${key}" style="display:none"></span>`;
      b.onclick = () => this.show(key);
      nav.appendChild(b);
    }
    document.getElementById("modal").onclick = (e) => {
      if (e.target.id === "modal") this.closeModal();
    };
    this.show("overview");
    this.updateAuthBtn();
    this.refresh();
    clearInterval(this.statusTimer);
    this.statusTimer = setInterval(() => this.refresh(true), 30000);
  },
  show(key) {
    this.tab = key;
    document.querySelectorAll("#tabs button").forEach(
      b => b.classList.toggle("active", b.dataset.tab === key));
    const mod = window.Tabs[key];
    const panel = document.getElementById("panel");
    if (mod && mod.render) mod.render(panel);
    else panel.innerHTML = `<div class="card">Tab '${this.esc(key)}' not loaded.</div>`;
  },
  setBadge(key, n) {
    const el = document.getElementById("b-" + key);
    if (!el) return;
    if (n > 0) { el.textContent = n > 99 ? "99+" : n; el.style.display = ""; }
    else el.style.display = "none";
  },
  async refresh(quiet) {
    const st = document.getElementById("status");
    this.updateAuthBtn();
    try {
      const data = await this.api("/api/status?db=all");
      const h = data.health || {}, bl = data.bloat || {};
      const core = h.core || {}, agents = h.agents || {};
      const bc = bl.core || {}, ba = bl.agents || {};
      const live = (d) => ((d.check || {}).ok === true ? "on" : "off");
      const pc = document.getElementById("pill-core"), pa = document.getElementById("pill-agents");
      pc.innerHTML = `<span class="dot ${live(core)}"></span>core.db`;
      pc.title = `${core.total_nodes || 0} notes, ${core.total_edges || 0} links`;
      pa.innerHTML = `<span class="dot ${live(agents)}"></span>agents.db`;
      pa.title = `${agents.total_nodes || 0} notes, ${agents.total_edges || 0} links`;
      const run = data.scheduler && data.scheduler.running;
      document.getElementById("sched-dot").className = "dot " + (run ? "on" : "off");
      document.getElementById("sched-txt").textContent =
        `Scheduler ${run ? "on" : "off"}`;
      const n = (d, k) => d[k] || 0;
      const cn = n(core, "total_nodes"), an = n(agents, "total_nodes");
      const ce = n(core, "total_edges"), ae = n(agents, "total_edges");
      const cp = n(bc, "ephemeral_events"), ap = n(ba, "ephemeral_events");
      const cc = n(bc, "contradicts_total"), ac = n(ba, "contradicts_total");
      // fetch candidate count for statsbar (non-blocking, fallback to ?)
      let candTotal = "?", candTip = "new labels not in any list";
      try {
        const candData = await this.api("/api/ephemeral_candidates?db=all&min_count=3");
        candTotal = (candData.candidates || []).length;
        candTip = `${candTotal} new labels asking for decision (not in Ephemeral nor Ignored)`;
      } catch(e) {}
      document.getElementById("statsbar").innerHTML =
        `Nodes <b>${cn + an}</b> (${cn} / ${an}) | ` +
        `Edges <b>${ce + ae}</b> (${ce} / ${ae}) | ` +
        `Ephemeral <b title="rows whose label is in Ephemeral list (will be cleaned)">${cp + ap}</b> (${cp} / ${ap}) | ` +
        `<span title="${candTip}">Candidates <b>${candTotal}</b></span> | ` +
        `Contradicts <b>${cc + ac}</b> (${cc} / ${ac}) | ` +
        `Freelist <b>${bc.freelist_pct ?? "?"}% / ${ba.freelist_pct ?? "?"}%</b> | ` +
        `Size <b>${core.db_size_mb || 0} / ${agents.db_size_mb || 0} MB</b>`;
      st.textContent = "System ready — " + new Date().toLocaleTimeString();
      // badges (quiet background refresh, failures ignored)
      this.asyncBadges();
    } catch (e) {
      if (!quiet) { st.textContent = "Error: " + e.message; this.toast(e.message); }
    }
  },
  async asyncBadges() {
    try {
      const g = await this.api("/api/graduate_preview?limit=1");
      this.setBadge("graduate", g.graduable || 0);
    } catch (e) {}
    try {
      const [cc, ac] = await Promise.all([
        this.api("/api/contradictions?db=core&status=pending&limit=1"),
        this.api("/api/contradictions?db=agents&status=pending&limit=1"),
      ]);
      const n = (x) => (x.counts && x.counts.pending) || (x.contradictions || []).length;
      this.setBadge("contradicts", n(cc) + n(ac));
    } catch (e) {}
    try {
      const m = await this.api("/api/mailbox?scope=user&state=pending&limit=100");
      this.setBadge("mail", (m.messages || []).length);
    } catch (e) {}
  },
};
window.App = App;
document.addEventListener("DOMContentLoaded", () => App.init());

