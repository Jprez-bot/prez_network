"""Understand a recording and find its strongest moments.

Signals (always measured, no AI needed):
  * loudness curve, relative to a rolling local baseline (so a loud game
    doesn't make everything look exciting)
  * silences, sudden rises, scene changes
  * transcript cues: laughter, exclamations, questions, speech density

Understanding:
  * Claude reads the timestamped transcript plus the measured signal peaks
    and proposes moments, structure (hook-first vs context-first), scores,
    platform fit and long-form chapters.
  * Without Claude, a heuristic engine builds the same structures from the
    signals alone. The engine used is stored on every moment and logged.

Selectivity: moments below ``min_moment_score`` are dropped and there is a
per-hour ceiling, so a six-hour stream with six good moments yields six.
"""
from __future__ import annotations

import math
import re

import numpy as np

from . import config, db, llm, media, transcribe
from .platforms import SHORT_PLATFORMS

SCORE_KEYS = ["hook_strength", "context_completeness", "emotional_intensity", "humor",
              "surprise", "story_quality", "reaction_strength", "audio_quality",
              "visual_quality", "payoff_strength", "standalone_quality", "editing_confidence"]

LAUGH = re.compile(r"\b(ha(ha)+|lol|lmao|lmfao|rofl|dying|i'?m crying)\b|\[laugh|\(laugh", re.I)
EXCLAIM = re.compile(r"\b(no way|what the|oh my god|omg|bro+|dude|let'?s go+|yo+|wait|are you serious|"
                     r"holy|insane|crazy|nah+|stop|why would|how did|what\?|ain'?t no way)\b", re.I)
STRONG = re.compile(r"\b(fuck\w*|shit\w*|damn|hell)\b|!", re.I)
OPINION = re.compile(r"\b(i think|honestly|the truth is|hot take|nobody|everybody|worst|best|overrated|underrated)\b", re.I)

