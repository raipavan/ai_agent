"""Vobiz answer URL + incoming call webhook + media WebSocket."""

from __future__ import annotations

import re
import asyncio
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Request, Response, WebSocket
from fastapi.responses import StreamingResponse
from loguru import logger

from config import settings
from core.outbound_numbers import build_phone_to_role_map
from core.state import (
    role_has_active_vobiz_call,
    _CAMPAIGN_DATA,
    _CAMPAIGN_TASKS,
    _ACTIVE_VOBIZ_CALLS_BY_ROLE,
    acquire_vobiz_call_slot,
    release_vobiz_call_slot,
    acquire_phone_slot,
    release_phone_slot,
    get_state,
    normalize_console_role,
    parse_manual_camp_role_suffix,
)
from core.storage import find_lead_by_phone, insert_incoming_call
from services.vobiz_bridge import (
    build_answer_xml,
    build_busy_message_xml,
    build_incoming_stream_xml,
    handle_vobiz_ws_live,
)

router = APIRouter(tags=["vobiz"])


def _build_busy_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Reject reason="busy"/></Response>'
    )


async def _vobiz_answer_impl(
    camp_id: Optional[str] = None,
    role: Optional[str] = None,
    request: Optional[Request] = None,
) -> Response:
    normalized_role = normalize_console_role(role) if role else None

    # If camp_id is in _CAMPAIGN_DATA, WE initiated this call (manual or campaign).
    # The slot was already acquired by the campaign worker before dial — skip the
    # busy check so the campaign's own answer URL doesn't block its own call.
    known_call = bool(camp_id and camp_id in _CAMPAIGN_DATA)

    is_busy = False
    if normalized_role and not known_call:
        if role_has_active_vobiz_call(normalized_role):
            is_busy = True

    if is_busy:
        return Response(content=_build_busy_xml(), media_type="application/xml")

    role_base = None
    if camp_id and camp_id in _CAMPAIGN_DATA:
        camp_role = _CAMPAIGN_DATA[camp_id].get("_role")
        if camp_role:
            state = get_state(camp_role)
            role_base = state.get("vobiz", {}).get("public_url")
    elif normalized_role:
        try:
            state = get_state(normalized_role)
            role_base = state.get("vobiz", {}).get("public_url")
        except Exception:
            role_base = None

    # Resolve the stream (WebSocket) base URL.
    # Priority: explicit stream URL > request Host header (auto-detect) > server_url > public callback URL.
    explicit_stream = (settings.vobiz_stream_public_base_url or "").strip().rstrip("/")
    wss_base = explicit_stream
    if not wss_base and request is not None:
        try:
            host_header = request.headers.get("host", "")
            if host_header:
                scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
                wss_base = f"{scheme}://{host_header}"
        except Exception:
            pass
    if not wss_base:
        wss_base = settings.server_url.rstrip("/")
    if not wss_base:
        wss_base = (role_base or settings.vobiz_public_base_url or "").rstrip("/")

    use_ws = False
    if request is not None:
        try:
            use_ws = request.query_params.get("use_ws", "").strip().lower() in ("1", "true", "yes")
        except Exception:
            pass

    if use_ws:
        wss_url = wss_base.replace("https://", "http://").replace("wss://", "ws://").replace("http://", "ws://") + "/ws/vobiz"
    else:
        wss_url = wss_base.replace("https://", "wss://").replace("http://", "ws://") + "/ws/vobiz"

    params = []
    agent_id = None
    resolved_manual_role = None
    if camp_id:
        params.append(f"camp_id={camp_id}")
        if camp_id in _CAMPAIGN_DATA:
            agent_id = _CAMPAIGN_DATA[camp_id].get("_agent_id")
        elif camp_id.startswith("sandbox-"):
            parts = camp_id.split("-")
            if len(parts) >= 2:
                agent_id = parts[1]

    if agent_id:
        params.append(f"agent_id={agent_id}")

    if camp_id and str(camp_id).startswith("manual_"):
        suffix = str(camp_id)[len("manual_") :]
        mr, _ = parse_manual_camp_role_suffix(suffix)
        if mr:
            resolved_manual_role = mr
    elif normalized_role:
        resolved_manual_role = normalized_role

    if resolved_manual_role:
        params.append(f"manual_role={quote(resolved_manual_role, safe='')}")

    if use_ws:
        params.append("use_ws=true")

    if params:
        wss_url += "?" + "&".join(params)

    if wss_base and (
        "trycloudflare.com" in wss_base
        or "trycloudflare.dev" in wss_base
        or "cfargotunnel.com" in wss_base
    ):
        logger.warning(
            "Vobiz <Stream> URL uses a Cloudflare quick-tunnel host ({}…). "
            "For stable calls set VOBIZ_STREAM_PUBLIC_BASE_URL to your VPS "
            "http://IP:PORT (same FastAPI server).",
            wss_base.split("//")[-1][:48],
        )

    if request is not None:
        try:
            logger.info(
                "Vobiz answer: method={} qs={}",
                request.method, dict(request.query_params),
            )
        except Exception:
            pass

    logger.info(
        "Vobiz answer: camp={} role={} wss_url={}",
        camp_id,
        normalized_role,
        wss_url,
    )
    # Greeting source selection:
    #   * GEMINI_LIVE_FIRST_OPENING on  → Gemini Live speaks the greeting itself
    #     (single consistent voice). No <Speak>, no recorded PCM.
    #   * A pre-recorded greeting PCM was primed (generated from Gemini 3.1
    #     Flash) → play that PCM on the media leg, skip Vobiz TTS <Speak>, and
    #     tell Gemini the greeting was already spoken so it doesn't repeat it.
    #   * Otherwise → speak the role greeting instantly via Vobiz TTS <Speak>
    #     (zero Gemini latency) and tell Gemini not to repeat it.
    greeting_text = ""
    play_url = ""
    try:
        gemini_first = bool(settings.gemini_live_first_opening)
    except Exception:
        gemini_first = False

    opening_primed = bool(
        camp_id and camp_id in _CAMPAIGN_DATA and _CAMPAIGN_DATA[camp_id].get("opening_pcm")
    )
    if gemini_first:
        # Model will speak the greeting via first-turn kick. Mark as spoken
        # so the PCM is NOT played and we avoid double greeting.
        if camp_id and camp_id in _CAMPAIGN_DATA:
            _CAMPAIGN_DATA[camp_id]["_greeting_spoken"] = True
            _CAMPAIGN_DATA[camp_id]["_greeting_spoken_by_xml"] = False
    if not gemini_first and camp_id and camp_id in _CAMPAIGN_DATA:
        if opening_primed:
            # Pre-recorded Gemini 3.1 Flash greeting. Vobiz treats <Stream> as
            # a TERMINAL verb — a <Play> after it never executes (verified on
            # calls: no GET /vobiz/greeting/*.wav when Stream came first), so
            # the XML carries ONLY <Stream> (connects instantly at pickup) and
            # the WS bridge itself streams the pre-recorded PCM to the caller
            # the moment the socket is up. Flags below keep the prompt logic
            # (Gemini must NOT re-speak the greeting) and recording seeding.
            _CAMPAIGN_DATA[camp_id]["_greeting_spoken"] = True
            _CAMPAIGN_DATA[camp_id]["_greeting_spoken_by_xml"] = True
        else:
            # Fallback: speak via Vobiz TTS <Speak>.
            try:
                from core.state import resolved_greeting_text

                g_role = normalized_role or resolved_manual_role or "sales_1"
                greeting_text = resolved_greeting_text(g_role)
                _CAMPAIGN_DATA[camp_id]["_greeting_spoken"] = True
                _CAMPAIGN_DATA[camp_id]["_greeting_spoken_by_xml"] = True
            except Exception as ge:
                logger.warning("Failed to resolve greeting for role={}: {}", normalized_role, ge)

    xml_content = build_answer_xml(
        wss_url,
        greeting_text=greeting_text,
        play_url=play_url,
        status_callback_url=(
            (settings.vobiz_public_base_url or "").rstrip("/") + "/vobiz/stream-status"
            if settings.vobiz_public_base_url
            else ""
        ),
    )
    logger.info("Vobiz answer returned XML: {}", xml_content)
    return Response(
        content=xml_content,
        media_type="application/xml",
    )


