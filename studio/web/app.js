/* Jimbo4prezz Studio — single-page UI (no build step). */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const main = () => $("#main");
let STATUS = null;
let PROFILES = [];

const PLATFORM_LABEL = { youtube: "YouTube Long-Form", youtube_shorts: "YouTube Shorts", tiktok: "TikTok", instagram_reels: "Instagram Reels", x: "X / Twitter" };
const SHORT_PLATFORMS = ["youtube_shorts", "tiktok", "instagram_reels", "x"];
const ALL_PLATFORMS = ["youtube", ...SHORT_PLATFORMS];

function fmt(t, dec = 1) {
  if (t == null || isNaN(t)) return "–";
  const neg = t < 0; t = Math.abs(t);
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const ss = s.toFixed(dec).padStart(dec ? 3 + dec : 2, "0");
  return (neg ? "-" : "") + (h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`);
}
function parseT(v) {
  v = String(v).trim();
  if (!v) return null;
  if (!v.includes(":")) return parseFloat(v);
  return v.split(":").map(parseFloat).reverse().reduce((acc, x, i) => acc + x * 60 ** i, 0);
}
const dur = (s) => s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${(s || 0).toFixed(1)}s`;
const ago = (ts) => { const d = Date.now() / 1000 - ts; return d < 60 ? "just now" : d < 3600 ? `${Math.floor(d / 60)} min ago` : d < 86400 ? `${Math.floor(d / 3600)} h ago` : new Date(ts * 1000).toLocaleDateString(); };
const scoreCls = (s) => s >= 80 ? "hi" : s >= 65 ? "mid" : "lo";
const fileUrl = (p, dl) => `/api/files?path=${encodeURIComponent(p)}${dl ? "&download=true" : ""}`;

async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.body && !(o.body instanceof FormData)) { o.body = JSON.stringify(o.body); o.headers = { "Content-Type": "application/json" }; }
  const r = await fetch(path, o);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* ignore */ }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}
const post = (p, body) => api(p, { method: "POST", body: body || {} });
const patch = (p, body) => api(p, { method: "PATCH", body });
const del = (p) => api(p, { method: "DELETE" });

function toast(msg, err = false) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : "");
  t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), err ? 7000 : 3500);
}
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; } catch (e) { toast(e.message, true); throw e; }
}

/* ---------------------------------------------------------------- jobs */
let jobTimer = null;
const jobWatch = new Map(); // job id -> callback
async function pollJobs() {
  try {
    const jobs = await api("/api/jobs?limit=15");
    const active = jobs.filter((j) => ["queued", "running"].includes(j.status));
    $("#jobs-mini").innerHTML = active.length ? `<div class="nav-group">Processing</div>` + active.map((j) =>
      `<div class="job"><b>${esc(j.kind)}</b> #${j.target_id}<div class="progress"><i style="width:${Math.round(j.progress * 100)}%"></i></div><div>${esc(j.message)}</div></div>`).join("") : "";
    for (const j of jobs) {
      if (jobWatch.has(j.id) && ["done", "failed", "interrupted"].includes(j.status)) {
        const cb = jobWatch.get(j.id); jobWatch.delete(j.id);
        if (j.status === "done") toast(`${j.kind} finished: ${j.message}`); else toast(`${j.kind} ${j.status}: ${j.message}`, true);
        cb && cb(j);
      }
    }
    clearTimeout(jobTimer);
    jobTimer = setTimeout(pollJobs, active.length || jobWatch.size ? 1500 : 6000);
  } catch (e) { jobTimer = setTimeout(pollJobs, 8000); }
}
function watchJob(id, cb) { if (id) { jobWatch.set(id, cb || (() => route())); clearTimeout(jobTimer); jobTimer = setTimeout(pollJobs, 500); } }

/* ---------------------------------------------------------------- router */
const ROUTES = {};
function page(name, fn) { ROUTES[name] = fn; }
async function route() {
  const [name, ...args] = (location.hash.replace(/^#\/?/, "") || "dashboard").split("/");
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === name));
  const fn = ROUTES[name] || ROUTES.dashboard;
  try { await fn(...args); } catch (e) { main().innerHTML = `<div class="card"><h3>Something went wrong</h3><pre>${esc(e.stack || e.message)}</pre></div>`; }
}
window.addEventListener("hashchange", route);
const go = (h) => { location.hash = h; };

/* ---------------------------------------------------------------- shared widgets */
function statusBadge(s) { return `<span class="badge ${esc(s)}">${esc(s)}</span>`; }
function platformChips(scores) {
  return `<div class="chips">${SHORT_PLATFORMS.map((p) => scores && scores[p] != null ?
    `<span class="badge ${scores[p] >= 80 ? "good" : scores[p] >= 70 ? "accent" : ""}">${esc(PLATFORM_LABEL[p])} ${scores[p]}</span>` : "").join("")}</div>`;
}
function scoreBars(scores) {
  if (!scores || !Object.keys(scores).length) return "";
  return `<div class="bars">${Object.entries(scores).map(([k, v]) =>
    `<span class="muted">${esc(k.replace(/_/g, " "))}</span><div class="bar"><i style="width:${v}%"></i></div><span>${v}</span>`).join("")}</div>`;
}
function uploader({ kind = "auto", multiple = true, title = "Drop videos here", onDone, showKind = true }) {
  const id = "up" + Math.random().toString(36).slice(2, 8);
  setTimeout(() => {
    const dz = $("#" + id); if (!dz) return;
    const input = $("input[type=file]", dz);
    const send = (files) => {
      if (!files.length) return;
      const fd = new FormData();
      [...files].forEach((f) => fd.append("files", f));
      fd.append("kind", showKind ? $(`#${id}-kind`).value : kind);
      fd.append("category", $(`#${id}-cat`).value);
      fd.append("project_id", $(`#${id}-proj`).value || "");
      fd.append("auto_analyze", "true");
      fd.append("auto_package", "true");
      const xhr = new XMLHttpRequest();
      const bar = $(".progress i", dz);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.style.width = (e.loaded / e.total * 100).toFixed(1) + "%"; };
      xhr.onload = () => {
        bar.style.width = "0%";
        if (xhr.status >= 300) { toast("Upload failed: " + xhr.responseText, true); return; }
        const res = JSON.parse(xhr.responseText);
        toast(`Uploaded ${res.length} file(s); analysis started`);
        res.forEach((r) => watchJob(r.job_id, () => route()));
        onDone && onDone(res);
      };
      xhr.onerror = () => toast("Upload failed", true);
      xhr.open("POST", "/api/sources/upload");
      xhr.send(fd);
    };
    input.onchange = () => send(input.files);
    dz.ondragover = (e) => { e.preventDefault(); dz.classList.add("over"); };
    dz.ondragleave = () => dz.classList.remove("over");
    dz.ondrop = (e) => { e.preventDefault(); dz.classList.remove("over"); send(e.dataTransfer.files); };
    api("/api/projects").then((ps) => { const sel = $(`#${id}-proj`); if (sel) sel.innerHTML = `<option value="">No project</option>` + ps.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join(""); });
  });
  const cats = ["auto", ...(STATUS?.content_types || [])];
  return `<div class="dropzone" id="${id}">
    <div style="font-weight:600;color:var(--text)">${esc(title)}</div>
    <div class="small" style="margin:4px 0 10px">Originals are stored read-only and never modified. Analysis and platform edits start automatically.</div>
    <div class="row" style="justify-content:center">
      <input type="file" accept="video/*,.mkv,.flv,.ts" ${multiple ? "multiple" : ""}>
      ${showKind ? `<select id="${id}-kind"><option value="auto">Type: auto</option><option value="recording">Livestream / recording</option><option value="twitch_vod">Twitch VOD</option><option value="twitch_clip">Twitch clip</option><option value="youtube">YouTube video</option><option value="gameplay">Gameplay recording</option><option value="reaction">Reaction video</option><option value="clip">Other clip</option></select>` : ""}
      <select id="${id}-cat">${cats.map((c) => `<option value="${c}">Category: ${c.replace("_", " ")}</option>`).join("")}</select>
      <select id="${id}-proj"><option value="">No project</option></select>
    </div>
    <div class="progress" style="margin-top:12px"><i style="width:0%"></i></div>
  </div>`;
}
function registerPathForm() {
  setTimeout(() => {
    const b = $("#reg-btn"); if (!b) return;
    b.onclick = () => act(async () => {
      const r = await post("/api/sources/register", { path: $("#reg-path").value, kind: $("#reg-kind").value });
      watchJob(r.job_id); go(`#/source/${r.source_id}`);
    }, "Registered — analysis started");
  });
  return `<details style="margin-top:10px"><summary class="muted small">Large recording already on this computer? Reference it in place (no copy)</summary>
    <div class="row" style="margin-top:8px"><input id="reg-path" placeholder="/path/to/stream.mkv" style="flex:1;min-width:260px">
    <select id="reg-kind"><option value="recording">Livestream / recording</option><option value="twitch_vod">Twitch VOD</option><option value="gameplay">Gameplay</option><option value="reaction">Reaction</option><option value="auto">auto</option></select>
    <button id="reg-btn">Add</button></div></details>`;
}
function planTable(plans, { showSource = true } = {}) {
  if (!plans.length) return `<div class="empty">No edits here yet.</div>`;
  return `<div class="table-wrap"><table><thead><tr><th>Edit</th><th>Platform</th>${showSource ? "<th>Source</th>" : ""}<th>Length</th><th>Format</th><th>Structure</th><th>Status</th><th>Confidence</th><th>Render</th></tr></thead><tbody>
  ${plans.map((p) => `<tr><td><a href="#/plan/${p.id}">${esc(p.title || "Untitled")}</a> <span class="muted small">v${p.version}</span></td>
    <td>${esc(PLATFORM_LABEL[p.platform] || p.platform)}</td>
    ${showSource ? `<td><a href="#/source/${p.source_id}">${esc(p.source_title)}</a><div class="small muted">${fmt(p.source_range[0])}–${fmt(p.source_range[1])}</div></td>` : ""}
    <td>${dur(p.duration)}</td><td>${esc(p.format?.aspect)} <span class="muted small">${esc(p.layout_mode)}</span></td>
    <td class="small">${esc((p.structure || "").replace("_", " "))}</td><td>${statusBadge(p.status)}</td>
    <td>${p.confidence != null ? Math.round(p.confidence) : "–"}</td>
    <td>${p.render ? statusBadge(p.render.status) : '<span class="muted small">not rendered</span>'}</td></tr>`).join("")}
  </tbody></table></div>`;
}
function momentCard(m) {
  const pkg = m.package_id ? `<a class="btn" href="#/package/${m.package_id}">Open package</a>` : `<button class="primary" data-pkg="${m.id}">Create platform edits</button>`;
  return `<div class="card">
    <div class="row"><span class="score ${scoreCls(m.overall_score)}">${Math.round(m.overall_score)}</span>
      <div style="flex:1;min-width:0"><b>${esc(m.title)}</b><div class="small muted">${esc(m.source_title || "")} · ${fmt(m.start)}–${fmt(m.end)} (${dur(m.end - m.start)}) · ${esc((m.structure || "").replace("_", " "))} · ${esc(m.engine)}</div></div></div>
    <div style="margin:8px 0">${platformChips(m.platform_scores)}</div>
    <div class="small">${esc(m.reasons || "")}</div>
    ${m.excerpt ? `<div class="small muted" style="margin-top:6px;max-height:54px;overflow:hidden">“${esc(m.excerpt.slice(0, 220))}${m.excerpt.length > 220 ? "…" : ""}”</div>` : ""}
    <div class="row" style="margin-top:10px">${pkg}<a class="btn" href="#/source/${m.source_id}/${m.start.toFixed(1)}">View in source</a>
      <button class="ghost tiny" data-pick="${m.id}">Choose platforms…</button></div>
    <div class="small muted" style="margin-top:6px">A recommendation, not a guarantee of virality.</div>
  </div>`;
}
function bindMomentButtons(root, reload) {
  $$("[data-pkg]", root).forEach((b) => b.onclick = () => act(async () => {
    b.disabled = true;
    const r = await post(`/api/moments/${b.dataset.pkg}/package`, {});
    if (!r.plans.length) { toast(r.message || "No platform fits this moment", true); b.disabled = false; return; }
    toast(`Created ${r.plans.length} platform edits${r.skipped.length ? ` (skipped: ${r.skipped.map((p) => PLATFORM_LABEL[p]).join(", ")})` : ""}`);
    go(`#/package/${r.package_id}`);
  }));
  $$("[data-pick]", root).forEach((b) => b.onclick = () => {
    const wrap = document.createElement("div");
    wrap.className = "row"; wrap.style.marginTop = "8px";
    wrap.innerHTML = `<div class="checks">${SHORT_PLATFORMS.map((p) => `<label><input type="checkbox" value="${p}" checked>${PLATFORM_LABEL[p]}</label>`).join("")}</div><button class="primary tiny">Create</button>`;
    b.parentElement.after(wrap); b.remove();
    $("button", wrap).onclick = () => act(async () => {
      const plats = $$("input:checked", wrap).map((i) => i.value);
      const r = await post(`/api/moments/${b.dataset.pick}/package`, { platforms: plats, force: true });
      go(`#/package/${r.package_id}`);
    });
  });
  reload;
}

