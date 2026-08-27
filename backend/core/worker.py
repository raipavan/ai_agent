"""Campaign worker — dials leads one-at-a-time per role; roles run in parallel."""

from __future__ import annotations
import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from loguru import logger
from core import kv_cache
from core.state import (
    _CAMPAIGN_DATA,
    _CAMPAIGN_TASKS,
    _LAST_WORKER_ACTIVITY,
    acquire_vobiz_call_slot,
    release_vobiz_call_slot,
    role_has_active_vobiz_call,
    active_vobiz_calls_for_role,
    total_active_vobiz_calls,
    get_state,
    normalize_console_role,
    phone_is_busy,
    acquire_phone_slot,
    release_phone_slot,
)
from core.storage import (
    due_schedules,
    expired_running_schedules,
    mark_schedule_status,
    promote_due_scheduled_callbacks,
    role_has_future_callback_scheduled,
    get_leads,
    update_lead_status,
    update_lead_call_info,
    save_role_state,
    reset_leads,
    wipe_leads,
    get_lead_counts,
    export_leads_csv,
    set_campaign_want_running,
    get_next_immediate_callback,
    update_scheduled_callback_status,
    is_duplicate_lead,
)
from core.state import add_leads_bulk
from core.utils import _build_opening_line
from config import settings
from core.campaign_hours import is_campaign_quiet_hours, quiet_hours_block_message
from core.vobiz_credentials import resolve_vobiz_credentials
from core.outbound_numbers import get_all_outbound_numbers

_background_tasks: set[asyncio.Task] = set()
_callback_tasks_in_flight: set[str] = set()  # roles with an active callback task in scheduler loop
_GLOBAL_CALL_SEMAPHORE = asyncio.Semaphore(6)
_ROLE_SEMAPHORES = {
    "sales_1": asyncio.Semaphore(2),
    "sales_2": asyncio.Semaphore(2),
}

_ALTERNATING_ACTIVE_ROLE = "sales_1"
_LAST_TURN_SWITCH_TIME = 0.0

def yield_alternating_turn(role: str):
    """Explicitly yield the alternating turn to the other sales campaign."""
    global _ALTERNATING_ACTIVE_ROLE, _LAST_TURN_SWITCH_TIME
    r = (role or "").strip().lower()
    if r in ("sales_1", "sales_2"):
        other_role = "sales_2" if r == "sales_1" else "sales_1"
        if _ALTERNATING_ACTIVE_ROLE == r:
            _ALTERNATING_ACTIVE_ROLE = other_role
            _LAST_TURN_SWITCH_TIME = time.time()
            logger.info(f"Alternating turn: {r} yielded turn to {other_role}.")

async def check_and_acquire_alternating_turn(role: str) -> bool:
    """Coordinated turn-taking: returns True if it's our turn to dial, False if we must wait."""
    global _ALTERNATING_ACTIVE_ROLE, _LAST_TURN_SWITCH_TIME
    
    r = (role or "").strip().lower()
    if r not in ("sales_1", "sales_2"):
        return True
        
    if _ALTERNATING_ACTIVE_ROLE == r:
        return True
        
    # We want to check if we can take over the turn
    other_role = "sales_2" if r == "sales_1" else "sales_1"
    
    # Condition 1: If other campaign is not active or is manually stopped, take over immediately
    other_task = _CAMPAIGN_TASKS.get(other_role)
    other_running = other_task is not None and not other_task.done()
    
    from core.state import _MANUALLY_STOPPED_ROLES
    other_stopped = other_role in _MANUALLY_STOPPED_ROLES
    
    if not other_running or other_stopped:
        _ALTERNATING_ACTIVE_ROLE = r
        _LAST_TURN_SWITCH_TIME = time.time()
        logger.info(f"Alternating turn: {r} taking over because {other_role} is not running or stopped.")
        return True
        
    # Condition 2: Safety net. If other campaign is running but has had 0 active calls 
    # for a long time (e.g. it went idle without yielding properly, or got stuck), 
    # we can steal the turn after a timeout.
    other_active_calls = active_vobiz_calls_for_role(other_role)
    if other_active_calls == 0:
        now = time.time()
        time_elapsed = now - _LAST_TURN_SWITCH_TIME
        if time_elapsed > 30.0:
            _ALTERNATING_ACTIVE_ROLE = r
            _LAST_TURN_SWITCH_TIME = now
            logger.info(f"Alternating turn: {r} stealing turn from {other_role} after idle timeout ({time_elapsed:.1f}s).")
            return True
            
    return False





# Once a lead is in ``dialing`` longer than this (process restart or hung WS), recycle it.
_STALE_DIALING_AFTER_SEC = 600
# Wait time when the queue becomes empty before exiting (gives the operator a chance to upload mid-run).
_EMPTY_QUEUE_GRACE_SEC = 30
# Fallback gap (seconds) between consecutive outbound calls if role_state.delay_sec is missing/invalid.
# Note: default gap is now random 120-180s in inter_call_gap_seconds_for_role();
# these are only used when an explicit delay_sec is set in role state.
_ENV_INTER_CALL_GAP_SEC = float(os.getenv("CAMPAIGN_INTER_CALL_GAP_SEC", "120"))
_INTER_CALL_GAP_MIN = 0.0
_INTER_CALL_GAP_MAX = 1200.0  # 20 min cap

# Round-robin phone number state per role
# Tracks: {role: {"phone_index": int, "hour_start": float, "total_calls_this_hour": int}}
_PHONE_ROUND_ROBIN_STATE: dict[str, dict] = {}
# Maximum calls per phone number before rotating to next number
_CALLS_PER_PHONE_MIN = 1
_CALLS_PER_PHONE_MAX = 1
# Maximum total calls per hour per role
_MAX_CALLS_PER_HOUR = 1000
# Maximum calls per upload source per day (IST midnight boundary)
_CALLS_PER_SOURCE_DAILY_MAX = 5000


def inter_call_gap_seconds_for_role(role: str) -> float:
    """Pause after each dial before the next pending lead."""
    import random as _random
    role_key = (role or "sales_1").strip().lower()
    # Pause between calls: ~3 minutes (175-185s) randomized
    val = _random.uniform(175.0, 185.0)
    logger.info(f"Generated dynamic randomized inter-call gap of {val:.2f}s for role={role_key}")
    return val


async def inter_call_gap_seconds_for_phone(phone_number: str, role: str) -> float:
    """Dynamically determine pause after each dial for a specific outbound line depending on recent connectivity."""
    import random as _random
    import time
    import json
    from core.storage import get_recent_call_outcomes_for_phone, get_recent_call_outcomes_for_role
    
    now = time.time()
    since_1h = now - 3600
    since_30m = now - 1800
    
    # 1. Strict Rate Limiting: 24 to 28 calls per hour per number
    try:
        recent_phone_calls = await get_recent_call_outcomes_for_phone(phone_number, since_1h)
    except Exception as e:
        logger.exception("Failed to query recent call outcomes for phone={}", phone_number)
        recent_phone_calls = []
        
    total_phone_calls = len(recent_phone_calls)
    
    if total_phone_calls >= 26:
        val = _random.uniform(180.0, 240.0)
        logger.info(f"Pacing: Rate ceiling reached ({total_phone_calls} calls/hour on {phone_number}). Enforcing cooldown gap: {val:.1f}s")
        return val
    elif total_phone_calls >= 23:
        val = _random.uniform(70.0, 100.0)
        logger.info(f"Pacing: Near rate ceiling ({total_phone_calls} calls/hour on {phone_number}). Moderate cooldown gap: {val:.1f}s")
        return val
        
    # 2. Query recent calls for the role in the last hour
    try:
        recent_role_calls_1h = await get_recent_call_outcomes_for_role(role, since_1h)
    except Exception as e:
        logger.exception("Failed to query recent call outcomes for role={}", role)
        recent_role_calls_1h = []
        
    connected_calls_1h = 0
    failed_calls_30m = 0
    total_calls_30m = 0
    
    for call in recent_role_calls_1h:
        status = (call.get("status") or "").lower()
        analysis = call.get("analysis") or {}
        if isinstance(analysis, str) and analysis.strip():
            try:
                analysis = json.loads(analysis)
            except Exception:
                analysis = {}
        
        disposition = (analysis.get("disposition") or "").lower()
        call_time = call.get("start_time") or 0
        
        # Check 30m window
        is_30m = (now - call_time) <= 1800
        if is_30m:
            total_calls_30m += 1
            if status in ("failed", "busy", "no answer", "no response", "no_response") or disposition in ("failed", "no answer", "busy", "wrong number", "not available", "no response"):
                failed_calls_30m += 1
                
        # Count genuinely connected calls in 1h (excluding voicemail and no-response)
        if status == "completed" or disposition in ("interested", "callback", "site_visit", "not_interested"):
            if disposition not in ("no response", "voice mail", "voicemail", ""):
                connected_calls_1h += 1
 
    # Dynamic gap based on target calls/hour for the entire role
    try:
        from core.state import get_state
        role_config = get_state(role) or {}
    except Exception:
        role_config = {}
        
    v_cfg = role_config.get("vobiz", {}) or {}
    from core.outbound_numbers import get_all_outbound_numbers
    numbers = get_all_outbound_numbers(role, v_cfg)
    num_lines = max(1, len(numbers))
    
    # Calculate poor connectivity: 3 or more failures in the last 30 minutes
    is_poor_connectivity = failed_calls_30m >= 3 or (total_calls_30m >= 3 and failed_calls_30m / total_calls_30m >= 0.6)
    
    from config import settings
    
    if settings.fast_dialing:
        # High-speed dialing: return direct randomized low gap (30-50s) to keep calls smooth
        val = _random.uniform(150.0, 180.0)
        logger.info(f"Pacing: fast-dialing active, but smoothed under ceiling. Set direct gap to {val:.2f}s for phone={phone_number}")
        return val
    elif is_poor_connectivity:
        # Moderate speed: ~5-12s gap
        target_total = 300.0
        cycle_time = (3600.0 * num_lines) / target_total
        avg_duration = 10.0
        base_gap = max(3.0, cycle_time - avg_duration)
        gap_min = 3.0
        gap_max = min(12.0, base_gap + 6.0)
        label = f"poor-connectivity (target {int(target_total)} calls/h, lines={num_lines})"
    else:
        # Normal speed: ~4-10s gap
        target_total = 400.0
        cycle_time = (3600.0 * num_lines) / target_total
        avg_duration = 8.0
        base_gap = max(2.0, cycle_time - avg_duration)
        gap_min = 2.0
        gap_max = min(10.0, base_gap + 5.0)
        label = f"good-connectivity (target {int(target_total)} calls/h, lines={num_lines})"
        
    val = _random.uniform(gap_min, gap_max)
    logger.info(
        f"Pacing: dynamic inter-call gap of {val:.2f}s for phone={phone_number} (role={role}) "
        f"({label}, 1h connected={connected_calls_1h}, 30m failed={failed_calls_30m}, 30m total={total_calls_30m})"
    )
    return val


def get_next_phone_number(role: str, vobiz_cfg: dict) -> str:
    """Get the next phone number to use for dialing (alternate after every call)."""
    
    numbers = get_all_outbound_numbers(role, vobiz_cfg)
    if not numbers:
        # Fallback to single number resolution
        auth_id, auth_token, from_number, public_url = resolve_vobiz_credentials(role, vobiz_cfg)
        return from_number
    
    if len(numbers) == 1:
        return numbers[0]
    
    now = time.time()
    state = _PHONE_ROUND_ROBIN_STATE.get(role, {})
    
    # Initialize state if needed
    if not state:
        state = {
            "phone_index": 0,
            "calls_on_current_phone": 0,
            "hour_start": now,
            "total_calls_this_hour": 0,
        }
        _PHONE_ROUND_ROBIN_STATE[role] = state
    
    # Check if an hour has passed since we started tracking
    hour_elapsed = now - state.get("hour_start", now)
    if hour_elapsed >= 3600:
        # Reset hourly counters
        state["hour_start"] = now
        state["total_calls_this_hour"] = 0
        state["phone_index"] = 0
        state["calls_on_current_phone"] = 0
    
    # Check if we've exceeded hourly limit
    if state["total_calls_this_hour"] >= _MAX_CALLS_PER_HOUR:
        logger.info(f"Hourly call limit ({_MAX_CALLS_PER_HOUR}) reached for {role}")
        return numbers[state.get("phone_index", 0) % len(numbers)]
    
    # Get current phone number
    phone_index = state.get("phone_index", 0) % len(numbers)
    selected_number = numbers[phone_index]
    
    # Increment calls on current phone
    state["calls_on_current_phone"] = state.get("calls_on_current_phone", 0) + 1
    
    # Check if we've exceeded max calls for this phone number
    max_for_phone = _CALLS_PER_PHONE_MAX
    if state["calls_on_current_phone"] >= max_for_phone:
        # Rotate to next phone number
        next_index = (phone_index + 1) % len(numbers)
        state["phone_index"] = next_index
        state["calls_on_current_phone"] = 0
        logger.info(f"Rotating phone number: {phone_index + 1} -> {next_index + 1} after {max_for_phone} calls")
    else:
        # Keep same phone number for next call
        pass
    
    # Increment hourly counter
    state["total_calls_this_hour"] = state.get("total_calls_this_hour", 0) + 1
    
    logger.info(f"Selected phone {phone_index + 1} for {role}, next will be phone {state['phone_index'] + 1}")
    
    return selected_number


def get_next_free_phone_number(role: str, vobiz_cfg: dict) -> str:
    """Round-robin pick that skips phone numbers currently held by the busy guard.

    Returns the first free line starting from the round-robin cursor; advances
    the cursor past it. Returns "" when every line for the role is busy.
    """
    from core.state import phone_is_busy

    numbers = get_all_outbound_numbers(role, vobiz_cfg)
    if not numbers:
        _, _, from_number, _ = resolve_vobiz_credentials(role, vobiz_cfg)
        return from_number

    now = time.time()
    state = _PHONE_ROUND_ROBIN_STATE.get(role, {})
    if not state:
        state = {
            "phone_index": 0,
            "calls_on_current_phone": 0,
            "hour_start": now,
            "total_calls_this_hour": 0,
        }
        _PHONE_ROUND_ROBIN_STATE[role] = state

    hour_elapsed = now - state.get("hour_start", now)
    if hour_elapsed >= 3600:
        state["hour_start"] = now
        state["total_calls_this_hour"] = 0
        state["phone_index"] = 0
        state["calls_on_current_phone"] = 0

    start = int(state.get("phone_index", 0) or 0) % len(numbers)
    for i in range(len(numbers)):
        idx = (start + i) % len(numbers)
        candidate = numbers[idx]
        if candidate and not phone_is_busy(candidate):
            state["phone_index"] = (idx + 1) % len(numbers)
            state["calls_on_current_phone"] = 0
            state["total_calls_this_hour"] = state.get("total_calls_this_hour", 0) + 1
            logger.info(f"Selected free phone {idx + 1} ({candidate}) for {role}")
            return candidate
    logger.warning(f"All phone lines busy for {role}; no free number available")
    return ""