@router.post("/vobiz/answer")
async def vobiz_answer_post(request: Request, camp_id: Optional[str] = None, role: Optional[str] = None):
    return await _vobiz_answer_impl(camp_id=camp_id, role=role, request=request)


@router.get("/vobiz/answer")
async def vobiz_answer_get(request: Request, camp_id: Optional[str] = None, role: Optional[str] = None):
    return await _vobiz_answer_impl(camp_id=camp_id, role=role, request=request)


@router.websocket("/ws/vobiz")
async def vobiz_ws_endpoint(
    websocket: WebSocket,
    camp_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    manual_role: Optional[str] = None,
    lead_name: Optional[str] = None,
):
    logger.info(
        "Vobiz WS connect: camp_id={} agent_id={} manual_role={} lead_name={}",
        camp_id, agent_id, manual_role, lead_name,
    )
    await handle_vobiz_ws_live(
        websocket,
        camp_id=camp_id,
        agent_id=agent_id,
        manual_role=manual_role,
        lead_name=lead_name,
    )


@router.post("/vobiz/incoming")
async def vobiz_incoming(request: Request):
    """
    Vobiz Application answer URL for incoming calls.
    Called when someone dials one of our phone numbers.
    """
    try:
        form = await request.form()
    except Exception:
        form = {}
    from_num = str(form.get("From") or form.get("from") or request.query_params.get("From", "")).strip()
    to_num = str(form.get("To") or form.get("to") or request.query_params.get("To", "")).strip()
    caller_id = str(form.get("CallUUID") or "").strip()

    logger.info("Vobiz incoming call: CallUUID={} From={} To={}", caller_id, from_num, to_num)

    # Determine role from the dialed number
    phone_map = build_phone_to_role_map()
    to_digits = re.sub(r"\D", "", to_num)
    role = phone_map.get(to_digits, "")

    if not role:
        logger.warning("Incoming call to unknown number To={} (digits={})", to_num, to_digits)
        return Response(
            content=build_busy_message_xml(
                "માફ કરશો, આ નંબર ઓળખાયો નથી. કૃપા કરીને પછીથી ફરી પ્રયાસ કરો. Sorry, this number is not recognized."
            ),
            media_type="application/xml",
        )

    role = normalize_console_role(role)
    logger.info("Incoming call routed to role={} (dialed number {})", role, to_num)

    # Check if role is busy with an active campaign call
    campaign_task = _CAMPAIGN_TASKS.get(role)
    campaign_active = bool(campaign_task and not campaign_task.done())
    if role_has_active_vobiz_call(role) or campaign_active:
        # Queue the call instead of rejecting
        from core.state import enqueue_inbound_call, get_inbound_queue_length
        queue_position = enqueue_inbound_call(
            role=role,
            from_num=from_num,
            from_digits=from_digits,
            lead_name=lead_name,
            call_uuid=caller_id,
        )
        logger.info("Role={} busy, queued inbound call from={} at position={}", role, from_num, queue_position)
        return Response(
            content=build_busy_message_xml(
                f"કૉલબેક માટે આભાર. હું હાલમાં બીજા કૉલ પર છું. "
                f"તમારો ઓવર કૉલ {queue_position} માં છે. જલ્દી માં કૉલ કરીશ. "
                f"Thank you for calling back. You are position {queue_position} in queue. I will call you shortly."
            ),
            media_type="application/xml",
        )

    # Look up caller as a known lead — try the resolved role first, then cross-role
    lead = await find_lead_by_phone(role, from_num)
    if not lead:
        # If role is a fallback default, try other roles too
        for alt_role in ("sales_1", "sales_2"):
            if alt_role == role:
                continue
            lead = await find_lead_by_phone(alt_role, from_num)
            if lead:
                role = alt_role
                logger.info("Incoming call matched to role={} from cross-role lead lookup", role)
                break
    lead_name = (lead or {}).get("name", "") if lead else ""

    # Build WebSocket URL with incoming camp_id format
    from_digits = re.sub(r"\D", "", from_num) if from_num else "unknown"
    explicit_stream = (settings.vobiz_stream_public_base_url or "").strip().rstrip("/")
    wss_base = explicit_stream
    if not wss_base:
        try:
            host_header = request.headers.get("host", "")
            if host_header:
                scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
                wss_base = f"{scheme}://{host_header}"
        except Exception:
            pass
    if not wss_base:
        wss_base = settings.server_url.rstrip("/")
    if not wss_base:
        wss_base = (settings.vobiz_public_base_url or "").rstrip("/")
    wss_url = wss_base.replace("https://", "wss://").replace("http://", "ws://") + "/ws/vobiz"
    camp_id = f"incoming_{role}_from_{from_digits}"
    wss_url += f"?camp_id={quote(camp_id, safe='')}"
    if lead_name:
        wss_url += f"&lead_name={quote(lead_name, safe='')}"

    if wss_base and (
        "trycloudflare.com" in wss_base
        or "trycloudflare.dev" in wss_base
        or "cfargotunnel.com" in wss_base
    ):
        logger.warning(
            "Incoming call WSS uses a Cloudflare quick-tunnel host ({}…). "
            "For stable calls set VOBIZ_STREAM_PUBLIC_BASE_URL to the VPS http://IP:PORT.",
            wss_base.split("//")[-1][:48],
        )

    # Acquire a Vobiz call slot so the role is marked busy during this incoming call
    acquire_vobiz_call_slot(role)

    # Create a persistent record in the incoming_calls table
    try:
        await insert_incoming_call(role, camp_id, from_num, lead_name or "")
    except Exception as e:
        logger.warning("Failed to insert incoming call record: {}", e)

    try:
        from core.notifications import push_notification

        push_notification(
            role,
            "Incoming call",
            f"{from_num} ({lead_name or 'Unknown caller'})",
            kind="call",
        )
    except Exception as ne:
        logger.warning("Failed to push incoming-call notification: {}", ne)

    logger.info("Incoming call: routing to wss_url={} lead={}", wss_url, lead_name or "unknown")

    # Speak the role greeting instantly via Vobiz TTS so the caller hears the
    # agent immediately (no Gemini first-token latency), then stream for the
    # live conversation. Gemini is told not to repeat the greeting.
    greeting_text = ""
    try:
        from core.state import resolved_greeting_text

        greeting_text = resolved_greeting_text(role)
    except Exception as ge:
        logger.warning("Failed to resolve greeting for inbound role={}: {}", role, ge)

    return Response(
        content=build_incoming_stream_xml(wss_url, greeting_text=greeting_text),
        media_type="application/xml",
    )


