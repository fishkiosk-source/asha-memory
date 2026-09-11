// System tab — backups & rollback split per database + run journals + history.
"use strict";
window.Tabs.system = {
  snapTable(title, list) {
    const rows = (list || []).map(s => `<tr><td>${App.esc(s.filename)}</td>
      <td>${App.esc(s.created_at || "")}</td><td>${(s.size_bytes / 1048576).toFixed(2)} MB</td>
      <td><button class="btn warn" onclick="Tabs.system.restore('${App.esc(s.filename)}')">Restore</button>
      <button class="btn danger" onclick="Tabs.system.del('${App.esc(s.filename)}')">Delete</button></td></tr>`).join("");
    return `<div class="card"><h3>${title}</h3>
      <table><tr><th>Backup</th><th>Taken</th><th>Size</th><th></th></tr>
      ${rows || "<tr><td colspan=4>None yet — Snapshot Now lives in Maintenance.</td></tr>"}</table></div>`;
  },
  async render(el) {
    el.innerHTML = `<div class="card">Loading system…</div>`;
    try {
      const [snaps, logs, hist] = await Promise.all([
        App.api("/api/snapshots?db=all"), App.api("/api/logs"),
        App.api("/api/history?limit=20"),
      ]);
      const all = snaps.snapshots || [];
      const core = all.filter(s => (s.filename || "").includes("_core_"));
      const agents = all.filter(s => (s.filename || "").includes("_agents_"));
      const other = all.filter(s => !(s.filename || "").includes("_core_") && !(s.filename || "").includes("_agents_"));
      const journalTable = (title, list, desc) => {
        const rows = (list || []).map(l =>
          `<tr><td><a href="#" onclick="Tabs.system.view('${App.esc(l.filename)}');return false">${App.esc(l.filename)}</a></td>
          <td>${(l.size_bytes / 1024).toFixed(1)} KB</td></tr>`).join("");
        return `<div class="card"><h3>${title}</h3><span class="fdesc">${desc}</span>
          <table><tr><th>Journal</th><th>Size</th></tr>
          ${rows || "<tr><td colspan=2>None</td></tr>"}</table></div>`;
      };
      const allLogs = logs.logs || [];
      const runLogs = allLogs.filter(l => !l.filename.includes("_core.") && !l.filename.includes("_agents."));
      const coreLogs = allLogs.filter(l => l.filename.includes("_core."));
      const agentsLogs = allLogs.filter(l => l.filename.includes("_agents."));
      const hrows = (hist.history || []).slice(-20).reverse().map(e => {
        const st = (e.status || "") === "success" ? `<span class="tag green">ok</span>`
          : `<span class="tag amber">${App.esc(e.status || "?")}</span>`;
        return `<tr><td>${App.esc(e.timestamp || "")}</td><td>${App.esc((e.jobs || []).join(", "))}</td>
        <td>${App.esc(e.target || "")}</td><td>${App.esc(e.duration_s || "")}s</td>
        <td>${st}</td></tr>`;
      }).join("");
      el.innerHTML = `<div class="grid2">${this.snapTable("Backups — core.db", core)}${this.snapTable("Backups — agents.db", agents)}</div>
        ${other.length ? this.snapTable("Backups — other", other) : ""}
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px">
          ${journalTable("Run Journals", runLogs, "Combined — both DBs in one file")}
          ${journalTable("Core Journals", coreLogs, "core.db only")}
          ${journalTable("Agents Journals", agentsLogs, "agents.db only")}
        </div>
        <div class="card"><h3>Execution history</h3><table><tr><th>Time</th><th>Jobs</th><th>Target</th><th>Took</th><th>Result</th></tr>
        ${hrows || "<tr><td colspan=5>None</td></tr>"}</table></div>`;
    } catch (e) { el.innerHTML = `<div class="card">Error: ${App.esc(e.message)}</div>`; }
  },
  async restore(filename) {
    const db = filename.includes("_agents_") ? "agents" : "core";
    if (!confirm(`Restore ${filename} over ${db}.db (pre-rollback backup taken)?`)) return;
    try {
      const r = await App.api("/api/restore_snapshot", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({db, filename})});
      App.toast(JSON.stringify(r).slice(0, 200));
      this.render(document.getElementById("panel")); App.refresh(true);
    } catch (e) { App.toast(e.message); }
  },
  async del(filename) {
    if (!confirm("Delete " + filename + "?")) return;
    try {
      await App.api("/api/delete_snapshot", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({filename})});
      this.render(document.getElementById("panel")); App.refresh(true);
    } catch (e) { App.toast(e.message); }
  },
   fmtLog(text) {
     // Human-readable journal renderer with system colours
     const esc = App.esc;
     const lines = esc(text).split("\n");
     let out = [];
     let inCode = false;
     for (let raw of lines) {
       const line = raw.trimEnd();
       if (!line.trim()) { out.push(`<div style="height:6px"></div>`); continue; }
       // code fence
       if (line.trim().startsWith("```")) { inCode = !inCode; out.push(inCode ? `<pre style="background:#010409;border:1px solid #21262d;border-radius:6px;padding:10px;font-size:11px;overflow:auto;margin:8px 0">` : `</pre>`); continue; }
       if (inCode) { out.push(line); continue; }
       let h = line.match(/^(#{1,3})\s+(.*)$/);
       if (h) {
         const lvl = h[1].length;
         const txt = h[2].replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
         if (lvl===1){
           // main run header
           const when = txt.replace("Brain run","").trim();
           out.push(`<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px;margin-bottom:12px;display:flex;align-items:center;gap:10px"><div style="width:10px;height:10px;border-radius:50%;background:#7ee787;box-shadow:0 0 8px rgba(126,231,135,.4)"></div><div><div style="font-size:15px;font-weight:600;color:#e6edf3">${txt}</div><div class="muted" style="font-size:11px">${esc(when)} · maintenance run</div></div><span class="tag" style="margin-left:auto;background:#21262d;color:#8b949e;border:1px solid #30363d">brain</span></div>`);
         } else if (lvl===2){
           out.push(`<div style="margin:14px 0 6px;padding:8px 10px;background:#161b22;border-left:3px solid #79c0ff;border-radius:4px;font-size:13px;font-weight:600;color:#e6edf3;display:flex;align-items:center;gap:8px"><span style="color:#79c0ff">#</span> ${txt} <span class="tag" style="margin-left:auto;background:#0f2a4d;color:#79c0ff;border-color:#1a3d6b;font-size:10px">${txt.toLowerCase().includes("health")?"health":txt.toLowerCase().includes("contrad")?"check":"job"}</span></div>`);
         } else {
           out.push(`<h${lvl+2} style="color:#c9d1d9;margin:10px 0 6px;font-size:13px">${txt}</h${lvl+2}>`);
         }
         continue;
       }
       if (/^\s*-\s+/.test(line)) {
         const m = line.match(/^(\s*)- (?:(core|agents|combined):\s+)?(.*)$/);
         const indent = (m && m[1] ? m[1].length : 0);
         const who = m && m[2] ? `<span class="tag ${m[2]==="core"?"blue":m[2]==="agents"?"amber":"grey"}" style="font-size:10px">${m[2]}</span>` : "";
         let body = (m ? m[3] : line.replace(/^\s*-\s+/,"")).replace(/\*\*(.+?)\*\*/g, "<b style='color:#e6edf3'>$1</b>");
         // detect key: value pairs with dict/json
         // pretty JSON dict like {'db': 'core', ...}
         if (body.includes("{") && body.includes("}")) {
           // try to pretty the dict
           body = body.replace(/'([^']+)':/g, '"$1":').replace(/'/g, '"');
           try { const obj = JSON.parse(body.slice(body.indexOf("{"))); body = body.slice(0, body.indexOf("{")) + `<pre style="background:#010409;border:1px solid #21262d;border-radius:6px;padding:8px;font-size:11px;overflow:auto;margin:6px 0;white-space:pre-wrap;word-break:break-all">${esc(JSON.stringify(obj,null,2))}</pre>`; } catch(e){}
         } else if (body.includes("=")) {
           // key=value pairs like contradictions_found=0
           body = body.replace(/(\w+)=([^\s,]+)/g, `<span class="muted">$1=</span><b style="color:#79c0ff">$2</b>`);
         } else if (body.includes(":")) {
           body = body.replace(/^([^:]+):/, `<span class="muted">$1:</span>`);
         }
         const pad = indent ? `margin-left:${indent*8}px` : "";
         out.push(`<div style="display:flex;gap:8px;align-items:flex-start;padding:4px 0;border-bottom:1px solid #161b22;${pad}"><span style="color:#30363d;margin-top:2px">•</span><div style="flex:1;font-size:12px;color:#c9d1d9">${who?who+" ":""}${body}</div></div>`);
         continue;
       }
       out.push(`<div style="font-size:12px;color:#8b949e;padding:2px 0">${line.replace(/\*\*(.+?)\*\*/g, "<b style='color:#e6edf3'>$1</b>")}</div>`);
     }
     return `<div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px;max-height:60vh;overflow:auto;font-family:system-ui;line-height:1.5">${out.join("")}</div>`;
   },
   async view(filename) {
     try {
       const d = await App.api(`/api/log_content?file=${encodeURIComponent(filename)}`);
       const isRun = filename.startsWith("brain_run");
       const badge = isRun ? `<span class="tag" style="background:#12361f;color:#7ee787;border-color:#1f4a2a">run</span>` : `<span class="tag grey">log</span>`;
       App.modal(`<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px"><h3 style="margin:0;flex:1">${App.esc(filename)}</h3>${badge}<button class="btn ghost" style="padding:4px 8px;font-size:11px" onclick="navigator.clipboard.writeText(document.getElementById('log-view-raw').textContent).then(()=>App.toast('Copied'))">Copy</button></div><div id="log-view-raw" style="display:none">${App.esc((d.content||"").slice(0,15000))}</div>${this.fmtLog((d.content || "").slice(0, 15000))}<div style="margin-top:12px;display:flex;gap:8px"><button class="btn" onclick="App.closeModal()">Close</button><span class="muted" style="font-size:11px;margin-left:auto">${App.esc((d.content||"").length)} chars · ${(d.content||"").split("\n").length} lines</span></div>`);
     } catch (e) { App.toast(e.message); }
   },
};
