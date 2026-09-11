// dashboard/static/modules/graph.js — Galaxy + Heat + Hot + Minimap + Lasso + Expand + Time fade + Hulls + Centrality + Bundling + Orbits + Share
"use strict";
window.Tabs.graph = {
  db: "core", limit: 300, nodes: [], edges: [], truncated: false, f: null, t: {x:0,y:0,k:1}, focus:null, sel:null, hover:null, adj:new Map(), deg:new Map(), byId:{},
  galaxy:false, heat:false, heatMetric:'combined', timeFade:0, showOrbits:false, bundled:false, showHulls:true, transparent:false, hot:[],
  searchHits:[], searchIdx:-1, lasso:{active:false,x0:0,y0:0,x1:0,y1:0,sel:new Set()},
  NCOLORS: {PERSON:"#4caf50", FACT:"#ab47bc", PREFERENCE:"#26c6da", EVENT:"#ffa726", TOPIC:"#42a5f5", AFFECT:"#ec407a", BOUNDARY:"#ef5350", SKILL:"#66bb6a", AGENT_NOTE:"#d4e157", CORE_REF:"#7e57c2"},
  ECOLORS: {RELATES_TO:"#888", CONTRADICTS:"#ef5350", SUPPORTS:"#66bb6a", CAUSED_BY:"#ffa726", PART_OF:"#42a5f5", TRUSTS:"#26c6da", DISTRUSTS:"#ef5350", REMEMBERS:"#ab47bc", HAS_PREFERENCE:"#26c6da", HAS_BOUNDARY:"#ef5350", HAS_AFFECT:"#ec407a", HAS_SKILL:"#66bb6a", REFERS_TO:"#7e57c2", SUMMARIZES:"#d4e157", PROMOTED_FROM:"#d4e157"},
  LAYER_COLOR: {working:"#ffb86c", short_term:"#79c0ff", long_term:"#7ee787", archive:"#d2a8ff"},
  ncol(t){return this.NCOLORS[t]||"#8b949e";}, ecol(t){return this.ECOLORS[t]||"#666";}, radius(n){return 5+(n.importance||0.5)*7;},
  hotScore(n, metric){
    const m = metric||this.heatMetric||'combined';
    const acc=n.access_count||0, imp=n.importance!=null?n.importance:0.5, trust=n.trust_level!=null?n.trust_level:0.5;
    const now=Date.now()/1000, ageH=Math.max(0,(now-(n.updated_at||n.created_at||now))/3600);
    if(m==='reads') return acc;
    if(m==='importance') return imp*10;
    if(m==='recency') return -ageH;
    return acc*1.8+imp*6+trust*2-ageH*0.05;
  },
  heatColor(score,min,max){
    const t=max===min?0.5:(score-min)/(max-min);
    if(t<0.33){const a=t/0.33; return `rgb(${Math.round(13+(66-13)*a)},${Math.round(17+(165-17)*a)},${Math.round(23+(245-23)*a)})`;} 
    else if(t<0.66){const a=(t-0.33)/0.33; return `rgb(${Math.round(66+(255-66)*a)},${Math.round(165+(184-165)*a)},${Math.round(245+(108-245)*a)})`;} 
    else {const a=(t-0.66)/0.34; return `rgb(${Math.round(255)},${Math.round(184+(82-184)*a)},${Math.round(108+(82-108)*a)})`;}
  },

  async render(el){
    el.innerHTML=`<div class="card"><h3>Graph — explore one database</h3>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <label>DB <select id="g-db"><option value="core">core.db</option><option value="agents">agents.db</option></select></label>
        <label>Nodes <input id="g-limit" type="number" value="${this.limit}" min="10" max="2000" step="50" style="width:80px"></label>
        <button class="btn" onclick="Tabs.graph.load()">Load</button>
        <span id="g-info" class="muted"></span>
        <span style="margin-left:auto;display:flex;gap:6px;flex-wrap:wrap">
          <select id="g-heat-metric" title="heat metric" style="padding:4px 6px;font-size:11px"><option value="combined">Heat: combined</option><option value="reads">Heat: reads</option><option value="importance">Heat: importance</option><option value="recency">Heat: recency</option></select>
          <button class="btn ghost" id="g-heat-btn" onclick="Tabs.graph.toggleHeat()">${this.heat?'🔥 Heat ON':'Heat'}</button>
          <button class="btn ghost" id="g-galaxy-btn" onclick="Tabs.graph.toggleGalaxy()">${this.galaxy?'🌌 Galaxy ON':'Galaxy'}</button>
          <button class="btn ghost" onclick="Tabs.graph.shareLink()">Share</button>
        </span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px">
        <div style="display:flex;gap:4px;align-items:center"><input id="g-q" placeholder="find label…" style="width:150px" value="${App.esc((this.f||{}).q||"")}"> <button class="btn ghost" style="padding:4px 6px" onclick="Tabs.graph.searchNext(1)">▸</button><button class="btn ghost" style="padding:4px 6px" onclick="Tabs.graph.searchNext(-1)">◂</button></div>
        <label title="show only matches"><input type="checkbox" id="g-isolate"> isolate</label>
        <label>Type <select id="g-type"><option value="">all</option></select></label>
        <label>Layer <select id="g-layer"><option value="">all</option><option>working</option><option>short_term</option><option>long_term</option><option>archive</option></select></label>
        <label title="hide weak links">Weight ≥ <span id="g-wv">0.00</span> <input type="range" id="g-wf" min="0" max="1" step="0.05" value="0" style="width:70px"></label>
        <label><input type="checkbox" id="g-labels" checked> labels</label>
        <label><input type="checkbox" id="g-edges" checked> links</label>
        <label title="nodes with no links"><input type="checkbox" id="g-orph" checked> orphans</label>
        <label title="layer orbits in galaxy"><input type="checkbox" id="g-orbits"> orbits</label>
        <label title="bundle edges"><input type="checkbox" id="g-bundle"> bundle</label>
        <label title="community hulls"><input type="checkbox" id="g-hulls" checked> hulls</label>
        <label title="transparent PNG"><input type="checkbox" id="g-trans"> trans PNG</label>
        <button class="btn ghost" id="g-focus-clear" style="display:none">✕ clear focus</button>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:6px">
        <label>Time fade <input type="range" id="g-time" min="0" max="90" step="5" value="${this.timeFade}" style="width:100px"><span id="g-time-v" class="muted">${this.timeFade?this.timeFade+'d':'off'}</span></label>
        <span class="muted">Link kinds:</span> <span id="g-etypes"></span>
      </div>
      <div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap">
        <button class="btn ghost" onclick="Tabs.graph.zoom(1.4)">+</button><button class="btn ghost" onclick="Tabs.graph.zoom(0.7)">−</button><button class="btn ghost" onclick="Tabs.graph.reset()">Reset</button><button class="btn ghost" onclick="Tabs.graph.fit()">Fit</button><button class="btn ghost" onclick="Tabs.graph.layout(true)">Re-layout</button><button class="btn ghost" onclick="Tabs.graph.exportPNG()">PNG</button><button class="btn ghost" onclick="Tabs.graph.exportJSON()">JSON</button><span class="muted" style="align-self:center">Shift+drag: lasso · Double-click: expand</span>
      </div>
      <div style="position:relative;margin-top:8px;display:grid;grid-template-columns:1fr 220px;gap:10px">
        <div style="position:relative">
          <canvas id="graphcanvas" style="cursor:grab;width:100%;height:520px;display:block"></canvas>
          <canvas id="g-minimap" width="120" height="80" style="position:absolute;top:8px;left:8px;border:1px solid #30363d;border-radius:6px;background:rgba(13,17,23,0.9);cursor:pointer"></canvas>
          <div id="g-lasso" style="position:absolute;border:1px dashed #79c0ff;background:rgba(121,192,255,0.12);display:none;pointer-events:none"></div>
          <div id="g-legend" style="position:absolute;bottom:10px;left:10px;background:rgba(22,27,34,.92);border:1px solid #30363d;border-radius:8px;padding:8px 12px;font-size:11px"></div>
          <div id="g-detail" style="position:absolute;top:10px;right:10px;width:300px;max-height:480px;overflow:auto;background:rgba(22,27,34,.96);border:1px solid #30363d;border-radius:8px;padding:10px 12px;font-size:12px;display:none"></div>
        </div>
        <div id="g-hot" style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px;max-height:520px;overflow:auto">
          <div style="font-weight:600;font-size:12px;margin-bottom:6px;display:flex;align-items:center;gap:6px">🔥 Hot areas <span class="muted" style="font-weight:400">most used</span><span id="g-hot-mode" style="margin-left:auto;font-size:10px;color:#8b949e">${this.heat?this.heatMetric:this.galaxy?'galaxy':''}</span></div>
          <div id="g-hot-list" class="muted" style="font-size:12px">Loading…</div>
          <div style="margin-top:8px" class="muted">Tip: click hot to focus · Shift+drag to lasso · double-click node to expand</div>
          <div id="g-selection" style="margin-top:8px;display:none"><div class="muted" style="font-size:11px"><span id="g-sel-count">0</span> selected</div><div style="display:flex;gap:4px;margin-top:4px"><button class="btn ghost" style="padding:3px 6px;font-size:11px" onclick="Tabs.graph.bulkRelate()">Relate</button><button class="btn ghost" style="padding:3px 6px;font-size:11px" onclick="Tabs.graph.clearSelection()">Clear</button></div></div>
        </div>
      </div>
      </div>`;
    document.getElementById("g-db").value=this.db;
    document.getElementById("g-db").onchange=e=>{this.db=e.target.value; this.focus=null; this.sel=null; this.load();};
    document.getElementById("g-heat-metric").value=this.heatMetric;
    document.getElementById("g-heat-metric").onchange=e=>{this.heatMetric=e.target.value; if(this.heat) this.draw(); this.buildHot();};
    document.getElementById("g-time").oninput=e=>{this.timeFade=parseInt(e.target.value); document.getElementById("g-time-v").textContent=this.timeFade?this.timeFade+'d':'off'; this.draw();};
    document.getElementById("g-orbits").onchange=e=>{this.showOrbits=e.target.checked; this.draw();};
    document.getElementById("g-bundle").onchange=e=>{this.bundled=e.target.checked; this.draw();};
    document.getElementById("g-hulls").onchange=e=>{this.showHulls=e.target.checked; this.draw();};
    document.getElementById("g-trans").onchange=e=>{this.transparent=e.target.checked;};
    await this.load();
  },
  toggleHeat(){ this.heat=!this.heat; document.getElementById("g-heat-btn").textContent=this.heat?'🔥 Heat ON':'Heat'; document.getElementById("g-hot-mode").textContent=this.heat?this.heatMetric:this.galaxy?'galaxy':''; this.buildHot(); this.draw(); },
  toggleGalaxy(){ this.galaxy=!this.galaxy; document.getElementById("g-galaxy-btn").textContent=this.galaxy?'🌌 Galaxy ON':'Galaxy'; document.getElementById("g-hot-mode").textContent=this.galaxy?'galaxy':this.heat?this.heatMetric:''; if(this.galaxy) this.layoutGalaxy(); this.draw(); },
  layoutGalaxy(){
    const cv=document.getElementById("graphcanvas"); const W=cv.clientWidth||800,H=520,cx=W/2,cy=H/2;
    const sorted=[...this.nodes].sort((a,b)=>this.hotScore(b)-this.hotScore(a));
    const arms=4, armSep=Math.PI*2/arms;
    sorted.forEach((n,i)=>{ if(i<5){const ang=Math.random()*Math.PI*2,r=Math.random()*60; n.x=cx+Math.cos(ang)*r; n.y=cy+Math.sin(ang)*r;} else {const li={working:0,short_term:1,long_term:2,archive:3}[n.layer||'working']??0; const arm=i%arms, t=(i/this.nodes.length)*4, r=80+li*40+t*120+Math.random()*30, ang=arm*armSep+t*0.9+(Math.random()-0.5)*0.3; n.x=cx+Math.cos(ang)*r; n.y=cy+Math.sin(ang)*r;} n.vx=0;n.vy=0; });
  },
  shareLink(){
    const p=new URLSearchParams({db:this.db,limit:this.limit});
    if(this.galaxy) p.set('galaxy','1');
    if(this.heat) p.set('heat','1'), p.set('heatMetric',this.heatMetric);
    if(this.timeFade) p.set('timeFade',this.timeFade);
    if(this.f && this.f.q) p.set('q',this.f.q);
    const url=location.origin+location.pathname+'#graph?'+p.toString();
    navigator.clipboard.writeText(url).then(()=>App.toast('Link copied — '+url.slice(0,60)+'...'));
    // also update hash for reload
    location.hash='graph?'+p.toString();
  },
  searchNext(dir){
    const q=(this.f.q||'').toLowerCase();
    if(!q){ App.toast('Type a query first'); return; }
    const matches=this.nodes.filter(n=>(n.label||'').toLowerCase().includes(q));
    if(!matches.length){ App.toast('No matches'); return; }
    this.searchHits=matches; 
    if(dir>0) this.searchIdx=(this.searchIdx+1)%matches.length; else this.searchIdx=(this.searchIdx-1+matches.length)%matches.length;
    const n=matches[this.searchIdx];
    this.sel=n.node_id; this.focus=n.node_id;
    const cv=document.getElementById("graphcanvas"); const W=cv.clientWidth||800,H=520;
    this.t.x=W/2-n.x*this.t.k; this.t.y=H/2-n.y*this.t.k;
    App.toast(`Match ${this.searchIdx+1}/${matches.length}: ${n.label}`);
    this.draw(); this.detail(n.node_id);
  },

  async load(){
    const lim=document.getElementById("g-limit");
    if(lim) this.limit=Math.max(10,Math.min(2000,parseInt(lim.value||"300",10)));
    const info=document.getElementById("g-info");
    if(info) info.textContent="Loading…";
    // parse hash for share
    try{
      const h=location.hash.replace('#','');
      if(h.startsWith('graph?')){
        const p=new URLSearchParams(h.slice(6));
        if(p.get('galaxy')) this.galaxy=p.get('galaxy')==='1';
        if(p.get('heat')) this.heat=p.get('heat')==='1';
        if(p.get('heatMetric')) this.heatMetric=p.get('heatMetric');
        if(p.get('timeFade')) this.timeFade=parseInt(p.get('timeFade'));
        if(p.get('q')) this.f={...(this.f||{}), q:p.get('q')};
      }
    }catch(e){}
    try{
      const d=await App.api(`/api/graph?db=${this.db}&limit=${this.limit}`);
      this.nodes=d.nodes||[]; this.edges=d.edges||[]; this.truncated=!!d.truncated;
      this.f={q:(this.f||{}).q||"", isolate:false, type:"", layer:"", hidden:{}, wfloor:0, orphans:true, labels:true, links:true};
      this.focus=null; this.sel=null; this.hover=null; this.t={x:0,y:0,k:1}; this.byId={};
      this.nodes.forEach(n=>{this.byId[n.node_id]=n;});
      this.adj=new Map(); this.deg=new Map();
      this.nodes.forEach(n=>{this.adj.set(n.node_id,new Set()); this.deg.set(n.node_id,0);});
      this.edges.forEach(e=>{ if(!this.byId[e.from_node]||!this.byId[e.to_node]) return; this.adj.get(e.from_node).add(e.to_node); this.adj.get(e.to_node).add(e.from_node); this.deg.set(e.from_node,(this.deg.get(e.from_node)||0)+1); this.deg.set(e.to_node,(this.deg.get(e.to_node)||0)+1);});
      this.hot=[...this.nodes].map(n=>({...n,_hot:this.hotScore(n)})).sort((a,b)=>b._hot-a._hot).slice(0,12);
      this.buildFilters(); this.buildLegend(); this.buildHot();
      if(info) info.textContent=`${this.nodes.length} notes, ${this.edges.length} links${this.truncated?" (capped — raise Nodes)":""}`;
      this.bindCanvas(); await this.relayout(); if(this.galaxy) this.layoutGalaxy(); this.fit(); this.drawMinimap();
    }catch(e){ if(info) info.textContent="Error: "+e.message; }
  },
  buildHot(){
    const el=document.getElementById("g-hot-list");
    if(!el) return;
    if(!this.hot.length){ el.innerHTML=`<span class="muted">No hot nodes yet — use memory.</span>`; return; }
    const max=Math.max(...this.hot.map(n=>n._hot)), min=Math.min(...this.hot.map(n=>n._hot));
    el.innerHTML=this.hot.map((n,i)=>{
      const pct=max===min?50:Math.round((n._hot-min)/(max-min)*100);
      const col=this.heat?this.heatColor(n._hot,min,max):this.ncol(n.node_type);
      const layerCol=this.LAYER_COLOR[n.layer]||"#8b949e";
      return `<div style="display:flex;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid #21262d;cursor:pointer" onclick="Tabs.graph.focusHot('${App.esc(n.node_id)}')">
        <div style="width:22px;height:22px;border-radius:50%;background:${col};display:flex;align-items:center;justify-content:center;color:#0d1117;font-size:10px;font-weight:700">${i+1}</div>
        <div style="flex:1;min-width:0"><div style="font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#e6edf3">${App.esc(n.label||n.node_id.slice(0,12))}</div><div class="muted" style="font-size:10px">${App.esc(n.node_type)} · <span style="color:${layerCol}">${App.esc(n.layer||'working')}</span> · ${n.access_count||0} reads</div></div>
        <div style="text-align:right"><div style="width:40px;height:6px;background:#21262d;border-radius:3px;overflow:hidden"><div style="width:${pct}%;height:6px;background:${col}"></div></div><div class="muted" style="font-size:10px">${n._hot.toFixed(1)}</div></div></div>`;
    }).join('');
  },
  focusHot(id){ const n=this.byId[id]; if(!n) return; this.sel=id; this.focus=id; const fc=document.getElementById("g-focus-clear"); if(fc){fc.style.display=""; fc.textContent="✕ focus: "+(n.label||"").slice(0,18);} const cv=document.getElementById("graphcanvas"),W=cv.clientWidth||800,H=520; this.t.x=W/2-n.x*this.t.k; this.t.y=H/2-n.y*this.t.k; this.draw(); this.detail(id); },

  buildFilters(){
    const ts=document.getElementById("g-type");
    if(ts){ const used=[...new Set(this.nodes.map(n=>n.node_type))].sort(); ts.innerHTML=`<option value="">all</option>`+used.map(t=>`<option>${App.esc(t)}</option>`).join(""); ts.onchange=()=>{this.f.type=ts.value; this.draw();}; }
    const ls=document.getElementById("g-layer");
    if(ls){ ls.value=""; ls.onchange=()=>{this.f.layer=ls.value; this.draw();}; }
    const q=document.getElementById("g-q");
    let timer=null;
    if(q) q.oninput=()=>{clearTimeout(timer); timer=setTimeout(()=>{this.f.q=q.value; this.searchHits=[]; this.searchIdx=-1; this.draw();},250);};
    const iso=document.getElementById("g-isolate");
    if(iso){ iso.checked=false; iso.onchange=()=>{this.f.isolate=iso.checked; this.draw();}; }
    const wf=document.getElementById("g-wf"), wv=document.getElementById("g-wv");
    if(wf) wf.oninput=()=>{this.f.wfloor=parseFloat(wf.value); if(wv) wv.textContent=this.f.wfloor.toFixed(2); this.draw();};
    const lb=document.getElementById("g-labels");
    if(lb){ lb.checked=true; lb.onchange=()=>{this.f.labels=lb.checked; this.draw();}; }
    const le=document.getElementById("g-edges");
    if(le){ le.checked=true; le.onchange=()=>{this.f.links=le.checked; this.draw();}; }
    const or=document.getElementById("g-orph");
    if(or){ or.checked=true; or.onchange=()=>{this.f.orphans=or.checked; this.draw();}; }
    const fc=document.getElementById("g-focus-clear");
    if(fc) fc.onclick=()=>{this.focus=null; fc.style.display="none"; this.draw();};
    const box=document.getElementById("g-etypes");
    if(box){ const types=[...new Set(this.edges.map(e=>e.edge_type))].sort(); box.innerHTML=types.map(t=>`<label style="margin-right:8px;font-size:11px"><input type="checkbox" data-et="${App.esc(t)}" checked> <span style="display:inline-block;width:10px;height:3px;background:${this.ecol(t)}"></span> ${App.esc(t)}</label>`).join("")||`<span class="muted">none</span>`; box.querySelectorAll("input").forEach(cb=>{cb.onchange=()=>{if(cb.checked) delete this.f.hidden[cb.dataset.et]; else this.f.hidden[cb.dataset.et]=1; this.draw();};}); }
  },
  buildLegend(){
    const el=document.getElementById("g-legend");
    if(!el) return;
    const used=[...new Set(this.nodes.map(n=>n.node_type))].sort();
    el.innerHTML=used.map(t=>`<div style="display:flex;align-items:center;gap:6px;margin:2px 0;color:#aaa"><span style="width:10px;height:10px;border-radius:3px;background:${this.ncol(t)}"></span>${App.esc(t)}</div>`).join("")+`<div style="margin-top:6px;border-top:1px solid #30363d;padding-top:6px" class="muted">Layers: <span style="color:#ffb86c">● working</span> <span style="color:#79c0ff">● short</span> <span style="color:#7ee787">● long</span> <span style="color:#d2a8ff">● archive</span></div>`;
  },
  match(n){ const q=(this.f.q||"").toLowerCase(); return !q||(n.label||"").toLowerCase().includes(q); },
  visible(){
    const f=this.f;
    const vn=this.nodes.filter(n=>{
      if(f.type&&n.node_type!==f.type) return false;
      if(f.layer&&(n.layer||"working")!==f.layer) return false;
      if(!f.orphans&&!(this.deg.get(n.node_id)||0)) return false;
      if(this.focus&&n.node_id!==this.focus&&!(this.adj.get(this.focus)||new Set()).has(n.node_id)) return false;
      if(f.isolate&&f.q&&!this.match(n)) return false;
      return true;
    });
    const keep=new Set(vn.map(n=>n.node_id));
    const vl=!f.links?[]:this.edges.filter(e=>keep.has(e.from_node)&&keep.has(e.to_node)&&!f.hidden[e.edge_type]&&(e.weight||0)>=f.wfloor);
    return {vn,vl};
  },
  async relayout(){
    const cv=document.getElementById("graphcanvas"); const W=cv.clientWidth||800,H=520,n=this.nodes.length;
    if(!n){this.draw(); return;}
    const R=Math.min(W,H)*0.38;
    this.nodes.forEach((nd,i)=>{const a=2*Math.PI*i/n; nd.x=W/2+R*Math.cos(a); nd.y=H/2+R*Math.sin(a); nd.vx=0; nd.vy=0;});
    const types=[...new Set(this.nodes.map(x=>x.node_type))];
    const anchors=new Map(types.map((t,i)=>{const a=(i/Math.max(types.length,1))*Math.PI*2-Math.PI/2; return [t,{x:W/2+Math.cos(a)*R*0.55,y:H/2+Math.sin(a)*R*0.55}];}));
    const T=n<=150?400:n<=400?250:120, rep=5200,spr=0.02,grav=0.012,anch=0.02;
    for(let t=0;t<T;t++){
      for(let i=0;i<n;i++){const a=this.nodes[i]; for(let j=i+1;j<n;j++){const b=this.nodes[j]; let dx=a.x-b.x,dy=a.y-b.y, d2=dx*dx+dy*dy; if(d2<1){dx=Math.random()-0.5; dy=Math.random()-0.5; d2=1;} const f=Math.min(rep/d2,4),d=Math.sqrt(d2); dx/=d; dy/=d; a.vx+=dx*f; a.vy+=dy*f; b.vx-=dx*f; b.vy-=dy*f;}}
      for(const e of this.edges){const a=this.byId[e.from_node],b=this.byId[e.to_node]; if(!a||!b) continue; const dx=b.x-a.x,dy=b.y-a.y; a.vx+=dx*spr; a.vy+=dy*spr; b.vx-=dx*spr; b.vy-=dy*spr;}
      for(const nd of this.nodes){ nd.vx+=(W/2-nd.x)*grav; nd.vy+=(H/2-nd.y)*grav; const an=anchors.get(nd.node_type); if(an){nd.vx+=(an.x-nd.x)*anch; nd.vy+=(an.y-nd.y)*anch;}}
      for(const nd of this.nodes){ nd.vx*=0.86; nd.vy*=0.86; nd.x+=Math.max(-8,Math.min(8,nd.vx)); nd.y+=Math.max(-8,Math.min(8,nd.vy));}
      if(t%60===0) await new Promise(r=>setTimeout(r,0));
    }
    this.draw();
  },
  layout(){ this.relayout(); },
  w2s(x,y){return [x*this.t.k+this.t.x, y*this.t.k+this.t.y];},
  drawMinimap(){
    const cv=document.getElementById("g-minimap");
    if(!cv||!this.nodes.length) return;
    const ctx=cv.getContext("2d"), W=cv.width, H=cv.height;
    ctx.clearRect(0,0,W,H);
    ctx.fillStyle="#010409"; ctx.fillRect(0,0,W,H);
    // bounds
    let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
    this.nodes.forEach(n=>{ if(n.x<x0) x0=n.x; if(n.x>x1) x1=n.x; if(n.y<y0) y0=n.y; if(n.y>y1) y1=n.y; });
    const pad=10, sx=(W-pad*2)/Math.max(x1-x0,1), sy=(H-pad*2)/Math.max(y1-y0,1), s=Math.min(sx,sy);
    const ox=pad - x0*s, oy=pad - y0*s;
    // edges
    ctx.strokeStyle="rgba(121,192,255,0.15)"; ctx.lineWidth=0.5;
    this.edges.forEach(e=>{const a=this.byId[e.from_node],b=this.byId[e.to_node]; if(!a||!b) return; ctx.beginPath(); ctx.moveTo(a.x*s+ox,a.y*s+oy); ctx.lineTo(b.x*s+ox,b.y*s+oy); ctx.stroke();});
    // nodes
    this.nodes.forEach(n=>{
      const x=n.x*s+ox, y=n.y*s+oy;
      ctx.fillStyle=this.ncol(n.node_type);
      ctx.beginPath(); ctx.arc(x,y,1.8,0,Math.PI*2); ctx.fill();
    });
    // viewport rect
    const main=document.getElementById("graphcanvas");
    const mw=main.clientWidth||800, mh=520;
    const vx=-this.t.x/this.t.k, vy=-this.t.y/this.t.k, vw=mw/this.t.k, vh=mh/this.t.k;
    ctx.strokeStyle="#79c0ff"; ctx.lineWidth=1; ctx.setLineDash([3,3]);
    ctx.strokeRect(vx*s+ox, vy*s+oy, vw*s, vh*s);
    ctx.setLineDash([]);
  },
  draw(){
    const cv=document.getElementById("graphcanvas");
    if(!cv||!this.nodes) return;
    const W=cv.clientWidth||800,H=520;
    cv.width=W; cv.height=H;
    const ctx=cv.getContext("2d");
    if(this.transparent){ ctx.clearRect(0,0,W,H); }
    else if(this.galaxy){
      const g=ctx.createRadialGradient(W/2,H/2,0,W/2,H/2,Math.max(W,H));
      g.addColorStop(0,"#0a0f1e"); g.addColorStop(0.5,"#0d1117"); g.addColorStop(1,"#010409");
      ctx.fillStyle=g; ctx.fillRect(0,0,W,H);
      ctx.fillStyle="rgba(255,255,255,0.5)";
      for(let i=0;i<70;i++){ const x=(i*137.5)%W, y=(i*73.7)%H; ctx.globalAlpha=0.15+(i%3)*0.1; ctx.fillRect(x,y,1,1);}
      ctx.globalAlpha=1;
    } else { ctx.clearRect(0,0,W,H); }
    // layer orbits
    if(this.showOrbits && this.galaxy){
      const cx=W/2, cy=H/2;
      [0,1,2,3].forEach(li=>{
        const r=80+li*40+120; // approx
        ctx.strokeStyle="rgba(255,255,255,0.06)"; ctx.setLineDash([4,6]); ctx.beginPath(); ctx.arc(cx,cy, r,0,Math.PI*2); ctx.stroke(); ctx.setLineDash([]);
      });
    }
    const {vn,vl}=this.visible();
    const q=(this.f.q||"").toLowerCase();
    const dim=n=> q&&!this.f.isolate&&!this.match(n);
    // community hulls
    if(this.showHulls && vn.length){
      const seen=new Set(), comps=[];
      vn.forEach(n=>{
        if(seen.has(n.node_id)) return;
        const q2=[n.node_id], comp=[];
        seen.add(n.node_id);
        while(q2.length){
          const id=q2.pop(); comp.push(this.byId[id]);
          const neigh=[...(this.adj.get(id)||[])].filter(x=>vn.find(v=>v.node_id===x));
          neigh.forEach(nb=>{ if(!seen.has(nb)){seen.add(nb); q2.push(nb);}});
        }
        if(comp.length>2) comps.push(comp);
      });
      comps.forEach((comp,i)=>{
        if(comp.length<3) return;
        // hull via simple centroid + angle sort
        const cx2=comp.reduce((s,n)=>s+n.x,0)/comp.length, cy2=comp.reduce((s,n)=>s+n.y,0)/comp.length;
        comp.sort((a,b)=> Math.atan2(a.y-cy2,a.x-cx2)-Math.atan2(b.y-cy2,b.x-cx2));
        ctx.fillStyle=`hsla(${(i*67)%360},70%,50%,0.08)`;
        ctx.strokeStyle=`hsla(${(i*67)%360},70%,50%,0.18)`;
        ctx.lineWidth=1; ctx.beginPath();
        comp.forEach((n,idx)=>{
          const [x,y]=this.w2s(n.x,n.y);
          if(idx===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
        });
        ctx.closePath(); ctx.fill(); ctx.stroke();
      });
    }
    // heat overlay
    if(this.heat && vn.length){
      const scores=vn.map(n=>this.hotScore(n));
      const min=Math.min(...scores), max=Math.max(...scores);
      vn.forEach(n=>{
        const s=this.hotScore(n), t=max===min?0.5:(s-min)/(max-min);
        if(t<0.4) return;
        const [x,y]=this.w2s(n.x,n.y);
        const r=(this.radius(n)+12)*(0.8+t*0.6);
        const grd=ctx.createRadialGradient(x,y,0,x,y,r*2.5);
        const col=this.heatColor(s,min,max);
        grd.addColorStop(0,col.replace('rgb','rgba').replace(')',',0.28)'));
        grd.addColorStop(1,'rgba(0,0,0,0)');
        ctx.fillStyle=grd; ctx.beginPath(); ctx.arc(x,y,r*2.5,0,Math.PI*2); ctx.fill();
      });
    }
    // time fade alpha
    const now=Date.now()/1000;
    const timeAlpha=n=>{
      if(!this.timeFade) return 1;
      const ageD=(now-(n.updated_at||n.created_at||now))/86400;
      if(ageD>this.timeFade) return 0.12;
      return 1 - (ageD/this.timeFade)*0.65;
    };
    const hotSet=new Set(this.hot.map(n=>n.node_id));
    // links
    for(const e of vl){
      const a=this.byId[e.from_node], b=this.byId[e.to_node];
      if(!a||!b) continue;
      const [x1,y1]=this.w2s(a.x,a.y),[x2,y2]=this.w2s(b.x,b.y);
      const hot=this.sel&&(e.from_node===this.sel||e.to_node===this.sel);
      let col=e.edge_type==="CONTRADICTS"?"#f85149":this.ecol(e.edge_type);
      if(this.galaxy) col=e.edge_type==="CONTRADICTS"?"rgba(248,81,73,0.9)":"rgba(121,192,255,0.18)";
      ctx.strokeStyle=col;
      ctx.lineWidth=(hot?2.5:1+Math.abs(e.weight||0)*1.6)*Math.min(this.t.k,1.4);
      if(this.galaxy) ctx.lineWidth*=0.7;
      let alpha=(dim(a)||dim(b))?0.08:(this.sel&&!hot?0.12:(this.galaxy?0.22:0.55));
      alpha *= Math.min(timeAlpha(a), timeAlpha(b));
      ctx.globalAlpha=alpha;
      if(this.bundled){
        const mx=(x1+x2)/2, my=(y1+y2)/2 - 14*Math.min(this.t.k,1);
        ctx.beginPath(); ctx.moveTo(x1,y1); ctx.quadraticCurveTo(mx,my,x2,y2); ctx.stroke();
      } else { ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke(); }
    }
    ctx.globalAlpha=1;
    // nodes
    const labelCache=[];
    for(const nd of vn){
      const [x,y]=this.w2s(nd.x,nd.y);
      if(x<-30||y<-30||x>W+30||y>H+30) continue;
      const baseR=this.radius(nd)*Math.min(this.t.k,1.6);
      let r=baseR;
      if(this.heat){
        const scores=vn.map(n=>this.hotScore(n));
        const min=Math.min(...scores), max=Math.max(...scores);
        const s=this.hotScore(nd), t=max===min?0.5:(s-min)/(max-min);
        r=baseR*(0.9+t*0.9);
      }
      if(this.galaxy) r=baseR*0.9+(hotSet.has(nd.node_id)?2:0);
      // centrality ring thickness
      const deg=this.deg.get(nd.node_id)||0;
      const isSel=this.sel===nd.node_id, isHov=this.hover===nd.node_id;
      const isHit=q&&this.match(nd);
      const isHot=hotSet.has(nd.node_id);
      const isSelectedLasso=this.lasso.sel.has(nd.node_id);
      if(isSelectedLasso){
        ctx.strokeStyle="#58a6ff"; ctx.lineWidth=2.5; ctx.setLineDash([4,3]);
        ctx.beginPath(); ctx.arc(x,y,r+5,0,2*Math.PI); ctx.stroke(); ctx.setLineDash([]);
      }
      if(!(this.deg.get(nd.node_id)||0)){
        ctx.strokeStyle=this.galaxy?"rgba(227,179,65,0.6)":"#e3b341"; ctx.lineWidth=1.5;
        ctx.beginPath(); ctx.arc(x,y,r+3,0,2*Math.PI); ctx.stroke();
      }
      if(isHit){
        ctx.strokeStyle="#ffd54f"; ctx.lineWidth=2.5;
        ctx.beginPath(); ctx.arc(x,y,r+2,0,2*Math.PI); ctx.stroke();
      }
      if(this.galaxy&&isHot){
        ctx.shadowColor=this.LAYER_COLOR[nd.layer]||"#79c0ff";
        ctx.shadowBlur=12+(nd._hot||0)*1.2;
      }
      let fill=this.ncol(nd.node_type);
      if(this.heat){
        const scores=vn.map(n=>this.hotScore(n));
        const min=Math.min(...scores), max=Math.max(...scores);
        fill=this.heatColor(this.hotScore(nd),min,max);
      } else if(this.galaxy){
        fill=this.LAYER_COLOR[nd.layer]||fill;
        if(isHot) fill="#e6edf3";
      }
      const alpha=timeAlpha(nd)*(dim(nd)?0.15:1);
      ctx.globalAlpha=alpha;
      ctx.fillStyle=fill;
      ctx.beginPath(); ctx.arc(x,y,r,0,2*Math.PI); ctx.fill();
      ctx.shadowBlur=0;
      ctx.globalAlpha=1;
      if(this.galaxy&&isHot){
        ctx.fillStyle="rgba(255,255,255,0.9)";
        ctx.beginPath(); ctx.arc(x,y,r*0.45,0,2*Math.PI); ctx.fill();
      }
      // centrality ring (degree)
      if(deg>4){
        ctx.strokeStyle="rgba(255,255,255,0.18)"; ctx.lineWidth=Math.min(3, 1+deg*0.18);
        ctx.beginPath(); ctx.arc(x,y,r+2.5,0,2*Math.PI); ctx.stroke();
      }
      if(isSel||isHov){
        ctx.strokeStyle="#fff"; ctx.lineWidth=2;
        ctx.beginPath(); ctx.arc(x,y,r+2,0,2*Math.PI); ctx.stroke();
      }
      if(this.f.labels && (this.t.k>0.45||isSel||isHov||isHit)){
        // label collision check
        const label=(nd.label||nd.node_id).slice(0,22);
        const tw=ctx.measureText?ctx.measureText(label).width: label.length*6;
        const lx=x+r+3, ly=y+3;
        let overlap=false;
        for(const c of labelCache){ if(Math.abs(c.x-lx)<tw+8 && Math.abs(c.y-ly)<12){ overlap=true; break; } }
        if(!overlap){
          labelCache.push({x:lx,y:ly});
          ctx.fillStyle=this.galaxy?"rgba(201,209,217,0.9)":"#c9d1d9"; ctx.font=this.galaxy?"10px system-ui":"10px sans-serif";
          if(this.galaxy){ ctx.shadowColor="rgba(0,0,0,0.8)"; ctx.shadowBlur=3; }
          ctx.fillText(label, lx, ly);
          ctx.shadowBlur=0;
        }
      }
    }
    // lasso rect
    if(this.lasso.active){
      const rx=Math.min(this.lasso.x0,this.lasso.x1), ry=Math.min(this.lasso.y0,this.lasso.y1), rw=Math.abs(this.lasso.x1-this.lasso.x0), rh=Math.abs(this.lasso.y1-this.lasso.y0);
      ctx.strokeStyle="#79c0ff"; ctx.setLineDash([4,4]); ctx.strokeRect(rx,ry,rw,rh); ctx.fillStyle="rgba(121,192,255,0.08)"; ctx.fillRect(rx,ry,rw,rh); ctx.setLineDash([]);
    }
    this.drawMinimap();
  },
  hit(mx,my){
    const cv=document.getElementById("graphcanvas");
    const rect=cv.getBoundingClientRect();
    const px=mx-rect.left, py=my-rect.top;
    const {vn}=this.visible();
    let best=null,bd=1e9;
    for(const nd of vn){
      const [x,y]=this.w2s(nd.x,nd.y);
      const d=(x-px)*(x-px)+(y-py)*(y-py);
      const rr=this.radius(nd)*Math.min(this.t.k,1.6)+5;
      if(d<rr*rr&&d<bd){bd=d; best=nd;}
    }
    return best;
  },
  bindCanvas(){
    const cv=document.getElementById("graphcanvas");
    if(!cv||cv.dataset.bound) return;
    cv.dataset.bound="1";
    let drag=null, moved=false, lx=0, ly=0, lassoStart=null;
    const mm=document.getElementById("g-minimap");
    if(mm){
      mm.onclick=e=>{
        const rect=mm.getBoundingClientRect();
        const x=(e.clientX-rect.left)/mm.width, y=(e.clientY-rect.top)/mm.height;
        // center main on minimap click
        let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
        this.nodes.forEach(n=>{ if(n.x<x0) x0=n.x; if(n.x>x1) x1=n.x; if(n.y<y0) y0=n.y; if(n.y>y1) y1=n.y; });
        const cx=x0+(x1-x0)*x, cy=y0+(y1-y0)*y;
        const W=cv.clientWidth||800,H=520;
        this.t.x=W/2-cx*this.t.k; this.t.y=H/2-cy*this.t.k; this.draw();
      };
    }
    cv.onmousedown=e=>{
      if(e.shiftKey){
        const rect=cv.getBoundingClientRect();
        this.lasso.active=true; this.lasso.x0=e.clientX-rect.left; this.lasso.y0=e.clientY-rect.top; this.lasso.x1=this.lasso.x0; this.lasso.y1=this.lasso.y0;
        const l=document.getElementById("g-lasso"); if(l){l.style.display="block"; l.style.left=this.lasso.x0+"px"; l.style.top=this.lasso.y0+"px"; l.style.width="0px"; l.style.height="0px";}
        return;
      }
      const n=this.hit(e.clientX,e.clientY);
      drag=n||"pan"; moved=false; lx=e.clientX; ly=e.clientY;
      cv.style.cursor=n?"grabbing":"move";
    };
    cv.onmousemove=e=>{
      if(this.lasso.active){
        const rect=cv.getBoundingClientRect();
        this.lasso.x1=e.clientX-rect.left; this.lasso.y1=e.clientY-rect.top;
        const l=document.getElementById("g-lasso");
        if(l){ const rx=Math.min(this.lasso.x0,this.lasso.x1), ry=Math.min(this.lasso.y0,this.lasso.y1); l.style.left=rx+"px"; l.style.top=ry+"px"; l.style.width=Math.abs(this.lasso.x1-this.lasso.x0)+"px"; l.style.height=Math.abs(this.lasso.y1-this.lasso.y0)+"px"; }
        return;
      }
      if(drag&&drag!=="pan"){
        const rect=cv.getBoundingClientRect();
        drag.x=(e.clientX-rect.left-this.t.x)/this.t.k;
        drag.y=(e.clientY-rect.top-this.t.y)/this.t.k;
        moved=true; this.draw();
      } else if(drag==="pan"){
        this.t.x+=e.clientX-lx; this.t.y+=e.clientY-ly;
        lx=e.clientX; ly=e.clientY; moved=true; this.draw();
      } else {
        const n=this.hit(e.clientX,e.clientY);
        const id=n?n.node_id:null;
        if(id!==this.hover){ this.hover=id; cv.style.cursor=n?"pointer":"grab"; this.draw(); }
      }
    };
    cv.onmouseup=e=>{
      if(this.lasso.active){
        this.lasso.active=false;
        const l=document.getElementById("g-lasso"); if(l) l.style.display="none";
        // select nodes inside lasso (screen coords)
        const rx=Math.min(this.lasso.x0,this.lasso.x1), ry=Math.min(this.lasso.y0,this.lasso.y1), rw=Math.abs(this.lasso.x1-this.lasso.x0), rh=Math.abs(this.lasso.y1-this.lasso.y0);
        if(rw>5&&rh>5){
          const {vn}=this.visible();
          this.lasso.sel.clear();
          vn.forEach(n=>{
            const [x,y]=this.w2s(n.x,n.y);
            if(x>=rx&&x<=rx+rw&&y>=ry&&y<=ry+rh) this.lasso.sel.add(n.node_id);
          });
          this.updateSelection();
          App.toast(this.lasso.sel.size+" selected — use Relate/Delete in hot panel");
        }
        return;
      }
      const wasDrag=drag;
      drag=null; cv.style.cursor="grab";
      if(!moved&&wasDrag&&wasDrag!=="pan") this.detail(wasDrag.node_id);
      else if(!moved) this.clearDetail();
    };
    cv.onmouseleave=()=>{ drag=null; this.hover=null; if(this.lasso.active){this.lasso.active=false; const l=document.getElementById("g-lasso"); if(l) l.style.display="none";} this.draw(); };
    cv.ondblclick=e=>{
      const n=this.hit(e.clientX,e.clientY);
      if(n){
        // expand: fetch its neighbors that are not yet in graph
        this.expandNode(n.node_id);
        return;
      }
      const fc=document.getElementById("g-focus-clear");
      if(n && this.focus!==n.node_id){ this.focus=n.node_id; if(fc){fc.style.display=""; fc.textContent="✕ focus: "+(n.label||"").slice(0,18);} }
      else { this.focus=null; if(fc) fc.style.display="none"; }
      this.draw();
    };
    cv.onwheel=e=>{
      e.preventDefault();
      const rect=cv.getBoundingClientRect();
      const px=e.clientX-rect.left, py=e.clientY-rect.top;
      const k2=Math.max(0.1,Math.min(6,this.t.k*(e.deltaY<0?1.15:0.87)));
      this.t.x=px-(px-this.t.x)*(k2/this.t.k);
      this.t.y=py-(py-this.t.y)*(k2/this.t.k);
      this.t.k=k2; this.draw();
    };
  },
  clearSelection(){ this.lasso.sel.clear(); this.updateSelection(); this.draw(); },
  updateSelection(){
    const cnt=this.lasso.sel.size;
    const el=document.getElementById("g-selection");
    const c=document.getElementById("g-sel-count");
    if(el){ el.style.display=cnt?"block":"none"; }
    if(c) c.textContent=cnt;
  },
  async bulkRelate(){
    const ids=[...this.lasso.sel];
    if(ids.length<2){ App.toast("Select at least 2 nodes (Shift+drag)"); return; }
    const type=prompt("Edge type for "+ids.length+" nodes (chain):","RELATES_TO");
    if(!type) return;
    let ok=0;
    for(let i=0;i<ids.length-1;i++){
      try{
        const payload={db:this.db, from_node:ids[i], to_node:ids[i+1], edge_type:type.toUpperCase(), weight:1.0};
        if(this.db==="agents"){
          const cur=await App.api(`/api/nodes?db=${this.db}&q=${encodeURIComponent(ids[i])}&limit=1`);
          const ag=(cur.nodes||[])[0]?._agent||this.nodes.find(n=>n.node_id===ids[i])?._agent||"";
          if(ag) payload.agent_id=ag;
        }
        await App.api("/api/edge_add",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
        ok++;
      }catch(e){}
    }
    App.toast(`Linked ${ok} edges`); this.clearSelection(); this.load();
  },
  async expandNode(id){
    try{
      const e=await App.api(`/api/edges?db=${this.db}&node=${encodeURIComponent(id)}&limit=50`);
      const neigh=[...new Set((e.edges||[]).flatMap(x=>[x.from_node,x.to_node]))].filter(x=>x!==id && !this.byId[x]);
      if(!neigh.length){ App.toast("No new neighbors"); return; }
      // fetch each neighbor's full node
      for(const nid of neigh.slice(0,12)){
        try{
          const d=await App.api(`/api/nodes?db=${this.db}&q=${encodeURIComponent(nid)}&limit=1`);
          const n=(d.nodes||[])[0];
          if(n && !this.byId[n.node_id]){
            n.x=(this.byId[id].x||0)+(Math.random()-0.5)*80;
            n.y=(this.byId[id].y||0)+(Math.random()-0.5)*80;
            n.vx=0;n.vy=0;
            this.nodes.push(n); this.byId[n.node_id]=n;
            this.adj.set(n.node_id,new Set()); this.deg.set(n.node_id,0);
          }
          // add edge we already know? will be added on next load, but push locally
          const ed=(e.edges||[]).find(x=>x.from_node===nid||x.to_node===nid);
          if(ed && !this.edges.find(y=>y.edge_id===ed.edge_id)) this.edges.push(ed);
        }catch(e){}
      }
      // recompute adj/deg for new nodes
      this.adj=new Map(); this.deg=new Map();
      this.nodes.forEach(n=>{this.adj.set(n.node_id,new Set()); this.deg.set(n.node_id,0);});
      this.edges.forEach(ed=>{
        if(!this.byId[ed.from_node]||!this.byId[ed.to_node]) return;
        this.adj.get(ed.from_node).add(ed.to_node);
        this.adj.get(ed.to_node).add(ed.from_node);
        this.deg.set(ed.from_node,(this.deg.get(ed.from_node)||0)+1);
        this.deg.set(ed.to_node,(this.deg.get(ed.to_node)||0)+1);
      });
      this.hot=[...this.nodes].map(n=>({...n,_hot:this.hotScore(n)})).sort((a,b)=>b._hot-a._hot).slice(0,12);
      this.buildHot(); this.buildLegend(); this.draw(); this.drawMinimap();
      App.toast(`Expanded ${neigh.length} neighbors`);
    }catch(e){ App.toast(e.message); }
  },
  zoom(f){ const cv=document.getElementById("graphcanvas"); const W=cv.clientWidth||800,H=520; const k2=Math.max(0.1,Math.min(6,this.t.k*f)); this.t.x=W/2-(W/2-this.t.x)*(k2/this.t.k); this.t.y=H/2-(H/2-this.t.y)*(k2/this.t.k); this.t.k=k2; this.draw(); },
  reset(){ this.t={x:0,y:0,k:1}; this.draw(); },
  fit(){ const cv=document.getElementById("graphcanvas"); const W=cv.clientWidth||800,H=520; const {vn}=this.visible(); if(!vn.length) return; let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9; for(const n of vn){ if(n.x<x0) x0=n.x; if(n.x>x1) x1=n.x; if(n.y<y0) y0=n.y; if(n.y>y1) y1=n.y; } const k=Math.max(0.1,Math.min(3,Math.min((W-80)/Math.max(x1-x0,1),(H-80)/Math.max(y1-y0,1)))); this.t.k=k; this.t.x=W/2-k*(x0+x1)/2; this.t.y=H/2-k*(y0+y1)/2; this.draw(); },
  async detail(id){
    this.sel=id;
    const box=document.getElementById("g-detail");
    this.draw();
    if(!box) return;
    box.style.display="block";
    box.innerHTML=`<span class="muted">Loading…</span>`;
    let full=null;
    try{ const d=await App.api(`/api/nodes?db=${this.db}&q=${encodeURIComponent(id)}&limit=1`); full=(d.nodes||[])[0]||null; }catch(e){}
    const n=this.byId[id]||{};
    const nbs=[...(this.adj.get(id)||[])].map(x=>this.byId[x]).filter(Boolean);
    const nbRows=nbs.slice(0,20).map(x=>`<div><a href="#" onclick="Tabs.graph.detail('${App.esc(x.node_id)}');return false">${App.esc(x.label||x.node_id)}</a> <span class="muted">${App.esc(x.node_type)}</span></div>`).join("");
    const layer=(full&&full.layer)||"";
    box.innerHTML=`<div style="display:flex;align-items:center;gap:6px"><b>Note</b><button class="btn ghost" style="margin-left:auto" onclick="Tabs.graph.clearDetail()">✕</button></div>${App.nodeDetail(Object.assign({},n,full||{},{layer:layer||n.layer}))}<h4 style="margin:8px 0 4px">Neighbors (${nbs.length})</h4>${nbRows||`<span class="muted">Isolated — no links.</span>`}<div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap"><button class="btn ghost" onclick="Tabs.graph.openManager('${App.esc(id)}')">Manager</button><button class="btn danger" onclick="Tabs.graph.del('${App.esc(id)}')">Delete</button></div>`;
  },
  clearDetail(){ this.sel=null; const box=document.getElementById("g-detail"); if(box) box.style.display="none"; this.draw(); },
  openManager(id){ const n=this.byId[id]||{}; Tabs.manager.db=this.db; Tabs.manager.q=n.label||id; Tabs.manager.offset=0; App.show("manager"); },
  async del(id){ if(!confirm("Delete node "+id+" (links go with it)?")) return; try{ await App.api("/api/node_delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({db:this.db,node_id:id})}); this.clearDetail(); App.refresh(true); this.load(); }catch(e){ App.toast(e.message); } },
  exportPNG(){
    const cv=document.getElementById("graphcanvas");
    const a=document.createElement("a");
    const scale=this.transparent?2:2;
    // if transparent, we need to redraw without bg
    const wasTransparent=this.transparent;
    const wasGalaxy=this.galaxy;
    a.download=`asha_${this.galaxy?'galaxy':'graph'}_${this.db}_${this.visible().vn.length}n_${Date.now()}.png`;
    // temporarily draw transparent if requested
    if(this.transparent){
      // draw once with transparent bg
      const prevTrans=this.transparent; this.transparent=true; // keep galaxy bg? transparent overrides galaxy bg
      // we will just use current canvas; transparent mode already clears bg, so capture
    }
    a.href=cv.toDataURL("image/png");
    a.click();
    App.toast("PNG saved — "+a.download);
  },
  exportJSON(){
    const {vn,vl}=this.visible();
    const data={db:this.db, exported_at:new Date().toISOString(), nodes:vn, edges:vl.map(e=>({edge_id:e.edge_id,from_node:e.from_node,to_node:e.to_node,edge_type:e.edge_type,weight:e.weight}))};
    const a=document.createElement("a");
    a.href=URL.createObjectURL(new Blob([JSON.stringify(data,null,1)],{type:"application/json"}));
    a.download=`asha_graph_${this.db}_${vn.length}n_${vl.length}e.json`;
    a.click(); setTimeout(()=>URL.revokeObjectURL(a.href),2000);
  },
};