LONG_SOURCE_SECONDS = 180  # anything longer gets long-form chapter detection


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    if len(x) == 0 or n <= 1:
        return x
    k = np.ones(n) / n
    return np.convolve(np.pad(x, (n // 2, n - 1 - n // 2), mode="edge"), k, mode="valid")


def _rolling_median(x: np.ndarray, n: int) -> np.ndarray:
    if len(x) == 0:
        return x
    # Block-wise median then interpolate (fast enough for 6 h at 0.5 s hops).
    block = max(1, n // 4)
    centers, meds = [], []
    for i in range(0, len(x), block):
        a, b = max(0, i - n // 2), min(len(x), i + n // 2)
        centers.append(i)
        meds.append(float(np.median(x[a:b])))
    return np.interp(np.arange(len(x)), centers, meds)


def build_signals(audio: dict, scenes: list[float], segments: list[dict], duration: float) -> dict:
    hop = audio.get("hop", media.ENERGY_HOP) or media.ENERGY_HOP
    db_curve = np.array(audio.get("db") or [], dtype=float)
    n = len(db_curve)
    if n == 0:
        n = max(1, int(math.ceil(duration / hop)))
        db_curve = np.full(n, -60.0)
    baseline = _rolling_median(db_curve, int(120 / hop))
    spread = max(3.0, float(np.std(db_curve)))
    loud = np.clip((db_curve - baseline) / spread, -2, 4)

    rise = np.zeros(n)
    sm = _smooth(db_curve, 3)
    lag = int(2 / hop)
    if n > lag:
        rise[lag:] = np.clip((sm[lag:] - sm[:-lag]) / spread, 0, 4)

    cue = np.zeros(n)
    speech = np.zeros(n)
    for w in transcribe.all_words(segments):
        i = int(w["s"] / hop)
        if 0 <= i < n:
            speech[i] += 1
            token = w["w"]
            if LAUGH.search(token):
                cue[i] += 2.0
            if STRONG.search(token):
                cue[i] += 0.8
    for s in transcribe.sentences(segments):
        i = int(s["start"] / hop)
        if 0 <= i < n:
            if EXCLAIM.search(s["text"]):
                cue[i] += 1.5
            if LAUGH.search(s["text"]):
                cue[i] += 1.0
            if s["text"].count("!") >= 1:
                cue[i] += 0.5

    scene = np.zeros(n)
    for t in scenes or []:
        i = int(t / hop)
        if 0 <= i < n:
            scene[i] += 1

    excitement = (1.0 * _smooth(loud, 5) + 0.6 * _smooth(rise, 3)
                  + 0.7 * _smooth(cue, 9) * 3 + 0.25 * _smooth(scene, 9) * 3)
    return {
        "hop": hop, "loud": loud, "rise": rise, "cue": cue, "speech": speech,
        "scene": scene, "excitement": _smooth(excitement, 5), "db": db_curve,
        "baseline": baseline, "has_transcript": bool(segments),
    }


def find_peaks(sig: dict, min_gap_s: float = 40, limit: int = 200) -> list[int]:
    ex = sig["excitement"]
    if len(ex) < 3:
        return [int(np.argmax(ex))] if len(ex) else []
    thr = float(np.percentile(ex, 85))
    hop = sig["hop"]
    gap = int(min_gap_s / hop)
    order = np.argsort(ex)[::-1]
    chosen: list[int] = []
    for i in order:
        if ex[i] < thr and chosen:
            break
        if all(abs(int(i) - c) > gap for c in chosen):
            chosen.append(int(i))
        if len(chosen) >= limit:
            break
    return sorted(chosen)


def _snap_start(t: float, sents: list[dict]) -> float:
    """Snap to the start of the sentence containing t; if t is in a pause, to the next sentence."""
    return snap_start(t, sents)


def snap_start(t: float, sents: list[dict]) -> float:
    for s in sents:
        if s["start"] - 0.05 <= t <= s["end"]:
            return s["start"]
        if s["start"] > t:
            return max(t, s["start"] - 0.3)
    return t


def _snap_end(t: float, sents: list[dict]) -> float:
    cands = [s["end"] for s in sents if s["end"] >= t - 0.05]
    return cands[0] if cands else t


def window_for_peak(i: int, sig: dict, sents: list[dict], duration: float,
                    silences: list[list[float]]) -> dict:
    hop = sig["hop"]
    ex = sig["excitement"]
    peak_t = i * hop
    base = float(np.median(ex))
    level = base + 0.4 * (ex[i] - base)
    j = i
    while j + 1 < len(ex) and (j - i) * hop < 20 and ex[j + 1] > level:
        j += 1
    payoff_end = min(duration, j * hop + 0.5)
    if sents:
        payoff_end = min(duration, _snap_end(payoff_end, sents))
    # Hook: the sentence (or ~3 s) where the peak lands.
    hook_start = max(0.0, peak_t - 1.0)
    hook_end = min(duration, peak_t + 2.5)
    if sents:
        hs = [s for s in sents if s["start"] <= peak_t + 0.5 and s["end"] >= peak_t - 1.5]
        if hs:
            hook_start, hook_end = hs[0]["start"], min(hs[-1]["end"], hs[0]["start"] + 6)
    # Context: walk back up to 30 s, stopping at a long silence (topic break).
    start = max(0.0, peak_t - 30)
    for a, b in silences:
        if b <= peak_t - 4 and b > start and (b - a) >= 2.5:
            start = b
    if sents:
        start = _snap_start(start, sents)
    end = min(duration, payoff_end + 1.5)
    return {"start": round(start, 2), "end": round(end, 2), "hook_start": round(hook_start, 2),
            "hook_end": round(hook_end, 2), "payoff_end": round(payoff_end, 2), "peak": round(peak_t, 2)}


def _range(sig: dict, key: str, a: float, b: float) -> np.ndarray:
    hop = sig["hop"]
    arr = sig[key]
    lo, hi = int(a / hop), max(int(a / hop) + 1, int(b / hop))
    return arr[max(0, lo):min(len(arr), hi)]


def score_window(w: dict, sig: dict, segments: list[dict], audio: dict, face: dict | None = None) -> dict:
    a, b = w["start"], w["end"]
    peak = w.get("peak", (a + b) / 2)
    loud = _range(sig, "loud", a, b)
    cue = _range(sig, "cue", a, b)
    rise = _range(sig, "rise", a, b)
    speech = _range(sig, "speech", a, b)
    text = transcribe.text_between(segments, a, b)
    length = max(1.0, b - a)
    words_per_s = (speech.sum() / length) if len(speech) else 0
    laughs = len(LAUGH.findall(text))
    excl = len(EXCLAIM.findall(text))
    opinions = len(OPINION.findall(text))
    lmax = float(loud.max()) if len(loud) else 0
    clamp = lambda v: int(max(0, min(100, round(v))))  # noqa: E731

    emotional = clamp(40 + lmax * 18 + excl * 3)
    reaction = clamp(35 + lmax * 15 + laughs * 8 + float(cue.max() if len(cue) else 0) * 6)
    humor = clamp(30 + laughs * 14 + (8 if excl else 0)) if sig["has_transcript"] else clamp(35 + lmax * 10)
    surprise = clamp(30 + float(rise.max() if len(rise) else 0) * 18 + excl * 4)
    early = _range(sig, "excitement", a, min(b, a + 3))
    hook = clamp(45 + (float(early.max()) if len(early) else 0) * 12 + (10 if peak - a < 4 else 0)
                 + (12 if w.get("hook_end", 0) > w.get("hook_start", 0) else 0))
    context = clamp(55 + (15 if sig["has_transcript"] and words_per_s > 1 else 0)
                    + (10 if peak - a >= 4 else -10))
    story = clamp(35 + min(30, words_per_s * 10) + opinions * 5) if sig["has_transcript"] else 45
    after = _range(sig, "loud", w.get("payoff_end", b) - 2, w.get("payoff_end", b))
    payoff = clamp(45 + (float(after.max()) if len(after) else 0) * 14 + laughs * 4)
    stats = audio.get("stats") or {}
    snr = (stats.get("p95_db", -20) - stats.get("floor_db", -60))
    peaks = _range({"hop": sig["hop"], "peak": np.array(audio.get("peak") or [0.0])}, "peak", a, b)
    clipping = float(np.mean(peaks >= 0.99)) if len(peaks) else 0
    audio_q = clamp(50 + min(35, snr) - clipping * 200)
    visual_q = 70
    if face:
        visual_q = clamp(60 + face.get("found_ratio", 0) * 30)
    standalone = clamp((context + payoff + hook) / 3 + (5 if 12 <= length <= 75 else -5))
    confidence = clamp(55 + (20 if sig["has_transcript"] else 0) + (10 if face and face.get("box") else 0))
    scores = {
        "hook_strength": hook, "context_completeness": context, "emotional_intensity": emotional,
        "humor": humor, "surprise": surprise, "story_quality": story, "reaction_strength": reaction,
        "audio_quality": audio_q, "visual_quality": visual_q, "payoff_strength": payoff,
        "standalone_quality": standalone, "editing_confidence": confidence,
    }
    return scores


WEIGHTS = {"hook_strength": 1.4, "context_completeness": 0.8, "emotional_intensity": 1.1,
           "humor": 1.1, "surprise": 0.9, "story_quality": 0.8, "reaction_strength": 1.3,
           "audio_quality": 0.5, "visual_quality": 0.4, "payoff_strength": 1.3,
           "standalone_quality": 1.2, "editing_confidence": 0.4}

PLATFORM_WEIGHTS = {
    "tiktok": {"hook_strength": 1.8, "humor": 1.3, "surprise": 1.2, "reaction_strength": 1.3, "payoff_strength": 1.1},
    "youtube_shorts": {"hook_strength": 1.3, "context_completeness": 1.2, "payoff_strength": 1.4, "story_quality": 1.1},
    "instagram_reels": {"hook_strength": 1.2, "visual_quality": 1.4, "standalone_quality": 1.3, "humor": 1.1},
    "x": {"story_quality": 1.4, "emotional_intensity": 1.2, "reaction_strength": 1.2, "humor": 1.1, "hook_strength": 1.0},
}


def overall(scores: dict) -> float:
    total = sum(WEIGHTS.values())
    return round(sum(scores.get(k, 50) * wt for k, wt in WEIGHTS.items()) / total, 1)


def platform_scores(scores: dict, length: float, essential: float | None = None) -> dict:
    """Per-platform fit. ``essential`` = shortest span that still carries context + payoff."""
    from .platforms import PLATFORMS
    essential = essential or length
    out = {}
    for p in SHORT_PLATFORMS:
        weights = dict(WEIGHTS)
        weights.update(PLATFORM_WEIGHTS.get(p, {}))
        base = sum(scores.get(k, 50) * wt for k, wt in weights.items()) / sum(weights.values())
        lo, hi = PLATFORMS[p]["duration"]
        # Clips can be trimmed, so only penalise when the essential part
        # (context + payoff) clearly cannot fit.
        if length > hi * 1.6:
            base -= min(25, (length - hi * 1.6) / hi * 25)
        if essential > hi:
            base -= min(20, (essential - hi) / hi * 30)
        elif essential < lo * 0.5:
            base -= 4
        if p == "x":
            base += 3 if essential <= 35 else -4 if essential > 60 else 0
            base += (scores.get("story_quality", 50) - 50) * 0.1
        if p == "tiktok":
            base += (scores.get("hook_strength", 50) - 60) * 0.15
        if p == "instagram_reels":
            base += (scores.get("visual_quality", 50) - 60) * 0.1
        if p == "youtube_shorts":
            base += (scores.get("context_completeness", 50) - 60) * 0.1
        out[p] = int(max(0, min(100, round(base))))
    return out


def explain(scores: dict) -> str:
    good = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:3]
    weak = [k for k, v in scores.items() if v < 50]
    names = lambda ks: ", ".join(k.replace("_", " ") for k in ks)  # noqa: E731
    msg = f"Strongest: {names([k for k, _ in good])}."
    if weak:
        msg += f" Weaker: {names(weak[:3])}."
    return msg


# ---------------------------------------------------------------------------
# Claude understanding
# ---------------------------------------------------------------------------

def _fmt(t: float) -> str:
    t = max(0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


_SCORE_SCHEMA = {"type": "object", "additionalProperties": False, "required": SCORE_KEYS,
                 "properties": {k: {"type": "number"} for k in SCORE_KEYS}}
_PLAT_SCHEMA = {"type": "object", "additionalProperties": False, "required": SHORT_PLATFORMS,
                "properties": {p: {"type": "number"} for p in SHORT_PLATFORMS}}
MOMENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "content_type", "moments", "chapters"],
    "properties": {
        "summary": {"type": "string"},
        "content_type": {"type": "string"},
        "moments": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["start", "end", "hook_start", "hook_end", "payoff_end", "structure", "title",
                         "why", "content_type", "scores", "platforms", "emphasis_words"],
            "properties": {
                "start": {"type": "number"}, "end": {"type": "number"},
                "hook_start": {"type": "number"}, "hook_end": {"type": "number"},
                "payoff_end": {"type": "number"},
                "structure": {"type": "string", "enum": ["hook_first", "context_first"]},
                "title": {"type": "string"}, "why": {"type": "string"},
                "content_type": {"type": "string"},
                "scores": _SCORE_SCHEMA, "platforms": _PLAT_SCHEMA,
                "emphasis_words": {"type": "array", "items": {"type": "string"}},
            }}},
        "chapters": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["start", "end", "title", "summary", "content_type"],
            "properties": {"start": {"type": "number"}, "end": {"type": "number"},
                           "title": {"type": "string"}, "summary": {"type": "string"},
                           "content_type": {"type": "string"}}}},
    },
}