async def _cancellable_sleep(role: str, total_seconds: float) -> bool:
    """Sleep in 0.5s slices but bail out as soon as the campaign is stopped.

    Returns True if the wait completed normally, False if the campaign was cancelled.
    """
    end = time.time() + max(0.0, total_seconds)
    while time.time() < end:
        if not _CAMPAIGN_TASKS.get(role):
            return False
        await asyncio.sleep(min(0.5, end - time.time()))
    return True


async def release_orphaned_dialing_leads(
    role: str,
    *,
    to_status: str = "failed",
    error: str = "Campaign stopped before call completed.",
) -> int:
    """Mark in-flight ``dialing`` rows terminal when the worker is not running (stop / quiet hours)."""
    try:
        rows = await get_leads(role, status="dialing", limit=10000)
    except Exception:
        logger.exception("Failed to release orphaned dialing leads role={}", role)
        return 0
    released = 0
    for r in rows:
        try:
            await update_lead_status(int(r["id"]), to_status, error=error)
            released += 1
        except Exception:
            logger.exception("release dialing lead id={}", r.get("id"))
    if released:
        logger.info(
            "Released {} orphaned dialing lead(s) → {} for role={}",
            released,
            to_status,
            role,
        )
    return released


async def _recover_stale_dialing(role: str) -> int:
    """Worker startup: previous process may have crashed with leads stuck on ``dialing``.

    Reset them to ``pending`` so this run can pick them up. Returns count recovered.
    """
    try:
        rows = await get_leads(role, status="dialing", limit=10000)
    except Exception:
        logger.exception("Failed to recover stale dialing leads")
        return 0
    recovered = 0
    for r in rows:
        await update_lead_status(r["id"], "pending")
        recovered += 1
    if recovered:
        logger.info(f"Recovered {recovered} stale 'dialing' leads → 'pending' for role={role}")
    return recovered



async def _prime_opening_audio(call_id: str, role: str, opening: str) -> None:
    """Pre-load (or generate from Gemini 3.1 Flash) the opening greeting PCM before
    dialing, so playback is ready the instant the WebSocket opens.

    Uses ``core.greeting_pcm.prewarm_opening`` which loads the cached
    ``greeting_{role}.pcm`` when the greeting text still matches, otherwise
    captures it fresh via Gemini 3.1 Flash (same model/voice as the call).
    """
    if settings.gemini_live_first_opening:
        logger.debug(
            "Skip opening PCM prime for call_id={} — Gemini Live speaks first",
            call_id,
        )
        return
    if call_id not in _CAMPAIGN_DATA:
        return
    try:
        from core.greeting_pcm import prewarm_opening

        await prewarm_opening(call_id, (opening or "").strip(), (settings.gemini_live_voice or "Leda").strip())
    except Exception as exc:
        logger.warning("Opening PCM prime failed for call_id={}: {}", call_id, exc)


async def _execute_scheduled_callback(role: str, cb: dict, outbound_phone: str = None) -> None:
    """Execute a single scheduled callback immediately.

    Reuses the same ``make_vobiz_call`` + ``_CAMPAIGN_DATA`` infrastructure
    as normal campaign leads. Returns when the call completes or fails.

    Creates a lead record so the transcript + analysis are saved and visible
    in the dashboard like any other campaign call.
    """
    from services.vobiz_bridge import make_vobiz_call, VobizCallError

    cb_id = int(cb["id"])
    cb_phone = cb.get("phone", "")
    cb_name = cb.get("name", "") or "Callback"

    if not cb_phone:
        await update_scheduled_callback_status(cb_id, "failed", error="No phone number")
        return

    await update_scheduled_callback_status(cb_id, "calling")

    state = get_state(role)
    v_cfg = state.get("vobiz", {}) or {}
    v_auth_id, v_token, v_from, v_base = resolve_vobiz_credentials(role, v_cfg)
    if outbound_phone:
        v_from = outbound_phone

    if not v_auth_id or not v_token or not v_base or not v_from:
        await update_scheduled_callback_status(cb_id, "failed", error="Telephony not configured")
        return

    # Create (or reuse) a lead record so the dashboard shows this callback call
    # and auto-analysis (transcript + rating) is triggered.  Reuses an existing
    # lead row with ``callback_scheduled`` status to avoid duplicate rows.
    try:
        from core.storage import find_or_create_callback_lead as _find_or_create_cb, update_lead_call_info as _cb_call_info
        lead_id = await _find_or_create_cb(role, phone=cb_phone, name=cb_name)
        await update_lead_status(lead_id, "dialing")
        await _cb_call_info(lead_id, start_time=time.time(), outbound_phone=v_from)
        logger.info(f"Callback lead ready: id={lead_id} phone={cb_phone}")
    except Exception as e:
        logger.exception(f"Failed to create callback lead for {cb_phone}")
        lead_id = None

    call_id = f"sched_cb_{role}_{cb_id}_{uuid.uuid4().hex[:8]}"
    _CAMPAIGN_DATA[call_id] = {
        "name": cb_name,
        "phone": cb_phone,
        "_role": role,
        "_scheduled_callback_id": cb_id,
        "_is_scheduled_callback": True,
    }
    if lead_id is not None:
        _CAMPAIGN_DATA[call_id]["_lead_id"] = lead_id
        _CAMPAIGN_DATA[call_id]["id"] = lead_id

    from core.dnc import is_phone_blocked
    if is_phone_blocked(cb_phone):
        logger.warning(f"Aborting scheduled callback dialing: {cb_phone} is in DNC list")
        await update_scheduled_callback_status(cb_id, "failed", error="Phone number is blocked (DNC)")
        if lead_id is not None:
            await update_lead_status(lead_id, "failed", error="Phone number is blocked (DNC)")
        return

    slot_acquired = False
    sem_acquired = False
    try:
        await _GLOBAL_CALL_SEMAPHORE.acquire()
        sem_acquired = True

        opening = _build_opening_line(_CAMPAIGN_DATA[call_id], role)
        await _prime_opening_audio(call_id, role, opening)

        acquire_vobiz_call_slot(role)
        slot_acquired = True
        logger.info(f"Scheduled callback call: {cb_name} ({cb_phone})")

        try:
            await make_vobiz_call(
                to=cb_phone,
                from_=v_from,
                answer_url=f"{v_base}/vobiz/answer?camp_id={call_id}&role={role}",
                auth_id=v_auth_id,
                auth_token=v_token,
            )
        except VobizCallError as ve:
            await update_scheduled_callback_status(
                cb_id, "failed", error=f"Vobiz {ve.status}: {ve.message}"
            )
            if lead_id is not None:
                await update_lead_status(lead_id, "failed", error=f"Vobiz {ve.status}: {ve.message}")
                if role in ("sales_1", "sales_2"):
                    try:
                        from core.storage import get_lead_whatsapp_sent, mark_whatsapp_sent
                        from services.whatsapp_leads import send_whatsapp_project_details
                        if not await get_lead_whatsapp_sent(lead_id):
                            logger.info("Callback call failed (VobizCallError) — sending WhatsApp details for lead {}", lead_id)
                            wa_result = await send_whatsapp_project_details(cb_phone, summary="Following up with details.", lead_name=cb_name)
                            if wa_result.get("sent"):
                                await mark_whatsapp_sent(lead_id)
                    except Exception as wa_err:
                        logger.exception("Failed to send WhatsApp for failed callback call: {}", wa_err)
            return

        answered = False
        call_started_at = time.time()
        MAX_RING_WAIT = 60
        MAX_TOTAL_WAIT = 360

        while True:
            info = _CAMPAIGN_DATA.get(call_id, {})
            if not answered and info.get("_call_connected_at"):
                answered = True
                logger.info(f"Scheduled callback connected: {cb_name} ({cb_phone})")
            if answered and info.get("_call_ended_at"):
                logger.info(f"Scheduled callback ended: {cb_name}")
                break

            elapsed = time.time() - call_started_at
            if not answered and elapsed >= MAX_RING_WAIT:
                logger.warning(f"Scheduled callback no answer: {cb_phone}")
                break
            if elapsed >= MAX_TOTAL_WAIT:
                logger.warning(f"Scheduled callback exceeded max time: {cb_phone}")
                break

            await asyncio.sleep(2)

        if not answered and lead_id is not None:
            await update_lead_status(lead_id, "failed", error="No answer / Timeout")
            if role in ("sales_1", "sales_2"):
                try:
                    from core.storage import get_lead_whatsapp_sent, mark_whatsapp_sent
                    from services.whatsapp_leads import send_whatsapp_project_details
                    if not await get_lead_whatsapp_sent(lead_id):
                        logger.info("Callback call failed (No answer / Timeout) — sending WhatsApp details for lead {}", lead_id)
                        wa_result = await send_whatsapp_project_details(cb_phone, summary="Following up with details.", lead_name=cb_name)
                        if wa_result.get("sent"):
                            await mark_whatsapp_sent(lead_id)
                except Exception as wa_err:
                    logger.exception("Failed to send WhatsApp for failed callback call: {}", wa_err)

        await update_scheduled_callback_status(
            cb_id,
            "completed" if answered else "failed",
            error=None if answered else "No answer / Timeout",
        )

        # Mark original lead as callback_completed so it doesn't get re-dialed
        if answered and cb.get("lead_id"):
            try:
                await update_lead_status(cb["lead_id"], "callback_completed")
            except Exception:
                logger.warning(f"Failed to mark original lead {cb['lead_id']} as callback_completed")
        elif not answered and cb.get("lead_id"):
            try:
                orig_lead_id = cb["lead_id"]
                await _schedule_failed_call_retry(role, orig_lead_id, cb_phone, cb_name, reason="no_answer")
            except Exception as re:
                logger.exception("Failed to schedule retry for failed callback lead {}", cb.get("lead_id"))

    except Exception as e:
        logger.exception(f"Scheduled callback failed for {cb_phone}")
        await update_scheduled_callback_status(cb_id, "failed", error=str(e)[:300])
    finally:
        if slot_acquired:
            release_vobiz_call_slot(role)
        _CAMPAIGN_DATA.pop(call_id, None)
        if sem_acquired:
            await asyncio.sleep(1.0)
            _GLOBAL_CALL_SEMAPHORE.release()


def _parse_log_id_date(log_id: str) -> str | None:
    """Extract YYYY-MM-DD from log_id patterns like camp-xxx-20260513T07291 or vobiz-live-20260518T161022-xxx."""
    import re
    m = re.search(r"(\d{4})(\d{2})(\d{2})T", log_id)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _read_transcript_jsonl(role: str, log_id: str) -> str:
    """Locate the JSONL transcript for a log_id and return its raw text.

    Scans the per-role ``data/<role>/logs/`` tree in both current and legacy
    systems. Parses the date from the log_id for exact-day lookup, then falls
    back to recent days. Returns empty string if nothing is found.
    """
    from datetime import datetime, timedelta, timezone

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    project_root = os.path.dirname(backend_dir)
    candidate_dirs: list[str] = []

    def _add_log_dir(base: str, day: str) -> None:
        for sub in (os.path.join(base, role, "logs", day), os.path.join(base, "logs", day)):
            if sub not in candidate_dirs:
                candidate_dirs.append(sub)

    # Date-prefixed lookup: extract date from log_id like camp-xxx-20260513T07291
    date_hint = _parse_log_id_date(log_id)
    if date_hint:
        _add_log_dir(os.path.join(backend_dir, "data"), date_hint)
        _add_log_dir(os.path.join(project_root, "data"), date_hint)
        # Conversation logs (turn-by-turn JSONL from live session)
        conv_base = Path(settings.conversation_log_dir)
        if not conv_base.is_absolute():
            conv_base = Path(backend_dir) / conv_base
        candidate_dirs.append(str(conv_base / date_hint))
        for legacy_base in (
            "/root/vernika/backend/data",
            "/root/vernika/agent/data",
            "/root/DataEdge/backend/data",
        ):
            _add_log_dir(legacy_base, date_hint)

    # Fallback: scan recent days across all known log trees (60d for older campaigns)
    today = datetime.now(timezone.utc).date()
    conv_base = Path(settings.conversation_log_dir)
    if not conv_base.is_absolute():
        conv_base = Path(backend_dir) / conv_base
    for delta in range(0, 60):
        d = (today - timedelta(days=delta)).isoformat()
        _add_log_dir(os.path.join(backend_dir, "data"), d)
        _add_log_dir(os.path.join(project_root, "data"), d)
        candidate_dirs.append(str(conv_base / d))
        for legacy_base in (
            "/root/vernika/backend/data",
            "/root/vernika/agent/data",
        ):
            _add_log_dir(legacy_base, d)

    for d in candidate_dirs:
        for ext in ("jsonl", "txt"):
            p = os.path.join(d, f"{log_id}.{ext}")
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        return f.read()
                except OSError:
                    continue
    return ""


def _disposition_to_status(disposition: str) -> str:
    """Map analyzer disposition → lead status the dashboard expects.

    Dispositions are normalised via ``canonical_disposition`` so punctuation,
    synonyms, and minor model rephrasings map deterministically.
    """
    from services.call_analyzer import canonical_disposition

    canon = canonical_disposition(disposition)
    if canon == "Interested":
        return "completed"
    if canon == "Not Interested":
        return "not_interested"
    if canon == "Wrong Number":
        return "failed"
    if canon == "No Answer":
        return "failed"
    if canon == "Busy":
        return "busy"
    # Call Later, Answered, anything unknown → successful connection bucket
    return "completed"


