"""Platform decision layer + edit-plan generation.

For each moment it decides which destinations it belongs on (possibly none),
then builds a *separate* edit plan per platform from the same source:
different trim, structure (hook-first vs context-first), silence removal,
framing/layout, captions and punch-ins, all driven by the platform spec and
the selected style profile. Plans are editable JSON; originals are untouched.

Plan JSON:
{
  "segments": [{"start", "end", "role": hook|context|payoff|body, "enabled": true}],  # play order, source time
  "removed":  [{"start", "end", "reason"}],                                       # restorable
  "structure": "hook_first" | "context_first",
  "format": {"aspect", "width", "height"},
  "layout": {"mode": crop|split|pip|fit_blur|full, "content": {cx, cy, zoom},
             "face": {cx, cy, zoom} | null, "split_ratio", "face_position", "override": false},
  "captions": {"enabled", "style", ...caption params, "lines": [...]},
  "zooms": [{"at": out_seconds, "duration", "scale"}],
  "audio": {"loudnorm": true, "lufs": -14, "denoise": false},
  "params_used": {...}, "notes": [...]
}
"""
from __future__ import annotations

import copy
import re

import numpy as np

from . import captions, config, db, llm, profiles, transcribe
from .analyzer import snap_start
from .platforms import PLATFORMS, SHORT_PLATFORMS, resolution

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ctx(source_id: int) -> tuple[dict, dict, list[dict]]:
    source = db.get("sources", source_id)
    analysis = db.get("analyses", source_id, key="source_id") or {}
    tr = db.get("transcripts", source_id, key="source_id") or {}
    return source, analysis, tr.get("segments") or []


def timeline(plan: dict) -> list[dict]:
    out, t = [], 0.0
    for s in plan.get("segments", []):
        if not s.get("enabled", True) or s["end"] - s["start"] < 0.05:
            continue
        out.append({**s, "out_start": t, "out_end": t + (s["end"] - s["start"])})
        t += s["end"] - s["start"]
    return out


def plan_duration(plan: dict) -> float:
    tl = timeline(plan)
    return tl[-1]["out_end"] if tl else 0.0


def source_to_output(plan: dict, t: float) -> float | None:
    for seg in timeline(plan):
        if seg["start"] <= t <= seg["end"] and seg.get("role") != "hook":
            return seg["out_start"] + (t - seg["start"])
    return None


def normalize_segments(segments: list[dict]) -> list[dict]:
    """Keep hook teasers first (in given order); sort and merge the rest."""
    hooks = [dict(s) for s in segments if s.get("role") == "hook"]
    body = sorted((dict(s) for s in segments if s.get("role") != "hook"), key=lambda s: s["start"])
    merged: list[dict] = []
    for s in body:
        s["start"], s["end"] = round(float(s["start"]), 3), round(float(s["end"]), 3)
        if s["end"] <= s["start"]:
            continue
        s.setdefault("enabled", True)
        if merged and s["start"] <= merged[-1]["end"] + 0.04 and s.get("enabled") == merged[-1].get("enabled"):
            merged[-1]["end"] = max(merged[-1]["end"], s["end"])
        else:
            merged.append(s)
    return hooks + merged


def _silence_gaps(start: float, end: float, words: list[dict], silences: list[list[float]],
                  tolerance: float, audio: dict) -> list[tuple[float, float]]:
    """Pauses longer than ``tolerance`` with no speech and quiet audio.

    Loud non-speech (laughing, yelling at the game) is *not* silence, so
    reactions survive silence removal.
    """
    gaps = []
    quiet = [(max(a, start), min(b, end)) for a, b in silences if b > start and a < end]
    if words:
        ws = [w for w in words if w["e"] > start and w["s"] < end]
        speech_gaps = []
        prev = start
        for w in ws:
            if w["s"] - prev > tolerance:
                speech_gaps.append((prev, w["s"]))
            prev = max(prev, w["e"])
        if end - prev > tolerance:
            speech_gaps.append((prev, end))
        # Intersect speech gaps with quiet audio when we have audio; else trust words.
        if audio.get("db"):
            for a, b in speech_gaps:
                for qa, qb in quiet:
                    s, e = max(a, qa), min(b, qb)
                    if e - s > tolerance:
                        gaps.append((s, e))
        else:
            gaps = speech_gaps
    else:
        gaps = [(a, b) for a, b in quiet if b - a > tolerance]
    return gaps


