// dashboard/static/modules/manager.js — v3 Manager, better than v2.
// Server-backed, paged, 8 sub-panels: Nodes/Edges/Vectors/Layers/Path/Schema/Stats/SQL
// No sql.js, no file download — live commits with incremental vectors.
"use strict";
window.Tabs.manager = {
  db: "core",
  view: "nodes",
  // nodes view state
  q: "", type: "", scope: "", attention: "", layer: "", source: "", agent: "",
  sort: "created_at", dir: "desc",
  offset: 0, limit: 25,
  sel: {},
  agents: [],
  // cache
  _nodes: [],
  _total: 0,
  NTYPES: ["PERSON","TOPIC","EVENT","FACT","PREFERENCE","BOUNDARY","AFFECT","AGENT_NOTE","CORE_REF","SKILL"],
  ETYPES: ["RELATES_TO","CONTRADICTS","SUPPORTS","CAUSED_BY","PART_OF","TRUSTS","DISTRUSTS","REMEMBERS","HAS_PREFERENCE","HAS_BOUNDARY","HAS_AFFECT","HAS_SKILL","REFERS_TO","SUMMARIZES","PROMOTED_FROM"],
  LAYERS: ["working","short_term","long_term","archive"],
  ATTN: ["agent_private","review_ready","core_verified"],

  async render(el) {
    // inject v2-like badge styles once
    if (!document.getElementById("mgr-style")) {
      const s = document.createElement("style");
      s.id = "mgr-style";
      s.textContent = `.type-badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:10px;font-weight:600;border:1px solid #30363d}
.type-PERSON{background:#1a2a3a;color:#7ee787;border-color:#1f4a2a}.type-TOPIC{background:#1a2a4d;color:#79c0ff;border-color:#1a3d6b}
.type-EVENT{background:#3b2300;color:#ffab70;border-color:#5a3d0a}.type-FACT{background:#2a1a3a;color:#d2a8ff;border-color:#3d2a5a}
.type-PREFERENCE{background:#12361f;color:#ffab70;border-color:#5a3d0a}.type-BOUNDARY{background:#3d1113;color:#ffa198;border-color:#5a1a1a}
.type-AFFECT{background:#3d1130;color:#ff7eb6;border-color:#5a1a3a}.type-AGENT_NOTE{background:#3b2e12;color:#ffce4d;border-color:#5a3d0a}
.type-CORE_REF{background:#0f2a4d;color:#79c0ff;border-color:#1a3d6b}.type-SKILL{background:#12361f;color:#7ee787;border-color:#1f4a2a}
.scope-badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:10px;font-weight:700;border:1px solid #30363d}
.scope-CORE{background:#0f2a4d;color:#79c0ff;border-color:#1a3d6b}.scope-AGENT{background:#3b2300;color:#ffce4d;border-color:#5a3d0a}
.att-badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:10px;font-weight:600;border:1px solid #30363d}
.att-agent_private{background:#21262d;color:#8b949e}.att-review_ready{background:#3b2e12;color:#ffce4d;border-color:#5a3d0a}
.att-core_verified{background:#12361f;color:#7ee787;border-color:#1f4a2a}
.layer-badge{display:inline-block;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:700;letter-spacing:.02em;border:1px solid transparent}
.layer-working{background:#3b2e12;color:#ffb86c;border-color:#5a3d0a;box-shadow:0 0 8px rgba(255,172,106,.15)}
.layer-short_term{background:#0f2a4d;color:#79c0ff;border-color:#1a3d6b;box-shadow:0 0 8px rgba(121,192,255,.15)}
.layer-long_term{background:#12361f;color:#7ee787;border-color:#1f4a2a;box-shadow:0 0 8px rgba(126,231,135,.15)}
.layer-archive{background:#2a1a3a;color:#d2a8ff;border-color:#3d2a5a;box-shadow:0 0 8px rgba(210,168,255,.15)}
.mgr-tabs{display:flex;gap:0;background:#111;border-bottom:1px solid #2a2a2a;margin:8px -12px 0;padding:0 12px;overflow-x:auto}
.mgr-tab{padding:8px 14px;font-size:12px;color:#777;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap}
.mgr-tab:hover{color:#ccc}.mgr-tab.active{color:#e0e0e0;border-bottom-color:#6af}
.mgr-tab .badge{background:#2a2a2a;color:#999;font-size:10px;padding:1px 6px;border-radius:8px;margin-left:4px}
.mgr-tab.active .badge{background:#1a3a5a;color:#8bf}
.bulk-bar{display:none;padding:6px 12px;background:#14202c;border-bottom:1px solid #1e2e40;font-size:12px;color:#9af;align-items:center;gap:8px;flex-wrap:wrap}
.conn-row{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid #222;font-size:12px}
`;
      document.head.appendChild(s);
    }
    el.innerHTML = `<div class="card" style="padding:0;overflow:hidden">
      <div style="padding:12px 12px 0;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <h3 style="margin:0">Manager — ${this.db === "core" ? "core.db (shared knowledge)" : "agents.db (all agents)"}</h3>
        <label>DB <select id="mgr-db"><option value="core">core.db</option><option value="agents">agents.db</option></select></label>
        <span id="mgr-health" class="muted" style="margin-left:auto"></span>
      </div>
      <div id="mgr-stats" style="display:flex;gap:16px;flex-wrap:wrap;padding:8px 12px;font-size:12px;color:#999;border-bottom:1px solid #2a2a2a"></div>
      <div class="mgr-tabs" id="mgr-tabs"></div>
      <div id="mgr-panel" style="padding:12px;min-height:300px"></div>
    </div>`;
    document.getElementById("mgr-db").value = this.db;
    document.getElementById("mgr-db").onchange = (e) => { this.db = e.target.value; this.offset = 0; this.sel = {}; this.render(el); };
    // ensure valid view — clicking away and back must not leave blank
    if (!["nodes","edges","vectors","layers","path","schema","stats","sql"].includes(this.view)) this.view = "nodes";
    this.renderTabs();
    // agents list is not needed to show the table — fetch in background
    App.api("/api/agents").then(ag => {
      this.agents = (ag.agents || []).map(a => a.agent_id);
    }).catch(() => { this.agents = []; });
    // show panel immediately — don't wait for health
    const panel = document.getElementById("mgr-panel");
    if (panel) panel.innerHTML = `<div class="muted">Loading ${App.esc(this.view)}…</div>`;
    try {
      await this.loadHealth();
    } catch (e) {
      const h = document.getElementById("mgr-health");
      if (h) h.textContent = "health error: " + e.message;
    }
    try {
      await this.loadView();
    } catch (e) {
      const p = document.getElementById("mgr-panel");
      if (p) p.innerHTML = `<div style="color:#f85149;padding:12px">Manager error: ${App.esc(e.message)}<br><pre style="white-space:pre-wrap;font-size:11px">${App.esc(e.stack || "")}</pre></div>`;
      console.error("Manager loadView", e);
    }
  },

  renderTabs() {
    const tabs = [
      ["nodes", "Nodes"], ["edges", "Links"], ["vectors", "Vectors"],
      ["layers", "Layers"], ["path", "Path"], ["schema", "Schema"],
      ["stats", "Stats"], ["sql", "SQL"]
    ];
    const c = document.getElementById("mgr-tabs");
    c.innerHTML = tabs.map(([k, label]) => `<div class="mgr-tab ${this.view === k ? "active" : ""}" data-v="${k}">${label}</div>`).join("");
    c.querySelectorAll(".mgr-tab").forEach(t => t.onclick = () => { this.view = t.dataset.v; this.offset = 0; this.renderTabs(); this.loadView(); });
  },

   async loadHealth() {
     try {
       const h = await App.api(`/api/manager_health?db=${this.db}`);
       const el = document.getElementById("mgr-health");
       const isActive = (k) => this._healthFilter === k ? "background:#1a3a5a;color:#8bf;border:1px solid #2a5a8a;border-radius:8px;padding:1px 6px;" : "";
        const pill = (label, n, color, key) => {
          if (n===undefined || n===null) return "";
          const active = this._healthFilter===key;
          const style = active ? "background:#1a3a5a;color:#8bf;border:1px solid #2a5a8a;border-radius:8px;padding:1px 6px;cursor:pointer" : `color:${color};cursor:pointer;border:1px solid transparent;padding:1px 6px`;
          return `<span style="${style}" onclick="Tabs.manager.jumpHealth('${key}')" title="${active ? 'click to clear filter — shows ${label.toLowerCase()} list' : 'click to filter'}">${label} ${n}</span>`;
        };
        const parts = [
          `${h.total_nodes} notes, ${h.total_edges} links`,
          pill("Orphans", h.isolated_count, "#e55", "orphans"),
          pill("Dup labels", h.dupes_count, "#ea6", "dup"),
          pill("Isolated", h.isolated_count, "#ea6", "isolated"),
          pill("Ephemeral", h.ephemeral_events, "#79c0ff", "ephemeral"),
        ].filter(Boolean).join(" · ");
       el.innerHTML = parts;
       const st = document.getElementById("mgr-stats");
       let hint = "click Orphans/Dup/Isolated/Ephemeral to filter — click again to clear";
       if (this._healthFilter) hint = `filtered by <b>${App.esc(this._healthFilter)}</b> — click again to clear · <a href="#" onclick="Tabs.manager.clearHealthFilter();return false" style="color:#79c0ff">clear</a>`;
       st.innerHTML = `<span>${h.total_nodes} notes</span><span>${h.total_edges} links</span><span class="muted">${hint}</span>`;
     } catch (e) {}
   },

   clearHealthFilter(){
     this._healthFilter=null; this._dupFilter=false; this._isolatedFilter=false; this._ephemeralFilter=false;
     this.offset=0; this.loadHealth(); this.loadNodes();
   },

   jumpHealth(kind) {
     const k = (kind||"").toLowerCase();
     // toggle: clicking active filter clears it
     if (this._healthFilter === k) {
       this.clearHealthFilter();
       return;
     }
     // clear previous
     this._healthFilter = k;
     this._dupFilter = k==="dup";
     this._isolatedFilter = k==="isolated";
     this._ephemeralFilter = k==="ephemeral";
      if (k === "orphans") { this.view = "nodes"; this._dupFilter=false; this._isolatedFilter=true; this._ephemeralFilter=false; this.offset=0; this.renderTabs(); this.loadNodes(); this.loadHealth(); return; }
     else if (k === "dup") { this.view = "nodes"; this.q = ""; this.type = ""; this.scope = ""; this.attention = ""; this.layer = ""; this.source = ""; this.agent = ""; this.offset=0; this.renderTabs(); this.loadNodes(); }
     else if (k === "isolated") { this.view = "nodes"; this.offset=0; this.renderTabs(); this.loadNodes(); }
     else if (k === "ephemeral") { this.view = "nodes"; this.offset=0; this.renderTabs(); this.loadNodes(); }
     this.loadHealth();
   },

  async loadView() {
    const p = document.getElementById("mgr-panel");
    if (!p) return;
    if (this.view === "nodes") return this.loadNodes();
    if (this.view === "edges") return this.loadEdges();
    if (this.view === "vectors") return this.loadVectors();
    if (this.view === "layers") return this.loadLayers();
    if (this.view === "path") return this.loadPath();
    if (this.view === "schema") return this.loadSchema();
    if (this.view === "stats") return this.loadStats();
    if (this.view === "sql") return this.loadSql();
  },

  // ── Nodes ──
  async loadNodes() {
    const p = document.getElementById("mgr-panel");
    p.innerHTML = `<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input id="mgr-q" placeholder="find label/content/id…" value="${App.esc(this.q)}" style="width:170px">
      <select id="mgr-type"><option value="">all kinds</option>${this.NTYPES.map(t => `<option ${this.type === t ? "selected" : ""}>${t}</option>`).join("")}</select>
      <select id="mgr-scope"><option value="">all scopes</option><option value="CORE" ${this.scope === "CORE" ? "selected" : ""}>CORE</option><option value="AGENT" ${this.scope === "AGENT" ? "selected" : ""}>AGENT</option></select>
      <select id="mgr-att"><option value="">all attention</option>${this.ATTN.map(a => `<option ${this.attention === a ? "selected" : ""}>${a}</option>`).join("")}<option value="__none" ${this.attention === "__none" ? "selected" : ""}>(none)</option></select>
      <select id="mgr-layer"><option value="">all layers</option>${this.LAYERS.map(l => `<option ${this.layer === l ? "selected" : ""}>${l}</option>`).join("")}</select>
      <select id="mgr-source"><option value="">all sources</option></select>
      <select id="mgr-agent"><option value="">all agents</option>${this.agents.map(a => `<option ${this.agent === a ? "selected" : ""}>${App.esc(a)}</option>`).join("")}</select>
      <button class="btn" onclick="Tabs.manager.applyNodesFilter()">Filter</button>
      <button class="btn ghost" onclick="Tabs.manager.clearNodesFilter()">Clear</button>
      <button class="btn" style="margin-left:auto" onclick="Tabs.manager.newNode()">+ New note</button>
      <span id="mgr-ninfo" class="muted"></span></div>
      <div id="mgr-bulk" class="bulk-bar"><span id="mgr-bulk-n">0 selected</span>
        <button class="btn danger" style="padding:2px 8px;font-size:11px" onclick="Tabs.manager.bulkDelete()">Delete</button>
        <select id="mgr-bulk-att" style="background:#1e1e1e;border:1px solid #333;color:#ccc;padding:3px 6px;font-size:11px"><option value="">Set attention…</option>${this.ATTN.map(a => `<option value="${a}">${a}</option>`).join("")}<option value="__clear">(remove)</option></select>
        <select id="mgr-bulk-layer" style="background:#1e1e1e;border:1px solid #333;color:#ccc;padding:3px 6px;font-size:11px"><option value="">Set layer…</option>${this.LAYERS.map(l => `<option value="${l}">${l}</option>`).join("")}</select>
        <button class="btn" style="padding:2px 8px;font-size:11px" onclick="Tabs.manager.bulkRelabel()">Relabel…</button>
        <button class="btn ghost" style="padding:2px 8px;font-size:11px" onclick="Tabs.manager.clearSel()">Clear</button>
      </div>
      <div style="overflow:auto"><table><tr>
        <th style="width:28px"><input type="checkbox" id="mgr-all"></th>
        <th class="sortable" data-k="node_id" style="cursor:pointer">ID</th><th>Scope</th><th class="sortable" data-k="node_type" style="cursor:pointer">Kind</th><th class="sortable" data-k="label" style="cursor:pointer">Note</th><th>Layer</th><th>Attention</th><th>Agent</th><th class="sortable" data-k="trust_level" style="cursor:pointer">Trust</th><th class="sortable" data-k="importance" style="cursor:pointer">Imp</th><th class="sortable" data-k="access_count" style="cursor:pointer">Reads</th><th class="sortable" data-k="created_at" style="cursor:pointer">Date</th><th class="sortable" data-k="updated_at" style="cursor:pointer">Updated</th><th></th></tr>
        <tbody id="mgr-rows"><tr><td colspan="14" class="muted">Loading…</td></tr></tbody></table></div>
      <div id="mgr-pager" style="margin-top:8px"></div>`;
    // wire filter inputs
    const qEl = document.getElementById("mgr-q");
    qEl.onkeydown = (e) => { if (e.key === "Enter") this.applyNodesFilter(); };
    // fetch distinct sources for dropdown
    try {
      const s = await App.api("/api/sql", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: this.db, sql: "SELECT DISTINCT source FROM nodes ORDER BY source"})});
      const sel = document.getElementById("mgr-source");
      (s.rows || []).forEach(r => {
        const v = r[0] || "";
        if (v) { const o = document.createElement("option"); o.value = v; o.textContent = v; if (v === this.source) o.selected = true; sel.appendChild(o); }
      });
    } catch (e) {}
    // wire sortable headers
    p.querySelectorAll("th.sortable").forEach(th => th.onclick = () => {
      const k = th.dataset.k;
      if (this.sort === k) this.dir = this.dir === "asc" ? "desc" : "asc";
      else { this.sort = k; this.dir = k === "label" ? "asc" : "desc"; }
      this.offset = 0; this.loadNodesData();
    });
    document.getElementById("mgr-bulk-att").onchange = (e) => { const v = e.target.value; e.target.value = ""; if (v) this.bulkSetAttention(v); };
    document.getElementById("mgr-bulk-layer").onchange = (e) => { const v = e.target.value; e.target.value = ""; if (v) this.bulkSetLayer(v); };
    await this.loadNodesData();
  },

  applyNodesFilter() {
    this.q = document.getElementById("mgr-q").value.trim();
    this.type = document.getElementById("mgr-type").value;
    this.scope = document.getElementById("mgr-scope").value;
    this.attention = document.getElementById("mgr-att").value;
    this.layer = document.getElementById("mgr-layer").value;
    this.source = document.getElementById("mgr-source").value;
    this.agent = document.getElementById("mgr-agent").value;
    this.offset = 0; this.loadNodesData();
  },
   clearNodesFilter() {
     this.q = ""; this.type = ""; this.scope = ""; this.attention = ""; this.layer = ""; this.source = ""; this.agent = "";
     this._dupFilter = false; this._isolatedFilter = false; this._ephemeralFilter=false; this._healthFilter=null;
     this.offset = 0; this.loadHealth(); this.render(document.getElementById("panel"));
   },

   async loadNodesData() {
     const info = document.getElementById("mgr-ninfo");
     if (info) info.textContent = "Loading…";
     try {
       // handle health-filtered views first (server-wide, not page-local)
        if (this._ephemeralFilter) {
          // ephemeral view: only rows whose label is in Ephemeral list (if list empty → 0, per user request)
          try {
            // fetch current Ephemeral list to filter — if empty, show empty (don't show all like MOLTBOOK_HEARTBEAT)
            let ephLabels = [];
            try {
              const cand = await App.api(`/api/ephemeral_candidates?db=${this.db}&min_count=3`);
              ephLabels = cand.ephemeral_labels || cand.allowlist || [];
            } catch(e) { ephLabels = []; }
            if (!ephLabels.length) {
              const body = document.getElementById("mgr-rows");
              const pager = document.getElementById("mgr-pager");
              if (info) info.textContent = `0 ephemeral events — Ephemeral list is empty (nothing targeted). Add labels in Ephemeral tab.`;
              if (body){
                const head = document.querySelector("#mgr-panel table tr");
                if(head) head.innerHTML = "<th>ID</th><th>Label</th><th>Body</th><th>When</th>";
              }
              const tbody = document.getElementById("mgr-rows");
              tbody.innerHTML = `<tr><td colspan=4 class="muted">No ephemeral events for current Ephemeral list. Add labels in Ephemeral tab → Compact will target them.</td></tr>`;
              if(pager) pager.innerHTML = `<span class="muted">Ephemeral filtered view — <a href="#" onclick="Tabs.manager.clearHealthFilter();return false" style="color:#79c0ff">clear</a></span>`;
              this._nodes=[]; this._total=0;
              return;
            }
            const inList = ephLabels.map(l => `'${l.replace(/'/g,"''")}'`).join(",");
            // unified ephemeral view: nodes (legacy) + ephemeral_events (new) where label in list
            const sqlNodes = `SELECT node_id as id, label, content as body, created_at FROM nodes WHERE label IN (${inList}) ORDER BY created_at DESC LIMIT 50`;
            const sqlEph = `SELECT id, label, body, created_at FROM ephemeral_events WHERE label IN (${inList}) ORDER BY created_at DESC LIMIT 50`;
            let rowsNodes=[], rowsEph=[];
            try { const dn = await App.api("/api/sql",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({db:this.db, sql: sqlNodes})}); rowsNodes = (dn.rows||[]).map(r=> ({id:r[0], label:r[1], body:r[2], ts:r[3], src:'nodes'})); } catch(e) {}
            try { const de = await App.api("/api/sql",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({db:this.db, sql: sqlEph})}); rowsEph = (de.rows||[]).map(r=> ({id:r[0], label:r[1], body:r[2], ts:r[3], src:'eph'})); } catch(e) {}
            const merged = [...rowsNodes, ...rowsEph].sort((a,b)=> (b.ts||0)-(a.ts||0)).slice(0,50);
           const body = document.getElementById("mgr-rows");
           const pager = document.getElementById("mgr-pager");
           if (info) info.textContent = `${merged.length} ephemeral rows (nodes ${rowsNodes.length} + events ${rowsEph.length}, filtered) — click health pill again to clear`;
           if (body){
             const head = document.querySelector("#mgr-panel table tr");
             if(head) head.innerHTML = "<th>ID</th><th>Label</th><th>Body</th><th>When</th><th>Src</th>";
           }
           const tbody = document.getElementById("mgr-rows");
           tbody.innerHTML = merged.map(r=>`<tr><td class="muted" style="font-family:monospace;font-size:11px">${App.esc(r.id)}</td><td><span class="tag" style="background:#0f2a4d;color:#79c0ff">${App.esc(r.label||"")}</span></td><td>${App.esc((r.body||"").slice(0,120))}</td><td class="muted">${App.esc(App.ago(r.ts))}</td><td class="muted" style="font-size:10px">${r.src}</td></tr>`).join("") || `<tr><td colspan=5 class="muted">No ephemeral rows for current Ephemeral list.</td></tr>`;
           if(pager) pager.innerHTML = `<span class="muted">Ephemeral filtered view (unified nodes+events) — <a href="#" onclick="Tabs.manager.clearHealthFilter();return false" style="color:#79c0ff">clear</a></span>`;
           this._nodes=[]; this._total=merged.length;
           return;
         } catch(e){ if(info) info.textContent = "Ephemeral error: "+e.message; return; }
       }
       if (this._dupFilter || this._isolatedFilter) {
         // fetch all nodes (up to 500) then filter by health set — covers whole DB, not just current page
         let urlAll = `/api/nodes?db=${this.db}&q=&type=&scope=&attention=&layer=&source=&agent=&sort=${this.sort}&dir=${this.dir}&offset=0&limit=500`;
         const dAll = await App.api(urlAll);
         let filtered = dAll.nodes||[];
         try {
           const h = await App.api(`/api/manager_health?db=${this.db}`);
           if (this._dupFilter){
             const dupLabels = new Set((h.dupes||[]).map(x=>x.label));
             filtered = filtered.filter(n=>dupLabels.has(n.label));
             if(info) info.textContent = `${filtered.length} dup-label notes (of ${dAll.total}) — click Dup again to clear`;
           } else if (this._isolatedFilter){
             const isoSet = new Set(h.isolated||[]);
             filtered = filtered.filter(n=>isoSet.has(n.node_id));
             if(info) info.textContent = `${filtered.length} isolated notes (of ${dAll.total}) — click Isolated again to clear`;
           }
         } catch(e){}
         // client-side paging on filtered set
         this._nodes = filtered.slice(this.offset, this.offset+this.limit);
         this._total = filtered.length;
         // need to still honour text filters? already fetched unfiltered, apply q/type etc manually if set? keep simple: if any text filter set, apply additionally
         if (this.q) { const ql=this.q.toLowerCase(); this._nodes=this._nodes.filter(n=> (n.label||"").toLowerCase().includes(ql)||(n.content||"").toLowerCase().includes(ql)); }
        } else {
          let url = `/api/nodes?db=${this.db}&q=${encodeURIComponent(this.q)}&type=${encodeURIComponent(this.type)}&scope=${encodeURIComponent(this.scope)}&attention=${encodeURIComponent(this.attention)}&layer=${encodeURIComponent(this.layer)}&source=${encodeURIComponent(this.source)}&agent=${encodeURIComponent(this.agent)}&sort=${this.sort}&dir=${this.dir}&offset=${this.offset}&limit=${this.limit}`;
          const d = await App.api(url);
          this._nodes = d.nodes || [];
          this._total = d.total;
          if (info) info.textContent = `${d.total} notes`;
        }
      const tbody = document.getElementById("mgr-rows");
      const agentCol = true; // always show for consistency
      tbody.innerHTML = this._nodes.map(n => {
        const c = App.esc((n.content || "").slice(0, 90));
        const att = n._attention || "";
        const scope = n._scope || "CORE";
        return `<tr class="${this.sel[n.node_id] ? "sel" : ""}" style="${this.sel[n.node_id] ? "background:#1a2a3a;" : ""}cursor:pointer" onclick="Tabs.manager.open('${App.esc(n.node_id)}')">
          <td onclick="event.stopPropagation()"><input type="checkbox" data-nid="${App.esc(n.node_id)}" ${this.sel[n.node_id] ? "checked" : ""}></td>
          <td style="font-family:monospace;font-size:11px;color:#777" title="${App.esc(n.node_id)}">${App.esc(n.node_id.slice(0, 12))}</td>
          <td><span class="scope-badge scope-${scope}">${scope}</span></td>
          <td><span class="type-badge type-${n.node_type}">${n.node_type}</span></td>
          <td><b>${App.esc(n.label || "")}</b><br><span class="muted" style="font-size:11px">${c}</span></td>
          <td><span class="layer-badge layer-${n.layer}">${n.layer}</span></td>
          <td>${att ? `<span class="att-badge att-${att}">${att}</span>` : `<span class="muted">—</span>`}</td>
          <td class="muted" style="font-size:11px">${App.esc(n._agent || "")}</td>
          <td>${n.trust_level != null ? n.trust_level.toFixed(2) : "—"}</td>
          <td>${n.importance != null ? n.importance.toFixed(2) : "—"}</td>
          <td>${n.access_count || 0}</td>
          <td class="muted" title="${App.esc(App.fdate(n.created_at))}">${App.esc(App.ago(n.created_at))}</td>
          <td class="muted" title="${App.esc(App.fdate(n.updated_at))}">${App.esc(App.ago(n.updated_at))}</td>
          <td style="white-space:nowrap" onclick="event.stopPropagation()"><button class="btn" style="padding:2px 6px;font-size:11px" onclick="Tabs.manager.edit('${App.esc(n.node_id)}')">Edit</button>
            <button class="btn danger" style="padding:2px 6px;font-size:11px" onclick="Tabs.manager.del('${App.esc(n.node_id)}')">Del</button></td></tr>`;
      }).join("") || `<tr><td colspan="14" class="muted">No notes.</td></tr>`;
      // pager
      const pager = document.getElementById("mgr-pager");
      const a = this._total ? this.offset + 1 : 0, b = Math.min(this.offset + this.limit, this._total);
      pager.innerHTML = `<button class="btn ghost" onclick="Tabs.manager.page(-1)" ${this.offset <= 0 ? "disabled" : ""}>Prev</button>
        <span class="muted">${a}–${b} of ${this._total}</span>
        <button class="btn ghost" onclick="Tabs.manager.page(1)" ${b >= this._total ? "disabled" : ""}>Next</button>`;
      // wire checkboxes
      tbody.querySelectorAll("input[data-nid]").forEach(cb => {
        cb.onchange = () => {
          const id = cb.getAttribute("data-nid");
          if (cb.checked) this.sel[id] = 1; else delete this.sel[id];
          cb.closest("tr").style.background = cb.checked ? "#1a2a3a" : "";
          this.updateBulk();
        };
      });
      const all = document.getElementById("mgr-all");
      if (all) { all.checked = this._nodes.length && this._nodes.every(n => this.sel[n.node_id]); all.onchange = () => {
        tbody.querySelectorAll("input[data-nid]").forEach(cb => {
          const id = cb.getAttribute("data-nid");
          if (all.checked) this.sel[id] = 1; else delete this.sel[id];
          cb.checked = all.checked; cb.closest("tr").style.background = all.checked ? "#1a2a3a" : "";
        }); this.updateBulk();
      }; }
      this.updateBulk();
    } catch (e) { if (info) info.textContent = "Error: " + e.message; }
  },

  page(d) { this.offset = Math.max(0, this.offset + d * this.limit); this.loadNodesData(); },
  updateBulk() {
    const n = Object.keys(this.sel).length;
    const bar = document.getElementById("mgr-bulk");
    if (bar) bar.style.display = n ? "flex" : "none";
    const cnt = document.getElementById("mgr-bulk-n");
    if (cnt) cnt.textContent = n + " selected";
  },
  clearSel() { this.sel = {}; this.loadNodesData(); },

  async bulkDelete() {
    const ids = Object.keys(this.sel);
    if (!ids.length) return;
    if (!confirm(`Delete ${ids.length} notes? Their links go with them.`)) return;
    const r = await App.api("/api/bulk_nodes", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: this.db, node_ids: ids, action: "delete"})});
    App.toast(`Deleted ${r.deleted}/${r.total}`); this.sel = {}; this.loadNodesData(); App.refresh(true); this.loadHealth();
  },
  async bulkSetAttention(val) {
    const ids = Object.keys(this.sel);
    if (!ids.length) { App.toast("Nothing selected"); return; }
    const r = await App.api("/api/bulk_nodes", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: this.db, node_ids: ids, action: "set_attention", value: val})});
    App.toast(`Updated ${r.updated}/${r.total}`); this.sel = {}; this.loadNodesData();
  },
  async bulkSetLayer(val) {
    const ids = Object.keys(this.sel);
    if (!ids.length) { App.toast("Nothing selected"); return; }
    const r = await App.api("/api/bulk_nodes", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: this.db, node_ids: ids, action: "set_layer", value: val})});
    App.toast(`Updated ${r.updated}/${r.total}`); this.sel = {}; this.loadNodesData();
  },
  bulkRelabel() {
    const ids = Object.keys(this.sel);
    if (!ids.length) { App.toast("Nothing selected"); return; }
    const v = prompt(`New label for ${ids.length} notes:`);
    if (!v || !v.trim()) return;
    App.api("/api/bulk_nodes", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: this.db, node_ids: ids, action: "relabel", value: v.trim()})}).then(r => {
      App.toast(`Updated ${r.updated}/${r.total}`); this.sel = {}; this.loadNodesData();
    }).catch(e => App.toast(e.message));
  },

  // ── Detail + Edit ──
  async open(id) {
    try {
      const d = await App.api(`/api/nodes?db=${this.db}&q=${encodeURIComponent(id)}&limit=1`);
      const n = (d.nodes || [])[0];
      if (!n) { App.toast("Not found"); return; }
      const e = await App.api(`/api/edges?db=${this.db}&node=${encodeURIComponent(id)}&limit=100`);
      const links = (e.edges || []).map(x => {
        const other = x.from_node === id ? x.to_node : x.from_node;
        const otherLabel = x.from_node === id ? x.to_label : x.from_label;
        const dir = x.from_node === id ? "→" : "←";
        return `<div class="conn-row"><div style="flex:1"><span class="muted">${App.esc(x.edge_type)} ${dir}</span> <b>${App.esc(otherLabel)}</b> <span class="muted">${App.esc(other.slice(0, 12))}</span> <span class="muted">w=${x.weight}</span></div>
          <button class="btn" style="padding:2px 6px;font-size:11px" onclick="Tabs.manager.open('${App.esc(other)}')">Open</button>
          <button class="btn danger" style="padding:2px 6px;font-size:11px" onclick="App.closeModal();Tabs.manager.delEdge('${App.esc(x.edge_id)}')">Del</button></div>`;
      }).join("");
      App.modal(`<h3>Manage note</h3>${App.nodeDetail(n)}
        <h4 style="margin:8px 0 4px">Layer</h4><div style="display:flex;gap:6px;flex-wrap:wrap">${this.LAYERS.map(l => `<button class="btn ${n.layer === l ? "" : "ghost"}" style="padding:2px 8px;font-size:11px" onclick="Tabs.manager.setLayer('${App.esc(id)}','${l}')">${l}</button>`).join("")}</div>
        <h4 style="margin:8px 0 4px">Links (${(e.edges || []).length})</h4>${links || `<span class="muted">No links — isolated.</span>`}
        <div style="margin-top:8px"><button class="btn" onclick="Tabs.manager.connectFrom('${App.esc(id)}')">+ Connect to another note</button></div>
        <div style="margin-top:8px;display:flex;gap:6px"><button class="btn" onclick="App.closeModal();Tabs.manager.edit('${App.esc(id)}')">Edit</button>
        <button class="btn danger" onclick="App.closeModal();Tabs.manager.del('${App.esc(id)}')">Delete</button>
        <button class="btn ghost" onclick="App.closeModal()">Close</button></div>`);
    } catch (e) { App.toast(e.message); }
  },

  setLayer(id, layer) {
    App.api("/api/bulk_nodes", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: this.db, node_ids: [id], action: "set_layer", value: layer})}).then(() => {
      App.toast(`Moved to ${layer}`); App.closeModal(); this.open(id);
    }).catch(e => App.toast(e.message));
  },

  connectFrom(id) {
    App.modal(`<h3>Connect note</h3><div class="muted">${App.esc(id.slice(0, 12))}…</div><br>
      <label>Other note (label or ID)<br><input id="cf-to" list="mgr-nodes-dl" style="width:100%" placeholder="search…"></label><datalist id="mgr-nodes-dl"></datalist><br><br>
      <label>Kind<br><select id="cf-type" style="width:100%">${this.ETYPES.map(t => `<option>${t}</option>`).join("")}</select></label><br><br>
      <label>Weight <input id="cf-w" type="number" step="0.1" min="-1" max="1" value="1.0" style="width:80px"></label>
      <label>Direction <select id="cf-dir"><option value="out">this → other</option><option value="in">other → this</option></select></label><br><br>
      <button class="btn" onclick="Tabs.manager.doConnect('${App.esc(id)}')">Connect</button>
      <button class="btn ghost" onclick="Tabs.manager.open('${App.esc(id)}')">Back</button>
      <pre id="cf-out" class="muted"></pre>`);
    const dl = document.getElementById("mgr-nodes-dl");
    const inp = document.getElementById("cf-to");
    let timer = null;
    inp.oninput = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = inp.value.trim();
        if (q.length < 2) return;
        try {
          const d = await App.api(`/api/nodes?db=${this.db}&q=${encodeURIComponent(q)}&limit=10`);
          dl.innerHTML = (d.nodes || []).map(n => `<option value="${App.esc(n.label)}">${App.esc(n.label)} (${n.node_id.slice(0, 8)})</option><option value="${App.esc(n.node_id)}"></option>`).join("");
        } catch (e) {}
      }, 300);
    };
  },

  async doConnect(id) {
    const raw = document.getElementById("cf-to").value.trim();
    const type = document.getElementById("cf-type").value;
    const w = parseFloat(document.getElementById("cf-w").value);
    const dir = document.getElementById("cf-dir").value;
    const out = document.getElementById("cf-out");
    if (!raw) { out.textContent = "Other note required"; return; }
    // resolve label or id via API
    let other = raw;
    try {
      const d = await App.api(`/api/nodes?db=${this.db}&q=${encodeURIComponent(raw)}&limit=5`);
      const m = (d.nodes || []).find(n => n.node_id === raw || n.label === raw);
      if (m) other = m.node_id;
      else if ((d.nodes || [])[0]) other = d.nodes[0].node_id;
    } catch (e) {}
    const fro = dir === "out" ? id : other;
    const to = dir === "out" ? other : id;
    try {
      const payload = {db: this.db, from_node: fro, to_node: to, edge_type: type, weight: w};
      if (this.db === "agents") {
        // need owner agent: infer from current node's agent
        const cur = await App.api(`/api/nodes?db=${this.db}&q=${encodeURIComponent(id)}&limit=1`);
        const ag = (cur.nodes || [])[0]?._agent || this.agents[0] || "";
        if (!ag) { out.textContent = "No owner agent found"; return; }
        payload.agent_id = ag;
      }
      const r = await App.api("/api/edge_add", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
      App.toast("Linked " + r.edge_id); this.open(id);
    } catch (e) { out.textContent = "Error: " + e.message; }
  },

  newNode() {
    const agentRow = this.db === "agents"
      ? `<label>Owner agent<br><select id="n-agent" style="width:100%">${this.agents.map(a => `<option>${App.esc(a)}</option>`).join("")}</select></label><br><br>` : "";
    App.modal(`<h3>New note — ${App.esc(this.db)}.db</h3>
      ${agentRow}
      <label>Kind<br><select id="n-type" style="width:100%">${this.NTYPES.map(t => `<option${t === "FACT" ? " selected" : ""}>${t}</option>`).join("")}</select></label><br><br>
      <label>Label<br><input id="n-label" style="width:100%" placeholder="short label"></label><br><br>
      <label>Content *<br><textarea id="n-content" style="min-height:100px"></textarea></label><br>
      <label>Source<br><input id="n-source" style="width:100%" value="CORE"></label><br><br>
      <label>Trust <input id="n-trust" type="number" step="0.1" min="0" max="1" value="0.5" style="width:80px"></label>
      <label>Importance <input id="n-imp" type="number" step="0.1" min="0" max="1" value="0.5" style="width:80px"></label><br><br>
      <label>Attention<br><select id="n-att" style="width:100%"><option value="">(none)</option>${this.ATTN.map(a => `<option>${a}</option>`).join("")}</select></label><br><br>
      <label>Layer<br><select id="n-layer" style="width:100%">${this.LAYERS.map(l => `<option${l === "working" ? " selected" : ""}>${l}</option>`).join("")}</select></label><br><br>
      <label>Extra data (JSON)<br><textarea id="n-meta" style="min-height:60px;font-size:11px">{}</textarea></label><br>
      <button class="btn" onclick="Tabs.manager.createNode()">Create</button>
      <button class="btn ghost" onclick="App.closeModal()">Cancel</button>
      <pre id="n-out" class="muted"></pre>`);
  },

  async createNode() {
    const out = document.getElementById("n-out");
    let meta = {};
    const rawMeta = document.getElementById("n-meta").value.trim();
    if (rawMeta && rawMeta !== "{}") {
      try { meta = JSON.parse(rawMeta); } catch (e) { out.textContent = "Extra data: " + e.message; return; }
    }
    const att = document.getElementById("n-att").value;
    if (att) meta.attention_state = att;
    const payload = {
      db: this.db,
      node_type: document.getElementById("n-type").value,
      label: document.getElementById("n-label").value,
      content: document.getElementById("n-content").value,
      trust: parseFloat(document.getElementById("n-trust").value),
      importance: parseFloat(document.getElementById("n-imp").value),
      source: document.getElementById("n-source").value || "CORE"
    };
    if (!payload.content.trim()) { out.textContent = "Content required"; return; }
    const ag = document.getElementById("n-agent");
    if (ag) payload.agent_id = ag.value;
    // attention/layer go via metadata + separate call? For create, layer via post-create bulk set
    const layer = document.getElementById("n-layer").value;
    if (Object.keys(meta).length) payload.metadata = meta; // server's node_add doesn't handle metadata yet, so we store via update after
    try {
      const r = await App.api("/api/node_add", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
      // apply attention/layer/metadata if needed
      if ((att || layer !== "working" || Object.keys(meta).length) && r.node_id) {
        const fields = {};
        if (Object.keys(meta).length) fields.metadata = meta;
        if (Object.keys(fields).length) {
          await App.api("/api/node_update", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: this.db, node_id: r.node_id, fields})});
        }
        if (layer !== "working") {
          await App.api("/api/bulk_nodes", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: this.db, node_ids: [r.node_id], action: "set_layer", value: layer})});
        }
      }
      App.closeModal(); App.toast("Created " + r.node_id);
      this.offset = 0; this.loadNodesData(); App.refresh(true); this.loadHealth();
    } catch (e) { out.textContent = "Error: " + e.message; }
  },

  async edit(id) {
    try {
      const d = await App.api(`/api/nodes?db=${this.db}&q=${encodeURIComponent(id)}&limit=1`);
      const n = (d.nodes || [])[0];
      if (!n) { App.toast("Not found"); return; }
      const meta = n.metadata || {};
      const att = meta.attention_state || "";
      App.modal(`<h3>Edit note</h3>
        <div class="muted">${App.esc(n.node_type)}${n._agent ? " · " + App.esc(n._agent) : ""} · layer ${App.esc(n.layer || "")} · added ${App.esc(App.ago(n.created_at))} · reads ${n.access_count || 0}×</div><br>
        <label>Label (<span id="e-label-n">0</span>)<br><input id="e-label" style="width:100%" value="${App.esc(n.label || "")}" oninput="document.getElementById('e-label-n').textContent=this.value.length"></label><br><br>
        <label>Content (<span id="e-content-n">0</span>)<br><textarea id="e-content" style="min-height:120px" oninput="document.getElementById('e-content-n').textContent=this.value.length">${App.esc(n.content || "")}</textarea></label><br>
        <label>Source<br><input id="e-source" style="width:100%" value="${App.esc(n.source || "")}"></label><br><br>
        <label>Trust <input id="e-trust" type="number" step="0.1" min="0" max="1" value="${n.trust_level}" style="width:80px"></label>
        <label>Importance <input id="e-imp" type="number" step="0.1" min="0" max="1" value="${n.importance}" style="width:80px"></label><br><br>
        <label>Attention<br><select id="e-att" style="width:100%"><option value="">(none)</option>${this.ATTN.map(a => `<option ${att === a ? "selected" : ""}>${a}</option>`).join("")}</select></label><br><br>
        <label>Layer<br><select id="e-layer" style="width:100%">${this.LAYERS.map(l => `<option ${n.layer === l ? "selected" : ""}>${l}</option>`).join("")}</select></label><br><br>
        <label>Extra data (JSON)<br><textarea id="e-meta" style="min-height:60px;font-size:11px">${App.esc(JSON.stringify(meta, null, 1))}</textarea></label><br>
        <button class="btn" onclick="Tabs.manager.save('${App.esc(id)}')">Apply to DB</button>
        <button class="btn ghost" onclick="App.closeModal()">Cancel</button>
        <pre id="e-out" class="muted"></pre>`);
      document.getElementById("e-label-n").textContent = (n.label || "").length;
      document.getElementById("e-content-n").textContent = (n.content || "").length;
      this._editOrig = n;
    } catch (e) { App.toast(e.message); }
  },

  async save(id) {
    const out = document.getElementById("e-out");
    let meta = null;
    const rawMeta = document.getElementById("e-meta").value.trim();
    if (rawMeta) {
      try {
        meta = JSON.parse(rawMeta);
        if (!meta || typeof meta !== "object" || Array.isArray(meta)) throw new Error("must be object");
      } catch (e) { out.textContent = "Extra data: " + e.message; return; }
    }
    const att = document.getElementById("e-att").value;
    if (meta) {
      if (att) meta.attention_state = att; else delete meta.attention_state;
    } else if (att) {
      meta = {attention_state: att};
    }
    const fields = {
      label: document.getElementById("e-label").value,
      content: document.getElementById("e-content").value,
      source: document.getElementById("e-source").value,
      trust_level: parseFloat(document.getElementById("e-trust").value),
      importance: parseFloat(document.getElementById("e-imp").value),
    };
    if (meta) fields.metadata = meta;
    const layer = document.getElementById("e-layer").value;
    try {
      const r = await App.api("/api/node_update", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: this.db, node_id: id, fields})});
      if (layer !== this._editOrig.layer) {
        await App.api("/api/bulk_nodes", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: this.db, node_ids: [id], action: "set_layer", value: layer})});
      }
      App.closeModal(); App.toast(r.status); this.loadNodesData(); App.refresh(true);
    } catch (e) { out.textContent = "Error: " + e.message; }
  },

  async del(id) {
    if (!confirm("Delete note " + id.slice(0, 12) + "? Its links go with it.")) return;
    try {
      await App.api("/api/node_delete", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: this.db, node_id: id})});
      delete this.sel[id]; App.toast("Deleted"); this.loadNodesData(); App.refresh(true); this.loadHealth();
    } catch (e) { App.toast(e.message); }
  },

  // ── Edges ──
  async loadEdges(forced) {
    const p = document.getElementById("mgr-panel");
    const q = forced === "orphan" ? "" : (document.getElementById("mgr-eq")?.value || "");
    const type = document.getElementById("mgr-etype")?.value || "";
    // if forced orphan, we render orphan edges from health
    if (forced === "orphan") {
      try {
        const h = await App.api(`/api/manager_health?db=${this.db}`);
        const rows = (h.orphans || []).map(e => `<tr><td class="muted" style="font-family:monospace;font-size:11px">${App.esc(e.edge_id.slice(0, 12))}</td>
          <td class="muted">${App.esc(e.from_node.slice(0, 12))} → ${App.esc(e.to_node.slice(0, 12))}</td><td><span class="tag" style="background:#3a1a1a;color:#e88">orphan ${App.esc(e.edge_type)}</span></td><td>${e.weight}</td>
          <td><button class="btn danger" style="padding:2px 6px;font-size:11px" onclick="Tabs.manager.delEdge('${App.esc(e.edge_id)}')">Del</button></td></tr>`).join("");
        p.innerHTML = `<div style="display:flex;gap:6px;align-items:center;margin-bottom:8px">
          <button class="btn ghost" onclick="Tabs.manager.loadEdges()">Back to all links</button>
          <span class="muted">${h.orphans.length} orphan links (missing endpoint)</span></div>
          <table><tr><th>ID</th><th>Endpoints</th><th>Kind</th><th>Wt</th><th></th></tr>${rows || `<tr><td colspan="5">No orphans.</td></tr>`}</table>`;
        return;
      } catch (e) { p.innerHTML = `<div class="muted">Error: ${App.esc(e.message)}</div>`; return; }
    }
    p.innerHTML = `<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input id="mgr-eq" placeholder="find edge…" value="${App.esc(q)}" style="width:170px">
      <select id="mgr-etype"><option value="">all kinds</option>${this.ETYPES.map(t => `<option ${type === t ? "selected" : ""}>${t}</option>`).join("")}</select>
      <button class="btn" onclick="Tabs.manager.applyEdgeFilter()">Filter</button>
      <button class="btn ghost" onclick="Tabs.manager.clearEdgeFilter()">Clear</button>
      <button class="btn" style="margin-left:auto" onclick="Tabs.manager.newEdge()">+ New link</button>
      <span id="mgr-einfo" class="muted"></span></div>
      <div id="mgr-edges-body"><span class="muted">Loading…</span></div>`;
    document.getElementById("mgr-eq").onkeydown = (e) => { if (e.key === "Enter") this.applyEdgeFilter(); };
    await this.loadEdgesData();
  },

  applyEdgeFilter() { this.offset = 0; this.loadEdgesData(); },
  clearEdgeFilter() { const q = document.getElementById("mgr-eq"); if (q) q.value = ""; const t = document.getElementById("mgr-etype"); if (t) t.value = ""; this.offset = 0; this.loadEdgesData(); },

  async loadEdgesData() {
    const info = document.getElementById("mgr-einfo");
    const body = document.getElementById("mgr-edges-body");
    const qEl = document.getElementById("mgr-eq");
    const tEl = document.getElementById("mgr-etype");
    const q = qEl ? qEl.value.trim() : "";
    const type = tEl ? tEl.value : "";
    try {
      const d = await App.api(`/api/edges?db=${this.db}&q=${encodeURIComponent(q)}&type=${encodeURIComponent(type)}&offset=${this.offset}&limit=${this.limit}`);
      if (info) info.textContent = `${d.total} links`;
      const rows = (d.edges || []).map(e => `<tr>
        <td class="muted" style="font-family:monospace;font-size:11px" title="${App.esc(e.edge_id)}">${App.esc(e.edge_id.slice(0, 12))}</td>
        <td>${App.esc(e.from_label || e.from_node.slice(0, 12))}<br><span class="muted" style="font-size:11px">${App.esc(e.from_node.slice(0, 16))}</span></td>
        <td>${App.esc(e.to_label || e.to_node.slice(0, 12))}<br><span class="muted" style="font-size:11px">${App.esc(e.to_node.slice(0, 16))}</span></td>
        <td><span class="tag" style="background:#1a2a3a;color:#6af">${App.esc(e.edge_type)}</span></td><td>${e.weight != null ? e.weight.toFixed(2) : "—"}</td>
        <td><button class="btn" style="padding:2px 6px;font-size:11px" onclick="Tabs.manager.editEdge('${App.esc(e.edge_id)}')">Edit</button>
          <button class="btn danger" style="padding:2px 6px;font-size:11px" onclick="Tabs.manager.delEdge('${App.esc(e.edge_id)}')">Del</button></td></tr>`).join("");
      body.innerHTML = `<table><tr><th>ID</th><th>From</th><th>To</th><th>Kind</th><th>Wt</th><th></th></tr>
        ${rows || `<tr><td colspan="6">No links.</td></tr>`}</table>
        <div style="margin-top:8px"><button class="btn ghost" onclick="Tabs.manager.edgePage(-1)" ${this.offset <= 0 ? "disabled" : ""}>Prev</button>
        <span class="muted">${this.offset + 1}–${Math.min(this.offset + this.limit, d.total)} of ${d.total}</span>
        <button class="btn ghost" onclick="Tabs.manager.edgePage(1)" ${this.offset + this.limit >= d.total ? "disabled" : ""}>Next</button></div>`;
    } catch (e) { body.innerHTML = `<div class="muted">Error: ${App.esc(e.message)}</div>`; }
  },

  edgePage(d) { this.offset = Math.max(0, this.offset + d * this.limit); this.loadEdgesData(); },

  newEdge() {
    const agentRow = this.db === "agents"
      ? `<label>Owner agent<br><select id="e-agent" style="width:100%">${this.agents.map(a => `<option>${App.esc(a)}</option>`).join("")}</select></label><br><br>` : "";
    App.modal(`<h3>New link — ${App.esc(this.db)}.db</h3>
      ${agentRow}
      <label>From (label or ID)<br><input id="e-from" list="mgr-dl" style="width:100%"></label><datalist id="mgr-dl"></datalist><br><br>
      <label>To (label or ID)<br><input id="e-to" list="mgr-dl2" style="width:100%"></label><datalist id="mgr-dl2"></datalist><br><br>
      <label>Kind<br><select id="e-type" style="width:100%">${this.ETYPES.map(t => `<option>${t}</option>`).join("")}</select></label><br><br>
      <label>Weight <input id="e-w" type="number" step="0.1" min="-1" max="1" value="1.0" style="width:80px"></label><br><br>
      <button class="btn" onclick="Tabs.manager.createEdge()">Create</button>
      <button class="btn ghost" onclick="App.closeModal()">Cancel</button>
      <pre id="e-out" class="muted"></pre>`);
    this.wireDl("e-from", "mgr-dl");
    this.wireDl("e-to", "mgr-dl2");
  },

  wireDl(inpId, dlId) {
    const inp = document.getElementById(inpId), dl = document.getElementById(dlId);
    let timer = null;
    inp.oninput = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = inp.value.trim();
        if (q.length < 2) return;
        try {
          const d = await App.api(`/api/nodes?db=${this.db}&q=${encodeURIComponent(q)}&limit=8`);
          dl.innerHTML = (d.nodes || []).map(n => `<option value="${App.esc(n.label)}">${App.esc(n.label)} — ${n.node_id.slice(0, 8)}</option><option value="${App.esc(n.node_id)}"></option>`).join("");
        } catch (e) {}
      }, 300);
    };
  },

  async createEdge() {
    const out = document.getElementById("e-out");
    const froRaw = document.getElementById("e-from").value.trim();
    const toRaw = document.getElementById("e-to").value.trim();
    const type = document.getElementById("e-type").value;
    const w = parseFloat(document.getElementById("e-w").value);
    if (!froRaw || !toRaw) { out.textContent = "Both ends required"; return; }
    // resolve label -> id
    const resolve = async (raw) => {
      const d = await App.api(`/api/nodes?db=${this.db}&q=${encodeURIComponent(raw)}&limit=5`);
      const m = (d.nodes || []).find(n => n.node_id === raw || n.label === raw);
      return m ? m.node_id : (d.nodes || [])[0]?.node_id || raw;
    };
    try {
      const fro = await resolve(froRaw);
      const to = await resolve(toRaw);
      if (fro === to) { out.textContent = "From and To cannot be same"; return; }
      const payload = {db: this.db, from_node: fro, to_node: to, edge_type: type, weight: w};
      const ag = document.getElementById("e-agent");
      if (ag) payload.agent_id = ag.value;
      const r = await App.api("/api/edge_add", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
      App.closeModal(); App.toast("Created " + r.edge_id); this.loadEdgesData(); App.refresh(true);
    } catch (e) { out.textContent = "Error: " + e.message; }
  },

  async editEdge(id) {
    // fetch edge detail via edges endpoint? Use sql for single edge
    try {
      const d = await App.api("/api/sql", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: this.db, sql: `SELECT * FROM edges WHERE edge_id='${id.replace(/'/g, "''")}'`})});
      const row = d.rows[0];
      if (!row) { App.toast("Not found"); return; }
      const cols = d.columns;
      const idx = (k) => cols.indexOf(k);
      const cur = {edge_id: row[idx("edge_id")], from_node: row[idx("from_node")], to_node: row[idx("to_node")], edge_type: row[idx("edge_type")], weight: row[idx("weight")], metadata: row[idx("metadata")]};
      let metaStr = "{}";
      try { metaStr = JSON.stringify(JSON.parse(cur.metadata || "{}"), null, 1); } catch (e) { metaStr = cur.metadata || "{}"; }
      App.modal(`<h3>Edit link</h3><div class="muted">${App.esc(cur.edge_id)}</div><br>
        <label>From<br><input id="ef-from" style="width:100%" value="${App.esc(cur.from_node)}"></label><br><br>
        <label>To<br><input id="ef-to" style="width:100%" value="${App.esc(cur.to_node)}"></label><br><br>
        <label>Kind<br><select id="ef-type" style="width:100%">${this.ETYPES.map(t => `<option ${t === cur.edge_type ? "selected" : ""}>${t}</option>`).join("")}</select></label><br><br>
        <label>Weight <input id="ef-w" type="number" step="0.1" min="-1" max="1" value="${cur.weight}" style="width:80px"></label><br><br>
        <label>Metadata<br><textarea id="ef-meta" style="min-height:60px;font-size:11px">${App.esc(metaStr)}</textarea></label><br>
        <button class="btn" onclick="Tabs.manager.saveEdge('${App.esc(id)}')">Save</button>
        <button class="btn ghost" onclick="App.closeModal()">Cancel</button>
        <pre id="ef-out" class="muted"></pre>`);
    } catch (e) { App.toast(e.message); }
  },

  async saveEdge(id) {
    const out = document.getElementById("ef-out");
    const fro = document.getElementById("ef-from").value.trim();
    const to = document.getElementById("ef-to").value.trim();
    const type = document.getElementById("ef-type").value;
    const w = parseFloat(document.getElementById("ef-w").value);
    let meta = {};
    const raw = document.getElementById("ef-meta").value.trim();
    if (raw) { try { meta = JSON.parse(raw); } catch (e) { out.textContent = "Metadata: " + e.message; return; } }
    if (!fro || !to) { out.textContent = "Both ends required"; return; }
    try {
      // delete + recreate is simplest for edge edit (edge_id stable, unique constraint)
      await App.api("/api/edge_delete", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: this.db, edge_id: id})});
      const payload = {db: this.db, from_node: fro, to_node: to, edge_type: type, weight: w};
      // preserve agent_id if agents.db
      if (this.db === "agents") {
        const cur = await App.api(`/api/nodes?db=${this.db}&q=${encodeURIComponent(fro)}&limit=1`);
        const ag = (cur.nodes || [])[0]?._agent || this.agents[0] || "";
        if (ag) payload.agent_id = ag;
      }
      const r = await App.api("/api/edge_add", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
      App.closeModal(); App.toast("Updated " + r.edge_id); this.loadEdgesData();
    } catch (e) { out.textContent = "Error: " + e.message; }
  },

  async delEdge(id) {
    if (!confirm("Delete link " + id.slice(0, 12) + "?")) return;
    try {
      await App.api("/api/edge_delete", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: this.db, edge_id: id})});
      App.toast("Deleted"); this.loadEdgesData(); App.refresh(true);
    } catch (e) { App.toast(e.message); }
  },

  // ── Vectors ──
  async loadVectors() {
    const p = document.getElementById("mgr-panel");
    p.innerHTML = `<div style="display:flex;gap:6px;align-items:center;margin-bottom:8px">
      <input id="mgr-vq" placeholder="find note…" style="width:170px" value="">
      <button class="btn" onclick="Tabs.manager.loadVectorsData()">Search</button>
      <span id="mgr-vinfo" class="muted"></span></div>
      <div id="mgr-vbody"><span class="muted">Loading…</span></div>`;
    document.getElementById("mgr-vq").onkeydown = (e) => { if (e.key === "Enter") this.loadVectorsData(); };
    await this.loadVectorsData();
  },
  async loadVectorsData() {
    const q = document.getElementById("mgr-vq")?.value.trim() || "";
    const body = document.getElementById("mgr-vbody");
    const info = document.getElementById("mgr-vinfo");
    try {
      const d = await App.api(`/api/vectors?db=${this.db}&q=${encodeURIComponent(q)}&offset=${this.offset}&limit=${this.limit}`);
      if (info) info.textContent = `${d.total} vectors`;
      body.innerHTML = `<table><tr><th>Note</th><th>Label</th><th>Top terms</th><th>Magnitude</th></tr>
        ${(d.vectors || []).map(v => `<tr><td class="muted" style="font-family:monospace;font-size:11px">${App.esc(v.node_id.slice(0, 12))}</td>
        <td>${App.esc(v.label)}</td>
        <td>${(v.top_terms || []).map(([t, w]) => `<span style="color:#8bf;margin-right:6px;font-size:11px">${App.esc(t)}<span style="color:#555">:${w.toFixed(2)}</span></span>`).join("") || `<span class="muted">—</span>`} <span class="muted" style="font-size:11px">(${v.term_count} terms)</span></td>
        <td>${v.magnitude != null ? v.magnitude.toFixed(2) : "—"}</td></tr>`).join("") || `<tr><td colspan="4">No vectors.</td></tr>`}</table>
        <div style="margin-top:8px"><button class="btn ghost" onclick="Tabs.manager.offset=Math.max(0,Tabs.manager.offset-Tabs.manager.limit);Tabs.manager.loadVectorsData()" ${this.offset <= 0 ? "disabled" : ""}>Prev</button>
        <span class="muted">${this.offset + 1}–${Math.min(this.offset + this.limit, d.total)} of ${d.total}</span>
        <button class="btn ghost" onclick="Tabs.manager.offset+=Tabs.manager.limit;Tabs.manager.loadVectorsData()" ${this.offset + this.limit >= d.total ? "disabled" : ""}>Next</button></div>`;
    } catch (e) { body.innerHTML = `<div class="muted">Error: ${App.esc(e.message)}</div>`; }
  },

  // ── Layers ──
  async loadLayers() {
    const p = document.getElementById("mgr-panel");
    p.innerHTML = `<span class="muted">Loading…</span>`;
    try {
      const d = await App.api(`/api/layers?db=${this.db}`);
      const grp = d.layers || {};
      const order = ["working","short_term","long_term","archive"];
      p.innerHTML = order.map(l => {
        const nodes = grp[l] || [];
        return `<div style="margin-bottom:12px"><h4>${l} — ${nodes.length} notes</h4>
          ${nodes.slice(0, 200).map(n => `<div style="font-size:12px;padding:2px 0;border-bottom:1px solid #222"><span class="type-badge type-${n.node_type}">${n.node_type}</span> ${App.esc(n.label || n.node_id.slice(0, 12))} <span class="muted">${App.esc((n.att || n.metadata && JSON.parse(n.metadata || "{}").attention_state) || "")}</span> ${n.promoted_at ? `<span class="muted">${App.esc(App.ago(n.promoted_at))}</span>` : ""}</div>`).join("") || `<span class="muted">none</span>`}
          ${nodes.length > 200 ? `<div class="muted">… ${nodes.length - 200} more</div>` : ""}</div>`;
      }).join("");
    } catch (e) { p.innerHTML = `<div class="muted">Error: ${App.esc(e.message)}</div>`; }
  },

  // ── Path ──
  async loadPath() {
    const p = document.getElementById("mgr-panel");
    p.innerHTML = `<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
      <input id="mgr-pfrom" list="mgr-pdl" placeholder="From (label or ID)" style="width:200px"><input id="mgr-pto" list="mgr-pdl" placeholder="To (label or ID)" style="width:200px"><datalist id="mgr-pdl"></datalist>
      <button class="btn" onclick="Tabs.manager.findPath()">Find path</button>
      <span class="muted">Shortest hops (undirected)</span></div>
      <pre id="mgr-path-out" style="white-space:pre-wrap;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:12px;min-height:120px">Pick two notes.</pre>`;
    const wire = (id) => {
      const el = document.getElementById(id);
      let t = null;
      el.oninput = () => {
        clearTimeout(t);
        t = setTimeout(async () => {
          const q = el.value.trim();
          if (q.length < 2) return;
          try {
            const d = await App.api(`/api/nodes?db=${this.db}&q=${encodeURIComponent(q)}&limit=6`);
            document.getElementById("mgr-pdl").innerHTML = (d.nodes || []).map(n => `<option value="${App.esc(n.label)}">${App.esc(n.label)} — ${n.node_id.slice(0, 8)}</option><option value="${App.esc(n.node_id)}"></option>`).join("");
          } catch (e) {}
        }, 300);
      };
    };
    wire("mgr-pfrom"); wire("mgr-pto");
  },
  async findPath() {
    const fro = document.getElementById("mgr-pfrom").value.trim();
    const to = document.getElementById("mgr-pto").value.trim();
    const out = document.getElementById("mgr-path-out");
    if (!fro || !to) { out.textContent = "Both ends required"; return; }
    out.textContent = "Searching…";
    try {
      const d = await App.api(`/api/path?db=${this.db}&from=${encodeURIComponent(fro)}&to=${encodeURIComponent(to)}`);
      if (!d.found) { out.textContent = d.message || "No path found"; return; }
      const steps = d.steps || [];
      out.textContent = `Path — ${d.hops} hop(s)\n` + steps.map((s, i) => {
        if (i === 0) return `  ${s.from_label} [${s.from.slice(0, 8)}]`;
        return `    ${s.edge_type} →\n  ${s.to_label} [${s.to.slice(0, 8)}]`;
      }).join("\n");
    } catch (e) { out.textContent = "Error: " + e.message; }
  },

  // ── Schema ──
  async loadSchema() {
    const p = document.getElementById("mgr-panel");
    p.innerHTML = `<span class="muted">Loading…</span>`;
    try {
      const d = await App.api(`/api/schema?db=${this.db}`);
      p.innerHTML = (d.tables || []).map(t => `<div style="margin-bottom:12px"><h4>${App.esc(t.name)}</h4>
        <pre style="white-space:pre-wrap;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px;font-size:11px">${App.esc(t.sql || "")}</pre>
        <div class="muted" style="font-size:11px">Columns: ${(t.columns || []).map(c => `${c.name} (${c.type})`).join(", ")}</div>
        <div class="muted" style="font-size:11px">Indexes: ${(t.indexes || []).map(i => i.name).join(", ") || "—"}</div></div>`).join("")
        + ((d.schema_meta || []).length ? `<h4>schema_meta</h4><pre>${App.esc((d.schema_meta || []).map(r => `${r.key} = ${r.value}`).join("\n"))}</pre>` : "");
    } catch (e) { p.innerHTML = `<div class="muted">Error: ${App.esc(e.message)}</div>`; }
  },

  // ── Stats ──
  async loadStats() {
    const p = document.getElementById("mgr-panel");
    p.innerHTML = `<span class="muted">Loading…</span>`;
    try {
      const d = await App.api(`/api/statistics?db=${this.db}`);
      const dd = d[this.db] || d.core || {};
      const pad = (s, n) => s + " ".repeat(Math.max(0, n - s.length));
      let out = `${this.db}.db — ${dd.total_nodes} notes, ${dd.total_edges} links\n`;
      out += `Vectors: ${dd.vectors ?? "?"}  Layers: ${JSON.stringify(dd.layers || {})}\n\n`;
      out += "-- Kinds --\n";
      Object.entries(dd.node_types || {}).sort((a, b) => b[1] - a[1]).forEach(([k, v]) => { out += `  ${pad(k, 16)} ${v}\n`; });
      out += "\n-- Layers --\n";
      Object.entries(dd.layers || {}).forEach(([k, v]) => { out += `  ${pad(k, 16)} ${v}\n`; });
      out += "\n-- Edges --\n";
      Object.entries(dd.edge_types || {}).sort((a, b) => b[1] - a[1]).forEach(([k, v]) => { out += `  ${pad(k, 16)} ${v}\n`; });
      out += `\nAverages — trust ${dd.trust_avg ?? "?"}  importance ${dd.importance_avg ?? "?"}\n`;
      p.innerHTML = `<pre style="white-space:pre-wrap;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:12px">${App.esc(out)}</pre>`;
    } catch (e) { p.innerHTML = `<div class="muted">Error: ${App.esc(e.message)}</div>`; }
  },

  // ── SQL ──
  async loadSql() {
    const p = document.getElementById("mgr-panel");
    p.innerHTML = `<div style="display:flex;gap:6px;align-items:center;margin-bottom:8px">
      <span class="muted">Read-only: SELECT / PRAGMA / EXPLAIN only</span>
      <button class="btn" style="margin-left:auto" onclick="Tabs.manager.runSql()">Run</button>
      <span id="mgr-sql-info" class="muted"></span></div>
      <textarea id="mgr-sql" style="width:100%;min-height:80px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:8px;font-family:monospace;font-size:12px" placeholder="SELECT * FROM nodes LIMIT 10"></textarea>
      <div id="mgr-sql-out" style="margin-top:8px;overflow:auto;max-height:400px"></div>`;
  },
  async runSql() {
    const sql = document.getElementById("mgr-sql").value.trim();
    const out = document.getElementById("mgr-sql-out");
    const info = document.getElementById("mgr-sql-info");
    if (!sql) return;
    out.innerHTML = `<span class="muted">Running…</span>`;
    try {
      const d = await App.api("/api/sql", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: this.db, sql})});
      if (info) info.textContent = `${d.row_count} rows${d.truncated ? " (first 200)" : ""}`;
      const head = `<tr>${(d.columns || []).map(c => `<th>${App.esc(c)}</th>`).join("")}</tr>`;
      const rows = (d.rows || []).map(r => `<tr>${r.map(v => `<td style="font-size:11px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${App.esc(v ?? "")}">${App.esc(v ?? "")}</td>`).join("")}</tr>`).join("");
      out.innerHTML = `<table><thead>${head}</thead><tbody>${rows || `<tr><td>No rows</td></tr>`}</tbody></table>`;
    } catch (e) { out.innerHTML = `<div style="color:#f85149">Error: ${App.esc(e.message)}</div>`; }
  },
};