def llm_understand(source: dict, segments: list[dict], peaks: list[dict], max_moments: int) -> dict:
    sents = transcribe.sentences(segments)
    lines = [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in sents]
    peak_lines = [f"- {p['peak']:.1f}s (signal {p['strength']:.2f}; window {p['start']:.1f}-{p['end']:.1f})"
                  for p in peaks[:60]]
    duration = source.get("duration") or 0
    is_clip = duration <= LONG_SOURCE_SECONDS
    prompt = f"""Source: "{source.get('title')}" ({source.get('kind')}, category hint: {source.get('category')}), duration {duration:.1f}s ({_fmt(duration)}).

Measured signal peaks (loudness spikes relative to local baseline, laughter/exclamation cues, scene changes):
{chr(10).join(peak_lines) or '(none)'}

Timestamped transcript (seconds):
{chr(10).join(lines)}

Tasks:
1. Find the strongest standalone short-form moments. Return at most {max_moments}; return fewer if fewer are genuinely strong. {'This is a single clip: return exactly one moment describing the best possible trim of it.' if is_clip else ''}
   For each: start/end (the full usable span incl. needed context), hook_start/hook_end (the single most compelling 1-6 s line or reaction), payoff_end (when the punchline/reaction resolves).
   structure = "context_first" when the setup must be heard first (Context → Build-up → Payoff); "hook_first" when opening on the hook then giving context works better (Hook → Context → Payoff).
   Score each criterion 0-100 honestly, and score platform fit 0-100 for {', '.join(SHORT_PLATFORMS)} (TikTok: fast hook; Shorts: hook/context/payoff; Reels: clean, personality; X: conversational, strong statement, short).
   emphasis_words: 0-8 words from the transcript worth highlighting in captions.
   Titles must describe what actually happens; never invent events.
2. {'Return an empty chapters list.' if is_clip else 'Split the recording into long-form chapters (coherent segments 3-30 min: a game round, a story, a reaction to one video, a discussion topic) suitable for YouTube long-form edits.'}
All timestamps are seconds on the source timeline."""
    return llm.ask_json(llm.SYSTEM_EDITOR, prompt, MOMENT_SCHEMA)