def _cut_gaps(spans: list[tuple[float, float]], gaps: list[tuple[float, float]], keep: float):
    """Remove gaps from spans, leaving ``keep``/2 of breathing room on each side."""
    removed = []
    for ga, gb in gaps:
        ca, cb = ga + keep / 2, gb - keep / 2
        if cb - ca < 0.2:
            continue
        new = []
        for a, b in spans:
            if cb <= a or ca >= b:
                new.append((a, b))
                continue
            if ca > a:
                new.append((a, ca))
            if cb < b:
                new.append((cb, b))
            removed.append({"start": round(max(ca, a), 3), "end": round(min(cb, b), 3), "reason": "silence"})
        spans = new
    return [(a, b) for a, b in spans if b - a >= 0.25], removed


def _loud_points(analysis: dict, a: float, b: float, n: int, min_gap: float) -> list[float]:
    audio = analysis.get("audio") or {}
    ex = audio.get("excitement") or audio.get("db") or []
    hop = audio.get("hop") or 0.5
    if not ex or n <= 0:
        return []
    lo, hi = int(a / hop), int(b / hop)
    seg = np.array(ex[lo:hi], dtype=float)
    if len(seg) == 0:
        return []
    order = np.argsort(seg)[::-1]
    pts: list[float] = []
    for i in order:
        t = (lo + int(i)) * hop
        if all(abs(t - p) >= min_gap for p in pts):
            pts.append(t)
        if len(pts) >= n:
            break
    return sorted(pts)


def _sents(segments):
    return transcribe.sentences(segments)


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


def choose_layout(source: dict, moment: dict | None, aspect: str, params: dict, category: str) -> dict:
    sw, sh = source.get("width") or 1920, source.get("height") or 1080
    src_aspect = sw / max(1, sh)
    face_info = ((moment or {}).get("signals") or {}).get("face") or {}
    if not face_info:
        an = db.get("analyses", source["id"], key="source_id") or {}
        face_info = an.get("faces") or {}
    box = face_info.get("box")
    found = face_info.get("found_ratio", 0)
    target = {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1.0}.get(aspect, 9 / 16)
    layout = {"mode": "full", "content": {"cx": 0.5, "cy": 0.5, "zoom": 1.0}, "face": None,
              "split_ratio": 0.62, "face_position": "bottom", "override": False, "reason": ""}
    if abs(src_aspect - target) < 0.05:
        layout["reason"] = "Source already matches the destination aspect."
        return layout
    pref = params.get("layout", "auto")
    face = None
    if box and found >= 0.4:
        fx, fy, fw, fh = box
        face = {"cx": round(fx + fw / 2, 4), "cy": round(fy + fh / 2, 4),
                "zoom": round(min(1.0, max(0.18, fh * 3.2)), 4)}
    small_cam = bool(box) and found >= 0.4 and box[2] < 0.16  # facecam overlay in a gameplay/screen capture
    if target >= src_aspect:  # e.g. vertical source to 16:9 — never crop people out
        layout.update(mode="fit_blur", reason="Taller source than destination: blurred fill.")
        return layout
    mode = pref
    if pref == "full" and target < 1:
        mode = "auto"  # 'full' only makes sense for landscape destinations
    if mode == "auto":
        if small_cam and category in ("gaming", "reaction", "auto", "highlight", "commentary", None):
            mode = "split"
            layout["reason"] = "Small facecam over gameplay/screen: split-screen keeps both the action and your reaction."
        elif face and not small_cam:
            mode = "crop"
            layout["reason"] = "Face-camera content: vertical crop that follows your face."
        elif category == "gaming":
            mode = "crop"
            layout["reason"] = "Gameplay with no facecam found: centered action crop."
        else:
            mode = "fit_blur"
            layout["reason"] = "No clear subject found: full frame over blurred fill (nothing cropped out)."
    else:
        layout["reason"] = f"Layout '{mode}' from style profile."
    layout["mode"] = mode
    if mode == "crop" and face and not small_cam:
        layout["content"] = {"cx": face["cx"], "cy": 0.5, "zoom": 1.0}
    if mode in ("split", "pip"):
        if face:
            layout["face"] = face
        else:
            layout["face"] = {"cx": 0.85, "cy": 0.8, "zoom": 0.3}
            layout["reason"] += " Facecam position guessed (bottom-right); adjust if needed."
        if small_cam and box:
            # Keep gameplay centered but away from the facecam corner.
            layout["content"] = {"cx": 0.5, "cy": 0.5, "zoom": 1.0}
    return layout


# ---------------------------------------------------------------------------
# Short-form
# ---------------------------------------------------------------------------


