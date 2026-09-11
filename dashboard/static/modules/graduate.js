// Graduate tab — agents.db curation (promote to core + demote back to private).
"use strict";
window.Tabs.graduate = {
  sel: {},
  async render(el) {
    el.innerHTML = `<div class="card">Loading graduate queue…</div>`;
    try {
      const d = await App.api("/api/graduate_preview?limit=100");
      const rows = (d.graduable_preview || d.notes || []).map(n => `<tr style="${this.sel[n.node_id] ? "background:#1a2a3a" : ""}">
        <td><input type="checkbox" data-nid="${App.esc(n.node_id)}"
          ${this.sel[n.node_id] ? "checked" : ""}></td>
        <td>${App.esc(n.label || "")}</td><td>${App.esc((n.content || "").slice(0, 120))}</td>
        <td><span class="att-badge att-${App.esc(n.attention || "review_ready")}">${App.esc(n.attention || "")}</span><div class="muted" style="font-size:10px">${n.attention==="review_ready"?"review-ready":"high-trust"}</div></td><td>${n.trust_level}</td><td>${n.importance}</td>
        <td style="white-space:nowrap"><button class="btn" style="padding:2px 6px;font-size:11px" onclick="Tabs.graduate.one('${App.esc(n.node_id)}')">Graduate</button>
        <button class="btn ghost" style="padding:2px 6px;font-size:11px" onclick="Tabs.graduate.demoteOne('${App.esc(n.node_id)}')">Demote</button></td></tr>`).join("");
      const selCount = Object.keys(this.sel).length;
      const emptyMsg = "<tr><td colspan=7>Queue empty — no review-ready notes. Mark notes as <code>review_ready</code> to make them graduable.</td></tr>";
      el.innerHTML = `<div class="card"><h3>Graduate — review queue (agents.db)</h3>
        <div class="muted">Total ${d.total_agent_notes} | Review-ready ${d.review_ready} | Graduable ${d.graduable} | Private ${d.agent_private} · <span class="muted">graduable = review_ready only (importance ignored)</span> — promote to core.db or demote</div>
        <div style="margin:6px 0;display:flex;gap:6px;flex-wrap:wrap;align-items:center">
          <button class="btn" id="grad-sel-btn" onclick="Tabs.graduate.selected()">Graduate Selected (${selCount})</button>
          <button class="btn ghost" id="demote-sel-btn" onclick="Tabs.graduate.demoteSelected()">Demote Selected (${selCount})</button>
          <button class="btn warn" onclick="Tabs.graduate.all()">Graduate All Graduable</button>
          <span class="muted" id="grad-sel-count" style="margin-left:auto">${selCount} selected</span></div>
        <table><tr><th></th><th>Label</th><th>Content</th><th>Attention</th><th>Trust</th><th>Imp</th><th>Actions</th></tr>
        ${rows || emptyMsg}</table></div>`;
      // wire checkboxes highlight
      el.querySelectorAll("input[data-nid]").forEach(cb => {
        cb.onchange = (e) => {
          const id = cb.getAttribute("data-nid");
          if (cb.checked) this.sel[id] = 1; else delete this.sel[id];
          cb.closest("tr").style.background = cb.checked ? "#1a2a3a" : "";
          this.updateBulkCount();
        };
      });
    } catch (e) { el.innerHTML = `<div class="card">Error: ${App.esc(e.message)}</div>`; }
  },
  updateBulkCount() {
    const n = Object.keys(this.sel).length;
    const g = document.getElementById("grad-sel-btn");
    const d = document.getElementById("demote-sel-btn");
    const c = document.getElementById("grad-sel-count");
    if (g) g.textContent = `Graduate Selected (${n})`;
    if (d) d.textContent = `Demote Selected (${n})`;
    if (c) c.textContent = `${n} selected`;
  },
  toggle(nid) {
    if (this.sel[nid]) delete this.sel[nid]; else this.sel[nid] = 1;
    this.updateBulkCount();
    // also update row highlight if already rendered
    const cb = document.querySelector(`input[data-nid="${CSS.escape(nid)}"]`);
    if (cb && cb.closest("tr")) cb.closest("tr").style.background = this.sel[nid] ? "#1a2a3a" : "";
  },
  async one(nid) { await this.send([nid]); },
  async demoteOne(nid) { await this.sendDemote([nid]); },
  async selected() { await this.send(Object.keys(this.sel)); },
  async demoteSelected() { await this.sendDemote(Object.keys(this.sel)); },
  async all() {
    const d = await App.api("/api/graduate_preview?limit=500");
    await this.send((d.graduable_preview || []).map(n => n.node_id));
  },
  async send(ids) {
    if (!ids.length) { App.toast("Nothing selected"); return; }
    try {
      const r = await App.api("/api/graduate", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({node_ids: ids})});
      App.toast("Graduated: " + JSON.stringify(r.core || r));
      this.sel = {}; this.render(document.getElementById("panel")); App.refresh(true);
    } catch (e) { App.toast(e.message); }
  },
  async sendDemote(ids) {
    if (!ids.length) { App.toast("Nothing selected"); return; }
    try {
      const r = await App.api("/api/demote", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({node_ids: ids})});
      App.toast(`Demoted ${r.demoted}/${r.total} back to private`);
      this.sel = {}; this.render(document.getElementById("panel")); App.refresh(true);
    } catch (e) { App.toast(e.message); }
  },
};