@router.get("/vobiz/greeting/{role}.wav")
async def vobiz_greeting_wav(role: str):
    """Serve the pre-recorded greeting PCM as a 16 kHz mono WAV so Vobiz can
    play it instantly via the <Play> verb in the answer XML (no WS latency)."""
    try:
        from core.greeting_pcm import load_recorded_greeting_pcm
        from services.vobiz_bridge.audio import pcm_resample
        import io
        import wave

        pcm, sr = load_recorded_greeting_pcm(role) or (b"", 16000)
        if not pcm:
            return Response(content=b"", media_type="audio/x-wav", status_code=404)
        pcm16, _ = pcm_resample(pcm, int(sr or 16000), 16000)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm16)
        return Response(content=buf.getvalue(), media_type="audio/x-wav")
    except Exception as exc:
        logger.warning("Failed to serve greeting WAV for role={}: {}", role, exc)
        return Response(content=b"", media_type="audio/x-wav", status_code=500)


@router.post("/vobiz/recording-callback")
async def vobiz_recording_callback(request: Request, camp_id: Optional[str] = None):
    """Vobiz posts the finished telephony recording URL here (``record=true``).

    The file is downloaded and stored OVER the local WS mix so the UI serves
    the canonical Vobiz-side recording. The local mix is kept as fallback.
    """
    data: dict = {}
    try:
        body = await request.json()
        if isinstance(body, dict):
            data = body
    except Exception:
        try:
            form = await request.form()
            data = dict(form)
        except Exception:
            data = {}
    qp = request.query_params
    rec_url = (
        data.get("RecordingUrl")
        or data.get("recording_url")
        or qp.get("RecordingUrl")
        or ""
    ).strip()
    cid = (
        (camp_id or "").strip()
        or str(data.get("camp_id") or "").strip()
        or (qp.get("camp_id") or "").strip()
    )
    call_uuid = str(data.get("CallUUID") or "").strip()
    if not rec_url or not cid:
        logger.warning(
            "Vobiz recording callback missing fields: has_url={} camp_id={!r} keys={}",
            bool(rec_url), cid, list(data)[:8],
        )
        return {"ok": False}

    import httpx
    import pathlib

    role = normalize_console_role((_CAMPAIGN_DATA.get(cid) or {}).get("_role") or "")
    base = pathlib.Path(settings.call_recording_dir)
    campaign_base = pathlib.Path(settings.campaign_recording_dir)
    target_dir = base / (role or "sales_1")

    # Search both campaign and manual recording trees for existing local mix
    search_dirs = [
        campaign_base / (role or "sales_1"),
        base / (role or "sales_1") / "manual",
        base / (role or "sales_1"),
    ]
    if not role:
        search_dirs = [p for p in base.rglob("*") if p.is_dir()]
    for child in search_dirs:
        if (child / f"{cid}.mp3").exists() or (child / f"{cid}.wav").exists():
            target_dir = child
            break
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / f"{cid}.mp3"

    # Plivo-style recording URLs may require API auth — try plain first, then
    # each configured credential pair.
    pairs: list[tuple] = [(None, None)]
    for aid, tok in (
        (getattr(settings, "vobiz_auth_id", ""), getattr(settings, "vobiz_auth_token", "")),
        (getattr(settings, "vobiz_sales_1_auth_id", ""), getattr(settings, "vobiz_sales_1_auth_token", "")),
        (getattr(settings, "vobiz_sales_2_auth_id", ""), getattr(settings, "vobiz_sales_2_auth_token", "")),
    ):
        if aid and tok:
            pairs.append((aid, tok))

    saved = False
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for aid, tok in pairs:
            kwargs = {"auth": (aid, tok)} if aid else {}
            try:
                resp = await client.get(rec_url, **kwargs)
                if resp.status_code == 200 and resp.content:
                    # Preserve the local WS mix before overwriting
                    if dest.exists():
                        local_backup = dest.with_suffix(".local.mp3")
                        try:
                            import shutil
                            shutil.copy2(str(dest), str(local_backup))
                            logger.info("Preserved local WS mix as {}", local_backup)
                        except Exception as be:
                            logger.warning("Failed to backup local mix: {}", be)
                    tmp = dest.with_suffix(".part")
                    tmp.write_bytes(resp.content)
                    tmp.replace(dest)
                    saved = True
                    break
            except Exception as exc:
                logger.warning("Vobiz recording download attempt failed (auth={}): {}", bool(aid), exc)

    if saved:
        logger.info(
            "Stored VOBIZ recording camp_id={} -> {} ({} bytes) uuid={}",
            cid, dest, dest.stat().st_size, call_uuid,
        )
        return {"ok": True, "path": str(dest)}
    logger.warning(
        "Could not fetch Vobiz recording for camp_id={} ({} auth attempts) — keeping local mix",
        cid, len(pairs),
    )
    return {"ok": False}


