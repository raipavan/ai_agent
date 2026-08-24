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
from pathlib import Path

import httpx
import websockets
from loguru import logger

VOBIZ_API_BASE = "https://api.vobiz.ai/api/v1"

# Retained background tasks (post-call analysis) so they are never
# garbage-collected mid-flight.
_BACKGROUND_TASKS: set = set()

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


def _stream_xml(wss_url: str, greeting_text: str = "", status_callback_url: str = "", play_url: str = "") -> str:
    url = _xml_escape((wss_url or "").strip())
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<Response>"]
    # <Stream> MUST come first: it is a non-blocking verb, so Vobiz opens the
    # WebSocket immediately at pickup and then starts <Play>/<Speak>. This lets
    # the Gemini session warm up DURING the greeting instead of connecting only
    # after the greeting finishes (which cost ~5 s of dead air and missed the
    # caller's first words entirely).
    stream = (
        'bidirectional="true" keepCallAlive="true" maxRetries="5" '
        'contentType="audio/x-l16;rate=16000"'
    )
    if status_callback_url:
        stream += f' statusCallbackUrl="{_xml_escape(status_callback_url)}" statusCallbackMethod="POST"'
    parts.append(f"<Stream {stream}>{url}</Stream>")
    if play_url:
        # Play the pre-recorded greeting immediately on pickup (Vobiz fetches
        # the WAV/MP3 itself) — no WebSocket setup latency before the opening.
        parts.append(f"<Play>{_xml_escape(play_url)}</Play>")
    elif greeting_text:
        parts.append(f"<Speak>{_xml_escape(greeting_text)}</Speak>")
    parts.append("</Response>")
    return "".join(parts)


def build_answer_xml(wss_url: str, greeting_text: str = "", status_callback_url: str = "", play_url: str = "") -> str:
    """Answer XML for outbound calls: fork audio to our WebSocket."""
    return _stream_xml(wss_url, greeting_text=greeting_text, status_callback_url=status_callback_url, play_url=play_url)


def build_incoming_stream_xml(wss_url: str, greeting_text: str = "", status_callback_url: str = "", play_url: str = "") -> str:
    """Answer XML for inbound calls: fork audio to our WebSocket."""
    return _stream_xml(wss_url, greeting_text=greeting_text, status_callback_url=status_callback_url, play_url=play_url)


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

        rag_query = "pricing proctoring interview assessment hiring demo portal screening background verification"
        rag_ctx = rag_context_for_role(role, rag_query)
    except Exception:
        rag_ctx = ""
    if not rag_ctx:
        rag_ctx = (state.get("rag") or "")[:3000]

    system_text = prompt.strip()
    if rag_ctx.strip():
        system_text = f"{system_text}\n\n## KNOWLEDGE BASE (use this to answer)\n{rag_ctx.strip()}"
    # Keep the system instruction compact so Gemini's first response is fast.
    try:
        from config import settings

        system_cap = int(settings.max_system_prompt_chars or 10000)
    except Exception:
        system_cap = 10000
    system_text = system_text[:system_cap]
    gemini_first = bool(settings.gemini_live_first_opening)

    if gemini_first:
        try:
            from core.state import resolved_greeting_text

            g_text = resolved_greeting_text(role).strip()
            if g_text:
                system_text = (
                    "CRITICAL GREETING INSTRUCTION: The call just connected with the prospect. "
                    f"You MUST speak this opening greeting immediately as your first response, word for word:\n\"{g_text}\"\n"
                    "Speak with a warm, natural, professional Indian English accent. "
                    "After speaking this opening greeting, stop, listen carefully to the caller's answer, and then continue the conversation naturally.\n\n"
                    + system_text
                )
        except Exception:
            pass
        needs_kick = True
        opening_pcm = None
    elif greeting_spoken:
        system_text = (
            "CRITICAL CALL CONTEXT: The phone system has ALREADY delivered the opening greeting to the caller: "
            "\"Hi, this is Priya from OpusHire. Is it the right time to speak?\" "
            "You are in MID-CONVERSATION. Do NOT repeat 'Hi, this is Priya', do NOT introduce yourself again, and do NOT repeat the greeting. "
            "If the caller confirms they are free to talk (yes / sure / go ahead / okay / of course), your VERY FIRST sentence must be exactly: "
            "\"We are calling regarding streamlining the entire recruitment process using AI.\" "
            "Only AFTER delivering that pitch line may you ask: \"How many candidates do you typically hire each month?\" "
            "Do not skip the pitch line. Listen to what the caller says and respond directly, helpfully, and conversationally to their words in an authentic Indian English or mirrored regional language.\n\n"
            + system_text
        )
        needs_kick = False
    else:
        needs_kick = False

    try:
        if role == "sales_2" and (settings.gemini_live_voice_sales_2 or "").strip():
            voice = settings.gemini_live_voice_sales_2.strip()
        elif role == "sales_1" and (settings.gemini_live_voice_sales_1 or "").strip():
            voice = settings.gemini_live_voice_sales_1.strip()
        else:
            voice = (settings.gemini_live_voice or "Leda").strip()
    except Exception:
        voice = "Leda"

    return role, opening_pcm, system_text, voice, needs_kick


