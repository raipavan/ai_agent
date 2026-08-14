"""Hard block for outbound campaign dialing outside allowed local hours (default IST).

Per-role calling windows:
  - sales_1 (Pitchx): calls allowed 11:00–17:00 local only (quiet outside)
  - sales_2 (Opushire): calls allowed 09:30–18:30 local only (quiet outside)
  - any other role (maruti, …): unchanged global quiet window 19:30–09:30 local
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from config import settings

try:
    from services.callback_time import zoneinfo_safe as _zoneinfo_safe
except ImportError:  # services/ absent in this checkout — fall back to stdlib
    from zoneinfo import ZoneInfo as _zoneinfo_safe


def zoneinfo_safe(name: str):
    """Backward-compatible tz resolver (falls back to UTC on unknown names)."""
    try:
        return _zoneinfo_safe(name)
    except Exception:
        from zoneinfo import ZoneInfo

        return ZoneInfo("UTC")


def _parse_hhmm(raw: str, default_h: int, default_m: int) -> time:
    s = (raw or "").strip()
    if not s:
        return time(default_h, default_m)
    parts = s.split(":")
    if len(parts) != 2:
        return time(default_h, default_m)
    try:
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("out of range")
        return time(h, m)
    except ValueError:
        return time(default_h, default_m)


def campaign_quiet_start() -> time:
    """First minute of the blocked window (inclusive), e.g. 19:30."""
    return _parse_hhmm(settings.campaign_quiet_start, 19, 30)


def campaign_quiet_end() -> time:
    """Last blocked minute ends when clock reaches this time (exclusive), e.g. 09:30."""
    return _parse_hhmm(settings.campaign_quiet_end, 9, 30)


def _role_window(role: str | None) -> tuple[time, time] | None:
    """Allowed calling window (start inclusive, end exclusive) for windowed roles, else None."""
    r = (role or "").strip().lower()
    if r == "sales_1":
        return (
            _parse_hhmm(settings.sales_1_call_window_start, 11, 0),
            _parse_hhmm(settings.sales_1_call_window_end, 17, 0),
        )
    if r == "sales_2":
        return (
            _parse_hhmm(settings.sales_2_call_window_start, 9, 30),
            _parse_hhmm(settings.sales_2_call_window_end, 18, 30),
        )
    return None


def _now_in_tz() -> datetime:
    tz = zoneinfo_safe(settings.transcript_callback_tz)
    return datetime.now(tz)


def is_campaign_quiet_hours(role: str | None = None, now: datetime | None = None) -> bool:
    """True when outbound campaign dialing must not run for the given role.

    - windowed roles (sales_1 / sales_2): quiet whenever local time is outside their window
    - any other role (or None): unchanged global quiet window (overnight by default)
    """
    if not settings.campaign_quiet_hours_enabled:
        return False
    tz = zoneinfo_safe(settings.transcript_callback_tz)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    t = now.time()
    win = _role_window(role)
    if win is not None:
        wstart, wend = win
        return t < wstart or t >= wend
    start = campaign_quiet_start()
    end = campaign_quiet_end()
    if start > end:
        return t >= start or t < end
    return start <= t < end


def _fmt_time(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def quiet_hours_block_message(role: str | None = None) -> str:
    """Human-readable reason returned from preflight / API."""
    tz = (settings.transcript_callback_tz or "Asia/Kolkata").strip()
    win = _role_window(role)
    if win is not None:
        ws = _fmt_time(win[0])
        we = _fmt_time(win[1])
        return f"Outbound calls for this role are allowed {ws}–{we} only ({tz})."
    qs = _fmt_time(campaign_quiet_start())
    qe = _fmt_time(campaign_quiet_end())
    return (
        f"Campaigns are blocked during quiet hours ({qs}–{qe} {tz}). "
        f"Outbound calling is allowed {qe}–{qs} only."
    )


def get_campaign_hours_status(role: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Snapshot for dashboard / start-button gating (role-aware)."""
    tz_name = (settings.transcript_callback_tz or "Asia/Kolkata").strip()
    enabled = settings.campaign_quiet_hours_enabled
    in_quiet = is_campaign_quiet_hours(role, now) if enabled else False
    now_local = (now or _now_in_tz())
    if now_local.tzinfo:
        now_local = now_local.astimezone(zoneinfo_safe(tz_name))
    win = _role_window(role)
    if win is not None:
        qs = win[1]  # quiet_start == window end
        qe = win[0]  # quiet_end == window start
        allowed_start = win[0]
        allowed_end = win[1]
    else:
        qs = campaign_quiet_start()
        qe = campaign_quiet_end()
        allowed_start = qe
        allowed_end = qs
    return {
        "enabled": enabled,
        "in_quiet_hours": in_quiet,
        "tz": tz_name,
        "quiet_start": _fmt_time(qs),
        "quiet_end": _fmt_time(qe),
        "allowed_start": _fmt_time(allowed_start),
        "allowed_end": _fmt_time(allowed_end),
        "local_time": now_local.strftime("%H:%M"),
        "block_message": quiet_hours_block_message(role) if in_quiet else "",
    }


def push_to_role_window(role: str, epoch: float) -> float:
    """Given a UTC epoch, return the earliest epoch >= it whose local time is inside the
    role's allowed calling window. If already inside, return the epoch unchanged.

    - windowed roles: their configured window (sales_1 11:00–17:00, sales_2 09:30–18:30)
    - any other role: default allowed window 09:30–19:30 (global quiet window complement)
    """
    tz = zoneinfo_safe(settings.transcript_callback_tz)
    dt = datetime.fromtimestamp(epoch, tz)
    win = _role_window(role)
    if win is not None:
        wstart, wend = win
    else:
        wstart = campaign_quiet_end()   # 09:30
        wend = campaign_quiet_start()   # 19:30
    t = dt.time()
    if wstart <= t < wend:
        return epoch
    nxt = dt.replace(hour=wstart.hour, minute=wstart.minute, second=0, microsecond=0)
    if nxt <= dt:
        nxt += timedelta(days=1)
    return nxt.timestamp()