def build_short_plan(source: dict, analysis: dict, segments: list[dict], moment: dict,
                     platform: str, profile: dict, aspect: str | None = None) -> dict:
    params = profiles.effective_params(profile)
    spec = PLATFORMS[platform]
    aspect, W, H = resolution(platform, aspect or _x_aspect(platform, source, moment))
    tmin = float(params["target_duration_min"])
    tmax = min(float(params["target_duration_max"]), spec["max_duration"])
    sents = _sents(segments)
    words = transcribe.all_words(segments)
    audio = analysis.get("audio") or {}
    silences = audio.get("silences") or []
    notes: list[str] = []
    is_clip = (source.get("duration") or 0) <= 180

    hook_s, hook_e = float(moment["hook_start"]), float(moment["hook_end"])
    payoff = float(moment.get("payoff_end") or moment["end"])
    react = params["payoff_tail"] * (0.5 + params["reaction_preservation"] * 0.5)
    end = min(float(moment["end"]) + react, payoff + react) if params["end_on_payoff"] else float(moment["end"])
    end = min(end, source["duration"])
    end = max(end, hook_e + 0.3)
    start = float(moment["start"])
    # Context preroll from the profile (how much setup this style keeps).
    min_start = max(0.0, hook_s - params["context_preroll"] - 10 * (1 - params["cut_aggressiveness"]))
    start = max(start, min_start)
    if sents:
        start = min(snap_start(start, sents), hook_s)

    spans = [(start, end)]
    tol = float(params["silence_tolerance"])
    gaps = _silence_gaps(start, end, words, silences, tol, audio)
    spans, removed = _cut_gaps(spans, gaps, keep=min(tol, 0.4))
    if removed:
        notes.append(f"Removed {len(removed)} pauses longer than {tol:.2f}s (loud reactions kept).")

    def total(sp):
        return sum(b - a for a, b in sp)

    # Structure decision
    structure = params["structure"]
    hook_len = min(hook_e - hook_s, 3.5)
    lead_in = sum(min(b, hook_s) - a for a, b in spans if a < hook_s)
    if structure == "auto":
        if lead_in > max(params["hook_max_seconds"] * 2.5, 6) and moment.get("structure") != "context_first":
            structure = "hook_first"
        elif moment.get("structure") == "hook_first" and lead_in > params["hook_max_seconds"]:
            structure = "hook_first"
        else:
            structure = "context_first"
    budget = tmax - (hook_len if structure == "hook_first" else 0)

    # Trim context from the front (sentence by sentence) until it fits / hook lands in time.
    def trim_front(sp, limit_start):
        if not sp:
            return sp
        cut_points = [s["start"] for s in sents if sp[0][0] < s["start"] < limit_start] or \
                     [t for t in np.arange(sp[0][0], limit_start, 1.0).tolist()[1:]]
        for cp in cut_points:
            if total(sp) <= budget and (structure == "hook_first" or
                                       sum(min(b, hook_s) - a for a, b in sp if a < hook_s) <= params["hook_max_seconds"] * 4):
                break
            removed.append({"start": round(sp[0][0], 3), "end": round(cp, 3), "reason": "context trim"})
            sp = [(max(a, cp), b) for a, b in sp if b > cp]
        return sp

    min_context = 1.5 if structure == "hook_first" else 0.8
    spans = trim_front(spans, max(spans[0][0], hook_s - min_context))
    if total(spans) > budget:
        # Still long: drop the quietest middle sentences between context and hook.
        mids = [s for s in sents if s["start"] > spans[0][0] + 1 and s["end"] < hook_s - 0.5]
        ex = (audio.get("excitement") or [])
        hop = audio.get("hop") or 0.5

        def energy(s):
            seg = ex[int(s["start"] / hop):int(s["end"] / hop) + 1]
            return float(np.mean(seg)) if seg else 0.0
        for s in sorted(mids, key=energy):
            if total(spans) <= budget:
                break
            spans, rem = _cut_gaps(spans, [(s["start"], s["end"])], keep=0.0)
            for r in rem:
                r["reason"] = "low-energy middle"
            removed.extend(rem)
    if total(spans) > budget:
        # Last resort: shorten the tail, never cutting before the payoff.
        over = total(spans) - budget
        a, b = spans[-1]
        new_b = max(payoff, b - over, a + 0.5)
        if new_b < b:
            removed.append({"start": round(new_b, 3), "end": round(b, 3), "reason": "length"})
            spans[-1] = (a, new_b)
    if total(spans) + (hook_len if structure == "hook_first" else 0) > tmax + 0.5:
        notes.append(f"Essential context + payoff is {total(spans):.0f}s, longer than this profile's {tmax:.0f}s target.")
    if total(spans) < tmin and not is_clip:
        # Extend context backwards so the clip isn't too thin.
        need = tmin - total(spans)
        new_start = max(0.0, spans[0][0] - need)
        if sents:
            new_start = min(snap_start(new_start, sents), spans[0][0])
        spans[0] = (new_start, spans[0][1])
        notes.append("Extended context to reach the minimum duration.")

    segs = []
    if structure == "hook_first":
        segs.append({"start": round(hook_s, 3), "end": round(hook_s + hook_len, 3), "role": "hook", "enabled": True})
        notes.append("Hook → Context → Payoff: opens on the strongest line, then plays the setup.")
    else:
        notes.append("Context → Build-up → Payoff.")
    for a, b in spans:
        parts = [(a, hook_s), (hook_s, b)] if a < hook_s - 0.3 and b > hook_s + 0.3 else [(a, b)]
        for x, y in parts:
            role = "context" if y <= hook_s + 0.01 else "payoff"
            segs.append({"start": round(x, 3), "end": round(y, 3), "role": role, "enabled": True})
    segs = [s for s in segs if s["role"] == "hook"] + sorted((s for s in segs if s["role"] != "hook"),
                                                              key=lambda s: s["start"])

    category = moment.get("content_type") or source.get("category") or "auto"
    layout = choose_layout(source, moment, aspect, params, category)
    plan = {
        "segments": segs, "removed": removed, "structure": structure,
        "format": {"aspect": aspect, "width": W, "height": H},
        "layout": layout,
        "audio": {"loudnorm": True, "lufs": params["loudness_lufs"], "denoise": bool(params["denoise"])},
        "notes": notes,
    }
    _add_captions_and_zooms(plan, segments, analysis, params, moment, layout)
    plan["params_used"] = {k: v for k, v in params.items() if not k.startswith("_")}
    return plan


