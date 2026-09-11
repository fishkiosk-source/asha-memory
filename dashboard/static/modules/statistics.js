// Statistics tab — clean CORE vs AGENTS separation, no mixed view.
"use strict";
window.Tabs.statistics = {
  db: "all",
  colors: {PERSON:"#7ee787",FACT:"#d2a8ff",EVENT:"#ffab70",TOPIC:"#79c0ff",PREFERENCE:"#ff7eb6",BOUNDARY:"#ffa198",AFFECT:"#ff7eb6",AGENT_NOTE:"#e3b341",CORE_REF:"#79c0ff",SKILL:"#7ee787",working:"#ffab70",short_term:"#79c0ff",long_term:"#7ee787",archive:"#d2a8ff",RELATES_TO:"#8b949e",CONTRADICTS:"#ffa198",SUPPORTS:"#7ee787",CAUSED_BY:"#ffab70",PART_OF:"#79c0ff",CORE:"#79c0ff",AGENT:"#e3b341"},
  color(k) { return this.colors[k] || "#58a6ff"; },

  barCanvas(id, dist, total) {
    const cv = document.getElementById(id);
    if (!cv) return;
    const entries = Object.entries(dist || {}).sort((a,b)=>b[1]-a[1]).slice(0,8);
    if (!entries.length) {
      const ctx=cv.getContext("2d"); ctx.clearRect(0,0,cv.width,cv.height);
      ctx.fillStyle="#8b949e"; ctx.font="12px system-ui"; ctx.fillText("no data",10,20);
      return;
    }
    const W=cv.width=360, H=cv.height=150, padL=105, padR=12, barH=13, gap=5;
    const max=Math.max(...entries.map(([,v])=>v),1);
    const ctx=cv.getContext("2d");
    ctx.clearRect(0,0,W,H);
    entries.forEach(([k,v],i)=>{
      const y=6+i*(barH+gap), w=(v/max)*(W-padL-padR-35);
      const col=this.color(k);
      ctx.fillStyle="#21262d"; ctx.fillRect(padL,y,W-padL-padR,barH);
      ctx.fillStyle=col; ctx.fillRect(padL,y,w,barH);
      ctx.fillStyle="#c9d1d9"; ctx.font="11px system-ui"; ctx.textAlign="right";
      ctx.fillText(k.slice(0,13), padL-8, y+9);
      ctx.textAlign="left"; ctx.fillStyle="#8b949e"; ctx.font="11px system-ui";
      ctx.fillText(String(v), padL+w+6, y+9);
      if(total){ctx.fillStyle="#6e7681"; ctx.font="10px system-ui"; ctx.fillText(Math.round(100*v/total)+"%", W-padR-28, y+9);}
    });
  },

  donutCanvas(id, dist) {
    const cv=document.getElementById(id);
    if(!cv) return;
    const entries=Object.entries(dist||{}).sort((a,b)=>b[1]-a[1]);
    if(!entries.length){const ctx=cv.getContext("2d");ctx.clearRect(0,0,cv.width,cv.height);ctx.fillStyle="#8b949e";ctx.font="12px system-ui";ctx.fillText("no data",10,20);return;}
    const W=cv.width=150,H=cv.height=150, cx=W/2, cy=H/2, r=58, ir=36;
    const total=entries.reduce((s,[,v])=>s+v,0);
    const ctx=cv.getContext("2d");
    ctx.clearRect(0,0,W,H);
    let a0=-Math.PI/2;
    entries.forEach(([k,v])=>{
      const a1=a0+(v/total)*Math.PI*2;
      ctx.beginPath();ctx.moveTo(cx,cy);ctx.arc(cx,cy,r,a0,a1);ctx.closePath();
      ctx.fillStyle=this.color(k);ctx.fill();
      ctx.strokeStyle="#0d1117";ctx.lineWidth=2;ctx.stroke();
      a0=a1;
    });
    ctx.beginPath();ctx.arc(cx,cy,ir,0,Math.PI*2);ctx.fillStyle="#161b22";ctx.fill();
    ctx.fillStyle="#c9d1d9";ctx.font="bold 13px system-ui";ctx.textAlign="center";ctx.fillText(String(total),cx,cy+4);
    ctx.font="10px system-ui";ctx.fillStyle="#8b949e";ctx.fillText("notes",cx,cy+14);
  },

  gauge(label, value, col) {
    const pct = Math.round(value*100);
    return `<div style="flex:1;min-width:90px;text-align:center"><div style="font-size:11px;color:#8b949e">${App.esc(label)}</div><div style="height:6px;background:#21262d;border-radius:3px;margin:4px 0;overflow:hidden"><div style="width:${pct}%;height:100%;background:${col}"></div></div><div style="font-size:12px;font-weight:700;color:${col}">${pct}%</div><div style="font-size:10px;color:#6e7681">avg ${(value||0).toFixed(3)}</div></div>`;
  },

  distTable(title, dist, total) {
    const rows=Object.entries(dist||{}).sort((a,b)=>b[1]-a[1]);
    if(!rows.length) return `<div><b>${App.esc(title)}</b> <span class="muted">— none</span></div>`;
    const body=rows.slice(0,6).map(([k,v])=>{
      const pct=total?Math.round(100*v/total):0;
      return `<tr><td><span class="tag" style="background:${this.color(k)}22;color:${this.color(k)};border-color:${this.color(k)}44">${App.esc(k)}</span></td><td style="text-align:right"><b>${v}</b> <span class="muted">${pct}%</span></td></tr>`;
    }).join("");
    return `<div><b style="font-size:12px;letter-spacing:.02em">${App.esc(title)}</b><table class="plain" style="margin-top:4px">${body}</table></div>`;
  },

  dbPanel(key, d, col) {
    if (!d) return "";
    const total = d.total_nodes || 0;
    return `
    <div class="card" style="border-top:3px solid ${col}; padding:14px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <h3 style="margin:0;color:${col};font-size:16px">${key === "core" ? "CORE DB" : "AGENTS DB"}</h3>
        <span class="tag" style="background:${col}22;color:${col};border-color:${col}55">${d.db_size_mb} MB</span>
        <span class="muted" style="margin-left:auto;font-size:11px">${total} notes · ${d.total_edges} links</span>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">
        <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:10px;display:flex;gap:8px">
          <div style="flex:1;text-align:center"><div style="font-size:22px;font-weight:800;color:${col}">${total}</div><div style="font-size:11px;color:#8b949e">NOTES</div></div>
          <div style="flex:1;text-align:center"><div style="font-size:22px;font-weight:800;color:#8b949e">${d.total_edges}</div><div style="font-size:11px;color:#8b949e">LINKS</div></div>
          <div style="flex:1;text-align:center"><div style="font-size:22px;font-weight:800;color:#6e7681">${d.db_size_mb}</div><div style="font-size:11px;color:#8b949e">MB</div></div>
        </div>
        <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:10px;display:flex;gap:10px;align-items:center">
          ${this.gauge("trust", d.trust_avg||0, "#7ee787")}
          ${this.gauge("importance", d.importance_avg||0, "#d2a8ff")}
        </div>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
        <div>
          <h4 style="margin:0 0 6px;font-size:12px;letter-spacing:.02em;color:#8b949e">NOTE KINDS</h4>
          <canvas id="c-${key}-kinds" width="360" height="150" style="max-width:100%;background:#0d1117;border:1px solid #21262d;border-radius:6px"></canvas>
          <div style="margin-top:6px">${this.distTable("", d.node_types, total)}</div>
        </div>
        <div>
          <h4 style="margin:0 0 6px;font-size:12px;letter-spacing:.02em;color:#8b949e">LINK KINDS</h4>
          <canvas id="c-${key}-edges" width="360" height="150" style="max-width:100%;background:#0d1117;border:1px solid #21262d;border-radius:6px"></canvas>
          <div style="margin-top:6px">${this.distTable("", d.edge_types, d.total_edges)}</div>
        </div>
      </div>

      <div style="display:grid;grid-template-columns:150px 1fr;gap:12px;margin-top:12px;align-items:start">
        <div>
          <h4 style="margin:0 0 6px;font-size:12px;letter-spacing:.02em;color:#8b949e">LAYERS</h4>
          <canvas id="c-${key}-layers" width="150" height="150" style="background:#0d1117;border:1px solid #21262d;border-radius:6px"></canvas>
        </div>
        <div>
          <div style="margin-bottom:8px">${this.distTable("Layers", d.layers, total)}</div>
          <div>${this.distTable("Sources", d.sources, total)}</div>
        </div>
      </div>

      <div style="margin-top:12px">
        <h4 style="margin:0 0 6px;font-size:12px;letter-spacing:.02em;color:#8b949e">MOST USED LABELS</h4>
        <div style="display:flex;gap:4px;flex-wrap:wrap">${(d.top_labels||[]).slice(0,8).map(t=>`<span class="tag" style="background:#21262d;color:#c9d1d9;border-color:#30363d">${App.esc(t.label)} <b style="color:${col}">×${t.cnt}</b></span>`).join(" ")||`<span class="muted">none</span>`}</div>
      </div>
    </div>`;
  },

  async render(el) {
    el.innerHTML = `<div class="card">Loading statistics…</div>`;
    try {
      const s=await App.api(`/api/statistics?db=${this.db}`);
      const core=s.core, agents=s.agents, combined=s.combined;

      // header with selector + export
      let header = `<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
        <label>Show <select id="s-db"><option value="all">Both — side by side</option><option value="core">CORE DB only</option><option value="agents">AGENTS DB only</option></select></label>
        <span class="muted">— live, updates on every change</span>
        <span style="margin-left:auto;display:flex;gap:6px">
          <button class="btn ghost" style="padding:4px 10px;font-size:12px" onclick="Tabs.statistics.exportMd('core')">Export CORE md</button>
          <button class="btn ghost" style="padding:4px 10px;font-size:12px" onclick="Tabs.statistics.exportMd('agents')">Export AGENTS md</button>
          <button class="btn" style="padding:4px 10px;font-size:12px;background:#238636" onclick="Tabs.statistics.exportMd('all')">Export ALL md</button>
        </span>`;

      if (this.db === "all" && combined) {
        header += `<span style="margin-left:auto" class="tag grey">Combined <b>${combined.total_nodes}</b> notes · <b>${combined.total_edges}</b> links</span>`;
      } else if (this.db === "core" && core) {
        header += `<span style="margin-left:auto" class="tag" style="background:#79c0ff22;color:#79c0ff;border-color:#79c0ff55"><b>${core.total_nodes}</b> notes in CORE</span>`;
      } else if (this.db === "agents" && agents) {
        header += `<span style="margin-left:auto" class="tag" style="background:#e3b34122;color:#e3b341;border-color:#e3b34155"><b>${agents.total_nodes}</b> notes in AGENTS</span>`;
      }
      header += `</div>`;

      let body = "";
      if (this.db === "all") {
        body = `<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
          ${this.dbPanel("core", core, "#79c0ff")}
          ${this.dbPanel("agents", agents, "#e3b341")}
        </div>`;
        // combined is already in header, no extra card needed
      } else if (this.db === "core") {
        body = this.dbPanel("core", core, "#79c0ff");
      } else {
        body = this.dbPanel("agents", agents, "#e3b341");
      }

      el.innerHTML = header + body;
      const sel=document.getElementById("s-db");
      if(sel){sel.value=this.db; sel.onchange=(e)=>{this.db=e.target.value; this.render(document.getElementById("panel"));};}
      // draw after DOM
      setTimeout(()=>{
        if (this.db === "all" || this.db === "core") {
          this.barCanvas("c-core-kinds", core?core.node_types:{}, core?core.total_nodes:0);
          this.barCanvas("c-core-edges", core?core.edge_types:{}, core?core.total_edges:0);
          this.donutCanvas("c-core-layers", core?core.layers:{});
        }
        if (this.db === "all" || this.db === "agents") {
          this.barCanvas("c-agents-kinds", agents?agents.node_types:{}, agents?agents.total_nodes:0);
          this.barCanvas("c-agents-edges", agents?agents.edge_types:{}, agents?agents.total_edges:0);
          this.donutCanvas("c-agents-layers", agents?agents.layers:{});
        }
      },40);
    } catch(e){ el.innerHTML=`<div class="card">Error: ${App.esc(e.message)}</div>`; }
  },
  async exportMd(which) {
    try {
      const s = await App.api(`/api/statistics?db=${which === "all" ? "all" : which}`);
      const dbs = which === "all" ? ["core","agents"] : [which];
      const now = new Date().toISOString();
      let md = `# ASHA Memory — Statistics Export\n\n> Generated ${now} — via \`GET /api/statistics?db=${which}\`\n\n`;
      for (const db of dbs) {
        const d = s[db];
        if (!d) continue;
        md += `## ${db.toUpperCase()} DB — ${d.db_size_mb} MB — ${d.total_nodes} notes · ${d.total_edges} links\n\n`;
        md += `| Metric | Value |\n|---|---|\n`;
        md += `| Notes | ${d.total_nodes} |\n| Links | ${d.total_edges} |\n| Size | ${d.db_size_mb} MB |\n| Avg trust | ${d.trust_avg} |\n| Avg importance | ${d.importance_avg} |\n\n`;
        const fmtDist = (title, dist) => {
          const rows = Object.entries(dist||{}).sort((a,b)=>b[1]-a[1]);
          if (!rows.length) return `**${title}:** — none\n\n`;
          let out = `**${title}:**\n\n| Type | Count | % |\n|---|---|---|\n`;
          const total = db === "core" ? d.total_nodes : d.total_nodes;
          // use appropriate total: node_types/layers/sources use nodes, edge_types use edges
          const isEdge = title.toLowerCase().includes("edge");
          const denom = isEdge ? d.total_edges : d.total_nodes;
          for (const [k,v] of rows) out += `| ${k} | ${v} | ${denom?Math.round(100*v/denom):0}% |\n`;
          return out + `\n`;
        };
        md += fmtDist("Note kinds", d.node_types);
        md += fmtDist("Link kinds", d.edge_types);
        md += fmtDist("Layers", d.layers);
        md += fmtDist("Sources", d.sources);
        md += `**Top labels:**\n\n| Label | Count |\n|---|---|\n`;
        for (const t of (d.top_labels||[])) md += `| ${t.label} | ${t.cnt} |\n`;
        if (!(d.top_labels||[]).length) md += `| — | — |\n`;
        md += `\n---\n\n`;
      }
      if (s.combined) md += `## Combined\n\n| Metric | Value |\n|---|---|\n| Notes | ${s.combined.total_nodes} |\n| Links | ${s.combined.total_edges} |\n\n`;
      const blob = new Blob([md], {type: "text/markdown"});
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `asha-stats-${which}-${now.slice(0,10)}.md`;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      App.toast(`Exported ${which} statistics`);
    } catch(e){ App.toast(e.message); }
  },
};