@router.post("/vobiz/hangup")
async def vobiz_hangup(request: Request):
    """Vobiz Hangup URL — triggered when a call ends."""
    try:
        form = await request.form()
    except Exception:
        form = {}
    logger.info("Vobiz hangup: {}", dict(form))

    # Finalize manual/incoming call rows via the call-UUID registry.
    try:
        from services.vobiz_bridge import resolve_camp_from_uuid

        uuidv = str(form.get("CallUUID") or form.get("RequestUUID") or "").strip()
        camp_id_mapped, _role_mapped = resolve_camp_from_uuid(uuidv)
        if camp_id_mapped:
            await _finalize_from_hangup(camp_id_mapped, form)
    except Exception as exc:
        logger.warning("hangup finalize failed: {}", exc)

    # Try to process the next queued inbound call
    camp_id = camp_id_mapped or str(form.get("camp_id") or form.get("CallUUID") or "").strip()
    if camp_id and camp_id in _CAMPAIGN_DATA:
        role = _CAMPAIGN_DATA[camp_id].get("_role", "sales_1")
        is_queued = _CAMPAIGN_DATA[camp_id].get("_queued_call", False)
        # Release the call slot
        release_vobiz_call_slot(role)
        outbound_phone = _CAMPAIGN_DATA[camp_id].get("_outbound_phone", "")
        if outbound_phone:
            release_phone_slot(outbound_phone)
        # Clean up campaign data
        del _CAMPAIGN_DATA[camp_id]
        # If this was a queued call, try to process the next one in queue
        if is_queued:
            logger.info("Queued call ended, processing next in queue for role={}", role)
            asyncio.create_task(_trigger_queue_processing(role))
    else:
        # For non-campaign calls (e.g., regular incoming), try to process queue
        role = normalize_console_role(form.get("role") or form.get("manual_role") or "sales_1")
        asyncio.create_task(_trigger_queue_processing(role))

    return Response(content="OK", status_code=200)