def _x_aspect(platform: str, source: dict, moment: dict) -> str | None:
    if platform != "x":
        return None
    face = ((moment or {}).get("signals") or {}).get("face") or {}
    sw, sh = source.get("width") or 16, source.get("height") or 9
    if sw < sh:
        return "9:16"
    # Face-dominant talking clips read better square on the X timeline.
    if face.get("box") and face["box"][2] >= 0.22:
        return "1:1"
    return "16:9"


def _add_captions_and_zooms(plan: dict, segments: list[dict], analysis: dict, params: dict,
                            moment: dict | None, layout: dict) -> None:
    tl = timeline(plan)
    spans = [(s["start"], s["end"]) for s in tl]
    loud = []
    for s in tl:
        loud += _loud_points(analysis, s["start"], s["end"], 2, 1.0)
    emphasis = (((moment or {}).get("signals") or {}).get("emphasis_words")) or []
    lines = captions.build_lines(segments, spans, params, emphasis, loud) if segments else []
    plan["captions"] = {
        "enabled": bool(params["captions_enabled"]) and bool(lines),
        "style": params["caption_style"], "words_per_chunk": params["caption_words_per_chunk"],
        "uppercase": params["caption_uppercase"], "position": params["caption_position"],
        "size": params["caption_size"], "animation": params["caption_animation"],
        "emphasis": params["caption_emphasis"], "lines": lines,
        "missing_transcript": not segments,
    }
    dur = tl[-1]["out_end"] if tl else 0
    n = int(round(params["zoom_per_minute"] * dur / 60))
    zooms = []
    if n > 0 and layout.get("mode") != "split":
        # Punch-ins on the loudest reaction beats, spaced out, never on the first second.
        candidates = []
        for s in tl:
            if s.get("role") == "hook" and plan.get("structure") == "hook_first":
                continue
            for t in _loud_points(analysis, s["start"], s["end"], 3, 2.0):
                candidates.append((t - s["start"] + s["out_start"], t))
        audio = analysis.get("audio") or {}
        ex = audio.get("excitement") or []
        hop = audio.get("hop") or 0.5
        candidates.sort(key=lambda c: -(ex[int(c[1] / hop)] if ex and int(c[1] / hop) < len(ex) else 0))
        min_gap = max(3.0, 60 / max(params["zoom_per_minute"], 0.1) * 0.5)
        for out_t, _ in candidates:
            if out_t < 1.0 or out_t > dur - 0.8:
                continue
            if all(abs(out_t - z["at"]) >= min_gap for z in zooms):
                zooms.append({"at": round(max(0, out_t - 0.25), 2),
                              "duration": round(min(2.4, 1.0 + params["reaction_preservation"]), 2),
                              "scale": round(params["zoom_intensity"], 3)})
            if len(zooms) >= n:
                break
    plan["zooms"] = sorted(zooms, key=lambda z: z["at"])


