"""Speech-to-text with word timestamps.

Engines, in order of preference for ``auto``:
  1. faster-whisper (pip install faster-whisper) - fast on CPU, word timestamps
  2. openai-whisper (pip install openai-whisper)
  3. none - the studio still works from audio/visual signals, and you can
     import an existing SRT/VTT/JSON transcript for any source.

Transcript format (stored in DB):
  [{"start": s, "end": e, "text": "...", "words": [{"w": "word", "s": s, "e": e}, ...]}]
"""
from __future__ import annotations

import re
from pathlib import Path

from . import config


class TranscriptionUnavailable(RuntimeError):
    pass


def available_engines() -> list[str]:
    engines = []
    try:
        import faster_whisper  # noqa: F401
        engines.append("faster-whisper")
    except ImportError:
        pass
    try:
        import whisper  # noqa: F401
        engines.append("openai-whisper")
    except ImportError:
        pass
    return engines


def transcribe(path: str | Path, progress=None) -> tuple[str, str, list[dict]]:
    settings = config.load_settings()
    choice = settings.get("transcription_engine", "auto")
    engines = available_engines()
    if choice not in ("auto", "none") and choice not in engines:
        raise TranscriptionUnavailable(f"Transcription engine '{choice}' is not installed")
    if choice == "none" or not engines:
        raise TranscriptionUnavailable(
            "No transcription engine installed. Run `pip install faster-whisper` "
            "or import an SRT/VTT transcript for this source.")
    engine = engines[0] if choice == "auto" else choice
    model_name = settings.get("whisper_model", "small")
    if engine == "faster-whisper":
        return _faster_whisper(path, model_name, progress)
    return _openai_whisper(path, model_name)


def _faster_whisper(path, model_name, progress):
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device="auto", compute_type="auto")
    segments, info = model.transcribe(str(path), word_timestamps=True, vad_filter=True)
    out = []
    total = getattr(info, "duration", 0) or 0
    for seg in segments:
        words = [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)}
                 for w in (seg.words or []) if w.word.strip()]
        out.append({"start": round(seg.start, 2), "end": round(seg.end, 2),
                    "text": seg.text.strip(), "words": words})
        if progress and total:
            progress(min(0.99, seg.end / total), f"Transcribing {seg.end/60:.0f}/{total/60:.0f} min")
    return "faster-whisper", getattr(info, "language", "") or "", out


def _openai_whisper(path, model_name):
    import whisper
    model = whisper.load_model(model_name)
    result = model.transcribe(str(path), word_timestamps=True)
    out = []
    for seg in result.get("segments", []):
        words = [{"w": w["word"].strip(), "s": round(w["start"], 2), "e": round(w["end"], 2)}
                 for w in seg.get("words", []) if w["word"].strip()]
        out.append({"start": round(seg["start"], 2), "end": round(seg["end"], 2),
                    "text": seg["text"].strip(), "words": words})
    return "openai-whisper", result.get("language", ""), out


_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


def _ts(s: str) -> float:
    m = _TS.search(s)
    if not m:
        raise ValueError(f"bad timestamp {s!r}")
    h, mi, se, ms = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(se) + int(ms.ljust(3, "0")) / 1000


def parse_subtitles(text: str) -> list[dict]:
    """Parse SRT or WebVTT into transcript segments with estimated word times."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text.strip())
    segs = []
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        a, _, b = lines[idx].partition("-->")
        try:
            start, end = _ts(a), _ts(b)
        except ValueError:
            continue
        body = " ".join(lines[idx + 1:])
        body = re.sub(r"<[^>]+>", "", body).strip()
        if body:
            segs.append({"start": round(start, 2), "end": round(end, 2), "text": body,
                         "words": estimate_words(body, start, end)})
    return segs


def estimate_words(text: str, start: float, end: float) -> list[dict]:
    words = text.split()
    if not words:
        return []
    weights = [max(1, len(w)) for w in words]
    total = sum(weights)
    out, t = [], start
    for w, wt in zip(words, weights):
        d = (end - start) * wt / total
        out.append({"w": w, "s": round(t, 2), "e": round(t + d, 2), "est": True})
        t += d
    return out


def all_words(segments: list[dict]) -> list[dict]:
    words = []
    for seg in segments or []:
        ws = seg.get("words") or estimate_words(seg.get("text", ""), seg["start"], seg["end"])
        words.extend(ws)
    return words


def text_between(segments: list[dict], start: float, end: float) -> str:
    return " ".join(w["w"] for w in all_words(segments) if w["e"] > start and w["s"] < end)


def sentences(segments: list[dict]) -> list[dict]:
    """Split transcript into sentence-ish units (for boundaries and the LLM)."""
    out, cur = [], []
    for w in all_words(segments):
        if cur and w["s"] - cur[-1]["e"] > 0.9:
            out.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?]$", w["w"]):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [{"start": ws[0]["s"], "end": ws[-1]["e"], "text": " ".join(x["w"] for x in ws)}
            for ws in out if ws]
