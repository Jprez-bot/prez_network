"""FastAPI app: REST API + the studio web UI."""
from __future__ import annotations

import json
import shutil
from contextlib import asynccontextmanager
import tempfile
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import (adobe, analyzer, captions, config, db, jobs, llm, media, pipeline, planner, profiles,
               references, render, thumbnails, transcribe)
from .platforms import CONTENT_TYPES, PLATFORMS, SHORT_PLATFORMS, label

WEB_DIR = config.STUDIO_ROOT / "web"



@asynccontextmanager
async def _lifespan(_app):
    config.ensure_dirs()
    profiles.seed_builtin_profiles()
    jobs.recover_interrupted()
    yield


app = FastAPI(title="Jimbo4prezz AI Content Studio", lifespan=_lifespan)


def _404(what: str = "not found"):
    raise HTTPException(404, what)


def _row(table: str, rid: int) -> dict:
    r = db.get(table, rid)
    if not r:
        _404(f"{table} {rid} not found")
    return r


# ---------------------------------------------------------------------------
# status / settings
# ---------------------------------------------------------------------------

@app.get("/api/status")
def status():
    return {
        "brand": config.load_settings().get("brand"),
        "data_dir": str(config.DATA_DIR),
        "ffmpeg": bool(config.tool_path("ffmpeg")), "ffprobe": bool(config.tool_path("ffprobe")),
        "claude": llm.available(), "transcription": transcribe.available_engines(),
        "adobe": adobe.detect(),
        "platforms": {k: {**v, "formats": {a: list(r) for a, r in v["formats"].items()}} for k, v in PLATFORMS.items()},
        "content_types": CONTENT_TYPES, "levels": references.LEVELS,
        "aspects": list(references.ASPECT_PARAMS),
    }


@app.get("/api/settings")
def get_settings():
    return config.load_settings()


@app.post("/api/settings")
def set_settings(body: dict = Body(...)):
    s = config.save_settings(body)
    llm._client = None
    return s


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------

@app.get("/api/dashboard")
def dashboard():
    review = {}
    for p in PLATFORMS:
        review[p] = db.one("SELECT COUNT(*) n FROM edit_plans WHERE platform=? AND status='draft'", (p,))["n"]
    strongest = db.query("SELECT m.*, s.title source_title, (SELECT id FROM packages p WHERE p.moment_id=m.id) package_id "
                         "FROM moments m JOIN sources s ON s.id=m.source_id "
                         "WHERE m.kind='short' ORDER BY m.overall_score DESC LIMIT 8")
    thumbs = db.one("SELECT COUNT(*) n FROM thumbnails")["n"]
    plans_needing_thumbs = db.one("SELECT COUNT(*) n FROM edit_plans p WHERE status='approved' AND NOT EXISTS "
                                  "(SELECT 1 FROM thumbnails t WHERE t.plan_id=p.id)")["n"]
    return {
        "projects": db.query("SELECT p.*, (SELECT COUNT(*) FROM sources s WHERE s.project_id=p.id) n_sources "
                             "FROM projects p ORDER BY id DESC LIMIT 10"),
        "recent_sources": db.query("SELECT id,title,kind,category,duration,status,created_at FROM sources "
                                   "ORDER BY id DESC LIMIT 8"),
        "jobs": db.query("SELECT * FROM jobs ORDER BY id DESC LIMIT 12"),
        "strongest": strongest, "review": review,
        "approved": db.one("SELECT COUNT(*) n FROM edit_plans WHERE status='approved'")["n"],
        "renders": db.one("SELECT COUNT(*) n FROM renders WHERE status='done'")["n"],
        "thumbnails": {"total": thumbs, "approved_without": plans_needing_thumbs},
        "adobe": adobe.detect(), "claude": llm.available(), "transcription": transcribe.available_engines(),
    }


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------

@app.get("/api/projects")
def list_projects():
    return db.query("SELECT p.*, (SELECT COUNT(*) FROM sources s WHERE s.project_id=p.id) n_sources "
                    "FROM projects p ORDER BY id DESC")


@app.post("/api/projects")
def create_project(body: dict = Body(...)):
    if not body.get("name"):
        raise HTTPException(400, "name required")
    pid = db.insert("projects", name=body["name"], description=body.get("description", ""), created_at=db.now())
    return db.get("projects", pid)


