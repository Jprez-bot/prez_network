"""Adobe Creative Cloud integration.

What is automated and how:
  * Premiere Pro — every edit plan exports as a Final Cut Pro 7 XML sequence
    (File > Import in Premiere) at the platform resolution, with each cut as a
    clip referencing the ORIGINAL file at its source timecode, basic motion
    for the vertical framing, hook/payoff markers, and an SRT caption file.
    A small ExtendScript imports both into the open project in one step.
  * Photoshop — thumbnails ship as layer PNGs + an ExtendScript that builds an
    editable layered PSD with a live text layer (see thumbnails.py).
  * Lightroom — extracted stills are written as full-quality JPEG/PNG into a
    folder you can add to a Lightroom catalog; no Lightroom automation is
    attempted (it has no supported local scripting API for this).

Adobe apps cannot be driven headlessly from this server; final rendering in
the studio always uses FFmpeg and is labelled as such. Detection below only
reports what is installed on this machine.
"""
from __future__ import annotations

import glob
import os
import platform as _platform
from pathlib import Path
from xml.sax.saxutils import escape

from . import captions, config, db
from .planner import timeline

APPS = {
    "premiere": "Adobe Premiere Pro",
    "photoshop": "Adobe Photoshop",
    "lightroom": "Adobe Lightroom",
    "after_effects": "Adobe After Effects",
    "audition": "Adobe Audition",
    "media_encoder": "Adobe Media Encoder",
}

USES = {
    "premiere": "Primary editor: import generated sequences (FCP XML) + captions; final polish & export.",
    "photoshop": "Primary thumbnail/cover tool: run the generated .jsx to get an editable layered PSD.",
    "lightroom": "Optional: grade extracted stills used for thumbnails.",
    "after_effects": "Not used by default (no practical benefit for the current pipeline).",
    "audition": "Not used by default; FFmpeg loudness normalisation covers leveling.",
    "media_encoder": "Optional: queue Premiere exports. Studio renders use FFmpeg.",
}


def _patterns() -> dict[str, list[str]]:
    system = _platform.system()
    if system == "Darwin":
        return {
            "premiere": ["/Applications/Adobe Premiere Pro*/Adobe Premiere Pro*.app"],
            "photoshop": ["/Applications/Adobe Photoshop*/Adobe Photoshop*.app"],
            "lightroom": ["/Applications/Adobe Lightroom*/Adobe Lightroom*.app", "/Applications/Adobe Lightroom*.app"],
            "after_effects": ["/Applications/Adobe After Effects*/Adobe After Effects*.app"],
            "audition": ["/Applications/Adobe Audition*/Adobe Audition*.app"],
            "media_encoder": ["/Applications/Adobe Media Encoder*/Adobe Media Encoder*.app"],
        }
    if system == "Windows":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files") + r"\Adobe"]
        return {
            "premiere": [r + r"\Adobe Premiere Pro*\Adobe Premiere Pro.exe" for r in roots],
            "photoshop": [r + r"\Adobe Photoshop*\Photoshop.exe" for r in roots],
            "lightroom": [r + r"\Adobe Lightroom*\*.exe" for r in roots],
            "after_effects": [r + r"\Adobe After Effects*\Support Files\AfterFX.exe" for r in roots],
            "audition": [r + r"\Adobe Audition*\Adobe Audition.exe" for r in roots],
            "media_encoder": [r + r"\Adobe Media Encoder*\Adobe Media Encoder.exe" for r in roots],
        }
    return {k: [] for k in APPS}


def detect() -> dict:
    found = {}
    extra = [p for p in os.environ.get("JIMBO_ADOBE_DIRS", "").split(os.pathsep) if p]
    for key, pats in _patterns().items():
        hits = []
        for pat in pats:
            hits += glob.glob(pat)
        for d in extra:
            hits += [h for h in glob.glob(os.path.join(d, "*")) if APPS[key].lower().split()[-1] in h.lower()]
        found[key] = {"name": APPS[key], "installed": bool(hits), "paths": sorted(set(hits))[:3], "use": USES[key]}
    return {"os": _platform.system(), "apps": found,
            "note": ("Studio renders always use FFmpeg. Adobe apps receive hand-off files "
                     "(Premiere XML sequences, Photoshop scripts) that open as editable projects.")}


# ---------------------------------------------------------------------------
# Premiere Pro (FCP7 XML)
# ---------------------------------------------------------------------------

def _pathurl(p: str) -> str:
    path = Path(p).resolve().as_posix()
    if not path.startswith("/"):
        path = "/" + path  # Windows drive letter
    return "file://localhost" + path.replace(" ", "%20")


def _rate(fps: float) -> tuple[int, str]:
    if abs(fps - 29.97) < 0.05:
        return 30, "TRUE"
    if abs(fps - 59.94) < 0.05:
        return 60, "TRUE"
    if abs(fps - 23.976) < 0.05:
        return 24, "TRUE"
    return int(round(fps or 30)), "FALSE"


