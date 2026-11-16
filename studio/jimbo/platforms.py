"""Destination platforms and what each one needs from an edit.

These are the hard constraints and default tendencies of a destination. The
selected style profile sits on top of them and decides the actual editing
choices (pacing, captions, zooms, ...)."""
from __future__ import annotations

PLATFORMS: dict[str, dict] = {
    "youtube": {
        "label": "YouTube Long-Form", "form": "long",
        "formats": {"16:9": (1920, 1080)}, "default_format": "16:9",
        "duration": (180, 3600), "max_duration": 12 * 3600,
        "notes": "Context, story, chronological continuity. Not a montage.",
        "thumbnail": (1280, 720),
    },
    "youtube_shorts": {
        "label": "YouTube Shorts", "form": "short",
        "formats": {"9:16": (1080, 1920)}, "default_format": "9:16",
        "duration": (20, 60), "max_duration": 180,
        "notes": "Strong opening, readable captions, hook → context → payoff.",
        "thumbnail": (1080, 1920),
    },
    "tiktok": {
        "label": "TikTok", "form": "short",
        "formats": {"9:16": (1080, 1920)}, "default_format": "9:16",
        "duration": (12, 50), "max_duration": 600,
        "notes": "Fastest hook, caption-heavy, strategic zooms.",
        "thumbnail": (1080, 1920),
    },
    "instagram_reels": {
        "label": "Instagram Reels", "form": "short",
        "formats": {"9:16": (1080, 1920)}, "default_format": "9:16",
        "duration": (15, 55), "max_duration": 180,
        "notes": "Clean presentation, slightly calmer pacing than TikTok.",
        "thumbnail": (1080, 1920),
    },
    "x": {
        "label": "X / Twitter", "form": "short",
        "formats": {"16:9": (1920, 1080), "1:1": (1080, 1080), "9:16": (1080, 1920)},
        "default_format": "16:9",
        "duration": (8, 45), "max_duration": 140,
        "notes": "Conversational, strong first statement, minimal editing.",
        "thumbnail": (1280, 720),
    },
}

SHORT_PLATFORMS = [k for k, v in PLATFORMS.items() if v["form"] == "short"]
LONG_PLATFORMS = [k for k, v in PLATFORMS.items() if v["form"] == "long"]

CONTENT_TYPES = ["gaming", "just_chatting", "reaction", "story", "discussion",
                 "commentary", "highlight", "irl", "debate"]


def resolution(platform: str, aspect: str | None = None) -> tuple[str, int, int]:
    spec = PLATFORMS[platform]
    aspect = aspect if aspect in spec["formats"] else spec["default_format"]
    w, h = spec["formats"][aspect]
    return aspect, w, h


def label(platform: str) -> str:
    return PLATFORMS.get(platform, {}).get("label", platform)