/* ---------------------------------------------------------------- dashboard */
page("dashboard", async () => {
  const d = await api("/api/dashboard");
  const adobeApps = Object.values(d.adobe.apps);
  main().innerHTML = `
  <h1>Dashboard</h1><p class="sub">One source → AI understands it → best moments → platform-specific edits → you approve.</p>
  ${!d.claude || !d.transcription.length ? `<div class="note" style="margin-bottom:14px">
    ${!d.claude ? "Claude isn't configured, so content understanding uses the signal heuristics (loudness, reactions, scene changes, transcript cues). Set ANTHROPIC_API_KEY to enable it. " : ""}
    ${!d.transcription.length ? "No speech-to-text engine is installed (<code>pip install faster-whisper</code>). You can still import SRT/VTT transcripts per source." : ""}</div>` : ""}
  <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(150px,1fr))">
    ${Object.entries(d.review).map(([p, n]) => `<a class="card" href="#/social/${p}" style="text-decoration:none;color:inherit"><div class="stat">${n}</div><div class="stat-label">${esc(PLATFORM_LABEL[p])} ready for review</div></a>`).join("")}
    <div class="card"><div class="stat">${d.approved}</div><div class="stat-label">Approved edits</div></div>
    <div class="card"><div class="stat">${d.renders}</div><div class="stat-label">Rendered videos</div></div>
    <a class="card" href="#/thumbnails" style="text-decoration:none;color:inherit"><div class="stat">${d.thumbnails.total}</div><div class="stat-label">Thumbnails · ${d.thumbnails.approved_without} approved edits without one</div></a>
  </div>
  <div class="cols" style="margin-top:18px">
    <div>
      <h2>Strongest clips discovered</h2>
      ${d.strongest.length ? `<div class="grid wide">${d.strongest.slice(0, 4).map((m) => momentCard(m)).join("")}</div>` : `<div class="empty">Upload a stream or Twitch clip to get started.</div>`}
      <h2>Add content</h2>${uploader({ title: "Drop a livestream, VOD, YouTube video or Twitch clips" })}${registerPathForm()}
    </div>
    <div>
      <h2>Processing jobs</h2>
      <div class="card">${d.jobs.length ? `<table><tbody>${d.jobs.map((j) => `<tr><td>${esc(j.kind)} #${j.target_id}</td><td>${statusBadge(j.status)}</td><td class="small">${esc(j.message)}</td><td>${["failed", "interrupted"].includes(j.status) ? `<button class="tiny" data-retry="${j.id}">Retry</button>` : ""}</td></tr>`).join("")}</tbody></table>` : `<span class="muted">No jobs yet.</span>`}</div>
      <h2>Recent recordings</h2>
      <div class="card">${d.recent_sources.length ? `<table><tbody>${d.recent_sources.map((s) => `<tr><td><a href="#/source/${s.id}">${esc(s.title)}</a></td><td class="small muted">${esc(s.kind)} · ${dur(s.duration)}</td><td>${statusBadge(s.status)}</td></tr>`).join("")}</tbody></table>` : `<span class="muted">Nothing yet.</span>`}</div>
      <h2>Active projects</h2>
      <div class="card">${d.projects.length ? d.projects.map((p) => `<div><a href="#/projects">${esc(p.name)}</a> <span class="muted small">${p.n_sources} sources</span></div>`).join("") : `<span class="muted">No projects. <a href="#/projects">Create one</a></span>`}</div>
      <h2>Integrations</h2>
      <div class="card small">
        <div>Claude content understanding: ${d.claude ? statusBadge("done").replace("done", "connected") : '<span class="badge warn">heuristic fallback</span>'}</div>
        <div style="margin-top:6px">Transcription: ${d.transcription.length ? esc(d.transcription.join(", ")) : '<span class="badge warn">none installed</span>'}</div>
        <div style="margin-top:6px">Adobe: ${adobeApps.filter((a) => a.installed).map((a) => esc(a.name)).join(", ") || '<span class="muted">no Adobe apps detected on this machine (hand-off files are still generated)</span>'} · <a href="#/adobe">details</a></div>
      </div>
    </div>
  </div>`;
  bindMomentButtons(main());
  $$("[data-retry]").forEach((b) => b.onclick = () => act(async () => { const r = await post(`/api/jobs/${b.dataset.retry}/retry`); watchJob(r.job_id); }, "Retrying"));
});

/* ---------------------------------------------------------------- projects */
page("projects", async () => {
  const ps = await api("/api/projects");
  main().innerHTML = `<h1>Projects</h1><p class="sub">Group sources (a stream, a series, an event). Every asset still links back to its source.</p>
  <div class="card row"><input id="pn" placeholder="Project name" style="flex:1"><input id="pd" placeholder="Description" style="flex:2"><button class="primary" id="pc">Create project</button></div>
  <div style="margin-top:14px">${ps.length ? ps.map((p) => `<div class="card" style="margin-bottom:10px"><div class="row"><b>${esc(p.name)}</b><span class="muted small">${p.n_sources} sources</span><span class="spacer"></span><button class="tiny ghost" data-del="${p.id}">Delete</button></div><div class="muted small">${esc(p.description)}</div><div id="ps${p.id}" class="small" style="margin-top:8px"></div></div>`).join("") : `<div class="empty">No projects yet.</div>`}</div>`;
  $("#pc").onclick = () => act(async () => { await post("/api/projects", { name: $("#pn").value, description: $("#pd").value }); route(); }, "Project created");
  $$("[data-del]").forEach((b) => b.onclick = () => confirm("Delete project? Sources are kept.") && act(async () => { await del(`/api/projects/${b.dataset.del}`); route(); }));
  for (const p of ps) {
    api(`/api/sources?project_id=${p.id}`).then((ss) => { const el = $(`#ps${p.id}`); if (el) el.innerHTML = ss.map((s) => `<a href="#/source/${s.id}">${esc(s.title)}</a> <span class="muted">(${esc(s.kind)}, ${s.n_plans} edits)</span>`).join(" · ") || '<span class="muted">No sources</span>'; });
  }
});

/* ---------------------------------------------------------------- source detail */
function drawTimeline(canvas, sig, items, duration, onClick) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const c = canvas.getContext("2d"); c.scale(dpr, dpr);
  c.fillStyle = "#0b0c10"; c.fillRect(0, 0, w, h);
  const D = duration || sig.duration || 1;
  for (const it of items) {
    c.fillStyle = it.color; c.globalAlpha = it.alpha ?? 0.35;
    c.fillRect(it.start / D * w, it.y ?? 0, Math.max(2, (it.end - it.start) / D * w), it.h ?? h);
  }
  c.globalAlpha = 1;
  const ex = sig.excitement || [];
  if (ex.length) {
    const mx = Math.max(...ex, 1), mn = Math.min(...ex, 0);
    c.strokeStyle = "#5ab0ff"; c.lineWidth = 1; c.beginPath();
    ex.forEach((v, i) => { const x = i * sig.hop / D * w, y = h - 4 - (v - mn) / (mx - mn || 1) * (h - 10); i ? c.lineTo(x, y) : c.moveTo(x, y); });
    c.stroke();
  }
  canvas.onclick = (e) => { const r = canvas.getBoundingClientRect(); onClick && onClick((e.clientX - r.left) / r.width * D); };
}

