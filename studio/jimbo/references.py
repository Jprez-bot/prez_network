"""Style references: learn editing characteristics from examples.

A reference can be a creator, a whole video, a section (00:00–00:30), a
specific technique, a Twitch clip, a short-form example, a thumbnail, a caption
example, a Premiere example, or just a written instruction. Each is stored
separately with its level, platforms, content types and the aspects it is
meant to teach (pacing, captions, zooms, ...).

Two kinds of evidence are extracted and kept apart:
  * media_params  — measured from the example file (shot length, cut rate,
                    silence, caption presence/position, split-screen, ...)
  * note_params   — from what you wrote ("I like how quickly he gets to the
                    funny moment"). These always override media guesses.

Nothing is copied from the reference itself (no assets, branding, or edits) —
only parameter values for your own profile.
"""
from __future__ import annotations

import numpy as np

from . import db, llm, media, profiles

LEVELS = ["creator", "video", "section", "technique", "clip", "short_form", "thumbnail",
          "caption", "premiere", "instruction"]

ASPECT_PARAMS = {
    "pacing": ["pacing", "cut_aggressiveness", "silence_tolerance", "hook_max_seconds", "target_duration_min",
               "target_duration_max", "intro_max_seconds"],
    "cuts": ["cut_aggressiveness", "silence_tolerance", "context_preroll", "end_on_payoff", "payoff_tail"],
    "hook": ["hook_max_seconds", "structure", "intro_max_seconds"],
    "reactions": ["reaction_preservation", "payoff_tail"],
    "captions": [k for k in profiles.PARAM_SCHEMA if k.startswith("caption")],
    "zooms": ["zoom_per_minute", "zoom_intensity"],
    "framing": ["layout"],
    "audio": ["loudness_lufs", "denoise", "music", "sfx_frequency"],
    "graphics": ["broll", "transitions", "sfx_frequency"],
}

LEVEL_ASPECTS = {
    "caption": ["captions"], "thumbnail": [], "technique": None, "instruction": None,
}


def _allowed_keys(level: str, aspects: str) -> set[str]:
    chosen = [a.strip().lower() for a in (aspects or "").split(",") if a.strip()]
    if not chosen:
        chosen = LEVEL_ASPECTS.get(level) or list(ASPECT_PARAMS)
    keys: set[str] = set()
    for a in chosen:
        keys.update(ASPECT_PARAMS.get(a, []))
    return keys or set(profiles.PARAM_SCHEMA)


# ---------------------------------------------------------------------------
# Measuring a video example
# ---------------------------------------------------------------------------

