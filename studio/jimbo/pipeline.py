"""High-level workflows wired to the job queue."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from . import adobe, analyzer, config, db, jobs, media, planner, references, render, thumbnails

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".flv", ".ts", ".m4v", ".avi"}


def ingest(path: Path, original_name: str, kind: str, category: str = "auto", title: str | None = None,
           project_id: int | None = None, copy: bool = True) -> int:
    """Register a source. Uploaded files are stored read-only; registered paths are only referenced."""
    info = media.probe(path)
    if not info["has_video"]:
        raise ValueError(f"{original_name}: no video stream found")
    if copy:
        digest = hashlib.sha1(f"{original_name}{info['size']}{db.now()}".encode()).hexdigest()[:10]
        dest = config.SOURCES_DIR / f"{digest}_{Path(original_name).name}"
        if path.resolve() != dest.resolve():
            shutil.move(str(path), dest)
        try:
            dest.chmod(0o444)  # originals are never modified
        except OSError:
            pass
        stored = dest
    else:
        stored = path.resolve()
    if kind == "auto":
        kind = "twitch_clip" if info["duration"] <= analyzer.LONG_SOURCE_SECONDS else "recording"
    sid = db.insert("sources", project_id=project_id, kind=kind, category=category,
                    title=title or Path(original_name).stem, path=str(stored), original_name=original_name,
                    stored_copy=1 if copy else 0, duration=info["duration"], width=info["width"],
                    height=info["height"], fps=info["fps"], has_audio=1 if info["has_audio"] else 0,
                    size=info["size"], status="new", meta=info, created_at=db.now())
    db.log_operation("ingest", "ffmpeg", f"{original_name}: {info['duration']:.1f}s {info['width']}x{info['height']}"
                     f" ({'stored copy' if copy else 'referenced in place'})", source_id=sid)
    return sid


@jobs.register("analyze")
def job_analyze(source_id: int, progress, auto_package: bool = True, **_):
    res = analyzer.analyze_source(source_id, progress=lambda p, m: progress(p * 0.85, m))
    made = 0
    if auto_package:
        moments = db.query("SELECT * FROM moments WHERE source_id=? AND kind='short' AND status='candidate' "
                           "ORDER BY overall_score DESC", (source_id,))
        source = db.get("sources", source_id)
        is_clip = (source.get("duration") or 0) <= analyzer.LONG_SOURCE_SECONDS
        for i, m in enumerate(moments):
            progress(0.85 + 0.15 * i / max(1, len(moments)), f"Building platform edits {i + 1}/{len(moments)}")
            # Uploaded Twitch clips always get a package (you chose that clip).
            out = planner.create_package(m["id"], force=is_clip,
                                         platforms=None if not is_clip or planner.decide_platforms(m) else
                                         ["tiktok", "youtube_shorts", "instagram_reels", "x"])
            made += len(out["plans"])
    msg = f"{res['moments']} moments, {res['chapters']} long-form segments, {made} platform edits ({res['engine']})"
    progress(1.0, msg)
    return {**res, "plans": made, "message": msg}


@jobs.register("render")
def job_render(plan_id: int, progress, **_):
    rid = render.render_plan(plan_id, progress=progress)
    return {"render_id": rid, "message": f"Rendered edit #{plan_id} with FFmpeg"}


@jobs.register("export")
def job_export(plan_id: int, progress, premiere: bool = True, thumbnail: bool = True, **_):
    row = db.get("edit_plans", plan_id)
    rendered = db.one("SELECT * FROM renders WHERE plan_id=? AND status='done' AND plan_version=? "
                      "ORDER BY id DESC", (plan_id, row.get("version", 1)))
    if not rendered or (row.get("updated_at") or 0) > (rendered.get("created_at") or 0):
        render.render_plan(plan_id, progress=lambda p, m: progress(p * 0.8, m))
    if premiere:
        adobe.export_premiere(plan_id)
    if thumbnail and not db.one("SELECT id FROM thumbnails WHERE plan_id=?", (plan_id,)):
        progress(0.9, "Thumbnail / cover")
        thumbnails.generate(plan_id=plan_id)
    return {"plan_id": plan_id, "message": f"Exported edit #{plan_id}"}


@jobs.register("reference")
def job_reference(ref_id: int, progress, **_):
    references.analyze_reference(ref_id, progress=progress)
    return {"ref_id": ref_id, "message": f"Reference #{ref_id} analysed"}