page("source", async (id, t0) => {
  const s = await api(`/api/sources/${id}`);
  const shorts = s.moments.filter((m) => m.kind === "short"), longs = s.moments.filter((m) => m.kind === "long");
  const isClip = s.duration <= 180;
  main().innerHTML = `
  <div class="row"><h1>${esc(s.title)}</h1>${statusBadge(s.status)}<span class="spacer"></span>
    <button id="reanalyze">Re-analyze</button><label class="btn" style="margin:0">Import transcript (SRT/VTT)<input type="file" id="tr-file" accept=".srt,.vtt,.json" hidden></label>
    <button class="ghost" id="del-src">Delete</button></div>
  <p class="sub">${esc(s.kind)} · ${esc(s.category)} · ${dur(s.duration)} · ${s.width}×${s.height} @ ${s.fps}fps · ${s.stored_copy ? "stored read-only copy" : "referenced in place"} · <span class="small">${esc(s.original_name)}</span></p>
  ${s.jobs[0] && ["queued", "running"].includes(s.jobs[0].status) ? `<div class="note">Analyzing: ${esc(s.jobs[0].message)} (${Math.round(s.jobs[0].progress * 100)}%)</div>` : ""}
  <div class="cols" style="margin-top:12px">
    <div>
      <video id="vid" controls preload="metadata" src="/api/sources/${s.id}/media"></video>
      <div class="timeline" style="margin-top:8px"><canvas id="tl"></canvas></div>
      <div class="small muted" style="margin-top:4px">Blue: excitement signal · yellow: short-form moments · purple: long-form segments. Click to seek.</div>
      <div class="card" style="margin-top:12px"><h3>Mark a moment manually</h3>
        <div class="row"><button class="tiny" id="mk-in">Set start = playhead</button><input class="t" id="mk-s" placeholder="start"><button class="tiny" id="mk-out">Set end = playhead</button><input class="t" id="mk-e" placeholder="end">
        <input id="mk-title" placeholder="Title (optional)" style="flex:1"><button class="primary" id="mk-go">Add &amp; score</button></div></div>
    </div>
    <div>
      <div class="card"><h3>Understanding</h3><div class="kv">
        <div>Engine</div><div>${esc(s.analysis?.engine || "not analyzed")}</div>
        <div>Transcript</div><div>${s.transcript ? `${esc(s.transcript.engine)} · ${s.transcript.segments} segments` : '<span class="muted">none</span>'}</div>
        <div>Scene changes</div><div>${s.analysis?.scenes ?? "–"}</div>
        <div>Face / facecam</div><div>${s.analysis?.faces?.box ? `found in ${Math.round(s.analysis.faces.found_ratio * 100)}% of samples` : "not found"}</div>
        <div>Silence</div><div>${s.analysis?.stats ? Math.round(s.analysis.stats.silence_ratio * 100) + "%" : "–"}</div>
      </div>${s.analysis?.summary ? `<p class="small">${esc(s.analysis.summary)}</p>` : ""}</div>
      ${!isClip ? `<div class="card" style="margin-top:12px"><h3>Long-form segments (${longs.length})</h3>
        ${longs.map((m) => `<label class="row small" style="color:var(--text)"><input type="checkbox" class="lf" value="${m.id}"> ${fmt(m.start, 0)}–${fmt(m.end, 0)} · ${esc(m.title)}</label>`).join("") || '<span class="muted">None detected.</span>'}
        <div class="row" style="margin-top:8px"><input id="lf-title" placeholder="Video title (optional)" style="flex:1"><label class="row small" style="margin:0;color:var(--text)"><input type="checkbox" id="lf-cold"> cold open</label><button class="primary" id="lf-go">Create YouTube edit</button></div></div>` : ""}
    </div>
  </div>
  <h2>Short-form moments (${shorts.length})</h2>
  ${shorts.length ? `<div class="grid wide">${shorts.map((m) => momentCard({ ...m, source_title: s.title, package_id: s.plans.find((p) => p.moment_id === m.id)?.package_id })).join("")}</div>` : `<div class="empty">${s.status === "analyzed" ? "No moment cleared the quality bar. That's intentional: the studio would rather recommend nothing than weak clips. You can mark one manually above." : "Not analyzed yet."}</div>`}
  <h2>Edits from this source</h2>${planTable(s.plans, { showSource: false })}`;
  const vid = $("#vid");
  if (t0) vid.addEventListener("loadedmetadata", () => { vid.currentTime = parseFloat(t0); }, { once: true });
  const sig = await api(`/api/sources/${id}/signals`);
  const items = [...longs.map((m) => ({ start: m.start, end: m.end, color: "#8a5cff", alpha: 0.18 })), ...shorts.map((m) => ({ start: m.start, end: m.end, color: "#ffcc33", alpha: 0.45 }))];
  const redraw = () => drawTimeline($("#tl"), sig, items, s.duration, (t) => { vid.currentTime = t; });
  redraw(); window.addEventListener("resize", redraw, { once: true });
  bindMomentButtons(main());
  $("#reanalyze").onclick = () => act(async () => { const r = await post(`/api/sources/${id}/analyze`, { auto_package: true }); watchJob(r.job_id); }, "Analysis started");
  $("#tr-file").onchange = (e) => act(async () => { const fd = new FormData(); fd.append("file", e.target.files[0]); await api(`/api/sources/${id}/transcript`, { method: "POST", body: fd }); route(); }, "Transcript imported — re-analyze to use it");
  $("#del-src").onclick = () => confirm("Delete this source and all its edits from the library? (Registered originals are never deleted from disk.)") && act(async () => { await del(`/api/sources/${id}`); go("#/dashboard"); });
  $("#mk-in").onclick = () => { $("#mk-s").value = fmt(vid.currentTime, 2); };
  $("#mk-out").onclick = () => { $("#mk-e").value = fmt(vid.currentTime, 2); };
  $("#mk-go").onclick = () => act(async () => { await post("/api/moments", { source_id: +id, start: parseT($("#mk-s").value), end: parseT($("#mk-e").value), title: $("#mk-title").value }); route(); }, "Moment added and scored");
  const lf = $("#lf-go");
  if (lf) lf.onclick = () => act(async () => {
    const ids = $$(".lf:checked").map((i) => +i.value);
    const r = await post(`/api/sources/${id}/longform`, { chapter_ids: ids, title: $("#lf-title").value, cold_open: $("#lf-cold").checked });
    go(`#/plan/${r.plan_id}`);
  }, "Long-form edit created");
});

/* ---------------------------------------------------------------- package */
page("package", async (id) => {
  const pkgs = await api(`/api/packages`);
  const p = pkgs.find((x) => String(x.id) === String(id));
  if (!p) { main().innerHTML = `<div class="empty">Package not found.</div>`; return; }
  main().innerHTML = `<h1>Content Package #${p.id}</h1>
  <p class="sub">${esc(p.moment_title || "")} · source <a href="#/source/${p.source_id}/${(p.start || 0).toFixed(1)}">${esc(p.source_title)}</a> ${fmt(p.start)}–${fmt(p.end)}</p>
  <div class="row" style="margin-bottom:12px">${platformChips(p.platform_scores)}</div>
  <div class="grid wide">${p.plans.map((pl) => `<div class="card">
    <div class="row"><b>${esc(PLATFORM_LABEL[pl.platform])}</b>${statusBadge(pl.status)}<span class="spacer"></span><span class="muted small">${dur(pl.duration)} · ${esc(pl.format?.aspect)}</span></div>
    ${pl.thumbnail ? `<img class="thumb" style="margin-top:8px;max-height:220px;object-fit:contain" src="/api/thumbnails/${pl.thumbnail.id}/file">` : ""}
    <div style="margin:8px 0"><a href="#/plan/${pl.id}">${esc(pl.title || "Untitled")}</a></div>
    <div class="small muted">${esc((pl.structure || "").replace("_", " "))} · layout ${esc(pl.layout_mode)} · captions ${pl.captions_on ? "on" : "off"} · ${esc(pl.profile_name || "")}</div>
    <div class="small muted">Source ${fmt(pl.source_range[0])}–${fmt(pl.source_range[1])}</div>
    <div class="row" style="margin-top:10px"><a class="btn primary" href="#/plan/${pl.id}">Review</a>
    ${pl.render?.status === "done" ? `<a class="btn" href="/api/renders/${pl.render.id}/file?download=true">Download</a>` : ""}</div></div>`).join("")}</div>
  <h2>Long-form edits using this moment</h2>${planTable(p.long_form)}
  <div class="row" style="margin-top:14px"><button id="pk-exp" class="primary">Render + export all in this package</button></div>`;
  $("#pk-exp").onclick = () => act(async () => { const r = await post("/api/export", { plan_ids: p.plans.map((x) => x.id) }); r.jobs.forEach((j) => watchJob(j.job_id)); }, "Export started");
});