def _trim_copy(path: str, start: float | None, end: float | None) -> str:
    if start is None and end is None:
        return path
    from . import config
    out = config.CACHE_DIR / f"refsec_{abs(hash((path, start, end)))}.mp4"
    if not out.exists():
        cmd = ["ffmpeg", "-v", "error", "-y"]
        if start:
            cmd += ["-ss", f"{start:.3f}"]
        cmd += ["-i", path]
        if end:
            cmd += ["-t", f"{end - (start or 0):.3f}"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", str(out)]
        media.run(cmd)
    return str(out)


def _caption_band(frames: list[np.ndarray]) -> dict:
    """Detect burned-in captions: rows of bright, outlined, word-sized blobs."""
    import cv2
    rows = []
    for img in frames:
        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        bright = (gray > 215).astype(np.uint8)
        dark = cv2.dilate((gray < 60).astype(np.uint8), np.ones((5, 5), np.uint8))
        n, _, stats, cent = cv2.connectedComponentsWithStats(bright, connectivity=8)
        ys = []
        for i in range(1, n):
            x, y, bw, bh, area = stats[i]
            if 0.012 * h < bh < 0.09 * h and bw < 0.25 * w and area > 12 and 0.1 * w < x < 0.9 * w:
                # outlined text: dark pixels around the glyph
                roi = dark[max(0, y - 3):y + bh + 3, max(0, x - 3):x + bw + 3]
                if roi.size and roi.mean() > 0.15:
                    ys.append(cent[i][1] / h)
        if len(ys) >= 3:
            hist, edges = np.histogram(ys, bins=20, range=(0, 1))
            k = int(np.argmax(hist))
            if hist[k] >= 3:
                rows.append((edges[k] + edges[k + 1]) / 2)
    if not frames:
        return {"likely": False}
    ratio = len(rows) / len(frames)
    if ratio < 0.35:
        return {"likely": False, "ratio": round(ratio, 2)}
    y = float(np.median(rows))
    pos = "upper" if y < 0.33 else "center" if y < 0.6 else "lower_third" if y < 0.82 else "lower"
    return {"likely": True, "ratio": round(ratio, 2), "y": round(y, 3), "position": pos}


def _split_seam(frames: list[np.ndarray]) -> float | None:
    seams = []
    for img in frames:
        g = img.mean(axis=2)
        h = g.shape[0]
        diff = np.abs(np.diff(g, axis=0)).mean(axis=1)
        lo, hi = int(h * 0.3), int(h * 0.75)
        k = int(np.argmax(diff[lo:hi])) + lo
        if diff[k] > 4 * (np.median(diff) + 1e-3):
            seams.append(k / h)
    if len(seams) >= max(3, 0.5 * len(frames)) and np.std(seams) < 0.02:
        return float(np.median(seams))
    return None


def measure_video(path: str, start: float | None, end: float | None) -> dict:
    clip = _trim_copy(path, start, end)
    info = media.probe(clip)
    dur = info["duration"] or 1
    cuts = media.scene_changes(clip, threshold=0.3, fps=6)
    shots = np.diff([0.0] + cuts + [dur]) if dur else np.array([dur])
    audio = media.audio_energy(clip) if info["has_audio"] else {"silences": [], "stats": {}}
    times = [dur * (i + 0.5) / 12 for i in range(12)]
    frames = [img for _, img in media.read_frames(clip, times, width=540)]
    cap = _caption_band(frames)
    seam = _split_seam(frames) if info["height"] > info["width"] else None
    faces = [media.detect_faces(f) for f in frames]
    face_ratio = sum(1 for f in faces if f) / max(1, len(frames))
    pauses = [b - a for a, b in audio.get("silences", [])]
    m = {
        "duration": round(dur, 2), "aspect": f"{info['width']}x{info['height']}",
        "vertical": info["height"] > info["width"],
        "cuts": len(cuts), "cuts_per_minute": round(len(cuts) / dur * 60, 2),
        "avg_shot_seconds": round(float(np.mean(shots)), 2) if len(shots) else dur,
        "median_shot_seconds": round(float(np.median(shots)), 2) if len(shots) else dur,
        "silence_ratio": (audio.get("stats") or {}).get("silence_ratio"),
        "longest_pause": round(max(pauses), 2) if pauses else 0,
        "loudness_median_db": (audio.get("stats") or {}).get("median_db"),
        "captions": cap, "split_screen_seam": round(seam, 3) if seam else None,
        "face_presence": round(face_ratio, 2),
        "first_cut_seconds": round(cuts[0], 2) if cuts else None,
    }
    return m


def media_to_params(m: dict, level: str) -> tuple[dict, list[str]]:
    p: dict = {}
    why: list[str] = []
    cpm = m.get("cuts_per_minute", 0)
    if m.get("duration", 0) >= 8:
        if cpm >= 25:
            p.update(pacing="aggressive", cut_aggressiveness=0.85)
        elif cpm >= 12:
            p.update(pacing="fast", cut_aggressiveness=0.7)
        elif cpm >= 5:
            p.update(pacing="natural", cut_aggressiveness=0.5)
        else:
            p.update(pacing="relaxed", cut_aggressiveness=0.3)
        why.append(f"{cpm} cuts/min, avg shot {m.get('avg_shot_seconds')}s → pacing {p['pacing']}")
    lp = m.get("longest_pause")
    if lp is not None and m.get("silence_ratio") is not None:
        p["silence_tolerance"] = max(0.25, min(2.0, lp if lp else 0.35))
        why.append(f"longest pause {lp}s, silence {m['silence_ratio']:.0%} → keeps pauses up to {p['silence_tolerance']:.2f}s")
    if m.get("vertical") and level in ("short_form", "clip", "video", "section", "creator", "technique"):
        d = m.get("duration", 0)
        if 5 < d < 180:
            p["target_duration_max"] = round(d * 1.15, 1)
            p["target_duration_min"] = round(max(8, d * 0.6), 1)
            why.append(f"example runs {d:.0f}s → target {p['target_duration_min']:.0f}–{p['target_duration_max']:.0f}s")
    fc = m.get("first_cut_seconds")
    if fc is not None and fc < 3:
        p["hook_max_seconds"] = max(0.8, round(fc, 1))
        why.append(f"first cut at {fc}s → fast hook")
    cap = m.get("captions") or {}
    if cap.get("likely"):
        p["captions_enabled"] = True
        p["caption_position"] = cap["position"]
        why.append(f"burned-in captions detected ({cap['ratio']:.0%} of frames) at {cap['position'].replace('_', ' ')}")
    elif cap and m.get("vertical"):
        p["captions_enabled"] = False
        why.append("no burned-in captions detected")
    if m.get("split_screen_seam"):
        p["layout"] = "split"
        why.append(f"split-screen layout (seam at {m['split_screen_seam']:.0%} height)")
    return profiles.clean_params(p), why


def measure_thumbnail(path: str) -> tuple[dict, dict]:
    import cv2
    img = cv2.imread(path)
    if img is None:
        raise ValueError("could not read image")
    h, w = img.shape[:2]
    q = media.frame_quality(img)
    faces = media.detect_faces(img)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    sat_mask = hsv[:, :, 1] > 150
    accent = None
    if sat_mask.mean() > 0.02:
        px = img[sat_mask]
        b, g, r = np.median(px, axis=0).astype(int).tolist()
        accent = f"#{r:02X}{g:02X}{b:02X}"
    side = None
    if faces:
        cx = faces[0][0] + faces[0][2] / 2
        side = "right" if cx > 0.5 else "left"
    m = {"size": [w, h], "brightness": round(q["brightness"], 1), "contrast": round(q["contrast"], 1),
         "saturation": round(q["saturation"], 1), "faces": faces[:1], "accent_color": accent,
         "face_area": round(faces[0][2] * faces[0][3], 3) if faces else 0}
    style = {}
    if side:
        style["subject_side"] = side
    if m["saturation"] > 110:
        style["saturation"] = 1.35
    elif m["saturation"] < 60:
        style["saturation"] = 1.05
    if m["contrast"] > 65:
        style["contrast"] = 1.25
    if accent:
        style["accent_color"] = accent
    if m["face_area"] > 0.08:
        style["subject_scale"] = 1.12
    return m, style


THUMB_NOTE_RULES = [
    (r"simple|minimal|clean", {"text_max_words": 2}),
    (r"no text|without text", {"text_max_words": 0}),
    (r"(lots of|more|big) text", {"text_max_words": 4}),
    (r"face (on|at) the left|left side", {"subject_side": "left"}),
    (r"face (on|at) the right|right side", {"subject_side": "right"}),
    (r"dramatic|expression|big face|close.?up", {"subject_scale": 1.15}),
    (r"bright|vibrant|colorful|colourful|saturated", {"saturation": 1.4}),
    (r"contrast|punchy|pop", {"contrast": 1.28}),
    (r"dark (background|vignette)|vignette", {"vignette": 0.5}),
    (r"lower ?case|not all caps", {"uppercase": False}),
]


def thumb_params_from_notes(notes: str) -> dict:
    import re
    out = {}
    for pat, ch in THUMB_NOTE_RULES:
        if re.search(pat, (notes or "").lower()):
            out.update(ch)
    return out


# ---------------------------------------------------------------------------
# Claude: turn your written explanation into parameters
# ---------------------------------------------------------------------------

def _llm_note_params(ref: dict, measured: dict | None, allowed: set[str]) -> tuple[dict, list[dict]]:
    props = {}
    for k in sorted(allowed):
        spec = profiles.PARAM_SCHEMA[k]
        if spec["type"] == "number":
            props[k] = {"type": "number"}
        elif spec["type"] == "bool":
            props[k] = {"type": "boolean"}
        else:
            props[k] = {"type": "string", "enum": spec["options"]}
    schema = {
        "type": "object", "additionalProperties": False, "required": ["changes", "observations"],
        "properties": {
            "changes": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["param", "value", "because"],
                "properties": {"param": {"type": "string", "enum": sorted(allowed)},
                               "value": {"type": "string"}, "because": {"type": "string"}}}},
            "observations": {"type": "array", "items": {"type": "string"}},
        }}
    ranges = "\n".join(f"- {k}: {profiles.PARAM_SCHEMA[k]['label']} "
                       f"({profiles.PARAM_SCHEMA[k].get('options') or [profiles.PARAM_SCHEMA[k].get('min'), profiles.PARAM_SCHEMA[k].get('max')] if profiles.PARAM_SCHEMA[k]['type'] != 'bool' else 'true/false'})"
                       for k in sorted(allowed))
    prompt = (f"A style reference for Jimbo4prezz's editing. Level: {ref['level']}. Name: {ref.get('name')}. "
              f"Creator: {ref.get('creator') or '-'}. URL: {ref.get('url') or '-'}. "
              f"Section: {ref.get('section_start')}–{ref.get('section_end')}. Platforms: {ref.get('platforms')}. "
              f"Content types: {ref.get('content_types')}. Aspects: {ref.get('aspects') or 'all'}.\n\n"
              f"What Jimbo wrote about it (highest priority):\n{ref.get('notes') or '(nothing written)'}\n\n"
              f"Measured from the example file: {measured or '(no file provided)'}\n\n"
              f"Translate ONLY what is supported by his words (and the measurements if present) into values for these "
              f"editing parameters:\n{ranges}\n\nReturn just the parameters that should change, values as strings. "
              "Do not imitate the creator's identity, branding, or assets; extract general editing principles only. "
              "Also list 2-6 short observations describing the principle he likes.")
    res = llm.ask_json(llm.SYSTEM_EDITOR, prompt, schema, max_tokens=6000)
    out, why = {}, []
    for ch in res.get("changes", []):
        v = ch["value"]
        spec = profiles.PARAM_SCHEMA.get(ch["param"])
        if not spec:
            continue
        if spec["type"] == "bool":
            v = str(v).lower() in ("true", "1", "yes")
        cv = profiles.clamp_param(ch["param"], v)
        if cv is not None:
            out[ch["param"]] = cv
            why.append({"param": ch["param"], "value": cv, "because": ch["because"]})
    return out, why + [{"observation": o} for o in res.get("observations", [])]