def refresh_captions(plan: dict, source_id: int, params: dict | None = None) -> dict:
    """Rebuild caption lines after the cut changed (keeps your style choices)."""
    source, analysis, segments = _ctx(source_id)
    cap = plan.get("captions") or {}
    p = dict(params or plan.get("params_used") or profiles.BASE)
    p.update({"caption_words_per_chunk": cap.get("words_per_chunk", p.get("caption_words_per_chunk", 3)),
              "caption_emphasis": cap.get("emphasis", p.get("caption_emphasis", 0.3))})
    tl = timeline(plan)
    loud = []
    for s in tl:
        loud += _loud_points(analysis, s["start"], s["end"], 2, 1.0)
    cap["lines"] = captions.build_lines(segments, [(s["start"], s["end"]) for s in tl], p, [], loud)
    plan["captions"] = cap
    return plan


# ---------------------------------------------------------------------------
# Long-form
# ---------------------------------------------------------------------------


def build_long_plan(source: dict, analysis: dict, segments: list[dict], ranges: list[tuple[float, float]],
                    profile: dict, cold_open: dict | None = None) -> dict:
    params = profiles.effective_params(profile)
    aspect, W, H = resolution("youtube", "16:9")
    words = transcribe.all_words(segments)
    audio = analysis.get("audio") or {}
    silences = audio.get("silences") or []
    tol = float(params["silence_tolerance"])
    segs, removed, notes = [], [], []
    if cold_open and (params["structure"] == "hook_first" or cold_open.get("force")):
        segs.append({"start": cold_open["start"], "end": cold_open["end"], "role": "hook", "enabled": True})
        notes.append("Cold open: teaser of the strongest moment before the story starts.")
    for a, b in ranges:
        gaps = _silence_gaps(a, b, words, silences, tol, audio)
        # Long-form keeps natural rhythm: only cut genuinely dead air.
        spans, rem = _cut_gaps([(a, b)], gaps, keep=min(tol, 0.8))
        removed += rem
        for x, y in spans:
            segs.append({"start": round(x, 3), "end": round(y, 3), "role": "body", "enabled": True})
    # Avoid a jumpy montage: merge segments separated by tiny cuts.
    merged = []
    for s in segs:
        if merged and s["role"] == merged[-1]["role"] == "body" and s["start"] - merged[-1]["end"] < 0.6:
            merged[-1]["end"] = s["end"]
        else:
            merged.append(s)
    if removed:
        notes.append(f"Removed {len(removed)} dead-air pauses longer than {tol:.1f}s; chronology preserved.")
    layout = {"mode": "full", "content": {"cx": 0.5, "cy": 0.5, "zoom": 1.0}, "face": None,
              "split_ratio": 0.62, "face_position": "bottom", "override": False,
              "reason": "Long-form keeps the original framing."}
    if (source.get("width") or 16) < (source.get("height") or 9):
        layout["mode"] = "fit_blur"
    plan = {"segments": merged, "removed": removed, "structure": "context_first" if not cold_open else "cold_open",
            "format": {"aspect": aspect, "width": W, "height": H}, "layout": layout,
            "audio": {"loudnorm": True, "lufs": params["loudness_lufs"], "denoise": bool(params["denoise"])},
            "notes": notes}
    _add_captions_and_zooms(plan, segments, analysis, params, None, layout)
    plan["params_used"] = {k: v for k, v in params.items() if not k.startswith("_")}
    return plan


# ---------------------------------------------------------------------------
# Titles / descriptions
# ---------------------------------------------------------------------------

_META_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["platform", "title", "description", "tags", "thumbnail_text"],
        "properties": {"platform": {"type": "string"}, "title": {"type": "string"},
                       "description": {"type": "string"},
                       "tags": {"type": "array", "items": {"type": "string"}},
                       "thumbnail_text": {"type": "string"}}}}},
}