async def _run_gemini_live(
    in_q: asyncio.Queue,
    out_q: asyncio.Queue,
    system_text: str,
    voice: str,
    stop_evt: asyncio.Event,
    transcript: list | None = None,
    needs_kick: bool = False,
):
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
                    "realtimeInputConfig": {
                        "automaticActivityDetection": {
                            "disabled": False,
                            "prefixPaddingMs": int(settings.gemini_live_vad_prefix_padding_ms or 30),
                            "silenceDurationMs": int(settings.gemini_live_vad_silence_duration_ms or 450),
                            "startOfSpeechSensitivity": (settings.gemini_live_start_sensitivity or "START_SENSITIVITY_HIGH").strip(),
                            "endOfSpeechSensitivity": (settings.gemini_live_end_sensitivity or "END_SENSITIVITY_HIGH").strip(),
                        },
                    },
                    # Both transcription configs MUST be present in the setup,
                    # otherwise Gemini never emits inputTranscription /
                    # outputTranscription events and the live transcript stays
                    # empty for the whole call.
                    "inputAudioTranscription": {},
                    "outputAudioTranscription": {},
                    "systemInstruction": {"parts": [{"text": system_text}]},
                }
            }
            await ws.send(json.dumps(setup))
            logger.info("Gemini Live setup sent (model={} voice={})", model, voice)

            async def keepalive():
                # Keep the Gemini Live WebSocket alive with protocol-level pings
                # so idle-tunnel/proxy timeouts do not drop the session with
                # "no close frame received or sent".
                try:
                    while True:
                        await asyncio.sleep(20)
                        await ws.ping()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.warning("Gemini Live keepalive ended: {}", exc)

            keepalive_task = asyncio.create_task(keepalive())

            # Shared turn state: True while the model is producing audio.
            # The hybrid VAD sender must not finalize the caller turn with
            # audioStreamEnd while Gemini is still talking.
            model_speaking = {"value": False}

            async def audio_reader():
                last_caller = ""
                last_agent = ""
                # Transcription events arrive as INCREMENTAL text chunks:
                #   {"serverContent": {"inputTranscription": {"text": "..."}}
                #    "outputTranscription": {"text": "..."}}
                # There is no isFinal flag — chunks must be buffered per side
                # and flushed as complete lines on turnComplete (and once more
                # when the socket closes).
                pending_caller: list[str] = []
                pending_agent: list[str] = []

                def _flush_transcript_turn() -> None:
                    nonlocal last_caller, last_agent
                    ci = "".join(pending_caller).strip()
                    ai = "".join(pending_agent).strip()
                    pending_caller.clear()
                    pending_agent.clear()
                    if ci and ci != last_caller:
                        last_caller = ci
                        transcript.append(f"Caller: {ci}")
                    if ai and ai != last_agent:
                        last_agent = ai
                        transcript.append(f"Agent: {ai}")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    server = msg.get("serverContent") or {}
                    if msg.get("setupComplete") is not None:
                        if needs_kick:
                            try:
                                await ws.send(
                                    json.dumps(
                                        {
                                            "clientContent": {
                                                "turns": [
                                                    {
                                                        "role": "user",
                                                        "parts": [{"text": "The call has connected."}],
                                                    }
                                                ],
                                                "turnComplete": True,
                                            }
                                        }
                                    )
                                )
                                logger.info("Gemini Live first-turn kick sent")
                            except Exception as exc:
                                logger.warning("Gemini Live first-turn kick failed: {}", exc)
                        continue
                    if server.get("interrupted"):
                        model_speaking["value"] = False
                        await out_q.put(("interrupted", b""))
                        continue
                    # Live transcription: buffer incremental chunks for both
                    # sides; complete lines are written per turn.
                    it = server.get("inputTranscription")
                    if isinstance(it, dict) and (it.get("text") or "").strip():
                        pending_caller.append(it["text"])
                    ot = server.get("outputTranscription")
                    if isinstance(ot, dict) and (ot.get("text") or "").strip():
                        pending_agent.append(ot["text"])
                    turn = server.get("modelTurn") or {}
                    for part in turn.get("parts", []):
                        audio = part.get("inlineData") or part.get("audio") or {}
                        data = audio.get("data")
                        if data:
                            model_speaking["value"] = True
                            await out_q.put(("audio", base64.b64decode(data)))
                    if server.get("turnComplete"):
                        model_speaking["value"] = False
                        _flush_transcript_turn()
                        await out_q.put(("turn_complete", b""))
                # Socket closed mid-turn: flush whatever is still buffered so
                # the last exchange is not lost from the transcript.
                _flush_transcript_turn()

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
            keepalive_task.cancel()
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