async def _analyze_and_update_lead(role: str, lead_id: int, log_id: str, callback_id: int | None = None):
    """Read the call's transcript, analyze it, and finalize the lead status.

    Writes terminal statuses — including ``callback_scheduled`` when the callee asks
    to be recalled at a specific future time parsed from QA (campaign promotes to
    ``pending`` when that moment passes).

    If ``callback_id`` is provided (scheduled callback), write the outcome back
    to the ``scheduled_callbacks`` row so the dashboard shows it.
    """
    # Heartbeat: stamp that this role's worker is still alive and processing calls.
    _LAST_WORKER_ACTIVITY[role] = time.time()
    if not log_id:
        logger.warning(f"Analyze: no log_id for lead {lead_id}; marking completed.")
        await update_lead_status(lead_id, "completed")
        return

    duration_sec = None
    try:
        from core.worker import _CAMPAIGN_DATA
        if log_id in _CAMPAIGN_DATA:
            duration_sec = _CAMPAIGN_DATA[log_id].get("call_duration_sec")
    except Exception:
        pass

    # Ensure _log_id is persisted on the lead row so transcript/recording
    # lookups resolve correctly (live_session.py sets it on connect, but this
    # provides a fallback for edge cases).
    extra = {}
    if lead_id is not None:
        try:
            from core.storage import get_lead
            lead_row = await get_lead(role, lead_id)
            if lead_row:
                raw_extra = lead_row.get("extra")
                if raw_extra:
                    extra = json.loads(raw_extra) if isinstance(raw_extra, str) else raw_extra
        except Exception as e:
            logger.warning("Failed to load lead extra info for lead_id={}: {}", lead_id, e)

    try:
        await update_lead_call_info(lead_id, log_id=log_id)
    except Exception:
        logger.warning("Analyze: failed to persist log_id for lead {}", lead_id)

    # ── Voicemail override check ──
    is_voicemail_flag = False
    try:
        from core.worker import _CAMPAIGN_DATA
        if log_id in _CAMPAIGN_DATA:
            is_voicemail_flag = bool(_CAMPAIGN_DATA[log_id].get("is_voicemail"))
    except Exception:
        pass

    if is_voicemail_flag:
        logger.info(f"Lead {lead_id} call marked as Voicemail early in session — bypass LLM.")
        await update_lead_status(
            lead_id,
            status="failed",
            analysis={
                "summary": "Call went to voicemail / answering machine.",
                "rating": 0,
                "disposition": "Voice Mail",
                "emotion_label": "Unknown",
                "emotion_rationale": "Answering machine greeting matched.",
                "emotion_confidence": None,
                "site_visit_agreed": False,
                "requested_callback_datetime_iso": None,
                "preferred_location": None,
                "preferred_budget": None,
                "email_address": None,
            },
        )
        return

    # Transcript resolution order:
    #   1) live WS transcript held in _CAMPAIGN_DATA (instant, no API cost)
    #   2) offline audio transcription of the saved recording
    #   3) JSONL live_session file (legacy inbound flow)
    transcript = ""
    try:
        from core.state import _CAMPAIGN_DATA as _CD

        live_t = (_CD.get(log_id) or {}).get("_transcript_text") or ""
        if str(live_t).strip():
            transcript = str(live_t)
            logger.info("Using live WS transcript for lead {} ({} chars)", lead_id, len(transcript))
    except Exception as e:
        logger.warning("Live transcript lookup failed for lead {}: {}", lead_id, e)

    if not (transcript or "").strip():
        try:
            from services.transcriber import transcribe_audio
            from services.call_recording import resolve_session_recording_path

            rec_path = None
            try:
                rec_path = resolve_session_recording_path(log_id)
            except Exception:
                rec_path = None
            if rec_path and rec_path.is_file():
                transcribed = await transcribe_audio(wav_path=str(rec_path))
                if transcribed:
                    transcript = transcribed
                    logger.info("Audio transcription successful for lead {}", lead_id)
        except Exception as e:
            logger.warning("Audio transcription failed for lead {}: {}", lead_id, e)

    # Fall back to JSONL live transcript only if both sources failed
    if not (transcript or "").strip():
        transcript = _read_transcript_jsonl(role, log_id)
        if (transcript or "").strip():
            logger.info("Falling back to JSONL transcript for lead {}", lead_id)

    if not (transcript or "").strip():
        logger.info(f"No transcript for lead {lead_id} (log_id={log_id})")
        analysis_dict = {"summary": "Call connected; transcript unavailable.", "rating": 0, "disposition": "Answered"}
        if duration_sec is not None:
            analysis_dict["duration"] = duration_sec
        await update_lead_status(
            lead_id,
            status="completed",
            analysis=analysis_dict,
        )
        return

    # Count how many turns are from the lead vs the AI
    # Supports JSONL (live_session), single-JSON (audio transcription), and plain text formats
    import re as _re
    lead_turns = 0

    def _is_valid_turn_content(text: str) -> bool:
        t = str(text or "").lower().strip(".,?![]() ")
        if not t or len(t) <= 1:
            return False
        # Ignore purely noise/silence turns
        if t in ("silence", "noise", "background noise", "cough", "sigh", "snort", "[silence]", "[noise]", "[cough]"):
            return False
        return True

    for line in transcript.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Try JSONL format first
        try:
            obj = json.loads(line)
            role_label = (obj.get("role") or obj.get("type", "")).lower()
            turn_content = (obj.get("content") or obj.get("text") or obj.get("message", "")).strip()
            if role_label == "user" and _is_valid_turn_content(turn_content):
                lead_turns += 1
                continue
            # Audio transcription wraps entire conversation in one JSON with role="assistant"
            # and USER:/ASSISTANT: prefixes embedded in content. Count USER: segments.
            if turn_content and "USER:" in turn_content.upper():
                user_parts = _re.findall(r'USER:\s*(.+?)(?=ASSISTANT:|$)', turn_content, _re.IGNORECASE)
                lead_turns += sum(1 for p in user_parts if _is_valid_turn_content(p))
            continue
        except Exception:
            pass
        # Plain text formats: "USER: ...", "CALLER: ..." (Vobiz live WS),
        # "ASSISTANT:/AGENT:" are AI turns and are ignored here.
        upper = line.upper()
        if (upper.startswith("USER:") or upper.startswith("CALLER:")) and ":" in line:
            turn_text = line.split(":", 1)[1].strip()
            if len(line) > 6 and _is_valid_turn_content(turn_text):
                lead_turns += 1

    if lead_turns < 1:
        _t_lower = (transcript or "").lower()
        if (
            "voicemail" in _t_lower
            or "record your message" in _t_lower
            or "after the tone" in _t_lower
        ):
            logger.info(f"Lead {lead_id} transcript matches voicemail prompt — marking Voice Mail.")
            await update_lead_status(
                lead_id,
                status="failed",
                analysis={
                    "summary": "Call reached voicemail / answering machine.",
                    "rating": 0,
                    "disposition": "Voice Mail",
                    "emotion_label": "Unknown",
                    "emotion_rationale": "Answering machine greeting detected in transcript.",
                    "emotion_confidence": None,
                    "site_visit_agreed": False,
                    "requested_callback_datetime_iso": None,
                    "preferred_location": None,
                    "preferred_budget": None,
                    "email_address": None,
                },
            )
            return
        logger.info(f"Lead {lead_id} had no verbal response — marking as No Response.")
        await update_lead_status(
            lead_id,
            status="no response",
            analysis={
                "summary": "Call connected but lead did not speak / no conversation.",
                "rating": 0,
                "disposition": "No Response",
                "emotion_label": "Unknown",
                "emotion_rationale": "No speech captured from the lead.",
                "emotion_confidence": None,
                "site_visit_agreed": False,
                "requested_callback_datetime_iso": None,
                "preferred_location": None,
                "preferred_budget": None,
                "email_address": None,
            },
        )
        return

    # Short transcript guardrail — very brief calls with minimal speech
    # should NOT go through the LLM analyzer (which tends to hallucinate).
    total_words = len(transcript.split())
    if total_words < 20:
        logger.info(f"Lead {lead_id} very short transcript ({total_words} words) — marking as No Answer, skipping LLM.")
        # For sales roles: silent pickup (lead picked up but said nothing) → no answer, NOT completed.
        # This prevents the analyzer from scheduling a callback for a lead who simply didn't speak.
        await update_lead_status(
            lead_id,
            status="no answer",
            analysis={
                "summary": "Call connected but lead did not speak / no conversation.",
                "rating": 0,
                "disposition": "No Response",
                "emotion_label": "Unknown",
                "emotion_rationale": "No speech captured from the lead.",
                "emotion_confidence": None,
                "site_visit_agreed": False,
                "requested_callback_datetime_iso": None,
                "next_action": {"action_type": "None", "datetime_iso": None, "details": "Lead did not speak during the call."},
                "preferred_location": None,
                "preferred_budget": None,
                "email_address": None,
            },
        )
        return

    # ── Overall analysis timeout: 30 seconds ──────────────────────
    # If transcription + LLM analysis takes longer than 30s, we write
    # a fallback analysis so the lead never stays "stuck in dialing".
    try:
        from services.call_analyzer import analyze_call_transcript, canonical_disposition
        from services.callback_time import annotate_analysis_callback_epoch
        from services.transcript_interest import apply_interest_disposition_override

        ANALYSIS_TIMEOUT = 30

        async def _run_analysis() -> dict:
            a = await analyze_call_transcript(transcript)
            annotate_analysis_callback_epoch(a, tz_name=settings.transcript_callback_tz, transcript_text=transcript)
            a = apply_interest_disposition_override(a, transcript)
            return a

        analysis = await asyncio.wait_for(_run_analysis(), timeout=ANALYSIS_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"Analysis timed out ({ANALYSIS_TIMEOUT}s) for lead {lead_id} — using fallback")
        analysis = {
            "summary": "Analysis timed out. Please use Re-analyze.",
            "rating": 0,
            "disposition": "Answered",
            "requested_callback_datetime_iso": None,
            "emotion_label": "Unknown",
            "emotion_rationale": "Analysis did not complete within the time limit.",
            "emotion_confidence": None,
            "site_visit_agreed": False,
            "preferred_location": None,
            "preferred_budget": None,
            "email_address": None,
        }
    except Exception as e:
        logger.exception(f"Analyzer call failed for lead {lead_id}")
        analysis = {
            "summary": "Analysis temporarily unavailable. Please use Re-analyze.",
            "rating": 0,
            "disposition": "Answered",
            "requested_callback_datetime_iso": None,
            "emotion_label": "Unknown",
            "emotion_rationale": "",
            "emotion_confidence": None,
            "site_visit_agreed": False,
            "preferred_location": None,
            "preferred_budget": None,
            "email_address": None,
        }

    try:
        rem_f = float(analysis.get("callback_reminder_epoch"))
    except (TypeError, ValueError):
        rem_f = None

    # ── Fallback callback epoch ───────────────────────────────────
    # If the LLM failed to produce a parseable callback_reminder_epoch
    # but the disposition or the raw ISO field suggests a callback was
    # requested, compute a reasonable fallback (24 hours / 1 day later at same time).
    if rem_f is None:
        disp_lc = str(analysis.get("disposition") or "").lower()
        iso_hint = analysis.get("requested_callback_datetime_iso")
        
        # Check explicit callback conditions: only if they said "busy" or "call later/callback"
        is_busy_or_later = any(kw in disp_lc for kw in ("call later", "busy", "callback", "follow up", "follow-up"))
        
        if is_busy_or_later or iso_hint:
            from services.callback_time import zoneinfo_safe
            from datetime import datetime
            
            rem_f = time.time() + 86400  # 24 hours / 1 day later
            analysis["callback_reminder_epoch"] = rem_f
            
            tz = zoneinfo_safe(settings.transcript_callback_tz)
            analysis["requested_callback_datetime_iso"] = datetime.fromtimestamp(rem_f, tz).isoformat()
            
            logger.info(
                f"Callback epoch missing for lead {lead_id} but disposition/context suggests callback — "
                f"scheduled for tomorrow at same clock time: {analysis['requested_callback_datetime_iso']}. "
                f"disposition={disp_lc!r} iso_hint={iso_hint!r}"
            )

    canon_disp = canonical_disposition(analysis.get("disposition"))
    
    # Busy calls are unified with failed retries 24-hours scheduler below
    now_t = time.time()
    is_cb = (rem_f is not None and rem_f > now_t)

    if rem_f is not None and rem_f > now_t:
        new_status = "callback_scheduled"
    elif rem_f is not None and rem_f <= now_t:
        new_status = "pending"
    elif role in ("sales_1", "sales_2"):
        if canon_disp in ("No Answer", "Wrong Number", "Voicemail", "Call Screened"):
            new_status = "failed"
        elif canon_disp == "Busy":
            new_status = "busy"
        else:
            new_status = "completed"
    else:
        new_status = _disposition_to_status(analysis.get("disposition", ""))
        if canon_disp == "Busy" and int(extra.get("failed_call_retries") or 0) >= 2:
            new_status = "failed"

    # ── Call Quality Auto-Retry (Learning Loop) ───────────────────
    # Disabled by user request: connection issues should not override outcomes/summaries
    is_poor_connection = False
    
    # 1. Check transcript text for keywords or repetition patterns
    trans_lower = (transcript or "").lower()
    static_words = ["static", "inaudible", "breaking", "cannot hear", "not clear", "bad connection", "connection issue", "disconnected"]
    has_static_keyword = any(w in trans_lower for w in static_words)
    
    # Check for repeated "can you hear me" / "hello" loops
    hello_count = trans_lower.count("hello") + trans_lower.count("can you hear me") + trans_lower.count("i can hear you")
    has_hello_loop = (hello_count >= 5)
    
    # 2. Check analysis summary and rating
    summary_lower = str(analysis.get("summary") or "").lower()
    has_poor_summary = any(w in summary_lower for w in ("static", "inaudible", "cannot hear", "disconnected", "connection issue", "unclear"))
    
    rating_val = None
    try:
        rating_val = int(analysis.get("rating") or 0)
    except Exception:
        pass
        
    if (has_static_keyword or has_hello_loop or has_poor_summary) and (rating_val is None or rating_val <= 2):
        is_poor_connection = False
        
    if is_poor_connection:
        from services.callback_time import zoneinfo_safe
        from datetime import datetime
        tz = zoneinfo_safe(settings.transcript_callback_tz)
        
        rem_f = time.time() + 300  # Schedule retry in 5 minutes (300 seconds)
        analysis["callback_reminder_epoch"] = rem_f
        analysis["requested_callback_datetime_iso"] = datetime.fromtimestamp(rem_f, tz).isoformat()
        analysis["disposition"] = "Call Later"
        analysis["summary"] = "Call had poor connection / unclear audio. Automatically rescheduled for retry in 5 minutes."
        new_status = "callback_scheduled"
        is_cb = True
        logger.info(
            f"Call quality issue detected for lead {lead_id} (rating={rating_val}, hello_count={hello_count}) — "
            f"automatically rescheduled retry callback in 5 minutes: {analysis['requested_callback_datetime_iso']}"
        )

    # ── Site Visit override ───────────────────────────────────────
    # If the analysis detected the customer explicitly agreed to a
    # physical site visit, promote the lead to "site_visit" status so
    # the dashboard can filter and track it separately.
    # Also check action_type as a backup in case Gemini set it but forgot site_visit_agreed.
    _sv_action_type = (analysis.get("next_action") or {}).get("action_type", "")
    if (
        analysis.get("site_visit_agreed") or _sv_action_type.strip().lower() in ("site visit", "site_visit")
    ):
        new_status = "site_visit"
        logger.info(
            "Lead {} agreed to site visit (site_visit_agreed={}, action_type={}) — status promoted to site_visit",
            lead_id, analysis.get("site_visit_agreed"), _sv_action_type,
        )

        # ── Site Visit Auto-Followup Scheduling ────────────────────────────────
        try:
            from datetime import datetime, timedelta
            from core.storage import get_lead as _get_lead_for_sv_sch, add_scheduled_callback
            from services.callback_time import zoneinfo_safe
            
            _lead_row_sv = await _get_lead_for_sv_sch(role, lead_id)
            if _lead_row_sv:
                phone = _lead_row_sv.get("phone", "")
                name = _lead_row_sv.get("name", "") or "Valued Customer"
                
                # Extract site visit date/time
                _next_act = analysis.get("next_action") or {}
                _sv_date_str = (_next_act.get("datetime_iso") or analysis.get("requested_callback_datetime_iso") or "").strip()
                if _sv_date_str:
                    tz = zoneinfo_safe(settings.transcript_callback_tz)
                    # Handle Z suffix
                    if _sv_date_str.endswith("Z") or _sv_date_str.endswith("z"):
                        _sv_date_str = _sv_date_str[:-1] + "+00:00"
                    
                    sv_dt = datetime.fromisoformat(_sv_date_str)
                    if sv_dt.tzinfo is None:
                        sv_dt = sv_dt.replace(tzinfo=tz)
                    else:
                        sv_dt = sv_dt.astimezone(tz)
                        
                    now_dt = datetime.now(tz)
                    
                    # 1. Day-Before Re-confirmation Call
                    # Target date: 1 day before site visit
                    # Target time: same clock time as current call
                    recon_dt = datetime.combine(
                        sv_dt.date() - timedelta(days=1),
                        now_dt.time()
                    ).replace(tzinfo=tz)
                    recon_epoch = recon_dt.timestamp()
                    
                    # 2. Day-of Site Visit Call (morning call to ask what time they are coming)
                    # Target date: day of site visit
                    # Target time: 10:00 AM (or 2 hours before site visit if scheduled before 12:00 PM)
                    if sv_dt.hour < 12:
                        day_of_dt = sv_dt - timedelta(hours=2)
                        if day_of_dt.hour < 9:
                            day_of_dt = day_of_dt.replace(hour=9, minute=0, second=0, microsecond=0)
                    else:
                        day_of_dt = sv_dt.replace(hour=10, minute=0, second=0, microsecond=0)
                    day_of_epoch = day_of_dt.timestamp()
                    
                    # Track scheduled callbacks
                    scheduled_any = False
                    earliest_future_epoch = None
                    
                    if recon_epoch > time.time():
                        await add_scheduled_callback(
                            role=role,
                            phone=phone,
                            name=f"{name} (Re-confirm Site Visit)",
                            scheduled_at=recon_epoch,
                            lead_id=lead_id
                        )
                        logger.info(
                            "Automatically scheduled day-before site visit re-confirmation callback for lead {} ({}) at {}",
                            lead_id, phone, recon_dt
                        )
                        scheduled_any = True
                        earliest_future_epoch = recon_epoch
                        
                    if day_of_epoch > time.time():
                        await add_scheduled_callback(
                            role=role,
                            phone=phone,
                            name=f"{name} (Day of Site Visit)",
                            scheduled_at=day_of_epoch,
                            lead_id=lead_id
                        )
                        logger.info(
                            "Automatically scheduled day-of site visit confirmation callback for lead {} ({}) at {}",
                            lead_id, phone, day_of_dt
                        )
                        scheduled_any = True
                        if earliest_future_epoch is None or day_of_epoch < earliest_future_epoch:
                            earliest_future_epoch = day_of_epoch
                    
                    # If we scheduled future calls, update the lead's analysis metadata
                    # so that it gets captured in the dashboard "Follow Up" card count/filters.
                    if scheduled_any and earliest_future_epoch:
                        analysis["callback_reminder_epoch"] = earliest_future_epoch
                        analysis["requested_callback_datetime_iso"] = datetime.fromtimestamp(earliest_future_epoch, tz).isoformat()
                        logger.info(
                            "Updated lead {} analysis with callback_reminder_epoch={:.0f} to surface in Follow Up section",
                            lead_id, earliest_future_epoch
                        )
        except Exception as ex:
            logger.exception("Failed to auto-schedule site visit follow-up callbacks for lead {}", lead_id)

    # ── Retake Retry Increment ───────────────────────────────────
    # If this is a campaign lead and the call connected (e.g. status is NOT terminal failed/busy/no_answer now),
    # but the previous status of the lead was in ('failed', 'busy', 'no answer', 'error'),
    # then this was a successful manual or automated "Retake" call.
    # We increment the failed_call_retries counter in extra so the dashboard Retake badge shows.
    if lead_id is not None and new_status not in ("failed", "busy", "no answer"):
        try:
            from core.storage import get_lead
            _prev_lead = await get_lead(role, lead_id)
            if _prev_lead:
                _prev_status = _prev_lead.get("status")
                if _prev_status in ("failed", "busy", "no answer", "error"):
                    _retries = int(extra.get("failed_call_retries") or 0)
                    extra["failed_call_retries"] = min(2, _retries + 1)
                    logger.info("Incrementing manual/callback retake count for lead {}: {} -> {}", lead_id, _retries, extra["failed_call_retries"])
        except Exception as e:
            logger.warning("Failed to calculate retake count for lead {}: {}", lead_id, e)

    from core.storage import update_lead_retry_state
    await update_lead_retry_state(lead_id, status=new_status, extra=extra, analysis=analysis)

    # Record this call attempt so the dashboard can show historical retakes.
    try:
        from core.storage import add_call_attempt, get_lead as _ca_get_lead
        _ca_lead = await _ca_get_lead(role, lead_id)
        _ca_attempt_num = int(extra.get("failed_call_retries") or 0) + 1
        await add_call_attempt(
            lead_id=lead_id,
            role=role,
            attempt_number=_ca_attempt_num,
            log_id=log_id,
            status=new_status or "completed",
            disposition=canon_disp or analysis.get("disposition", ""),
            summary=str(analysis.get("summary", "") or "")[:3000],
            rating=analysis.get("rating"),
            duration_sec=duration_sec,
            callback_scheduled_at=rem_f,
        )
    except Exception as _ca_e:
        logger.warning("Failed to record call attempt for lead {}: {}", lead_id, _ca_e)

    try:
        from core.events import get_event_bus
        get_event_bus().publish("lead_updated", role=role, lead_id=lead_id)
    except Exception:
        pass

    # Update the materialized dashboard state (sub-ms incremental update)
    try:
        from core.dashboard_state import notify_lead_updated
        old_lead_status = "dialing" if lead_id else "pending"
        if lead_id is not None:
            try:
                from core.storage import get_lead as _gl
                _prev = await _gl(role, lead_id)
                if _prev:
                    old_lead_status = str(_prev.get("status") or "pending").strip().lower()
            except Exception:
                pass
        notify_lead_updated(
            role=role, lead_id=lead_id,
            old_status=old_lead_status, new_status=new_status,
            start_time=time.time(), disposition=canon_disp or analysis.get("disposition", ""),
            analysis_raw=analysis,
        )
    except Exception:
        pass

    logger.info(
        f"Analysis updated for lead {lead_id}: status={new_status} disposition={analysis.get('disposition')!r} "
        f"rating={analysis.get('rating')} callback_epoch={analysis.get('callback_reminder_epoch')!r}"
    )

    # ── Reschedule Voicemail / Analyzed Failures ─────────────────
    if new_status in ("failed", "no answer", "busy"):
        try:
            from core.storage import get_lead
            lead_row = await get_lead(role, lead_id)
            if lead_row:
                await _schedule_failed_call_retry(role, lead_id, lead_row.get("phone"), lead_row.get("name"), reason="no_answer")
                logger.info("Automatically scheduled retry for voicemail / analyzed failure for lead {}", lead_id)
        except Exception as retry_ex:
            logger.exception("Failed to schedule retry for voicemail/failed lead {}: {}", lead_id, retry_ex)

    if (new_status == "callback_scheduled" or is_cb) and rem_f is not None:
        try:
            from core.storage import add_scheduled_callback, get_lead as _get_lead_for_cb
            _lead_row = await _get_lead_for_cb(role, lead_id)
            if _lead_row:
                await add_scheduled_callback(
                    role,
                    phone=_lead_row.get("phone", ""),
                    name=_lead_row.get("name", ""),
                    scheduled_at=rem_f,
                    lead_id=lead_id,
                )
                logger.info(
                    f"Scheduled callback entry created for lead {lead_id} "
                    f"(phone={_lead_row.get('phone')}, scheduled_at={rem_f})"
                )
        except Exception as e:
            logger.exception(f"Failed to create scheduled_callback entry for lead {lead_id}")

    # ── WhatsApp + Email bulk auto-send ───────────────────────────
    # Send to ALL actionable dispositions: Interested, Not Interested,
    # Call Later / Callback, and Site Visit — for ALL roles.
    _should_send_wa = (
        canon_disp in ("Interested", "Not Interested", "Call Later", "Callback")
        or new_status in ("interested", "not_interested", "site_visit", "callback_scheduled")
    )
    if not _should_send_wa:
        logger.info("Auto-send skipped for lead {}: disposition={}, status={}", lead_id, canon_disp, new_status)
    else:
        # ── Pitchx (sales_1) guarantee ──
        # If a Pitchx lead is Interested, WhatsApp project details MUST be sent.
        # This is already covered by _should_send_wa above (Interested is one of
        # the actionable dispositions) — make the guarantee explicit and logged.
        _role_l = (role or "").strip().lower()
        is_interested = canon_disp == "Interested" or new_status in ("interested",)
        if _role_l == "sales_1" and is_interested:
            logger.info("Pitchx interested lead {} — WhatsApp details ensured", lead_id)

        if is_interested:
            try:
                from core.notifications import push_notification

                push_notification(
                    role,
                    "Interested lead",
                    f"{lead_name or lead_phone} ({lead_phone})",
                    kind="lead",
                )
            except Exception as ne:
                logger.warning("Failed to push interested-lead notification: {}", ne)

        # ── WhatsApp ──
        try:
            from core.state import is_whatsapp_sent_for_call
            from core.storage import get_lead as _get_lead_for_wa
            from core.storage import get_lead_whatsapp_sent, mark_whatsapp_sent
            from services.whatsapp_leads import send_whatsapp_project_details

            _lead_row_wa = await _get_lead_for_wa(role, lead_id)
            _call_id = _lead_row_wa.get("_call_id") if _lead_row_wa else None

            if await get_lead_whatsapp_sent(lead_id) or is_whatsapp_sent_for_call(_call_id):
                logger.info("WhatsApp already sent for lead {} (call_id={}) — skipping duplicate", lead_id, _call_id)
            elif _lead_row_wa:
                phone_wa = _lead_row_wa.get("phone", "")
                if phone_wa:
                    wa_details = analysis.get("summary", "")
                    wa_name = _lead_row_wa.get("name", "")
                    wa_result = await send_whatsapp_project_details(phone_wa, summary=wa_details, lead_name=wa_name)
                    logger.info("WhatsApp details sent for lead {} ({}): {}", lead_id, phone_wa, wa_result)
                    if wa_result.get("sent"):
                        await mark_whatsapp_sent(lead_id)
        except Exception as e:
            logger.exception(f"WhatsApp auto-send failed for lead {lead_id}: {e}")

        # ── Email ──
        try:
            from core.storage import get_lead_email_sent, mark_email_sent, get_lead as _get_lead_for_email
            from services.email_leads import send_bulk_project_email
            _lead_row_email = await _get_lead_for_email(role, lead_id)
            email_to = ""
            if _lead_row_email:
                email_to = (_lead_row_email.get("email") or "").strip()
            if not email_to or "@" not in email_to:
                email_to = (analysis.get("email_address") or "").strip()

            if email_to and "@" in email_to:
                db_email = (_lead_row_email.get("email") or "").strip() if _lead_row_email else ""
                if db_email != email_to:
                    from core.storage import update_lead_info
                    await update_lead_info(lead_id, email=email_to)
                    logger.info("Updated lead {} email column to {}", lead_id, email_to)

                if await get_lead_email_sent(lead_id):
                    logger.info("Email already sent for lead {} — skipping duplicate", lead_id)
                else:
                    em_summary = analysis.get("summary", "")
                    em_name = _lead_row_email.get("name", "") if _lead_row_email else ""
                    email_result = await send_bulk_project_email(email_to, summary=em_summary, lead_name=em_name)
                    logger.info("Bulk email sent for lead {} ({}): {}", lead_id, email_to, email_result)
                    if email_result.get("sent"):
                        await mark_email_sent(lead_id)
            else:
                logger.info("Lead {} has no email address — skipping email send", lead_id)
        except Exception as e:
            logger.exception(f"Email auto-send failed for lead {lead_id}: {e}")

    # ── Callback outcome write-back ──────────────────────────────
    # If this lead was created by a scheduled callback, write the
    # analysis outcome back to the scheduled_callbacks row so the
    # dashboard displays the result (interested, not interested, etc.).
    if callback_id is not None:
        try:
            from core.storage import update_scheduled_callback_analysis
            await update_scheduled_callback_analysis(
                callback_id,
                disposition=analysis.get("disposition", ""),
                summary=analysis.get("summary", ""),
                rating=analysis.get("rating"),
                next_action=analysis.get("next_action"),
                analysis_json=analysis,
            )
            logger.info(
                "Callback {} outcome saved: disposition={!r} rating={}",
                callback_id, analysis.get("disposition"), analysis.get("rating"),
            )
        except Exception as e:
            logger.exception(f"Failed to save callback {callback_id} outcome: {e}")

    # ── Virtual Meet tracking ────────────────────────────────────
    # When analysis detects Virtual Meet was discussed, persist to DB.
    try:
        _vm_next = analysis.get("next_action") or {}
        if (_vm_next.get("action_type") or "").strip().lower() in ("virtual meet", "virtual"):
            from core.storage import add_virtual_meet as _add_vm
            _vm_date = _vm_next.get("datetime_iso", "") or analysis.get("requested_callback_datetime_iso", "")
            _vm_details = _vm_next.get("details", "") or analysis.get("summary", "")
            _vm_notes = f"{_vm_date} | {_vm_details}" if _vm_date else _vm_details
            await _add_vm(lead_id, role, _vm_date or "TBD", "TBD", notes=_vm_notes)
            logger.info("Virtual meet recorded for lead {}: {}", lead_id, _vm_notes)
    except Exception as e:
        logger.exception(f"Virtual meet tracking failed for lead {lead_id}: {e}")

    # ── Site Visit tracking ──────────────────────────────────────
    # When analysis detects the customer agreed to a physical site visit,
    # log it for dashboard display and follow-up scheduling.
    try:
        _sv_next = analysis.get("next_action") or {}
        if analysis.get("site_visit_agreed") or (_sv_next.get("action_type") or "").strip().lower() in ("site visit", "site_visit"):
            _sv_date = _sv_next.get("datetime_iso", "") or analysis.get("requested_callback_datetime_iso", "")
            _sv_details = _sv_next.get("details", "") or analysis.get("summary", "")
            logger.info(
                "Site visit agreed for lead {}: date={} details={}",
                lead_id, _sv_date or "not specified", _sv_details,
            )
    except Exception as e:
        logger.exception(f"Site visit tracking failed for lead {lead_id}: {e}")


