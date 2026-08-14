"""Vobiz telephony bridge (real implementation).

REST:      POST https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/
           Headers: X-Auth-ID, X-Auth-Token, Content-Type: application/json
XML:       <Response><Stream bidirectional="true" keepCallAlive="true"
           contentType="audio/x-l16;rate=16000">wss://…</Stream></Response>
WebSocket: JSON events — Vobiz→app: start/media/playedStream/clearedAudio;
           app→Vobiz: playAudio/checkpoint/clearAudio/stop.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
import wave

import httpx
import websockets
from loguru import logger

VOBIZ_API_BASE = "https://api.vobiz.ai/api/v1"

# Silence a long-lived connection warning from the client websockets lib.
try:
    import websockets.version
except Exception:  # pragma: no cover
    pass


class VobizCallError(Exception):
    """Raised when Vobiz refuses a call."""

    def __init__(self, status: int, message: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message


# ── REST: make a call ────────────────────────────────────────────────────

# Vobiz call UUID → local camp_id, so the hangup webhook can finalize the row.
_UUID_TO_CAMP: dict[str, str] = {}
_UUID_TO_ROLE: dict[str, str] = {}


def register_call_uuid(request_uuid: str, camp_id: str, role: str = "") -> None:
    if request_uuid:
        _UUID_TO_CAMP[request_uuid] = camp_id
        if role:
            _UUID_TO_ROLE[request_uuid] = role


def resolve_camp_from_uuid(uuid_value: str) -> tuple[str, str]:
    """Return (camp_id, role) for a Vobiz call UUID, or ("", "")."""
    return _UUID_TO_CAMP.get(uuid_value, ""), _UUID_TO_ROLE.get(uuid_value, "")


def _notify_failure(from_number: str, to: str, err: "VobizCallError") -> None:
    """Push an operator notification when Vobiz refuses a call for an agent.

    The agent (role) is resolved from the caller-ID number so every call path
    (manual, campaign, callbacks, factory sandbox) reports into the right
    agent's notification feed.
    """
    try:
        import re as _re

        from core import notifications
        from core.outbound_numbers import build_phone_to_role_map

        digits = _re.sub(r"\D", "", str(from_number or ""))
        try:
            phone_map = build_phone_to_role_map()
        except Exception:
            phone_map = {}
        role = phone_map.get(digits, "")
        if not role:
            role = "sales_1"

        if err.status == 402:
            title = "Insufficient balance"
        elif err.status == 403:
            title = "Number blocked"
        else:
            title = "Outbound call failed"
        notifications.push_notification(
            role,
            title,
            f"{to} (from {from_number}) — {err.message}",
            kind="call",
        )
    except Exception as exc:
        logger.warning("Failed to push Vobiz failure notification: {}", exc)


async def make_vobiz_call(
    to: str,
    from_: str,
    answer_url: str,
    auth_id: str,
    auth_token: str,
    **kwargs,
) -> dict:
    """Initiate an outbound call via the Vobiz REST API.

    Returns the parsed JSON response (``{"api_id", "message", "request_uuid"}``).
    Raises ``VobizCallError`` on any failure — a notification is pushed for the
    owning agent first.
    """
    url = f"{VOBIZ_API_BASE}/Account/{auth_id}/Call/"
    payload = {
        "from": from_,
        "to": to,
        "answer_url": answer_url,
        "answer_method": "POST",
    }
    for key in ("hangup_url", "ring_url", "time_limit", "caller_name"):
        if kwargs.get(key):
            payload[key] = kwargs[key]
    if kwargs.get("machine_detection"):
        payload["machine_detection"] = kwargs["machine_detection"]

    headers = {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": auth_token,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        err = VobizCallError(502, f"Could not reach Vobiz API: {exc}")
        _notify_failure(from_, to, err)
        raise err

    if resp.status_code >= 400:
        try:
            body = resp.json()
            message = body.get("message") or body.get("error") or body.get("detail") or resp.text
        except Exception:
            message = resp.text
        err = VobizCallError(resp.status_code, (message or "").strip()[:300])
        if resp.status_code == 403:
            try:
                from core.outbound_numbers import mark_number_blocked

                mark_number_blocked(from_)
            except Exception:
                pass
        _notify_failure(from_, to, err)
        raise err

    try:
        data = resp.json()
    except Exception:
        return {"message": "Call fired", "request_uuid": ""}
    # Vobiz may return 200 with an ``error`` field (e.g. blocked number / DLT).
    if isinstance(data, dict) and data.get("error"):
        err = VobizCallError(502, str(data["error"]).strip()[:300])
        _notify_failure(from_, to, err)
        raise err

    # Correlate Vobiz's call UUID with our camp_id for hangup-webhook finalize.
    try:
        from urllib.parse import urlparse, parse_qs

        camp_id = parse_qs(urlparse(answer_url).query).get("camp_id", [""])[0]
        role_param = parse_qs(urlparse(answer_url).query).get("role", [""])[0]
        if camp_id:
            register_call_uuid(
                str(data.get("request_uuid") or ""), camp_id, role_param or ""
            )
    except Exception:
        pass
    return data


# ── Voice XML ────────────────────────────────────────────────────────────


def _xml_escape(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _stream_xml(wss_url: str, greeting_text: str = "", status_callback_url: str = "") -> str:
    url = _xml_escape((wss_url or "").strip())
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<Response>"]
    if greeting_text:
        parts.append(f"<Speak>{_xml_escape(greeting_text)}</Speak>")
    stream = (
        'bidirectional="true" keepCallAlive="true" maxRetries="5" '
        'contentType="audio/x-l16;rate=16000"'
    )
    if status_callback_url:
        stream += f' statusCallbackUrl="{_xml_escape(status_callback_url)}" statusCallbackMethod="POST"'
    parts.append(f"<Stream {stream}>{url}</Stream>")
    parts.append("</Response>")
    return "".join(parts)


def build_answer_xml(wss_url: str, greeting_text: str = "", status_callback_url: str = "") -> str:
    """Answer XML for outbound calls: fork audio to our WebSocket."""
    return _stream_xml(wss_url, greeting_text=greeting_text, status_callback_url=status_callback_url)


def build_incoming_stream_xml(wss_url: str, greeting_text: str = "", status_callback_url: str = "") -> str:
    """Answer XML for inbound calls: fork audio to our WebSocket."""
    return _stream_xml(wss_url, greeting_text=greeting_text, status_callback_url=status_callback_url)


def build_busy_message_xml() -> str:
    """Busy XML — Vobiz should not dial; reject the leg."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Reject reason="busy"/></Response>'
    )


