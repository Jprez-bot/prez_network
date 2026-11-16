"""Thumbnails (YouTube 1280×720) and vertical covers (1080×1920).

Concepts are built only from real frames of your footage: candidate frames are
scored for sharpness, exposure, face presence/size and how loud the moment
was (your reaction), then composited with Pillow. Every thumbnail also gets a
Photoshop hand-off: separate layer PNGs plus an ExtendScript (.jsx) that
rebuilds an editable layered PSD with a live text layer in Photoshop.
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from . import config, db, media
from .planner import timeline

SIZES = {"youtube": (1280, 720), "cover": (1080, 1920)}

THUMB_STYLE_DEFAULTS = {
    "text_max_words": 3, "text_color": "#FFFFFF", "accent_color": "#FFD400", "stroke": 0.012,
    "subject_side": "right", "contrast": 1.18, "saturation": 1.25, "vignette": 0.35,
    "text_position": "left", "uppercase": True, "subject_scale": 1.0,
}


def thumbnail_style() -> dict:
    """Thumbnail style profile = defaults ← thumbnail references (written notes win)."""
    style = dict(THUMB_STYLE_DEFAULTS)
    for r in db.query("SELECT * FROM refs WHERE level='thumbnail' AND status!='disabled' ORDER BY id"):
        ex = r.get("extracted") or {}
        style.update(ex.get("thumbnail_media") or {})
        style.update(ex.get("thumbnail_notes") or {})
    return style


def candidate_frames(source: dict, plan: dict | None, start: float, end: float, n: int = 12) -> list[dict]:
    analysis = db.get("analyses", source["id"], key="source_id") or {}
    audio = analysis.get("audio") or {}
    ex = audio.get("excitement") or []
    hop = audio.get("hop") or 0.5
    if plan:
        spans = [(s["start"], s["end"]) for s in timeline(plan) if s.get("role") != "hook"] or [(start, end)]
    else:
        spans = [(start, end)]
    total = sum(b - a for a, b in spans)
    times = []
    for i in range(n):
        target = total * (i + 0.5) / n
        acc = 0.0
        for a, b in spans:
            if acc + (b - a) >= target:
                times.append(a + (target - acc))
                break
            acc += b - a
    out = []
    for t, img in media.read_frames(source["path"], times, width=960):
        q = media.frame_quality(img)
        faces = media.detect_faces(img)
        loud = ex[int(t / hop)] if ex and int(t / hop) < len(ex) else 0
        face_area = faces[0][2] * faces[0][3] if faces else 0
        score = (min(q["sharpness"], 800) / 800 * 30 + (1 - abs(q["brightness"] - 125) / 125) * 15
                 + min(q["contrast"], 70) / 70 * 10 + min(face_area * 400, 25) + max(0, min(loud, 3)) / 3 * 20)
        out.append({"t": round(t, 2), "score": round(score, 1), "faces": faces[:1], "quality": q})
    out.sort(key=lambda f: f["score"], reverse=True)
    return out


def _font(size: int):
    path = config.thumbnail_font()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _cover(img: Image.Image, W: int, H: int, focus_x: float = 0.5, focus_y: float = 0.45) -> Image.Image:
    scale = max(W / img.width, H / img.height)
    rs = img.resize((int(img.width * scale + 0.5), int(img.height * scale + 0.5)), Image.LANCZOS)
    x = int(min(max(focus_x * rs.width - W / 2, 0), rs.width - W))
    y = int(min(max(focus_y * rs.height - H / 2, 0), rs.height - H))
    return rs.crop((x, y, x + W, y + H))


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def compose(frame_path: Path, kind: str, text: str, faces: list, out_dir: Path, style: dict) -> dict:
    W, H = SIZES[kind]
    src = Image.open(frame_path).convert("RGB")
    fx, fy = 0.5, 0.45
    if faces:
        b = faces[0]
        fx, fy = b[0] + b[2] / 2, b[1] + b[3] / 2
    focus_x = fx
    if kind == "youtube" and faces:
        # Put the face on the configured side, leaving room for text.
        focus_x = fx - 0.18 if style["subject_side"] == "right" else fx + 0.18
    bg = _cover(src, W, H, min(max(focus_x, 0), 1), fy)
    bg = ImageEnhance.Contrast(bg).enhance(style["contrast"])
    bg = ImageEnhance.Color(bg).enhance(style["saturation"])
    bg_layer = bg.copy()
    if style.get("vignette", 0) > 0:
        mask = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(mask)
        d.ellipse((-W * 0.25, -H * 0.25, W * 1.25, H * 1.25), fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(min(W, H) * 0.18))
        dark = ImageEnhance.Brightness(bg).enhance(1 - style["vignette"])
        bg = Image.composite(bg, dark, mask)

    subject_layer = None
    if faces and kind == "youtube":
        # Subject layer: enlarged crop around the face (real pixels from the frame).
        b = faces[0]
        sw_, sh_ = src.size
        size = int(max(b[2] * sw_, b[3] * sh_) * 2.6)
        cx, cy = int((b[0] + b[2] / 2) * sw_), int((b[1] + b[3] / 2) * sh_)
        box = (max(0, cx - size // 2), max(0, cy - size // 2), min(sw_, cx + size // 2), min(sh_, cy + size // 2))
        subj = src.crop(box)
        target_h = int(H * 0.92 * style.get("subject_scale", 1.0))
        subj = subj.resize((int(subj.width * target_h / subj.height), target_h), Image.LANCZOS)
        subj = ImageEnhance.Contrast(subj).enhance(style["contrast"])
        alpha = Image.new("L", subj.size, 0)
        ImageDraw.Draw(alpha).ellipse((subj.width * 0.04, subj.height * 0.02, subj.width * 0.96, subj.height * 1.1), fill=255)
        alpha = alpha.filter(ImageFilter.GaussianBlur(subj.width * 0.05))
        subj.putalpha(alpha)
        subject_layer = subj
        sx = W - subj.width - int(W * 0.02) if style["subject_side"] == "right" else int(W * 0.02)
        bg.paste(subj, (sx, H - subj.height), subj)
        subject_pos = (sx, H - subj.height)
    else:
        subject_pos = None

    words = text.split()[: int(style.get("text_max_words", 3)) + 2]
    text = " ".join(words)
    if style.get("uppercase"):
        text = text.upper()
    canvas = bg.copy()
    text_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    text_meta = None
    if text:
        draw = ImageDraw.Draw(text_layer)
        area_w = int(W * (0.55 if kind == "youtube" else 0.86))
        size = int(H * (0.2 if kind == "youtube" else 0.085))
        font = _font(size)
        lines = _wrap(draw, text, font, area_w)
        while (len(lines) > 3 or max(draw.textlength(ln, font=font) for ln in lines) > area_w) and size > 20:
            size = int(size * 0.9)
            font = _font(size)
            lines = _wrap(draw, text, font, area_w)
        stroke = max(2, int(size * style["stroke"] * 6))
        line_h = int(size * 1.05)
        block_h = line_h * len(lines)
        if kind == "youtube":
            x0 = int(W * 0.04) if style["subject_side"] == "right" else W - area_w - int(W * 0.04)
            y0 = int((H - block_h) / 2)
        else:
            x0 = int((W - area_w) / 2)
            y0 = int(H * 0.14)
        for i, ln in enumerate(lines):
            color = _hex(style["accent_color"]) if i == len(lines) - 1 and len(lines) > 1 else _hex(style["text_color"])
            lw = draw.textlength(ln, font=font)
            x = x0 if kind == "youtube" else int((W - lw) / 2)
            draw.text((x, y0 + i * line_h), ln, font=font, fill=color + (255,), stroke_width=stroke,
                      stroke_fill=(0, 0, 0, 255))
        text_meta = {"text": text, "lines": lines, "x": x0, "y": y0, "size": size, "stroke": stroke,
                     "color": style["text_color"], "accent": style["accent_color"],
                     "font": Path(config.thumbnail_font() or "").stem}
    canvas = Image.alpha_composite(canvas.convert("RGBA"), text_layer).convert("RGB")

    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{kind}.png"
    canvas.save(final, optimize=True)
    canvas.save(out_dir / f"{kind}.jpg", quality=92)
    bg_layer.save(out_dir / "layer_background.png")
    if subject_layer is not None:
        subject_layer.save(out_dir / "layer_subject.png")
    text_layer.save(out_dir / "layer_text.png")
    layers = {"size": [W, H], "background": "layer_background.png",
              "subject": "layer_subject.png" if subject_layer is not None else None,
              "subject_pos": subject_pos, "text": text_meta, "text_png": "layer_text.png",
              "style": style}
    (out_dir / "layers.json").write_text(json.dumps(layers, indent=2))
    jsx = photoshop_script(out_dir, layers)
    return {"path": str(final), "layers": {**layers, "jsx": str(jsx)}}


def photoshop_script(out_dir: Path, layers: dict) -> Path:
    """ExtendScript that builds an editable layered PSD (File > Scripts > Browse… in Photoshop)."""
    W, H = layers["size"]
    t = layers.get("text") or {}
    folder = out_dir.resolve().as_posix()
    text_js = ""
    if t:
        text_js = f"""
