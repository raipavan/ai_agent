"""Audio transcription via Gemini REST (audio -> transcript)."""

from __future__ import annotations

from loguru import logger

from services.call_analyzer import analyze_call_audio


async def transcribe_audio(*args, **kwargs) -> str:
    """Transcribe a WAV recording; returns "" when no usable audio is available."""
    wav_path = kwargs.get("wav_path")
    if not wav_path:
        return ""
    try:
        analysis = await analyze_call_audio(str(wav_path))
    except Exception as e:
        logger.warning("Transcription failed for {}: {}", wav_path, e)
        return ""
    return str(analysis.get("transcript") or "")