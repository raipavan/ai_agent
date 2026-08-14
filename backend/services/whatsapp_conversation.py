"""WhatsApp conversation history (stub, in-memory)."""

from __future__ import annotations

_hist: dict[str, list[dict]] = {}


def add_message(phone: str, sender: str, text: str) -> None:
    _hist.setdefault(phone, []).append({"sender": sender, "text": text})


async def analyze_inbound_message(phone: str, text: str) -> dict:
    """Stub: never auto-responds."""
    return {"should_respond": False}


def get_history(phone: str, limit: int = 50) -> list:
    return list(_hist.get(phone, []))[-limit:]
