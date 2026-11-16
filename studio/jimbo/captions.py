"""Caption generation (source-timed lines) and SRT/ASS export on the output timeline.

Lines are stored in *source* time inside the edit plan, so when you change a
cut the captions still line up. At render/export time they are mapped onto
the output timeline through the plan's segment list.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import transcribe

STYLE_PRESETS = {
    # font scale is relative to output height
    "bold_pop":  {"size": 0.052, "outline": 0.0062, "shadow": 0.0, "bold": 1, "box": False,
                  "primary": "&H00FFFFFF", "emph": "&H0000E1FF", "outline_c": "&H00000000"},
    "clean":     {"size": 0.040, "outline": 0.0035, "shadow": 0.002, "bold": 1, "box": False,
                  "primary": "&H00FFFFFF", "emph": "&H0066D9FF", "outline_c": "&H00101010"},
    "karaoke":   {"size": 0.050, "outline": 0.0058, "shadow": 0.0, "bold": 1, "box": False,
                  "primary": "&H00FFFFFF", "emph": "&H0000FF9C", "outline_c": "&H00000000"},
    "minimal":   {"size": 0.034, "outline": 0.0028, "shadow": 0.0, "bold": 0, "box": False,
                  "primary": "&H00FFFFFF", "emph": "&H00FFFFFF", "outline_c": "&H00000000"},
    "boxed":     {"size": 0.040, "outline": 0.006, "shadow": 0.0, "bold": 1, "box": True,
                  "primary": "&H00FFFFFF", "emph": "&H0000E1FF", "outline_c": "&H90000000"},
}

POSITION_MARGIN = {"lower": 0.07, "lower_third": 0.26, "center": 0.5, "upper": 0.12}

FILLER = {"um", "uh", "erm", "uhh", "umm", "like,", "um,", "uh,"}


def build_lines(segments_transcript: list[dict], spans: list[tuple[float, float]], params: dict,
                emphasis_words: list[str] | None = None, loud_times: list[float] | None = None) -> list[dict]:
    """Chunk transcript words inside ``spans`` into caption lines (source time)."""
    words = [w for w in transcribe.all_words(segments_transcript)
             if any(w["e"] > a and w["s"] < b for a, b in spans)]
    per = max(1, int(round(params.get("caption_words_per_chunk", 3))))
    emph_set = {re.sub(r"[^\w']", "", e.lower()) for e in (emphasis_words or []) if e}
    loud_times = loud_times or []
    lines: list[dict] = []
    cur: list[dict] = []

    def flush():
        if not cur:
            return
        text_words = [w["w"] for w in cur]
        emph = []
        for idx, w in enumerate(cur):
            key = re.sub(r"[^\w']", "", w["w"].lower())
            if key in emph_set or (params.get("caption_emphasis", 0) > 0.35 and re.search(r"!$", w["w"])):
                emph.append(idx)
            elif params.get("caption_emphasis", 0) > 0 and any(abs(w["s"] - t) < 0.25 for t in loud_times) \
                    and len(key) > 3:
                emph.append(idx)
        lines.append({"start": round(cur[0]["s"], 2), "end": round(max(cur[-1]["e"], cur[0]["s"] + 0.3), 2),
                      "text": " ".join(text_words), "emph": emph,
                      "words": [{"w": w["w"], "s": w["s"], "e": w["e"]} for w in cur]})
        cur.clear()

    for w in words:
        if w["w"].lower() in FILLER and params.get("cut_aggressiveness", 0.5) >= 0.5:
            continue
        if cur and (w["s"] - cur[-1]["e"] > 0.6):
            flush()
        cur.append(w)
        if len(cur) >= per or re.search(r"[.!?,]$", w["w"]):
            flush()
    flush()
    return lines


def map_to_output(lines: list[dict], timeline: list[dict]) -> list[dict]:
    """Map source-timed lines onto the output timeline (segments in play order)."""
    out = []
    for seg in timeline:
        a, b, off = seg["start"], seg["end"], seg["out_start"]
        for ln in lines:
            if ln["end"] <= a or ln["start"] >= b:
                continue
            s = max(ln["start"], a)
            e = min(ln["end"], b)
            if e - s < 0.12:
                continue
            words = [{"w": w["w"], "s": max(w["s"], a) - a + off, "e": min(w["e"], b) - a + off}
                     for w in ln.get("words", []) if w["e"] > a and w["s"] < b]
            out.append({"start": s - a + off, "end": e - a + off, "text": ln["text"],
                        "emph": ln.get("emph", []), "words": words})
    out.sort(key=lambda x: x["start"])
    for i in range(len(out) - 1):  # no overlapping captions
        if out[i]["end"] > out[i + 1]["start"]:
            out[i]["end"] = max(out[i]["start"] + 0.1, out[i + 1]["start"])
    return out


def _srt_ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    ms = int(round((s - int(s)) * 1000))
    if ms == 1000:
        ms = 999
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"


def write_srt(lines: list[dict], path: Path) -> Path:
    blocks = []
    for i, ln in enumerate(lines, 1):
        blocks.append(f"{i}\n{_srt_ts(ln['start'])} --> {_srt_ts(ln['end'])}\n{ln['text']}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def _ass_ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def write_ass(lines: list[dict], path: Path, width: int, height: int, params: dict, font: str = "Inter Black") -> Path:
    style = dict(STYLE_PRESETS.get(params.get("caption_style", "bold_pop"), STYLE_PRESETS["bold_pop"]))
    scale = float(params.get("caption_size", 1.0))
    base = min(width, height) if width > height else height
    size = int(base * style["size"] * scale * (0.62 if width > height else 1.0))
    outline = max(1, int(base * style["outline"] * scale))
    shadow = int(base * style["shadow"])
    pos = params.get("caption_position", "lower_third")
    if pos == "center":
        align, margin_v = 5, 0
    elif pos == "upper":
        align, margin_v = 8, int(height * POSITION_MARGIN["upper"])
    else:
        align, margin_v = 2, int(height * POSITION_MARGIN.get(pos, 0.2))
    border_style = 3 if style["box"] else 1
    upper = bool(params.get("caption_uppercase", False))
    anim = params.get("caption_animation", "none")
    margin_lr = int(width * 0.08)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},{style['primary']},{style['emph']},{style['outline_c']},&H80000000,{style['bold']},0,0,0,100,100,0,0,{border_style},{outline},{shadow},{align},{margin_lr},{margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for ln in lines:
        words = ln["text"].split()
        emph = set(ln.get("emph", []))
        prefix = ""
        if anim == "pop":
            prefix = r"{\fscx82\fscy82\t(0,90,\fscx100\fscy100)}"
        elif anim == "fade":
            prefix = r"{\fad(80,60)}"
        if params.get("caption_style") == "karaoke" and ln.get("words"):
            parts = []
            for w in ln["words"]:
                dur_cs = max(1, int(round((w["e"] - w["s"]) * 100)))
                token = _esc(w["w"].upper() if upper else w["w"])
                parts.append(rf"{{\kf{dur_cs}}}{token}")
            body = " ".join(parts)
            # In karaoke, secondary colour is the highlight that sweeps in.
            body = r"{\1c" + style["emph"] + r"&\2c" + style["primary"] + "&}" + body
        else:
            parts = []
            for i, w in enumerate(words):
                token = _esc(w.upper() if upper else w)
                if i in emph:
                    token = r"{\c" + style["emph"] + r"&}" + token + r"{\c" + style["primary"] + "&}"
                parts.append(token)
            body = " ".join(parts)
        events.append(f"Dialogue: 0,{_ass_ts(ln['start'])},{_ass_ts(ln['end'])},Cap,,0,0,0,,{prefix}{body}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return path