# ── WebSocket media bridge (Vobiz ↔ Gemini Live) ─────────────────────────


def _resolve_session_context(camp_id, agent_id, manual_role, lead_name):
    """Return (role, opening_pcm, system_text, voice) for this leg."""
    from core.state import _CAMPAIGN_DATA, get_state

    role = None
    opening_pcm = None
    greeting_spoken = False
    if camp_id and camp_id in _CAMPAIGN_DATA:
        entry = _CAMPAIGN_DATA[camp_id] or {}
        role = entry.get("_role") or entry.get("role")
        opening_pcm = entry.get("opening_pcm")
        greeting_spoken = bool(entry.get("_greeting_spoken"))
    role = (role or manual_role or "sales_1").strip().lower()

    try:
        state = get_state(role)
    except Exception:
        state = {}
    from prompts.priya import get_role_prompt_text

    prompt = (state.get("prompt") or "").strip() or get_role_prompt_text(role)

    rag_ctx = ""
    try:
        from core.state import rag_context_for_role

        rag_ctx = rag_context_for_role(
            role, "service booking pricing warranty insurance roadside emergency faq"
        )
    except Exception:
        rag_ctx = ""
    if not rag_ctx:
        rag_ctx = (state.get("rag") or "")[:3000]

    system_text = prompt.strip()
    if rag_ctx.strip():
        system_text = f"{system_text}\n\n## KNOWLEDGE BASE (use this to answer)\n{rag_ctx.strip()}"
    # Keep the system instruction compact so Gemini's first response is fast.
    system_text = system_text[:6000]
    if greeting_spoken:
        system_text += (
            "\n\nIMPORTANT: The phone system has ALREADY spoken your greeting to the caller. "
            "Do NOT repeat any greeting. Start by listening and respond naturally to what "
            "the caller says. Keep replies short."
        )

    try:
        from config import settings

        if role == "sales_2" and (settings.gemini_live_voice_sales_2 or "").strip():
            voice = settings.gemini_live_voice_sales_2.strip()
        elif role == "sales_1" and (settings.gemini_live_voice_sales_1 or "").strip():
            voice = settings.gemini_live_voice_sales_1.strip()
        else:
            voice = (settings.gemini_live_voice or "Leda").strip()
    except Exception:
        voice = "Leda"

    return role, opening_pcm, system_text, voice


