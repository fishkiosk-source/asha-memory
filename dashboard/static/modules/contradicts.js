// Contradicts tab — curation queue per database, Manager-grade polish.
"use strict";
window.Tabs.contradicts = {
  db: "core",
  status: "pending",
  sel: {},
  actions: [
    ["confirmed", "Yes, real clash", "Keep watching this pair.", "#79c0ff"],
    ["ignored", "Not a clash", "Dismiss — notes are fine.", "#8b949e"],
    ["delete", "Unlink only", "Remove link, keep both.", "#d2a8ff"],
    ["keep_from", "Keep left", "Left survives, right DELETED.", "#ffa198"],
    ["keep_to", "Keep right", "Right survives, left DELETED.", "#ffa198"],
    ["merge", "Fuse", "Combine into left, delete right.", "#e3b341"],
  ],
  actionLabel(a) {
    const f = this.actions.find(x => x[0] === a);
    return f ? f[1] : a;
  },
  actionColor(a) {
    const f = this.actions.find(x => x[0] === a);
    return f ? f[3] : "#30363d";
  },
  async render(el) {
    el.innerHTML = `<div class="card">Loading contradictions…</div>`;
    try {
      const [d, other] = await Promise.all([
        App.api(`/api/contradictions?db=${this.db}&status=${this.status}&limit=50`),
        App.api(`/api/contradictions?db=${this.db === "core" ? "agents" : "core"}&status=pending&limit=1`),
      ]);
      const counts = d.counts || {};
      const otherN = (other.counts && other.counts.pending) || 0;
      const isCore = this.db === "core";
      const accent = isCore ? "#79c0ff" : "#e3b341";
      const here = isCore ? "core.db — shared knowledge" : "agents.db — one agent at a time";
      const agentCol = !isCore;
      const list = d.contradictions || [];
      const trustTag = (v) => {
        if (v == null) return `<span class="muted">—</span>`;
        const col = v >= 0.8 ? "#7ee787" : v <= 0.3 ? "#ffa198" : "#c9d1d9";
        return `<span style="color:${col};font-weight:700">${v.toFixed(2)}</span>`;
      };
      const overlapTags = (words) => (words || []).map(w=>`<span class="tag" style="background:#3b2300;color:#e3b341;border-color:#5a3d0a">${App.esc(w)}</span>`).join(" ") || `<span class="muted">—</span>`;
      const rows = list.map((c, i) => {
        const sug = c.suggested_action;
        const sugTag = sug ? `<span class="tag" style="background:${this.actionColor(sug)}22;color:${this.actionColor(sug)};border-color:${this.actionColor(sug)}55">${App.esc(this.actionLabel(sug))}</span>` : `<span class="tag grey">you decide</span>`;
        const conf = c.metadata && c.metadata.confidence != null ? `<span class="muted">${(c.metadata.confidence*100).toFixed(0)}%</span>` : "";
        return `<tr style="${this.sel[c.edge_id] ? "background:#1c2b45" : ""}">
          <td><input type="checkbox" data-eid="${App.esc(c.edge_id)}" ${this.sel[c.edge_id] ? "checked" : ""}></td>
          <td><div style="display:flex;gap:6px;align-items:center"><span class="type-badge type-${App.esc(c.from_type)}">${App.esc(c.from_type || "?")}</span> <b>${App.esc(c.from_label || "")}</b></div><div class="muted" style="font-size:11px">${App.esc((c.from_content || "").slice(0, 130))}</div><div style="margin-top:2px">trust ${trustTag(c.from_trust)}</div></td>
          <td><div style="display:flex;gap:6px;align-items:center"><span class="type-badge type-${App.esc(c.to_type)}">${App.esc(c.to_type || "?")}</span> <b>${App.esc(c.to_label || "")}</b></div><div class="muted" style="font-size:11px">${App.esc((c.to_content || "").slice(0, 130))}</div><div style="margin-top:2px">trust ${trustTag(c.to_trust)}</div></td>
          ${agentCol ? `<td><span class="tag" style="background:#3b2e12;color:#ffce4d">${App.esc(c.agent_id || "")}</span></td>` : ""}
          <td>${overlapTags((c.metadata || {}).overlap_words)} <div style="margin-top:2px">${conf}</div></td>
          <td>${sugTag}</td>
          <td style="white-space:nowrap"><div style="display:flex;gap:4px;flex-wrap:wrap">
            <button class="btn ghost" style="padding:3px 8px;font-size:11px" onclick='Tabs.contradicts.compare(${i})'>Compare</button>
            <button class="btn" style="padding:3px 8px;font-size:11px;background:#0f2a4d;color:#79c0ff;border:1px solid #1a3d6b" onclick='Tabs.contradicts.act("${c.edge_id}","confirmed")'>Confirm</button>
            <button class="btn danger" style="padding:3px 8px;font-size:11px" onclick='Tabs.contradicts.act("${c.edge_id}","delete")'>Unlink</button>
          </div></td></tr>`;
      }).join("");
      this._list = list;
      const legend = this.actions.map(([k, label, help, col]) =>
        `<div style="display:flex;gap:8px;align-items:center"><span class="tag" style="background:${col}22;color:${col};border-color:${col}55">${App.esc(label)}</span><span class="muted">${App.esc(help)}</span></div>`).join("");
      el.innerHTML = `<div class="card" style="border-left:3px solid ${accent}">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><h3 style="margin:0">Disagreements — ${here}</h3>
          <span class="tag ${isCore ? "blue" : "amber"}">${this.db}.db</span>
          <span class="muted" style="margin-left:auto">badge = both DBs</span></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0">
          <span class="tag ${counts.pending ? "amber" : "grey"}"><b>${counts.pending || 0}</b> waiting here</span>
          <span class="tag grey">waiting in ${isCore ? "agents.db" : "core.db"} <b>${otherN}</b></span>
          <span class="tag green"><b>${counts.confirmed || 0}</b> confirmed</span>
          <span class="tag grey"><b>${counts.ignored || 0}</b> ignored</span>
          <span class="tag blue"><b>${counts.resolved || 0}</b> resolved</span>
        </div>
        <span class="fdesc" style="margin:0">${isCore ? "Core clashes confuse every agent — curate here first." : "Agent clashes confuse that agent. Pairs never mix two agents."}</span>
      </div>
      <div class="card">
        <div style="display:grid;grid-template-columns:1fr auto;gap:12px;align-items:end">
          <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:end">
            <label>Database<br><select id="c-db"><option value="core">core.db</option><option value="agents">agents.db</option></select></label>
            <label>Show<br><select id="c-status">${["pending","confirmed","ignored","all"].map(s=>`<option ${s===this.status?"selected":""}>${s}</option>`).join("")}</select></label>
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end">
            <button class="btn ghost" onclick="Tabs.contradicts.rescan()">Re-scan now</button>
            <button class="btn ghost" onclick="Tabs.contradicts.clearAll()">Clear all</button>
            <button class="btn ghost" onclick="Tabs.contradicts.auto(true)">Preview auto-resolve</button>
            <button class="btn warn" onclick="Tabs.contradicts.auto(false)">Auto-resolve low-trust</button>
          </div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px;padding:8px 10px;background:#0d1117;border:1px solid #21262d;border-radius:8px">
          <label>With selected:<br><select id="c-bulk" style="min-width:160px">${this.actions.map(([k,label])=>`<option value="${k}">${App.esc(label)}</option>`).join("")}</select></label>
          <button class="btn" onclick="Tabs.contradicts.bulk()">Apply to selected (<span id="c-n">0</span>)</button>
          <label style="display:flex;align-items:center;gap:6px;margin-left:8px"><input type="checkbox" id="c-all"> <span class="muted">select all</span></label>
          <span class="muted" style="margin-left:auto">${list.length} pairs</span>
        </div>
        <div style="overflow:auto;margin-top:10px"><table><tr><th></th><th>Says this</th><th>Says that</th>${agentCol?"<th>Agent</th>":""}<th>Overlap</th><th>Suggestion</th><th>Decide</th></tr>
        ${rows || `<tr><td colspan=${agentCol?7:6} style="text-align:center;padding:20px" class="muted">Queue empty — nothing disagrees.</td></tr>`}</table></div>
        <div class="sec" style="margin-top:12px"><h4>What do the buttons mean?</h4><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:6px">${legend}</div></div>
      </div>`;
      document.getElementById("c-db").value = this.db;
      document.getElementById("c-db").onchange = (e) => {
        this.db = e.target.value; this.sel = {}; this.render(document.getElementById("panel"));
      };
      document.getElementById("c-status").onchange = (e) => {
        this.status = e.target.value; this.render(document.getElementById("panel"));
      };
      document.getElementById("c-all").onchange = (e) => {
        const all = e.target.checked;
        document.querySelectorAll("#panel input[data-eid]").forEach(cb => {
          const id = cb.getAttribute("data-eid");
          if (all) this.sel[id] = 1; else delete this.sel[id];
          cb.checked = all;
          cb.closest("tr").style.background = all ? "#1c2b45" : "";
        });
        this.count();
      };
      document.querySelectorAll("#panel input[data-eid]").forEach(cb => {
        cb.onchange = () => {
          const id = cb.getAttribute("data-eid");
          if (cb.checked) this.sel[id] = 1; else delete this.sel[id];
          cb.closest("tr").style.background = cb.checked ? "#1c2b45" : "";
          this.count();
        };
      });
      this.count();
    } catch (e) { el.innerHTML = `<div class="card">Error: ${App.esc(e.message)}</div>`; }
  },
  count() {
    const n = document.getElementById("c-n");
    if (n) n.textContent = Object.keys(this.sel).length;
  },
  toggle(id) { if (this.sel[id]) delete this.sel[id]; else this.sel[id] = 1; this.count(); },
  compare(i) {
    const c = (this._list || [])[i];
    if (!c) return;
    const trustTag = (v, label) => `<span style="color:${v>=0.8?"#7ee787":v<=0.3?"#ffa198":"#c9d1d9"}">${label} trust <b>${v}</b></span>`;
    App.modal(`<h3>Side by side — ${App.esc(c.from_label)} ↔ ${App.esc(c.to_label)}</h3>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;align-items:stretch"><div class="card" style="border-left:3px solid #79c0ff;margin:0;display:flex;flex-direction:column"><h4 style="margin:0 0 6px">${App.esc(c.from_label || "")} <span class="type-badge type-${App.esc(c.from_type)}">${App.esc(c.from_type||"")}</span></h4>
      <div class="muted" style="margin-bottom:6px">${trustTag(c.from_trust,"Left")}</div>
      <pre style="white-space:pre-wrap;word-break:break-word;overflow-wrap:break-word;background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:8px;flex:1;min-height:140px;max-height:300px;overflow:auto;margin:0">${App.esc(c.from_content || "")}</pre></div>
      <div class="card" style="border-left:3px solid #e3b341;margin:0;display:flex;flex-direction:column"><h4 style="margin:0 0 6px">${App.esc(c.to_label || "")} <span class="type-badge type-${App.esc(c.to_type)}">${App.esc(c.to_type||"")}</span></h4>
      <div class="muted" style="margin-bottom:6px">${trustTag(c.to_trust,"Right")}</div>
      <pre style="white-space:pre-wrap;word-break:break-word;overflow-wrap:break-word;background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:8px;flex:1;min-height:140px;max-height:300px;overflow:auto;margin:0">${App.esc(c.to_content || "")}</pre></div></div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin:8px 0">${((c.metadata||{}).overlap_words||[]).map(w=>`<span class="tag" style="background:#3b2300;color:#e3b341">${App.esc(w)}</span>`).join(" ")} <span class="tag grey">confidence ${(c.metadata||{}).confidence!=null?((c.metadata.confidence*100).toFixed(0)+"%"):"—"}</span></div>
      <div style="display:flex;gap:6px"><button class="btn" style="background:#0f2a4d;color:#79c0ff" onclick="Tabs.contradicts.act('${c.edge_id}','confirmed');App.closeModal()">Confirm clash</button>
      <button class="btn ghost" onclick="Tabs.contradicts.act('${c.edge_id}','ignored');App.closeModal()">Not a clash</button>
      <button class="btn ghost" onclick="App.closeModal()">Close</button></div>`);
    // widen modal for side-by-side compare (default 640 is too narrow and grid2 would stack)
    setTimeout(()=>{
      const m=document.querySelector("#modal>div");
      if(m){m.style.maxWidth="900px";m.style.width="92%";}
    },10);
    const origClose=App.closeModal;
    const restore=()=>{
      const m=document.querySelector("#modal>div");
      if(m){m.style.maxWidth="";m.style.width="";}
      document.removeEventListener("click", restore);
    };
    setTimeout(()=>{document.getElementById("modal").addEventListener("click", (e)=>{if(e.target.id==="modal") restore();});},20);
  },
  async act(edge_id, action, quiet) {
    try {
      const r = await App.api("/api/contradiction_action", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({edge_id, action, db: this.db})});
      if (!quiet) {
        App.toast(JSON.stringify(r).slice(0, 200));
        this.render(document.getElementById("panel")); App.refresh(true);
      }
      return r;
    } catch (e) { App.toast(e.message); }
  },
  async bulk() {
    const sel = document.getElementById("c-bulk");
    const action = sel ? sel.value : "confirmed";
    const ids = Object.keys(this.sel);
    if (!ids.length) { App.toast("Nothing selected"); return; }
    if ((action === "keep_from" || action === "keep_to" || action === "merge") &&
        !confirm(`${this.actionLabel(action)} for ${ids.length} pair(s)? Notes WILL be deleted.`)) return;
    for (const id of ids) await this.act(id, action, true);
    this.sel = {};
    App.toast(`Applied to ${ids.length} pair(s)`);
    this.render(document.getElementById("panel")); App.refresh(true);
  },
  async rescan() {
    try {
      App.toast("Scanning…");
      const r = await App.api("/api/run_job", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({jobs: ["contradictions"], target: this.db})});
      App.toast(`Re-scan done — ${JSON.stringify(r.run.results.detect_contradictions[this.db] || r.run.results.detect_contradictions || "").slice(0,200)}`);
      this.render(document.getElementById("panel")); App.refresh(true);
    } catch (e) { App.toast(e.message); }
  },
  async clearAll() {
    if (!confirm(`Clear all contradictions in ${this.db}.db?`)) return;
    try {
      const r = await App.api("/api/contradictions/clear", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({db: this.db})});
      App.toast(`Cleared ${r[this.db]?.deleted ?? 0} in ${this.db}.db`);
      this.render(document.getElementById("panel")); App.refresh(true);
    } catch (e) { App.toast(e.message); }
  },
  async auto(dry) {
    try {
      const r = await App.api("/api/contradiction_auto_resolve", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({dry_run: dry, db: this.db})});
      App.modal(`<h3>Auto-resolve ${dry ? "(dry-run)" : ""} — ${App.esc(this.db)}.db</h3><pre style="white-space:pre-wrap;background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:10px;max-height:400px;overflow:auto">${App.esc(JSON.stringify(r, null, 1).slice(0, 4000))}</pre><button class="btn" onclick="App.closeModal()">Close</button>`);
      if (!dry) { this.render(document.getElementById("panel")); App.refresh(true); }
    } catch (e) { App.toast(e.message); }
  },
};
