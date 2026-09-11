// Core Helper tab — core.db WORKING regulator preview + regulate, mirror of Observer.
"use strict";
window.Tabs.core_helper = {
  actionTag(a) {
    if (a === "to_long_term") return `<span class="tag blue">→ long-term</span>`;
    if (a === "to_short_term") return `<span class="tag amber">→ short-term</span>`;
    if ((a || "").startsWith("keep")) return `<span class="tag green">keep</span>`;
    return `<span class="tag grey">${App.esc(a || "?")}</span>`;
  },
  async render(el) {
    el.innerHTML = `<div class="card">Loading Core Helper…</div>`;
    try {
      const d = await App.api("/api/core_helper_preview");
      const rows = (d.preview || []).map(p => `<tr>
        <td>${App.esc(p.label || "")}</td>
        <td><span class="tag grey">${App.esc(p.node_type || "")}</span></td>
        <td>${p.trust_level ?? "?"}</td><td>${p.importance ?? "?"}</td><td>${p.access_count}</td>
        <td>${p.age_hours}h</td><td>${App.esc(p.target_layer)}</td><td>${this.actionTag(p.action)}</td></tr>`).join("");
      el.innerHTML = `<div class="card"><h3>Core Helper — working memory (core.db only, other layers untouched)</h3>
        <div style="font-size:14px;margin-bottom:6px"><b>${d.core_working_count}</b> working notes · age gate <b>${d.min_age_hours}h</b> · regulator <b>${d.enabled ? "on" : "off"}</b></div>
        <span class="fdesc">Rules: age ≥ ${d.min_age_hours}h AND imp≥${d.imp_threshold} trust≥${d.trust_high} → keep hot; trust≤${d.trust_low} → short-term; trusted but not important → short-term; imp≥${d.imp_threshold} + ${d.trust_low}&lt;trust&lt;${d.trust_high} → acc≥${d.access_threshold} long-term else short-term. Archive is manual-only (Core sets it).</span>
        <div style="font-size:12px;margin:6px 0" class="muted">Moves: <b>${(d.move_counts||{}).to_short_term||0}</b> → short-term · <b>${(d.move_counts||{}).to_long_term||0}</b> → long-term · <b>${(d.move_counts||{}).keep||0}</b> keep</div>
        <div style="margin:6px 0"><button class="btn" onclick="Tabs.core_helper.regulate(true)">Preview (dry-run)</button>
        <button class="btn warn" onclick="Tabs.core_helper.regulate(false)">Regulate Now</button>
        <button class="btn ghost" onclick="Tabs.core_helper.render(document.getElementById('panel'))">Refresh</button></div>
        <pre id="ch-out" class="muted"></pre>
        <table><tr><th>Note</th><th>Type</th><th>Trust</th><th>Imp</th><th>Reads</th><th>Age</th><th>Target</th><th>Fate</th></tr>
        ${rows || "<tr><td colspan=8>Working layer empty — nothing to tidy.</td></tr>"}</table></div>`;
    } catch (e) { el.innerHTML = `<div class="card">Error: ${App.esc(e.message)}</div>`; }
  },
  async regulate(dry) {
    try {
      const r = await App.api("/api/regulate_core_helper", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({dry_run: dry})});
      document.getElementById("ch-out").textContent = JSON.stringify(r, null, 1).slice(0, 2500);
      App.refresh(true);
      if (!dry) this.render(document.getElementById("panel"));
    } catch (e) { App.toast(e.message); }
  },
};