var tl = doc.artLayers.add(); tl.kind = LayerKind.TEXT; tl.name = "Title text (editable)";
var ti = tl.textItem; ti.contents = {json.dumps(chr(13).join(t.get('lines') or [t.get('text', '')]))};
ti.size = new UnitValue({t.get('size', 120)} * 72 / doc.resolution, "pt");
ti.position = [new UnitValue({t.get('x', 40)}, "px"), new UnitValue({t.get('y', 40) + t.get('size', 120) * 0.85}, "px")];
var c = new SolidColor(); c.rgb.hexValue = "{t.get('color', '#FFFFFF').lstrip('#')}"; ti.color = c;
try {{ ti.font = "{t.get('font') or 'Arial-Black'}"; }} catch (e) {{}}
ti.leading = new UnitValue({t.get('size', 120) * 1.05} * 72 / doc.resolution, "pt"); ti.useAutoLeading = false;
// Stroke: Layer Style > Stroke {t.get('stroke', 8)}px black (apply manually or via your saved style)
"""
    subject_js = ""
    if layers.get("subject"):
        sx, sy = layers.get("subject_pos") or (0, 0)
        subject_js = f"""placeLayer(folder + "/{layers['subject']}", "Subject (cutout)", {sx}, {sy});"""
    script = f"""// Jimbo4prezz Studio — Photoshop thumbnail builder