def _motion(layout: dict, region: str, sw: int, sh: int, W: int, H: int, box_h: int | None = None,
            y_center: float | None = None) -> str:
    """Basic Motion (scale + center) approximating the studio framing."""
    from .render import _crop_box
    area_h = box_h or H
    if layout.get("mode") == "full" or region == "full":
        scale = min(W / sw, H / sh) * 100
        return _motion_xml(scale, 0, 0)
    spec = layout.get(region) or layout.get("content") or {"cx": 0.5, "cy": 0.5, "zoom": 1.0}
    cw, ch, cx, cy = _crop_box(sw, sh, spec["cx"], spec["cy"], spec.get("zoom", 1.0), W / area_h)
    scale = area_h / ch * 100
    # Offset so the crop center lands at the region center (fraction of sequence size).
    crop_cx, crop_cy = cx + cw / 2, cy + ch / 2
    dx = (sw / 2 - crop_cx) * scale / 100 / W
    target_cy = (y_center if y_center is not None else 0.5)
    dy = ((sh / 2 - crop_cy) * scale / 100 + (target_cy - 0.5) * H) / H
    return _motion_xml(scale, dx, dy)


def _motion_xml(scale: float, dx: float, dy: float) -> str:
    return f"""<filter><effect><name>Basic Motion</name><effectid>basic</effectid><effectcategory>motion</effectcategory>
<effecttype>motion</effecttype><mediatype>video</mediatype>
<parameter><parameterid>scale</parameterid><name>Scale</name><valuemin>0</valuemin><valuemax>1000</valuemax><value>{scale:.2f}</value></parameter>
<parameter><parameterid>center</parameterid><name>Center</name><value><horiz>{dx:.5f}</horiz><vert>{dy:.5f}</vert></value></parameter>
</effect></filter>"""


def premiere_xml(plan_row: dict, source: dict) -> str:
    plan = plan_row["plan"]
    tl = timeline(plan)
    fps = source.get("fps") or 30
    tb, ntsc = _rate(fps)
    W, H = plan["format"]["width"], plan["format"]["height"]
    sw, sh = source.get("width") or 1920, source.get("height") or 1080
    src_frames = int(round((source.get("duration") or 0) * tb))
    rate = f"<rate><timebase>{tb}</timebase><ntsc>{ntsc}</ntsc></rate>"
    name = escape(f"{plan_row.get('title') or 'Edit'} [{plan_row['platform']} {plan['format']['aspect']}]")
    file_def = (f"<file id=\"file-1\"><name>{escape(Path(source['path']).name)}</name>"
                f"<pathurl>{escape(_pathurl(source['path']))}</pathurl>{rate}<duration>{src_frames}</duration>"
                f"<media><video><samplecharacteristics>{rate}<width>{sw}</width><height>{sh}</height>"
                f"</samplecharacteristics></video>"
                + ("<audio><samplecharacteristics><depth>16</depth><samplerate>48000</samplerate>"
                   "</samplecharacteristics><channelcount>2</channelcount></audio>" if source.get("has_audio") else "")
                + "</media></file>")
    layout = plan.get("layout") or {"mode": "full"}
    v1, v2, a1 = [], [], []
    face_track = layout.get("mode") in ("split", "pip") and layout.get("face")
    ratio = float(layout.get("split_ratio", 0.62))
    face_top = layout.get("face_position") == "top"
    for i, seg in enumerate(tl):
        s_in, s_out = int(round(seg["start"] * tb)), int(round(seg["end"] * tb))
        t0, t1 = int(round(seg["out_start"] * tb)), int(round(seg["out_end"] * tb))
        common = (f"<name>{escape(Path(source['path']).name)}</name><enabled>TRUE</enabled>"
                  f"<duration>{src_frames}</duration>{rate}<start>{t0}</start><end>{t1}</end>"
                  f"<in>{s_in}</in><out>{s_out}</out>")
        ffile = file_def if i == 0 else "<file id=\"file-1\"/>"
        if layout.get("mode") == "split" and face_track:
            main_h = int(H * (1 - ratio)) if face_top else int(H * ratio)
            cam_h = H - main_h
            main_c = (cam_h + main_h / 2) / H if face_top else (main_h / 2) / H
            cam_c = (cam_h / 2) / H if face_top else (main_h + cam_h / 2) / H
            v1.append(f"<clipitem id=\"v1-{i}\">{common}{ffile}"
                      f"{_motion(layout, 'content', sw, sh, W, H, main_h, main_c)}</clipitem>")
            v2.append(f"<clipitem id=\"v2-{i}\">{common}<file id=\"file-1\"/>"
                      f"{_motion(layout, 'face', sw, sh, W, H, cam_h, cam_c)}</clipitem>")
        else:
            region = "full" if layout.get("mode") in ("full", "fit_blur") else "content"
            v1.append(f"<clipitem id=\"v1-{i}\">{common}{ffile}{_motion(layout, region, sw, sh, W, H)}</clipitem>")
            if layout.get("mode") == "pip" and face_track:
                pw = int(W * 0.42)
                v2.append(f"<clipitem id=\"v2-{i}\">{common}<file id=\"file-1\"/>"
                          f"{_motion(layout, 'face', sw, sh, pw, int(pw * 0.75), int(pw * 0.75))}</clipitem>")
        if source.get("has_audio"):
            a1.append(f"<clipitem id=\"a1-{i}\">{common}<file id=\"file-1\"/>"
                      f"<sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack></clipitem>")
    markers = []
    for seg in tl:
        if seg.get("role") in ("hook", "payoff"):
            markers.append(f"<marker><name>{seg['role'].upper()}</name><comment>source {seg['start']:.2f}s</comment>"
                           f"<in>{int(seg['out_start'] * tb)}</in><out>-1</out></marker>")
    for z in plan.get("zooms", []):
        markers.append(f"<marker><name>PUNCH-IN {z['scale']:.2f}x</name><comment>{z.get('duration', 1.5):.1f}s</comment>"
                       f"<in>{int(z['at'] * tb)}</in><out>-1</out></marker>")
    total = int(round((tl[-1]["out_end"] if tl else 0) * tb))
    video_fmt = (f"<format><samplecharacteristics>{rate}<width>{W}</width><height>{H}</height>"
                 f"<pixelaspectratio>square</pixelaspectratio></samplecharacteristics></format>")
    tracks = f"<track>{''.join(v1)}</track>" + (f"<track>{''.join(v2)}</track>" if v2 else "")
    audio = f"<audio><track>{''.join(a1)}</track></audio>" if a1 else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
