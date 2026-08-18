"""Post-call analysis via Gemini REST (audio + transcript)."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import httpx
from loguru import logger

from config import settings

_TIMEOUT_SECONDS = 90.0
_MIN_WAV_BYTES = 16000  # ~0.5s of PCM16 mono 16kHz audio

_ALLOWED_DISPOSITIONS = frozenset(
    {
        "demo_booked",
        "interested",
        "pricing_inquiry",
        "inquiry",
        "callback_requested",
        "service_booking",
        "complaint",
        "not_interested",
        "other",
    }
)


def _system_prompt() -> str:
    """System prompt for call analysis — configured via .env, never hardcoded."""
    return (settings.gemini_call_analysis_prompt or "").strip()


async def analyze_call_audio(wav_path: str) -> dict:
    """Analyze a WAV call recording via Gemini (inline audio). Empty for bad audio."""
    try:
        size = Path(wav_path).stat().st_size
    except Exception as e:
        logger.warning("WAV not readable: {} — {}", wav_path, e)
        return {}
    if size < _MIN_WAV_BYTES:
        logger.info("WAV too short to analyze ({} bytes): {}", size, wav_path)
        return {}
    try:
        data = base64.b64encode(Path(wav_path).read_bytes()).decode("ascii")
    except Exception as e:
        logger.warning("Failed to read WAV {}: {}", wav_path, e)
        return {}
    return await _gemini_analyze(
        [{"inline_data": {"mime_type": "audio/wav", "data": data}}],
        _system_prompt(),
    )


async def analyze_call_transcript(transcript, *args, **kwargs) -> dict:
    """Analyze a call transcript (text) via Gemini. Empty dict for empty input."""
    if transcript is None or not str(transcript).strip():
        return {}
    return await _gemini_analyze([{"text": str(transcript)}], _system_prompt())


async def _gemini_analyze(parts: list, system_prompt: str) -> dict:
    """Send parts to Gemini generateContent and parse the JSON response."""
    api_key = (settings.gemini_api_key or "").strip()
    if not api_key:
        logger.warning("Gemini analysis skipped: no GEMINI_API_KEY configured")
        return {}
    if not system_prompt:
        logger.warning("Gemini analysis skipped: GEMINI_CALL_ANALYSIS_PROMPT not set in .env")
        return {}
    model = (settings.gemini_call_analysis_model or "gemini-2.5-flash").strip()
    endpoint = settings.gemini_endpoint or ""
    if not endpoint:
        logger.warning("Gemini analysis skipped: GEMINI_ENDPOINT not set")
        return {}
    url = endpoint.format(model=model)
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {"responseMimeType": "application/json"},
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, params={"key": api_key}, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("Gemini analysis request failed: {}", e)
        return {}
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as e:
        logger.warning("Gemini analysis response missing text: {}", e)
        return {}
    text = str(text).strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        text = text.strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    try:
        result = json.loads(text)
    except (ValueError, TypeError) as e:
        logger.warning("Gemini analysis returned invalid JSON: {}", e)
        return {}
    if not isinstance(result, dict):
        logger.warning("Gemini analysis returned a non-object JSON payload")
        return {}
    return result


def canonical_disposition(disposition, *args, **kwargs) -> str:
    """Normalize any disposition string to the allowed set (default not_interested)."""
    if disposition is None:
        return "not_interested"
    raw = str(disposition).strip().lower()
    if not raw:
        return "not_interested"
    if raw in _ALLOWED_DISPOSITIONS:
        return raw
    norm = re.sub(r"[^a-z]", "", raw)
    if "notinterested" in norm:
        return "not_interested"
    if "demo" in norm:
        return "demo_booked"
    if "pricing" in norm:
        return "pricing_inquiry"
    if "callback" in norm:
        return "callback_requested"
    if "interested" in norm:
        return "interested"
    if "inquiry" in norm or "enquiry" in norm or "interest" in norm:
        return "inquiry"
    if "booking" in norm or "service" in norm:
        return "service_booking"
    if "complaint" in norm:
        return "complaint"
    return "other"