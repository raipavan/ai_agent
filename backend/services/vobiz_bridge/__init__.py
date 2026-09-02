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
import time
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
    # Recording is handled at the trunk level (recording=true on the Vobiz trunk).
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
        from config import settings as _settings

        rag_query = (getattr(_settings, "rag_query", "") or "").strip()
        if not rag_query:
            # Derive the KB query from the saved script so retrieval matches the pitch.
            import re as _re

            words = _re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", (prompt or "") + " " + (state.get("rag") or ""))
            seen: set[str] = set()
            keep: list[str] = []
            for w in words:
                low = w.lower()
                if low in seen or low in {"the", "and", "for", "you", "your", "not", "are", "with", "from", "this", "that", "have", "will", "speak", "never", "must", "only", "do", "does", "into", "onto", "about", "what", "when", "where", "which", "they", "them", "their", "there", "then", "than", "say", "said", "ask", "can", "would", "could", "should", "may", "also", "very", "just", "but", "was", "were", "has", "had", "been", "being", "our", "out", "over", "under", "again", "further", "once", "here", "each", "few", "more", "most", "other", "some", "such", "own", "same", "so", "than", "too", "very", "call", "calls", "customer", "customers", "answer", "questions", "question"}:
                    continue
                seen.add(low)
                keep.append(w)
                if len(keep) >= 12:
                    break
            rag_query = " ".join(keep)
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
        try:
            from core.state import resolved_greeting_text

            spoken_greeting = resolved_greeting_text(role).strip()
        except Exception:
            spoken_greeting = ""
        if not spoken_greeting:
            spoken_greeting = "Namaste! Priya speaking from Lila Decor."
        system_text = (
            "CRITICAL CALL CONTEXT: The phone system has ALREADY delivered the opening greeting to the caller: "
            f"\"{spoken_greeting}\" "
            "You are in MID-CONVERSATION. Do NOT repeat the greeting and do NOT introduce yourself again. "
            "Continue the conversation naturally exactly as your system prompt instructs: acknowledge the caller's "
            "reply, deliver your pitch, and follow the conversation flow below. "
            "Listen to what the caller says and respond directly, helpfully, and conversationally in the caller's language.\n\n"
            + system_text
        )
        needs_kick = False
    else:
        needs_kick = False

    try:
        from config import settings as _settings

        per_role_voice = getattr(_settings, f"gemini_live_voice_{role}", "") or ""
        voice = (per_role_voice or _settings.gemini_live_voice or "Leda").strip()
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
                        await out_q.put(("interrupted", b"", 0))
                        continue
                    # DEBUG: log only the transcription sub-objects (any
                    # shape) so we can verify what this model version emits —
                    # without dumping base64 audio payloads.
                    if "inputTranscription" in server or "outputTranscription" in server:
                        try:
                            _tdbg = {
                                k: server[k]
                                for k in ("inputTranscription", "outputTranscription")
                                if k in server
                            }
                            logger.info("TRANSCRIPT EVT: {}", json.dumps(_tdbg)[:300])
                        except Exception:
                            pass
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
                            # Trust the mimeType's actual rate — never hardcode
                            # 24000. A wrong assumed rate resamples at the wrong
                            # ratio and produces metallic/robotic voice.
                            mt = str(audio.get("mimeType") or "")
                            rate = 24000
                            if "rate=" in mt:
                                try:
                                    rate = int(mt.split("rate=", 1)[1].split(";")[0])
                                except Exception:
                                    pass
                            model_speaking["value"] = True
                            await out_q.put(("audio", base64.b64decode(data), rate))
                    if server.get("turnComplete"):
                        model_speaking["value"] = False
                        _flush_transcript_turn()
                        await out_q.put(("turn_complete", b"", 0))
                # Socket closed mid-turn: flush whatever is still buffered so
                # the last exchange is not lost from the transcript.
                _flush_transcript_turn()

            async def audio_sender():
                # Hybrid VAD (echo-proof): client-side energy gate on the
                # caller's audio. After REAL caller speech is detected,
                # ~HYBRID_END_SILENCE_MS of sustained silence triggers
                # realtimeInput.audioStreamEnd so Gemini finalizes the turn
                # IMMEDIATELY instead of waiting for its server-side timer.
                #
                # Echo/noise immunity: the gate is BLIND while Gemini's own
                # voice is playing (+ a short tail guard) — any energy on the
                # caller leg then is line echo of our agent audio, not speech —
                # and arming requires SUSTAINED energy so single noise bursts
                # cannot trigger it. Exactly ONE audioStreamEnd per armed
                # segment; no time-debounce, so real turns keep zero extra
                # latency.
                import struct as _struct

                hybrid_enabled = bool(getattr(settings, "gemini_live_hybrid_vad_enabled", True))
                hybrid_end_ms = max(
                    int(getattr(settings, "gemini_live_hybrid_end_silence_ms", 300) or 300), 50
                )
                ENERGY_RMS = max(
                    float(getattr(settings, "gemini_live_hybrid_energy_threshold", 600) or 600), 50.0
                )  # int16 RMS threshold: room noise << this < speech
                ARM_CHUNKS = 3              # ~120 ms sustained energy required to arm speech
                AGENT_ECHO_GUARD_S = 0.25   # gate stays blind this long after agent audio stops
                speaking = False
                silence_ms = 0.0
                arm_run = 0
                last_agent_active_ts = 0.0

                async def _send_audio_stream_end() -> None:
                    await ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))

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
                    if not hybrid_enabled:
                        continue
                    # ── Hybrid VAD energy gate (16 kHz PCM16 mono) ──
                    try:
                        now = time.monotonic()
                        if model_speaking["value"]:
                            # Agent voice playing → caller-leg energy is echo.
                            last_agent_active_ts = now
                            speaking = False
                            arm_run = 0
                            silence_ms = 0.0
                            continue
                        if last_agent_active_ts and (now - last_agent_active_ts) < AGENT_ECHO_GUARD_S:
                            speaking = False
                            arm_run = 0
                            silence_ms = 0.0
                            continue
                        n = len(chunk) // 2
                        if n > 0:
                            samples = _struct.unpack("<" + str(n) + "h", chunk[: n * 2])
                            rms = (sum(s * s for s in samples) / n) ** 0.5
                        else:
                            rms = 0.0
                        chunk_ms = (len(chunk) / 2) / 16.0  # bytes → samples → ms @16 kHz
                        if rms > ENERGY_RMS:
                            arm_run += 1
                            silence_ms = 0.0
                            if not speaking and arm_run >= ARM_CHUNKS:
                                speaking = True
                                logger.info("Hybrid VAD: caller speech detected")
                        elif speaking:
                            silence_ms += chunk_ms
                            if silence_ms >= hybrid_end_ms:
                                await _send_audio_stream_end()
                                logger.info(
                                    "Hybrid VAD: {:.0f} ms caller silence after speech -> audioStreamEnd sent",
                                    silence_ms,
                                )
                                speaking = False
                                silence_ms = 0.0
                    except Exception as _vad_exc:
                        logger.debug("Hybrid VAD gate error (non-fatal): {}", _vad_exc)

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
    in_q: asyncio.Queue = asyncio.Queue(maxsize=400)
    out_q: asyncio.Queue = asyncio.Queue(maxsize=400)
    # Live audio-quality metrics for this call: packet gaps on the Vobiz leg,
    # caller chunks dropped under backpressure, last media arrival time.
    _metrics = {"gaps": 0, "in_drops": 0, "last_media_ts": 0.0}
    transcript: list[str] = []
    gemini_task = asyncio.create_task(
        _run_gemini_live(in_q, out_q, system_text, voice, stop_evt, transcript, needs_kick=needs_kick)
    )

    async def play_audio(pcm: bytes, rate: int):
        if not stream_id or not pcm:
            return
        chunk_bytes = int(rate * 2 * 0.04)  # 40 ms frames
        _play_t0 = time.monotonic()
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
            # REALTIME PACING: wall-clock timing ensures each 40 ms frame
            # is delivered at exactly realtime intervals. Fixed 38ms sleep
            # ignores time spent encoding JSON and sending WebSocket frames,
            # causing burst delivery and metallic/robotic artifacts.
            _play_elapsed = time.monotonic() - _play_t0
            if _play_elapsed < 0.040:
                await asyncio.sleep(0.040 - _play_elapsed)
            _play_t0 = time.monotonic()

    playing = False
    # While the greeting <Play>/<Speak> (or our own PCM playout) is running we
    # must NOT forward caller audio to Gemini — otherwise room noise / the
    # caller's first "hello" lands in the model while the opening is still
    # playing and can trigger a premature overlapping reply.
    caller_gate_until = 0.0
    media_count = 0
    gated_count = 0
    forwarded_count = 0
    greeting_task: asyncio.Task | None = None
    # First-response latency probes: when the first caller frame is forwarded
    # to Gemini (after the greeting gate) and when the first agent audio chunk
    # comes back. The delta is logged once per call as ms.
    _lat = {"fwd_first": 0.0, "audio_first": 0.0}

    async def play_opening_pcm_stream(pcm_raw: bytes, raw_sr: int):
        nonlocal playing
        if not stream_id or not pcm_raw:
            return
        pcm16k, _ = pcm_resample(pcm_raw, int(raw_sr or 24000), 16000)
        chunk_bytes = int(16000 * 2 * 0.04)  # 40 ms = 1280 bytes
        _greet_t0 = time.monotonic()
        playing = True
        logger.info("Starting paced opening greeting streaming ({} bytes, sr=16000)", len(pcm16k))
        try:
            for i in range(0, len(pcm16k), chunk_bytes):
                if stop_evt.is_set():
                    break
                piece = pcm16k[i : i + chunk_bytes]
                if not piece:
                    break
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
                _greet_elapsed = time.monotonic() - _greet_t0
                if _greet_elapsed < 0.040:
                    await asyncio.sleep(0.040 - _greet_elapsed)
                _greet_t0 = time.monotonic()
        except Exception as exc:
            logger.warning("Opening greeting streaming error: {}", exc)
        finally:
            playing = False
            logger.info("Finished paced opening greeting streaming")

    async def playback_loop():
        nonlocal playing
        buffered = b""
        buffered_rate = 24000
        # Gemini Live streams native PCM (rate taken from each part's
        # mimeType), but the Vobiz <Stream> leg is negotiated at 16 kHz.
        # pcm_resample() is now an anti-aliased polyphase FIR — linear
        # interpolation aliasing was a major source of metallic artifacts.
        #
        # Frame discipline: flush at ~100 ms (80–120 ms band) and emit EXACT
        # 40 ms frames, carrying the ragged remainder across flushes so Vobiz
        # receives uniform packets (irregular tails every flush caused clicks).
        FLUSH_BYTES = int(16000 * 2 * 0.100)   # 100 ms of PCM16 at 16 kHz
        FRAME_BYTES = int(16000 * 2 * 0.040)   # exact 40 ms outbound frames
        carry = bytearray()                    # sub-frame remainder for playback

        async def _emit(pcm16k: bytes) -> None:
            nonlocal playing
            await play_audio(pcm16k, 16000)
            playing = True

        # CONTINUOUS RESAMPLING CONTEXT: resampling every flush independently
        # makes the polyphase FIR treat chunk edges as zeros — injecting a
        # tiny discontinuity EVERY flush (~every 100 ms) that is heard as a
        # metallic/buzzy overlay across the whole call (worst right after the
        # greeting, when speech actually starts). Fix: prepend the last few ms
        # of raw source audio to each flush (filter left-context) and drop the
        # leading output samples that context produced. The emitted stream is
        # then identical to one infinite-buffer resample.
        CTX_BYTES = (int(24000 * 2 * 0.004) // 2) * 2  # 4 ms, enforced even for 16-bit PCM alignment
        prev_tail = b""

        def _resample_contiguous() -> bytes:
            nonlocal buffered, prev_tail
            combined = prev_tail + buffered
            out, _ = pcm_resample(combined, buffered_rate, 16000)
            if prev_tail:
                # Output bytes attributable to the prepended context.
                drop = (len(prev_tail) * 16000 * 2) // max(buffered_rate * 2, 1)
                drop = (drop // 2) * 2  # enforce even-byte 16-bit PCM alignment
                if 0 < drop < len(out):
                    out = out[drop:]
            prev_tail = combined[-CTX_BYTES:]
            buffered = b""
            return out

        while not stop_evt.is_set():
            try:
                kind, payload, payload_rate = await asyncio.wait_for(out_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if kind == "interrupted":
                buffered = b""
                prev_tail = b""
                carry.clear()
                if greeting_task and not greeting_task.done():
                    greeting_task.cancel()
                if playing and stream_id:
                    await websocket.send_text(
                        json.dumps({"event": "clearAudio", "streamId": stream_id})
                    )
                playing = False
                continue
            if kind == "audio":
                if not _lat["audio_first"] and _lat["fwd_first"]:
                    _lat["audio_first"] = time.monotonic()
                    logger.info(
                        "First response latency: {:.0f} ms (first caller frame forwarded -> first agent audio back)",
                        (_lat["audio_first"] - _lat["fwd_first"]) * 1000,
                    )
                if payload_rate:
                    buffered_rate = int(payload_rate)
                buffered += payload
                if len(buffered) >= FLUSH_BYTES:
                    carry.extend(_resample_contiguous())
                    while len(carry) >= FRAME_BYTES:
                        await _emit(bytes(carry[:FRAME_BYTES]))
                        del carry[:FRAME_BYTES]
                continue
            if kind == "turn_complete":
                if buffered:
                    carry.extend(_resample_contiguous())
                if carry:
                    await _emit(bytes(carry))
                    carry.clear()
                prev_tail = b""
                if stream_id:
                    await websocket.send_text(
                        json.dumps({"event": "checkpoint", "streamId": stream_id, "name": f"t{int(time.time()*1000)}"})
                    )
                playing = False

    playback_task = asyncio.create_task(playback_loop())

    async def _metrics_loop():
        # Periodic audio-quality snapshot for monitoring/verification.
        try:
            while not stop_evt.is_set():
                await asyncio.sleep(10)
                logger.info(
                    "Audio metrics camp_id={}: media={} gaps={} in_drops={} in_qsize={} out_qsize={}",
                    camp_id, media_count, _metrics["gaps"], _metrics["in_drops"],
                    in_q.qsize(), out_q.qsize(),
                )
        except asyncio.CancelledError:
            pass

    metrics_task = asyncio.create_task(_metrics_loop())

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
                fmt_ct = str(fmt.get("contentType") or fmt.get("encoding") or "")
                fmt_ch = int(fmt.get("channels") or 1)
                logger.info(
                    "Vobiz stream started: stream_id={} rate={} contentType={!r} channels={} (full mediaFormat={})",
                    stream_id, inbound_rate, fmt_ct, fmt_ch, fmt,
                )
                # Validate the negotiated format — drift here silently corrupts
                # every decoded packet downstream.
                if fmt_ct and "l16" not in fmt_ct.lower():
                    logger.warning(
                        "Vobiz contentType {!r} is NOT audio/x-l16 — decode assumptions invalid!",
                        fmt_ct,
                    )
                if "16000" not in fmt_ct and inbound_rate != 16000:
                    logger.warning(
                        "Vobiz sampleRate {} != 16000 — caller frames will be resampled",
                        inbound_rate,
                    )
                if fmt_ch != 1:
                    logger.warning(
                        "Vobiz reports {} channels — pipeline assumes mono; interleaving will sound metallic",
                        fmt_ch,
                    )
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
                # The pre-recorded greeting is NOT Gemini output, so no
                # outputTranscription event exists for it — add its line to the
                # live transcript manually so the UI shows the greeting.
                try:
                    from core.state import resolved_greeting_text

                    _greeting_line = (resolved_greeting_text(role) or "").strip()
                    if _greeting_line and opening_pcm:
                        transcript.insert(0, f"Agent: {_greeting_line}")
                except Exception as _gt_exc:
                    logger.warning("Failed to record greeting transcript line: {}", _gt_exc)
                if opening_pcm and not spoken_by_xml:
                    pcm, sr = opening_pcm
                    caller_gate_until = time.monotonic() + (len(pcm) / 2 / max(int(sr or 16000), 1)) + 0.3
                    greeting_task = asyncio.create_task(play_opening_pcm_stream(pcm, sr))
                elif opening_pcm and spoken_by_xml:
                    # Vobiz ignores <Play> after <Stream>, so the bridge itself
                    # delivers the pre-recorded greeting: stream the cached PCM
                    # to the caller immediately (the WS is already up) and gate
                    # caller forwarding for the greeting duration so Gemini's
                    # first listen starts on a clean turn.
                    # play_opening_pcm_stream() resamples to 16 kHz.
                    pcm, sr = opening_pcm
                    raw_sr = max(int(sr or 24000), 1)
                    gate_secs = len(pcm) / 2 / raw_sr + 0.3
                    caller_gate_until = time.monotonic() + gate_secs
                    greeting_task = asyncio.create_task(play_opening_pcm_stream(pcm, raw_sr))
                    logger.info(
                        "Playing pre-recorded greeting via WS ({} bytes @ {} Hz, gate {:.2f}s)",
                        len(pcm), raw_sr, gate_secs,
                    )
            elif ev == "media":
                media = msg.get("media") or {}
                payload = media.get("payload")
                if not payload:
                    continue
                pcm = base64.b64decode(payload)
                if inbound_rate != 16000:
                    pcm, _ = pcm_resample(pcm, inbound_rate, 16000)
                # Enforce even-byte alignment for 16-bit PCM samples
                if len(pcm) % 2 != 0:
                    pcm = pcm[:-1]
                media_count += 1
                # Packet-gap detection: Vobiz streams ~20-40 ms frames back to
                # back; a wall-clock jump means packets were lost upstream.
                _now = time.monotonic()
                if media_count > 1:
                    delta = _now - _metrics["last_media_ts"]
                    if delta > 0.15:
                        _metrics["gaps"] += 1
                        if _metrics["gaps"] <= 5 or _metrics["gaps"] % 25 == 0:
                            logger.warning(
                                "Vobiz media gap: {:.0f} ms silence jump (gap #{}) camp_id={}",
                                delta * 1000, _metrics["gaps"], camp_id,
                            )
                _metrics["last_media_ts"] = _now
                if time.monotonic() < caller_gate_until:
                    gated_count += 1
                    continue
                forwarded_count += 1
                if not _lat["fwd_first"]:
                    _lat["fwd_first"] = time.monotonic()
                # Never silently drop caller audio: a dropped chunk is a hole
                # in what Gemini hears (robotic turns). Wait briefly instead.
                try:
                    await asyncio.wait_for(in_q.put(pcm), timeout=0.05)
                except asyncio.TimeoutError:
                    _metrics["in_drops"] += 1
                    if _metrics["in_drops"] == 1 or _metrics["in_drops"] % 50 == 0:
                        logger.warning(
                            "in_q saturated — {} caller chunks dropped total camp_id={}",
                            _metrics["in_drops"], camp_id,
                        )
            elif ev == "stop":
                break
            # playedStream / clearedAudio are informational; ignored.
    finally:
        if greeting_task and not greeting_task.done():
            greeting_task.cancel()
        metrics_task.cancel()
        playback_task.cancel()
        gemini_task.cancel()
        stop_evt.set()
        for task in (gemini_task, playback_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(gemini_task, playback_task, return_exceptions=True)

        # Recordings come exclusively from Vobiz server-side telephony
        # recording (record=true in make_vobiz_call). The finished MP3 is
        # downloaded via /vobiz/recording-callback — no local mix needed.
        log_id = camp_id or ""

        try:
            await _finalize_vobiz_call_leg(
                role, camp_id, lead_name, transcript_text="\n".join(transcript),
                log_id=log_id,
            )
        except Exception as exc:
            logger.warning(
                "Vobiz call finalize raised for camp_id={}: {}", camp_id, exc
            )
        try:
            await websocket.close(code=1000)
        except Exception:
            pass
        logger.info(
            "Vobiz WS live ended for camp_id={} (media={} gated={} forwarded_to_gemini={} live_transcript_lines={})",
            camp_id, media_count, gated_count, forwarded_count,
            len(transcript),
        )

        # Post-call analysis via live transcription in the background.
        # The task reference is retained with a done-callback so it can
        # never be silently dropped.
        if camp_id and "\n".join(transcript).strip():
            try:
                task = asyncio.create_task(
                    _analyze_and_store_call(
                        role, camp_id, "\n".join(transcript),
                    )
                )
                _BACKGROUND_TASKS.add(task)
                task.add_done_callback(_BACKGROUND_TASKS.discard)
            except Exception as exc:
                logger.warning("Failed to schedule post-call analysis: {}", exc)


async def _analyze_and_store_call(
    role: str, camp_id: str, live_transcript: str
) -> None:
    """Post-call analysis using Gemini's live transcription.

    Extracts sentiment/disposition from the transcript and persists into the
    manual/incoming call row. Recordings come from Vobiz server-side only.
    """
    from core.storage import _get_conn

    analysis: dict = {}
    try:
        if live_transcript.strip():
            from services.call_analyzer import analyze_call_transcript
            analysis = await analyze_call_transcript(live_transcript)
    except Exception as exc:
        logger.warning("Post-call text analysis failed for {}: {}", camp_id, exc)

    if not analysis:
        logger.info("Post-call analysis empty for {} — nothing stored", camp_id)
        return

    analysis["transcript"] = live_transcript

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