async def _finalize_manual_call_leg(
    role: str, camp_id: str, live_log_id: str, duration_sec: float | None = None
) -> None:
    """Post-call analyzer + SQLite row for console **Make a Call** legs (no lead row)."""
    from core.storage import finalize_manual_call_record, manual_call_row_by_camp_id

    if not await manual_call_row_by_camp_id(camp_id):
        logger.warning("Manual call finalize: no manual_calls row for camp_id={}", camp_id)
        return

    # ── Voicemail override check ──
    is_voicemail_flag = False
    try:
        from core.worker import _CAMPAIGN_DATA
        if live_log_id in _CAMPAIGN_DATA:
            is_voicemail_flag = bool(_CAMPAIGN_DATA[live_log_id].get("is_voicemail"))
    except Exception:
        pass

    if is_voicemail_flag:
        logger.info(f"Manual call {camp_id} marked as Voicemail early in session — bypass LLM.")
        analysis = {
            "summary": "Call went to voicemail / answering machine.",
            "rating": 0,
            "disposition": "Voice Mail",
            "emotion_label": "Unknown",
            "emotion_rationale": "Answering machine greeting matched.",
            "emotion_confidence": None,
            "site_visit_agreed": False,
            "requested_callback_datetime_iso": None,
        }
        await finalize_manual_call_record(camp_id, live_log_id, duration_sec, analysis)
        return

    # Always prefer audio transcription — Gemini Live JSONL transcripts contain
    # single-word assistant fragments that produce garbage analysis.
    transcript = ""
    try:
        from services.transcriber import transcribe_audio

        transcribed = await transcribe_audio(live_log_id, role)
        if transcribed:
            transcript = transcribed
            logger.info("Manual call audio transcription successful for camp_id={}", camp_id)
    except Exception as e:
        logger.warning("Manual call audio transcription failed: {}", e)

    # Fall back to JSONL live transcript only if audio transcription failed
    if not (transcript or "").strip():
        transcript = _read_transcript_jsonl(role, live_log_id)
        if (transcript or "").strip():
            logger.info("Falling back to JSONL transcript for camp_id={}", camp_id)

    analysis: dict
    if not (transcript or "").strip():
        analysis = {
            "summary": "Call ended; transcript unavailable.",
            "rating": 0,
            "next_steps": "N/A",
            "disposition": "Answered",
            "emotion_label": "Unknown",
            "emotion_rationale": "No speech captured in transcript.",
            "emotion_confidence": None,
        }
    else:
        # analyze_call_transcript now has built-in gemini → local fallback
        # and never raises — always returns a valid dict.
        from services.call_analyzer import analyze_call_transcript

        analysis = await analyze_call_transcript(transcript)
        # ── Annotate callback epoch (mirrors campaign lead path) ────
        from services.callback_time import annotate_analysis_callback_epoch
        annotate_analysis_callback_epoch(
            analysis,
            tz_name=settings.transcript_callback_tz,
            transcript_text=transcript,
        )

    # ── Schedule callback if requested ─────────────────────────────
    try:
        rem_f_manual = None
        try:
            rem_f_manual = float(analysis.get("callback_reminder_epoch"))
        except (TypeError, ValueError):
            pass

        if rem_f_manual is not None and rem_f_manual > time.time():
            from core.storage import add_scheduled_callback as _add_cb_manual, manual_call_row_by_camp_id as _mcr_cb
            _mc_cb = await _mcr_cb(camp_id)
            if _mc_cb:
                _cb_phone = _mc_cb.get("to_phone", "")
                _cb_name = _mc_cb.get("callee_name", "") or "Manual Call"
                if _cb_phone:
                    _cb_id = await _add_cb_manual(
                        role,
                        phone=_cb_phone,
                        name=_cb_name,
                        scheduled_at=rem_f_manual,
                    )
                    logger.info(
                        "Manual call: scheduled callback id={} for {} ({}) at epoch={:.0f} (camp_id={})",
                        _cb_id, _cb_name, _cb_phone, rem_f_manual, camp_id,
                    )
    except Exception as e:
        logger.exception("Manual call: callback scheduling failed for camp_id={}: {}", camp_id, e)

    await finalize_manual_call_record(camp_id, live_log_id, duration_sec, analysis)
    try:
        from core.events import get_event_bus
        get_event_bus().publish("lead_updated", role=role, lead_id=None)
    except Exception:
        pass
    logger.info(
        "Manual call outcome saved camp_id={} disposition={!r}",
        camp_id,
        analysis.get("disposition"),
    )
    
    # Release active and phone slots for manual calls
    try:
        from core.state import _CAMPAIGN_DATA, release_vobiz_call_slot, release_phone_slot
        meta = _CAMPAIGN_DATA.get(camp_id) or {}
        outbound_phone = meta.get("_outbound_phone")
        release_vobiz_call_slot(role)
        if outbound_phone:
            release_phone_slot(outbound_phone)
            logger.info("Released slots for manual call {}: phone={}", camp_id, outbound_phone)
    except Exception as e:
        logger.exception("Failed to release slots for manual call {}: {}", camp_id, e)

    # ── WhatsApp auto-send for manual calls ────────────────────────
    try:
        is_sales = role in ("sales_1", "sales_2")
        _wa_next = analysis.get("next_action") or {}
        if is_sales or (_wa_next.get("action_type") or "").strip().lower() == "whatsapp":
            from core.state import is_whatsapp_sent_for_call
            if is_whatsapp_sent_for_call(camp_id):
                logger.info("WhatsApp already sent for manual call {} — skipping duplicate", camp_id)
            else:
                from core.storage import manual_call_row_by_camp_id as _mcr
                from services.call_analyzer import canonical_disposition as _canon_disp
                _mc_row = await _mcr(camp_id)
                if _mc_row:
                    _disp = _canon_disp(analysis.get("disposition"))
                    if _disp in ("Not Interested", "not_interested") and not is_sales:
                        logger.info("WhatsApp send skipped for manual call {}: Not Interested", camp_id)
                    else:
                        _phone_wa = _mc_row.get("to_phone", "")
                        _name_wa = _mc_row.get("callee_name", "")
                        if _phone_wa:
                            from services.whatsapp_leads import send_whatsapp_project_details
                            _details = _wa_next.get("details") or analysis.get("summary", "")
                            _wa_result = await send_whatsapp_project_details(_phone_wa, summary=_details, lead_name=_name_wa)
                            logger.info("WhatsApp sent for manual call {} ({}): {}", camp_id, _phone_wa, _wa_result)
    except Exception as e:
        logger.exception("WhatsApp auto-send failed for manual call {}: {}", camp_id, e)

    # ── Virtual Meet tracking for manual calls ─────────────────────
    try:
        _vm_next = analysis.get("next_action") or {}
        if (_vm_next.get("action_type") or "").strip().lower() in ("virtual meet", "virtual"):
            from core.storage import manual_call_row_by_camp_id as _mcr_vm
            _mc_row_vm = await _mcr_vm(camp_id)
            if _mc_row_vm:
                _vm_details = _vm_next.get("details") or analysis.get("summary", "")
                logger.info("Virtual Meet requested in manual call {}: {}", camp_id, _vm_details)
    except Exception as e:
        logger.exception("Virtual Meet tracking failed for manual call {}: {}", camp_id, e)

    # ── Site Visit tracking for manual calls ─────────────────────
    try:
        _sv_next_mc = analysis.get("next_action") or {}
        if analysis.get("site_visit_agreed") or (_sv_next_mc.get("action_type") or "").strip().lower() in ("site visit", "site_visit"):
            _sv_details_mc = _sv_next_mc.get("details") or analysis.get("summary", "")
            logger.info("Site Visit agreed in manual call {}: {}", camp_id, _sv_details_mc)
            
            # Replicate campaign site visit callback scheduling for manual calls
            from datetime import datetime, timedelta
            from core.storage import manual_call_row_by_camp_id as _mcr_sv_mc, add_scheduled_callback as _add_cb_sv_mc
            from services.callback_time import zoneinfo_safe
            
            _mc_row_sv = await _mcr_sv_mc(camp_id)
            if _mc_row_sv:
                _phone = _mc_row_sv.get("to_phone", "")
                _name = _mc_row_sv.get("callee_name", "") or "Manual Call"
                
                # Check if we have a matching lead_id in the db first
                from core.storage import find_lead_by_phone
                _db_lead = await find_lead_by_phone(role, _phone)
                _lead_id = _db_lead.get("id") if _db_lead else None
                
                # Extract site visit date/time
                _sv_date_str = (_sv_next_mc.get("datetime_iso") or analysis.get("requested_callback_datetime_iso") or "").strip()
                if _sv_date_str and _phone:
                    tz = zoneinfo_safe(settings.transcript_callback_tz)
                    if _sv_date_str.endswith("Z") or _sv_date_str.endswith("z"):
                        _sv_date_str = _sv_date_str[:-1] + "+00:00"
                    
                    sv_dt = datetime.fromisoformat(_sv_date_str)
                    if sv_dt.tzinfo is None:
                        sv_dt = sv_dt.replace(tzinfo=tz)
                    else:
                        sv_dt = sv_dt.astimezone(tz)
                        
                    now_dt = datetime.now(tz)
                    
                    # 1. Day-Before Re-confirmation Call
                    recon_dt = datetime.combine(
                        sv_dt.date() - timedelta(days=1),
                        now_dt.time()
                    ).replace(tzinfo=tz)
                    recon_epoch = recon_dt.timestamp()
                    
                    # 2. Day-of Site Visit Call
                    if sv_dt.hour < 12:
                        day_of_dt = sv_dt - timedelta(hours=2)
                        if day_of_dt.hour < 9:
                            day_of_dt = day_of_dt.replace(hour=9, minute=0, second=0, microsecond=0)
                    else:
                        day_of_dt = sv_dt.replace(hour=10, minute=0, second=0, microsecond=0)
                    day_of_epoch = day_of_dt.timestamp()
                    
                    if recon_epoch > time.time():
                        await _add_cb_sv_mc(
                            role=role,
                            phone=_phone,
                            name=f"{_name} (Re-confirm Site Visit)",
                            scheduled_at=recon_epoch,
                            lead_id=_lead_id
                        )
                        logger.info(
                            "Manual Call: Automatically scheduled day-before site visit re-confirmation callback for {} ({}) at {}",
                            _name, _phone, recon_dt
                        )
                        
                    if day_of_epoch > time.time():
                        await _add_cb_sv_mc(
                            role=role,
                            phone=_phone,
                            name=f"{_name} (Day of Site Visit)",
                            scheduled_at=day_of_epoch,
                            lead_id=_lead_id
                        )
                        logger.info(
                            "Manual Call: Automatically scheduled day-of site visit confirmation callback for {} ({}) at {}",
                            _name, _phone, day_of_dt
                        )
    except Exception as e:
        logger.exception("Site Visit tracking failed for manual call {}: {}", camp_id, e)


