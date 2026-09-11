// Maintenance tab — the 9 brain jobs, each explained, results split per database.
"use strict";
window.Tabs.maintenance = {
  db: "all",
  jobs: [
    ["dedup", "Merge duplicates", "Finds notes saying the same thing and merges them.", "#e3b341"],
    ["compact", "Clear telemetry", "Deletes expired ephemeral logs.", "#79c0ff"],
    ["agent_working", "Tidy scratch", "Demotes stale agent notes.", "#7ee787"],
    ["core_helper", "Core Helper", "Regulates core working memory (→ short/long).", "#5bc0de"],
    ["tiers", "Decay layers", "Decays short/long-term importance & prunes stale.", "#d2a8ff"],
    ["age_prune", "Prune unused", "Removes stale unread notes.", "#ffa198"],
    ["contradictions", "Find disagreements", "Scans core + agents for clashes.", "#e3b341"],
    ["graduation", "Graduate", "Manual — use Graduate tab.", "#79c0ff"],
    ["discover", "Discover links", "Wires related notes together.", "#7ee787"],
    ["prune_empty_agents", "Prune ghosts", "Deletes agents with 0 notes (respects min-age).", "#ff7b72"],
    ["vacuum", "Vacuum", "Reclaims disk space.", "#8b949e"],
  ],
  async render(el) {
    let free = "";
    try {
      const b = await App.api("/api/bloat?db=all");
      const pill = (name, d) => {
        if (!d) return "";
        const col = d.needs_vacuum ? "#ffa198" : "#7ee787";
        const tag = d.needs_vacuum ? "vacuum suggested" : "fine";
        return `<span class="pill" style="border-color:${col};color:${col}">${name} waste <b>${d.freelist_pct}%</b> · ${tag}</span>`;
      };
      free = `<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0">${pill("core.db", b.core)} ${pill("agents.db", b.agents)} <span class="muted">threshold ${((b.core || {}).vacuum_threshold_pct ?? "?")}% — <span style="color:#79c0ff;cursor:pointer" onclick="App.show('config')">Config → Vacuum</span></span></div>`;
    } catch (e) {}
    const cards = this.jobs.map(([j, label, help, col]) =>
      `<div style="border-left:3px solid ${col};background:#0d1117;border:1px solid #21262d;border-left-color:${col};border-radius:8px;padding:10px 12px;display:flex;flex-direction:column;gap:6px">
        <div style="display:flex;align-items:center;gap:8px"><span class="tag" style="background:${col}22;color:${col};border-color:${col}88">${j}</span><b style="font-size:13px">${App.esc(label)}</b></div>
        <span class="fdesc" style="margin:0">${App.esc(help)}</span>
        <button class="btn" style="align-self:flex-start;margin-top:4px;background:${col};color:#0d1117;border-color:${col};font-weight:700" onclick="Tabs.maintenance.run(['${j}'])">Run ${App.esc(label)}</button>
      </div>`).join("");
    el.innerHTML = `<div class="card">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><h3 style="margin:0">Maintenance — brain jobs</h3>
      <span class="tag blue">canonical order</span><span class="muted">graduation & vacuum excluded from FULL</span></div>
      <span class="fdesc">Compact = <b>compact</b> job (telemetry); Vacuum = <b>vacuum</b> job (disk). Jobs run in canonical order.</span>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:8px 0">
        <label>Target <select id="m-db"><option value="all">both databases</option><option value="core">core.db</option><option value="agents">agents.db</option></select></label>
        <span class="muted">→</span>
        <button class="btn" onclick="Tabs.maintenance.run(null)" style="background:#1f6feb">Run FULL (defaults)</button>
        <button class="btn ghost" onclick="Tabs.maintenance.snapshot()">Snapshot Now</button>
      </div>
      ${free}
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px;margin-bottom:12px">${cards}</div>
    <div class="card"><h4 style="margin:0 0 8px">Last run</h4><div id="m-out" class="muted" style="white-space:pre-wrap;background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:10px;min-height:60px">Idle — pick a job or Run FULL.</div></div>`;
    document.getElementById("m-db").value = this.db;
    document.getElementById("m-db").onchange = (e) => { this.db = e.target.value; };
  },
  fmtVal(v) {
    if (v && typeof v === "object") {
      const parts = [];
      for (const [db, d] of Object.entries(v)) {
        if (d && typeof d === "object" && "status" in d) {
          const st = d.status === "success" ? `<span class="tag green">ok</span>`
            : d.status === "skipped" ? `<span class="tag grey">skipped</span>`
            : `<span class="tag red">${App.esc(d.status)}</span>`;
          const extra = Object.entries(d).filter(([k, x]) =>
            !["status"].includes(k) && typeof x !== "object")
            .map(([k, x]) => `<span class="muted">${App.esc(k)}</span> <b>${App.esc(x)}</b>`).join(" · ");
          parts.push(`<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><span class="tag ${db === "core" ? "blue" : "amber"}">${App.esc(db)}.db</span> ${st} <span class="muted">${extra}</span></div>`);
        }
      }
      if (parts.length) return parts.join("");
    }
    return App.esc(JSON.stringify(v));
  },
  async run(jobs) {
    const out = document.getElementById("m-out");
    out.textContent = "Running…";
    out.style.color = "#8b949e";
    try {
      const target = document.getElementById("m-db").value;
      const data = await App.api("/api/run_job", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(jobs ? {jobs, target} : {target})});
      const run = data.run || data;
      const blocks = Object.entries(run.results || {}).map(([job, v]) =>
        `<div class="sec" style="border-left-color:#1f6feb"><h4>${App.esc(job)}</h4>${this.fmtVal(v)}</div>`).join("");
      const h = run.health_after ? `<div class="sec" style="border-left-color:#7ee787"><h4>Health afterwards</h4><span class="muted">${App.esc(JSON.stringify(run.health_after.combined || run.health_after))}</span></div>` : "";
      out.innerHTML = `<div style="margin-bottom:8px"><span class="tag blue">${App.esc((run.jobs || []).join(", "))}</span> → <span class="tag grey">${App.esc(run.target || "")}</span> <span class="muted">in ${App.esc(run.duration_s || "?")}s</span></div>${blocks}${h}`;
      App.toast("Run finished"); App.refresh(true);
    } catch (e) { out.textContent = "Error: " + e.message; out.style.color = "#ffa198"; }
  },
  async snapshot() {
    try {
      const target = document.getElementById("m-db").value;
      const data = await App.api("/api/create_snapshot", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: target})});
      App.toast("Snapshot: " + JSON.stringify(data)); App.refresh(true);
    } catch (e) { App.toast(e.message); }
  },
};