@app.delete("/api/projects/{pid}")
def delete_project(pid: int):
    db.execute("UPDATE sources SET project_id=NULL WHERE project_id=?", (pid,))
    db.delete("projects", pid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

@app.post("/api/sources/upload")
async def upload_sources(files: list[UploadFile] = File(...), kind: str = Form("auto"),
                         category: str = Form("auto"), project_id: str = Form(""),
                         title: str = Form(""), auto_analyze: str = Form("true"),
                         auto_package: str = Form("true")):
    created = []
    for f in files:
        suffix = Path(f.filename or "upload.mp4").suffix.lower() or ".mp4"
        if suffix not in pipeline.VIDEO_EXT:
            raise HTTPException(400, f"{f.filename}: unsupported file type")
        tmp = Path(tempfile.mkstemp(suffix=suffix, dir=config.SOURCES_DIR)[1])
        with tmp.open("wb") as out:
            shutil.copyfileobj(f.file, out, length=8 * 1024 * 1024)
        try:
            sid = pipeline.ingest(tmp, f.filename or tmp.name, kind, category,
                                  title if (title and len(files) == 1) else None,
                                  int(project_id) if project_id else None, copy=True)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            raise HTTPException(400, str(e))
        job = None
        if auto_analyze == "true":
            job = jobs.submit("analyze", sid, {"auto_package": auto_package == "true"})
        created.append({"source_id": sid, "job_id": job})
    return created


@app.post("/api/sources/register")
def register_source(body: dict = Body(...)):
    """Reference a large local recording in place (no copy; never modified)."""
    p = Path(body.get("path", "")).expanduser()
    if not p.is_file():
        raise HTTPException(400, "file not found on this machine")
    sid = pipeline.ingest(p, p.name, body.get("kind", "auto"), body.get("category", "auto"),
                          body.get("title"), body.get("project_id"), copy=False)
    job = jobs.submit("analyze", sid, {"auto_package": body.get("auto_package", True)}) \
        if body.get("auto_analyze", True) else None
    return {"source_id": sid, "job_id": job}


@app.get("/api/sources")
def list_sources(kind: str | None = None, project_id: int | None = None):
    sql = ("SELECT s.*, (SELECT COUNT(*) FROM moments m WHERE m.source_id=s.id AND m.kind='short') n_moments, "
           "(SELECT COUNT(*) FROM moments m WHERE m.source_id=s.id AND m.kind='long') n_chapters, "
           "(SELECT COUNT(*) FROM edit_plans e WHERE e.source_id=s.id AND e.status!='superseded') n_plans "
           "FROM sources s WHERE 1=1")
    params: list[Any] = []
    if kind:
        kinds = kind.split(",")
        sql += f" AND s.kind IN ({','.join('?' * len(kinds))})"
        params += kinds
    if project_id:
        sql += " AND s.project_id=?"
        params.append(project_id)
    return db.query(sql + " ORDER BY s.id DESC", params)


@app.get("/api/sources/{sid}")
def get_source(sid: int):
    s = _row("sources", sid)
    an = db.get("analyses", sid, key="source_id") or {}
    tr = db.get("transcripts", sid, key="source_id")
    s["analysis"] = {"summary": an.get("summary"), "engine": an.get("engine"),
                     "scenes": len(an.get("scenes") or []), "faces": an.get("faces"),
                     "stats": (an.get("audio") or {}).get("stats")} if an else None
    s["transcript"] = {"engine": tr["engine"], "segments": len(tr.get("segments") or []),
                       "language": tr.get("language")} if tr else None
    s["moments"] = db.query("SELECT * FROM moments WHERE source_id=? ORDER BY kind DESC, overall_score DESC, start",
                            (sid,))
    s["plans"] = _plan_rows("WHERE e.source_id=? AND e.status!='superseded'", [sid])
    s["jobs"] = db.query("SELECT * FROM jobs WHERE kind='analyze' AND target_id=? ORDER BY id DESC LIMIT 3", (sid,))
    return s


@app.patch("/api/sources/{sid}")
def patch_source(sid: int, body: dict = Body(...)):
    _row("sources", sid)
    fields = {k: body[k] for k in ("title", "kind", "category", "project_id") if k in body}
    db.update("sources", sid, **fields)
    return db.get("sources", sid)


@app.delete("/api/sources/{sid}")
def delete_source(sid: int):
    s = _row("sources", sid)
    db.delete("sources", sid)
    if s.get("stored_copy"):
        p = Path(s["path"])
        if config.SOURCES_DIR in p.resolve().parents:
            try:
                p.chmod(0o644)
                p.unlink(missing_ok=True)
            except OSError:
                pass
    return {"ok": True}


@app.post("/api/sources/{sid}/analyze")
def analyze(sid: int, body: dict = Body(default={})):
    _row("sources", sid)
    return {"job_id": jobs.submit("analyze", sid, {"auto_package": body.get("auto_package", True)})}


@app.post("/api/sources/{sid}/transcript")
async def import_transcript(sid: int, file: UploadFile = File(...)):
    _row("sources", sid)
    text = (await file.read()).decode("utf-8", errors="replace")
    if file.filename and file.filename.endswith(".json"):
        segs = json.loads(text)
    else:
        segs = transcribe.parse_subtitles(text)
    if not segs:
        raise HTTPException(400, "no subtitle cues found")
    db.upsert("transcripts", "source_id", source_id=sid, engine="imported", language="",
              segments=segs, created_at=db.now())
    db.log_operation("transcription", "imported", f"{len(segs)} cues from {file.filename}", source_id=sid)
    return {"segments": len(segs)}


@app.get("/api/sources/{sid}/transcript")
def get_transcript(sid: int):
    tr = db.get("transcripts", sid, key="source_id")
    return tr or {"segments": []}


@app.get("/api/sources/{sid}/media")
def source_media(sid: int):
    s = _row("sources", sid)
    return FileResponse(s["path"])


@app.get("/api/sources/{sid}/frame")
def source_frame(sid: int, t: float = 0.0, w: int = 480):
    s = _row("sources", sid)
    out = config.CACHE_DIR / "frames" / f"s{sid}_{t:.2f}_{w}.jpg"
    if not out.exists():
        media.extract_frame(s["path"], t, out, width=w)
    return FileResponse(out, media_type="image/jpeg")


@app.get("/api/sources/{sid}/signals")
def source_signals(sid: int, points: int = 600):
    an = db.get("analyses", sid, key="source_id") or {}
    audio = an.get("audio") or {}
    ex = audio.get("excitement") or []
    hop = audio.get("hop") or 0.5
    if not ex:
        return {"hop": hop, "excitement": [], "duration": 0}
    step = max(1, len(ex) // points)
    ds = [max(ex[i:i + step]) for i in range(0, len(ex), step)]
    return {"hop": hop * step, "excitement": [round(x, 3) for x in ds], "duration": len(ex) * hop,
            "scenes": an.get("scenes") or []}


@app.post("/api/sources/{sid}/longform")
def make_longform(sid: int, body: dict = Body(default={})):
    _row("sources", sid)
    ranges = [tuple(r) for r in body.get("ranges", [])]
    pid = planner.create_long_form(sid, ranges=ranges, chapter_ids=body.get("chapter_ids"),
                                   title=body.get("title", ""), cold_open=bool(body.get("cold_open")),
                                   profile_id=body.get("profile_id"))
    return {"plan_id": pid}


# ---------------------------------------------------------------------------
# moments & packages
# ---------------------------------------------------------------------------

@app.get("/api/moments")
def list_moments(source_id: int | None = None, kind: str = "short", min_score: float = 0):
    sql = ("SELECT m.*, s.title source_title, s.kind source_kind, "
           "(SELECT id FROM packages p WHERE p.moment_id=m.id) package_id FROM moments m "
           "JOIN sources s ON s.id=m.source_id WHERE m.kind=?")
    params: list[Any] = [kind]
    if source_id:
        sql += " AND m.source_id=?"
        params.append(source_id)
    if min_score:
        sql += " AND m.overall_score>=?"
        params.append(min_score)
    return db.query(sql + " ORDER BY m.overall_score DESC, m.start", params)


@app.post("/api/moments")
def create_moment(body: dict = Body(...)):
    """Manually mark a moment (start/end on the source); it is scored like detected ones."""
    sid = int(body["source_id"])
    s = _row("sources", sid)
    start, end = float(body["start"]), float(body["end"])
    if not 0 <= start < end <= (s["duration"] or end):
        raise HTTPException(400, "invalid range")
    an = db.get("analyses", sid, key="source_id") or {}
    tr = db.get("transcripts", sid, key="source_id") or {}
    segs = tr.get("segments") or []
    audio = {**(an.get("audio") or {}), "peak": []}
    sig = analyzer.build_signals(audio, an.get("scenes") or [], segs, s["duration"])
    hook_s = float(body.get("hook_start", start))
    w = {"start": start, "end": end, "hook_start": hook_s, "hook_end": float(body.get("hook_end", min(end, hook_s + 3))),
         "payoff_end": float(body.get("payoff_end", end)), "peak": hook_s}
    face = media.face_track(s["path"], start, end, samples=6)
    scores = analyzer.score_window(w, sig, segs, audio, face)
    mid = db.insert("moments", source_id=sid, kind=body.get("kind", "short"), start=start, end=end,
                    hook_start=w["hook_start"], hook_end=w["hook_end"], payoff_end=w["payoff_end"],
                    structure=body.get("structure", "context_first"), title=body.get("title") or "Manual moment",
                    summary="Marked manually", reasons=analyzer.explain(scores),
                    excerpt=transcribe.text_between(segs, start, end)[:4000],
                    content_type=body.get("content_type") or s.get("category"), scores=scores,
                    overall_score=analyzer.overall(scores),
                    platform_scores=analyzer.platform_scores(scores, end - start),
                    signals={"face": face, "emphasis_words": []}, engine="manual", status="candidate",
                    created_at=db.now())
    return db.get("moments", mid)


@app.patch("/api/moments/{mid}")
def patch_moment(mid: int, body: dict = Body(...)):
    _row("moments", mid)
    fields = {k: body[k] for k in ("start", "end", "hook_start", "hook_end", "payoff_end", "structure",
                                   "title", "status", "content_type") if k in body}
    db.update("moments", mid, **fields)
    return db.get("moments", mid)


@app.delete("/api/moments/{mid}")
def delete_moment(mid: int):
    db.delete("moments", mid)
    return {"ok": True}


@app.post("/api/moments/{mid}/package")
def package_moment(mid: int, body: dict = Body(default={})):
    _row("moments", mid)
    return planner.create_package(mid, platforms=body.get("platforms"), force=bool(body.get("force")))


@app.get("/api/packages")
def list_packages(source_id: int | None = None):
    sql = ("SELECT p.*, m.start, m.end, m.overall_score, m.title moment_title, m.platform_scores, "
           "s.title source_title FROM packages p LEFT JOIN moments m ON m.id=p.moment_id "
           "JOIN sources s ON s.id=p.source_id")
    params: list[Any] = []
    if source_id:
        sql += " WHERE p.source_id=?"
        params.append(source_id)
    rows = db.query(sql + " ORDER BY p.id DESC", params)
    for r in rows:
        r["plans"] = _plan_rows("WHERE e.package_id=? AND e.status!='superseded'", [r["id"]])
        r["long_form"] = _plan_rows(
            "WHERE e.form='long' AND e.source_id=? AND e.status!='superseded' AND EXISTS (SELECT 1 FROM json_each(e.plan, '$.segments') j "
            "WHERE json_extract(j.value,'$.start') <= ? AND json_extract(j.value,'$.end') >= ?)",
            [r["source_id"], r.get("start") or 0, r.get("end") or 0])
        r["thumbnails"] = db.query("SELECT id, plan_id, kind, path FROM thumbnails WHERE plan_id IN "
                                   f"({','.join(str(p['id']) for p in r['plans']) or '0'})")
    return rows


# ---------------------------------------------------------------------------
# edit plans
# ---------------------------------------------------------------------------

def _plan_rows(where: str, params: list) -> list[dict]:
    rows = db.query("SELECT e.id, e.source_id, e.moment_id, e.package_id, e.version, e.platform, e.content_type, "
                    "e.form, e.profile_id, e.status, e.title, e.confidence, e.engine, e.plan, e.created_at, "
                    "e.updated_at, s.title source_title, s.kind source_kind, sp.name profile_name "
                    "FROM edit_plans e JOIN sources s ON s.id=e.source_id "
                    f"LEFT JOIN style_profiles sp ON sp.id=e.profile_id {where} ORDER BY e.id DESC", params)
    for r in rows:
        plan = r.pop("plan") or {}
        tl = planner.timeline(plan)
        r["duration"] = round(tl[-1]["out_end"], 2) if tl else 0
        r["source_range"] = [min((s["start"] for s in tl), default=0), max((s["end"] for s in tl), default=0)]
        r["format"] = plan.get("format")
        r["layout_mode"] = (plan.get("layout") or {}).get("mode")
        r["structure"] = plan.get("structure")
        r["captions_on"] = (plan.get("captions") or {}).get("enabled")
        r["platform_label"] = label(r["platform"])
        rnd = db.one("SELECT id, status, path, created_at, plan_version FROM renders WHERE plan_id=? "
                     "ORDER BY id DESC LIMIT 1", (r["id"],))
        r["render"] = rnd
        r["thumbnail"] = db.one("SELECT id, path FROM thumbnails WHERE plan_id=? ORDER BY selected DESC, id DESC "
                                "LIMIT 1", (r["id"],))
        r["premiere"] = any(config.ADOBE_DIR.glob(f"plan{r['id']}_v{r['version']}_*/sequence.xml"))
    return rows


@app.get("/api/plans")
def list_plans(platform: str | None = None, status: str | None = None, form: str | None = None,
               source_id: int | None = None, package_id: int | None = None, include_superseded: bool = False):
    where, params = ["1=1"], []
    if not include_superseded:
        where.append("e.status!='superseded'")
    for col, val in (("e.platform", platform), ("e.status", status), ("e.form", form),
                     ("e.source_id", source_id), ("e.package_id", package_id)):
        if val:
            vals = str(val).split(",")
            where.append(f"{col} IN ({','.join('?' * len(vals))})")
            params += vals
    return _plan_rows("WHERE " + " AND ".join(where), params)


@app.get("/api/plans/{pid}")
def get_plan(pid: int):
    r = _row("edit_plans", pid)
    r["timeline"] = planner.timeline(r["plan"])
    r["duration"] = round(r["timeline"][-1]["out_end"], 2) if r["timeline"] else 0
    r["source"] = db.get("sources", r["source_id"])
    r["moment"] = db.get("moments", r["moment_id"]) if r.get("moment_id") else None
    r["profile"] = db.get("style_profiles", r["profile_id"]) if r.get("profile_id") else None
    if r["profile"]:
        r["profile"]["effective"] = {k: v for k, v in profiles.effective_params(r["profile"]).items() if k != "_layers"}
    r["renders"] = db.query("SELECT * FROM renders WHERE plan_id=? ORDER BY id DESC", (pid,))
    r["thumbnails"] = db.query("SELECT * FROM thumbnails WHERE plan_id=? ORDER BY selected DESC, id DESC", (pid,))
    r["siblings"] = _plan_rows("WHERE e.package_id=? AND e.id!=? AND e.status!='superseded'",
                               [r["package_id"], pid]) if r.get("package_id") else []
    r["history"] = db.query("SELECT id, version, status, created_at FROM edit_plans WHERE source_id=? AND "
                            "platform=? AND (id=? OR parent_id=? OR id=?) ORDER BY version",
                            (r["source_id"], r["platform"], pid, pid, r.get("parent_id") or -1))
    r["operations"] = db.query("SELECT * FROM operations WHERE plan_id=? ORDER BY id DESC LIMIT 30", (pid,))
    r["premiere_dir"] = next((str(p.parent) for p in config.ADOBE_DIR.glob(f"plan{pid}_v{r['version']}_*/sequence.xml")), None)
    return r


@app.patch("/api/plans/{pid}")
def patch_plan(pid: int, body: dict = Body(...)):
    _row("edit_plans", pid)
    try:
        return planner.save_plan_edit(pid, body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/plans/{pid}/status")
def plan_status(pid: int, body: dict = Body(...)):
    if body.get("status") not in ("draft", "approved", "rejected"):
        raise HTTPException(400, "status must be draft|approved|rejected")
    return planner.set_status(pid, body["status"], body.get("note", ""))


@app.post("/api/plans/{pid}/regenerate")
def plan_regenerate(pid: int, body: dict = Body(default={})):
    try:
        return {"plan_id": planner.regenerate(pid, body.get("profile_id"))}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/plans/{pid}/duplicate")
def plan_duplicate(pid: int, body: dict = Body(...)):
    if body.get("platform") not in PLATFORMS:
        raise HTTPException(400, "unknown platform")
    return {"plan_id": planner.duplicate_for_platform(pid, body["platform"], body.get("aspect"))}


@app.post("/api/plans/{pid}/captions/refresh")
def plan_refresh_captions(pid: int):
    r = _row("edit_plans", pid)
    plan = planner.refresh_captions(r["plan"], r["source_id"])
    db.update("edit_plans", pid, plan=plan, updated_at=db.now())
    return db.get("edit_plans", pid)


@app.post("/api/plans/{pid}/render")
def plan_render(pid: int):
    _row("edit_plans", pid)
    return {"job_id": jobs.submit("render", pid)}


@app.post("/api/plans/{pid}/premiere")
def plan_premiere(pid: int):
    _row("edit_plans", pid)
    return adobe.export_premiere(pid)


@app.get("/api/plans/{pid}/preview")
def plan_preview(pid: int, t: float | None = None):
    r = _row("edit_plans", pid)
    s = db.get("sources", r["source_id"])
    tl = planner.timeline(r["plan"])
    if t is None:
        t = (tl[0]["start"] + tl[0]["end"]) / 2 if tl else 0
    out = config.CACHE_DIR / "previews" / f"p{pid}_{int(db.now() * 1000)}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    for old in out.parent.glob(f"p{pid}_*.jpg"):
        old.unlink(missing_ok=True)
    render.preview_frame(r["plan"], s, t, out)
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/plans/{pid}/captions.srt")
def plan_srt(pid: int):
    r = _row("edit_plans", pid)
    lines = captions.map_to_output((r["plan"].get("captions") or {}).get("lines") or [], planner.timeline(r["plan"]))
    tmp = config.CAPTIONS_DIR / f"plan{pid}.srt"
    captions.write_srt(lines, tmp)
    return PlainTextResponse(tmp.read_text(), headers={"Content-Disposition": f"attachment; filename=plan{pid}.srt"})


@app.delete("/api/plans/{pid}")
def delete_plan(pid: int):
    db.delete("edit_plans", pid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# renders & files
# ---------------------------------------------------------------------------

@app.get("/api/renders/{rid}/file")
def render_file(rid: int, download: bool = False):
    r = _row("renders", rid)
    if r["status"] != "done" or not Path(r["path"]).exists():
        _404("render file not available")
    return FileResponse(r["path"], media_type="video/mp4",
                        filename=Path(r["path"]).name if download else None)


@app.get("/api/files")
def data_file(path: str, download: bool = False):
    """Serve generated files (Adobe hand-off, captions, layers) from the data directory only."""
    p = Path(path).resolve()
    if config.DATA_DIR not in p.parents or not p.is_file():
        _404("file not available")
    return FileResponse(p, filename=p.name if download else None)


@app.get("/api/files/list")
def list_dir(path: str):
    p = Path(path).resolve()
    if config.DATA_DIR not in p.parents and p != config.DATA_DIR or not p.is_dir():
        _404("folder not available")
    return [{"name": c.name, "path": str(c), "size": c.stat().st_size} for c in sorted(p.iterdir()) if c.is_file()]


# ---------------------------------------------------------------------------
# thumbnails
# ---------------------------------------------------------------------------

@app.get("/api/thumbnails")
def list_thumbnails(plan_id: int | None = None, source_id: int | None = None):
    sql = "SELECT t.*, e.platform, e.title plan_title FROM thumbnails t LEFT JOIN edit_plans e ON e.id=t.plan_id WHERE 1=1"
    params: list[Any] = []
    if plan_id:
        sql += " AND t.plan_id=?"
        params.append(plan_id)
    if source_id:
        sql += " AND t.source_id=?"
        params.append(source_id)
    return db.query(sql + " ORDER BY t.id DESC LIMIT 200", params)


@app.get("/api/thumbnails/candidates")
def thumb_candidates(plan_id: int):
    r = _row("edit_plans", plan_id)
    s = db.get("sources", r["source_id"])
    return thumbnails.candidate_frames(s, r["plan"], 0, s["duration"], n=12)


@app.post("/api/thumbnails")
def make_thumbnails(body: dict = Body(...)):
    try:
        ids = thumbnails.generate(plan_id=body.get("plan_id"), source_id=body.get("source_id"),
                                  kind=body.get("kind"), frame_time=body.get("frame_time"),
                                  text=body.get("text"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return [db.get("thumbnails", i) for i in ids]


@app.post("/api/thumbnails/{tid}/select")
def select_thumbnail(tid: int):
    t = _row("thumbnails", tid)
    db.execute("UPDATE thumbnails SET selected=0 WHERE plan_id IS ? AND source_id=?", (t["plan_id"], t["source_id"]))
    db.update("thumbnails", tid, selected=1)
    return db.get("thumbnails", tid)


@app.get("/api/thumbnails/{tid}/file")
def thumbnail_file(tid: int):
    t = _row("thumbnails", tid)
    return FileResponse(t["path"], headers={"Cache-Control": "no-store"})


@app.get("/api/thumbnail-style")
def thumbnail_style():
    return thumbnails.thumbnail_style()


# ---------------------------------------------------------------------------
# export center
# ---------------------------------------------------------------------------

@app.post("/api/export")
def export(body: dict = Body(...)):
    ids = body.get("plan_ids") or []
    if body.get("scope") == "approved":
        plats = body.get("platforms") or list(PLATFORMS)
        ids = [r["id"] for r in db.query(
            f"SELECT id FROM edit_plans WHERE status='approved' AND platform IN ({','.join('?' * len(plats))})", plats)]
    if not ids:
        raise HTTPException(400, "nothing to export")
    meta = {"premiere": body.get("premiere", True), "thumbnail": body.get("thumbnail", True)}
    return {"jobs": [{"plan_id": i, "job_id": jobs.submit("export", i, meta)} for i in ids]}


# ---------------------------------------------------------------------------
# style profiles
# ---------------------------------------------------------------------------

@app.get("/api/profiles/schema")
def profile_schema():
    return {"params": profiles.PARAM_SCHEMA, "base": profiles.BASE}


@app.get("/api/profiles")
def list_profiles():
    return profiles.list_profiles()


@app.post("/api/profiles")
def create_profile(body: dict = Body(...)):
    if not body.get("name") or body.get("platform") not in PLATFORMS:
        raise HTTPException(400, "name and a valid platform are required")
    if db.get("style_profiles", body["name"], key="name"):
        raise HTTPException(400, "a profile with that name exists")
    base = dict(profiles.BASE)
    if body.get("copy_from"):
        src = db.get("style_profiles", body["copy_from"])
        if src:
            base.update(src.get("params") or {})
    else:
        builtin = next((p for p in profiles.BUILTIN_PROFILES if p["platform"] == body["platform"]), None)
        if builtin:
            base.update(builtin["params"])
    base.update(profiles.clean_params(body.get("params") or {}))
    pid = db.insert("style_profiles", name=body["name"], platform=body["platform"],
                    content_types=body.get("content_types", []), description=body.get("description", ""),
                    params=base, learned={}, builtin=0, created_at=db.now(), updated_at=db.now())
    p = db.get("style_profiles", pid)
    p["effective"] = profiles.effective_params(p)
    return p


@app.patch("/api/profiles/{pid}")
def patch_profile(pid: int, body: dict = Body(...)):
    p = _row("style_profiles", pid)
    fields: dict = {}
    if "params" in body:
        params = dict(p.get("params") or {})
        params.update(profiles.clean_params(body["params"]))
        fields["params"] = params
    for k in ("name", "description", "content_types", "platform"):
        if k in body:
            fields[k] = body[k]
    if body.get("reset_learned"):
        fields["learned"] = {}
    db.update("style_profiles", pid, updated_at=db.now(), **fields)
    p = db.get("style_profiles", pid)
    p["effective"] = profiles.effective_params(p)
    return p


@app.delete("/api/profiles/{pid}")
def delete_profile(pid: int):
    p = _row("style_profiles", pid)
    if p["builtin"]:
        raise HTTPException(400, "built-in profiles can be edited but not deleted")
    db.execute("UPDATE edit_plans SET profile_id=NULL WHERE profile_id=?", (pid,))
    db.delete("style_profiles", pid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# references
# ---------------------------------------------------------------------------

@app.get("/api/references")
def list_references():
    rows = db.query("SELECT * FROM refs ORDER BY id DESC")
    names = {p["id"]: p["name"] for p in db.query("SELECT id, name FROM style_profiles")}
    for r in rows:
        r["profile_names"] = [names.get(i, f"#{i}") for i in r.get("profile_ids") or []]
    return rows


@app.post("/api/references")
async def create_reference(level: str = Form(...), name: str = Form(""), creator: str = Form(""),
                           url: str = Form(""), notes: str = Form(""), section_start: str = Form(""),
                           section_end: str = Form(""), platforms: str = Form("[]"),
                           content_types: str = Form("[]"), aspects: str = Form(""),
                           profile_ids: str = Form("[]"), file: UploadFile | None = File(None)):
    if level not in references.LEVELS:
        raise HTTPException(400, "unknown reference level")
    path = None
    if file is not None and file.filename:
        dest = config.REFERENCES_DIR / f"{int(db.now())}_{Path(file.filename).name}"
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out, length=8 * 1024 * 1024)
        path = str(dest)

    def ts(v):
        if not v:
            return None
        if ":" in v:
            parts = [float(x) for x in v.split(":")]
            return sum(x * 60 ** i for i, x in enumerate(reversed(parts)))
        return float(v)

    ref = {"level": level, "name": name or (Path(path).stem if path else url or level), "creator": creator,
           "url": url, "path": path, "section_start": ts(section_start), "section_end": ts(section_end),
           "platforms": json.loads(platforms or "[]"), "content_types": json.loads(content_types or "[]"),
           "aspects": aspects, "notes": notes, "profile_ids": json.loads(profile_ids or "[]")}
    if not ref["profile_ids"] and level != "thumbnail":
        ref["profile_ids"] = references.auto_link_profiles(ref)
    rid = db.insert("refs", **ref, extracted={}, status="new", created_at=db.now())
    job = jobs.submit("reference", rid)
    return {"ref_id": rid, "job_id": job}


@app.patch("/api/references/{rid}")
def patch_reference(rid: int, body: dict = Body(...)):
    _row("refs", rid)
    fields = {k: body[k] for k in ("name", "creator", "url", "notes", "platforms", "content_types", "aspects",
                                   "profile_ids", "status", "section_start", "section_end", "level") if k in body}
    db.update("refs", rid, **fields)
    if body.get("reanalyze", "notes" in fields or "aspects" in fields):
        jobs.submit("reference", rid)
    return db.get("refs", rid)


@app.delete("/api/references/{rid}")
def delete_reference(rid: int):
    db.delete("refs", rid)
    return {"ok": True}


@app.get("/api/references/{rid}/file")
def reference_file(rid: int):
    r = _row("refs", rid)
    if not r.get("path"):
        _404("no file")
    return FileResponse(r["path"])


# ---------------------------------------------------------------------------
# library / ops / jobs
# ---------------------------------------------------------------------------

@app.get("/api/library")
def library(type: str = "all"):
    items: list[dict] = []

    def add(kind, rows, fn):
        if type in ("all", kind):
            items.extend(fn(r) for r in rows)

    add("source", db.query("SELECT * FROM sources ORDER BY id DESC"),
        lambda r: {"type": "source", "subtype": r["kind"], "id": r["id"], "title": r["title"],
                   "source_id": r["id"], "detail": f"{(r['duration'] or 0) / 60:.1f} min · {r['width']}x{r['height']}",
                   "created_at": r["created_at"], "path": r["path"]})
    add("moment", db.query("SELECT m.*, s.title st FROM moments m JOIN sources s ON s.id=m.source_id ORDER BY m.id DESC"),
        lambda r: {"type": "moment", "subtype": r["kind"], "id": r["id"], "title": r["title"],
                   "source_id": r["source_id"], "detail": f"{r['st']} @ {r['start']:.1f}–{r['end']:.1f}s"
                   + (f" · score {r['overall_score']}" if r.get("overall_score") else ""), "created_at": r["created_at"]})
    add("plan", db.query("SELECT e.*, s.title st FROM edit_plans e JOIN sources s ON s.id=e.source_id "
                         "WHERE e.status!='superseded' ORDER BY e.id DESC"),
        lambda r: {"type": "plan", "subtype": r["platform"], "id": r["id"], "title": r["title"],
                   "source_id": r["source_id"], "detail": f"{label(r['platform'])} · {r['status']} · v{r['version']}",
                   "created_at": r["created_at"]})
    add("render", db.query("SELECT r.*, e.platform, e.source_id FROM renders r JOIN edit_plans e ON e.id=r.plan_id "
                           "WHERE r.status='done' ORDER BY r.id DESC"),
        lambda r: {"type": "render", "subtype": r["platform"], "id": r["id"], "title": Path(r["path"]).name,
                   "source_id": r["source_id"], "plan_id": r["plan_id"],
                   "detail": f"{r['width']}x{r['height']} · {r['duration'] or 0:.1f}s", "created_at": r["created_at"],
                   "path": r["path"]})
    add("thumbnail", db.query("SELECT * FROM thumbnails ORDER BY id DESC"),
        lambda r: {"type": "thumbnail", "subtype": r["kind"], "id": r["id"], "title": r.get("text") or "thumbnail",
                   "source_id": r["source_id"], "plan_id": r["plan_id"], "detail": f"frame @ {r['frame_time']:.1f}s",
                   "created_at": r["created_at"], "path": r["path"]})
    add("reference", db.query("SELECT * FROM refs ORDER BY id DESC"),
        lambda r: {"type": "reference", "subtype": r["level"], "id": r["id"], "title": r["name"],
                   "detail": r.get("creator") or r.get("url") or "", "created_at": r["created_at"]})
    add("profile", db.query("SELECT * FROM style_profiles ORDER BY id"),
        lambda r: {"type": "profile", "subtype": r["platform"], "id": r["id"], "title": r["name"],
                   "detail": r.get("description") or "", "created_at": r["created_at"]})
    if type in ("all", "premiere"):
        for d in sorted(config.ADOBE_DIR.glob("plan*/")):
            items.append({"type": "premiere", "subtype": "sequence", "id": d.name, "title": d.name,
                          "detail": "FCP XML + captions + import script", "path": str(d),
                          "created_at": d.stat().st_mtime})
    if type in ("all", "caption"):
        for f in sorted(config.RENDERS_DIR.glob("*/*.srt")):
            items.append({"type": "caption", "subtype": "srt", "id": f.name, "title": f.name, "detail": "SRT",
                          "path": str(f), "created_at": f.stat().st_mtime})
    items.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return items


@app.get("/api/operations")
def operations(limit: int = 200, source_id: int | None = None):
    if source_id:
        return db.query("SELECT * FROM operations WHERE source_id=? ORDER BY id DESC LIMIT ?", (source_id, limit))
    rows = db.query("SELECT * FROM operations ORDER BY id DESC LIMIT ?", (limit,))
    summary = db.query("SELECT engine, COUNT(*) n FROM operations GROUP BY engine ORDER BY n DESC")
    return {"rows": rows, "summary": summary}


@app.get("/api/jobs")
def list_jobs(limit: int = 50):
    return db.query("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/jobs/{jid}")
def get_job(jid: int):
    return _row("jobs", jid)


@app.post("/api/jobs/{jid}/retry")
def retry_job(jid: int):
    return {"job_id": jobs.retry(jid)}


@app.exception_handler(media.MediaError)
def media_error(_, exc: media.MediaError):
    return JSONResponse(status_code=500, content={"detail": str(exc)[-1500:]})


# ---------------------------------------------------------------------------
# web UI
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