async def _finalize_incoming_call_leg(
    role: str, camp_id: str, live_log_id: str, duration_sec: float | None = None
) -> None:
    """Post-call analyzer + SQLite row for incoming (customer call-back) legs."""
    from core.storage import (
        finalize_incoming_call_record,
        incoming_call_row_by_camp_id,
        add_lead as _inbound_add_lead,
        update_lead_status as _inbound_update_status,
        update_lead_call_info as _inbound_update_info,
    )

    row = await incoming_call_row_by_camp_id(camp_id)
    if not row:
        logger.warning("Incoming call finalize: no incoming_calls row for camp_id={}", camp_id)
        return

    transcript = ""
    try:
        from services.transcriber import transcribe_audio

        transcribed = await transcribe_audio(live_log_id, role)
        if transcribed:
            transcript = transcribed
            logger.info("Incoming call audio transcription successful for camp_id={}", camp_id)
    except Exception as e:
        logger.warning("Incoming call audio transcription failed: {}", e)

    if not (transcript or "").strip():
        transcript = _read_transcript_jsonl(role, live_log_id)
        if (transcript or "").strip():
            logger.info("Falling back to JSONL transcript for camp_id={}", camp_id)

    analysis: dict
    if not (transcript or "").strip():
        analysis = {
            "summary": "Call ended; transcript unavailable.",
            "rating": 0,
            "next_steps": "N/A",
            "disposition": "Answered",
            "emotion_label": "Unknown",
            "emotion_rationale": "No speech captured in transcript.",
            "emotion_confidence": None,
        }
    else:
        from services.call_analyzer import analyze_call_transcript

        analysis = await analyze_call_transcript(transcript)

    await finalize_incoming_call_record(camp_id, live_log_id, duration_sec, analysis)
    logger.info(
        "Incoming call outcome saved camp_id={} disposition={!r}",
        camp_id,
        analysis.get("disposition"),
    )

    # Create a lead record so the dashboard shows this incoming call
    from_phone = row.get("from_phone", "")
    caller_name = row.get("caller_name", "") or "Inbound Call"
    if from_phone:
        try:
            from core.storage import find_lead_by_phone, update_lead_info
            existing_lead = await find_lead_by_phone(role, from_phone)
            if existing_lead:
                lead_id = existing_lead["id"]
                # Update caller name if previous was generic
                if existing_lead.get("name") in ("", "Inbound Call", "unknown", None) and caller_name != "Inbound Call":
                    await update_lead_info(lead_id, name=caller_name)
            else:
                lead_id = await _inbound_add_lead(role, name=caller_name, phone=from_phone)

            started_at = row.get("started_at")
            start_epoch = None
            if started_at:
                try:
                    from datetime import datetime
                    dt = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
                    start_epoch = dt.timestamp() - (duration_sec or 0)
                except Exception:
                    start_epoch = time.time() - (duration_sec or 0)
            await _inbound_update_info(lead_id, log_id=live_log_id, start_time=start_epoch or time.time())
            await _analyze_and_update_lead(role, lead_id, live_log_id)
            logger.info("Incoming call lead updated/created and analyzed: id={} phone={}", lead_id, from_phone)
        except Exception as e:
            logger.exception("Failed to create lead for incoming call: {}", e)


