"""Timezone helpers (stub implementation using stdlib zoneinfo)."""

from __future__ import annotations

from zoneinfo import ZoneInfo


def zoneinfo_safe(name: str):
    """Return a tzinfo for ``name``, falling back to UTC."""
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def annotate_analysis_callback_epoch(analysis, *args, **kwargs):
    """Stub: leave analysis untouched (no callback-time annotation)."""
    return analysis
