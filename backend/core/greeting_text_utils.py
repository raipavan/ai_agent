"""Normalize stored greeting lines per role (delegates to role sandbox)."""

from __future__ import annotations

from core.role_sandbox import coerce_stored_greeting as _coerce_stored_greeting


def _greeting_normalized(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def coerce_stored_greeting(role: str, text: str | None) -> str:
    return _coerce_stored_greeting(role, text)