async def _schedule_failed_call_retry(role: str, lead_id: int, lead_phone: str, lead_name: str, reason: str = "") -> None:
    """
    Schedule a re-dial for leads that failed to connect.

    Default retry policy (persistent until lead picks up OR says "Not Interested"):
      Retry 1  →  15 minutes later
      Retry 2  →  30 minutes later
      Retry 3  →  1 hour later
      Retry 4+ →  2 hours later  (runs indefinitely up to MAX_RETRIES)

    No-answer policy for sales_1 / sales_2 (``reason`` is a no-answer reason):
      Retry 1  →  24 hours later
      Retry 2  →  48 hours later
      Then     →  mark the lead failed ("No answer after 2 retries (24h/48h)")

    Quiet hours (9:30 AM – 7:30 PM IST) are respected; if the computed
    retry time falls outside the role's allowed window it is pushed to the
    next allowed slot (role-aware via ``push_to_role_window`` with a legacy
    IST fallback so behavior never breaks).
    """
    try:
        import json
        import time
        from core.storage import get_lead, add_scheduled_callback, update_lead_retry_state
        from services.callback_time import zoneinfo_safe
        from datetime import datetime

        _role_l = (role or "").strip().lower()
        _reason_l = (reason or "").strip().lower()
        _NO_ANSWER_REASONS = ("no_answer", "no answer", "timeout", "noanswer")
        _is_no_answer_sales = _reason_l in _NO_ANSWER_REASONS and _role_l in ("sales_1", "sales_2")

        if _is_no_answer_sales:
            # No-answer schedule for pitch/sales campaigns: 24h → 48h → stop.
            MAX_RETRIES = 2
            _COOLDOWNS = [86400, 172800]      # retry 1: 24 hours, retry 2: 48 hours
            _DEFAULT_COOLDOWN = 172800
        else:
            # Maximum retry attempts before giving up (effectively unlimited for
            # normal operations — 100 retries ≈ 200+ hours of persistent calling).
            MAX_RETRIES = 100
            _COOLDOWNS = [900, 1800, 3600]    # seconds for retries 1-3
            _DEFAULT_COOLDOWN = 7200          # 2 hours for retry 4 and beyond

        lead_row = await get_lead(role, lead_id)
        if not lead_row:
            return

        # Do not reschedule retries for leads that have already been resolved.
        current_status = (lead_row.get("status") or "").lower()
        DO_NOT_RETRY = {
            "completed", "not_interested", "callback_completed",
            "interested", "site_visit", "callback_scheduled",
        }
        if current_status in DO_NOT_RETRY:
            logger.info(
                "Skipping failed-call retry for lead {} ({}) — status is already {}",
                lead_id, lead_name, current_status,
            )
            return

        extra = lead_row.get("extra") or {}
        retries = int(extra.get("failed_call_retries") or 0)

        if retries >= MAX_RETRIES:
            from core.storage import update_lead_status
            if _is_no_answer_sales:
                _final_error = "No answer after 2 retries (24h/48h)"
            else:
                _final_error = f"No answer / Timeout after {MAX_RETRIES} retries"
            await update_lead_status(lead_id, "failed", error=_final_error)
            logger.info(
                "Failed-call retry limit reached ({} retries) for lead {} → marking failed",
                MAX_RETRIES, lead_id,
            )
            return

        # ── Progressive cooldown intervals ──────────────────────────────────
        # Default ladder: Retry 1: 15 min, Retry 2: 30 min, Retry 3: 1 hr, Retry 4+: 2 hrs.
        # No-answer (sales_1/sales_2): Retry 1: 24 hrs, Retry 2: 48 hrs.
        cooldown = _COOLDOWNS[retries] if retries < len(_COOLDOWNS) else _DEFAULT_COOLDOWN

        next_retry_count = retries + 1
        retry_epoch = time.time() + cooldown

        # ── Quiet-hours push ─────────────────────────────────────────────────
        # Push the retry epoch into the role's allowed calling window.
        # Falls back to the legacy IST window (before 9:30 AM or after 7:30 PM
        # → next 9:30 AM) if the role-aware helper is not available yet.
        try:
            from core.campaign_hours import push_to_role_window as _push_to_role_window
            retry_epoch = _push_to_role_window(role, retry_epoch)
        except (ImportError, TypeError):
            import datetime as _dt
            from services.callback_time import zoneinfo_safe as _zsafe
            _IST = _zsafe("Asia/Kolkata")
            _rdt = _dt.datetime.fromtimestamp(retry_epoch, tz=_IST)
            _hr = _rdt.hour + _rdt.minute / 60.0
            if _hr >= 19.5 or _hr < 9.5:          # inside quiet window
                _morning = _rdt.replace(hour=9, minute=30, second=0, microsecond=0)
                if _rdt.hour >= 19:                # after 7:30 PM → push to next morning
                    _morning += _dt.timedelta(days=1)
                retry_epoch = _morning.timestamp()

        extra["failed_call_retries"] = next_retry_count

        analysis = {}
        if lead_row.get("analysis"):
            try:
                analysis = json.loads(lead_row.get("analysis"))
            except Exception:
                analysis = {}

        analysis["callback_reminder_epoch"] = retry_epoch
        tz = zoneinfo_safe(settings.transcript_callback_tz)
        analysis["requested_callback_datetime_iso"] = datetime.fromtimestamp(retry_epoch, tz).isoformat()

        orig_status = lead_row.get("status") or "failed"
        if orig_status == "busy":
            analysis["disposition"] = "Busy"
        elif orig_status in ("no_answer", "no answer"):
            analysis["disposition"] = "No Answer"
        else:
            orig_disp = analysis.get("disposition")
            if orig_disp in ("Failed", "No Answer", "Busy", "Wrong Number", "Not Available", "Voicemail", "Voice Mail"):
                analysis["disposition"] = orig_disp
            else:
                analysis["disposition"] = "Failed"

        await update_lead_retry_state(lead_id, status=orig_status, extra=extra, analysis=analysis)
        await add_scheduled_callback(
            role=role,
            phone=lead_phone,
            name=f"{lead_name} (Retake {next_retry_count})",
            scheduled_at=retry_epoch,
            lead_id=lead_id,
        )
        logger.info(
            "Scheduled failed-call retry {}/{} (cooldown={}s, status={}, disposition={}) for lead {} ({}) at {}",
            next_retry_count, MAX_RETRIES, cooldown, orig_status,
            analysis["disposition"], lead_id, lead_phone, retry_epoch,
        )
    except Exception:
        logger.exception("Failed to schedule failed call retry for lead {}", lead_id)


async def _campaign_worker_role(role: str):
    """Campaign manager task that spawns concurrent sub-workers per phone line."""
    logger.info(f"Campaign manager for {role} started.")
    # Mark activity now so the stall watchdog does not treat a freshly started
    # worker as "stalled" before its first call has had time to complete.
    _LAST_WORKER_ACTIVITY[role] = time.time()
    await _recover_stale_dialing(role)

    state = get_state(role)
    v_cfg = state.get("vobiz", {}) or {}
    numbers = get_all_outbound_numbers(role, v_cfg)
    if not numbers:
        v_auth_id, v_token, v_from, v_base = resolve_vobiz_credentials(role, v_cfg)
        numbers = [v_from] if v_from else []

    if not numbers:
        logger.error(f"No phone numbers configured for role={role}. Stopping campaign.")
        await set_campaign_want_running(role, False)
        _CAMPAIGN_TASKS[role] = None
        return

    num_phones = len(numbers)
    logger.info(f"Spawning {num_phones} parallel sub-workers for {role}.")

    sub_tasks = []
    for idx, phone in enumerate(numbers):
        task = asyncio.create_task(
            _campaign_sub_worker_role(role, phone, idx, num_phones)
        )
        sub_tasks.append(task)

    try:
        await asyncio.gather(*sub_tasks)
    except asyncio.CancelledError:
        logger.info(f"Campaign manager for {role} cancelled. Cancelling sub-workers.")
        for t in sub_tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*sub_tasks, return_exceptions=True)
        raise
    except Exception as e:
        logger.exception(f"Campaign manager error for {role}: {e}")
    finally:
        logger.info(f"Campaign manager for {role} finished.")


