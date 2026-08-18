"""Dashboard compatibility API for the operator console UI.

The console (console.html) polls /api/dashboard, /api/calls, /api/live,
/api/conversations, /api/callbacks/queue and /api/campaigns/*. This router
implements those endpoints over the real Postgres data + runtime state so the
dashboard updates in realtime and call history is populated.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import StreamingResponse
from loguru import logger

from core.state import (
    _CAMPAIGN_DATA,
    _CAMPAIGN_TASKS,
    get_state,
    get_inbound_queue_length,
    total_active_vobiz_calls,
)

router = APIRouter(tags=["dashboard"])

_IST = timezone(timedelta(hours=5, minutes=30))


def _role_from_request(request: Request, default: str = "sales_1") -> str:
    role_param = (request.query_params.get("role") or "").strip()
    if role_param:
        from core.state import normalize_console_role

        return normalize_console_role(role_param)
    try:
        from core.auth import console_role_from_request

        return console_role_from_request(request, default=default)
    except Exception:
        return default


def _fmt_duration(sec) -> str:
    try:
        sec = int(float(sec or 0))
    except Exception:
        sec = 0
    if sec <= 0:
        return "—"
    m, s = divmod(sec, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"


def _fmt_date(raw) -> str:
    if not raw:
        return "—"
    s = str(raw)
    try:
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_IST)
        return dt.strftime("%b %d, %H:%M")
    except Exception:
        return s[:16]


def _analysis_dict(raw) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except Exception:
        return {}


def _recording_available(log_id: str) -> bool:
    if not (log_id or "").strip():
        return False
    try:
        from services.call_recording import resolve_session_recording_path

        p = resolve_session_recording_path(log_id)
        return bool(p and p.is_file())
    except Exception:
        return False


def _all_call_rows(role: str) -> list[dict]:
    """Merge manual calls, incoming calls and called campaign leads into one list."""
    from core.storage import _get_conn

    conn = _get_conn()
    out: list[dict] = []
    for row in conn.execute(
        "SELECT * FROM manual_calls WHERE role = %s ORDER BY id DESC LIMIT 2000", (role,)
    ).fetchall():
        an = _analysis_dict(row.get("analysis_json"))
        rec_avail = _recording_available(str(row.get("log_id") or row.get("camp_id") or ""))
        out.append(
            {
                "id": f"m{row['id']}",
                "name": row["callee_name"] or "Unknown",
                "phone": row["to_phone"] or "",
                "vehicle": "—",
                "intent": an.get("disposition") or row["disposition"] or row["summary"] or row["status"],
                "duration_sec": row["duration_sec"],
                "duration": _fmt_duration(row["duration_sec"]),
                "sentiment": an.get("emotion") or row["emotion_label"] or "Neutral",
                "language": an.get("language") or "—",
                "outcome": row["status"],
                "date": _fmt_date(row["started_at"]),
                "raw_date": row["started_at"],
                "direction": "Outbound",
                "rating": int(an.get("rating") or 0),
                "transcript": an.get("transcript") or row["summary"] or "",
                "cost": "—",
                "recording_available": rec_avail,
                "recording_url": f"/api/manual/calls/{row['id']}/recording?role={role}" if rec_avail else "",
            }
        )
    for row in conn.execute(
        "SELECT * FROM incoming_calls WHERE role = %s ORDER BY id DESC LIMIT 2000", (role,)
    ).fetchall():
        an = _analysis_dict(row.get("analysis_json"))
        rec_avail = _recording_available(str(row.get("log_id") or row.get("camp_id") or ""))
        out.append(
            {
                "id": f"i{row['id']}",
                "name": row["caller_name"] or "Unknown",
                "phone": row["from_phone"] or "",
                "vehicle": "—",
                "intent": an.get("disposition") or row["disposition"] or row["summary"] or row["status"],
                "duration_sec": row["duration_sec"],
                "duration": _fmt_duration(row["duration_sec"]),
                "sentiment": an.get("emotion") or row["emotion_label"] or "Neutral",
                "language": an.get("language") or "—",
                "outcome": row["status"],
                "date": _fmt_date(row["started_at"]),
                "raw_date": row["started_at"],
                "direction": "Inbound",
                "rating": int(an.get("rating") or 0),
                "transcript": an.get("transcript") or row["summary"] or "",
                "cost": "—",
                "recording_available": rec_avail,
                "recording_url": f"/api/incoming/calls/{row['id']}/recording?role={role}" if rec_avail else "",
            }
        )
    for row in conn.execute(
        """
        SELECT * FROM leads
        WHERE role = %s AND (start_time IS NOT NULL AND start_time > 0)
        ORDER BY id DESC LIMIT 3000
        """,
        (role,),
    ).fetchall():
        an = _analysis_dict(row.get("analysis"))
        extra = row.get("extra") or {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except Exception:
                extra = {}
        out.append(
            {
                "id": f"l{row['id']}",
                "name": row["name"] or "Unknown",
                "phone": row["phone"] or "",
                "vehicle": extra.get("vehicle") or "—",
                "intent": an.get("disposition") or row["status"],
                "duration_sec": None,
                "duration": _fmt_duration(None),
                "sentiment": an.get("emotion") or "Neutral",
                "language": an.get("language") or "—",
                "outcome": row["status"],
                "date": _fmt_date(row["created_at"]),
                "raw_date": row["created_at"],
                "direction": "Outbound",
                "rating": int(an.get("rating") or 0),
                "transcript": an.get("summary") or "",
                "cost": "—",
            }
        )
    out.sort(key=lambda c: str(c["raw_date"] or ""), reverse=True)
    return out


def build_dashboard_stats(role: str) -> dict:
    """Aggregate stats used by GET /api/dashboard and SSE stats_update.

    All values are computed from Postgres (manual_calls, incoming_calls, leads)
    plus live runtime state. Chart payloads (timeline / hourly / sentiment) are
    real aggregates so the console renders only actual data.
    """
    from core.storage import _get_conn

    conn = _get_conn()
    mc = conn.execute(
        "SELECT COUNT(*) c, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) done, "
        "AVG(duration_sec) ad FROM manual_calls WHERE role = %s", (role,)
    ).fetchone()
    ic = conn.execute(
        "SELECT COUNT(*) c, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) done, "
        "AVG(duration_sec) ad FROM incoming_calls WHERE role = %s", (role,)
    ).fetchone()
    lc = conn.execute(
        "SELECT COUNT(*) c, SUM(CASE WHEN status='interested' THEN 1 ELSE 0 END) interested, "
        "SUM(CASE WHEN status IN ('completed','interested','site_visit','callback_scheduled','not_interested') THEN 1 ELSE 0 END) resolved "
        "FROM leads WHERE role = %s AND (start_time IS NOT NULL AND start_time > 0)", (role,)
    ).fetchone()
    cb = conn.execute(
        "SELECT COUNT(*) c FROM scheduled_callbacks WHERE role = %s AND status = 'scheduled'", (role,)
    ).fetchone()

    total = int(mc["c"] or 0) + int(ic["c"] or 0) + int(lc["c"] or 0)
    done = int(mc["done"] or 0) + int(ic["done"] or 0) + int(lc["resolved"] or 0)
    dur = float((mc["ad"] or 0) + (ic["ad"] or 0))
    durs = int(mc["done"] or 0) + int(ic["done"] or 0)
    avg_dur = f"{int(dur / durs)}s" if durs else "—"
    live_calls = len(_live_calls_for_role(role))
    active = max(int(total_active_vobiz_calls()), live_calls)
    interested = int(lc["interested"] or 0)
    success_rate = round((done / total) * 100) if total else 0

    today_start = datetime.now(_IST).replace(hour=0, minute=0, second=0, microsecond=0)
    today_sql = today_start.strftime("%Y-%m-%d %H:%M:%S")
    tc = conn.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM manual_calls WHERE role=%s AND started_at >= %s) + "
        "(SELECT COUNT(*) FROM incoming_calls WHERE role=%s AND started_at >= %s) + "
        "(SELECT COUNT(*) FROM leads WHERE role=%s AND start_time IS NOT NULL AND start_time > 0 AND to_timestamp(start_time) >= %s::timestamp) c",
        (role, today_sql, role, today_sql, role, today_sql),
    ).fetchone()
    today_calls = int(tc["c"] or 0)

    # ── 7-day timeline (last 7 days, oldest → newest) — bucketed in Python ──
    labels: list[str] = []
    counts: list[int] = []
    day_start = today_start - timedelta(days=6)
    day_sql = day_start.strftime("%Y-%m-%d %H:%M:%S")
    buckets: dict[int, int] = {}

    def _bucket(ts) -> None:
        if not ts:
            return
        s = str(ts)
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_IST)
        if dt < day_start or dt >= today_start + timedelta(days=1):
            return
        idx = (dt - day_start).days
        buckets[idx] = buckets.get(idx, 0) + 1

    for r in conn.execute(
        "SELECT started_at FROM manual_calls WHERE role = %s AND started_at >= %s", (role, day_sql)
    ).fetchall():
        _bucket(r["started_at"])
    for r in conn.execute(
        "SELECT started_at FROM incoming_calls WHERE role = %s AND started_at >= %s", (role, day_sql)
    ).fetchall():
        _bucket(r["started_at"])
    for r in conn.execute(
        "SELECT start_time FROM leads WHERE role = %s AND start_time IS NOT NULL AND start_time > 0 AND start_time >= %s",
        (role, day_start.timestamp()),
    ).fetchall():
        _bucket(datetime.fromtimestamp(float(r["start_time"]), _IST).strftime("%Y-%m-%d %H:%M:%S"))
    for i in range(7):
        labels.append((day_start + timedelta(days=i)).strftime("%a"))
        counts.append(buckets.get(i, 0))

    # ── hourly distribution (call start hour, local) ──
    hourly = [0] * 24
    for r in conn.execute(
        "SELECT EXTRACT(HOUR FROM TO_TIMESTAMP(started_at, 'YYYY-MM-DD HH24:MI:SS')) h, COUNT(*) c "
        "FROM manual_calls WHERE role=%s AND started_at IS NOT NULL GROUP BY 1", (role,)
    ).fetchall():
        hourly[min(23, int(r["h"] or 0))] += int(r["c"] or 0)
    for r in conn.execute(
        "SELECT EXTRACT(HOUR FROM TO_TIMESTAMP(started_at, 'YYYY-MM-DD HH24:MI:SS')) h, COUNT(*) c "
        "FROM incoming_calls WHERE role=%s AND started_at IS NOT NULL GROUP BY 1", (role,)
    ).fetchall():
        hourly[min(23, int(r["h"] or 0))] += int(r["c"] or 0)
    for r in conn.execute(
        "SELECT EXTRACT(HOUR FROM TO_TIMESTAMP(start_time)) h, COUNT(*) c "
        "FROM leads WHERE role=%s AND start_time IS NOT NULL AND start_time > 0 GROUP BY 1", (role,)
    ).fetchall():
        hourly[min(23, int(r["h"] or 0))] += int(r["c"] or 0)

    # ── sentiment breakdown + satisfaction from real analysis labels ──
    POSITIVE = {"Satisfied", "Happy", "Excited"}
    NEGATIVE = {"Annoyed", "Urgent"}
    sentiment: dict[str, int] = {}
    pos = neg = with_label = 0
    for row in conn.execute(
        "SELECT analysis_json FROM manual_calls WHERE role=%s", (role,)
    ).fetchall():
        an = _analysis_dict(row.get("analysis_json"))
        emo = an.get("emotion") or row.get("emotion_label") or ""
        if emo:
            with_label += 1
            sentiment[emo] = sentiment.get(emo, 0) + 1
            if emo in POSITIVE:
                pos += 1
            elif emo in NEGATIVE:
                neg += 1
    for row in conn.execute(
        "SELECT analysis_json FROM incoming_calls WHERE role=%s", (role,)
    ).fetchall():
        an = _analysis_dict(row.get("analysis_json"))
        emo = an.get("emotion") or row.get("emotion_label") or ""
        if emo:
            with_label += 1
            sentiment[emo] = sentiment.get(emo, 0) + 1
            if emo in POSITIVE:
                pos += 1
            elif emo in NEGATIVE:
                neg += 1
    for row in conn.execute(
        "SELECT analysis FROM leads WHERE role=%s AND start_time IS NOT NULL AND start_time > 0", (role,)
    ).fetchall():
        an = _analysis_dict(row.get("analysis"))
        emo = an.get("emotion") or ""
        if emo:
            with_label += 1
            sentiment[emo] = sentiment.get(emo, 0) + 1
            if emo in POSITIVE:
                pos += 1
            elif emo in NEGATIVE:
                neg += 1
    satisfaction = round(pos / with_label * 100) if with_label else 0

    # ── AI status: real — unhealthy if unread AI alerts exist ──
    ai_status = "Online"
    try:
        bad = conn.execute(
            "SELECT 1 FROM notifications WHERE read = 0 AND title IN "
            "('AI credits depleted', 'AI voice engine error') LIMIT 1"
        ).fetchone()
        if bad:
            ai_status = "Offline"
    except Exception:
        pass

    return {
        "total_calls": total,
        "today_calls": today_calls,
        "active_calls": active,
        "live_calls": live_calls,
        "today_bookings": interested,
        "waiting_queue": sum(get_inbound_queue_length(r) for r in ("sales_1", "sales_2")),
        "callback_queue": int(cb["c"] or 0),
        "customer_satisfaction": satisfaction,
        "workshop_occupancy": 0,
        "ai_status": ai_status,
        "success_rate": success_rate,
        "spare_parts_alerts": 0,
        "advisor_performance": 0,
        "avg_duration": avg_dur,
        "answered": done,
        "missed": max(total - done, 0),
        "satisfaction": satisfaction,
        "vehicles_in_service": 0,
        "vehicles_ready": 0,
        "timeline": {"labels": labels, "counts": counts},
        "hourly": {"labels": [f"{h % 12 or 12}{'a' if h < 12 else 'p'}" for h in range(24)], "counts": hourly},
        "sentiment": sentiment,
    }


def _recent_calls(role: str, limit: int = 8) -> list[dict]:
    rows = _all_call_rows(role)[:limit]
    return [
        {
            "id": c["id"],
            "name": c["name"],
            "vehicle": c["vehicle"],
            "service_type": str(c["intent"])[:60],
            "outcome": c["outcome"],
            "date": c["date"],
        }
        for c in rows
    ]


@router.get("/api/dashboard")
async def api_dashboard(request: Request):
    role = _role_from_request(request)
    stats = build_dashboard_stats(role)
    from core import notifications

    items, _unread = notifications.list_notifications(role, limit=10)
    return {
        "stats": stats,
        "recent_calls": _recent_calls(role),
        "notifications": items,
        "role": role,
    }


# ── Call history ─────────────────────────────────────────────────────────


@router.get("/api/calls")
async def api_calls(
    request: Request,
    page: int = Query(1),
    per_page: int = Query(25),
    search: str = Query(""),
    status: str = Query(""),
    sentiment: str = Query(""),
    intent: str = Query(""),
    language: str = Query(""),
):
    role = _role_from_request(request)
    rows = _all_call_rows(role)

    s = (search or "").strip().lower()
    st = (status or "").strip().lower()
    sent = (sentiment or "").strip().lower()
    it = (intent or "").strip().lower()
    lang = (language or "").strip().lower()

    def match(c):
        if s and s not in str(c["name"]).lower() and s not in str(c["phone"]).lower():
            return False
        if st and str(c["outcome"]).lower() != st:
            return False
        if sent and sent not in str(c["sentiment"]).lower():
            return False
        if it and it not in str(c["intent"]).lower():
            return False
        if lang and lang not in str(c["language"]).lower():
            return False
        return True

    filtered = [c for c in rows if match(c)]
    total = len(filtered)
    pp = max(1, min(int(per_page), 100))
    total_pages = max(1, (total + pp - 1) // pp)
    page = max(1, min(int(page), total_pages))
    page_rows = filtered[(page - 1) * pp : page * pp]
    return {
        "total": total,
        "page": page,
        "per_page": pp,
        "total_pages": total_pages,
        "calls": [
            {
                "id": c["id"],
                "name": c["name"],
                "phone": c["phone"],
                "vehicle": c["vehicle"],
                "intent": c["intent"],
                "duration": c["duration"],
                "sentiment": c["sentiment"],
                "language": c["language"],
                "outcome": c["outcome"],
                "date": c["date"],
            }
            for c in page_rows
        ],
    }


@router.get("/api/calls/{call_id}")
async def api_call_detail(call_id: str, request: Request):
    role = _role_from_request(request)
    row = next((c for c in _all_call_rows(role) if c["id"] == call_id), None)
    if not row:
        raise HTTPException(404, "Call not found")
    return row


@router.post("/api/calls/{call_id}/reanalyze")
async def api_call_reanalyze(call_id: str, request: Request):
    role = _role_from_request(request)
    row = next((c for c in _all_call_rows(role) if c["id"] == call_id), None)
    if not row:
        raise HTTPException(404, "Call not found")
    try:
        from services.call_analyzer import analyze_call_transcript

        await analyze_call_transcript(row.get("transcript") or "")
    except Exception as exc:
        logger.warning("reanalyze failed for {}: {}", call_id, exc)
    return {"status": "ok", "id": call_id}


# ── Live calls ───────────────────────────────────────────────────────────


def _live_calls_for_role(role: str) -> list[dict]:
    """Active calls for a role: connected legs, ringing (dialed) legs and
    manual dialing rows. Used by GET /api/live and build_dashboard_stats."""
    from core.storage import _get_conn

    calls: list[dict] = []
    now = time.time()
    for camp_id, c in _CAMPAIGN_DATA.items():
        if c.get("_role") != role:
            continue
        connected = c.get("_call_connected_at")
        ended = c.get("_call_ended_at")
        dial_epoch = c.get("_dial_epoch")
        if connected and not ended:
            dur = int(now - float(connected))
            calls.append(
                {
                    "id": camp_id,
                    "name": c.get("name") or "Unknown",
                    "phone": c.get("phone") or "",
                    "direction": "outbound",
                    "number": c.get("_outbound_phone") or "",
                    "vehicle": "—",
                    "language": "—",
                    "duration": _fmt_duration(dur),
                    "status": "Live",
                }
            )
        elif not connected and not ended and c.get("_outbound_phone"):
            # Ringing legs older than 120s with no connect are stale (missed
            # call / hangup before answer) — hide them from the live board.
            if dial_epoch is not None and now - float(dial_epoch) > 120:
                continue
            calls.append(
                {
                    "id": camp_id,
                    "name": c.get("name") or "Unknown",
                    "phone": c.get("phone") or "",
                    "direction": "outbound",
                    "number": c.get("_outbound_phone") or "",
                    "vehicle": "—",
                    "language": "—",
                    "duration": "ringing…",
                    "status": "Ringing",
                }
            )
    conn = _get_conn()
    for row in conn.execute(
        "SELECT * FROM manual_calls WHERE role = %s AND status = 'dialing' LIMIT 10", (role,)
    ).fetchall():
        calls.append(
            {
                "id": f"m{row['id']}",
                "name": row["callee_name"] or "Unknown",
                "phone": row["to_phone"] or "",
                "direction": "outbound",
                "number": "—",
                "vehicle": "—",
                "language": "—",
                "duration": "dialing…",
                "status": "Dialing",
            }
        )
    return calls


@router.get("/api/live")
async def api_live(request: Request):
    role = _role_from_request(request)
    calls = _live_calls_for_role(role)
    return {"active_count": len(calls), "calls": calls}


# ── Callback queue ───────────────────────────────────────────────────────


@router.get("/api/callbacks/queue")
async def api_callback_queue(request: Request):
    role = _role_from_request(request)
    from core.storage import _get_conn

    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM scheduled_callbacks WHERE role = %s AND status = 'scheduled' "
        "ORDER BY scheduled_at ASC LIMIT 20",
        (role,),
    ).fetchall()
    queue = [
        {"name": r["name"] or r["phone"], "phone": r["phone"], "reason": "Scheduled callback"}
        for r in rows
    ]
    return {"queue": queue}


# ── Conversations ────────────────────────────────────────────────────────


@router.get("/api/conversations")
async def api_conversations(request: Request, page: int = Query(1)):
    role = _role_from_request(request)
    rows = _all_call_rows(role)[:100]
    return {
        "conversations": [
            {
                "id": c["id"],
                "name": c["name"],
                "phone": c["phone"],
                "sentiment": c["sentiment"],
                "language": c["language"],
                "duration": c["duration"],
                "intent": c["intent"],
                "vehicle": c["vehicle"],
                "date": c["date"],
                "ai_confidence": 0.9,
                "rating": min(c["rating"], 5),
                "summary": c.get("intent") or "",
                "transcript": c.get("transcript") or "",
                "recording_available": c.get("recording_available", False),
                "recording_url": c.get("recording_url", ""),
            }
            for c in rows
        ],
        "page": int(page),
    }


# ── Campaigns page ───────────────────────────────────────────────────────


@router.get("/api/campaigns/status")
async def api_campaigns_status(request: Request):
    role = _role_from_request(request)
    from core import storage as lead_storage
    from core.state import _MANUALLY_STOPPED_ROLES

    counts = await lead_storage.get_lead_counts(role)
    running = _CAMPAIGN_TASKS.get(role) is not None
    paused = await lead_storage.is_campaign_globally_paused() or role in _MANUALLY_STOPPED_ROLES
    total = int(counts.get("total", 0))
    called = total - int(counts.get("pending", 0)) - int(counts.get("dialing", 0))
    completed = total - int(counts.get("pending", 0)) - int(counts.get("dialing", 0))
    current_lead = None
    try:
        dialing = await lead_storage.get_leads(role, status="dialing", limit=1)
        if dialing:
            l = dialing[0]
            extra = l.get("extra") or {}
            if isinstance(extra, str):
                try:
                    extra = json.loads(extra)
                except Exception:
                    extra = {}
            current_lead = {"name": l.get("name") or "", "vehicle": extra.get("vehicle") or "—"}
    except Exception:
        pass
    return {
        "running": running,
        "paused": paused,
        "leads_total": total,
        "leads_called": called,
        "leads_completed": completed,
        "current_lead": current_lead,
        "notifications": [],
    }


@router.get("/api/phone-numbers")
async def api_phone_numbers(request: Request):
    role = _role_from_request(request)
    from core.outbound_numbers import get_all_outbound_numbers

    state = get_state(role)
    numbers = get_all_outbound_numbers(role, state.get("vobiz", {}) or {})
    return {"numbers": [n for n in numbers if n]}


@router.get("/api/campaigns/files")
async def api_campaign_files(request: Request):
    role = _role_from_request(request)
    from core import storage as lead_storage

    sources = await lead_storage.get_campaign_sources(role, [])
    return {
        "files": [
            {
                "name": s.get("name") or "upload",
                "size": "—",
                "leads": int(s.get("total") or 0),
                "uploaded": "—",
                "status": "Paused" if s.get("paused") else "Active",
            }
            for s in sources
        ]
    }


@router.post("/api/campaigns/control")
async def api_campaign_control(request: Request):
    role = _role_from_request(request)
    from core import storage as lead_storage
    from core.state import _MANUALLY_STOPPED_ROLES

    try:
        body = await request.json()
    except Exception:
        body = {}
    action = str(body.get("action") or "").strip().lower()

    if action == "start" or action == "resume":
        await lead_storage.set_campaign_globally_paused(False)
        _MANUALLY_STOPPED_ROLES.discard(role)
        await lead_storage.set_campaign_want_running(role, True)
        if _CAMPAIGN_TASKS.get(role) is None:
            from core.worker import _campaign_worker_role

            _CAMPAIGN_TASKS[role] = asyncio.create_task(_campaign_worker_role(role))
    elif action == "stop" or action == "pause":
        await lead_storage.set_campaign_want_running(role, False)
        _MANUALLY_STOPPED_ROLES.add(role)
        task = _CAMPAIGN_TASKS.get(role)
        if task and not task.done():
            task.cancel()
        _CAMPAIGN_TASKS[role] = None
    elif action == "clear":
        from core.storage import _wipe_leads_sync

        _wipe_leads_sync(role)
    elif action == "set_pause":
        try:
            secs = float(body.get("pause_seconds") or 5)
        except Exception:
            secs = 5.0
        from core.storage import _save_role_state_sync

        _save_role_state_sync(role, delay_sec=max(0.0, min(secs, 1200.0)))
    else:
        raise HTTPException(400, f"Unknown action: {action}")

    try:
        from core.notifications import push_notification

        push_notification(role, f"Campaign {action}", kind="campaign")
    except Exception:
        pass
    return {"status": "ok", "action": action, "role": role}


@router.post("/api/campaigns/files/upload")
async def api_campaign_upload(request: Request, file: UploadFile = File(...)):
    role = _role_from_request(request)
    from core import storage as lead_storage

    content = await file.read()
    text = ""
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        text = content.decode("latin-1", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise HTTPException(400, "Empty file")
    header = [h.strip().lower() for h in lines[0].split(",")]
    leads = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 2:
            continue
        lead = {"name": parts[0].strip(), "phone": parts[1].strip()}
        for i, h in enumerate(header):
            if i < len(parts) and h not in ("name", "phone"):
                lead[h] = parts[i].strip()
        lead["extra"] = {"upload_source": file.filename or "upload"}
        leads.append(lead)
    if not leads:
        raise HTTPException(400, "No leads parsed")
    n = await lead_storage.bulk_add_leads(role, leads)
    return {"status": "ok", "imported": n, "filename": file.filename}


@router.get("/api/campaigns/events")
async def api_campaign_events(request: Request):
    role = _role_from_request(request)

    async def gen():
        while True:
            if await request.is_disconnected():
                break
            try:
                status = await api_campaigns_status(request)
                status["type"] = "campaign_update"
                yield f"data: {json.dumps(status, default=str)}\n\n"
            except Exception as exc:
                logger.warning("campaign SSE error: {}", exc)
            await asyncio.sleep(5)

    return StreamingResponse(gen(), media_type="text/event-stream")
