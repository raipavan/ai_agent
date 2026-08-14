"""Notification REST endpoints for the operator console."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from core import notifications as notif_store

router = APIRouter(tags=["notifications"])


def _role_from_request(request: Request, default: str = "sales_1") -> str | None:
    role_param = (request.query_params.get("role") or "").strip()
    if role_param:
        return role_param
    try:
        from core.auth import console_role_from_request

        return console_role_from_request(request, default=default)
    except Exception:
        return default


@router.get("/api/notifications")
async def get_notifications(
    request: Request,
    role: str = Query(""),
    limit: int = Query(50),
):
    r = role or _role_from_request(request)
    items, unread = notif_store.list_notifications(r, limit=limit)
    return {"notifications": items, "unread": unread, "role": r}


@router.post("/api/notifications/{notif_id}/read")
async def notification_read(notif_id: int):
    ok = notif_store.mark_read(notif_id)
    return {"status": "ok" if ok else "not_found", "id": notif_id}


@router.post("/api/notifications/read-all")
async def notifications_read_all(request: Request, role: str = Query("")):
    r = role or _role_from_request(request)
    count = notif_store.mark_all_read(r)
    return {"status": "ok", "marked": count}