def write_metadata(source: dict, excerpt: str, title_hint: str, platforms: list[str],
                   source_id: int | None = None) -> dict[str, dict]:
    brand = config.load_settings().get("brand", "Jimbo4prezz")
    if excerpt and llm.available():
        try:
            prompt = (f"Write post metadata for {brand}'s content for these platforms: {', '.join(platforms)}.\n"
                      f"Source: {source.get('title')} ({source.get('kind')}, {source.get('category')}).\n"
                      f"Working title: {title_hint}\nTranscript of the edit:\n{excerpt[:12000]}\n\n"
                      "Rules: describe only what actually happens in the transcript. YouTube: curiosity title "
                      "under 70 chars plus a 2-4 sentence description. Shorts/TikTok/Reels: short caption with "
                      "3-6 relevant hashtags in tags. X: a punchy one-line post. thumbnail_text: 2-4 words max "
                      "that are true to the content.")
            res = llm.ask_json(llm.SYSTEM_EDITOR, prompt, _META_SCHEMA, max_tokens=8000)
            out = {it["platform"]: it for it in res.get("items", []) if it["platform"] in platforms}
            db.log_operation("metadata", "claude", f"{len(out)} platforms", source_id=source_id)
            if out:
                for p in platforms:
                    out.setdefault(p, _heuristic_meta(title_hint, excerpt, p, brand))
                return out
        except Exception as e:  # noqa: BLE001
            db.log_operation("metadata", "heuristic", f"Claude failed: {e!r}", source_id=source_id)
    return {p: _heuristic_meta(title_hint, excerpt, p, brand) for p in platforms}


def _heuristic_meta(title: str, excerpt: str, platform: str, brand: str) -> dict:
    title = (title or "").strip().strip("…") or "Stream moment"
    words = re.findall(r"[A-Za-z']{4,}", excerpt.lower())
    stop = {"that", "this", "with", "what", "have", "just", "like", "your", "they", "there", "about",
            "were", "then", "them", "would", "know", "yeah", "really", "gonna", "going", "because"}
    freq: dict[str, int] = {}
    for w in words:
        if w not in stop:
            freq[w] = freq.get(w, 0) + 1
    top = [w for w, _ in sorted(freq.items(), key=lambda kv: -kv[1])[:4]]
    tags = [brand.lower(), "twitch", "streamer"] + top
    if platform == "youtube":
        desc = f"{title}\n\nFrom the {brand} stream. {excerpt[:240]}…"
    elif platform == "x":
        desc = title
    else:
        desc = f"{title} #{brand.lower()} " + " ".join(f"#{t}" for t in top[:3])
    short_text = " ".join(title.split()[:3]).upper()
    return {"platform": platform, "title": title[:95], "description": desc, "tags": tags,
            "thumbnail_text": short_text}


# ---------------------------------------------------------------------------
# Packages / plan lifecycle
# ---------------------------------------------------------------------------


def decide_platforms(moment: dict) -> list[str]:
    """Which destinations this moment belongs on (possibly none).

    A platform qualifies when it clears the platform threshold. A moment that
    already passed the selectivity bar also goes to the platforms it fits best
    (within 4 points of its best fit), so strong moments are never orphaned.
    """
    settings = config.load_settings()
    thr = settings.get("min_platform_score", 70)
    ps = moment.get("platform_scores") or {}
    chosen = [p for p in SHORT_PLATFORMS if ps.get(p, 0) >= thr]
    if not chosen and ps and (moment.get("overall_score") or 0) >= settings.get("min_moment_score", 62):
        best = max(ps.values())
        chosen = [p for p in SHORT_PLATFORMS if ps.get(p, 0) >= best - 4]
    return chosen


def _store_plan(source: dict, moment: dict | None, package_id: int | None, platform: str, profile: dict,
                plan: dict, meta: dict, form: str, confidence: float, engine: str, parent_id=None,
                version: int = 1) -> int:
    if meta.get("thumbnail_text"):
        plan["thumbnail_text"] = meta["thumbnail_text"]
    return db.insert(
        "edit_plans", source_id=source["id"], moment_id=(moment or {}).get("id"), package_id=package_id,
        parent_id=parent_id, version=version, platform=platform,
        content_type=(moment or {}).get("content_type") or source.get("category"), form=form,
        profile_id=profile["id"], status="draft", title=meta.get("title"), description=meta.get("description"),
        tags=meta.get("tags", []), plan=plan, original_plan=copy.deepcopy(plan), confidence=confidence,
        engine=engine, created_at=db.now(), updated_at=db.now())