async def _run_gemini_live(in_q: asyncio.Queue, out_q: asyncio.Queue, system_text: str, voice: str, stop_evt: asyncio.Event, transcript: list | None = None):
    """Bridge task: forward caller PCM to Gemini Live and push model audio out.

    ``transcript`` (when provided) collects live transcription lines
    ("Caller: …" / "Agent: …") from Gemini's transcription events so the call
    record can show the conversation after hangup.
    """
    from config import settings

    if transcript is None:
        transcript = []

    api_key = (settings.gemini_api_key or "").strip()
    model = (settings.gemini_live_model or "models/gemini-2.5-flash-live-preview").strip()
    if not api_key:
        logger.error("GEMINI_API_KEY not set — cannot run Gemini Live")
        return

    ws_url = (
        "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
        f"?key={api_key}"
    )

    try:
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
            logger.info("Gemini Live setup sent (model={} voice={})", model, voice)

            async def audio_reader():
                last_caller = ""
                last_agent = ""
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    server = msg.get("serverContent") or {}
                    if msg.get("setupComplete") is not None:
                        continue
                    if server.get("interrupted"):
                        await out_q.put(("interrupted", b""))
                        continue
                    # Live transcription: caller speech via inputTranscription
                    # (emitted by default), agent speech via outputTranscription
                    # when the API emits it. Only finalized utterances are kept
                    # (partials are ignored) and consecutive repeats deduped.
                    it = server.get("inputTranscription")
                    if isinstance(it, dict) and it.get("isFinal"):
                        text = (it.get("text") or "").strip()
                        if text and text != last_caller:
                            last_caller = text
                            transcript.append(f"Caller: {text}")
                    ot = server.get("outputTranscription")
                    if isinstance(ot, dict) and ot.get("isFinal"):
                        text = (ot.get("text") or "").strip()
                        if text and text != last_agent:
                            last_agent = text
                            transcript.append(f"Agent: {text}")
                    turn = server.get("modelTurn") or {}
                    for part in turn.get("parts", []):
                        audio = part.get("inlineData") or part.get("audio") or {}
                        data = audio.get("data")
                        if data:
                            await out_q.put(("audio", base64.b64decode(data)))
                    if server.get("turnComplete"):
                        await out_q.put(("turn_complete", b""))

            async def audio_sender():
                while not stop_evt.is_set():
                    try:
                        chunk = await asyncio.wait_for(in_q.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if not chunk:
                        continue
                    # New Gemini Live protocol: realtimeInput.audio (the old
                    # realtimeInput.mediaChunks form is deprecated). The audio
                    # object MUST include mimeType or Gemini closes with 1007
                    # "Request contains an invalid argument".
                    await ws.send(
                        json.dumps(
                            {
                                "realtimeInput": {
                                    "audio": {
                                        "data": base64.b64encode(chunk).decode(),
                                        "mimeType": "audio/pcm;rate=16000",
                                    }
                                }
                            }
                        )
                    )

            await asyncio.gather(audio_reader(), audio_sender())
    except Exception as exc:
        logger.error("Gemini Live session failed: {}", exc)
        msg = str(exc)[:300]
        if "credit" in msg.lower() or "billing" in msg.lower() or "prepayment" in msg.lower():
            title = "AI credits depleted"
            body = "Gemini Live needs billing top-up (ai.studio/projects). Conversation will not work until recharged."
        else:
            title = "AI voice engine error"
            body = msg
        for role in ("sales_1", "sales_2"):
            _push_once(role, title, body)


def _push_once(role: str, title: str, body: str) -> None:
    """Push a notification unless an identical unread one already exists."""
    try:
        from core.storage import _get_conn

        conn = _get_conn()
        row = conn.execute(
            "SELECT 1 FROM notifications WHERE role = %s AND title = %s AND read = 0 LIMIT 1",
            (role, title),
        ).fetchone()
        if row:
            return
    except Exception:
        pass
    from core import notifications

    notifications.push_notification(role, title, body, kind="system")


async def check_gemini_credits() -> bool:
    """Probe Gemini Live with the configured key. Returns True when healthy.

    Sends the SAME complete setup the bridge uses (a bare ``{"setup": {"model"}}
    message makes Gemini reply with a generic 1011 "Internal error encountered").
    Pushes deduped operator notifications on failure, and auto-resolves previous
    unread "AI credits depleted" / "AI voice engine error" alerts when healthy.
    """
    from config import settings

    api_key = (settings.gemini_api_key or "").strip()
    if not api_key:
        for role in ("sales_1", "sales_2"):
            _push_once(role, "AI voice engine error", "GEMINI_API_KEY is not set.")
        return False
    model = (settings.gemini_live_model or "models/gemini-2.5-flash-live-preview").strip()
    voice = (settings.gemini_live_voice or "Leda").strip()
    url = (
        "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
        f"?key={api_key}"
    )
    setup = {
        "setup": {
            "model": model,
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}},
                },
            },
            "systemInstruction": {"parts": [{"text": "Say nothing. Just acknowledge silently."}]},
        }
    }

    def _clear_alerts() -> None:
        """Auto-resolve stale unread AI alerts once the probe passes."""
        try:
            from core.storage import _get_conn

            conn = _get_conn()
            conn.execute(
                "UPDATE notifications SET read = 1 WHERE read = 0 AND title IN "
                "('AI credits depleted', 'AI voice engine error')"
            )
            logger.info("Gemini probe healthy — auto-resolved previous AI alerts")
        except Exception:
            pass

    try:
        async with websockets.connect(url, max_size=2**23, open_timeout=15) as ws:
            await ws.send(json.dumps(setup))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                msg = json.loads(raw)
                if msg.get("setupComplete") is not None:
                    logger.info("Gemini credits probe OK (setupComplete)")
                    _clear_alerts()
                    return True
                logger.warning("Gemini probe unexpected first message: {}", str(msg)[:150])
                return True
            except asyncio.TimeoutError:
                logger.info("Gemini credits probe OK (no error within 8s)")
                _clear_alerts()
                return True
    except Exception as exc:
        text = str(exc).split("; then sent")[0].strip()[:200]
        logger.warning("Gemini credits probe failed: {}", text)
        if any(k in text.lower() for k in ("credit", "prepayment", "billing")):
            for role in ("sales_1", "sales_2"):
                _push_once(
                    role,
                    "AI credits depleted",
                    "Gemini Live needs billing top-up (ai.studio/projects). Conversation will not work until recharged.",
                )
        else:
            for role in ("sales_1", "sales_2"):
                _push_once(role, "AI voice engine error", text)
        return False


