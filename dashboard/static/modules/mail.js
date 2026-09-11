// Mail tab — human operator console, Manager-grade.
// Inbox (user) + New Message modal + All traffic (5 newest + mini graphs) + Log drawer.
"use strict";
window.Tabs.mail = {
  timer: null,
  backoff: 15000,
  agents: [],
  inbox: [],
  traffic: [],
  async render(el) {
    el.innerHTML = `<div class="card">Loading mail…</div>`;
    clearTimeout(this.timer);
    try {
      const [agents, inbox] = await Promise.all([
        App.api("/api/agents"),
        App.api("/api/mailbox?scope=user&state=pending&limit=50"),
      ]);
      this.agents = agents.agents || [];
      this.inbox = inbox.messages || [];
      const opts = [`<option value="core">core</option>`].concat(
        this.agents.map(a => `<option value="agent:${App.esc(a.agent_id)}">agent:${App.esc(a.agent_id)} — ${App.esc(a.slug || "")}</option>`)).join("");
      // inbox rows - clickable row opens message
      const irows = this.inbox.map(m => `<tr style="cursor:pointer" onclick="Tabs.mail.open('${App.esc(m.msg_id)}')">
        <td><span class="tag ${m.from_scope === "core" ? "blue" : "amber"}">${App.esc(m.from_scope)}</span></td>
        <td><b>${App.esc((m.body || "").slice(0, 80))}</b><br><span class="muted" style="font-size:11px">${App.esc((m.body || "").slice(80, 160))}</span></td>
        <td class="muted" title="${App.esc(new Date(m.created_at*1000).toLocaleString())}">${App.esc(App.ago(m.created_at))}</td>
        <td onclick="event.stopPropagation()"><button class="btn ghost" style="padding:3px 8px;font-size:11px" onclick="Tabs.mail.reply('${App.esc(m.msg_id)}')">Reply</button> <button class="btn ghost" style="padding:3px 8px;font-size:11px" onclick="Tabs.mail.ack('${App.esc(m.msg_id)}')">Ack</button> <button class="btn danger" style="padding:3px 8px;font-size:11px" onclick="Tabs.mail.del('${App.esc(m.msg_id)}')">Del</button></td></tr>`).join("");
      el.innerHTML = `<div class="card" style="border-left:3px solid #79c0ff">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><h3 style="margin:0">Inbox — for you (user)</h3>
          <span class="tag ${this.inbox.length ? "amber" : "grey"}"><b>${this.inbox.length}</b> pending</span>
          <button class="btn" style="margin-left:auto" onclick="Tabs.mail.newMsg()">+ New message</button>
          <button class="btn ghost" onclick="if(confirm('Wipe all mail?')) Tabs.mail.wipe()">Wipe all</button></div>
        <table style="margin-top:8px"><tr><th>From</th><th>Message</th><th>When</th><th></th></tr>
        <tbody id="mail-inbox-body">${irows || `<tr><td colspan=4 class="muted" style="text-align:center;padding:16px">No pending messages — all caught up.</td></tr>`}</tbody></table></div>
        <div class="card">
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><h3 style="margin:0">All traffic</h3>
            <span class="muted">newest 5</span>
            <button class="btn ghost" style="margin-left:auto" onclick="Tabs.mail.openLog()">Log — all traffic</button></div>
          <div id="mail-traffic-5" class="muted">Loading…</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:10px">
            <div><canvas id="mail-by-sender" width="360" height="140" style="max-width:100%"></canvas></div>
            <div><canvas id="mail-by-state" width="180" height="140" style="max-width:100%"></canvas><div id="mail-stats" class="muted" style="margin-top:6px;font-size:11px"></div></div>
          </div>
        </div>`;
      this.pollTraffic5();
      this.drawGraphsSoon();
      clearTimeout(this.timer);
      this.timer = setTimeout(() => {
        if (App.tab !== "mail") return;
        this.refreshInbox();
        this.pollTraffic5();
        this.drawGraphsSoon();
        clearTimeout(this.timer);
        this.timer = setTimeout(() => { if (App.tab === "mail") this.render(document.getElementById("panel")); }, this.backoff);
      }, this.backoff);
    } catch (e) { el.innerHTML = `<div class="card">Error: ${App.esc(e.message)}</div>`; }
  },

  async refreshInbox() {
    try {
      const inbox = await App.api("/api/mailbox?scope=user&state=pending&limit=50");
      this.inbox = inbox.messages || [];
      const irows = this.inbox.map(m => `<tr style="cursor:pointer" onclick="Tabs.mail.open('${App.esc(m.msg_id)}')">
        <td><span class="tag ${m.from_scope === "core" ? "blue" : "amber"}">${App.esc(m.from_scope)}</span></td>
        <td><b>${App.esc((m.body || "").slice(0, 80))}</b><br><span class="muted" style="font-size:11px">${App.esc((m.body || "").slice(80, 160))}</span></td>
        <td class="muted" title="${App.esc(new Date(m.created_at*1000).toLocaleString())}">${App.esc(App.ago(m.created_at))}</td>
        <td onclick="event.stopPropagation()"><button class="btn ghost" style="padding:3px 8px;font-size:11px" onclick="Tabs.mail.reply('${App.esc(m.msg_id)}')">Reply</button> <button class="btn ghost" style="padding:3px 8px;font-size:11px" onclick="Tabs.mail.ack('${App.esc(m.msg_id)}')">Ack</button> <button class="btn danger" style="padding:3px 8px;font-size:11px" onclick="Tabs.mail.del('${App.esc(m.msg_id)}')">Del</button></td></tr>`).join("");
      const tb = document.getElementById("mail-inbox-body");
      if (tb) tb.innerHTML = irows || `<tr><td colspan=4 class="muted" style="text-align:center;padding:16px">No pending messages — all caught up.</td></tr>`;
    } catch (e) {}
  },

  async pollTraffic5() {
    try {
      const d = await App.api("/api/mailbox?scope=all&state=all&limit=50");
      this.traffic = d.messages || [];
      const five = this.traffic.slice(0,5);
      const rows = five.map(m => `<tr><td><span class="tag ${m.from_scope === "core" ? "blue" : "amber"}">${App.esc(m.from_scope)}</span></td>
        <td><span class="tag grey">${App.esc(m.receipt_scope || m.to_scope)}</span></td>
        <td>${App.esc((m.body || "").slice(0, 60))}</td>
        <td class="muted">${App.esc(App.ago(m.created_at))}</td>
        <td>${m.acked_at ? `<span class="tag green">acked</span>` : m.noted_at ? `<span class="tag amber">noted</span>` : `<span class="tag grey">pending</span>`}</td></tr>`).join("");
      const t = document.getElementById("mail-traffic-5");
      if (t) t.innerHTML = `<table><tr><th>From</th><th>To</th><th>Body</th><th>When</th><th>State</th></tr>${rows || `<tr><td colspan=5>No traffic yet</td></tr>`}</table>`;
      // stats
      const st = document.getElementById("mail-stats");
      if (st) {
        const total = this.traffic.length;
        const byState = {pending:0, noted:0, acked:0};
        this.traffic.forEach(m=>{ if(m.acked_at) byState.acked++; else if(m.noted_at) byState.noted++; else byState.pending++; });
        st.innerHTML = `${total} total · <span style="color:#8b949e">${byState.pending} pending</span> · <span style="color:#e3b341">${byState.noted} noted</span> · <span style="color:#7ee787">${byState.acked} acked</span>`;
      }
      this.drawGraphsSoon();
    } catch (e) {}
  },

  drawGraphsSoon() {
    setTimeout(()=>this.drawGraphs(), 50);
  },
  drawGraphs() {
    // by sender bar
    const cv = document.getElementById("mail-by-sender");
    if (cv && this.traffic) {
      const counts = {};
      this.traffic.forEach(m=>{ counts[m.from_scope]=(counts[m.from_scope]||0)+1; });
      const entries = Object.entries(counts).sort((a,b)=>b[1]-a[1]).slice(0,6);
      const ctx=cv.getContext("2d");
      const W=cv.width=360, H=cv.height=140, padL=110, barH=14, gap=6;
      ctx.clearRect(0,0,W,H);
      if (!entries.length) { ctx.fillStyle="#8b949e"; ctx.fillText("no traffic",10,20); return; }
      const max=Math.max(...entries.map(([,v])=>v));
      entries.forEach(([k,v],i)=>{
        const y=8+i*(barH+gap), w=(v/max)*(W-padL-30);
        const col=k==="core"?"#79c0ff":k.startsWith("agent:")?"#e3b341":"#8b949e";
        ctx.fillStyle="#21262d"; ctx.fillRect(padL,y,W-padL-20,barH);
        ctx.fillStyle=col; ctx.fillRect(padL,y,w,barH);
        ctx.fillStyle="#c9d1d9"; ctx.font="11px system-ui"; ctx.textAlign="right";
        ctx.fillText(k.slice(0,18), padL-8, y+10);
        ctx.textAlign="left"; ctx.fillStyle="#8b949e"; ctx.fillText(String(v), padL+w+6, y+10);
      });
    }
    // by state donut
    const cv2=document.getElementById("mail-by-state");
    if(cv2 && this.traffic){
      const byState={pending:0, noted:0, acked:0};
      this.traffic.forEach(m=>{ if(m.acked_at) byState.acked++; else if(m.noted_at) byState.noted++; else byState.pending++; });
      const entries=Object.entries(byState).filter(([,v])=>v>0);
      const W=cv2.width=180,H=cv2.height=140,cx=W/2,cy=H/2-6,r=52,ir=32;
      const ctx=cv2.getContext("2d");
      ctx.clearRect(0,0,W,H);
      if(!entries.length){ctx.fillStyle="#8b949e";ctx.fillText("no data",10,20);return;}
      const col={pending:"#8b949e", noted:"#e3b341", acked:"#7ee787"};
      let a0=-Math.PI/2;
      const total=entries.reduce((s,[,v])=>s+v,0);
      entries.forEach(([k,v])=>{
        const a1=a0+(v/total)*Math.PI*2;
        ctx.beginPath();ctx.moveTo(cx,cy);ctx.arc(cx,cy,r,a0,a1);ctx.closePath();
        ctx.fillStyle=col[k]||"#58a6ff";ctx.fill();
        ctx.strokeStyle="#0d1117";ctx.lineWidth=2;ctx.stroke();
        a0=a1;
      });
      ctx.beginPath();ctx.arc(cx,cy,ir,0,Math.PI*2);ctx.fillStyle="#161b22";ctx.fill();
      ctx.fillStyle="#c9d1d9";ctx.font="bold 14px system-ui";ctx.textAlign="center";ctx.fillText(String(total),cx,cy+4);
      ctx.font="10px system-ui";ctx.fillStyle="#8b949e";ctx.fillText("msgs",cx,cy+16);
    }
  },

  newMsg() {
    const opts = [`<option value="core">core</option>`].concat(
      this.agents.map(a => `<option value="agent:${App.esc(a.agent_id)}">agent:${App.esc(a.agent_id)} — ${App.esc(a.slug||"")}</option>`)).join("");
    App.modal(`<h3>New message — as user</h3>
      <label>To<br><select id="mail-new-to" style="width:100%">${opts}</select></label><br><br>
      <label>Message<br><textarea id="mail-new-body" placeholder="your message…" style="min-height:100px"></textarea></label><br>
      <div style="display:flex;gap:8px;margin-top:8px"><button class="btn" onclick="Tabs.mail.sendNew()">Send</button>
      <button class="btn ghost" onclick="App.closeModal()">Cancel</button></div>
      <pre id="mail-new-out" class="muted"></pre>`);
  },
  async sendNew() {
    const to=document.getElementById("mail-new-to").value;
    const body=document.getElementById("mail-new-body").value.trim();
    const out=document.getElementById("mail-new-out");
    if(!body){out.textContent="Body required";return;}
    try{
      await App.api("/api/mailbox/send",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({from:"user",to,body})});
      App.closeModal();App.toast("Sent to "+to);this.render(document.getElementById("panel"));App.refresh(true);
    }catch(e){out.textContent="Error: "+e.message;}
  },

  async open(msgId) {
    const m = this.inbox.find(x=>x.msg_id===msgId) || this.traffic.find(x=>x.msg_id===msgId);
    // fetch fresh if not in cache
    let msg=m;
    if(!msg){
      try{
        const d=await App.api(`/api/mailbox?scope=all&state=all&limit=100`);
        msg=(d.messages||[]).find(x=>x.msg_id===msgId);
      }catch(e){}
    }
    if(!msg){App.toast("Not found");return;}
    const from=msg.from_scope, to=msg.to_scope || msg.receipt_scope;
    const when=new Date(msg.created_at*1000).toLocaleString();
     const canReply = from !== "user";
     const replyTo = from;
     App.modal(`<h3>Message</h3>
       <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><span class="tag ${from==="core"?"blue":"amber"}">${App.esc(from)}</span> → <span class="tag grey">${App.esc(to||"")}</span> <span class="muted">${App.esc(when)} · ${msg.acked_at?"acked":msg.noted_at?"noted":"pending"}</span></div>
       <pre style="white-space:pre-wrap;background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:10px;margin-top:8px;max-height:240px;overflow:auto">${App.esc(msg.body||"")}</pre>
       <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
         ${canReply?`<button class="btn" onclick="Tabs.mail.reply('${App.esc(msg.msg_id)}')">Reply</button>`:""}
         <button class="btn ghost" onclick="Tabs.mail.ack('${App.esc(msg.msg_id)}');App.closeModal()">Mark acked</button>
         <button class="btn ghost" onclick="App.closeModal()">Close</button>
       </div>
       <div id="mail-reply-area" style="margin-top:12px;display:none">
         <label>Reply to ${App.esc(replyTo)}<br><textarea id="mail-reply-body" placeholder="your reply…" style="min-height:80px"></textarea></label><br>
         <button class="btn" onclick="Tabs.mail.sendReply('${App.esc(msg.msg_id)}','${App.esc(replyTo)}')">Send reply</button>
         <button class="btn ghost" onclick="document.getElementById('mail-reply-area').style.display='none'">Cancel</button>
       </div>`);
    // mark read (receipt read_at) — soft, no ack
    try{ await App.api(`/api/mailbox?scope=user&state=pending&limit=1`); }catch(e){}
  },

   async reply(msgId) {
     let area=document.getElementById("mail-reply-area");
     // if called from inbox where modal not open, open it first
     if(!area){
       await this.open(msgId);
       area=document.getElementById("mail-reply-area");
       if(!area) return;
     }
     area.style.display="block";
     const inp=document.getElementById("mail-reply-body");
     if(inp) inp.focus();
   },
  async sendReply(origId, toScope) {
    const body=document.getElementById("mail-reply-body").value.trim();
    if(!body){App.toast("Body required");return;}
    try{
      await App.api("/api/mailbox/send",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({from:"user",to:toScope,body})});
      // ack original
      await App.api("/api/mailbox/ack",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({scope:"user",msg_ids:[origId]})});
      App.closeModal();App.toast("Replied to "+toScope);this.render(document.getElementById("panel"));App.refresh(true);
    }catch(e){App.toast(e.message);}
  },

  async openLog() {
    App.modal(`<h3>All traffic — log</h3>
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap">
        <input id="mail-log-q" placeholder="search from/to/body…" style="flex:1;min-width:180px">
        <select id="mail-log-state"><option value="all">all states</option><option value="pending">pending</option><option value="noted">noted</option><option value="acked">acked</option></select>
        <button class="btn ghost" onclick="Tabs.mail.renderLog()">Filter</button>
        <button class="btn danger" onclick="Tabs.mail.wipe()">Wipe all</button>
        <button class="btn ghost" onclick="App.closeModal()">Close</button>
      </div>
      <div id="mail-log-body" class="muted">Loading…</div>`);
    document.getElementById("mail-log-q").onkeydown=(e)=>{if(e.key==="Enter") this.renderLog();};
    await this.renderLog();
  },
  async renderLog() {
    const q=(document.getElementById("mail-log-q")?.value||"").toLowerCase();
    const state=document.getElementById("mail-log-state")?.value||"all";
    const body=document.getElementById("mail-log-body");
    if(!body) return;
    body.innerHTML=`<span class="muted">Loading…</span>`;
    try{
      const d=await App.api(`/api/mailbox?scope=all&state=${state}&limit=100`);
      let rows=d.messages||[];
      if(q) rows=rows.filter(m=> (m.from_scope+" "+(m.receipt_scope||m.to_scope)+" "+(m.body||"")).toLowerCase().includes(q));
      body.innerHTML=`<div class="muted" style="margin-bottom:6px">${rows.length} messages (of ${d.messages.length} fetched, state=${state})</div>
        <table><tr><th>From</th><th>To</th><th>Body</th><th>When</th><th>State</th><th></th></tr>
        ${rows.map(m=>`<tr><td><span class="tag ${m.from_scope==="core"?"blue":"amber"}">${App.esc(m.from_scope)}</span></td>
          <td><span class="tag grey">${App.esc(m.receipt_scope||m.to_scope)}</span></td>
          <td>${App.esc((m.body||"").slice(0,140))}</td>
          <td class="muted">${App.esc(new Date(m.created_at*1000).toLocaleString())}</td>
          <td>${m.acked_at?`<span class="tag green">acked</span>`:m.noted_at?`<span class="tag amber">noted</span>`:`<span class="tag grey">pending</span>`}</td>
          <td style="white-space:nowrap"><button class="btn ghost" style="padding:2px 6px;font-size:11px" onclick="Tabs.mail.open('${App.esc(m.msg_id)}')">Open</button> <button class="btn danger" style="padding:2px 6px;font-size:11px" onclick="Tabs.mail.del('${App.esc(m.msg_id)}')">Del</button></td></tr>`).join("")||`<tr><td colspan=6>No matches</td></tr>`}</table>`;
    }catch(e){body.innerHTML=`<div style="color:#ffa198">Error: ${App.esc(e.message)}</div>`;}
  },

  async del(mid) {
    if(!confirm("Delete message "+mid+"?")) return;
    try{
      await App.api("/api/mailbox/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({msg_id:mid})});
      App.toast("Deleted"); this.render(document.getElementById("panel")); App.refresh(true);
      // if log open, refresh it
      if(document.getElementById("mail-log-body")) this.renderLog();
    }catch(e){App.toast(e.message);}
  },
  async wipe() {
    if(!confirm("Wipe ALL mail — every message in every inbox will be deleted. For a clean deployment?")) return;
    try{
      const r=await App.api("/api/mailbox/wipe",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({})});
      App.toast(`Wiped ${r.wiped} messages`); App.closeModal(); this.render(document.getElementById("panel")); App.refresh(true);
    }catch(e){App.toast(e.message);}
  },
  async ack(mid) {
    try {
      await App.api("/api/mailbox/ack",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({scope:"user",msg_ids:[mid]})});
      this.render(document.getElementById("panel"));App.refresh(true);
    } catch(e){App.toast(e.message);}
  },
};
