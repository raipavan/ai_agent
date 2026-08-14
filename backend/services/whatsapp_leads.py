"""WhatsApp outbound messaging (stub)."""

from __future__ import annotations

from loguru import logger


def parse_meta_webhook_messages(body) -> list:
    """Stub: no Meta webhook messages parsed."""
    return []


async def process_dariaan_whatsapp_inbound(*args, **kwargs) -> dict:
    logger.warning("WhatsApp inbound processing is a stub (services/ placeholder)")
    return {}


async def send_whatsapp_project_details(phone: str, summary: str = "", lead_name: str = "") -> dict:
    logger.warning("WhatsApp details send is a stub (services/ placeholder)")
    return {"sent": False, "error": "WhatsApp bridge unavailable (stub build)"}


async def send_whatsapp_text_message(to_phone: str, text: str) -> dict:
    logger.warning("WhatsApp text send is a stub (services/ placeholder)")
    return {"sent": False, "error": "WhatsApp bridge unavailable (stub build)"}


def wa_me_link(number: str) -> str:
    return f"https://wa.me/{number}"


async def _get_openwa_session_uuid(client=None) -> str:
    """Stub: no OpenWA session."""
    return ""
