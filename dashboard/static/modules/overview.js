// Overview tab — full status of both databases + rough shape. Details live in Statistics.
"use strict";
window.Tabs.overview = {
  rough(dist, total) {
    const rows = Object.entries(dist || {}).sort((a, b) => b[1] - a[1]).slice(0, 5);
    if (!rows.length || !total) return `<span class="muted">empty</span>`;
    const col = {PERSON:"#7ee787",FACT:"#d2a8ff",EVENT:"#ffab70",TOPIC:"#79c0ff",PREFERENCE:"#ff7eb6",BOUNDARY:"#ffa198",AFFECT:"#ff7eb6",AGENT_NOTE:"#e3b341",CORE_REF:"#79c0ff",SKILL:"#7ee787"};
    return rows.map(([k, v]) => `<span class="tag" style="background:${(col[k]||"#21262d")}22;color:${col[k]||"#c9d1d9"};border:1px solid ${(col[k]||"#30363d")}55">${App.esc(k)} <b>${v}</b></span>`).join(" ");
  },
  card(name, d, b) {
    d = d || {}; b = b || {};
    const ok = (d.check || {}).ok;
    const tag = ok === true ? `<span class="tag green">healthy</span>`
      : ok === false ? `<span class="tag red">check failed</span>`
      : `<span class="tag grey">unknown</span>`;
    const row = (k, v, color) => `<tr><td><span class="muted">${k}</span></td><td><b style="color:${color || "#e6edf3"}">${v}</b></td></tr>`;
    const wasteCol = (b.needs_vacuum ? "#ffa198" : "#7ee787");
    return `<div class="card" style="border-left:3px solid ${name.includes("core") ? "#79c0ff" : "#e3b341"}"><h3 style="margin:0 0 8px">${name} ${tag}</h3>
      <table class="plain">
      ${row("Notes", d.total_nodes || 0, "#79c0ff")}
      ${row("Links", d.total_edges || 0, "#d2a8ff")}
      ${row("Size", (d.db_size_mb || 0) + " MB", "#c9d1d9")}
      ${row("Wasted", (b.freelist_pct ?? "?") + "%" + ((b.needs_vacuum ? ` <span class="tag red">vacuum</span>` : ` <span class="tag green">ok</span>`)), wasteCol)}
      ${row("Ephemeral log", b.ephemeral_events ?? "?", "#ffab70")}
      ${row("Clashes", b.contradicts_total ?? "?", "#e3b341")}
      ${row("Backups", d.snapshots || 0, "#8b949e")}
      </table>
      <div style="margin-top:10px;padding-top:8px;border-top:1px solid #21262d"><span class="muted">Roughly</span><br>${this.rough(d.node_types, d.total_nodes)}</div></div>`;
  },
  async render(el) {
    el.innerHTML = `<div class="card">Loading overview…</div>`;
    try {
      const data = await App.api("/api/status?db=all");
      const h = data.health || {}, b = data.bloat || {};
      const hist = (data.history || []).slice(-5).reverse().map(e => {
        const st = (e.status || "") === "success" ? `<span class="tag green">ok</span>`
          : `<span class="tag amber">${App.esc(e.status || "?")}</span>`;
        const targetTag = e.target === "core" ? "blue" : e.target === "agents" ? "amber" : "grey";
        return `<tr><td>${App.esc(e.timestamp || "")}</td><td><span class="tag grey">${App.esc((e.jobs || []).join(", "))}</span></td>` +
        `<td><span class="tag ${targetTag}">${App.esc(e.target || "")}</span></td><td>${App.esc(e.duration_s || "")}s</td><td>${st}</td></tr>`;
      }).join("");
      el.innerHTML = `<div class="grid2">${this.card("core.db — shared knowledge", h.core, b.core)}${this.card("agents.db — agent notes", h.agents, b.agents)}</div>
        <div class="card"><h3>Recent runs <span class="muted">— last 5</span></h3><table><tr><th>Time</th><th>Jobs</th><th>Target</th><th>Took</th><th>Result</th></tr>${hist || "<tr><td colspan=5>No runs yet</td></tr>"}</table></div>`;
    } catch (e) { el.innerHTML = `<div class="card">Error: ${App.esc(e.message)}</div>`; }
  },
};
