"""Editing style profiles.

A profile is a set of editing parameters plus three layers on top:
  base params (built-in defaults for the platform)
  → reference-derived adjustments (from creator / video / clip examples)
  → your written instructions (always win over what was inferred from video)
  → learned adjustments from your approvals and manual edits.

``effective_params(profile)`` merges these into what the planner uses.
"""
from __future__ import annotations

import copy
import re

from . import db

# Every tunable parameter, with type/range so the UI can render a form and
# reference analysis can clamp values.
PARAM_SCHEMA: dict[str, dict] = {
    "target_duration_min": {"type": "number", "min": 5, "max": 3600, "label": "Target min duration (s)"},
    "target_duration_max": {"type": "number", "min": 8, "max": 7200, "label": "Target max duration (s)"},
    "hook_max_seconds": {"type": "number", "min": 0.5, "max": 30, "label": "Seconds allowed before the hook lands"},
    "structure": {"type": "enum", "options": ["auto", "hook_first", "context_first"], "label": "Clip structure"},
    "pacing": {"type": "enum", "options": ["relaxed", "natural", "fast", "aggressive"], "label": "Pacing"},
    "silence_tolerance": {"type": "number", "min": 0.15, "max": 5, "label": "Longest pause kept (s)"},
    "cut_aggressiveness": {"type": "number", "min": 0, "max": 1, "label": "Cut aggressiveness (0 natural – 1 aggressive)"},
    "reaction_preservation": {"type": "number", "min": 0, "max": 1, "label": "Reaction preservation"},
    "context_preroll": {"type": "number", "min": 0, "max": 30, "label": "Context kept before the moment (s)"},
    "end_on_payoff": {"type": "bool", "label": "End right after the payoff"},
    "payoff_tail": {"type": "number", "min": 0, "max": 6, "label": "Reaction tail after payoff (s)"},
    "captions_enabled": {"type": "bool", "label": "Captions"},
    "caption_style": {"type": "enum", "options": ["bold_pop", "clean", "karaoke", "minimal", "boxed"], "label": "Caption style"},
    "caption_words_per_chunk": {"type": "number", "min": 1, "max": 12, "label": "Words per caption"},
    "caption_uppercase": {"type": "bool", "label": "Uppercase captions"},
    "caption_position": {"type": "enum", "options": ["lower", "lower_third", "center", "upper"], "label": "Caption position"},
    "caption_size": {"type": "number", "min": 0.5, "max": 2, "label": "Caption size (relative)"},
    "caption_animation": {"type": "enum", "options": ["none", "pop", "fade"], "label": "Caption animation"},
    "caption_emphasis": {"type": "number", "min": 0, "max": 1, "label": "Emphasis-word frequency"},
    "zoom_per_minute": {"type": "number", "min": 0, "max": 20, "label": "Punch-ins per minute"},
    "zoom_intensity": {"type": "number", "min": 1.0, "max": 1.6, "label": "Punch-in scale"},
    "transitions": {"type": "enum", "options": ["none", "minimal", "frequent"], "label": "Transitions"},
    "sfx_frequency": {"type": "number", "min": 0, "max": 1, "label": "Sound effects frequency"},
    "music": {"type": "enum", "options": ["none", "subtle", "featured"], "label": "Music"},
    "layout": {"type": "enum", "options": ["auto", "crop", "split", "pip", "fit_blur", "full"], "label": "Vertical layout"},
    "loudness_lufs": {"type": "number", "min": -24, "max": -9, "label": "Loudness target (LUFS)"},
    "denoise": {"type": "bool", "label": "Noise reduction"},
    "intro_max_seconds": {"type": "number", "min": 0, "max": 120, "label": "Max intro length (s)"},
    "broll": {"type": "enum", "options": ["none", "rare", "frequent"], "label": "B-roll / memes / graphics"},
}

