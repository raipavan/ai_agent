"""Runtime state management — uses SQLite for persistence, in-memory for active tracking."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional
from loguru import logger

# In-memory active tracking (not persisted)
_ACTIVE_VOBIZ_CALLS: int = 0
_ACTIVE_VOBIZ_CALLS_BY_ROLE: dict[str, int] = {}
_CAMPAIGN_DATA: dict[str, dict[str, Any]] = {}
_CAMPAIGN_TASKS: dict[str, Any] = {}
_OPENING_PCM_CACHE: dict[str, tuple[bytes, int]] = {}
_LAST_WORKER_ACTIVITY: dict[str, float] = {}
_MANUALLY_STOPPED_ROLES: set[str] = set()

# Inbound call queue — when AI is busy, incoming calls are queued
# Structure: {role: [list of queued caller dicts]}
# Each queued caller: {"from_num": str, "from_digits": str, "lead_name": str, "call_uuid": str}
_INBOUND_CALL_QUEUE: dict[str, list[dict[str, str]]] = {}

_ROLES = (
    "sales_1",
    "sales_2",
    "sales_3",
    "sales_4",
    "sales_5",
)


from typing import Optional

def normalize_console_role(role: Optional[str]) -> str:
    """Ensure the role is valid, defaulting to 'sales_1' (Pitchx)."""
    r = (role or "sales_1").lower().strip()
    return r if r in _ROLES else "sales_1"


def active_vobiz_calls_for_role(role: str) -> int:
    """Outbound/live legs currently active for one console role."""
    return int(_ACTIVE_VOBIZ_CALLS_BY_ROLE.get(normalize_console_role(role), 0))


def get_max_concurrency_for_role(role: str) -> int:
    """Get max concurrent calls allowed for a role, based on configured phone numbers count."""
    try:
        from core.outbound_numbers import get_all_outbound_numbers
        v_cfg = get_state(role).get("vobiz", {}) or {}
        numbers = get_all_outbound_numbers(role, v_cfg)
        return max(1, len(numbers))
    except Exception:
        return 1


def role_has_active_vobiz_call(role: str) -> bool:
    """True when this role has reached its maximum active concurrent calls."""
    return active_vobiz_calls_for_role(role) >= get_max_concurrency_for_role(role)


def total_active_vobiz_calls() -> int:
    """Total live outbound legs across all roles (for dashboards)."""
    return int(sum(_ACTIVE_VOBIZ_CALLS_BY_ROLE.values()))


def acquire_vobiz_call_slot(role: str) -> None:
    """Reserve one telephony slot for ``role``; updates global active count for dashboards."""
    global _ACTIVE_VOBIZ_CALLS
    r = normalize_console_role(role)
    _ACTIVE_VOBIZ_CALLS_BY_ROLE[r] = active_vobiz_calls_for_role(r) + 1
    _ACTIVE_VOBIZ_CALLS = total_active_vobiz_calls()


def release_vobiz_call_slot(role: str) -> None:
    """Release a telephony slot for ``role``."""
    global _ACTIVE_VOBIZ_CALLS
    r = normalize_console_role(role)
    cur = int(_ACTIVE_VOBIZ_CALLS_BY_ROLE.get(r, 0))
    if cur > 0:
        nxt = cur - 1
        if nxt <= 0:
            _ACTIVE_VOBIZ_CALLS_BY_ROLE.pop(r, None)
        else:
            _ACTIVE_VOBIZ_CALLS_BY_ROLE[r] = nxt
    _ACTIVE_VOBIZ_CALLS = total_active_vobiz_calls()


def parse_manual_camp_role_suffix(suffix: str) -> tuple[str, str]:
    """Parse ``role`` and optional per-attempt token from camp_id after the ``manual_`` prefix.

    Formats:
      - ``{role}`` — legacy single shared manual leg id
      - ``{role}_{token}`` — unique id per manual dial (``token`` may contain underscores)
    """
    suf = (suffix or "").strip()
    if not suf:
        return "sales_1", ""
    for r in sorted(_ROLES, key=len, reverse=True):
        if suf == r:
            return r, ""
        # ``manual_{role}_{uuid}`` (current) and legacy ``manual_{role}-{token}`` both map to ``role``.
        for sep in ("_", "-"):
            prefix = r + sep
            if suf.startswith(prefix):
                return r, suf[len(prefix) :]
    return normalize_console_role(suf), ""


def resolved_greeting_text(role: str) -> str:
    """Gre stored in SQLite (coerced); if missing or invalidated, packaged role opener."""
    from core.greeting_text_utils import coerce_stored_greeting

    state = get_state(role)
    raw = state.get("greeting_text") or ""
    text = coerce_stored_greeting(role, raw).strip()
    if text:
        return text
    from core.opening_line import packaged_fallback_greeting

    return packaged_fallback_greeting(role)


def init_state():
    """Initialize campaign tasks for all roles."""
    for role in _ROLES:
        _CAMPAIGN_TASKS[role] = None

def get_state(role: str) -> dict:
    """Get in-memory state for a role (prompt, rag, vobiz config, etc.)."""
    try:
        from core.storage import _get_role_state_sync
        return _get_role_state_sync(role or "sales_1")
    except Exception as e:
        logger.warning(f"Storage not available, using fallback: {e}")
        from core.storage import default_inter_call_gap_sec

        r = (role or "sales_1").strip().lower()
        return {
            "role": r,
            "prompt": "",
            "rag": "",
            "delay_sec": default_inter_call_gap_sec(r),
            "vobiz": {},
        }


def rag_context_for_role(role: str, query_text: str) -> str:
    """Top-k chunked RAG context for one role, formatted as reference text.

    Thin re-export of ``rag.rag_context_for_role`` so callers (e.g. the
    ``services/`` package) can import it from ``core.state`` without touching
    ``rag.py``. Returns ``""`` when RAG is disabled or nothing matches.
    """
    try:
        from rag import rag_context_for_role as _rag_ctx

        return _rag_ctx(role, query_text) or ""
    except Exception:
        logger.warning("rag_context_for_role failed for role={}", role)
        return ""

def save_role_state(
    role: str,
    prompt: str = None,
    rag: str = None,
    vobiz_config: dict = None,
    delay_sec: float = None,
    greeting_text: str = None,
    prompt_parts: dict = None,
):
    """Persist role state to the database."""
    try:
        from core.storage import _save_role_state_sync

        _save_role_state_sync(
            role,
            prompt=prompt,
            rag=rag,
            vobiz_config=vobiz_config,
            delay_sec=delay_sec,
            greeting_text=greeting_text,
            prompt_parts=prompt_parts,
        )
    except Exception as e:
        logger.error(f"Failed to save state for {role}: {e}")

def get_leads(role: str, status: str = None, limit: int = 500) -> list[dict]:
    try:
        from core.storage import _get_leads_sync
        return _get_leads_sync(role, status=status, limit=limit)
    except Exception:
        logger.exception("get_leads failed for role={!r}", role)
        return []

def add_leads_bulk(role: str, leads: list[dict]) -> int:
    from core.storage import _bulk_add_leads_sync

    return _bulk_add_leads_sync(role, leads)

def update_lead_status(lead_id: int, status: str, error: str = None, analysis: dict = None):
    try:
        from core.storage import _update_lead_status_sync
        _update_lead_status_sync(lead_id, status, error=error, analysis=analysis)
    except Exception as e:
        logger.error(f"Failed to update lead status: {e}")

def update_lead_call_info(lead_id: int, log_id: str = None, call_id: str = None, start_time: float = None):
    try:
        from core.storage import _update_lead_call_info_sync
        _update_lead_call_info_sync(lead_id, log_id=log_id, call_id=call_id, start_time=start_time)
    except Exception as e:
        logger.error(f"Failed to update lead call info: {e}")

def reset_leads(role: str):
    try:
        from core.storage import _reset_leads_sync
        _reset_leads_sync(role)
    except Exception as e:
        logger.error(f"Failed to reset leads: {e}")

def wipe_leads(role: str):
    try:
        from core.storage import _wipe_leads_sync
        _wipe_leads_sync(role)
    except Exception as e:
        logger.error(f"Failed to wipe leads: {e}")

def get_lead_counts(role: str) -> dict:
    try:
        from core.storage import _get_lead_counts_sync
        return _get_lead_counts_sync(role)
    except Exception:
        logger.exception("get_lead_counts failed for role={!r}", role)
        return {"total": 0, "pending": 0, "dialing": 0, "completed": 0, "failed": 0, "not_interested": 0}

def export_leads_csv(role: str, status_filter: str = "all") -> list[dict]:
    try:
        from core.storage import _export_leads_csv_sync
        return _export_leads_csv_sync(role, status_filter)
    except Exception:
        return []

from pathlib import Path

def _get_role_path(role: str, subpath: str = None) -> Path:
    from config import settings
    # Assuming standard data directory layout
    base_dir = Path("data") / role
    if subpath:
        base_dir = base_dir / subpath
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


_WHATSAPP_SENT_CALLS: set[str] = set()


def mark_whatsapp_sent_for_call(camp_id: str) -> None:
    """Track that a WhatsApp message was successfully sent during the live call."""
    if camp_id:
        _WHATSAPP_SENT_CALLS.add(camp_id)


def is_whatsapp_sent_for_call(camp_id: str) -> bool:
    """Check if WhatsApp details have already been sent for this call."""
    return camp_id in _WHATSAPP_SENT_CALLS if camp_id else False


_BUSY_PHONE_NUMBERS: set[str] = set()


def phone_is_busy(phone_number: str) -> bool:
    """Check if the given phone number is currently in use for a call."""
    if not phone_number:
        return False
    from core.phone_norm import norm_phone_str
    return norm_phone_str(phone_number) in _BUSY_PHONE_NUMBERS


def acquire_phone_slot(phone_number: str) -> None:
    """Mark a phone number as busy/in use."""
    if not phone_number:
        return
    from core.phone_norm import norm_phone_str
    norm = norm_phone_str(phone_number)
    _BUSY_PHONE_NUMBERS.add(norm)
    logger.info(f"Acquired phone slot: {norm}")


def release_phone_slot(phone_number: str) -> None:
    """Release the phone number from busy state."""
    if not phone_number:
        return
    from core.phone_norm import norm_phone_str
    norm = norm_phone_str(phone_number)
    if norm in _BUSY_PHONE_NUMBERS:
        _BUSY_PHONE_NUMBERS.remove(norm)
        logger.info(f"Released phone slot: {norm}")


# --- Inbound Call Queue Management ---

def enqueue_inbound_call(role: str, from_num: str, from_digits: str, lead_name: str, call_uuid: str) -> int:
    """Add an incoming caller to the queue for a role. Returns queue position (1-indexed)."""
    r = normalize_console_role(role)
    if r not in _INBOUND_CALL_QUEUE:
        _INBOUND_CALL_QUEUE[r] = []
    entry = {
        "from_num": from_num,
        "from_digits": from_digits,
        "lead_name": lead_name,
        "call_uuid": call_uuid,
    }
    _INBOUND_CALL_QUEUE[r].append(entry)
    pos = len(_INBOUND_CALL_QUEUE[r])
    logger.info(f"Queued inbound call for role={r} from={from_num} position={pos}")
    return pos


def dequeue_inbound_call(role: str) -> Optional[dict[str, str]]:
    """Remove and return the next queued caller for a role, or None if queue is empty."""
    r = normalize_console_role(role)
    queue = _INBOUND_CALL_QUEUE.get(r, [])
    if not queue:
        return None
    entry = queue.pop(0)
    logger.info(f"Dequeued inbound call for role={r} from={entry.get('from_num')}")
    return entry


def get_inbound_queue_length(role: str) -> int:
    """Return the number of callers waiting in the queue for a role."""
    r = normalize_console_role(role)
    return len(_INBOUND_CALL_QUEUE.get(r, []))


def clear_inbound_queue(role: str) -> int:
    """Clear all queued callers for a role. Returns count cleared."""
    r = normalize_console_role(role)
    count = len(_INBOUND_CALL_QUEUE.get(r, []))
    _INBOUND_CALL_QUEUE[r] = []
    logger.info(f"Cleared inbound queue for role={r}, removed {count} callers")
    return count


async def process_inbound_queue(role: str) -> bool:
    """Check if there are queued inbound calls to dial. If the role is free,
    dial the next queued caller. Returns True if a call was initiated."""
    r = normalize_console_role(role)
    queue = _INBOUND_CALL_QUEUE.get(r, [])
    if not queue:
        return False

    # Check if role is currently busy
    if role_has_active_vobiz_call(r):
        logger.info(f"Role {r} still busy, {len(queue)} callers waiting in queue")
        return False

    # Role is free — dequeue and dial the next caller
    entry = dequeue_inbound_call(r)
    if not entry:
        return False

    from_num = entry.get("from_num", "")
    from_digits = entry.get("from_digits", "")
    lead_name = entry.get("lead_name", "")
    call_uuid = entry.get("call_uuid", "")

    logger.info(f"Processing queued inbound call: role={r} from={from_num} name={lead_name}")

    # Build camp_id for this queued call
    camp_id = f"queued_{r}_{from_digits}_{call_uuid}"

    # Acquire a slot so the role is marked busy
    acquire_vobiz_call_slot(r)

    # Get the outbound phone number for this role
    try:
        from core.outbound_numbers import get_next_outbound_number_for_role
        from core.storage import get_role_state
        state = await get_role_state(r)
        vobiz_config = state.get("vobiz", {})
        outbound_number = get_next_outbound_number_for_role(r, vobiz_config)
    except Exception as e:
        logger.error(f"Failed to get outbound number for role={r}: {e}")
        release_vobiz_call_slot(r)
        return False

    if not outbound_number:
        logger.error(f"No outbound number configured for role={r}")
        release_vobiz_call_slot(r)
        return False

    # Mark the phone as busy
    acquire_phone_slot(outbound_number)

    # Build the WebSocket URL for the queued call
    from config import settings
    explicit_stream = (settings.vobiz_stream_public_base_url or "").strip().rstrip("/")
    wss_base = explicit_stream
    if not wss_base:
        wss_base = settings.server_url.rstrip("/")
    if not wss_base:
        wss_base = (settings.vobiz_public_base_url or "").rstrip("/")
    wss_url = wss_base.replace("https://", "wss://").replace("http://", "ws://") + "/ws/vobiz"
    wss_url += f"?camp_id={camp_id}&lead_name={lead_name}"

    # Create a campaign data entry so the system treats this as an outbound call
    _CAMPAIGN_DATA[camp_id] = {
        "_role": r,
        "_manual_leg": True,
        "phone": from_digits,
        "name": lead_name or "Unknown",
        "_outbound_phone": outbound_number,
        "_queued_call": True,
    }

    # Get Vobiz credentials and dial
    try:
        from core.vobiz_credentials import resolve_vobiz_credentials
        auth_id, auth_token, _, v_base = resolve_vobiz_credentials(r, vobiz_config)
    except Exception as e:
        logger.error(f"Failed to resolve Vobiz credentials for role={r}: {e}")
        release_vobiz_call_slot(r)
        release_phone_slot(outbound_number)
        if camp_id in _CAMPAIGN_DATA:
            del _CAMPAIGN_DATA[camp_id]
        return False

    if not auth_id or not auth_token or not v_base or not outbound_number:
        logger.error(f"Vobiz not fully configured for role={r}")
        release_vobiz_call_slot(r)
        release_phone_slot(outbound_number)
        if camp_id in _CAMPAIGN_DATA:
            del _CAMPAIGN_DATA[camp_id]
        return False

    answer_url = f"{v_base}/vobiz/answer?camp_id={camp_id}&role={r}"

    sem_acquired = False
    try:
        from core.worker import _GLOBAL_CALL_SEMAPHORE
        await _GLOBAL_CALL_SEMAPHORE.acquire()
        sem_acquired = True
        from services.vobiz_bridge import make_vobiz_call
        await make_vobiz_call(
            to=from_digits,
            from_=outbound_number,
            answer_url=answer_url,
            auth_id=auth_id,
            auth_token=auth_token,
        )
        logger.info(f"Successfully completed queued call: camp_id={camp_id} to={from_digits}")
        return True
    except Exception as e:
        logger.error(f"Failed to initiate queued call for role={r} to={from_digits}: {e}")
        return False
    finally:
        if sem_acquired:
            await asyncio.sleep(1.0)
            _GLOBAL_CALL_SEMAPHORE.release()
        release_vobiz_call_slot(r)
        release_phone_slot(outbound_number)
        if camp_id in _CAMPAIGN_DATA:
            del _CAMPAIGN_DATA[camp_id]

