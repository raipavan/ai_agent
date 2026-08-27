"""Live greeting PCM capture from Gemini 3.1 Flash (same model/voice as the call).

Connects to Gemini Live WebSocket, asks the model to speak the greeting text
word-for-word, and returns the raw 24 kHz PCM so it can be cached as the
pre-recorded opening audio (``greeting_{role}.pcm``).
"""

from __future__ import annotations

import asyncio
import base64
import json

import websockets

from loguru import logger

from config import settings


def _role_voice(role: str) -> str:
    """Pick the live voice for a role (mirrors vobiz_bridge._resolve_session_context)."""
    try:
        if role == "sales_2" and (settings.gemini_live_voice_sales_2 or "").strip():
            return settings.gemini_live_voice_sales_2.strip()
        if role == "sales_1" and (settings.gemini_live_voice_sales_1 or "").strip():
            return settings.gemini_live_voice_sales_1.strip()
        return (settings.gemini_live_voice or "Leda").strip()
    except Exception:
        return "Leda"


async def capture_live_greeting_pcm(role: str, text: str):
    """Speak ``text`` via Gemini 3.1 Flash and return ``(pcm, sample_rate)``.

    Sample rate is Gemini Live's native 24 kHz; callers resample to 16 kHz
    before caching/playing. Raises RuntimeError on failure.
    """
    api_key = (settings.gemini_api_key or "").strip()
    model = (settings.gemini_live_model or "models/gemini-3.1-flash-live-preview").strip()
    voice = _role_voice(role)
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set — cannot capture greeting")

    ws_url = (
        "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
        f"?key={api_key}"
    )
    system_text = (
        "You are Priya, the professional greeting voice for an outbound sales call from OpusHire, "
        "an AI-powered Unified Recruitment Infrastructure platform in India. "
        "You MUST speak with a warm, clear, natural, authentic Indian English accent (natural Indian cadence and pronunciation). "
        "Speak the greeting EXACTLY as written below, word for word. "
        "Do not add any extra words, filler, or commentary. Output only the spoken greeting."
    )

    chunks: list[bytes] = []
    setup_done = asyncio.Event()
    turn_done = asyncio.Event()

    async def reader(ws):
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("setupComplete") is not None:
                setup_done.set()
                continue
            server = msg.get("serverContent") or {}
            turn = server.get("modelTurn") or {}
            for part in turn.get("parts", []):
                audio = part.get("inlineData") or part.get("audio") or {}
                data = audio.get("data")
                if data:
                    chunks.append(base64.b64decode(data))
            if server.get("turnComplete"):
                turn_done.set()
                break

    async with websockets.connect(
        ws_url,
        max_size=2**23,
        open_timeout=20,
        close_timeout=10,
    ) as ws:
        setup = {
            "setup": {
                "model": model,
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}},
                    },
                },
                "systemInstruction": {"parts": [{"text": system_text}]},
            }
        }
        await ws.send(json.dumps(setup))
        reader_task = asyncio.create_task(reader(ws))
        try:
            await asyncio.wait_for(setup_done.wait(), timeout=20)
        except asyncio.TimeoutError:
            reader_task.cancel()
            raise RuntimeError("Gemini Live did not complete setup for greeting capture")
        await ws.send(
            json.dumps(
                {
                    "clientContent": {
                        "turns": [{"role": "user", "parts": [{"text": text}]}],
                        "turnComplete": True,
                    }
                }
            )
        )
        try:
            await asyncio.wait_for(turn_done.wait(), timeout=60)
        except asyncio.TimeoutError:
            reader_task.cancel()
            raise RuntimeError("Timed out waiting for Gemini greeting audio")
        reader_task.cancel()

    pcm = b"".join(chunks)
    if not pcm:
        raise RuntimeError("Gemini returned no audio for greeting capture")
    logger.info(
        "Captured greeting via Gemini 3.1 Flash: role={} model={} voice={} ({} bytes)",
        role,
        model,
        voice,
        len(pcm),
    )
    return pcm, 24000


async def save_greeting_pcm_file(
    role: str,
    pcm: bytes,
    sr: int,
    *,
    variant: str = "",
    greeting_text: str = "",
):
    """Persist greeting PCM + meta to ``greeting_{role}[_variant].pcm``. Returns the path."""
    import hashlib

    from core.greeting_pcm import greeting_pcm_paths

    pcm_path, meta_path = greeting_pcm_paths(role, variant)
    pcm_path.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.md5((greeting_text or "").strip().encode()).hexdigest()[:16]
    meta = {
        "text_hash": h,
        "voice": _role_voice(role),
        "sr": int(sr),
        "source": "gemini_live_capture",
        "model": (settings.gemini_live_model or "models/gemini-3.1-flash-live-preview").strip(),
    }
    pcm_path.write_bytes(pcm)
    meta_path.write_text(json.dumps(meta, indent=0), encoding="utf-8")
    logger.info("Saved greeting PCM role={} variant={} ({} bytes)", role, variant or "(default)", len(pcm))
    return pcm_path