BASE = {
    "target_duration_min": 20, "target_duration_max": 55, "hook_max_seconds": 3,
    "structure": "auto", "pacing": "natural", "silence_tolerance": 0.7,
    "cut_aggressiveness": 0.5, "reaction_preservation": 0.8, "context_preroll": 6,
    "end_on_payoff": True, "payoff_tail": 1.2, "captions_enabled": True,
    "caption_style": "bold_pop", "caption_words_per_chunk": 3, "caption_uppercase": True,
    "caption_position": "lower_third", "caption_size": 1.0, "caption_animation": "pop",
    "caption_emphasis": 0.3, "zoom_per_minute": 3, "zoom_intensity": 1.12,
    "transitions": "none", "sfx_frequency": 0.0, "music": "none", "layout": "auto",
    "loudness_lufs": -14, "denoise": False, "intro_max_seconds": 0, "broll": "none",
}

BUILTIN_PROFILES = [
    {
        "name": "Jimbo4prezz – YouTube Long Form", "platform": "youtube",
        "content_types": ["gaming", "reaction", "just_chatting", "story", "discussion", "commentary"],
        "description": "Storytelling, personality, natural pacing, strong context, moderate editing. Not a montage.",
        "params": {"target_duration_min": 480, "target_duration_max": 1800, "hook_max_seconds": 20,
                   "structure": "context_first", "pacing": "natural", "silence_tolerance": 1.4,
                   "cut_aggressiveness": 0.3, "reaction_preservation": 0.95, "context_preroll": 20,
                   "end_on_payoff": False, "payoff_tail": 3, "captions_enabled": False,
                   "caption_style": "clean", "caption_words_per_chunk": 7, "caption_uppercase": False,
                   "caption_position": "lower", "caption_animation": "none", "caption_emphasis": 0.0,
                   "zoom_per_minute": 0.6, "zoom_intensity": 1.08, "layout": "full",
                   "intro_max_seconds": 30, "loudness_lufs": -14},
    },
    {
        "name": "Jimbo4prezz – TikTok", "platform": "tiktok",
        "content_types": ["gaming", "reaction", "story", "just_chatting", "highlight"],
        "description": "Strong hook, fast pacing, mobile captions, strategic zooms, high retention.",
        "params": {"target_duration_min": 15, "target_duration_max": 48, "hook_max_seconds": 1.5,
                   "structure": "auto", "pacing": "fast", "silence_tolerance": 0.45,
                   "cut_aggressiveness": 0.7, "reaction_preservation": 0.8, "context_preroll": 4,
                   "caption_style": "bold_pop", "caption_words_per_chunk": 2, "caption_uppercase": True,
                   "caption_position": "lower_third", "caption_size": 1.1, "caption_emphasis": 0.45,
                   "zoom_per_minute": 5, "zoom_intensity": 1.15, "layout": "auto", "loudness_lufs": -13},
    },
    {
        "name": "Jimbo4prezz – YouTube Shorts", "platform": "youtube_shorts",
        "content_types": ["gaming", "reaction", "story", "just_chatting", "highlight"],
        "description": "Hook, context, payoff. Strong readable captions, clean vertical framing.",
        "params": {"target_duration_min": 20, "target_duration_max": 58, "hook_max_seconds": 2.5,
                   "structure": "auto", "pacing": "natural", "silence_tolerance": 0.6,
                   "cut_aggressiveness": 0.55, "reaction_preservation": 0.85, "context_preroll": 6,
                   "caption_style": "bold_pop", "caption_words_per_chunk": 3, "caption_uppercase": True,
                   "zoom_per_minute": 3.5, "zoom_intensity": 1.12, "layout": "auto", "loudness_lufs": -14},
    },
    {
        "name": "Jimbo4prezz – Instagram Reels", "platform": "instagram_reels",
        "content_types": ["gaming", "reaction", "story", "just_chatting", "irl"],
        "description": "Clean presentation, strong opening, personality, mobile-first framing.",
        "params": {"target_duration_min": 18, "target_duration_max": 52, "hook_max_seconds": 2.5,
                   "structure": "auto", "pacing": "natural", "silence_tolerance": 0.65,
                   "cut_aggressiveness": 0.5, "reaction_preservation": 0.85, "context_preroll": 6,
                   "caption_style": "clean", "caption_words_per_chunk": 4, "caption_uppercase": False,
                   "caption_animation": "fade", "caption_emphasis": 0.2,
                   "zoom_per_minute": 2.5, "zoom_intensity": 1.1, "layout": "auto", "loudness_lufs": -14},
    },
    {
        "name": "Jimbo4prezz – X Clips", "platform": "x",
        "content_types": ["reaction", "debate", "commentary", "gaming", "just_chatting"],
        "description": "Strong statement, funny moment, reaction, conversation. Minimal unnecessary editing.",
        "params": {"target_duration_min": 10, "target_duration_max": 40, "hook_max_seconds": 2,
                   "structure": "context_first", "pacing": "natural", "silence_tolerance": 0.8,
                   "cut_aggressiveness": 0.35, "reaction_preservation": 0.9, "context_preroll": 3,
                   "caption_style": "minimal", "caption_words_per_chunk": 6, "caption_uppercase": False,
                   "caption_position": "lower", "caption_animation": "none", "caption_emphasis": 0.0,
                   "zoom_per_minute": 0.5, "zoom_intensity": 1.06, "layout": "full", "loudness_lufs": -14},
    },
]