async def _finalize_vobiz_call_leg(role: str, camp_id, lead_name, transcript_text: str = "") -> None:
    """Record end-of-call state and finalize Postgres rows after the media loop.

    Manual legs (``manual_…``) and incoming legs (``incoming_…``) get their
    ``manual_calls`` / ``incoming_calls`` rows closed here so history and the
    dashboard stop showing them as in-progress. Campaign-lead legs (plain or
    ``queued_…`` camp_ids) are polled by the campaign sub-worker, which watches
    ``_call_connected_at`` / ``_call_ended_at`` itself.

    ``transcript_text`` (captured live from Gemini's input/output transcription
    events) is stored on the row so the console call detail shows it.
    """
    ended_at = time.time()
    connected_at = None
    try:
        from core.state import _CAMPAIGN_DATA

        if camp_id and camp_id in _CAMPAIGN_DATA:
            connected_at = _CAMPAIGN_DATA[camp_id].get("_call_connected_at")
            _CAMPAIGN_DATA[camp_id]["_call_ended_at"] = ended_at
            _CAMPAIGN_DATA[camp_id]["_transcript_text"] = transcript_text
    except Exception as exc:
        logger.warning(
            "Failed to set Vobiz ended state for camp_id={}: {}", camp_id, exc
        )

    duration_sec = None
    if connected_at is not None:
        try:
            duration_sec = max(0.0, ended_at - float(connected_at))
        except (TypeError, ValueError):
            duration_sec = None

    analysis = {}
    if transcript_text:
        analysis = {"transcript": transcript_text, "summary": transcript_text[:200]}

    if camp_id and str(camp_id).startswith("manual_"):
        try:
            from core.storage import (
                finalize_manual_call_record,
                manual_call_row_by_camp_id,
            )

            if await manual_call_row_by_camp_id(camp_id):
                await finalize_manual_call_record(camp_id, "", duration_sec, analysis)
                logger.info(
                    "Vobiz manual call finalized: camp_id={} duration={}s transcript={} chars",
                    camp_id, duration_sec, len(transcript_text),
                )
        except Exception as exc:
            logger.warning(
                "Failed to finalize manual call row for camp_id={}: {}", camp_id, exc
            )
    elif camp_id and str(camp_id).startswith("incoming_"):
        try:
            from core.storage import (
                finalize_incoming_call_record,
                incoming_call_row_by_camp_id,
            )

            if await incoming_call_row_by_camp_id(camp_id):
                await finalize_incoming_call_record(camp_id, "", duration_sec, analysis)
                logger.info(
                    "Vobiz incoming call finalized: camp_id={} duration={}s transcript={} chars",
                    camp_id, duration_sec, len(transcript_text),
                )
        except Exception as exc:
            logger.warning(
                "Failed to finalize incoming call row for camp_id={}: {}", camp_id, exc
            )

    if duration_sec is not None:
        try:
            from core.notifications import push_notification

            push_notification(
                role,
                "Call ended",
                f"{lead_name or 'Unknown'} — {int(round(duration_sec))}s",
                kind="call",
            )
        except Exception as exc:
            logger.warning("Failed to push Vobiz ended notification: {}", exc)

    logger.info("Vobiz call ended: camp_id={} duration={}s", camp_id, duration_sec)