def create_package(moment_id: int, platforms: list[str] | None = None, profile_overrides: dict | None = None,
                   force: bool = False) -> dict:
    moment = db.get("moments", moment_id)
    if not moment:
        raise ValueError("moment not found")
    source, analysis, segments = _ctx(moment["source_id"])
    chosen = platforms or decide_platforms(moment)
    skipped = [p for p in SHORT_PLATFORMS if p not in chosen]
    if not chosen and not force:
        return {"package_id": None, "plans": [], "skipped": skipped,
                "message": "No platform scored above the threshold; nothing created."}
    existing = db.one("SELECT * FROM packages WHERE moment_id=?", (moment_id,))
    pkg_id = existing["id"] if existing else db.insert(
        "packages", source_id=source["id"], moment_id=moment_id,
        name=f"Package — {moment.get('title') or 'moment'}", created_at=db.now())
    metas = write_metadata(source, moment.get("excerpt") or "", moment.get("title") or "", chosen, source["id"])
    plan_ids = []
    for p in chosen:
        prof = profiles.profile_for(p, moment.get("content_type"), (profile_overrides or {}).get(p))
        plan = build_short_plan(source, analysis, segments, moment, p, prof)
        conf = round(((moment.get("platform_scores") or {}).get(p, 60)
                      * ((moment.get("scores") or {}).get("editing_confidence", 70) / 100)), 1)
        pid = _store_plan(source, moment, pkg_id, p, prof, plan, metas.get(p, {}), "short", conf,
                          moment.get("engine") or "heuristic")
        plan_ids.append(pid)
        db.log_operation("edit_plan", "claude" if moment.get("engine") == "claude" else "heuristic",
                         f"{p}: {plan_duration(plan):.1f}s, {plan['structure']}, layout {plan['layout']['mode']}",
                         source_id=source["id"], plan_id=pid)
    db.update("moments", moment_id, status="packaged")
    return {"package_id": pkg_id, "plans": plan_ids, "skipped": skipped}


def create_long_form(source_id: int, ranges: list[tuple[float, float]] | None = None,
                     chapter_ids: list[int] | None = None, title: str = "", cold_open: bool = False,
                     profile_id: int | None = None) -> int:
    source, analysis, segments = _ctx(source_id)
    rngs = list(ranges or [])
    chapters = []
    if chapter_ids:
        chapters = [db.get("moments", c) for c in chapter_ids]
        chapters = [c for c in chapters if c and c["source_id"] == source_id]
        rngs += [(c["start"], c["end"]) for c in chapters]
    if not rngs:
        rngs = [(0.0, source["duration"])]
    rngs.sort()
    prof = profiles.profile_for("youtube", source.get("category"), profile_id)
    co = None
    if cold_open:
        best = db.one("SELECT * FROM moments WHERE source_id=? AND kind='short' AND start>=? AND end<=? "
                      "ORDER BY overall_score DESC LIMIT 1", (source_id, rngs[0][0], rngs[-1][1]))
        if best:
            co = {"start": best["hook_start"], "end": min(best["hook_end"] + 1.0, best["hook_start"] + 8), "force": True}
    plan = build_long_plan(source, analysis, segments, rngs, prof, co)
    excerpt = " ".join(transcribe.text_between(segments, a, min(b, a + 300)) for a, b in rngs)[:12000]
    hint = title or (chapters[0]["title"] if chapters else source.get("title") or "Stream")
    meta = write_metadata(source, excerpt, hint, ["youtube"], source_id).get("youtube", {})
    if title:
        meta["title"] = title
    pid = _store_plan(source, chapters[0] if len(chapters) == 1 else None, None, "youtube", prof, plan, meta,
                      "long", 75.0, analysis.get("engine") or "heuristic")
    db.log_operation("edit_plan", "heuristic", f"youtube long-form: {plan_duration(plan)/60:.1f} min from "
                     f"{sum(b - a for a, b in rngs)/60:.1f} min", source_id=source_id, plan_id=pid)
    return pid


def regenerate(plan_id: int, profile_id: int | None = None) -> int:
    old = db.get("edit_plans", plan_id)
    if not old:
        raise ValueError("plan not found")
    source, analysis, segments = _ctx(old["source_id"])
    prof = db.get("style_profiles", profile_id or old["profile_id"]) or profiles.profile_for(old["platform"])
    if old["form"] == "long":
        rngs = []
        for s in old["plan"]["segments"]:
            if s.get("role") == "hook":
                continue
            if rngs and s["start"] - rngs[-1][1] < 5:
                rngs[-1] = (rngs[-1][0], s["end"])
            else:
                rngs.append((s["start"], s["end"]))
        plan = build_long_plan(source, analysis, segments, rngs, prof)
    else:
        moment = db.get("moments", old["moment_id"]) if old.get("moment_id") else None
        if not moment:
            raise ValueError("original moment is gone; duplicate or edit manually instead")
        plan = build_short_plan(source, analysis, segments, moment, old["platform"], prof,
                                old["plan"]["format"]["aspect"])
    new_version = (old.get("version") or 1) + 1
    pid = _store_plan(source, db.get("moments", old["moment_id"]) if old.get("moment_id") else None,
                      old.get("package_id"), old["platform"], prof, plan,
                      {"title": old["title"], "description": old["description"], "tags": old["tags"]},
                      old["form"], old.get("confidence") or 70, old.get("engine") or "heuristic",
                      parent_id=old["id"], version=new_version)
    db.update("edit_plans", plan_id, status="superseded", updated_at=db.now())
    return pid