def clamp_param(key: str, value):
    spec = PARAM_SCHEMA.get(key)
    if spec is None:
        return None
    t = spec["type"]
    try:
        if t == "number":
            v = float(value)
            return max(spec["min"], min(spec["max"], v))
        if t == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if t == "enum":
            return value if value in spec["options"] else None
    except (TypeError, ValueError):
        return None
    return None


def clean_params(params: dict) -> dict:
    out = {}
    for k, v in (params or {}).items():
        cv = clamp_param(k, v)
        if cv is not None:
            out[k] = cv
    return out


def seed_builtin_profiles() -> None:
    for p in BUILTIN_PROFILES:
        if db.get("style_profiles", p["name"], key="name"):
            continue
        params = dict(BASE)
        params.update(p["params"])
        db.insert("style_profiles", name=p["name"], platform=p["platform"],
                  content_types=p["content_types"], description=p["description"],
                  params=params, learned={}, builtin=1, created_at=db.now(), updated_at=db.now())


def list_profiles() -> list[dict]:
    rows = db.query("SELECT * FROM style_profiles ORDER BY builtin DESC, id")
    for r in rows:
        r["effective"] = effective_params(r)
    return rows


def _reference_layers(profile_id: int) -> tuple[dict, dict, list[dict]]:
    """Merge adjustments from every reference linked to this profile.

    Returns (inferred_from_media, from_written_instructions, contributors).
    Numeric values are averaged across references; written instructions are
    kept separate so they always override media-derived guesses.
    """
    refs = db.query("SELECT * FROM refs WHERE status != 'disabled' ORDER BY id")
    inferred: dict[str, list] = {}
    written: dict[str, list] = {}
    contributors = []
    for r in refs:
        if profile_id not in (r.get("profile_ids") or []):
            continue
        ex = r.get("extracted") or {}
        used = False
        for bucket, src in ((inferred, ex.get("media_params") or {}), (written, ex.get("note_params") or {})):
            for k, v in src.items():
                bucket.setdefault(k, []).append(v)
                used = True
        if used:
            contributors.append({"id": r["id"], "name": r.get("name"), "level": r["level"]})

    def fold(bucket):
        out = {}
        for k, vals in bucket.items():
            spec = PARAM_SCHEMA.get(k)
            if not spec:
                continue
            if spec["type"] == "number":
                out[k] = clamp_param(k, sum(float(v) for v in vals) / len(vals))
            else:
                out[k] = vals[-1]  # most recent reference wins for enums/bools
        return out

    return fold(inferred), fold(written), contributors


def effective_params(profile: dict) -> dict:
    params = dict(BASE)
    params.update(clean_params(profile.get("params") or {}))
    inferred, written, contributors = _reference_layers(profile["id"]) if profile.get("id") else ({}, {}, [])
    params.update(inferred)
    params.update(written)
    learned = profile.get("learned") or {}
    for k, v in (learned.get("adjust") or {}).items():
        spec = PARAM_SCHEMA.get(k)
        if spec and spec["type"] == "number" and k in params:
            params[k] = clamp_param(k, params[k] + v)
    params["_layers"] = {"references": contributors, "inferred": inferred, "written": written,
                         "learned": learned.get("adjust") or {}}
    return params