<sequence id="jimbo-plan-{plan_row['id']}">
<name>{name}</name><duration>{total}</duration>{rate}
<timecode>{rate}<string>00:00:00:00</string><frame>0</frame><displayformat>NDF</displayformat></timecode>
{''.join(markers)}
<media><video>{video_fmt}{tracks}</video>{audio}</media>
</sequence>
</xmeml>
"""


def premiere_import_script(xml_path: Path, srt_path: Path | None) -> str:
    files = [xml_path.resolve().as_posix()] + ([srt_path.resolve().as_posix()] if srt_path else [])
    arr = ", ".join(f'"{f}"' for f in files)
    return f"""// Jimbo4prezz Studio — import an edit plan into the open Premiere Pro project.
// Premiere: File > Scripts (or run via ExtendScript / the "Commands" panel).
#target premierepro
var files = [{arr}];
if (app.project) {{
  app.project.importFiles(files, true, app.project.getInsertionBin(), false);
  alert("Imported Jimbo4prezz sequence" + (files.length > 1 ? " and captions" : "") + ".");
}} else {{ alert("Open a Premiere Pro project first."); }}
"""


def export_premiere(plan_id: int) -> dict:
    row = db.get("edit_plans", plan_id)
    if not row:
        raise ValueError("plan not found")
    source = db.get("sources", row["source_id"])
    out_dir = config.ADOBE_DIR / f"plan{plan_id}_v{row.get('version', 1)}_{row['platform']}"
    out_dir.mkdir(parents=True, exist_ok=True)
    xml_path = out_dir / "sequence.xml"
    xml_path.write_text(premiere_xml(row, source), encoding="utf-8")
    srt_path = None
    cap = row["plan"].get("captions") or {}
    if cap.get("lines"):
        lines = captions.map_to_output(cap["lines"], timeline(row["plan"]))
        srt_path = captions.write_srt(lines, out_dir / "captions.srt")
        cp = {"caption_style": cap.get("style"), "caption_size": cap.get("size", 1),
              "caption_position": cap.get("position"), "caption_uppercase": cap.get("uppercase"),
              "caption_animation": cap.get("animation")}
        captions.write_ass(lines, out_dir / "captions.ass", row["plan"]["format"]["width"],
                           row["plan"]["format"]["height"], cp)
    jsx = out_dir / "import_into_premiere.jsx"
    jsx.write_text(premiere_import_script(xml_path, srt_path), encoding="utf-8")
    installed = detect()["apps"]["premiere"]["installed"]
    db.log_operation("premiere_sequence", "adobe-premiere-export",
                     f"FCP XML sequence {row['plan']['format']['width']}x{row['plan']['format']['height']}"
                     f"{' + SRT captions' if srt_path else ''}; Premiere {'detected' if installed else 'not detected on this machine'}",
                     source_id=source["id"], plan_id=plan_id)
    return {"dir": str(out_dir), "xml": str(xml_path), "srt": str(srt_path) if srt_path else None,
            "jsx": str(jsx), "premiere_installed": installed}