async def _trigger_queue_processing(role: str):
    """Background task to process the inbound call queue."""
    try:
        from core.state import process_inbound_queue
        await process_inbound_queue(role)
    except Exception as e:
        logger.error(f"Failed to process inbound queue for role={role}: {e}")


def _parse_ts_epoch(raw) -> float | None:
    """Parse Vobiz's 'yyyy-MM-dd HH:mm:ss' (local) timestamps to epoch."""
    try:
        from datetime import datetime, timezone, timedelta

        s = str(raw or "").strip()
        if not s:
            return None
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone(timedelta(hours=5, minutes=30))).timestamp()
    except Exception:
        return None


async def _finalize_from_hangup(camp_id: str, form: dict) -> None:
    """Finalize a manual/incoming call row when Vobiz reports the hangup."""
    import time

    from core.storage import _get_conn, manual_call_row_by_camp_id, incoming_call_row_by_camp_id

    answer_ts = _parse_ts_epoch(form.get("AnswerTime"))
    end_ts = _parse_ts_epoch(form.get("EndTime"))
    duration = None
    if answer_ts is not None and end_ts is not None:
        duration = round(max(end_ts - answer_ts, 0.0), 2)
    status = "completed" if answer_ts is not None else "failed"

    conn = _get_conn()
    row = await manual_call_row_by_camp_id(camp_id)
    if row and row.get("status") == "dialing":
        conn.execute(
            "UPDATE manual_calls SET status = %s, ended_at = to_char(now(), 'YYYY-MM-DD HH24:MI:SS'), "
            "duration_sec = %s WHERE camp_id = %s",
            (status, duration, camp_id),
        )
        logger.info("Manual call finalized via hangup webhook: camp_id={} status={} duration={}", camp_id, status, duration)
    irow = await incoming_call_row_by_camp_id(camp_id)
    if irow and irow.get("status") != "completed":
        conn.execute(
            "UPDATE incoming_calls SET status = %s, ended_at = to_char(now(), 'YYYY-MM-DD HH24:MI:SS'), "
            "duration_sec = %s WHERE camp_id = %s",
            (status, duration, camp_id),
        )

    if camp_id in _CAMPAIGN_DATA:
        _CAMPAIGN_DATA[camp_id]["_call_ended_at"] = time.time()

    try:
        from core.notifications import push_notification

        role = (_CAMPAIGN_DATA.get(camp_id) or {}).get("_role") or (
            (row or {}).get("role") or "sales_1"
        )
        if status == "failed":
            push_notification(role, "Call ended — no answer", f"camp_id={camp_id}", kind="call")
    except Exception:
        pass


@router.post("/vobiz/stream-status")
async def vobiz_stream_status(request: Request):
    """Stream status callback (StartStream / PlayedStream / StopStream)."""
    try:
        form = await request.form()
    except Exception:
        form = {}
    logger.info("Vobiz stream status: {}", dict(form))
    return Response(content="OK", status_code=200)


@router.get("/vobiz/hangup")
async def vobiz_hangup_get(request: Request):
    """Vobiz Hangup URL (GET fallback)."""
    try:
        form = await request.form()
    except Exception:
        form = {}
    logger.info("Vobiz hangup (GET): {}", dict(form))
    # Try to process the next queued inbound call
    role = normalize_console_role(request.query_params.get("role") or request.query_params.get("manual_role") or "sales_1")
    asyncio.create_task(_trigger_queue_processing(role))
    return Response(content="OK", status_code=200)
