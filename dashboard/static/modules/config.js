// Config tab — brain controls grouped by area, every value explained.
// Edits land in brain/config.json (the single live config). Units in brackets.
"use strict";
window.Tabs.config = {
  groups: [
    {title: "Scheduler & snapshots", desc: "When the brain runs on its own and how it backs up first.",
     fields: [
       ["interval_minutes", "Run interval [min]", "Minutes between automatic brain runs."],
       ["snapshot_cooldown_s", "Snapshot cooldown [s]", "Minimum seconds between two auto snapshots."],
       ["keep_last_snapshots", "Keep snapshots", "How many snapshots are kept per database."],
       ["keep_last_logs", "Keep run logs", "How many run reports are kept."],
     ],
     toggles: [
       ["auto_snapshot_before_jobs", "Snapshot before jobs", "Take a backup before each job run (coalesced: at most one per database per run)."],
     ]},
    {title: "Maintenance cycle", desc: "Decay & prune for short/long-term layers (promotions now handled by Observer + Core Helper).",
      fields: [
        ["max_unused_days", "Unused age [days]", "Short/long-term nodes untouched this long become prune candidates (with floor + quiet + edgeless)."],
        ["prune_importance_floor", "Prune floor [0–1]", "Nodes below this importance may be pruned when stale."],
      ],
      toggles: []},
    {title: "Duplicates", desc: "Finding and merging notes that say the same thing.",
     fields: [
       ["dedup_similarity_threshold", "Duplicate bar [0–1]", "Similarity above which two notes count as duplicates."],
       ["dedup_scan_cap", "Scan cap [nodes]", "Maximum notes scanned per dedup run."],
     ],
     toggles: []},
    {title: "Link discovery", desc: "Automatically wiring related notes together.",
     fields: [
       ["discover_scan_cap", "Scan cap [nodes]", "Maximum notes scanned per discover run."],
       ["discover_link_floor", "Link floor [0–1]", "Minimum similarity to create a link."],
       ["discover_link_ceil", "Duplicate ceiling [0–1]", "Above this counts as duplicate, not a link."],
     ],
     toggles: []},
     {title: "Contradictions", desc: "Spotting notes that disagree with each other (core + every agent).",
     fields: [
       ["contradiction_low_trust", "Low trust [0–1]", "Below this a note is the losing suspect."],
       ["contradiction_high_trust", "High trust [0–1]", "Above this a note is the trusted survivor."],
       ["contradiction_scan_cap", "Scan cap [nodes]", "Maximum notes scanned per detection run."],
       ["contradiction_min_overlap_ratio", "Overlap ratio [0–1]", "Shared words / shorter note — need ≥ this to be same claim (e.g. 0.25 = 25%)."],
     ],
     toggles: [
       ["contradiction_auto_resolve", "Auto-resolve", "Automatically drop the low-trust side. Off = you decide in the Contradicts tab."],
     ]},
     {title: "Ephemeral telemetry", desc: "Short-lived machine chatter (not knowledge). The label allowlist itself lives in the Ephemeral tab.",
      fields: [
        ["ephemeral_keep_last", "Keep per label [rows]", "Newest rows kept per label after a compact."],
        ["ephemeral_max_age_hours", "Max age [hours]", "Rows older than this are deleted by compact. Always applies (e.g. 168 = 7 days)."],
      ],
      toggles: []},
    {title: "Vacuum", desc: "Reclaiming disk space after pruning.",
     fields: [
       ["vacuum_freelist_threshold_pct", "Waste trigger [%]", "VACUUM when wasted pages exceed this share."],
       ["vacuum_freelist_min_pages", "Min waste [pages]", "Never VACUUM below this much waste."],
     ],
     toggles: [
       ["vacuum_after_prune", "Vacuum after prune", "Auto-reclaim space when a run pruned a lot."],
     ]},
    {title: "Agent working memory", desc: "Keeping each agent's scratch space from overflowing.",
     fields: [
       ["agent_working_high_water", "High-water [notes]", "Max working notes per agent before demotion kicks in."],
       ["agent_working_demote_batch", "Demote batch [notes]", "How many notes per regulate run at most."],
       ["agent_working_max_age_hours", "Max age [hours]", "Working notes older than this are demotion candidates."],
       ["agent_working_weight_access", "Access weight", "How much recent reads protect a note."],
       ["agent_working_weight_importance", "Importance weight", "How much importance protects a note."],
       ["agent_working_weight_age", "Age weight", "How strongly age pushes a note out."],
     ],
     toggles: [
       ["agent_working_regulator_enabled", "Regulator on", "Master switch for the working-memory janitor."],
     ]},
     {title: "Ghost agents", desc: "Agents that have no notes left (orphaned identities).",
      fields: [
        ["prune_empty_agents_min_age_hours", "Min age [hours]", "Ghost must be at least this old before auto-removal (0 = immediate)."],
      ],
      toggles: [
        ["prune_empty_agents", "Remove ghosts", "Auto-delete agents with 0 notes during maintenance (also adds box below). Off = ghosts accumulate, on = pruned."],
      ]},
     {title: "Core Helper — working regulator", desc: "Auto-tunes core.db WORKING layer (never touches short_term/long_term/archive). Toggle on = runs each maintenance cycle; age gate handles the rest.",
      fields: [
        ["core_helper_min_age_hours", "Min age [hours]", "Working node must be this old before evaluation (72 = 3 days)."],
        ["core_helper_imp_threshold", "Importance bar [0–1]", "Below this = not important; above = important."],
        ["core_helper_trust_low", "Trust low [0–1]", "≤ low = low-trust → short-term."],
        ["core_helper_trust_high", "Trust high [0–1]", "≥ high = high-trust; middle = access decides."],
        ["core_helper_access_threshold", "Access bar [reads]", "When trust is middle and important, ≥ this → long-term, else short-term."],
      ],
      toggles: [
        ["core_helper_enabled", "Core Helper on", "Master switch — when off, maintenance skips core WORKING regulation. Archive never auto — Core sets it manually."],
      ]},
     {title: "System", desc: "Plumbing. Change carefully.",
     fields: [],
     toggles: [
       ["auto_rebuild_vectors", "Rebuild search index", "Refresh the search index after maintenance runs. Off = stale search results."],
     ]},
  ],
  cfg: {},
  allKeys() {
    const out = [];
    for (const g of this.groups) {
      for (const [k] of g.fields) out.push(k);
      for (const [k] of g.toggles) out.push(k);
    }
    return out;
  },
  async render(el) {
    el.innerHTML = `<div class="card">Loading config…</div>`;
    try {
      const d = await App.api("/api/config");
      this.cfg = d.config || {};
      // scheduler running is the live truth; fall back to persisted cron_enabled
      let schedRunning = !!this.cfg.cron_enabled;
      try {
        const st = await App.api("/api/status?db=all");
        if (st.scheduler) schedRunning = !!st.scheduler.running;
      } catch(e) {}
      const secs = this.groups.map(g => {
        const fs = g.fields.map(([k, label, help]) =>
          `<span class="frow"><label style="font-weight:600">${App.esc(label)}<br><input id="cfg-${k}" value="${App.esc(this.cfg[k] ?? "")}" style="width:130px;margin-top:4px"></label><span class="fdesc">${App.esc(help)}</span></span>`).join("");
        const ts = [...g.toggles].sort((a, b) => a[1].localeCompare(b[1])).map(([k, label, help]) =>
          `<div style="display:flex;align-items:flex-start;gap:10px;margin:8px 0;padding:8px 10px;background:#0d1117;border:1px solid #21262d;border-radius:8px"><label class="switch"><input type="checkbox" id="cfg-${k}" ${this.cfg[k] ? "checked" : ""}><span class="slider"></span></label><div><b>${App.esc(label)}</b> ${this.cfg[k] ? `<span class="tag green">on</span>` : `<span class="tag grey">off</span>`}<span class="fdesc" style="margin:4px 0 0">${App.esc(help)}</span></div></div>`).join("");
        return `<div class="sec"><h4>${App.esc(g.title)}</h4><span class="fdesc">${App.esc(g.desc)}</span><div>${fs}</div><div>${ts}</div></div>`;
      }).join("");
      el.innerHTML = `<div class="card"><h3>Config — brain controls (live, no restart)</h3>
        ${secs}
        <div class="sec"><h4>Dashboard token</h4>
        <span class="fdesc">Password for this dashboard. Changing it logs everyone out.</span>
        <div><label>Token <input id="cfg-dashboard_token" type="password" value="" placeholder="(hidden — fill to change)" style="width:220px"></label></div></div>
        <div style="margin:12px 0 0"><button class="btn" onclick="Tabs.config.save()">Save Config</button>
        <button class="btn ghost" onclick="Tabs.config.reload()">Reload from disk</button>
        <button class="btn ghost" onclick="Tabs.config.reset()">Reset Defaults</button>
        <button class="btn warn" onclick="Tabs.config.vacuumCheck()">Check & Auto-VACUUM</button></div>
        <pre id="c-out" class="muted"></pre></div>
        <div class="card"><h3>Scheduler</h3>
        <span class="fdesc">The background timer that runs maintenance on its own. Toggle persists — dashboard auto-restarts the scheduler if it was on.</span>
        <div style="display:flex;align-items:center;gap:12px;margin:12px 0;padding:12px;background:#0d1117;border:1px solid #21262d;border-radius:8px">
          <label class="switch"><input type="checkbox" id="cfg-sched-toggle" ${schedRunning ? "checked" : ""} onchange="Tabs.config.schedToggle(this.checked)"><span class="slider"></span></label>
          <div><b>Scheduler</b> <span id="cfg-sched-state">${schedRunning ? `<span class="tag green">on</span>` : `<span class="tag grey">off</span>`}</span><div class="fdesc" style="margin:4px 0 0">When on, brain runs every ${App.esc(this.cfg.interval_minutes || 60)} min — survives dashboard restarts.</div></div>
          <span class="muted" style="margin-left:auto" id="cfg-sched-hint">${schedRunning ? "Running" : "Stopped"}</span>
        </div>
        <pre id="c-sched" class="muted"></pre></div>`;
    } catch (e) { el.innerHTML = `<div class="card">Error: ${App.esc(e.message)}</div>`; }
  },
  collect() {
    const payload = {};
    for (const k of this.allKeys()) {
      const elm = document.getElementById("cfg-" + k);
      if (!elm) continue;
      if (elm.type === "checkbox") {
        if (elm.checked !== !!this.cfg[k]) payload[k] = elm.checked;
      } else if (elm.value !== "" && elm.value !== String(this.cfg[k])) {
        payload[k] = elm.value;
      }
    }
    const tok = document.getElementById("cfg-dashboard_token").value;
    if (tok) payload["dashboard_token"] = tok;
    return payload;
  },
  async save() {
    try {
      const r = await App.api("/api/config", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify(this.collect())});
      document.getElementById("c-out").textContent = "Saved.";
      this.cfg = r.config || this.cfg; App.refresh(true);
    } catch (e) { App.toast(e.message); }
  },
  async reload() {
    try {
      const r = await App.api("/api/config", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({reload: true})});
      this.cfg = r.config || {}; this.render(document.getElementById("panel"));
      App.toast("Reloaded from disk"); App.refresh(true);
    } catch (e) { App.toast(e.message); }
  },
  async reset() {
    if (!confirm("Reset all brain config to defaults?")) return;
    try {
      const r = await App.api("/api/config_reset", {method: "POST"});
      this.cfg = r.config || {}; this.render(document.getElementById("panel"));
    } catch (e) { App.toast(e.message); }
  },
  async vacuumCheck() {
    try {
      const r = await App.api("/api/check_vacuum", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: "both"})});
      document.getElementById("c-out").textContent = JSON.stringify(r, null, 1).slice(0, 2000);
      App.refresh(true);
    } catch (e) { App.toast(e.message); }
  },
  async sched(on) {
    return this.schedToggle(on);
  },
  async schedToggle(on) {
    // read live interval from the input if the user edited it without saving
    let minutes = this.cfg.interval_minutes || 60;
    const ivEl = document.getElementById("cfg-interval_minutes");
    if (ivEl && ivEl.value !== "") {
      const v = parseInt(ivEl.value, 10);
      if (!isNaN(v) && v > 0) minutes = v;
    }
    // optimistic UI
    const stateEl = document.getElementById("cfg-sched-state");
    const hintEl = document.getElementById("cfg-sched-hint");
    const toggleEl = document.getElementById("cfg-sched-toggle");
    if (stateEl) stateEl.innerHTML = on ? `<span class="tag green">on</span>` : `<span class="tag grey">off</span>`;
    if (hintEl) hintEl.textContent = on ? "Enabling…" : "Stopping…";
    if (toggleEl) toggleEl.disabled = true;
    try {
      const r = await App.api("/api/scheduler", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({enabled: !!on, interval_minutes: minutes,
          max_unused_days: this.cfg.max_unused_days || 4})});
      // persist cron_enabled locally so re-render without fetch stays correct
      this.cfg.cron_enabled = !!on;
      this.cfg.interval_minutes = minutes;
      const running = !!r.is_running;
      if (stateEl) stateEl.innerHTML = running ? `<span class="tag green">on</span>` : `<span class="tag grey">off</span>`;
      if (hintEl) hintEl.textContent = running ? "Running" : "Stopped";
      if (toggleEl) { toggleEl.checked = running; toggleEl.disabled = false; }
      const out = document.getElementById("c-sched");
      if (out) out.textContent = (running ? "Scheduler on" : "Scheduler off") + ` — interval ${minutes} min`;
      App.refresh(true);
    } catch (e) {
      if (toggleEl) { toggleEl.checked = !on; toggleEl.disabled = false; }
      if (hintEl) hintEl.textContent = "Error";
      App.toast(e.message);
    }
  },
};