async def _finalize_vobiz_call_leg(role: str, camp_id, lead_name, transcript_text: str = "", log_id: str = "") -> None:
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
                await finalize_manual_call_record(camp_id, log_id, duration_sec, analysis)
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
                await finalize_incoming_call_record(camp_id, log_id, duration_sec, analysis)
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

    try:
        from core.state import _CAMPAIGN_DATA, release_phone_slot, release_vobiz_call_slot
        if camp_id and camp_id in _CAMPAIGN_DATA:
            outbound_phone = _CAMPAIGN_DATA[camp_id].get("_outbound_phone", "")
            if outbound_phone:
                release_phone_slot(outbound_phone)
            release_vobiz_call_slot(role)
    except Exception:
        pass

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

    role, opening_pcm, system_text, voice, needs_kick = _resolve_session_context(
        camp_id, agent_id, manual_role, lead_name
    )
    logger.info(
        "Vobiz WS live: camp_id={} role={} lead={} opening_pcm={} needs_kick={}",
        camp_id, role, lead_name, bool(opening_pcm), needs_kick,
    )

    from services.vobiz_bridge.audio import pcm_resample

    stream_id: str | None = None
    inbound_rate = 16000
    stop_evt = asyncio.Event()
    in_q: asyncio.Queue = asyncio.Queue(maxsize=200)
    out_q: asyncio.Queue = asyncio.Queue(maxsize=200)
    transcript: list[str] = []
    gemini_task = asyncio.create_task(
        _run_gemini_live(in_q, out_q, system_text, voice, stop_evt, transcript, needs_kick=needs_kick)
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
    # While the greeting <Play>/<Speak> (or our own PCM playout) is running we
    # must NOT forward caller audio to Gemini — otherwise room noise / the
    # caller's first "hello" lands in the model while the opening is still
    # playing and can trigger a premature overlapping reply. Caller audio is
    # still captured into caller_pcm for the recording.
    caller_gate_until = 0.0
    caller_pcm = bytearray()   # caller speech, 16 kHz PCM16
    agent_pcm = bytearray()    # agent (Gemini) speech, 24 kHz PCM16
    MAX_REC_SEC = 20 * 60
    CALLER_CAP = 16000 * 2 * MAX_REC_SEC
    AGENT_CAP = 24000 * 2 * MAX_REC_SEC
    greeting_task: asyncio.Task | None = None

    async def play_opening_pcm_stream(pcm_raw: bytes, raw_sr: int):
        nonlocal playing
        if not stream_id or not pcm_raw:
            return
        pcm16k, _ = pcm_resample(pcm_raw, int(raw_sr or 24000), 16000)
        chunk_bytes = int(16000 * 2 * 0.04)  # 40 ms = 1280 bytes
        playing = True
        logger.info("Starting paced opening greeting streaming ({} bytes, sr=16000)", len(pcm16k))
        try:
            for i in range(0, len(pcm16k), chunk_bytes):
                if stop_evt.is_set():
                    break
                piece = pcm16k[i : i + chunk_bytes]
                if not piece:
                    break
                if len(agent_pcm) < AGENT_CAP:
                    agent_pcm.extend(piece)
                await websocket.send_text(
                    json.dumps(
                        {
                            "event": "playAudio",
                            "streamId": stream_id,
                            "media": {
                                "contentType": "audio/x-l16",
                                "sampleRate": 16000,
                                "payload": base64.b64encode(piece).decode(),
                            },
                        }
                    )
                )
                await asyncio.sleep(0.038)
        except Exception as exc:
            logger.warning("Opening greeting streaming error: {}", exc)
        finally:
            playing = False
            logger.info("Finished paced opening greeting streaming")

    async def playback_loop():
        nonlocal playing
        buffered = b""
        # Gemini Live streams native 24 kHz PCM, but the Vobiz <Stream> leg is
        # negotiated at 16 kHz. Resample before sending so the caller hears the
        # correct pitch and speed (playing 24 kHz on a 16 kHz clock would sound
        # 1.5× slower and ~7 semitones deeper — i.e. a "male" voice).
        # Flush generated audio as soon as ~120 ms accumulates instead of
        # waiting for the whole turn — this removes the long silence before
        # the agent starts speaking.
        FLUSH_BYTES = 16000 * 2 * 0.12  # 120 ms of PCM16 at 16 kHz
        while not stop_evt.is_set():
            try:
                kind, payload = await asyncio.wait_for(out_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if kind == "interrupted":
                buffered = b""
                if greeting_task and not greeting_task.done():
                    greeting_task.cancel()
                if playing and stream_id:
                    await websocket.send_text(
                        json.dumps({"event": "clearAudio", "streamId": stream_id})
                    )
                playing = False
                continue
            if kind == "audio":
                buffered += payload
                playing = True
                if len(buffered) >= FLUSH_BYTES:
                    pcm16k, _ = pcm_resample(buffered, 24000, 16000)
                    if len(agent_pcm) < AGENT_CAP:
                        agent_pcm.extend(pcm16k)
                    await play_audio(pcm16k, 16000)
                    buffered = b""
                continue
            if kind == "turn_complete":
                if buffered:
                    pcm16k, _ = pcm_resample(buffered, 24000, 16000)
                    if len(agent_pcm) < AGENT_CAP:
                        agent_pcm.extend(pcm16k)
                    await play_audio(pcm16k, 16000)
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
                # Greeting handling: when the answer XML used a Vobiz TTS
                # <Speak>, that already delivered the greeting (so the recorded
                # PCM must NOT play again). When the greeting comes from the
                # pre-recorded Gemini 3.1 Flash PCM (no <Speak>), play it here.
                spoken_by_xml = False
                try:
                    from core.state import _CAMPAIGN_DATA

                    spoken_by_xml = bool(
                        camp_id in _CAMPAIGN_DATA
                        and _CAMPAIGN_DATA[camp_id].get("_greeting_spoken_by_xml")
                    )
                except Exception:
                    pass
                if opening_pcm and not spoken_by_xml:
                    pcm, sr = opening_pcm
                    caller_gate_until = time.monotonic() + (len(pcm) / 2 / max(int(sr or 16000), 1)) + 0.3
                    greeting_task = asyncio.create_task(play_opening_pcm_stream(pcm, sr))
                elif opening_pcm and spoken_by_xml:
                    # The greeting is being played by Vobiz via <Play>. Keep
                    # the recording faithful: seed agent_pcm with the greeting
                    # audio so the saved WAV/MP3 contains the opening. Gate
                    # caller forwarding for the greeting duration so Gemini's
                    # first listen starts on a clean turn.
                    try:
                        pcm, sr = opening_pcm
                        pcm16, _ = pcm_resample(pcm, int(sr or 16000), 16000)
                        if len(agent_pcm) < AGENT_CAP:
                            agent_pcm.extend(pcm16)
                        caller_gate_until = time.monotonic() + (len(pcm16) / 2 / 16000) + 0.3
                        logger.info(
                            "Seeded recording with <Play> greeting ({} bytes, sr=16000, caller gate {:.2f}s)",
                            len(pcm16), len(pcm16) / 2 / 16000 + 0.3,
                        )
                    except Exception as exc:
                        logger.warning("Failed to seed greeting into recording: {}", exc)
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
                if time.monotonic() < caller_gate_until:
                    continue
                try:
                    in_q.put_nowait(pcm)
                except asyncio.QueueFull:
                    pass
            elif ev == "stop":
                break
            # playedStream / clearedAudio are informational; ignored.
    finally:
        if greeting_task and not greeting_task.done():
            greeting_task.cancel()
        playback_task.cancel()
        gemini_task.cancel()
        # Persist a playable recording BEFORE finalize so the row's log_id can
        # point at a real file. The mixed caller+agent 16 kHz WAV is written to
        # CALL_RECORDING_DIR when enabled; log_id = camp_id (unique per call).
        log_id = camp_id or ""
        recording_path = None
        try:
            recording_path = _save_call_recording_wav(
                role, camp_id, bytes(caller_pcm), bytes(agent_pcm)
            )
            if recording_path:
                log_id = camp_id or recording_path.stem
        except Exception as exc:
            logger.warning("Failed to persist call recording for camp_id={}: {}", camp_id, exc)

        try:
            await _finalize_vobiz_call_leg(
                role, camp_id, lead_name, transcript_text="\n".join(transcript),
                log_id=log_id,
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
        # (already written by _finalize_vobiz_call_leg) act as fallback. The
        # task reference is retained with a done-callback so it can never be
        # silently dropped.
        captured = bytes(caller_pcm) + b"" + bytes(agent_pcm)
        if camp_id and len(captured) >= 16000 * 2:  # >= 1 s of audio
            try:
                task = asyncio.create_task(
                    _analyze_and_store_call(
                        role, camp_id, bytes(caller_pcm), bytes(agent_pcm),
                        "\n".join(transcript),
                    )
                )
                _BACKGROUND_TASKS.add(task)
                task.add_done_callback(_BACKGROUND_TASKS.discard)
            except Exception as exc:
                logger.warning("Failed to schedule post-call analysis: {}", exc)


def _save_call_recording_wav(
    role: str, camp_id: str, caller_pcm: bytes, agent_pcm: bytes
):
    """Mix caller + agent into a single 16 kHz mono recording and persist it.

    Writes ``<CALL_RECORDING_DIR>/<role>/<camp_id>.wav`` (for analysis) and an
    MP3 twin ``<CALL_RECORDING_DIR>/<role>/<camp_id>.mp3`` for streaming/playback.
    Returns the MP3 path (falling back to WAV) or None when disabled/empty.
    """
    try:
        from config import settings
        from services.vobiz_bridge.audio import pcm_resample

        if not settings.call_recording_enabled:
            return None
        agent16 = agent_pcm
        total = max(len(caller_pcm), len(agent16))
        if total < 16000 * 2:  # < 1 s of audio — not worth persisting
            return None
        frames = bytearray()
        for i in range(0, total - 1, 2):
            cs = int.from_bytes(caller_pcm[i:i+2], "little", signed=True) if i < len(caller_pcm) - 1 else 0
            as_ = int.from_bytes(agent16[i:i+2], "little", signed=True) if i < len(agent16) - 1 else 0
            s = cs + int(as_ * 0.7)
            frames += int(max(-32768, min(32767, s))).to_bytes(2, "little", signed=True)
        if not frames:
            return None
        base = Path(settings.call_recording_dir)
        out_dir = base / (role or "sales_1")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{camp_id}.wav"
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(bytes(frames))
        logger.info("Saved call recording: {} ({} bytes)", out_path, out_path.stat().st_size)
        mp3_path = out_dir / f"{camp_id}.mp3"
        try:
            _encode_wav_to_mp3(out_path, mp3_path)
            logger.info("Saved call recording MP3: {} ({} bytes)", mp3_path, mp3_path.stat().st_size)
            return mp3_path
        except Exception as exc:
            logger.warning("MP3 encode failed for camp_id={} (keeping WAV): {}", camp_id, exc)
            return out_path
    except Exception as exc:
        logger.warning("Failed to save call recording for camp_id={}: {}", camp_id, exc)
        return None


def _encode_wav_to_mp3(wav_path: Path, mp3_path: Path, bit_rate: int = 96) -> None:
    """Encode a 16 kHz mono WAV to MP3 with lameenc (pure Python wheel)."""
    import lameenc

    with wave.open(str(wav_path), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        data = wf.readframes(wf.getnframes())
    enc = lameenc.Encoder()
    enc.set_bit_rate(bit_rate)
    enc.set_in_sample_rate(rate)
    enc.set_channels(channels)
    enc.set_quality(5)
    mp3 = enc.encode(data) + enc.flush()
    mp3_path.write_bytes(mp3)


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