def profile_for(platform: str, content_type: str | None = None, profile_id: int | None = None) -> dict:
    if profile_id:
        p = db.get("style_profiles", profile_id)
        if p:
            return p
    rows = db.query("SELECT * FROM style_profiles WHERE platform=? ORDER BY builtin ASC, id DESC", (platform,))
    if content_type:
        for r in rows:
            if content_type in (r.get("content_types") or []) and not r["builtin"]:
                return r
    # A user-created profile for the platform beats the built-in one.
    for r in rows:
        if not r["builtin"]:
            return r
    if rows:
        return rows[-1]
    seed_builtin_profiles()
    return profile_for(platform, content_type)


# ---------------------------------------------------------------------------
# Written-instruction → parameter rules (used without Claude, and as a sanity
# layer under Claude's interpretation). Each rule: regex, param changes.
# ---------------------------------------------------------------------------
NOTE_RULES: list[tuple[str, dict, str]] = [
    (r"(quick|fast|immediately|straight|right away).{0,40}(funny|moment|point|action|hook|good part)",
     {"hook_max_seconds": 1.5, "structure": "hook_first"}, "Gets to the moment quickly"),
    (r"(gets? to the point|no (long )?intro|skip(s)? the intro)", {"hook_max_seconds": 1.5, "intro_max_seconds": 0}, "No slow intro"),
    (r"(keeps?|preserv\w*|leaves?) .{0,30}reactions?", {"reaction_preservation": 0.95, "payoff_tail": 2.0}, "Keeps reactions in"),
    (r"(remov\w*|cuts?( out)?) .{0,30}(unnecessary|boring|dead|filler) .{0,20}(conversation|talk|air|parts?|silence)",
     {"silence_tolerance": 0.45, "cut_aggressiveness": 0.65}, "Removes unnecessary conversation"),
    (r"(natural|not over.?edit|overedit|over-edit|not too edited|doesn.?t feel edited)",
     {"cut_aggressiveness": 0.35, "transitions": "none", "sfx_frequency": 0.0, "pacing": "natural"},
     "Natural feel, not over-edited"),
    (r"zooms?.{0,40}(not|don.?t|without).{0,20}(overus|too many|too much|every)",
     {"zoom_per_minute": 2.0, "zoom_intensity": 1.1}, "Zooms, but sparingly"),
    (r"(like|love).{0,20}zooms?(?!.{0,40}(not|don.?t))", {"zoom_per_minute": 4.0, "zoom_intensity": 1.14}, "Likes zooms"),
    (r"(no|hate|don.?t (like|want)) zooms?", {"zoom_per_minute": 0.0}, "No zooms"),
    (r"(like|love).{0,20}captions?", {"captions_enabled": True}, "Likes the captions"),
    (r"(no|hate|don.?t (like|want)) captions?", {"captions_enabled": False}, "No captions"),
    (r"(big|large|bold) (captions?|text)", {"caption_size": 1.25, "caption_style": "bold_pop"}, "Big bold captions"),
    (r"(small|subtle|clean) (captions?|subtitles|text)", {"caption_size": 0.85, "caption_style": "clean", "caption_uppercase": False}, "Clean captions"),
    (r"(one|1|single) word (at a time|captions?)", {"caption_words_per_chunk": 1}, "One word at a time"),
    (r"(word.by.word|highlight\w* (each|the current) word|karaoke)", {"caption_style": "karaoke"}, "Word-by-word highlight"),
    (r"(fast|quick|snappy|tight) (pac|cut|edit)", {"pacing": "fast", "cut_aggressiveness": 0.7, "silence_tolerance": 0.4}, "Fast pacing"),
    (r"(slow|relaxed|breath|let it breathe)", {"pacing": "relaxed", "silence_tolerance": 1.2, "cut_aggressiveness": 0.3}, "Relaxed pacing"),
    (r"(ends?|stop\w*) (right |immediately )?(after|on) the (payoff|punchline|joke)", {"end_on_payoff": True, "payoff_tail": 0.8}, "Ends on the payoff"),
    (r"(context|setup|set up) (first|before)", {"structure": "context_first", "context_preroll": 8}, "Context before payoff"),
    (r"(hook|best part|payoff) (first|at the start|up front)", {"structure": "hook_first"}, "Hook first"),
    (r"(split.?screen|facecam (on|at) (the )?(top|bottom))", {"layout": "split"}, "Split-screen layout"),
    (r"(picture.in.picture|pip|facecam (in|on) (the )?corner)", {"layout": "pip"}, "Picture-in-picture layout"),
    (r"(sound effects|sfx)", {"sfx_frequency": 0.4}, "Uses sound effects"),
    (r"(no|without|hate) (sound effects|sfx)", {"sfx_frequency": 0.0}, "No sound effects"),
    (r"(no|without) (music|background music)", {"music": "none"}, "No music"),
    (r"(no|without|hate) transitions?", {"transitions": "none"}, "No transitions"),
    (r"(memes?|b.?roll|graphics|screenshots?)", {"broll": "rare"}, "Occasional B-roll / memes"),
    (r"(lower|bottom) (third|of the screen).{0,20}captions?|captions?.{0,20}(lower|bottom)", {"caption_position": "lower_third"}, "Captions low"),
    (r"captions?.{0,20}(middle|center|centre)", {"caption_position": "center"}, "Captions centered"),
    (r"(all caps|uppercase)", {"caption_uppercase": True}, "Uppercase captions"),
]


