"""Email automation (stub)."""

from __future__ import annotations

from loguru import logger


async def send_email_project_details(*args, **kwargs) -> dict:
    logger.warning("Email send is a stub (services/ placeholder)")
    return {"sent": False, "error": "Email bridge unavailable (stub build)"}


async def send_bulk_project_email(*args, **kwargs) -> dict:
    logger.warning("Bulk email send is a stub (services/ placeholder)")
    return {"sent": False, "error": "Email bridge unavailable (stub build)"}


async def send_report_email(*args, **kwargs) -> dict:
    logger.warning("Report email send is a stub (services/ placeholder)")
    return {"sent": False, "error": "Email bridge unavailable (stub build)"}