async def handle_vobiz_ws_live(
    websocket,
    camp_id=None,
    agent_id=None,
    manual_role=None,
    lead_name=None,
):
    """Accept the Vobiz media WebSocket and run the AI conversation.

    Flow: Vobiz connects → ``start`` (grab streamId) → caller audio ``media``
    frames are resampled to 16 kHz PCM and fed to Gemini Live; Gemini's 24 kHz
    output is sent back as ``playAudio`` L16/24000 frames. A pre-recorded
    opening PCM (if primed) is played first.
    """
    await websocket.accept()

    role, opening_pcm, system_text, voice = _resolve_session_context(
        camp_id, agent_id, manual_role, lead_name
    )
    logger.info(
        "Vobiz WS live: camp_id={} role={} lead={} opening_pcm={}",
        camp_id, role, lead_name, bool(opening_pcm),
    )

    from services.vobiz_bridge.audio import pcm_resample

    stream_id: str | None = None
    inbound_rate = 16000
    stop_evt = asyncio.Event()
    in_q: asyncio.Queue = asyncio.Queue(maxsize=200)
    out_q: asyncio.Queue = asyncio.Queue(maxsize=200)
    transcript: list[str] = []
    gemini_task = asyncio.create_task(
        _run_gemini_live(in_q, out_q, system_text, voice, stop_evt, transcript)
    )

    async def play_audio(pcm: bytes, rate: int):
        if not stream_id or not pcm:
            return
        chunk_bytes = int(rate * 2 * 0.04)  # 40 ms frames
        for i in range(0, len(pcm), chunk_bytes):
            piece = pcm[i : i + chunk_bytes]
            if not piece:
                break
            await websocket.send_text(
                json.dumps(
                    {
                        "event": "playAudio",
                        "streamId": stream_id,
                        "media": {
                            "contentType": "audio/x-l16",
                            "sampleRate": rate,
                            "payload": base64.b64encode(piece).decode(),
                        },
                    }
                )
            )

    playing = False
    caller_pcm = bytearray()   # caller speech, 16 kHz PCM16
    agent_pcm = bytearray()    # agent (Gemini) speech, 24 kHz PCM16
    MAX_REC_SEC = 20 * 60
    CALLER_CAP = 16000 * 2 * MAX_REC_SEC
    AGENT_CAP = 24000 * 2 * MAX_REC_SEC

    async def playback_loop():
        nonlocal playing
        buffered = b""
        # Flush generated audio as soon as ~240 ms accumulates instead of
        # waiting for the whole turn — this removes the long silence before
        # the agent starts speaking.
        FLUSH_BYTES = 24000 * 2 * 0.24  # 240 ms of PCM16 at 24 kHz
        while not stop_evt.is_set():
            try:
                kind, payload = await asyncio.wait_for(out_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if kind == "interrupted":
                buffered = b""
                if playing and stream_id:
                    await websocket.send_text(
                        json.dumps({"event": "clearAudio", "streamId": stream_id})
                    )
                playing = False
                continue
            if kind == "audio":
                buffered += payload
                if len(agent_pcm) < AGENT_CAP:
                    agent_pcm.extend(payload)
                playing = True
                if len(buffered) >= FLUSH_BYTES:
                    await play_audio(buffered, 24000)
                    buffered = b""
                continue
            if kind == "turn_complete":
                if buffered:
                    await play_audio(buffered, 24000)
                if stream_id:
                    await websocket.send_text(
                        json.dumps({"event": "checkpoint", "streamId": stream_id, "name": f"t{int(time.time()*1000)}"})
                    )
                buffered = b""
                playing = False

    playback_task = asyncio.create_task(playback_loop())

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except Exception:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            ev = msg.get("event")
            if ev == "start":
                start = msg.get("start") or {}
                stream_id = start.get("streamId")
                fmt = start.get("mediaFormat") or {}
                inbound_rate = int(fmt.get("sampleRate") or 16000)
                logger.info("Vobiz stream started: stream_id={} rate={}", stream_id, inbound_rate)
                try:
                    from core.state import _CAMPAIGN_DATA

                    if camp_id and camp_id in _CAMPAIGN_DATA:
                        _CAMPAIGN_DATA[camp_id]["_call_connected_at"] = time.time()
                except Exception as exc:
                    logger.warning(
                        "Failed to set Vobiz connected state for camp_id={}: {}",
                        camp_id, exc,
                    )
                try:
                    from core.notifications import push_notification

                    push_notification(
                        role,
                        "Call connected",
                        f"{lead_name or 'Unknown'} answered the call",
                        kind="call",
                    )
                except Exception as exc:
                    logger.warning("Failed to push Vobiz connected notification: {}", exc)
                logger.info("Vobiz call connected: camp_id={}", camp_id)
                # The answer XML already speaks the greeting via <Speak> (Vobiz
                # TTS, zero Gemini latency). Only play a recorded opening PCM
                # when the greeting was NOT already spoken, to avoid the caller
                # hearing the greeting twice.
                greeting_spoken = False
                try:
                    from core.state import _CAMPAIGN_DATA

                    greeting_spoken = bool(
                        camp_id in _CAMPAIGN_DATA
                        and _CAMPAIGN_DATA[camp_id].get("_greeting_spoken")
                    )
                except Exception:
                    pass
                if opening_pcm and not greeting_spoken:
                    pcm, sr = opening_pcm
                    pcm16k, sr = pcm_resample(pcm, int(sr or 24000), 16000)
                    await play_audio(pcm16k, 16000)
            elif ev == "media":
                media = msg.get("media") or {}
                payload = media.get("payload")
                if not payload:
                    continue
                pcm = base64.b64decode(payload)
                if inbound_rate != 16000:
                    pcm, _ = pcm_resample(pcm, inbound_rate, 16000)
                if len(caller_pcm) < CALLER_CAP:
                    caller_pcm.extend(pcm)
                try:
                    in_q.put_nowait(pcm)
                except asyncio.QueueFull:
                    pass
            elif ev == "stop":
                break
            # playedStream / clearedAudio are informational; ignored.
    finally:
        try:
            await _finalize_vobiz_call_leg(
                role, camp_id, lead_name, transcript_text="\n".join(transcript)
            )
        except Exception as exc:
            logger.warning(
                "Vobiz call finalize raised for camp_id={}: {}", camp_id, exc
            )
        stop_evt.set()
        for task in (gemini_task, playback_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(gemini_task, playback_task, return_exceptions=True)
        try:
            await websocket.close(code=1000)
        except Exception:
            pass
        logger.info("Vobiz WS live ended for camp_id={}", camp_id)

        # Post-call analysis: transcribe the recorded audio + sentiment, in the
        # background so hangup is never delayed. Live transcription lines
        # (already written by _finalize_vobiz_call_leg) act as fallback.
        captured = bytes(caller_pcm) + b"" + bytes(agent_pcm)
        if camp_id and len(captured) >= 16000 * 2:  # >= 1 s of audio
            try:
                asyncio.create_task(
                    _analyze_and_store_call(
                        role, camp_id, bytes(caller_pcm), bytes(agent_pcm),
                        "\n".join(transcript),
                    )
                )
            except Exception as exc:
                logger.warning("Failed to schedule post-call analysis: {}", exc)


async def _analyze_and_store_call(
    role: str, camp_id: str, caller_pcm: bytes, agent_pcm: bytes, live_transcript: str
) -> None:
    """Offline post-call analysis: transcribe caller+agent audio via Gemini REST,
    extract sentiment/disposition, and persist into the manual/incoming call row.

    Runs fire-and-forget after hangup. Falls back to the live transcription
    lines when the audio analysis yields nothing.
    """
    from core.storage import _get_conn

    wav_path = None
    analysis: dict = {}
    try:
        from services.vobiz_bridge.audio import pcm_resample

        agent16 = agent_pcm
        if agent_pcm:
            agent16, _ = pcm_resample(agent_pcm, 24000, 16000)
        # Mix caller + agent into one 16 kHz mono track (agent at 0.7 gain).
        total = max(len(caller_pcm), len(agent16))
        if total >= 16000 * 2:
            frames = bytearray()
            for i in range(0, total - 1, 2):
                cs = int.from_bytes(caller_pcm[i:i+2], "little", signed=True) if i < len(caller_pcm) - 1 else 0
                as_ = int.from_bytes(agent16[i:i+2], "little", signed=True) if i < len(agent16) - 1 else 0
                s = cs + int(as_ * 0.7)
                frames += int(max(-32768, min(32767, s))).to_bytes(2, "little", signed=True)
            if frames:
                fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="vobiz_call_")
                os.close(fd)
                with wave.open(wav_path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(16000)
                    wf.writeframes(bytes(frames))
                from services.call_analyzer import analyze_call_audio

                analysis = await analyze_call_audio(wav_path)
    except Exception as exc:
        logger.warning("Post-call audio analysis failed for {}: {}", camp_id, exc)
    finally:
        if wav_path:
            try:
                os.remove(wav_path)
            except Exception:
                pass

    if not analysis and live_transcript.strip():
        try:
            from services.call_analyzer import analyze_call_transcript

            analysis = await analyze_call_transcript(live_transcript)
        except Exception as exc:
            logger.warning("Post-call text analysis failed for {}: {}", camp_id, exc)

    if not analysis:
        logger.info("Post-call analysis empty for {} — nothing stored", camp_id)
        return

    live_lines = [ln.strip() for ln in live_transcript.splitlines() if ln.strip()]
    if len(live_lines) >= 2 and not (analysis.get("transcript") or "").strip():
        analysis["transcript"] = live_transcript
    elif (analysis.get("transcript") or "").strip() and not live_lines:
        pass
    elif len(live_lines) >= 2 and (analysis.get("transcript") or "").strip():
        # Keep the richer one (prefer the offline verbatim transcript).
        pass

    emo = (analysis.get("emotion") or "").strip()
    conf = analysis.get("emotion_confidence")
    try:
        conf_f = float(conf) if conf not in (None, "") else None
    except (TypeError, ValueError):
        conf_f = None
    disp = (analysis.get("disposition") or "").strip()
    summary = (analysis.get("summary") or analysis.get("transcript") or "")[:500]
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT 1 FROM manual_calls WHERE camp_id = %s", (camp_id,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE manual_calls SET analysis_json = %s, summary = %s, "
                "emotion_label = %s, emotion_rationale = %s, emotion_confidence = %s, "
                "disposition = %s WHERE camp_id = %s",
                (json.dumps(analysis, ensure_ascii=False), summary,
                 emo, (analysis.get("emotion_rationale") or "")[:300], conf_f,
                 disp, camp_id),
            )
            logger.info(
                "Post-call analysis stored: camp_id={} emotion={!r} transcript={} chars",
                camp_id, emo, len(analysis.get("transcript") or ""),
            )
            return
        irow = conn.execute(
            "SELECT 1 FROM incoming_calls WHERE camp_id = %s", (camp_id,)
        ).fetchone()
        if irow:
            conn.execute(
                "UPDATE incoming_calls SET analysis_json = %s, summary = %s, "
                "emotion_label = %s, emotion_rationale = %s, emotion_confidence = %s, "
                "disposition = %s WHERE camp_id = %s",
                (json.dumps(analysis, ensure_ascii=False), summary,
                 emo, (analysis.get("emotion_rationale") or "")[:300], conf_f,
                 disp, camp_id),
            )
            logger.info(
                "Post-call analysis stored (incoming): camp_id={} emotion={!r} transcript={} chars",
                camp_id, emo, len(analysis.get("transcript") or ""),
            )
    except Exception as exc:
        logger.warning("Failed to persist post-call analysis for {}: {}", camp_id, exc)


def close_vobiz_client(*args, **kwargs) -> None:
    return None