def params_from_notes(notes: str) -> tuple[dict, list[str]]:
    text = (notes or "").lower()
    params: dict = {}
    matched: list[str] = []
    for pattern, changes, label in NOTE_RULES:
        if re.search(pattern, text):
            # Negative rules listed later override earlier positive ones.
            params.update(changes)
            matched.append(label)
    return clean_params(params), matched


def apply_feedback(profile_id: int, original: dict, final: dict, action: str) -> dict:
    """Nudge a profile's learned adjustments from what you changed before approving.

    Small exponential steps, so one unusual clip doesn't rewrite the style.
    """
    profile = db.get("style_profiles", profile_id)
    if not profile:
        return {}
    learned = copy.deepcopy(profile.get("learned") or {})
    adjust = learned.setdefault("adjust", {})
    counts = learned.setdefault("counts", {"approved": 0, "rejected": 0, "edited": 0})
    counts[action] = counts.get(action, 0) + 1
    delta = {}
    rate = 0.25

    def dur(plan):
        return sum(s["end"] - s["start"] for s in plan.get("segments", []) if s.get("enabled", True))

    if action == "approved" and original and final:
        od, fd = dur(original), dur(final)
        if od > 0 and abs(fd - od) / od > 0.08:
            delta["duration_ratio"] = round(fd / od, 3)
            change = (fd - od) * rate
            adjust["target_duration_max"] = round(adjust.get("target_duration_max", 0) + change, 2)
            adjust["target_duration_min"] = round(adjust.get("target_duration_min", 0) + change * 0.5, 2)
        o_first = min((s["start"] for s in original.get("segments", []) if s.get("enabled", True)), default=None)
        f_first = min((s["start"] for s in final.get("segments", []) if s.get("enabled", True)), default=None)
        if o_first is not None and f_first is not None and abs(f_first - o_first) > 0.5:
            delta["start_shift"] = round(f_first - o_first, 2)
            adjust["context_preroll"] = round(adjust.get("context_preroll", 0) - (f_first - o_first) * rate, 2)
        oz, fz = len(original.get("zooms", [])), len(final.get("zooms", []))
        if oz != fz and od > 0:
            per_min_change = (fz - oz) / max(fd, 1) * 60
            delta["zoom_change_per_min"] = round(per_min_change, 2)
            adjust["zoom_per_minute"] = round(adjust.get("zoom_per_minute", 0) + per_min_change * rate, 2)
        oc = original.get("captions", {}).get("enabled")
        fc = final.get("captions", {}).get("enabled")
        if oc != fc:
            delta["captions_enabled"] = fc
    learned["adjust"] = {k: v for k, v in adjust.items() if abs(v) > 1e-3}
    learned["counts"] = counts
    db.update("style_profiles", profile_id, learned=learned, updated_at=db.now())
    return delta
