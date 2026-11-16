"""Render an edit plan to a real video file with FFmpeg.

Reads the original via input seeking (never modifies it), applies the
layout (crop / split-screen / picture-in-picture / blurred fill), punch-ins,
burned-in captions (libass) and loudness normalisation, and writes an H.264
MP4 at the platform resolution. Plans with many segments (long-form) are
rendered in batches and joined losslessly.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from . import captions, config, db, media
from .planner import timeline

FPS_CAP = 60
BATCH = 40


def _crop_box(sw: int, sh: int, cx: float, cy: float, zoom: float, aspect: float) -> tuple[int, int, int, int]:
    """Pixel crop (w, h, x, y) of the given aspect centered near (cx, cy)."""
    zoom = max(0.05, min(1.0, zoom))
    ch = sh * zoom
    cw = ch * aspect
    if cw > sw:
        cw = sw
        ch = cw / aspect
    cw, ch = int(cw) // 2 * 2, int(ch) // 2 * 2
    x = int(min(max(cx * sw - cw / 2, 0), sw - cw))
    y = int(min(max(cy * sh - ch / 2, 0), sh - ch))
    return max(2, cw), max(2, ch), x, y


def layout_filter(layout: dict, sw: int, sh: int, W: int, H: int, inp: str, out: str) -> str:
    mode = layout.get("mode", "full")
    content = layout.get("content") or {"cx": 0.5, "cy": 0.5, "zoom": 1.0}
    face = layout.get("face")
    if mode == "crop":
        w, h, x, y = _crop_box(sw, sh, content["cx"], content["cy"], content.get("zoom", 1.0), W / H)
        return f"[{inp}]crop={w}:{h}:{x}:{y},scale={W}:{H}:flags=lanczos,setsar=1[{out}]"
    if mode == "fit_blur":
        return (f"[{inp}]split=2[{out}bg][{out}fg];"
                f"[{out}bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"gblur=sigma=28,eq=brightness=-0.08:saturation=1.1[{out}bgb];"
                f"[{out}fg]scale={W}:{H}:force_original_aspect_ratio=decrease[{out}fgs];"
                f"[{out}bgb][{out}fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[{out}]")
    if mode == "split" and face:
        ratio = float(layout.get("split_ratio", 0.62))
        top_h = int(H * ratio) // 2 * 2
        bot_h = H - top_h
        face_top = layout.get("face_position", "bottom") == "top"
        main_h, cam_h = (bot_h, top_h) if face_top else (top_h, bot_h)
        cw, ch, cx, cy = _crop_box(sw, sh, content["cx"], content["cy"], content.get("zoom", 1.0), W / main_h)
        fw, fh, fx, fy = _crop_box(sw, sh, face["cx"], face["cy"], face.get("zoom", 0.3), W / cam_h)
        first, second = ("cam", "main") if face_top else ("main", "cam")
        return (f"[{inp}]split=2[{out}a][{out}b];"
                f"[{out}a]crop={cw}:{ch}:{cx}:{cy},scale={W}:{main_h}:flags=lanczos,setsar=1[{out}main];"
                f"[{out}b]crop={fw}:{fh}:{fx}:{fy},scale={W}:{cam_h}:flags=lanczos,setsar=1[{out}cam];"
                f"[{out}{first}][{out}{second}]vstack=inputs=2[{out}]")
    if mode == "pip" and face:
        cw, ch, cx, cy = _crop_box(sw, sh, content["cx"], content["cy"], content.get("zoom", 1.0), W / H)
        pw = int(W * 0.42) // 2 * 2
        ph = int(pw * 0.75) // 2 * 2
        fw, fh, fx, fy = _crop_box(sw, sh, face["cx"], face["cy"], face.get("zoom", 0.3), pw / ph)
        margin = int(W * 0.04)
        pos_y = int(H * 0.08)
        return (f"[{inp}]split=2[{out}a][{out}b];"
                f"[{out}a]crop={cw}:{ch}:{cx}:{cy},scale={W}:{H}:flags=lanczos,setsar=1[{out}main];"
                f"[{out}b]crop={fw}:{fh}:{fx}:{fy},scale={pw}:{ph},pad={pw+8}:{ph+8}:4:4:white[{out}cam];"
                f"[{out}main][{out}cam]overlay={margin}:{pos_y},setsar=1[{out}]")
    # full: letterbox/pillarbox into the output size
    return (f"[{inp}]scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[{out}]")


def _zoom_chain(zooms: list[dict], layout: dict, W: int, H: int, inp: str, out: str) -> str:
    """Punch-ins: constant-scale crops overlaid during each zoom window (no zoompan jitter)."""
    if not zooms:
        return f"[{inp}]null[{out}]"
    groups: dict[float, list[dict]] = {}
    for z in zooms:
        groups.setdefault(round(float(z.get("scale", 1.12)), 3), []).append(z)
    # Zoom towards the face when we framed on it, else the center.
    cy = 0.42 if layout.get("mode") == "crop" else 0.5
    parts = [f"[{inp}]split={len(groups) + 1}" + "".join(f"[{out}s{i}]" for i in range(len(groups) + 1))]
    cur = f"{out}s0"
    for gi, (scale, zs) in enumerate(groups.items(), 1):
        zw, zh = int(W / scale) // 2 * 2, int(H / scale) // 2 * 2
        x, y = (W - zw) // 2, int((H - zh) * cy)
        enable = "+".join(f"between(t,{z['at']:.3f},{z['at'] + z.get('duration', 1.5):.3f})" for z in zs)
        parts.append(f"[{out}s{gi}]crop={zw}:{zh}:{x}:{y},scale={W}:{H}[{out}z{gi}]")
        nxt = f"{out}o{gi}"
        parts.append(f"[{cur}][{out}z{gi}]overlay=0:0:enable='{enable}'[{nxt}]")
        cur = nxt
    parts.append(f"[{cur}]null[{out}]")
    return ";".join(parts)


def _ffpath(p: Path) -> str:
    s = str(p).replace("\\", "/")
    return s.replace(":", r"\:").replace("'", r"\'").replace(",", r"\,")


def build_command(plan: dict, source: dict, segs: list[dict], out_path: Path, ass_path: Path | None,
                  zooms: list[dict], fps: float, preset: str, crf: int) -> list[str]:
    sw, sh = source["width"] or 1920, source["height"] or 1080
    W, H = plan["format"]["width"], plan["format"]["height"]
    layout = plan.get("layout") or {"mode": "full"}
    has_audio = bool(source.get("has_audio"))
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y"]
    for s in segs:
        cmd += ["-ss", f"{s['start']:.3f}", "-t", f"{s['end'] - s['start']:.3f}", "-i", source["path"]]
    fparts = []
    concat_in = ""
    for i, s in enumerate(segs):
        d = s["end"] - s["start"]
        fparts.append(layout_filter(layout, sw, sh, W, H, f"{i}:v", f"l{i}"))
        fparts.append(f"[l{i}]fps={fps},format=yuv420p,setpts=PTS-STARTPTS[v{i}]")
        if has_audio:
            fade = min(0.02, d / 4)
            fparts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS,"
                          f"afade=t=in:d={fade:.3f},afade=t=out:st={max(0, d - fade):.3f}:d={fade:.3f}[a{i}]")
        else:
            fparts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={d:.3f}[a{i}]")
        concat_in += f"[v{i}][a{i}]"
    fparts.append(f"{concat_in}concat=n={len(segs)}:v=1:a=1[vc][ac]")
    fparts.append(_zoom_chain(zooms, layout, W, H, "vc", "vz"))
    vlast = "vz"
    if ass_path is not None:
        fontsdir = ""
        for d in ("/usr/share/fonts/opentype/inter", "/usr/share/fonts"):
            if Path(d).exists():
                fontsdir = f":fontsdir={_ffpath(Path(d))}"
                break
        fparts.append(f"[vz]ass=filename={_ffpath(ass_path)}{fontsdir}[vs]")
        vlast = "vs"
    audio = plan.get("audio") or {}
    achain = []
    if audio.get("denoise"):
        achain.append("afftdn=nf=-25")
    if audio.get("loudnorm", True):
        achain.append(f"loudnorm=I={float(audio.get('lufs', -14)):.1f}:TP=-1.5:LRA=11")
    achain.append("aresample=48000")
    fparts.append(f"[ac]{','.join(achain)}[ao]")
    cmd += ["-filter_complex", ";".join(fparts), "-map", f"[{vlast}]", "-map", "[ao]",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-r", f"{fps}", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", str(out_path)]
    return cmd


def render_plan(plan_id: int, progress=lambda p, m: None) -> int:
    row = db.get("edit_plans", plan_id)
    if not row:
        raise ValueError("plan not found")
    source = db.get("sources", row["source_id"])
    plan = row["plan"]
    tl = timeline(plan)
    if not tl:
        raise ValueError("plan has no enabled segments")
    settings = config.load_settings()
    fps = min(FPS_CAP, round(source.get("fps") or 30)) or 30
    if plan["format"]["height"] > plan["format"]["width"] and fps > 30 and row.get("form") == "short":
        fps = 30 if fps < 50 else 60
    W, H = plan["format"]["width"], plan["format"]["height"]
    out_dir = config.RENDERS_DIR / f"source_{source['id']}"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(db.now())
    out_path = out_dir / f"plan{plan_id}_v{row.get('version', 1)}_{row['platform']}_{W}x{H}_{stamp}.mp4"
    render_id = db.insert("renders", plan_id=plan_id, path=str(out_path), engine="ffmpeg", width=W, height=H,
                          status="rendering", plan_version=row.get("version", 1), created_at=db.now())
    cap = plan.get("captions") or {}
    cparams = {"caption_style": cap.get("style", "bold_pop"), "caption_size": cap.get("size", 1.0),
               "caption_position": cap.get("position", "lower_third"),
               "caption_uppercase": cap.get("uppercase", False), "caption_animation": cap.get("animation", "none")}
    lines_out = captions.map_to_output(cap.get("lines") or [], tl) if cap.get("enabled") else []
    work = Path(tempfile.mkdtemp(prefix=f"render{plan_id}_", dir=config.CACHE_DIR))
    log = []
    try:
        batches = [tl[i:i + BATCH] for i in range(0, len(tl), BATCH)]
        parts = []
        for bi, batch in enumerate(batches):
            off = batch[0]["out_start"]
            end = batch[-1]["out_end"]
            b_lines = [{**ln, "start": ln["start"] - off, "end": ln["end"] - off,
                        "words": [{**w, "s": w["s"] - off, "e": w["e"] - off} for w in ln.get("words", [])]}
                       for ln in lines_out if ln["end"] > off and ln["start"] < end]
            b_zooms = [{**z, "at": z["at"] - off} for z in plan.get("zooms", []) if off <= z["at"] < end]
            ass = None
            if b_lines:
                ass = captions.write_ass(b_lines, work / f"b{bi}.ass", W, H, cparams,
                                         font=settings.get("caption_font", "Inter Black"))
            target = out_path if len(batches) == 1 else work / f"part{bi:04d}.mp4"
            cmd = build_command(plan, source, batch, target, ass, b_zooms, fps,
                                settings.get("render_preset", "medium"), int(settings.get("render_crf", 20)))
            log.append(" ".join(cmd[:6]) + f" … ({len(batch)} segments)")
            proc = subprocess.run(cmd, capture_output=True)
            if proc.returncode != 0:
                raise media.MediaError(proc.stderr.decode(errors="replace")[-3000:])
            parts.append(target)
            progress((bi + 1) / len(batches) * 0.95, f"Rendered part {bi + 1}/{len(batches)}")
        if len(parts) > 1:
            lst = work / "list.txt"
            lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
            media.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                       "-c", "copy", "-movflags", "+faststart", str(out_path)])
        info = media.probe(out_path)
        # Sidecar caption file for platform upload / Premiere import.
        if lines_out:
            captions.write_srt(lines_out, out_path.with_suffix(".srt"))
        db.update("renders", render_id, status="done", duration=info["duration"], log="\n".join(log))
        detail = (f"{row['platform']} {W}x{H} {info['duration']:.1f}s, layout {plan['layout']['mode']}, "
                  f"{len(plan.get('zooms', []))} punch-ins, captions {'on' if lines_out else 'off'}")
        db.log_operation("render", "ffmpeg", detail, source_id=source["id"], plan_id=plan_id)
        progress(1.0, "Render complete")
        return render_id
    except Exception as e:
        db.update("renders", render_id, status="failed", log=("\n".join(log) + "\n" + str(e))[-6000:])
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def preview_frame(plan: dict, source: dict, t: float, out: Path) -> Path:
    """Single framed preview image of the layout at source time ``t`` (for the framing editor)."""
    W, H = plan["format"]["width"], plan["format"]["height"]
    sw, sh = source["width"] or 1920, source["height"] or 1080
    scale = 540 / max(W, H)
    pw, ph = int(W * scale) // 2 * 2, int(H * scale) // 2 * 2
    lf = layout_filter(plan.get("layout") or {}, sw, sh, W, H, "0:v", "l")
    media.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", source["path"], "-frames:v", "1",
               "-filter_complex", f"{lf};[l]scale={pw}:{ph}[o]", "-map", "[o]", "-q:v", "3", str(out)])
    return out