// Photoshop: File > Scripts > Browse… and pick this file. Builds an editable layered PSD.
#target photoshop
var folder = "{folder}";
app.preferences.rulerUnits = Units.PIXELS;
var doc = app.documents.add({W}, {H}, 72, "Jimbo4prezz thumbnail", NewDocumentMode.RGB, DocumentFill.TRANSPARENT);
function placeLayer(path, name, x, y) {{
  var f = new File(path); if (!f.exists) return null;
  var src = app.open(f); src.selection.selectAll(); src.selection.copy(); src.close(SaveOptions.DONOTSAVECHANGES);
  app.activeDocument = doc; var l = doc.paste(); l.name = name;
  var b = l.bounds; l.translate(x - b[0].value, y - b[1].value); return l;
}}
placeLayer(folder + "/{layers['background']}", "Background frame", 0, 0);
{subject_js}
{text_js}
var psd = new File(folder + "/thumbnail.psd");
var opts = new PhotoshopSaveOptions(); opts.layers = true;
doc.saveAs(psd, opts, true);
"""
    path = out_dir / "build_thumbnail.jsx"
    path.write_text(script, encoding="utf-8")
    return path


def short_text(title: str, max_words: int) -> str:
    """2-4 punchy words from the title, ending on a clause boundary when possible."""
    import re
    if max_words <= 0:
        return ""
    words = title.replace("…", "").split()
    for i in range(min(len(words), max_words + 1), 0, -1):
        if re.search(r"[!?.,:]$", words[i - 1]):
            return " ".join(words[:i]).rstrip(",:")
    return " ".join(words[:max_words])


def generate(plan_id: int | None = None, source_id: int | None = None, kind: str | None = None,
             frame_time: float | None = None, text: str | None = None) -> list[int]:
    plan_row = db.get("edit_plans", plan_id) if plan_id else None
    if plan_row:
        source_id = plan_row["source_id"]
    source = db.get("sources", source_id)
    if not source:
        raise ValueError("source not found")
    plan = plan_row["plan"] if plan_row else None
    if kind is None:
        kind = "youtube" if (not plan_row or plan_row["form"] == "long" or
                             plan["format"]["width"] > plan["format"]["height"]) else "cover"
    style = thumbnail_style()
    start, end = 0.0, source["duration"]
    if frame_time is not None:
        frames = [{"t": frame_time, "faces": [], "score": 0}]
        import cv2
        got = media.read_frames(source["path"], [frame_time], width=960)
        if got:
            frames[0]["faces"] = media.detect_faces(got[0][1])[:1]
    else:
        frames = candidate_frames(source, plan, start, end, n=10)[:3]
    if text is None:
        text = ""
        if plan_row:
            text = plan.get("thumbnail_text") or short_text(plan_row.get("title") or "", style["text_max_words"])
    ids = []
    for i, fr in enumerate(frames):
        out_dir = config.THUMBS_DIR / f"source_{source['id']}" / f"{'plan' + str(plan_id) if plan_id else 'src'}_{kind}_{int(db.now())}_{i}"
        frame_path = media.extract_frame(source["path"], fr["t"], out_dir / "frame.jpg")
        res = compose(frame_path, kind, text, fr.get("faces") or [], out_dir, style)
        tid = db.insert("thumbnails", plan_id=plan_id, source_id=source["id"], kind=kind, frame_time=fr["t"],
                        text=text, path=res["path"], layers=res["layers"], engine="pillow",
                        selected=1 if i == 0 and frame_time is None else 0, created_at=db.now())
        ids.append(tid)
    db.log_operation("thumbnail", "pillow",
                     f"{len(ids)} {kind} concept(s) + Photoshop .jsx hand-off (Photoshop builds the editable PSD)",
                     source_id=source["id"], plan_id=plan_id)
    return ids