async def _campaign_sub_worker_role(role: str, phone_number: str, phone_index: int, num_phones: int):
    """Worker task that dials leads for a specific phone number/index in parallel."""
    logger.info(f"Sub-worker {phone_index} ({phone_number}) for {role} started.")

    empty_since: float | None = None
    modulo = num_phones
    remainder = (phone_index + 1) % num_phones

    while True:
        try:
            if not _CAMPAIGN_TASKS.get(role):
                logger.info(f"Campaign task cancelled for {role} (sub-worker {phone_index}).")
                break

            # ── Check Daily Calling Limit (Max limit calls per phone per day) ──
            try:
                from core.storage import get_daily_call_count_for_phone
                daily_count = await get_daily_call_count_for_phone(phone_number)
                limit = settings.daily_call_limit_per_phone
                if daily_count >= limit:
                    logger.info(
                        "Outbound phone number {} has reached its daily limit of {} calls ({}/{}) today. Idling...",
                        phone_number,
                        limit,
                        daily_count,
                        limit,
                    )
                    await asyncio.sleep(60.0)
                    continue
            except Exception as daily_err:
                logger.warning("Failed to check daily call count for {}: {}", phone_number, daily_err)

            if is_campaign_quiet_hours(role=role):
                await asyncio.sleep(5.0)
                continue

            # ── Check Alternating Campaigns Turn ──
            if role in ("sales_1", "sales_2"):
                if not await check_and_acquire_alternating_turn(role):
                    await asyncio.sleep(1.0)
                    continue

            try:
                await promote_due_scheduled_callbacks(time.time())
            except Exception as e:
                logger.exception("promote_due_scheduled_callbacks failed")

            state = get_state(role)

            # ── Check for immediate scheduled callbacks atomically claimed ──
            try:
                from core.storage import claim_next_immediate_callback
                immediate_cb = await claim_next_immediate_callback(role, time.time())
                if immediate_cb:
                    if phone_is_busy(phone_number):
                        logger.info(
                            "Scheduled callback id={} for {} ({}) is due, but phone {} is busy/engaged. Waiting...",
                            immediate_cb["id"],
                            immediate_cb.get("name", "?"),
                            immediate_cb.get("phone", "?"),
                            phone_number,
                        )
                        await asyncio.sleep(2)
                        continue
                    empty_since = None
                    logger.info(
                        "Executing scheduled callback id={} for {} ({}) on sub-worker {}",
                        immediate_cb["id"],
                        immediate_cb.get("name", "?"),
                        immediate_cb.get("phone", "?"),
                        phone_index,
                    )
                    await _execute_scheduled_callback(role, immediate_cb, outbound_phone=phone_number)
                    continue
            except Exception as e:
                logger.exception("Immediate callback check failed")

            # ── Fetch pending leads partitioned to this worker ──
            pending = await get_leads(role, status="pending", limit=1000, modulo=modulo, remainder=remainder)

            # ── Filter out paused upload sources ──
            from core.storage import get_paused_sources_sync
            paused_sources = get_paused_sources_sync(role)
            if paused_sources:
                filtered = []
                for p in pending:
                    p_extra = {}
                    try:
                        raw_ext = p.get("extra", "{}")
                        p_extra = json.loads(raw_ext) if isinstance(raw_ext, str) else (raw_ext or {})
                    except Exception:
                        pass
                    p_src = p_extra.get("upload_source", "") or "Manual Calls / Direct Entry"
                    if p_src not in paused_sources:
                        filtered.append(p)
                if len(filtered) < len(pending):
                    logger.debug(f"Paused source filter: {len(pending) - len(filtered)} leads skipped for role={role}")
                pending = filtered

            # ── Filter out sources that hit daily call cap ──
            from core.storage import get_daily_call_count_for_source
            capped_sources: set[str] = set()
            seen_sources: set[str] = set()
            for p in pending:
                p_extra = {}
                try:
                    raw_ext = p.get("extra", "{}")
                    p_extra = json.loads(raw_ext) if isinstance(raw_ext, str) else (raw_ext or {})
                except Exception:
                    pass
                p_src = p_extra.get("upload_source", "") or "Manual Calls / Direct Entry"
                if p_src in seen_sources:
                    continue
                seen_sources.add(p_src)
                try:
                    daily_src_count = await get_daily_call_count_for_source(role, p_src)
                    if daily_src_count >= _CALLS_PER_SOURCE_DAILY_MAX:
                        capped_sources.add(p_src)
                except Exception:
                    pass
            if capped_sources:
                filtered2 = []
                for p in pending:
                    p_extra = {}
                    try:
                        raw_ext = p.get("extra", "{}")
                        p_extra = json.loads(raw_ext) if isinstance(raw_ext, str) else (raw_ext or {})
                    except Exception:
                        pass
                    p_src = p_extra.get("upload_source", "") or "Manual Calls / Direct Entry"
                    if p_src not in capped_sources:
                        filtered2.append(p)
                skipped_src = len(pending) - len(filtered2)
                if skipped_src:
                    logger.info(f"Source daily cap filter: {skipped_src} leads skipped for role={role}, capped sources={capped_sources}")
                pending = filtered2

            if not pending:
                try:
                    now_t = time.time()
                    from core.storage import role_has_pending_scheduled_callbacks as _has_sched_cb
                    has_future_cb = await role_has_future_callback_scheduled(role, now_t)
                    has_sched_cb = await _has_sched_cb(role)
                    if has_future_cb or has_sched_cb:
                        if role in ("sales_1", "sales_2") and active_vobiz_calls_for_role(role) == 0:
                            yield_alternating_turn(role)
                        empty_since = None
                        await _cancellable_sleep(role, 15.0)
                        continue
                except Exception as e:
                    logger.exception("Deferred callback idle check failed")

                if role in ("sales_1", "sales_2") and active_vobiz_calls_for_role(role) == 0:
                    yield_alternating_turn(role)

                if empty_since is None:
                    empty_since = time.time()
                    logger.info(f"Queue empty for {role} (sub-worker {phone_index}); waiting for new leads...")
                await asyncio.sleep(5.0)
                continue
            empty_since = None

            # Each sub-worker only waits for ITS OWN phone to be free.
            # Other phones in the same role can call simultaneously.
            if phone_is_busy(phone_number):
                logger.debug(
                    f"Phone {phone_number} busy for {role} sub-worker {phone_index} — waiting."
                )
                await asyncio.sleep(2)
                continue

            lead = pending[0]
            lead_id = lead["id"]
            lead_phone = lead["phone"]
            lead_name = lead.get("name", "Unknown")

            if await is_duplicate_lead(role, lead_phone, lead_id):
                logger.info(f"Skipping duplicate lead id={lead_id} phone={lead_phone}")
                await update_lead_status(lead_id, "failed", error="Duplicate lead skipped")
                continue

            from core.dnc import is_phone_blocked
            if is_phone_blocked(lead_phone):
                logger.warning(f"Skipping DNC blocked lead id={lead_id} phone={lead_phone}")
                await update_lead_status(lead_id, "failed", error="DNC blocked number")
                continue

            await update_lead_status(lead_id, "dialing")
            await update_lead_call_info(lead_id, start_time=time.time(), outbound_phone=phone_number)

            call_id = str(uuid.uuid4())
            _CAMPAIGN_DATA[call_id] = {
                **lead,
                "_lead_id": lead_id,
                "_leadIndex": -1,
                "_role": role,
                "_call_id": call_id,
                "_log_id": call_id,
            }

            v_cfg = state.get("vobiz", {}) or {}
            v_auth_id, v_token, _, v_base = resolve_vobiz_credentials(role, v_cfg)
            v_from = phone_number
            logger.info(f"Using phone number {v_from} for sub-worker {phone_index} of role {role}")

            if not v_auth_id or not v_token or not v_base or not v_from:
                logger.error(
                    f"Telephony not configured for role={role} (sub-worker {phone_index}): auth_id={'set' if v_auth_id else 'missing'}, "
                    f"token={'set' if v_token else 'missing'}, base={'set' if v_base else 'missing'}, "
                    f"from_number={'set' if v_from else 'missing'}."
                )
                await update_lead_status(lead_id, "failed", error="Telephony not configured")
                _CAMPAIGN_DATA.pop(call_id, None)
                break

            from services.vobiz_bridge import make_vobiz_call, VobizCallError
            slot_acquired = False
            sem_acquired = False
            role_sem_acquired = False
            try:
                if role in _ROLE_SEMAPHORES:
                    await _ROLE_SEMAPHORES[role].acquire()
                    role_sem_acquired = True
                await _GLOBAL_CALL_SEMAPHORE.acquire()
                sem_acquired = True
                try:
                    from services.campaign_live import set_active_campaign_call, clear_transcript_session
                    set_active_campaign_call(call_id)
                    clear_transcript_session(call_id)
                except Exception as _ce:
                    logger.exception("campaign_live setup skipped: {}", _ce)

                opening = _build_opening_line(lead, role)
                await _prime_opening_audio(call_id, role, opening)

                acquire_phone_slot(phone_number)   # mark THIS phone as busy
                acquire_vobiz_call_slot(role)       # update role-level dashboard counter
                slot_acquired = True
                logger.info(
                    f"Call initiated on sub-worker {phone_index}: {lead_name} ({lead_phone}) "
                    f"[role_active={active_vobiz_calls_for_role(role)} total={total_active_vobiz_calls()}]"
                )

                call_placed = False
                try:
                    await make_vobiz_call(
                        to=lead_phone, from_=v_from,
                        answer_url=f"{v_base}/vobiz/answer?camp_id={call_id}&role={role}",
                        auth_id=v_auth_id, auth_token=v_token
                    )
                    call_placed = True
                except VobizCallError as ve:
                    logger.error(
                        f"Vobiz refused call to {lead_phone} on sub-worker {phone_index}: HTTP {ve.status} — {ve.message}"
                    )
                    await update_lead_status(lead_id, "failed", error=f"Vobiz {ve.status}: {ve.message}")
                    await _schedule_failed_call_retry(role, lead_id, lead_phone, lead_name, reason="vobiz_error")
                    if role in ("sales_1", "sales_2"):
                        try:
                            from core.storage import get_lead_whatsapp_sent, mark_whatsapp_sent
                            from services.whatsapp_leads import send_whatsapp_project_details
                            if not await get_lead_whatsapp_sent(lead_id):
                                logger.info("Call failed (VobizCallError) — sending WhatsApp details for lead {}", lead_id)
                                wa_result = await send_whatsapp_project_details(lead_phone, summary="Following up with details.", lead_name=lead_name)
                                if wa_result.get("sent"):
                                    await mark_whatsapp_sent(lead_id)
                        except Exception as wa_err:
                            logger.exception("Failed to send WhatsApp for failed call: {}", wa_err)

                        # Email auto-send on failed Vobiz call
                        try:
                            from core.storage import get_lead_email_sent, mark_email_sent
                            from services.email_leads import send_email_project_details
                            email_to = (lead.get("email") or "").strip()
                            if email_to and "@" in email_to:
                                if not await get_lead_email_sent(lead_id):
                                    logger.info("Call failed (VobizCallError) — sending email details for lead {}", lead_id)
                                    email_result = await send_email_project_details(email_to, summary="Following up with details.")
                                    if email_result.get("sent"):
                                        await mark_email_sent(lead_id)
                        except Exception as email_err:
                            logger.exception("Failed to send Email for failed call: {}", email_err)

                if call_placed:
                    answered = False
                    call_started_at = time.time()
                    MAX_RING_WAIT = 40
                    MAX_TOTAL_WAIT = 360

                    while True:
                        if not _CAMPAIGN_TASKS.get(role):
                            break

                        info = _CAMPAIGN_DATA.get(call_id, {})
                        if not answered and info.get("_call_connected_at"):
                            answered = True
                            logger.info(f"Call connected on sub-worker {phone_index} with {lead_name} ({lead_phone})")
                        if answered and info.get("_call_ended_at"):
                            logger.info(f"Call ended naturally on sub-worker {phone_index} with {lead_name}")
                            break

                        elapsed = time.time() - call_started_at
                        if not answered and elapsed >= MAX_RING_WAIT:
                            logger.warning(f"No answer for {lead_name} ({lead_phone}) after {MAX_RING_WAIT}s — moving on.")
                            break
                        if elapsed >= MAX_TOTAL_WAIT:
                            logger.warning(f"Call to {lead_name} exceeded {MAX_TOTAL_WAIT}s — forcing next.")
                            break

                        lead_finalized = False
                        try:
                            rows = await get_leads(role, limit=2000)
                            for l in rows:
                                if l["id"] == lead_id and l["status"] in ("completed", "not_interested", "failed"):
                                    logger.info(f"Lead {lead_name} status finalized as {l['status']}")
                                    lead_finalized = True
                                    break
                        except Exception:
                            logger.exception("Lead status check failed")
                        if lead_finalized:
                            break

                        _LAST_WORKER_ACTIVITY[role] = time.time()
                        await asyncio.sleep(2)

                    if not answered:
                        logger.info(f"Lead {lead_name} did not connect — marking failed.")
                        await update_lead_status(lead_id, "failed", error="No answer / Timeout")
                        await _schedule_failed_call_retry(role, lead_id, lead_phone, lead_name, reason="no_answer")
                        if role in ("sales_1", "sales_2"):
                            try:
                                from core.storage import get_lead_whatsapp_sent, mark_whatsapp_sent
                                from services.whatsapp_leads import send_whatsapp_project_details
                                if not await get_lead_whatsapp_sent(lead_id):
                                    logger.info("Call failed (No answer / Timeout) — sending WhatsApp details for lead {}", lead_id)
                                    wa_result = await send_whatsapp_project_details(lead_phone, summary="Following up with details.", lead_name=lead_name)
                                    if wa_result.get("sent"):
                                        await mark_whatsapp_sent(lead_id)
                            except Exception as wa_err:
                                logger.exception("Failed to send WhatsApp for failed call: {}", wa_err)

                            # Email auto-send on No answer / Timeout
                            try:
                                from core.storage import get_lead_email_sent, mark_email_sent
                                from services.email_leads import send_email_project_details
                                email_to = (lead.get("email") or "").strip()
                                if email_to and "@" in email_to:
                                    if not await get_lead_email_sent(lead_id):
                                        logger.info("Call failed (No answer / Timeout) — sending email details for lead {}", lead_id)
                                        email_result = await send_email_project_details(email_to, summary="Following up with details.")
                                        if email_result.get("sent"):
                                            await mark_email_sent(lead_id)
                            except Exception as email_err:
                                logger.exception("Failed to send Email for failed call: {}", email_err)

                    # D2: Analyze completed campaign calls and update lead disposition
                    if answered and info.get("_call_ended_at"):
                        _log_for_analysis = (_CAMPAIGN_DATA.get(call_id) or {}).get("_log_id") or call_id
                        logger.info("D2: Analyzing completed call for {} ({}) log_id={}", lead_name, lead_phone, _log_for_analysis)
                        try:
                            await _analyze_and_update_lead(role, lead_id, _log_for_analysis)
                        except Exception as analyze_err:
                            logger.exception("D2: Post-call analysis failed for lead {}: {}", lead_id, analyze_err)

                    log_id = (_CAMPAIGN_DATA.get(call_id, {}) or {}).get("_log_id")
                    if log_id:
                        try:
                            await update_lead_call_info(lead_id, log_id=log_id, call_id=call_id)
                        except Exception as exc:
                            logger.exception(f"Persist log_id failed for lead {lead_id}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"Call trigger failed for {lead_phone}")
                await update_lead_status(lead_id, "failed", error=str(e))
                await _schedule_failed_call_retry(role, lead_id, lead_phone, lead_name, reason="exception")
                if role in ("sales_1", "sales_2"):
                    try:
                        from core.storage import get_lead_whatsapp_sent, mark_whatsapp_sent
                        from services.whatsapp_leads import send_whatsapp_project_details
                        if not await get_lead_whatsapp_sent(lead_id):
                            logger.info("Call failed (Exception) — sending WhatsApp details for lead {}", lead_id)
                            wa_result = await send_whatsapp_project_details(lead_phone, summary="Following up with details.", lead_name=lead_name)
                            if wa_result.get("sent"):
                                await mark_whatsapp_sent(lead_id)
                    except Exception as wa_err:
                        logger.exception("Failed to send WhatsApp for failed call: {}", wa_err)

                    # Email auto-send on Exception fail
                    try:
                        from core.storage import get_lead_email_sent, mark_email_sent
                        from services.email_leads import send_email_project_details
                        email_to = (lead.get("email") or "").strip()
                        if email_to and "@" in email_to:
                            if not await get_lead_email_sent(lead_id):
                                logger.info("Call failed (Exception) — sending email details for lead {}", lead_id)
                                email_result = await send_email_project_details(email_to, summary="Following up with details.")
                                if email_result.get("sent"):
                                    await mark_email_sent(lead_id)
                    except Exception as email_err:
                        logger.exception("Failed to send Email for failed call: {}", email_err)
            finally:
                if slot_acquired:
                    release_phone_slot(phone_number)    # free THIS phone's slot
                    release_vobiz_call_slot(role)       # update role-level dashboard counter
                    logger.info(
                          f"Call slot released for {role} phone={phone_number} sub-worker {phone_index} "
                          f"[role_active={active_vobiz_calls_for_role(role)} total={total_active_vobiz_calls()}]"
                    )
                    if role in ("sales_1", "sales_2") and active_vobiz_calls_for_role(role) == 0:
                        yield_alternating_turn(role)
                    # Process any queued inbound callers now that we're free
                    try:
                        from core.state import process_inbound_queue
                        asyncio.create_task(process_inbound_queue(role))
                    except Exception as _qe:
                        logger.error(f"Queue processing error for {role}: {_qe}")
                _CAMPAIGN_DATA.pop(call_id, None)
                if sem_acquired:
                    await asyncio.sleep(1.0)
                    _GLOBAL_CALL_SEMAPHORE.release()
                if role_sem_acquired:
                    _ROLE_SEMAPHORES[role].release()


            if not _CAMPAIGN_TASKS.get(role):
                break

            # ── Check for immediate scheduled callbacks ──
            while True:
                if not _CAMPAIGN_TASKS.get(role):
                    break
                if is_campaign_quiet_hours(role=role):
                    break
                from core.storage import claim_next_immediate_callback
                immediate_cb = await claim_next_immediate_callback(role, time.time())
                if not immediate_cb:
                    break
                logger.info(
                    "Executing scheduled callback id={} for {} ({}) on sub-worker {}",
                    immediate_cb["id"],
                    immediate_cb.get("name", "?"),
                    immediate_cb.get("phone", "?"),
                    phone_index,
                )
                await _execute_scheduled_callback(role, immediate_cb, outbound_phone=phone_number)

            if not _CAMPAIGN_TASKS.get(role):
                break

            gap = await inter_call_gap_seconds_for_phone(phone_number, role)
            if not await _cancellable_sleep(role, gap):
                break

        except asyncio.CancelledError:
            logger.info(f"Sub-worker {phone_index} for {role} cancelled.")
            break
        except Exception as e:
            logger.exception(f"Sub-worker {phone_index} error for {role}")
            await asyncio.sleep(10)

    logger.info(f"Sub-worker {phone_index} for {role} finished.")


# ─── Campaign Scheduler ───────────────────────────────────────────────
# Polls the ``schedules`` table every ``_SCHEDULER_POLL_SEC`` seconds and,
# for each row whose ``run_at`` has been reached and ``status='scheduled'``,
# starts the same per-role campaign worker the **Start Campaign** button
# triggers — so a user can upload a CSV in the morning and have it dial out
# automatically at, say, 5 PM.

_SCHEDULER_POLL_SEC = float(os.getenv("CAMPAIGN_SCHEDULER_POLL_SEC", "30"))


async def _run_scheduled_campaign(
    role: str,
    schedule_id: int,
    stop_at: float | None = None,
):
    """Wrapper that ties a schedule row's lifecycle to a campaign worker run.

    Uses the same ``_CAMPAIGN_TASKS[role]`` slot the manual toggle uses so the
    Stop button, status endpoint, and dashboard pill all reflect the run
    correctly without any extra plumbing.

    If ``stop_at`` (epoch-UTC seconds) is provided, the campaign is forcibly
    stopped at that moment by cancelling the worker task. The schedule row is
    marked ``completed`` (not ``cancelled``) because reaching the end of the
    operator-defined window is the intended terminal state, not a failure.
    """
    stop_watcher: asyncio.Task | None = None
    stopped_by_window = False

    async def _window_stop_watcher() -> None:
        """Sleep until ``stop_at`` then cancel the campaign worker."""
        nonlocal stopped_by_window
        if stop_at is None:
            return
        delay = max(0.0, float(stop_at) - time.time())
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        active = _CAMPAIGN_TASKS.get(role)
        if active and not active.done():
            # Wait for active Vobiz call to finish before cancelling
            while role_has_active_vobiz_call(role):
                logger.info(
                    "Scheduler stop window reached for role={}, but active call is in progress. "
                    "Waiting for call to complete before cancelling worker...",
                    role,
                )
                await asyncio.sleep(5.0)
            stopped_by_window = True
            logger.info(
                f"Scheduled campaign id={schedule_id} role={role!r}: "
                f"stop window reached — cancelling worker."
            )
            active.cancel()

    try:
        await mark_schedule_status(schedule_id, "running", started_at=time.time())
        task = asyncio.create_task(_campaign_worker_role(role))
        _CAMPAIGN_TASKS[role] = task
        if stop_at is not None:
            stop_watcher = asyncio.create_task(_window_stop_watcher())

        try:
            await task
        except asyncio.CancelledError:
            # If we cancelled the worker because the stop window expired, treat
            # it as a clean completion. Otherwise (Stop button / process
            # shutdown), surface as cancelled.
            if stopped_by_window:
                await mark_schedule_status(schedule_id, "completed")
                logger.info(
                    f"Scheduled campaign id={schedule_id} role={role!r} "
                    f"→ completed (auto-stopped at end of window)"
                )
                return
            raise
        # Worker exited naturally (queue empty + grace period).
        await mark_schedule_status(schedule_id, "completed")
        logger.info(f"Scheduled campaign id={schedule_id} role={role!r} → completed")
    except asyncio.CancelledError:
        await mark_schedule_status(
            schedule_id, "cancelled", error="Run cancelled before completion."
        )
        logger.info(f"Scheduled campaign id={schedule_id} role={role!r} → cancelled")
        raise
    except Exception as e:
        await mark_schedule_status(schedule_id, "failed", error=str(e)[:300])
        logger.exception(f"Scheduled campaign id={schedule_id} role={role!r} failed")
    finally:
        if stop_watcher and not stop_watcher.done():
            stop_watcher.cancel()


async def _schedule_preflight(role: str) -> str | None:
    """Mirror of the checks in ``/api/campaign/toggle``. Returns an error string
    if the run cannot be started right now, else ``None``.
    """
    from core.storage import is_campaign_globally_paused

    if await is_campaign_globally_paused():
        return (
            "Campaign is paused. Outbound dialing will not run until you click "
            "Start during calling hours (9:30 AM – 8:30 PM IST)."
        )
    if is_campaign_quiet_hours(role=role):
        return quiet_hours_block_message(role=role)
    running = _CAMPAIGN_TASKS.get(role)
    if running and not running.done():
        return "A campaign is already running for this role."
    counts = await get_lead_counts(role)
    if counts.get("pending", 0) <= 0 and counts.get("dialing", 0) <= 0:
        return "No pending leads at scheduled time. Upload a list before the schedule fires."
    state = get_state(role)
    v_cfg = state.get("vobiz", {}) or {}
    auth_id, auth_token, _from_num, base_url = resolve_vobiz_credentials(role, v_cfg)
    if not auth_id or not auth_token or not base_url:
        return "Telephony bridge not configured (Vobiz Auth ID / Token / Public URL missing)."
    return None


async def _enforce_window_stop(sched: dict) -> None:
    """Force-stop a scheduled run whose stop window has expired.

    Two cases:
      a) The campaign worker is still running in this process → cancel it.
         The wrapper's CancelledError handler will mark the schedule as
         ``completed`` (because ``stopped_by_window`` is True after we cancel).
         Actually — the wrapper only flips ``stopped_by_window`` inside its
         own watcher. Since this enforcement path comes from the polling loop
         (e.g. after a server restart that lost the inline watcher), we mark
         the row directly and rely on the worker's CancelledError path to
         exit cleanly.
      b) No worker is running for this role (process restart, manual Stop) →
         just close out the row.
    """
    schedule_id = int(sched.get("id") or 0)
    role = normalize_console_role(sched.get("role") or "sales_1")
    if not schedule_id:
        return
    active = _CAMPAIGN_TASKS.get(role)
    if active and not active.done():
        logger.info(
            f"Scheduler: stop window reached for id={schedule_id} role={role!r} "
            f"after restart — cancelling worker."
        )
        active.cancel()
    await mark_schedule_status(
        schedule_id, "completed",
        error=None,
    )


async def _scheduler_loop():
    """Long-running task that fires due schedules. Cancel-safe.

    Two responsibilities every poll:
      1. Start any ``scheduled`` rows whose ``run_at`` has passed.
      2. Force-stop any ``running`` rows whose ``stop_at`` has passed (the
         inline stop watcher handles the happy path; this is the safety net
         for process restarts).
    """
    logger.info(f"Campaign scheduler started (poll every {_SCHEDULER_POLL_SEC:.0f}s).")
    _last_quiet_hours_state = None
    while True:
        try:
            now = time.time()

            try:
                await promote_due_scheduled_callbacks(now)
            except Exception as e:
                logger.exception("Scheduler: promote_due_scheduled_callbacks failed")

            # ── 1. Fire due schedules ──
            try:
                due = await due_schedules(now)
            except Exception as e:
                logger.exception("Scheduler: due_schedules query failed")
                due = []

            for sched in due:
                schedule_id = int(sched.get("id") or 0)
                role = normalize_console_role(sched.get("role") or "sales_1")
                stop_at = sched.get("stop_at")
                if not schedule_id:
                    continue

                err = await _schedule_preflight(role)
                if err:
                    await mark_schedule_status(schedule_id, "failed", error=err)
                    logger.warning(
                        f"Scheduled campaign id={schedule_id} role={role!r} skipped — {err}"
                    )
                    continue

                # Edge case: stop_at already passed before we even fired (clock
                # skew / very short window). Don't bother starting.
                if stop_at is not None and float(stop_at) <= now:
                    await mark_schedule_status(
                        schedule_id, "failed",
                        error="Stop time passed before the campaign could start.",
                    )
                    logger.warning(
                        f"Scheduled campaign id={schedule_id} role={role!r} "
                        f"stop_at already past — not starting."
                    )
                    continue

                logger.info(
                    f"Scheduled campaign id={schedule_id} role={role!r} firing now "
                    f"(name={sched.get('name')!r}, stop_at={stop_at})"
                )
                # Don't await — let it run in the background while we keep polling.
                task = asyncio.create_task(
                    _run_scheduled_campaign(role, schedule_id, stop_at=stop_at)
                )
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)

            # ── 2. Enforce stop windows (safety net for restarts) ──
            try:
                expired = await expired_running_schedules(now)
            except Exception as e:
                logger.exception("Scheduler: expired_running query failed")
                expired = []
            for sched in expired:
                await _enforce_window_stop(sched)

            # ── 2b. Dispatch 24h WhatsApp Polite Reminders ──
            try:
                from core.storage import get_due_whatsapp_reminders, mark_whatsapp_reminder_sent
                from services.whatsapp_leads import send_whatsapp_text_message

                due_reminders = await get_due_whatsapp_reminders(now - 86400)
                for lead in due_reminders:
                    lead_id = lead["id"]
                    lead_name = lead.get("name", "there") or "there"
                    phone = lead.get("phone")
                    if not phone:
                        continue
                    text = f"Hi {lead_name}, I hope you had a chance to go through the details of OpusHire we shared yesterday. Are you interested in learning how it can help your hiring team cut time-to-hire in half? Please let us know."
                    logger.info("Sending WhatsApp polite reminder to lead_id={} ({})", lead_id, phone)
                    try:
                        res = await send_whatsapp_text_message(phone, text)
                        await mark_whatsapp_reminder_sent(lead_id)
                        logger.info("WhatsApp reminder sent successfully for lead_id={} result={}", lead_id, res)
                    except Exception as wa_err:
                        logger.error("Failed to send WhatsApp reminder to lead_id={} : {}", lead_id, wa_err)
            except Exception as e:
                logger.exception("Scheduler: whatsapp reminders dispatch failed")

            # ── 2c. Hot-reload prompt & RAG from files (every 5 min) ──
            try:
                _last_rag_sync = getattr(_scheduler_loop, "_last_rag_sync", 0.0)
                if now - _last_rag_sync > 300:
                    from core.role_sandbox import sync_all_role_sandboxes_on_startup
                    sync_all_role_sandboxes_on_startup()
                    _scheduler_loop._last_rag_sync = now
                    logger.debug("Scheduler: synced prompt/RAG from files to DB state")
                    # Refresh the chunked RAG index for every console role using
                    # the freshly-synced role rag text. Failure is non-fatal.
                    try:
                        from rag import index_role_rag
                        for _r in ("sales_1", "sales_2"):
                            try:
                                _st = get_state(_r)
                                _n = index_role_rag(_r, _st.get("rag") or "")
                                logger.debug(
                                    "Scheduler: RAG chunk index for {} has {} chunks",
                                    _r,
                                    _n,
                                )
                            except Exception as _e:
                                logger.warning(
                                    "Scheduler: RAG chunk index refresh failed for {}: {}",
                                    _r,
                                    _e,
                                )
                    except Exception as _e:
                        logger.warning("Scheduler: RAG chunk index refresh skipped: {}", _e)
            except Exception as e:
                logger.warning("Scheduler: prompt/RAG sync failed: {}", e)

            # ── 3. Execute due scheduled callbacks for idle roles (only in allowed hours) ──
            from core.storage import is_campaign_globally_paused as _is_paused
            if not await _is_paused():
                for _role in ("sales_1", "sales_2"):
                    if is_campaign_quiet_hours(role=_role):
                        continue  # Role-specific quiet window — skip callbacks for this role
                    try:
                        active = _CAMPAIGN_TASKS.get(_role)
                        if active and not active.done():
                            continue  # Campaign worker is running — it handles callbacks
                        if role_has_active_vobiz_call(_role):
                            continue  # Role is busy on a call
                        if _role in _callback_tasks_in_flight:
                            continue  # Already dispatching a callback for this role
                        # Idle role: check for immediate callbacks and execute them
                        cb = await get_next_immediate_callback(_role, now)
                        if cb is not None:
                            logger.info(
                                "Scheduler: executing idle-role callback id={} for {} ({})",
                                cb["id"],
                                cb.get("name", "?"),
                                cb.get("phone", "?"),
                            )
                            _callback_tasks_in_flight.add(_role)
                            # Run in background so we don't block the scheduler poll
                            async def _run_cb_and_clear_flag(r=_role, cb_=cb):
                                try:
                                    await _execute_scheduled_callback(r, cb_)
                                finally:
                                    _callback_tasks_in_flight.discard(r)
                            task = asyncio.create_task(_run_cb_and_clear_flag())
                            _background_tasks.add(task)
                            task.add_done_callback(_background_tasks.discard)
                    except Exception as e:
                        logger.exception("Scheduler: immediate callback check failed for role={}", _role)

            # ── 4. Auto-start campaigns during allowed hours (9:30 AM – 7:30 PM) ──
            try:
                from core.state import _MANUALLY_STOPPED_ROLES
                from core.storage import is_campaign_globally_paused, set_campaign_globally_paused
                # is_campaign_quiet_hours is imported at module level (line 50).
                # This is the scheduler-wide quiet→allowed transition detector
                # covering ALL roles, so pass role=None to keep the legacy
                # global quiet-hours behavior (per-role windows are enforced
                # at the role-scoped call sites above).
                current_quiet_hours = is_campaign_quiet_hours(role=None)

                if _last_quiet_hours_state is not None and _last_quiet_hours_state == True and current_quiet_hours == False:
                    # Transition to allowed hours (e.g., 9:30 AM IST)
                    logger.info("Scheduler transition: entering allowed hours. Clearing manual stops, unpausing campaigns, and starting tasks.")
                    _MANUALLY_STOPPED_ROLES.clear()
                    await set_campaign_globally_paused(False)
                    for _role in ("sales_1", "sales_2"):
                        await set_campaign_want_running(_role, True)
                        active = _CAMPAIGN_TASKS.get(_role)
                        if not active or active.done():
                            counts = await get_lead_counts(_role)
                            pending = int(counts.get("pending", 0) or 0)
                            if pending > 0:
                                logger.info(
                                    "Scheduler auto-start: starting campaign for role={} as allowed hours are active and pending leads exist.",
                                    _role,
                                )
                                _CAMPAIGN_TASKS[_role] = asyncio.create_task(_campaign_worker_role(_role))

                elif _last_quiet_hours_state is not None and _last_quiet_hours_state == False and current_quiet_hours == True:
                    # Transition to quiet hours (e.g., 7:30 PM IST)
                    logger.info("Scheduler transition: entering quiet hours. Cancelling tasks, pausing campaigns globally, and releasing dialing leads.")
                    await set_campaign_globally_paused(True)
                    for _role in ("sales_1", "sales_2"):
                        await set_campaign_want_running(_role, False)
                        active = _CAMPAIGN_TASKS.get(_role)
                        if active and not active.done():
                            active.cancel()
                            _CAMPAIGN_TASKS[_role] = None
                        # Release dialing leads back to pending so they can be retried tomorrow
                        await release_orphaned_dialing_leads(
                            _role,
                            to_status="pending",
                            error="Campaign paused: quiet hours (7:30 PM – 9:30 AM IST) reached.",
                        )
                    # ── Send EOD report email ──
                    try:
                        from services.email_leads import send_report_email
                        report_result = await send_report_email()
                        logger.info("EOD report email result: {}", report_result)
                    except Exception as e:
                        logger.exception("Failed to send EOD report email: {}", e)

                # Track state & run normal loop checks
                _last_quiet_hours_state = current_quiet_hours

                if current_quiet_hours:
                    if _MANUALLY_STOPPED_ROLES:
                        logger.info("Scheduler: quiet hours active — clearing manually stopped override list.")
                        _MANUALLY_STOPPED_ROLES.clear()
                else:
                    # Allowed hours: auto-start if not running, not manually stopped, and not globally paused
                    if not await is_campaign_globally_paused():
                        for _role in ("sales_1", "sales_2"):
                            if _role not in _MANUALLY_STOPPED_ROLES:
                                active = _CAMPAIGN_TASKS.get(_role)
                                if not active or active.done():
                                    counts = await get_lead_counts(_role)
                                    pending = int(counts.get("pending", 0) or 0)
                                    if pending > 0:
                                        logger.info(
                                            "Scheduler: auto-starting campaign for role={} as allowed hours are active and pending leads exist.",
                                            _role,
                                        )
                                        await set_campaign_want_running(_role, True)
                                        _CAMPAIGN_TASKS[_role] = asyncio.create_task(_campaign_worker_role(_role))
                        # Stall watchdog: a worker task that is still alive but blocked
                        # (e.g. a hung call WebSocket) is NOT done(), so the auto-start
                        # above never fires. Restart it if it goes silent while leads remain.
                        _STALL_SEC = float(os.getenv("CAMPAIGN_STALL_WATCHDOG_SEC", "600"))
                        for _role in ("sales_1", "sales_2"):
                            if _role in _MANUALLY_STOPPED_ROLES:
                                continue
                            _active = _CAMPAIGN_TASKS.get(_role)
                            if not _active or _active.done():
                                continue
                            _last = _LAST_WORKER_ACTIVITY.get(_role, 0.0)
                            if (now - _last) <= _STALL_SEC:
                                continue
                            _counts = await get_lead_counts(_role)
                            _pending = int(_counts.get("pending", 0) or 0)
                            if _pending <= 0:
                                continue
                            if active_vobiz_calls_for_role(_role) > 0:
                                logger.debug("Watchdog: role={} has active calls — skipping restart", _role)
                                continue
                            logger.warning(
                                "Scheduler watchdog: role={} worker stalled (no activity {}s, pending={}) - restarting.",
                                _role, int(now - _last), _pending,
                            )
                            _active.cancel()
                            try:
                                await asyncio.wait_for(_active, timeout=5.0)
                            except BaseException:
                                pass
                            await release_orphaned_dialing_leads(
                                _role,
                                to_status="pending",
                                error="Watchdog restart: detected stalled worker (no call activity).",
                            )
                            await set_campaign_want_running(_role, True)
                            _CAMPAIGN_TASKS[_role] = asyncio.create_task(_campaign_worker_role(_role))
            except Exception as e:
                logger.exception("Scheduler: campaign auto-start checks failed")

        except asyncio.CancelledError:
            logger.info("Campaign scheduler cancelled.")
            raise
        except Exception as e:
            logger.exception("Scheduler loop iteration error")

        # Sleep in slices so cancellation is responsive even if poll interval is large.
        slept = 0.0
        while slept < _SCHEDULER_POLL_SEC:
            await asyncio.sleep(min(1.0, _SCHEDULER_POLL_SEC - slept))
            slept += 1.0
