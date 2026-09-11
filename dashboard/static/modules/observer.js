// Observer tab — agents.db WORKING janitor preview + regulate, color-coded.
"use strict";
window.Tabs.observer = {
  actionTag(a) {
    if (a === "demote_next") return `<span class="tag red">demote next</span>`;
    if (a === "stale_soon") return `<span class="tag amber">going stale</span>`;
    if (a === "keep") return `<span class="tag green">keep</span>`;
    if ((a || "").startsWith("protected")) return `<span class="tag blue">protected</span>`;
    return `<span class="tag grey">${App.esc(a || "?")}</span>`;
  },
  async render(el) {
    el.innerHTML = `<div class="card">Loading observer…</div>`;
    try {
      const d = await App.api("/api/agent_working_preview");
      const w = d.weights || {};
      const rows = (d.preview || []).map(p => `<tr><td>${App.esc(p.label || "")}</td>
        <td>${App.esc(p.agent_id || "")}</td><td><b>${p.score}</b></td><td>${p.age_hours}h</td>
        <td>${p.days_left}d</td><td>${this.actionTag(p.action)}</td></tr>`).join("");
      el.innerHTML = `<div class="card"><h3>Observer — agent scratch space (agents.db only, core untouched)</h3>
        <div style="font-size:14px;margin-bottom:6px"><b>${d.agent_working_count}</b> working notes · high-water mark <b>${d.high_water}</b> · regulator <b>${d.enabled ? "on" : "off"}</b></div>
        <span class="fdesc">Score = reads × ${w.wa ?? "?"} + importance × ${w.wi ?? "?"} − age × ${w.wd ?? "?"} — low score demotes first. Review-ready notes are protected.</span>
        <div style="margin:6px 0"><button class="btn" onclick="Tabs.observer.regulate(true)">Preview (dry-run)</button>
        <button class="btn warn" onclick="Tabs.observer.regulate(false)">Regulate Now</button></div>
        <pre id="o-out" class="muted"></pre>
        <table><tr><th>Note</th><th>Agent</th><th>Score</th><th>Age</th><th>Left</th><th>Fate</th></tr>
        ${rows || "<tr><td colspan=6>Working space empty — nothing to tidy.</td></tr>"}</table></div>`;
    } catch (e) { el.innerHTML = `<div class="card">Error: ${App.esc(e.message)}</div>`; }
  },
  async regulate(dry) {
    try {
      const r = await App.api("/api/regulate_agent_working", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({dry_run: dry})});
      document.getElementById("o-out").textContent = JSON.stringify(r, null, 1).slice(0, 2000);
      App.refresh(true);
    } catch (e) { App.toast(e.message); }
  },
};
