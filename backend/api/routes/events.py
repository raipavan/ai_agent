"""SSE endpoint — pushes live campaign state to dashboard clients."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from loguru import logger

from core.events import get_event_bus

router = APIRouter(tags=["events"])


async def _build_state(role: str) -> dict | None:
    """Rebuild campaign state payload — now reads from materialized state (<5ms)."""
    try:
        from core import kv_cache
        from core import storage as lead_storage

        cached = kv_cache.state_get(role)
        if cached is not None:
            return cached

        from core.dashboard_state import build_api_payload_sync
        payload = build_api_payload_sync(role)
        if payload is None:
            return None

        payload["campaign_paused"] = await lead_storage.is_campaign_globally_paused()
        kv_cache.state_set(role, payload)
        return payload
    except Exception as e:
        logger.warning("SSE build_state failed for role={}: {}", role, e)
        return None


async def _fetch_lead(role: str, lead_id: int) -> dict | None:
    """Fetch a single lead row (for pushing the changed lead to the client)."""
    try:
        from core import storage as lead_storage
        from core.campaign_payload import slim_lead_for_api
        row = await lead_storage.get_lead(role, lead_id)
        if row:
            return slim_lead_for_api(dict(row), role=role)
    except Exception as e:
        logger.warning("SSE fetch_lead failed: {}", e)
    return None


def _stats_update_frame(role: str) -> str | None:
    """Build a ``stats_update`` SSE frame with fresh dashboard stats."""
    try:
        from api.routes.dashboard_api import build_dashboard_stats

        stats = build_dashboard_stats(role)
        return f"data: {json.dumps({'type': 'stats_update', 'stats': stats, 'role': role})}\n\n"
    except Exception as exc:
        logger.warning("SSE stats_update failed: {}", exc)
        return None


def _resolve_role(request: Request) -> str | None:
    """Extract role from JWT (header or access_token query param)."""
    try:
        from core.auth import _decode_jwt
        for src in (
            request.headers.get("Authorization", "").removeprefix("Bearer "),
            request.query_params.get("access_token", ""),
            request.query_params.get("token", ""),
        ):
            if src:
                payload = _decode_jwt(src)
                if payload and payload.get("role"):
                    return str(payload["role"])
    except Exception:
        pass
    return None


@router.get("/api/events/stream")
async def sse_stream(request: Request, role: str = Query("sales_1")):
    """Server-Sent Events stream. Sends full state + changed lead on each event."""
    jwt_role = _resolve_role(request)
    if jwt_role:
        role = jwt_role
    bus = get_event_bus()
    q = bus.subscribe()

    async def event_generator():
        try:
            # Send initial state immediately so the client has data
            state = await _build_state(role)
            if state:
                yield f"data: {json.dumps({'type': 'state', 'state': state, 'changed_lead': None})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'state', 'state': None, 'changed_lead': None})}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    raw = await asyncio.wait_for(q.get(), timeout=5.0)
                    msg = json.loads(raw)
                    if msg.get("type") == "lead_updated" and msg.get("role") == role:
                        lead_id = msg.get("lead_id")
                        state = await _build_state(role)
                        changed_lead = await _fetch_lead(role, lead_id) if lead_id else None
                        yield f"data: {json.dumps({'type': 'lead_updated', 'state': state, 'changed_lead': changed_lead})}\n\n"
                        # Push fresh dashboard stats immediately so the console
                        # reacts to call status changes without the 5s timer.
                        frame = _stats_update_frame(role)
                        if frame:
                            yield frame
                except asyncio.TimeoutError:
                    # Periodic dashboard stats so the console updates in realtime.
                    frame = _stats_update_frame(role)
                    yield frame if frame else ":\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