# ---------------------------------------------------------------------------
# Long-form chapters (heuristic)
# ---------------------------------------------------------------------------

def heuristic_chapters(duration: float, silences: list[list[float]], scenes: list[float],
                       segments: list[dict]) -> list[dict]:
    if duration <= LONG_SOURCE_SECONDS:
        return []
    breaks = sorted({round(b, 1) for a, b in silences if b - a >= 6} | set())
    # Scene-cluster breaks: big visual changes after a quiet period.
    chapters = []
    start = 0.0
    target_min, target_max = 300.0, 1500.0
    candidates = sorted(breaks + [t for t in scenes if t > 0])
    for t in candidates + [duration]:
        length = t - start
        if length >= target_min or t == duration:
            if length > target_max:
                n = math.ceil(length / target_max)
                step = length / n
                for k in range(n):
                    chapters.append([start + k * step, start + (k + 1) * step])
            elif length > 30 or not chapters:
                chapters.append([start, t])
            else:
                chapters[-1][1] = t
            start = t
    out = []
    for idx, (a, b) in enumerate(chapters, 1):
        text = transcribe.text_between(segments, a, min(b, a + 60))
        title = (text[:60] + "…") if len(text) > 60 else (text or f"Segment {idx}")
        out.append({"start": round(a, 2), "end": round(b, 2), "title": f"Part {idx}: {title}",
                    "summary": "", "content_type": "auto"})
    return out


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def analyze_source(source_id: int, progress=lambda p, m: None) -> dict:
    source = db.get("sources", source_id)
    if not source:
        raise ValueError("source not found")
    settings = config.load_settings()
    path = source["path"]
    duration = source.get("duration") or media.probe(path)["duration"]
    db.update("sources", source_id, status="analyzing")

    # 1. Transcript
    progress(0.02, "Transcript")
    tr = db.get("transcripts", source_id, key="source_id")
    segments = (tr or {}).get("segments") or []
    if not segments:
        try:
            engine, lang, segments = transcribe.transcribe(
                path, progress=lambda p, m: progress(0.02 + p * 0.45, m))
            db.upsert("transcripts", "source_id", source_id=source_id, engine=engine,
                      language=lang, segments=segments, created_at=db.now())
            db.log_operation("transcription", "whisper", f"{engine}: {len(segments)} segments", source_id=source_id)
        except transcribe.TranscriptionUnavailable as e:
            db.log_operation("transcription", "skipped", str(e), source_id=source_id)
        except Exception as e:  # noqa: BLE001 - keep analysing from signals
            db.log_operation("transcription", "failed", repr(e), source_id=source_id)

    # 2. Audio + visual signals
    progress(0.5, "Audio analysis")
    audio = media.audio_energy(path) if source.get("has_audio") else {"hop": 0.5, "db": [], "peak": [], "silences": [], "stats": {}}
    db.log_operation("audio_analysis", "ffmpeg", f"{len(audio['db'])} loudness samples", source_id=source_id)
    progress(0.62, "Scene detection")
    scenes = media.scene_changes(path)
    db.log_operation("scene_detection", "ffmpeg", f"{len(scenes)} scene changes", source_id=source_id)

    sig = build_signals(audio, scenes, segments, duration)
    sents = transcribe.sentences(segments)
    is_clip = duration <= LONG_SOURCE_SECONDS
    hours = max(duration / 3600, 1 / 60)
    max_moments = 1 if is_clip else max(1, int(math.ceil(hours * settings["max_moments_per_hour"])))

    peak_idx = find_peaks(sig, min_gap_s=20 if is_clip else 40)
    windows = []
    for i in peak_idx:
        w = window_for_peak(i, sig, sents, duration, audio.get("silences", []))
        w["strength"] = float(sig["excitement"][i])
        windows.append(w)
    windows.sort(key=lambda w: w["strength"], reverse=True)

    # 3. Understanding
    progress(0.7, "Understanding content")
    engine = "heuristic"
    summary = ""
    chapters: list[dict] = []
    proposals: list[dict] = []
    category = source.get("category") or "auto"
    if segments and llm.available():
        try:
            result = llm_understand(source, segments, windows, max_moments)
            engine = "claude"
            summary = result.get("summary", "")
            if category == "auto":
                category = result.get("content_type") or category
            chapters = [c for c in result.get("chapters", []) if 0 <= c["start"] < c["end"] <= duration + 1]
            for m in result.get("moments", []):
                if not (0 <= m["start"] < m["end"] <= duration + 1):
                    continue
                m["end"] = min(m["end"], duration)
                m["scores"] = {k: int(max(0, min(100, v))) for k, v in m["scores"].items()}
                m["platforms"] = {k: int(max(0, min(100, v))) for k, v in m["platforms"].items()}
                proposals.append(m)
            db.log_operation("content_understanding", "claude",
                             f"{len(proposals)} moments, {len(chapters)} chapters", source_id=source_id)
        except Exception as e:  # noqa: BLE001
            db.log_operation("content_understanding", "heuristic", f"Claude unavailable: {e!r}", source_id=source_id)
            engine = "heuristic"
    if engine == "heuristic":
        if not llm.available():
            db.log_operation("content_understanding", "heuristic",
                             "No Claude credentials / transcript; using signal heuristics", source_id=source_id)
        for w in windows[: max_moments * 3]:
            proposals.append({"start": w["start"], "end": w["end"], "hook_start": w["hook_start"],
                              "hook_end": w["hook_end"], "payoff_end": w["payoff_end"], "peak": w["peak"],
                              "structure": "hook_first" if w["peak"] - w["start"] > 8 else "context_first",
                              "title": "", "why": "", "content_type": category, "emphasis_words": []})
        if is_clip and not proposals:
            proposals.append({"start": 0, "end": duration, "hook_start": 0, "hook_end": min(3, duration),
                              "payoff_end": duration, "structure": "context_first", "title": "", "why": "",
                              "content_type": category, "emphasis_words": []})
        chapters = heuristic_chapters(duration, audio.get("silences", []), scenes, segments)

    # 4. Face tracking, scoring, selection
    progress(0.8, "Scoring moments")
    moments = []
    for k, m in enumerate(proposals):
        face = media.face_track(path, m["start"], m["end"], samples=6)
        measured = score_window({**m, "peak": m.get("peak", (m["hook_start"] + m["hook_end"]) / 2)},
                                sig, segments, audio, face)
        scores = m.get("scores") or measured
        if m.get("scores"):
            # Blend Claude's editorial read with measured audio/visual quality.
            for key in ("audio_quality", "visual_quality"):
                scores[key] = int(round((scores[key] + measured[key]) / 2))
        length = m["end"] - m["start"]
        essential = max(1.0, m["payoff_end"] - m["hook_start"]) + 4
        plat = m.get("platforms") or platform_scores(scores, length, essential)
        ov = overall(scores)
        excerpt = transcribe.text_between(segments, m["start"], m["end"])
        title = m.get("title") or _auto_title(segments, m, k)
        moments.append({**m, "scores": scores, "platforms": plat, "overall": ov, "face": face,
                        "excerpt": excerpt, "title": title,
                        "why": m.get("why") or explain(scores)})
        progress(0.8 + 0.15 * (k + 1) / max(1, len(proposals)), f"Scored {k+1}/{len(proposals)}")

    moments.sort(key=lambda m: m["overall"], reverse=True)
    keep = []
    for m in moments:
        if len(keep) >= max_moments:
            break
        if not is_clip and m["overall"] < settings["min_moment_score"]:
            continue
        if any(min(m["end"], o["end"]) - max(m["start"], o["start"]) > 0.5 * (m["end"] - m["start"]) for o in keep):
            continue
        keep.append(m)
    if is_clip and not keep and moments:
        keep = moments[:1]  # you uploaded this clip deliberately; keep the best trim

    # 5. Persist (replace previous candidates that were never turned into edits)
    db.execute("DELETE FROM moments WHERE source_id=? AND status='candidate' AND id NOT IN "
               "(SELECT moment_id FROM edit_plans WHERE moment_id IS NOT NULL)", (source_id,))
    for m in keep:
        db.insert("moments", source_id=source_id, kind="short", start=m["start"], end=m["end"],
                  hook_start=m["hook_start"], hook_end=m["hook_end"], payoff_end=m["payoff_end"],
                  structure=m["structure"], title=m["title"], summary=m.get("why", ""),
                  reasons=m["why"], excerpt=m["excerpt"][:4000], content_type=m.get("content_type") or category,
                  scores=m["scores"], overall_score=m["overall"], platform_scores=m["platforms"],
                  signals={"face": m["face"], "emphasis_words": m.get("emphasis_words", []),
                           "peak": m.get("peak")},
                  engine=engine, status="candidate", created_at=db.now())
    for c in chapters:
        db.insert("moments", source_id=source_id, kind="long", start=c["start"], end=c["end"],
                  hook_start=c["start"], hook_end=c["start"], payoff_end=c["end"], structure="context_first",
                  title=c["title"], summary=c.get("summary", ""), reasons="", content_type=c.get("content_type"),
                  excerpt=transcribe.text_between(segments, c["start"], min(c["end"], c["start"] + 120))[:2000],
                  scores={}, overall_score=None, platform_scores={}, signals={},
                  engine=engine, status="candidate", created_at=db.now())

    faces_summary = media.face_track(path, 0, duration, samples=10)
    db.upsert("analyses", "source_id", source_id=source_id,
              audio={"hop": audio["hop"], "db": audio["db"], "silences": audio["silences"], "stats": audio["stats"],
                     "excitement": [round(float(x), 3) for x in sig["excitement"].tolist()]},
              scenes=scenes, faces=faces_summary, chapters=chapters, summary=summary, engine=engine,
              created_at=db.now())
    db.update("sources", source_id, status="analyzed", category=category)
    progress(1.0, f"Found {len(keep)} short-form moments and {len(chapters)} long-form segments")
    return {"moments": len(keep), "chapters": len(chapters), "engine": engine}


def _auto_title(segments: list[dict], m: dict, k: int) -> str:
    hook = transcribe.text_between(segments, m["hook_start"], m["hook_end"]).strip()
    if hook:
        hook = re.sub(r"\s+", " ", hook)
        return (hook[:70] + "…") if len(hook) > 70 else hook
    return f"Moment at {_fmt(m.get('peak', m['start']))}"
