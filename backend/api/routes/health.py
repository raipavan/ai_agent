"""`GET /health` — process summary."""

from __future__ import annotations

from fastapi import APIRouter

from core.state import _ACTIVE_VOBIZ_CALLS, _CAMPAIGN_TASKS

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    active_campaigns = sum(1 for t in _CAMPAIGN_TASKS.values() if t and not t.done())
    return {
        "status": "healthy",
        "mode": "bridge",
        "active_calls": _ACTIVE_VOBIZ_CALLS,
        "active_campaigns": active_campaigns,
    }
