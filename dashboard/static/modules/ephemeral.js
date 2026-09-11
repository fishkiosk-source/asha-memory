// Ephemeral tab — telemetry per database + ephemeral list + ignored list + compact.
"use strict";
window.Tabs.ephemeral = {
  db: "all",
  statCard(name, st, filterSet) {
    const all = st || {labels:{}, total:0};
    const labels = Object.entries(all.labels || {});
    const filtered = filterSet ? labels.filter(([lab]) => filterSet.has(lab)) : labels;
    const sorted = filtered.sort((a, b) => b[1].count - a[1].count);
    const rows = sorted.slice(0, 10).map(([lab, info]) =>
      `<tr><td>${App.esc(lab)}</td><td><b>${info.count}</b></td></tr>`).join("");
    const totalFiltered = filtered.reduce((s, [,info]) => s + (info.count||0), 0);
    const title = filterSet ? "rows in list" : "rows total";
    return `<div class="card"><h3>${name}</h3>
      <div style="font-size:15px;margin-bottom:6px"><b>${totalFiltered}</b> ${title} <span class="muted" style="font-size:11px">/ ${all.total||0} total</span></div>
      <table class="plain"><tr><th>Label</th><th>Rows</th></tr>
      ${rows || "<tr><td colspan=2>Empty.</td></tr>"}</table></div>`;
  },
  async render(el) {
    el.innerHTML = `<div class="card">Loading ephemeral…</div>`;
    try {
      const [stats, c] = await Promise.all([
        App.api("/api/ephemeral_stats?db=all"),
        App.api(`/api/ephemeral_candidates?db=${this.db}&min_count=3`),
      ]);
      const ephemeral = (c.ephemeral_labels || c.allowlist || []).map(l =>
        `<span class="pill" style="background:#3b2300;border-color:#9e6a03">${App.esc(l)} <a href="#" onclick="Tabs.ephemeral.removeEphemeral('${App.esc(l)}');return false">×</a></span>`).join(" ");
      const ignored = (c.ignored_labels || []).map(l =>
        `<span class="pill" style="background:#12361f;border-color:#1f4a2a">${App.esc(l)} <a href="#" onclick="Tabs.ephemeral.removeIgnored('${App.esc(l)}');return false">×</a></span>`).join(" ");
      const ephSet = new Set(c.ephemeral_labels || c.allowlist || []);
      const ignSet = new Set(c.ignored_labels || []);
      const cands = (c.candidates || []).map(x => {
        const first = x.oldest ? App.when(x.oldest) : "—";
        const last = x.newest ? App.when(x.newest) : "—";
        const span = x.span_days ? `${x.span_days}d (${x.span_hours}h)` : "—";
        const rate = x.per_day ? `${x.per_day}/day` : "—";
        const reason = `count ${x.count} ≥3, not in any list, span ${span}, rate ${rate}`;
        const samples = (x.samples || []).map(s => `<div class="muted" style="font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px" title="${App.esc(s)}">${App.esc(s.slice(0,80))}</div>`).join("") || "<span class='muted'>—</span>";
        return `<tr><td><b>${App.esc(x.label)}</b><div class="muted" style="font-size:11px">candidate: ${reason}</div></td><td><b>${x.count}</b><div class="muted" style="font-size:11px">${rate}</div></td><td>${App.esc(x.db)}</td><td style="font-size:11px">${first}<div class="muted">→ ${last}</div><div class="muted">span ${span}</div></td><td>${samples}</td><td style="white-space:nowrap"><button class="btn warn" style="padding:3px 8px;font-size:11px" onclick="Tabs.ephemeral.addEphemeral('${App.esc(x.label)}')" title="Add to Ephemeral list → will be cleaned on Compact">Ephemeral (target)</button> <button class="btn ghost" style="padding:3px 8px;font-size:11px" onclick="Tabs.ephemeral.addIgnored('${App.esc(x.label)}')" title="Add to Allowed/Ignored → scanner skips it forever (not cleaned)">Keep → ignore</button></td></tr>`;
      }).join("");
      el.innerHTML = `<div class="grid2">${this.statCard("core.db telemetry", stats.core, ephSet)}${this.statCard("agents.db telemetry", stats.agents, ephSet)}</div>
        <div class="card"><h3>Ephemeral labels — one shared list, both databases</h3>
        <span class="fdesc">Labels here are confirmed <b>ephemeral</b>. Compact <b>only</b> targets these (per-DB TTL + <code>keep_last</code>); they are cleaned on every scan. Click <b>×</b> to unmark — label goes back to “New labels” candidates.</span>
        <div style="margin:6px 0">${ephemeral || `<span class="muted">Empty — no ephemeral labels. Add candidates below.</span>`}</div>
        <div style="margin:6px 0"><input id="e-new-ephemeral" placeholder="LABEL (e.g. FEED_SNAPSHOT)">
        <button class="btn warn" onclick="Tabs.ephemeral.addNewEphemeral()">Add to ephemeral</button> <span class="muted" style="font-size:11px">→ will be cleaned on next Compact</span></div></div>
        <div class="card"><h3>Allowed / Ignored labels — scanner skips these</h3>
        <span class="fdesc">Labels here are <b>ignored</b> by the ephemeral scanner. They will <b>never</b> appear as candidates and are <b>never</b> cleaned by Compact. Use for labels that look like telemetry but are actually wanted. Click <b>×</b> to unmark — label goes back to candidates.</span>
        <div style="margin:6px 0">${ignored || `<span class="muted">Empty — no ignored labels.</span>`}</div>
        <div style="margin:6px 0"><input id="e-new-ignored" placeholder="LABEL (e.g. MY_KEEP_LABEL)">
        <button class="btn ghost" onclick="Tabs.ephemeral.addNewIgnored()">Add to ignored</button> <span class="muted" style="font-size:11px">→ scanner will skip it</span></div></div>
        <div class="card"><h3>New labels asking for a decision</h3>
        <span class="fdesc">Unknown telemetry (not in either list, count ≥3). <b>Ephemeral (target)</b> adds to Ephemeral list above — will be cleaned. <b>Keep → ignore</b> adds to Allowed/Ignored — scanner skips it. Both remove from this table.</span>
        <label>Show <select id="e-db"><option value="all">both databases</option>
        <option value="core">core.db</option><option value="agents">agents.db</option></select> <span class="muted" style="font-size:11px">min_count 3</span></label>
        <button class="btn ghost" style="margin-left:8px;padding:3px 10px;font-size:11px" onclick="Tabs.ephemeral.render(document.getElementById('panel'))" title="Fast re-scan — re-reads ephemeral_events for new candidates (no DB write)">↻ Re-scan</button> <span class="muted" style="font-size:11px">fast check for new candidates</span>
        <table style="margin-top:6px; font-size:12px"><tr><th>Label <div class="muted" style="font-weight:normal">why candidate?</div></th><th>Rows <div class="muted" style="font-weight:normal">rate</div></th><th>Lives in</th><th>First → Last <div class="muted" style="font-weight:normal">span</div></th><th>Sample bodies</th><th>Decision</th></tr>
        ${cands || "<tr><td colspan=6>None — everything is classified. New labels will appear here when count ≥ 3 and not in either list.</td></tr>"}</table>
         <div style="margin-top:8px"><button class="btn ghost" style="padding:3px 10px;font-size:11px" onclick="Tabs.ephemeral.render(document.getElementById('panel'))">↻ Re-scan</button> <button class="btn warn" onclick="Tabs.ephemeral.compact()">Compact Now (only ephemeral list)</button> <span class="muted" style="font-size:11px">only deletes rows whose label is in the Ephemeral list above</span></div>
         <div class="card" style="border-left:3px solid #ff7eb6"><h4>Bring DB up to date — v2 leftovers as nodes</h4>
           <span class="fdesc">Migrated <code>v2 DBs</code> left telemetry as <b>nodes</b> (e.g. <code>RUNTIME_SAMPLE</code> as <code>AGENT_NOTE</code> in Manager). This scans <code>nodes</code> where <code>label IN Ephemeral list</code> and purges them with the same <code>TTL (hours)</code> + <code>keep_last</code> as <code>Compact</code> (1h/2). Use for one-off <code>v2→v3</code> cleanup — future writes should go to <code>ephemeral_events</code>.</span>
           <div style="margin:8px 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
             <label>DB <select id="e-sync-db"><option value="agents">agents.db</option><option value="core">core.db</option><option value="both">both</option></select></label>
             <button class="btn ghost" onclick="Tabs.ephemeral.syncNodes(true)">Preview sync (dry-run)</button>
             <button class="btn danger" onclick="Tabs.ephemeral.syncNodes(false)">Bring DB up to date</button>
             <span class="muted" style="font-size:11px">purges matching <b>nodes</b> → Manager list shrinks to keep_last</span>
           </div>
           <pre id="e-sync-out" class="muted" style="white-space:pre-wrap;background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:8px;min-height:40px">Idle — preview first.</pre>
         </div></div>`;
      document.getElementById("e-db").value = this.db;
      document.getElementById("e-db").onchange = (e) => {
        this.db = e.target.value; this.render(document.getElementById("panel"));
      };
    } catch (e) { el.innerHTML = `<div class="card">Error: ${App.esc(e.message)}</div>`; }
  },
  async add(label) { return this.addEphemeral(label); },
  async remove(label) { return this.removeEphemeral(label); },
  async addEphemeral(label) { await this.sendEphemeral({action: "add", label}); },
  async removeEphemeral(label) { await this.sendEphemeral({action: "remove", label}); },
  async addIgnored(label) { await this.sendIgnored({action: "add", label}); },
  async removeIgnored(label) { await this.sendIgnored({action: "remove", label}); },
  async addNewEphemeral() {
    const v = document.getElementById("e-new-ephemeral").value.trim();
    if (v) await this.sendEphemeral({action: "add", label: v});
  },
  async addNewIgnored() {
    const v = document.getElementById("e-new-ignored").value.trim();
    if (v) await this.sendIgnored({action: "add", label: v});
  },
  async addNew() {
    const v = document.getElementById("e-new-ephemeral").value.trim() || document.getElementById("e-new-ignored")?.value.trim();
    if (v) await this.sendEphemeral({action: "add", label: v});
  },
  async send(payload) { return this.sendEphemeral(payload); },
  async sendEphemeral(payload) {
    try {
      await App.api("/api/ephemeral_allowlist", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
      this.render(document.getElementById("panel"));
    } catch (e) { App.toast(e.message); }
  },
  async sendIgnored(payload) {
    try {
      await App.api("/api/ephemeral_ignored", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
      this.render(document.getElementById("panel"));
    } catch (e) { App.toast(e.message); }
  },
  async compact() {
    try {
      const r = await App.api("/api/compact_ephemeral", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: this.db})});
      App.toast("Compacted: " + JSON.stringify(r)); App.refresh(true);
    } catch (e) { App.toast(e.message); }
  },
  async syncNodes(dry) {
    const sel = document.getElementById("e-sync-db");
    const target = sel ? sel.value : this.db;
    const out = document.getElementById("e-sync-out");
    if (out) out.textContent = dry ? "Previewing…" : "Syncing…";
    try {
      const r = await App.api("/api/ephemeral_sync_nodes", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: target, dry_run: dry})});
      if (out) out.textContent = JSON.stringify(r, null, 1).slice(0, 4000);
      App.toast(dry ? "Preview done" : "Sync done — Manager should now show ≤ keep_last per label");
      App.refresh(true);
      if (!dry) this.render(document.getElementById("panel"));
    } catch (e) {
      if (out) out.textContent = "Error: " + e.message;
      App.toast(e.message);
    }
  },
};
