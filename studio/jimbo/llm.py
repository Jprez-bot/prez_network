"""Claude integration for content understanding.

Used for: finding and scoring moments from the transcript, choosing hook vs
context structure, writing platform titles/descriptions, and turning your
written style notes into editing parameters. Every call is optional - if no
credentials are configured the studio falls back to the heuristic engine and
records that in the operations log.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from . import config

_client = None


def available() -> bool:
    settings = config.load_settings()
    if not settings.get("llm_enabled", True):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    # An `ant auth login` profile also works with the zero-arg client.
    if shutil.which("ant"):
        try:
            out = subprocess.run(["ant", "auth", "status"], capture_output=True, timeout=10)
            return out.returncode == 0 and b"no active" not in out.stdout.lower()
        except (OSError, subprocess.TimeoutExpired):
            return False
    return False


def client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def ask_json(system: str, prompt: str, schema: dict, max_tokens: int = 32000) -> dict:
    """One structured call. Returns parsed JSON matching ``schema``.

    Streams (long transcripts) and opts into the server-side refusal fallback.
    Raises on any failure so callers can fall back to heuristics.
    """
    settings = config.load_settings()
    model = settings.get("llm_model") or "claude-opus-5-5"
    effort = settings.get("llm_effort") or "high"
    with client().beta.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        thinking={"type": "adaptive"},
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise RuntimeError("Claude declined the request")
    if message.stop_reason == "max_tokens":
        raise RuntimeError("Claude response was truncated (max_tokens)")
    text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
    return json.loads(text)


SYSTEM_EDITOR = (
    "You are the senior editor for Jimbo4prezz, a streamer/creator. You work from transcripts "
    "with timestamps and measured audio/visual signals of his own recordings and clips. "
    "You are selective: you would rather return six genuinely strong moments than forty weak ones. "
    "Never invent events, quotes, or reactions that are not supported by the transcript or signals. "
    "Scores are editorial recommendations, not virality guarantees."
)