def analyze_reference(ref_id: int, progress=lambda p, m: None) -> dict:
    ref = db.get("refs", ref_id)
    if not ref:
        raise ValueError("reference not found")
    db.update("refs", ref_id, status="analyzing")
    extracted: dict = {"engine": []}
    allowed = _allowed_keys(ref["level"], ref.get("aspects") or "")
    measured = None
    try:
        if ref["level"] == "thumbnail":
            if ref.get("path"):
                m, style = measure_thumbnail(ref["path"])
                extracted["thumbnail_measured"] = m
                extracted["thumbnail_media"] = style
                extracted["engine"].append("opencv")
            extracted["thumbnail_notes"] = thumb_params_from_notes(ref.get("notes") or "")
            db.log_operation("reference_analysis", "opencv", f"thumbnail reference #{ref_id}")
        else:
            if ref.get("path"):
                progress(0.2, "Measuring example video")
                measured = measure_video(ref["path"], ref.get("section_start"), ref.get("section_end"))
                mp, why = media_to_params(measured, ref["level"])
                extracted["measured"] = measured
                extracted["media_params"] = {k: v for k, v in mp.items() if k in allowed}
                extracted["media_why"] = why
                extracted["engine"].append("ffmpeg/opencv")
            progress(0.6, "Reading your notes")
            rule_params, matched = profiles.params_from_notes(ref.get("notes") or "")
            note_params = {k: v for k, v in rule_params.items() if k in allowed}
            interpretation = [{"rule": m} for m in matched]
            if (ref.get("notes") or "").strip() and llm.available():
                try:
                    lp, why = _llm_note_params(ref, measured, allowed)
                    note_params.update(lp)
                    interpretation = why + interpretation
                    extracted["engine"].append("claude")
                except Exception as e:  # noqa: BLE001
                    interpretation.append({"observation": f"Claude unavailable, used keyword rules ({e!r})"})
            extracted["note_params"] = note_params
            extracted["interpretation"] = interpretation
            db.log_operation("reference_analysis", "+".join(extracted["engine"]) or "heuristic",
                             f"reference #{ref_id}: {len(extracted.get('media_params', {}))} measured, "
                             f"{len(note_params)} from notes")
        db.update("refs", ref_id, extracted=extracted, status="analyzed")
    except Exception as e:
        db.update("refs", ref_id, extracted={**extracted, "error": str(e)}, status="failed")
        raise
    progress(1.0, "Reference analysed")
    return extracted


def auto_link_profiles(ref: dict) -> list[int]:
    """Pick profiles matching the reference's platforms/content types (when none chosen)."""
    plats = ref.get("platforms") or []
    ctypes = ref.get("content_types") or []
    ids = []
    for p in db.query("SELECT * FROM style_profiles"):
        if plats and p["platform"] not in plats:
            continue
        if ctypes and not set(ctypes) & set(p.get("content_types") or []):
            continue
        if not plats and not ctypes:
            continue
        ids.append(p["id"])
    return ids
