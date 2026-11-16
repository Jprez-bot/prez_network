"""Paths and runtime settings for the Jimbo4prezz studio.

Everything the studio creates lives under one data directory (default
``studio/data``; override with ``JIMBO_DATA_DIR``). Originals are copied or
referenced read-only; renders, thumbnails and Adobe hand-off files are written
to their own folders, so the original recording is never modified.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

STUDIO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("JIMBO_DATA_DIR", STUDIO_ROOT / "data")).resolve()

SOURCES_DIR = DATA_DIR / "sources"        # uploaded originals (never modified)
REFERENCES_DIR = DATA_DIR / "references"  # style reference uploads
RENDERS_DIR = DATA_DIR / "renders"        # rendered platform videos
THUMBS_DIR = DATA_DIR / "thumbnails"      # thumbnails / covers + Photoshop hand-off
ADOBE_DIR = DATA_DIR / "adobe"            # Premiere XML / ExtendScript hand-off
CAPTIONS_DIR = DATA_DIR / "captions"      # SRT / ASS files
CACHE_DIR = DATA_DIR / "cache"            # frames, waveforms, previews
DB_PATH = DATA_DIR / "studio.db"
SETTINGS_PATH = DATA_DIR / "settings.json"

ALL_DIRS = [DATA_DIR, SOURCES_DIR, REFERENCES_DIR, RENDERS_DIR, THUMBS_DIR,
            ADOBE_DIR, CAPTIONS_DIR, CACHE_DIR]

DEFAULT_SETTINGS = {
    "brand": "Jimbo4prezz",
    # Claude is used for content understanding when an API key / profile exists.
    "llm_enabled": True,
    "llm_model": "claude-opus-5-5",
    "llm_effort": "high",
    # Transcription: "auto" tries faster-whisper, then openai-whisper, else none
    # (you can still import an SRT/VTT transcript per source).
    "transcription_engine": "auto",
    "whisper_model": "small",
    # Selectivity: minimum overall score for a short-form moment to be kept.
    "min_moment_score": 62,
    # Hard ceiling on short-form moments per hour of footage (quality > quantity).
    "max_moments_per_hour": 4,
    "min_platform_score": 70,
    "render_preset": "medium",
    "render_crf": 20,
    "caption_font": "Inter Black",
    "thumbnail_font_path": "",
}


def ensure_dirs() -> None:
    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    ensure_dirs()
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            data.update(json.loads(SETTINGS_PATH.read_text()))
        except (OSError, json.JSONDecodeError):
            pass
    return data


def save_settings(updates: dict) -> dict:
    data = load_settings()
    for k, v in updates.items():
        if k in DEFAULT_SETTINGS:
            data[k] = v
    SETTINGS_PATH.write_text(json.dumps(data, indent=2))
    return data


def tool_path(name: str) -> str | None:
    return shutil.which(name)


FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/inter/Inter-Black.otf",
    "/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "C:/Windows/Fonts/ariblk.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def thumbnail_font() -> str | None:
    custom = load_settings().get("thumbnail_font_path")
    if custom and Path(custom).exists():
        return custom
    for f in FONT_CANDIDATES:
        if Path(f).exists():
            return f
    return None