def duplicate_for_platform(plan_id: int, platform: str, aspect: str | None = None) -> int:
    old = db.get("edit_plans", plan_id)
    if not old:
        raise ValueError("plan not found")
    source, analysis, segments = _ctx(old["source_id"])
    prof = profiles.profile_for(platform, old.get("content_type"))
    moment = db.get("moments", old["moment_id"]) if old.get("moment_id") else None
    if moment and PLATFORMS[platform]["form"] == "short":
        plan = build_short_plan(source, analysis, segments, moment, platform, prof, aspect)
    else:
        # No moment (manual edit): carry the cut over, re-frame and re-style for the new platform.
        plan = copy.deepcopy(old["plan"])
        params = profiles.effective_params(prof)
        asp, W, H = resolution(platform, aspect)
        plan["format"] = {"aspect": asp, "width": W, "height": H}
        plan["layout"] = choose_layout(source, moment, asp, params, old.get("content_type") or "auto")
        _add_captions_and_zooms(plan, segments, analysis, params, moment, plan["layout"])
        plan["params_used"] = {k: v for k, v in params.items() if not k.startswith("_")}
    meta = write_metadata(source, transcribe.text_between(segments, plan["segments"][0]["start"],
                                                          plan["segments"][-1]["end"]),
                          old.get("title") or "", [platform], source["id"]).get(platform, {})
    form = PLATFORMS[platform]["form"]
    return _store_plan(source, moment, old.get("package_id"), platform, prof, plan, meta, form,
                       old.get("confidence") or 70, old.get("engine") or "heuristic")


def save_plan_edit(plan_id: int, updates: dict) -> dict:
    """Apply manual edits (segments, captions, layout, zooms, metadata)."""
    row = db.get("edit_plans", plan_id)
    if not row:
        raise ValueError("plan not found")
    plan = copy.deepcopy(row["plan"])
    cut_changed = False
    if "segments" in updates:
        plan["segments"] = normalize_segments(updates["segments"])
        cut_changed = True
    for key in ("layout", "zooms", "audio", "removed", "format"):
        if key in updates:
            plan[key] = updates[key]
    if "layout" in updates:
        plan["layout"]["override"] = True
    if "captions" in updates:
        cap = dict(plan.get("captions") or {})
        cap.update(updates["captions"])
        plan["captions"] = cap
    if cut_changed and not (updates.get("captions") or {}).get("lines"):
        plan = refresh_captions(plan, row["source_id"])
    fields = {k: updates[k] for k in ("title", "description", "tags") if k in updates}
    if "platform" in updates and updates["platform"] in PLATFORMS:
        fields["platform"] = updates["platform"]
        asp, W, H = resolution(updates["platform"], plan["format"].get("aspect"))
        plan["format"] = {"aspect": asp, "width": W, "height": H}
    if "profile_id" in updates:
        fields["profile_id"] = updates["profile_id"]
    db.update("edit_plans", plan_id, plan=plan, updated_at=db.now(), **fields)
    db.insert("feedback", plan_id=plan_id, profile_id=row["profile_id"], action="edited",
              delta={"keys": sorted(updates.keys())}, created_at=db.now())
    return db.get("edit_plans", plan_id)


def set_status(plan_id: int, status: str, note: str = "") -> dict:
    row = db.get("edit_plans", plan_id)
    if not row:
        raise ValueError("plan not found")
    db.update("edit_plans", plan_id, status=status, updated_at=db.now())
    delta = {}
    if status in ("approved", "rejected") and row.get("profile_id"):
        delta = profiles.apply_feedback(row["profile_id"], row.get("original_plan") or {}, row["plan"], status)
    db.insert("feedback", plan_id=plan_id, profile_id=row.get("profile_id"), action=status, delta=delta,
              note=note, created_at=db.now())
    return db.get("edit_plans", plan_id)