/* ---------------------------------------------------------------- plan editor */
page("plan", async (id) => {
  const P = await api(`/api/plans/${id}`);
  const plan = P.plan, src = P.source;
  const lay = plan.layout || {};
  const cap = plan.captions || {};
  const vertical = plan.format.height > plan.format.width;
  const sel = (name, opts, val) => `<select data-k="${name}">${opts.map((o) => `<option ${o === val ? "selected" : ""}>${o}</option>`).join("")}</select>`;
  main().innerHTML = `
  <div class="row"><h1>${esc(P.title || "Untitled edit")}</h1>${statusBadge(P.status)}<span class="badge blue">${esc(PLATFORM_LABEL[P.platform])}</span><span class="badge">v${P.version}</span></div>
  <p class="sub">Source: <a href="#/source/${src.id}">${esc(src.title)}</a> · ${dur(P.duration)} · ${plan.format.width}×${plan.format.height} (${esc(plan.format.aspect)}) · ${esc((plan.structure || "").replace("_", " "))} · style: ${esc(P.profile?.name || "–")} · AI confidence ${P.confidence != null ? Math.round(P.confidence) : "–"} · engine ${esc(P.engine)}
  ${P.package_id ? ` · <a href="#/package/${P.package_id}">package #${P.package_id}</a>` : ""}</p>
  <div class="row" style="margin-bottom:14px">
    <button class="good" id="b-approve">Approve</button><button class="bad" id="b-reject">Reject</button><button id="b-draft" class="ghost">Back to draft</button>
    <button id="b-save" class="primary">Save changes</button>
    <button id="b-render">Render video</button><button id="b-premiere">Export to Premiere</button><button id="b-thumb">Thumbnail / cover</button>
    <span class="spacer"></span>
    <select id="b-dup-p">${ALL_PLATFORMS.map((p) => `<option value="${p}">${PLATFORM_LABEL[p]}</option>`).join("")}</select><button id="b-dup">Duplicate for platform</button>
    <select id="b-regen-prof">${PROFILES.map((p) => `<option value="${p.id}" ${p.id === P.profile_id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select><button id="b-regen">Regenerate</button>
  </div>
  ${(plan.notes || []).length ? `<div class="note" style="margin-bottom:12px">${plan.notes.map(esc).join("<br>")}</div>` : ""}
  <div class="cols">
    <div>
      <div class="tabs"><button class="on" data-tab="src">Source preview</button><button data-tab="out">Rendered output</button></div>
      <div id="tab-src"><video id="vid" controls preload="metadata" src="/api/sources/${src.id}/media"></video>
        <div class="row" style="margin-top:6px"><button id="play-edit" class="primary tiny">▶ Play the edit (skips cut sections)</button><span id="play-info" class="small muted"></span></div></div>
      <div id="tab-out" hidden>${P.renders.length ? P.renders.map((r) => `<div class="card" style="margin-bottom:10px"><div class="row small">${statusBadge(r.status)} v${r.plan_version} · ${r.width}×${r.height} · ${r.duration ? dur(r.duration) : ""} · ${esc(r.engine)} · ${ago(r.created_at)}
        ${r.status === "done" ? `<span class="spacer"></span><a class="btn tiny" href="/api/renders/${r.id}/file?download=true">Download MP4</a>` : ""}</div>
        ${r.status === "done" ? `<video controls preload="none" src="/api/renders/${r.id}/file" style="margin-top:8px;max-height:520px"></video>` : `<pre>${esc((r.log || "").slice(-800))}</pre>`}</div>`).join("") : `<div class="empty">Not rendered yet.</div>`}</div>

      <h2>Cut <span class="muted small">(source timestamps — the original is never modified)</span></h2>
      <div class="table-wrap"><table id="segs"><thead><tr><th>#</th><th>Role</th><th>Start</th><th></th><th>End</th><th></th><th>Len</th><th>On</th><th></th></tr></thead><tbody></tbody></table></div>
      <div class="row" style="margin-top:6px"><button class="tiny" id="seg-add">+ Add section at playhead</button><span class="small muted" id="seg-total"></span></div>
      <h3>Removed sections</h3><div id="removed" class="small"></div>

      <h2>Captions</h2>
      <div class="row"><label class="row" style="margin:0;color:var(--text)"><input type="checkbox" id="cap-on" ${cap.enabled ? "checked" : ""}> Burn in captions</label>
        <span>Style ${sel("style", ["bold_pop", "clean", "karaoke", "minimal", "boxed"], cap.style)}</span>
        <span>Position ${sel("position", ["lower", "lower_third", "center", "upper"], cap.position)}</span>
        <span>Animation ${sel("animation", ["none", "pop", "fade"], cap.animation)}</span>
        <span>Size <input type="number" step="0.05" min="0.5" max="2" id="cap-size" value="${cap.size ?? 1}" style="width:70px"></span>
        <label class="row" style="margin:0;color:var(--text)"><input type="checkbox" id="cap-upper" ${cap.uppercase ? "checked" : ""}> UPPERCASE</label>
        <button class="tiny" id="cap-rebuild">Rebuild from transcript</button><a class="btn tiny" href="/api/plans/${P.id}/captions.srt">SRT</a></div>
      ${cap.missing_transcript ? `<div class="note" style="margin-top:8px">No transcript for this source, so no caption text. Import an SRT/VTT on the source page or install faster-whisper.</div>` : ""}
      <div id="cap-lines" style="margin-top:10px;max-height:380px;overflow:auto"></div>
    </div>
    <div>
      <h2 style="margin-top:0">Framing</h2>
      <div class="card">
        <div class="row"><span>Layout ${sel("mode", ["crop", "split", "pip", "fit_blur", "full"], lay.mode)}</span>${lay.override ? '<span class="badge accent">manual override</span>' : ""}</div>
        <div class="small muted" style="margin:6px 0">${esc(lay.reason || "")}</div>
        <div class="frame-wrap"><img id="preview" alt="framing preview"></div>
        <div class="row small" style="margin-top:6px"><span>Preview at</span><input class="t" id="prev-t"><button class="tiny" id="prev-go">Update preview</button><button class="tiny" id="prev-ph">Use playhead</button></div>
        <h3>Main / gameplay region</h3>
        <div class="param-grid">${["cx", "cy", "zoom"].map((k) => `<div class="field"><label>${{ cx: "Horizontal center", cy: "Vertical center", zoom: "Zoom (share of source height)" }[k]} <span id="cv-${k}">${(lay.content?.[k] ?? 0.5).toFixed(2)}</span></label><input type="range" min="${k === "zoom" ? 0.2 : 0}" max="1" step="0.01" data-region="content" data-key="${k}" value="${lay.content?.[k] ?? (k === "zoom" ? 1 : 0.5)}"></div>`).join("")}</div>
        <h3>Face / facecam region <span class="muted small">(split-screen & picture-in-picture)</span></h3>
        <div class="param-grid">${["cx", "cy", "zoom"].map((k) => `<div class="field"><label>${{ cx: "Horizontal center", cy: "Vertical center", zoom: "Zoom" }[k]} <span id="fv-${k}">${(lay.face?.[k] ?? 0.5).toFixed(2)}</span></label><input type="range" min="0.05" max="1" step="0.01" data-region="face" data-key="${k}" value="${lay.face?.[k] ?? (k === "zoom" ? 0.3 : 0.8)}"></div>`).join("")}</div>
        <div class="row"><span>Split: main share <input type="number" id="split-ratio" step="0.02" min="0.3" max="0.8" value="${lay.split_ratio ?? 0.62}" style="width:70px"></span><span>Facecam ${sel("face_position", ["bottom", "top"], lay.face_position || "bottom")}</span></div>
      </div>
      <h2>Punch-ins</h2>
      <div id="zooms"></div><button class="tiny" id="zoom-add">+ Punch-in at current output time</button>
      <h2>Title & description</h2>
      <div class="field"><label>Title</label><input id="m-title" value="${esc(P.title || "")}"></div>
      <div class="field"><label>Description / caption</label><textarea id="m-desc">${esc(P.description || "")}</textarea></div>
      <div class="field"><label>Tags (comma separated)</label><input id="m-tags" value="${esc((P.tags || []).join(", "))}"></div>
      <div class="row"><span>Platform</span><select id="m-platform">${ALL_PLATFORMS.map((p) => `<option value="${p}" ${p === P.platform ? "selected" : ""}>${PLATFORM_LABEL[p]}</option>`).join("")}</select>
        <span>Style</span><select id="m-profile">${PROFILES.map((p) => `<option value="${p.id}" ${p.id === P.profile_id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></div>
      ${P.moment ? `<h2>Why this moment</h2><div class="card"><div class="row"><span class="score ${scoreCls(P.moment.overall_score)}">${Math.round(P.moment.overall_score || 0)}</span><div>${platformChips(P.moment.platform_scores)}</div></div><p class="small">${esc(P.moment.reasons || "")}</p>${scoreBars(P.moment.scores)}<p class="small muted">Recommendation, not a guarantee.</p></div>` : ""}
      ${P.siblings.length ? `<h2>Other versions in this package</h2>${planTable(P.siblings, { showSource: false })}` : ""}
      <h2>Thumbnails</h2><div class="grid">${P.thumbnails.map((t) => `<div><img class="thumb" src="/api/thumbnails/${t.id}/file">${t.selected ? '<span class="badge good">selected</span>' : ""}</div>`).join("") || '<span class="muted small">None yet.</span>'}</div>
      ${P.premiere_dir ? `<h2>Premiere hand-off</h2><div class="card small" id="prem-files">Loading…</div>` : ""}
      <h2>Processing log</h2><div class="card small">${P.operations.map((o) => `<div><span class="badge">${esc(o.engine)}</span> ${esc(o.operation)} — ${esc(o.detail)}</div>`).join("") || '<span class="muted">–</span>'}</div>
    </div>
  </div>`;

  // ---- state
  const state = { segments: JSON.parse(JSON.stringify(plan.segments)), removed: JSON.parse(JSON.stringify(plan.removed || [])),
    zooms: JSON.parse(JSON.stringify(plan.zooms || [])), lines: JSON.parse(JSON.stringify(cap.lines || [])),
    layout: JSON.parse(JSON.stringify(lay)), dirty: false };
  if (!state.layout.content) state.layout.content = { cx: 0.5, cy: 0.5, zoom: 1 };
  const vid = $("#vid");
  const markDirty = () => { state.dirty = true; $("#b-save").textContent = "Save changes •"; };

  function renderSegs() {
    const tb = $("#segs tbody");
    let total = 0;
    tb.innerHTML = state.segments.map((s, i) => {
      const len = s.end - s.start; if (s.enabled !== false) total += len;
      return `<tr class="seg-row ${s.role === "hook" ? "hook" : ""} ${s.enabled === false ? "disabled" : ""}"><td>${i + 1}</td><td>${esc(s.role)}</td>
      <td><input class="t" data-i="${i}" data-f="start" value="${fmt(s.start, 2)}"></td><td><button class="tiny ghost" data-ph="${i}:start" title="set from playhead">⏱</button></td>
      <td><input class="t" data-i="${i}" data-f="end" value="${fmt(s.end, 2)}"></td><td><button class="tiny ghost" data-ph="${i}:end" title="set from playhead">⏱</button></td>
      <td>${len.toFixed(1)}s</td><td><input type="checkbox" data-en="${i}" ${s.enabled !== false ? "checked" : ""}></td>
      <td><button class="tiny ghost" data-seek="${s.start}">▶</button><button class="tiny ghost" data-rm="${i}">✕</button></td></tr>`;
    }).join("");
    $("#seg-total").textContent = `Output length ${dur(total)} · target ${P.profile ? `${Math.round(P.profile.effective?.target_duration_min ?? 0)}–${Math.round(P.profile.effective?.target_duration_max ?? 0)}s` : ""}`;
    $$("input[data-f]", tb).forEach((inp) => inp.onchange = () => { const v = parseT(inp.value); if (v != null && !isNaN(v)) { state.segments[+inp.dataset.i][inp.dataset.f] = v; markDirty(); renderSegs(); } });
    $$("[data-ph]", tb).forEach((b) => b.onclick = () => { const [i, f] = b.dataset.ph.split(":"); state.segments[+i][f] = +vid.currentTime.toFixed(2); markDirty(); renderSegs(); });
    $$("[data-en]", tb).forEach((c) => c.onchange = () => { state.segments[+c.dataset.en].enabled = c.checked; markDirty(); renderSegs(); });
    $$("[data-rm]", tb).forEach((b) => b.onclick = () => { const s = state.segments.splice(+b.dataset.rm, 1)[0]; state.removed.push({ start: s.start, end: s.end, reason: "removed by you" }); markDirty(); renderSegs(); renderRemoved(); });
    $$("[data-seek]", tb).forEach((b) => b.onclick = () => { vid.currentTime = +b.dataset.seek; vid.play(); });
  }
  function renderRemoved() {
    $("#removed").innerHTML = state.removed.length ? state.removed.map((r, i) => `<div class="row" style="gap:6px"><span>${fmt(r.start, 2)}–${fmt(r.end, 2)} (${(r.end - r.start).toFixed(1)}s) <span class="muted">${esc(r.reason)}</span></span>
      <button class="tiny ghost" data-seekr="${r.start}">▶</button><button class="tiny" data-restore="${i}">Restore</button></div>`).join("") : '<span class="muted">Nothing removed.</span>';
    $$("[data-restore]").forEach((b) => b.onclick = () => { const r = state.removed.splice(+b.dataset.restore, 1)[0]; state.segments.push({ start: r.start, end: r.end, role: "body", enabled: true }); state.segments = sortSegs(state.segments); markDirty(); renderSegs(); renderRemoved(); });
    $$("[data-seekr]").forEach((b) => b.onclick = () => { vid.currentTime = +b.dataset.seekr; vid.play(); });
  }
  const sortSegs = (segs) => [...segs.filter((s) => s.role === "hook"), ...segs.filter((s) => s.role !== "hook").sort((a, b) => a.start - b.start)];
  function renderLines() {
    $("#cap-lines").innerHTML = state.lines.map((l, i) => `<div class="cap-line"><span class="small muted">${fmt(l.start, 2)}</span><input data-line="${i}" value="${esc(l.text)}"><button class="tiny ghost" data-lrm="${i}">✕</button></div>`).join("") || '<span class="muted small">No caption lines.</span>';
    $$("[data-line]").forEach((inp) => inp.onchange = () => { const l = state.lines[+inp.dataset.line]; l.text = inp.value; l.words = []; markDirty(); });
    $$("[data-lrm]").forEach((b) => b.onclick = () => { state.lines.splice(+b.dataset.lrm, 1); markDirty(); renderLines(); });
  }
  function renderZooms() {
    $("#zooms").innerHTML = state.zooms.length ? `<table><thead><tr><th>At (output)</th><th>Duration</th><th>Scale</th><th></th></tr></thead><tbody>${state.zooms.map((z, i) => `<tr><td><input class="t" data-z="${i}" data-zf="at" value="${z.at}"></td><td><input class="t" data-z="${i}" data-zf="duration" value="${z.duration}"></td><td><input class="t" data-z="${i}" data-zf="scale" value="${z.scale}"></td><td><button class="tiny ghost" data-zrm="${i}">✕</button></td></tr>`).join("")}</tbody></table>` : '<span class="muted small">No punch-ins.</span>';
    $$("[data-z]").forEach((inp) => inp.onchange = () => { state.zooms[+inp.dataset.z][inp.dataset.zf] = parseFloat(inp.value); markDirty(); });
    $$("[data-zrm]").forEach((b) => b.onclick = () => { state.zooms.splice(+b.dataset.zrm, 1); markDirty(); renderZooms(); });
  }
  renderSegs(); renderRemoved(); renderLines(); renderZooms();

  // ---- edit playback (plays the cut on the source video)
  let playing = null;
  $("#play-edit").onclick = () => {
    const segs = state.segments.filter((s) => s.enabled !== false);
    if (!segs.length) return;
    playing = { i: 0, segs }; vid.currentTime = segs[0].start; vid.play();
  };
  vid.addEventListener("timeupdate", () => {
    if (!playing) return;
    const s = playing.segs[playing.i];
    $("#play-info").textContent = `section ${playing.i + 1}/${playing.segs.length} (${s.role})`;
    if (vid.currentTime >= s.end - 0.05 || vid.currentTime < s.start - 0.5) {
      playing.i += 1;
      if (playing.i >= playing.segs.length) { vid.pause(); playing = null; $("#play-info").textContent = "end of edit"; return; }
      vid.currentTime = playing.segs[playing.i].start;
    }
  });
  vid.addEventListener("pause", () => { if (playing && vid.currentTime < playing.segs[playing.i].end - 0.2) playing = null; });

  // ---- tabs
  $$(".tabs button").forEach((b) => b.onclick = () => { $$(".tabs button").forEach((x) => x.classList.toggle("on", x === b)); $("#tab-src").hidden = b.dataset.tab !== "src"; $("#tab-out").hidden = b.dataset.tab !== "out"; });

  // ---- framing
  const firstSeg = state.segments.find((s) => s.role !== "hook") || state.segments[0];
  $("#prev-t").value = fmt(firstSeg ? (firstSeg.start + firstSeg.end) / 2 : 0, 2);
  const refreshPreview = async (save) => {
    if (save) await saveAll(true);
    $("#preview").src = `/api/plans/${id}/preview?t=${parseT($("#prev-t").value)}&_=${Date.now()}`;
  };
  refreshPreview(false);
  $("#prev-go").onclick = () => refreshPreview(state.dirty);
  $("#prev-ph").onclick = () => { $("#prev-t").value = fmt(vid.currentTime, 2); refreshPreview(state.dirty); };
  $$("[data-region]").forEach((r) => r.oninput = () => {
    const reg = r.dataset.region, k = r.dataset.key;
    state.layout[reg] = state.layout[reg] || { cx: 0.8, cy: 0.8, zoom: 0.3 };
    state.layout[reg][k] = parseFloat(r.value);
    $(`#${reg === "content" ? "cv" : "fv"}-${k}`).textContent = parseFloat(r.value).toFixed(2);
    markDirty();
  });
  $$("[data-region]").forEach((r) => r.onchange = () => refreshPreview(true));
  $('select[data-k="mode"]').onchange = (e) => { state.layout.mode = e.target.value; if (["split", "pip"].includes(e.target.value) && !state.layout.face) state.layout.face = { cx: 0.85, cy: 0.8, zoom: 0.3 }; markDirty(); refreshPreview(true); };
  $('select[data-k="face_position"]').onchange = (e) => { state.layout.face_position = e.target.value; markDirty(); refreshPreview(true); };
  $("#split-ratio").onchange = (e) => { state.layout.split_ratio = parseFloat(e.target.value); markDirty(); refreshPreview(true); };
  $$('select[data-k="style"],select[data-k="position"],select[data-k="animation"],#cap-on,#cap-upper,#cap-size').forEach((el) => el.onchange = markDirty);
  $$("#m-title,#m-desc,#m-tags,#m-platform,#m-profile").forEach((el) => el.onchange = markDirty);

  $("#seg-add").onclick = () => { const t = vid.currentTime; state.segments.push({ start: +t.toFixed(2), end: +(t + 3).toFixed(2), role: "body", enabled: true }); state.segments = sortSegs(state.segments); markDirty(); renderSegs(); };
  $("#zoom-add").onclick = () => {
    // map the playhead (source time) to output time
    let out = 0, at = null;
    for (const s of state.segments.filter((x) => x.enabled !== false)) { if (s.role !== "hook" && vid.currentTime >= s.start && vid.currentTime <= s.end) { at = out + vid.currentTime - s.start; break; } out += s.end - s.start; }
    if (at == null) { toast("Playhead is not inside the edit", true); return; }
    state.zooms.push({ at: +at.toFixed(2), duration: 1.5, scale: 1.12 }); state.zooms.sort((a, b) => a.at - b.at); markDirty(); renderZooms();
  };

  async function saveAll(quiet) {
    const body = {
      segments: state.segments, removed: state.removed, zooms: state.zooms, layout: state.layout,
      captions: { enabled: $("#cap-on").checked, style: $('select[data-k="style"]').value, position: $('select[data-k="position"]').value,
        animation: $('select[data-k="animation"]').value, size: parseFloat($("#cap-size").value), uppercase: $("#cap-upper").checked, lines: state.lines },
      title: $("#m-title").value, description: $("#m-desc").value, tags: $("#m-tags").value.split(",").map((x) => x.trim()).filter(Boolean),
    };
    if ($("#m-platform").value !== P.platform) body.platform = $("#m-platform").value;
    if (+$("#m-profile").value !== P.profile_id) body.profile_id = +$("#m-profile").value;
    await patch(`/api/plans/${id}`, body);
    state.dirty = false; $("#b-save").textContent = "Save changes";
    if (!quiet) toast("Saved");
  }
  $("#b-save").onclick = () => act(async () => { await saveAll(); route(); });
  $("#cap-rebuild").onclick = () => act(async () => { await saveAll(true); await post(`/api/plans/${id}/captions/refresh`); route(); }, "Captions rebuilt from transcript");
  const setStatus = (s) => act(async () => { if (state.dirty) await saveAll(true); await post(`/api/plans/${id}/status`, { status: s }); route(); }, `Marked ${s}`);
  $("#b-approve").onclick = () => setStatus("approved");
  $("#b-reject").onclick = () => setStatus("rejected");
  $("#b-draft").onclick = () => setStatus("draft");
  $("#b-render").onclick = () => act(async () => { if (state.dirty) await saveAll(true); const r = await post(`/api/plans/${id}/render`); watchJob(r.job_id); }, "Render started (FFmpeg)");
  $("#b-premiere").onclick = () => act(async () => { if (state.dirty) await saveAll(true); const r = await post(`/api/plans/${id}/premiere`); toast(`Premiere sequence written to ${r.dir}`); route(); });
  $("#b-thumb").onclick = () => act(async () => { await post("/api/thumbnails", { plan_id: +id }); route(); }, "Thumbnail concepts generated");
  $("#b-dup").onclick = () => act(async () => { const r = await post(`/api/plans/${id}/duplicate`, { platform: $("#b-dup-p").value }); go(`#/plan/${r.plan_id}`); }, "Duplicated for platform");
  $("#b-regen").onclick = () => confirm("Regenerate this edit with the selected style? Your manual changes stay in the previous version.") && act(async () => { const r = await post(`/api/plans/${id}/regenerate`, { profile_id: +$("#b-regen-prof").value }); go(`#/plan/${r.plan_id}`); }, "Regenerated");
  if (P.premiere_dir) api(`/api/files/list?path=${encodeURIComponent(P.premiere_dir)}`).then((fs) => { $("#prem-files").innerHTML = `<div class="muted">${esc(P.premiere_dir)}</div>` + fs.map((f) => `<div><a href="${fileUrl(f.path, true)}">${esc(f.name)}</a></div>`).join("") + `<p class="muted">In Premiere: File › Import › sequence.xml (or run import_into_premiere.jsx). The sequence references your original file at its source timecode.</p>`; });
  window.onbeforeunload = () => state.dirty ? "unsaved" : undefined;
});

/* ---------------------------------------------------------------- studios */
page("longform", async () => {
  const sources = (await api("/api/sources")).filter((s) => s.duration > 180);
  const plans = await api("/api/plans?form=long");
  main().innerHTML = `<h1>Long-Form Studio</h1><p class="sub">Edited YouTube videos from streams: story, context and personality first — chronology preserved, dead air removed, no constant montage.</p>
  ${uploader({ title: "Drop a livestream recording or VOD (2–6 h is fine)" })}${registerPathForm()}
  <h2>Recordings</h2>
  ${sources.length ? `<div class="table-wrap"><table><thead><tr><th>Recording</th><th>Length</th><th>Status</th><th>Long-form segments</th><th>Short moments</th><th>Edits</th><th></th></tr></thead><tbody>${sources.map((s) => `<tr><td><a href="#/source/${s.id}">${esc(s.title)}</a><div class="small muted">${esc(s.kind)} · ${esc(s.category)}</div></td><td>${dur(s.duration)}</td><td>${statusBadge(s.status)}</td><td>${s.n_chapters}</td><td>${s.n_moments}</td><td>${s.n_plans}</td><td><a class="btn tiny" href="#/source/${s.id}">Build video</a></td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">No long recordings yet.</div>`}
  <h2>Long-form edits</h2>${planTable(plans)}`;
});

page("shorts", async () => {
  const moments = await api("/api/moments?kind=short");
  const plans = await api("/api/plans?platform=youtube_shorts,tiktok,instagram_reels&status=draft");
  main().innerHTML = `<h1>Shorts Studio</h1><p class="sub">The strongest standalone moments across all your recordings, scored per platform. Fewer, better clips — weak moments are filtered out on purpose.</p>
  <div class="row"><span class="muted small">Minimum score</span><input type="range" id="minsc" min="0" max="95" value="0"><span id="minsc-v">0</span></div>
  <div class="grid wide" id="mlist" style="margin-top:10px">${moments.map(momentCard).join("") || '<div class="empty">No moments yet — upload a recording.</div>'}</div>
  <h2>Short-form edits waiting for review</h2>${planTable(plans)}`;
  bindMomentButtons(main());
  $("#minsc").oninput = (e) => { $("#minsc-v").textContent = e.target.value; $("#mlist").innerHTML = moments.filter((m) => m.overall_score >= +e.target.value).map(momentCard).join(""); bindMomentButtons($("#mlist")); };
});

page("twitch", async () => {
  const clips = await api("/api/sources?kind=twitch_clip,clip,gameplay");
  const plans = await api(`/api/plans`);
  const byS = {}; plans.forEach((p) => { (byS[p.source_id] = byS[p.source_id] || []).push(p); });
  main().innerHTML = `<h1>Twitch Clip Studio</h1><p class="sub">Upload Twitch clips (one or many). Each is analyzed — dead setup trimmed, strongest moment found, reaction kept, ended on the payoff — and turned into separate TikTok, Shorts, Reels and X edits that all reference the same original clip.</p>
  ${uploader({ kind: "twitch_clip", showKind: false, title: "Drop Twitch clips here (multiple allowed)" })}
  <h2>Clips</h2>
  ${clips.length ? clips.map((c) => {
    const ps = byS[c.id] || [];
    return `<div class="card" style="margin-bottom:12px"><div class="row"><b><a href="#/source/${c.id}">${esc(c.title)}</a></b><span class="muted small">${dur(c.duration)} · ${c.width}×${c.height}</span>${statusBadge(c.status)}<span class="spacer"></span>${ps[0]?.package_id ? `<a class="btn tiny" href="#/package/${ps[0].package_id}">Package</a>` : ""}</div>
    ${ps.length ? `<div class="table-wrap" style="margin-top:8px"><table><tbody>${ps.map((p) => `<tr><td><a href="#/plan/${p.id}">${esc(PLATFORM_LABEL[p.platform])}</a></td><td>${fmt(p.source_range[0])}–${fmt(p.source_range[1])} of original</td><td>${dur(p.duration)}</td><td>${esc(p.format?.aspect)} · ${esc(p.layout_mode)}</td><td>${esc((p.structure || "").replace("_", " "))}</td><td>${statusBadge(p.status)}</td></tr>`).join("")}</tbody></table></div>` : `<div class="small muted" style="margin-top:6px">${c.status === "analyzed" ? "No edits." : "Analyzing…"}</div>`}</div>`;
  }).join("") : `<div class="empty">No clips yet.</div>`}`;
});

page("social", async (platform) => {
  platform = platform || "tiktok";
  const status = new URLSearchParams(location.hash.split("?")[1] || "").get("status") || "draft";
  const plans = await api(`/api/plans?platform=${platform}${status !== "all" ? `&status=${status}` : ""}`);
  main().innerHTML = `<h1>Social Clip Studio</h1><p class="sub">Review queue per destination. Each version is its own edit — not a resize.</p>
  <div class="tabs">${ALL_PLATFORMS.map((p) => `<button class="${p === platform ? "on" : ""}" data-p="${p}">${PLATFORM_LABEL[p]}</button>`).join("")}</div>
  <div class="chips" style="margin-bottom:12px">${["draft", "approved", "rejected", "all"].map((s) => `<span class="chip ${s === status ? "on" : ""}" data-s="${s}">${s}</span>`).join("")}</div>
  ${planTable(plans)}`;
  $$("[data-p]").forEach((b) => b.onclick = () => go(`#/social/${b.dataset.p}?status=${status}`));
  $$("[data-s]").forEach((b) => b.onclick = () => go(`#/social/${platform}?status=${b.dataset.s}`));
});

/* ---------------------------------------------------------------- thumbnails */
page("thumbnails", async (planId) => {
  const plans = await api("/api/plans");
  const style = await api("/api/thumbnail-style");
  const thumbs = await api("/api/thumbnails");
  const cur = planId ? plans.find((p) => String(p.id) === String(planId)) : null;
  main().innerHTML = `<h1>Thumbnail Studio</h1><p class="sub">Concepts are built from real frames of your footage (sharpness, exposure, your face, and how loud the moment was). Every concept ships with layer PNGs and a Photoshop script that builds an editable layered PSD.</p>
  <div class="cols"><div>
    <div class="row"><span>Edit</span><select id="tp" style="flex:1"><option value="">Choose an edit…</option>${plans.map((p) => `<option value="${p.id}" ${cur && cur.id === p.id ? "selected" : ""}>${esc(PLATFORM_LABEL[p.platform])} — ${esc(p.title || "Untitled")} (${p.status})</option>`).join("")}</select></div>
    <div id="cands" style="margin-top:12px"></div>
    <h2>Generated</h2>
    <div class="grid">${thumbs.filter((t) => !cur || t.plan_id === cur.id).map((t) => `<div class="card"><img class="thumb" src="/api/thumbnails/${t.id}/file"><div class="small" style="margin-top:6px">${esc(t.kind)} · frame ${fmt(t.frame_time)} · ${t.selected ? '<span class="badge good">selected</span>' : `<button class="tiny" data-sel="${t.id}">Select</button>`}</div>
      <div class="small"><a href="${fileUrl(t.path, true)}">PNG</a> · ${t.layers?.jsx ? `<a href="${fileUrl(t.layers.jsx, true)}">Photoshop .jsx</a>` : ""} · <a href="#/plan/${t.plan_id}">edit</a></div></div>`).join("") || '<div class="empty">No thumbnails yet.</div>'}</div>
  </div><div>
    <h2 style="margin-top:0">Jimbo4prezz Thumbnail Style Profile</h2>
    <div class="card"><div class="kv">${Object.entries(style).map(([k, v]) => `<div>${esc(k.replace(/_/g, " "))}</div><div>${esc(v)}</div>`).join("")}</div>
    <p class="small muted">Learned from your thumbnail references (written notes win over what's measured). <a href="#/references">Add a thumbnail reference</a>.</p></div>
    <div class="note" style="margin-top:12px">Photoshop: File › Scripts › Browse… → pick <code>build_thumbnail.jsx</code>. It assembles the background frame, subject cut-out and a live text layer, and saves <code>thumbnail.psd</code> next to it.</div>
  </div></div>`;
  $("#tp").onchange = (e) => go(`#/thumbnails/${e.target.value}`);
  $$("[data-sel]").forEach((b) => b.onclick = () => act(async () => { await post(`/api/thumbnails/${b.dataset.sel}/select`); route(); }));
  if (cur) {
    $("#cands").innerHTML = `<div class="card"><h3>Pick a frame</h3><div class="row"><input id="tt" placeholder="Thumbnail text (2–4 words, true to the content)" style="flex:1" value=""><select id="tk"><option value="">auto size</option><option value="youtube">YouTube 1280×720</option><option value="cover">Vertical cover 1080×1920</option></select><button class="primary" id="tauto">Auto-pick best frames</button></div><div class="grid" id="cgrid" style="margin-top:10px"><span class="muted">Scoring frames…</span></div></div>`;
    $("#tauto").onclick = () => act(async () => { await post("/api/thumbnails", { plan_id: cur.id, text: $("#tt").value || undefined, kind: $("#tk").value || undefined }); route(); }, "Generated");
    const cands = await api(`/api/thumbnails/candidates?plan_id=${cur.id}`);
    $("#cgrid").innerHTML = cands.map((c) => `<div><img class="thumb" src="/api/sources/${cur.source_id}/frame?t=${c.t}&w=360"><div class="row small" style="margin-top:4px">${fmt(c.t)} · score ${c.score}${c.faces.length ? " · face" : ""}<span class="spacer"></span><button class="tiny" data-ft="${c.t}">Use</button></div></div>`).join("");
    $$("[data-ft]").forEach((b) => b.onclick = () => act(async () => { await post("/api/thumbnails", { plan_id: cur.id, frame_time: +b.dataset.ft, text: $("#tt").value || undefined, kind: $("#tk").value || undefined }); route(); }, "Generated"));
  }
});

/* ---------------------------------------------------------------- references */
page("references", async () => {
  const refs = await api("/api/references");
  const levels = STATUS.levels, aspects = STATUS.aspects;
  const levelLabel = { creator: "CREATOR REFERENCE", video: "VIDEO REFERENCE", section: "Specific section", technique: "Specific technique", clip: "TWITCH CLIP REFERENCE", short_form: "SHORT-FORM REFERENCE", thumbnail: "THUMBNAIL REFERENCE", caption: "CAPTION REFERENCE", premiere: "PREMIERE PRO REFERENCE", instruction: "MY PERSONAL INSTRUCTIONS" };
  main().innerHTML = `<h1>Editing References</h1><p class="sub">Show the studio what you want. Each reference is stored separately (creator, video, section, technique, clip, thumbnail…) and only teaches the aspects you pick. Your written explanation always wins over what's measured. Principles are extracted — nothing about the creator's identity, branding or assets is copied.</p>
  <div class="card"><h3>Add a reference</h3>
    <div class="param-grid">
      <div class="field"><label>Type</label><select id="r-level">${levels.map((l) => `<option value="${l}">${levelLabel[l] || l}</option>`).join("")}</select></div>
      <div class="field"><label>Name</label><input id="r-name" placeholder="e.g. Agent – Twitch clip cuts"></div>
      <div class="field"><label>Creator</label><input id="r-creator" placeholder="Creator / channel"></div>
      <div class="field"><label>Link (optional)</label><input id="r-url" placeholder="https://…"></div>
      <div class="field"><label>Example file (video or image)</label><input type="file" id="r-file" accept="video/*,image/*"></div>
      <div class="field"><label>Section (optional)</label><div class="row"><input class="t" id="r-s" placeholder="00:00"><span>–</span><input class="t" id="r-e" placeholder="00:30"></div></div>
    </div>
    <div class="field"><label>What do you like about it? (most important)</label><textarea id="r-notes" placeholder="“I like how quickly he gets to the funny moment.” “I like the zooms but I don't want them overused.” “Keep my reactions in.”"></textarea></div>
    <div class="field"><label>Teaches these aspects (none = all)</label><div class="checks">${aspects.map((a) => `<label><input type="checkbox" class="r-asp" value="${a}">${a}</label>`).join("")}</div></div>
    <div class="field"><label>Use for platforms</label><div class="checks">${ALL_PLATFORMS.map((p) => `<label><input type="checkbox" class="r-plat" value="${p}">${PLATFORM_LABEL[p]}</label>`).join("")}</div></div>
    <div class="field"><label>Use for content types</label><div class="checks">${STATUS.content_types.map((c) => `<label><input type="checkbox" class="r-ct" value="${c}">${c.replace("_", " ")}</label>`).join("")}</div></div>
    <div class="field"><label>Apply to style profiles (none = auto-match platforms/content types)</label><div class="checks">${PROFILES.map((p) => `<label><input type="checkbox" class="r-prof" value="${p.id}">${esc(p.name)}</label>`).join("")}</div></div>
    <button class="primary" id="r-add">Add &amp; analyze</button>
  </div>
  <h2>Your references (${refs.length})</h2>
  ${refs.map((r) => {
    const ex = r.extracted || {};
    return `<div class="card" style="margin-bottom:12px"><div class="row"><span class="badge accent">${esc(levelLabel[r.level] || r.level)}</span><b>${esc(r.name)}</b><span class="muted small">${esc(r.creator || "")}</span>${statusBadge(r.status)}<span class="spacer"></span>
      ${r.path ? `<a class="btn tiny" href="/api/references/${r.id}/file" target="_blank">File</a>` : ""}${r.url ? `<a class="btn tiny" href="${esc(r.url)}" target="_blank" rel="noopener">Link</a>` : ""}
      <button class="tiny" data-toggle="${r.id}" data-st="${r.status === "disabled" ? "analyzed" : "disabled"}">${r.status === "disabled" ? "Enable" : "Disable"}</button><button class="tiny ghost" data-rdel="${r.id}">Delete</button></div>
      <div class="small muted" style="margin:4px 0">${r.section_start != null ? `Section ${fmt(r.section_start, 0)}–${fmt(r.section_end, 0)} · ` : ""}Platforms: ${esc((r.platforms || []).map((p) => PLATFORM_LABEL[p]).join(", ") || "any")} · Content: ${esc((r.content_types || []).join(", ") || "any")} · Aspects: ${esc(r.aspects || "all")} · Profiles: ${esc((r.profile_names || []).join(", ") || "—")}</div>
      <textarea data-notes="${r.id}" style="min-height:60px">${esc(r.notes)}</textarea><button class="tiny" data-savenotes="${r.id}">Save notes &amp; re-analyze</button>
      <div class="cols" style="margin-top:10px">
        <div><h3>From your notes</h3>${ex.note_params ? `<div class="kv">${Object.entries(ex.note_params).map(([k, v]) => `<div>${esc(k)}</div><div><b>${esc(v)}</b></div>`).join("")}</div>` : ""}
          ${(ex.interpretation || []).map((i) => `<div class="small">• ${esc(i.because ? `${i.param} → ${i.value}: ${i.because}` : i.observation || i.rule)}</div>`).join("")}
          ${ex.thumbnail_notes ? `<div class="kv">${Object.entries(ex.thumbnail_notes).map(([k, v]) => `<div>${esc(k)}</div><div>${esc(v)}</div>`).join("")}</div>` : ""}</div>
        <div><h3>Measured from the example</h3>${ex.measured ? `<div class="kv small">${["duration", "aspect", "cuts_per_minute", "avg_shot_seconds", "longest_pause", "silence_ratio", "face_presence"].map((k) => `<div>${k.replace(/_/g, " ")}</div><div>${esc(ex.measured[k])}</div>`).join("")}<div>captions</div><div>${ex.measured.captions?.likely ? "yes, " + esc(ex.measured.captions.position) : "not detected"}</div><div>split-screen</div><div>${ex.measured.split_screen_seam ? "yes" : "no"}</div></div>${(ex.media_why || []).map((w) => `<div class="small">• ${esc(w)}</div>`).join("")}` : ex.thumbnail_measured ? `<pre>${esc(JSON.stringify(ex.thumbnail_media, null, 1))}</pre>` : '<span class="muted small">No file — notes only.</span>'}
          ${ex.error ? `<div class="note">${esc(ex.error)}</div>` : ""}</div>
      </div></div>`;
  }).join("") || '<div class="empty">No references yet. Start with a Twitch clip whose cutting you like and write what you like about it.</div>'}`;
  $("#r-add").onclick = () => act(async () => {
    const fd = new FormData();
    const vals = (cls) => JSON.stringify($$(cls + ":checked").map((i) => i.value));
    fd.append("level", $("#r-level").value); fd.append("name", $("#r-name").value); fd.append("creator", $("#r-creator").value);
    fd.append("url", $("#r-url").value); fd.append("notes", $("#r-notes").value); fd.append("section_start", $("#r-s").value); fd.append("section_end", $("#r-e").value);
    fd.append("platforms", vals(".r-plat")); fd.append("content_types", vals(".r-ct")); fd.append("aspects", $$(".r-asp:checked").map((i) => i.value).join(","));
    fd.append("profile_ids", JSON.stringify($$(".r-prof:checked").map((i) => +i.value)));
    if ($("#r-file").files[0]) fd.append("file", $("#r-file").files[0]);
    const r = await api("/api/references", { method: "POST", body: fd });
    watchJob(r.job_id, () => route()); route();
  }, "Reference added — analyzing");
  $$("[data-rdel]").forEach((b) => b.onclick = () => confirm("Delete this reference?") && act(async () => { await del(`/api/references/${b.dataset.rdel}`); route(); }));
  $$("[data-toggle]").forEach((b) => b.onclick = () => act(async () => { await patch(`/api/references/${b.dataset.toggle}`, { status: b.dataset.st, reanalyze: false }); route(); }));
  $$("[data-savenotes]").forEach((b) => b.onclick = () => act(async () => { const r = await patch(`/api/references/${b.dataset.savenotes}`, { notes: $(`[data-notes="${b.dataset.savenotes}"]`).value }); setTimeout(route, 1500); }, "Re-analyzing"));
});

/* ---------------------------------------------------------------- profiles */
page("profiles", async (pid) => {
  PROFILES = await api("/api/profiles");
  const schema = (await api("/api/profiles/schema")).params;
  const cur = PROFILES.find((p) => String(p.id) === String(pid)) || PROFILES[0];
  const eff = cur.effective, L = eff._layers;
  const input = (k, spec, v) => spec.type === "bool" ? `<input type="checkbox" data-p="${k}" ${v ? "checked" : ""}>`
    : spec.type === "enum" ? `<select data-p="${k}">${spec.options.map((o) => `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select>`
      : `<input type="number" step="any" data-p="${k}" value="${v}">`;
  main().innerHTML = `<h1>Style Profiles</h1><p class="sub">Saved editing styles. Effective value = your base settings ← reference examples ← your written instructions ← what the studio learned from your approvals and manual edits.</p>
  <div class="row" style="margin-bottom:12px">${PROFILES.map((p) => `<a class="chip ${p.id === cur.id ? "on" : ""}" href="#/profiles/${p.id}">${esc(p.name)}</a>`).join("")}</div>
  <div class="cols"><div class="card">
    <div class="row"><h3 style="margin:0">${esc(cur.name)}</h3><span class="badge blue">${esc(PLATFORM_LABEL[cur.platform])}</span>${cur.builtin ? '<span class="badge">built-in</span>' : ""}<span class="spacer"></span><button class="primary" id="pf-save">Save</button>${!cur.builtin ? `<button class="ghost tiny" id="pf-del">Delete</button>` : ""}</div>
    <div class="field" style="margin-top:10px"><label>Description</label><input id="pf-desc" value="${esc(cur.description)}"></div>
    <div class="field"><label>Content types</label><div class="checks">${STATUS.content_types.map((c) => `<label><input type="checkbox" class="pf-ct" value="${c}" ${(cur.content_types || []).includes(c) ? "checked" : ""}>${c.replace("_", " ")}</label>`).join("")}</div></div>
    <div class="param-grid">${Object.entries(schema).map(([k, spec]) => `<div class="field"><label>${esc(spec.label)}</label>${input(k, spec, (cur.params || {})[k] ?? eff[k])}
      ${JSON.stringify(eff[k]) !== JSON.stringify((cur.params || {})[k]) ? `<div class="eff">effective: ${esc(typeof eff[k] === "number" ? Math.round(eff[k] * 100) / 100 : eff[k])}</div>` : ""}</div>`).join("")}</div>
  </div><div>
    <div class="card"><h3>Where the effective style comes from</h3>
      <div class="layer"><b>References:</b> ${L.references.map((r) => `${esc(r.name)} (${esc(r.level)})`).join(", ") || "none linked"}</div>
      <div class="layer"><b>Measured from examples:</b> ${esc(JSON.stringify(L.inferred))}</div>
      <div class="layer"><b>Your written instructions:</b> ${esc(JSON.stringify(L.written))}</div>
      <div class="layer"><b>Learned from your edits:</b> ${esc(JSON.stringify(L.learned))} ${cur.learned?.counts ? `<span class="muted">(${esc(JSON.stringify(cur.learned.counts))})</span>` : ""}</div>
      <button class="tiny" id="pf-reset" style="margin-top:8px">Reset learned adjustments</button></div>
    <div class="card" style="margin-top:12px"><h3>New profile</h3>
      <div class="field"><label>Name</label><input id="np-name" placeholder="e.g. Jimbo4prezz – Gaming TikTok"></div>
      <div class="field"><label>Platform</label><select id="np-plat">${ALL_PLATFORMS.map((p) => `<option value="${p}">${PLATFORM_LABEL[p]}</option>`).join("")}</select></div>
      <div class="field"><label>Start from</label><select id="np-copy"><option value="">Platform default</option>${PROFILES.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("")}</select></div>
      <div class="field"><label>Content types</label><div class="checks">${STATUS.content_types.map((c) => `<label><input type="checkbox" class="np-ct" value="${c}">${c.replace("_", " ")}</label>`).join("")}</div></div>
      <button class="primary" id="np-go">Create profile</button>
      <p class="small muted">A non-built-in profile that matches a platform + content type is used automatically for new edits of that kind.</p></div>
  </div></div>`;
  $("#pf-save").onclick = () => act(async () => {
    const params = {};
    $$("[data-p]").forEach((el) => { params[el.dataset.p] = el.type === "checkbox" ? el.checked : el.type === "number" ? parseFloat(el.value) : el.value; });
    await patch(`/api/profiles/${cur.id}`, { params, description: $("#pf-desc").value, content_types: $$(".pf-ct:checked").map((i) => i.value) });
    route();
  }, "Profile saved");
  $("#pf-reset").onclick = () => act(async () => { await patch(`/api/profiles/${cur.id}`, { reset_learned: true }); route(); }, "Learned adjustments cleared");
  const d = $("#pf-del"); if (d) d.onclick = () => confirm("Delete profile?") && act(async () => { await del(`/api/profiles/${cur.id}`); go("#/profiles"); });
  $("#np-go").onclick = () => act(async () => { const p = await post("/api/profiles", { name: $("#np-name").value, platform: $("#np-plat").value, copy_from: +$("#np-copy").value || null, content_types: $$(".np-ct:checked").map((i) => i.value) }); go(`#/profiles/${p.id}`); }, "Profile created");
});

/* ---------------------------------------------------------------- library */
page("library", async (type = "all") => {
  const items = await api(`/api/library?type=${type}`);
  const types = ["all", "source", "moment", "plan", "render", "thumbnail", "caption", "premiere", "reference", "profile"];
  const link = (it) => ({ source: `#/source/${it.id}`, moment: `#/source/${it.source_id}`, plan: `#/plan/${it.id}`, render: `#/plan/${it.plan_id}`, thumbnail: `#/plan/${it.plan_id}`, reference: "#/references", profile: `#/profiles/${it.id}` }[it.type]);
  main().innerHTML = `<h1>Content Library</h1><p class="sub">Every asset is a database record linked to its source and project.</p>
  <div class="chips" style="margin-bottom:12px">${types.map((t) => `<a class="chip ${t === type ? "on" : ""}" href="#/library/${t}">${t}</a>`).join("")}<input id="lq" placeholder="Search…" style="margin-left:auto"></div>
  <div class="table-wrap"><table><thead><tr><th>Type</th><th>Title</th><th>Detail</th><th>Source</th><th>Added</th><th></th></tr></thead><tbody id="lb">
  ${items.map((it) => `<tr data-q="${esc((it.title + " " + it.detail).toLowerCase())}"><td><span class="badge">${esc(it.type)}</span> <span class="small muted">${esc(it.subtype || "")}</span></td>
    <td>${link(it) ? `<a href="${link(it)}">${esc(it.title || "–")}</a>` : esc(it.title)}</td><td class="small">${esc(it.detail)}</td>
    <td>${it.source_id ? `<a href="#/source/${it.source_id}">#${it.source_id}</a>` : ""}</td><td class="small muted">${it.created_at ? ago(it.created_at) : ""}</td>
    <td>${it.type === "render" ? `<a class="btn tiny" href="/api/renders/${it.id}/file?download=true">MP4</a>` : it.path && ["thumbnail", "caption"].includes(it.type) ? `<a class="btn tiny" href="${fileUrl(it.path, true)}">Download</a>` : ""}</td></tr>`).join("")}
  </tbody></table></div>`;
  $("#lq").oninput = (e) => { const q = e.target.value.toLowerCase(); $$("#lb tr").forEach((tr) => { tr.hidden = q && !tr.dataset.q.includes(q); }); };
});

/* ---------------------------------------------------------------- export center */
page("export", async () => {
  const status = new URLSearchParams(location.hash.split("?")[1] || "").get("status") || "approved";
  const plans = await api(`/api/plans${status !== "all" ? `?status=${status}` : ""}`);
  main().innerHTML = `<h1>Content Export Center</h1><p class="sub">Render with FFmpeg, write Premiere Pro sequences + caption files, and generate thumbnails/covers — for one platform, several, or everything approved.</p>
  <div class="row" style="margin-bottom:10px"><div class="chips">${["approved", "draft", "rejected", "all"].map((s) => `<a class="chip ${s === status ? "on" : ""}" href="#/export?status=${s}">${s}</a>`).join("")}</div><span class="spacer"></span>
    <div class="checks">${ALL_PLATFORMS.map((p) => `<label><input type="checkbox" class="xp" value="${p}" checked>${PLATFORM_LABEL[p]}</label>`).join("")}</div>
    <label class="row small" style="margin:0;color:var(--text)"><input type="checkbox" id="x-prem" checked>Premiere XML</label><label class="row small" style="margin:0;color:var(--text)"><input type="checkbox" id="x-thumb" checked>Thumbnail</label>
    <button id="x-sel">Export selected</button><button class="primary" id="x-all">Export all approved (checked platforms)</button></div>
  <div class="table-wrap"><table><thead><tr><th><input type="checkbox" id="x-chk"></th><th>Edit</th><th>Source</th><th>Content type</th><th>Platform</th><th>Format</th><th>Resolution</th><th>Duration</th><th>Source timestamps</th><th>Editing style</th><th>AI confidence</th><th>Review</th><th>Thumbnail / cover</th><th>Captions</th><th>Premiere</th><th>Export method</th></tr></thead><tbody>
  ${plans.map((p) => `<tr><td><input type="checkbox" class="xr" value="${p.id}" data-p="${p.platform}"></td>
    <td><a href="#/plan/${p.id}">${esc(p.title || "Untitled")}</a></td>
    <td><a href="#/source/${p.source_id}">${esc(p.source_title)}</a><div class="small muted">${esc(p.source_kind)}</div></td>
    <td>${esc(p.content_type || "–")} <span class="muted small">${esc(p.form)}</span></td><td>${esc(PLATFORM_LABEL[p.platform])}</td>
    <td>${esc(p.format?.aspect)}</td><td>${p.format?.width}×${p.format?.height}</td><td>${dur(p.duration)}</td>
    <td class="small">${fmt(p.source_range[0])}–${fmt(p.source_range[1])}</td><td class="small">${esc(p.profile_name || "–")}</td>
    <td>${p.confidence != null ? Math.round(p.confidence) : "–"}</td><td>${statusBadge(p.status)}</td>
    <td>${p.thumbnail ? `<img src="/api/thumbnails/${p.thumbnail.id}/file" style="height:44px;border-radius:4px">` : '<span class="muted small">–</span>'}</td>
    <td>${p.captions_on ? `<a class="small" href="/api/plans/${p.id}/captions.srt">SRT</a>` : '<span class="muted small">off</span>'}</td>
    <td>${p.premiere ? '<span class="badge good">sequence</span>' : '<span class="muted small">–</span>'}</td>
    <td class="small">${p.render?.status === "done" ? `<a class="btn tiny" href="/api/renders/${p.render.id}/file?download=true">MP4</a> FFmpeg` : p.render ? statusBadge(p.render.status) : "FFmpeg render"}${p.premiere ? " + Premiere XML" : ""}</td></tr>`).join("") || `<tr><td colspan="16"><div class="empty">Nothing ${status}.</div></td></tr>`}
  </tbody></table></div>`;
  $("#x-chk").onchange = (e) => $$(".xr").forEach((c) => { c.checked = e.target.checked; });
  const opts = () => ({ premiere: $("#x-prem").checked, thumbnail: $("#x-thumb").checked });
  $("#x-sel").onclick = () => act(async () => { const ids = $$(".xr:checked").map((c) => +c.value); const r = await post("/api/export", { plan_ids: ids, ...opts() }); r.jobs.forEach((j) => watchJob(j.job_id)); }, "Export started");
  $("#x-all").onclick = () => act(async () => { const r = await post("/api/export", { scope: "approved", platforms: $$(".xp:checked").map((c) => c.value), ...opts() }); r.jobs.forEach((j) => watchJob(j.job_id)); toast(`Exporting ${r.jobs.length} edits`); });
});

/* ---------------------------------------------------------------- adobe */
page("adobe", async () => {
  const st = await api("/api/status");
  const ops = await api("/api/operations?limit=150");
  main().innerHTML = `<h1>Adobe Integration</h1><p class="sub">${esc(st.adobe.note)}</p>
  <div class="table-wrap card"><table><thead><tr><th>App</th><th>On this machine</th><th>How the studio uses it</th></tr></thead><tbody>
  ${Object.values(st.adobe.apps).map((a) => `<tr><td>${esc(a.name)}</td><td>${a.installed ? `<span class="badge good">detected</span> <span class="small muted">${esc(a.paths[0] || "")}</span>` : '<span class="badge">not detected</span>'}</td><td class="small">${esc(a.use)}</td></tr>`).join("")}
  </tbody></table></div>
  <h2>Who handled what</h2>
  <div class="chips" style="margin-bottom:10px">${ops.summary.map((s) => `<span class="badge ${s.engine.startsWith("adobe") ? "accent" : s.engine === "ffmpeg" ? "blue" : ""}">${esc(s.engine)}: ${s.n}</span>`).join("")}</div>
  <div class="table-wrap"><table><thead><tr><th>When</th><th>Operation</th><th>Engine</th><th>Detail</th><th>Links</th></tr></thead><tbody>
  ${ops.rows.map((o) => `<tr><td class="small muted">${ago(o.created_at)}</td><td>${esc(o.operation)}</td><td><span class="badge ${o.engine.startsWith("adobe") ? "accent" : o.engine === "ffmpeg" ? "blue" : ""}">${esc(o.engine)}</span></td><td class="small">${esc(o.detail)}</td><td class="small">${o.plan_id ? `<a href="#/plan/${o.plan_id}">edit</a> ` : ""}${o.source_id ? `<a href="#/source/${o.source_id}">source</a>` : ""}</td></tr>`).join("")}
  </tbody></table></div>`;
});

/* ---------------------------------------------------------------- settings */
page("settings", async () => {
  const s = await api("/api/settings");
  const st = await api("/api/status");
  const f = (k, label, type = "text", extra = "") => `<div class="field"><label>${label}</label><input id="s-${k}" type="${type}" value="${esc(s[k])}" ${extra}></div>`;
  main().innerHTML = `<h1>Settings</h1><p class="sub">Data folder: <code>${esc(st.data_dir)}</code></p>
  <div class="cols"><div class="card">
    ${f("brand", "Brand name")}
    <div class="field"><label class="row" style="color:var(--text)"><input type="checkbox" id="s-llm_enabled" ${s.llm_enabled ? "checked" : ""}> Use Claude for content understanding, titles and style notes</label></div>
    ${f("llm_model", "Claude model")}
    <div class="field"><label>Claude effort</label><select id="s-llm_effort">${["low", "medium", "high", "xhigh", "max"].map((e) => `<option ${e === s.llm_effort ? "selected" : ""}>${e}</option>`).join("")}</select></div>
    <div class="field"><label>Transcription engine</label><select id="s-transcription_engine">${["auto", "faster-whisper", "openai-whisper", "none"].map((e) => `<option ${e === s.transcription_engine ? "selected" : ""}>${e}</option>`).join("")}</select></div>
    ${f("whisper_model", "Whisper model (tiny / base / small / medium / large-v3)")}
    ${f("min_moment_score", "Minimum moment score (selectivity)", "number")}
    ${f("max_moments_per_hour", "Max short-form moments per hour of footage", "number")}
    ${f("min_platform_score", "Minimum platform-fit score to create a platform edit", "number")}
    <div class="field"><label>Render preset</label><select id="s-render_preset">${["ultrafast", "veryfast", "fast", "medium", "slow"].map((e) => `<option ${e === s.render_preset ? "selected" : ""}>${e}</option>`).join("")}</select></div>
    ${f("render_crf", "Render quality (CRF, lower = better)", "number")}
    ${f("caption_font", "Caption font family")}
    ${f("thumbnail_font_path", "Thumbnail font file (.ttf/.otf, optional)")}
    <button class="primary" id="s-save">Save settings</button>
  </div><div class="card"><h3>System</h3><div class="kv">
    <div>FFmpeg</div><div>${st.ffmpeg ? "found" : '<span class="badge bad">missing — install FFmpeg</span>'}</div>
    <div>Claude</div><div>${st.claude ? "credentials found" : "not configured (set ANTHROPIC_API_KEY)"}</div>
    <div>Transcription</div><div>${esc(st.transcription.join(", ") || "none — pip install faster-whisper")}</div>
    <div>Adobe apps</div><div>${Object.values(st.adobe.apps).filter((a) => a.installed).map((a) => esc(a.name)).join(", ") || "none detected"}</div>
  </div></div></div>`;
  $("#s-save").onclick = () => act(async () => {
    const body = {};
    for (const k of Object.keys(s)) { const el = $(`#s-${k}`); if (!el) continue; body[k] = el.type === "checkbox" ? el.checked : el.type === "number" ? parseFloat(el.value) : el.value; }
    await post("/api/settings", body); route();
  }, "Settings saved");
});

/* ---------------------------------------------------------------- boot */
(async () => {
  STATUS = await api("/api/status");
  PROFILES = await api("/api/profiles");
  route();
  pollJobs();
})();
