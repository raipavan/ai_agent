"""Campaign management routes — SQLite-backed, production-ready."""

from __future__ import annotations

import asyncio
import csv
import datetime
import io
import re

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from core.utils import range_file_response
from loguru import logger
from pydantic import BaseModel, Field

from config import settings
from core import storage as lead_storage
from core.state import (
    save_role_state, add_leads_bulk,
    update_lead_status, reset_leads, wipe_leads,
    export_leads_csv, _CAMPAIGN_TASKS, total_active_vobiz_calls,
    normalize_console_role,
    _ROLES,
    get_state,
    get_inbound_queue_length,
)
from core.campaign_payload import (
    build_campaign_state_dashboard_fields,
    enrich_lead_for_console,
    slim_lead_for_api,
)
from core.campaign_hours import get_campaign_hours_status
from core.worker import (
    _campaign_worker_role,
    _analyze_and_update_lead,
    inter_call_gap_seconds_for_role,
    _read_transcript_jsonl,
    release_orphaned_dialing_leads,
)

from core.phone_norm import norm_phone_str as _norm_phone_str
from services.call_recording import resolve_session_recording_path
from services.excel_report import get_report_kpi_summary
from core.outbound_numbers import get_all_outbound_numbers
from core import kv_cache

router = APIRouter(prefix="/api/campaign", tags=["campaign"])


def _jwt_payload_from_request(request: Request) -> dict | None:
    """Bearer header or ``access_token`` / ``token`` query (for ``<audio src>`` playback)."""
    from core.auth import _decode_jwt

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        payload = _decode_jwt(auth[7:])
        if payload:
            return payload
    for key in ("access_token", "token"):
        raw = (request.query_params.get(key) or "").strip()
        if raw:
            payload = _decode_jwt(raw)
            if payload:
                return payload
    return None


def _campaign_role(request: Request) -> str:
    """Resolve role from query param first, then JWT, then default."""
    from core.state import normalize_console_role
    
    # Check query parameter first (frontend sends ?role=sales_1 or ?role=sales_2)
    role_param = request.query_params.get("role", "").strip()
    if role_param:
        normalized = normalize_console_role(role_param)
        return normalized
    
    # Fall back to JWT
    from core.auth import console_role_from_request
    return console_role_from_request(request, default="sales_1")


def _sanitize_tabular_rows(rows: list[dict]) -> list[dict]:
    """Normalize CSV/XLS headers: strip BOM, trim keys and string cell values."""
    fixed: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        nr: dict = {}
        for k, v in r.items():
            nk = str(k).replace("\ufeff", "").strip() if k is not None else ""
            if not nk:
                nk = str(k)
            nv = "" if v is None else str(v).strip()
            nr[nk] = nv
        fixed.append(nr)
    return fixed


def _extract_phone_cell(row: dict, phone_hint: str | None, norm_phone) -> str:
    """Use auto-detected phone column first, then scan every cell for a dialable number."""
    keys = list(row.keys())
    order: list[str] = []
    if phone_hint:
        h = str(phone_hint).strip()
        if h in keys:
            order.append(h)
    order.extend(k for k in keys if k not in order)
    for k in order:
        cand = norm_phone(str(row.get(k, "") or "").strip())
        if cand:
            return cand
    return ""


def _looks_like_row_index_header(col: str) -> bool:
    """Headers such as ``S.No``, ``#``, ``ID`` — not person's name / company."""

    raw = str(col or "").strip()
    if not raw:
        return False
    hn = re.sub(r"[^\w+#]+", " ", raw.strip().lower()).strip().replace(".", "").replace("_", "")
    if not hn.replace("#", ""):
        return True
    if any(tok in hn for tok in ("name", "fullname", "first name", "person", "contact name", "customer name", "lead name")):
        return False
    compact = hn.replace(" ", "")
    needles = ("sno", "slno", "serialno", "linenumber", "lineno", "rownum", "rownumber")
    if any(n in compact for n in needles):
        return True
    if hn in {"id", "#", "sn", "sl", "index", "rank", "serial", "row"} or hn.endswith(" id"):
        return True
    if hn.startswith("unnamed"):
        return True
    if hn.startswith("col") and len(hn) > 3 and hn[3:].isdigit():
        return True
    return False


def _column_values_mostly_row_numbers(values: list[str], threshold: float = 0.7) -> bool:
    """True when cells look like spreadsheet row counters (``11.0``, ``10``…) not people."""

    nonempty: list[str] = []
    for v in values:
        t = str(v or "").strip().replace(",", "").replace(" ", "")
        if t:
            nonempty.append(t)
    if len(nonempty) < 3:
        return False
    pat = re.compile(r"^-?\d+(?:\.(?:0+|00+))?$")
    hits = sum(1 for t in nonempty if pat.fullmatch(t))
    return hits / len(nonempty) >= threshold


_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _is_phone_value(val: str) -> bool:
    v = val.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "").replace("+", "").replace(".", "")
    return v.isdigit() and 7 <= len(v) <= 15


def _is_email_value(val: str) -> bool:
    return bool(_EMAIL_RE.match(val.strip()))


def _score_column(values: list, check_fn) -> float:
    if not values:
        return 0.0
    hits = sum(1 for v in values if v and check_fn(str(v)))
    return hits / len(values)


def _is_product_like_column(col: str) -> bool:
    """RFQ sheets often label product/subject columns — never use as contact name."""
    cl = col.strip().lower()
    product_keys = (
        "product", "subject", "rfq", "specification", "spec", "category",
        "item", "description", "requirement", "material", "commodity",
        "goods", "particulars", "enquiry", "inquiry",
    )
    return any(kw in cl for kw in product_keys)


def detect_lead_columns(rows: list[dict], upload_role: str = "sales_1") -> dict:
    """Auto-detect phone / name / email / company columns in an uploaded sheet.

    HR lead lists carry just ``Name`` + ``Mobile Number``; header keywords win
    for the dial column whenever its values look dialable at all.
    """
    if not rows:
        return {}
    cols = list(rows[0].keys())
    sample = rows[:30]
    col_values = {c: [str(r.get(c, "") or "") for r in sample] for c in cols}

    phone_scores = {c: _score_column(col_values[c], _is_phone_value) for c in cols}
    email_scores = {c: _score_column(col_values[c], _is_email_value) for c in cols}

    email_col = max(email_scores, key=email_scores.get) if email_scores else None
    if email_col and email_scores[email_col] < 0.3:
        email_col = None

    # Header-first phone pick: HR lead sheets name the dial column
    # ``Mobile Number`` / ``Phone`` / ``WhatsApp No`` — trust the header
    # whenever at least some of its values parse as a dialable number.
    phone_col = None
    for c in cols:
        cl = str(c).strip().lower()
        if not any(w in cl for w in ("mobile", "phone", "whatsapp", "contact")):
            continue
        if _looks_like_row_index_header(c) or phone_scores.get(c, 0.0) <= 0:
            continue
        if phone_col is None or phone_scores[c] > phone_scores[phone_col]:
            phone_col = c

    if phone_col is None:
        phone_col = max(phone_scores, key=phone_scores.get) if phone_scores else None
        if phone_col and phone_scores[phone_col] < 0.3:
            phone_col = None

    if phone_col is None and phone_scores:
        bk = max(phone_scores, key=phone_scores.get)
        if phone_scores[bk] > 0:
            phone_col = bk

    text_cols = [c for c in cols if c not in (phone_col, email_col)]
    NAME_KEYWORDS = ['name', 'person', 'client', 'buyer', 'seller', 'agent', 'contact', 'lead', 'customer', 'hr']
    COMPANY_KEYWORDS = ['company', 'business', 'organization', 'org', 'firm', 'brand', 'employer', 'shop', 'store', 'enterprise']
    if upload_role == "rfqs":
        # RFQ lists: company = buyer org; product/subject must not become ``name``.
        COMPANY_KEYWORDS = COMPANY_KEYWORDS + ['buyer', 'customer', 'account', 'organisation', 'organization name']

    def _col_matches(col: str, keywords: list) -> bool:
        cl = col.strip().lower()
        return any(kw in cl for kw in keywords)

    def _bad_for_contact_field(c: str) -> bool:
        if upload_role == "rfqs" and _is_product_like_column(c):
            return True
        return bool(
            _looks_like_row_index_header(c)
            or _column_values_mostly_row_numbers(col_values.get(c, []))
        )

    product_cols = {c for c in text_cols if _is_product_like_column(c)} if upload_role == "rfqs" else set()

    name_col = company_col = None
    for c in text_cols:
        if _bad_for_contact_field(c):
            continue
        if c.strip().lower() in ('name', 'full name', 'first name', 'contact name', 'customer name', 'hr name', 'hr contact', 'hr'):
            name_col = c
            break
    for c in text_cols:
        if _bad_for_contact_field(c):
            continue
        if c.strip().lower() in ('company', 'company name', 'business', 'organization'):
            company_col = c
            break
    if not name_col:
        for c in text_cols:
            if c == company_col or _bad_for_contact_field(c):
                continue
            if _col_matches(c, NAME_KEYWORDS):
                name_col = c
                break
    if not company_col:
        for c in text_cols:
            if c == name_col or _bad_for_contact_field(c):
                continue
            if _col_matches(c, COMPANY_KEYWORDS):
                company_col = c
                break

    remaining = [c for c in text_cols if c not in (name_col, company_col)]

    def _pick_fallback(candidates: list[str]):
        for c in candidates:
            if _bad_for_contact_field(c):
                continue
            return c
        return None

    if not name_col:
        name_col = _pick_fallback([c for c in remaining if c not in product_cols])
        if name_col:
            remaining = [c for c in remaining if c != name_col]
    if not company_col:
        company_col = _pick_fallback(remaining)

    logger.info(
        f"Auto-detected columns for {upload_role} → phone:{phone_col}, name:{name_col}, "
        f"email:{email_col}, company:{company_col}"
    )
    return {
        "phone": phone_col,
        "name": name_col,
        "email": email_col,
        "company": company_col,
        "product_cols": list(product_cols) if product_cols else [],
    }


@router.get("/sources")
async def campaign_sources(request: Request):
    """List all upload sources for this role with lead counts and pause status."""
    role = _campaign_role(request)
    paused_sources = await lead_storage.get_paused_sources(role)

    sources = await lead_storage.get_campaign_sources(role, paused_sources)
    return {"sources": sources, "paused_sources": paused_sources}


@router.post("/sources/toggle")
async def campaign_source_toggle(request: Request):
    """Toggle pause/play for a specific upload source."""
    role = _campaign_role(request)
    body = await request.json()
    source_name = body.get("source", "")
    if not source_name:
        raise HTTPException(status_code=400, detail="Missing 'source' field")

    paused_sources = await lead_storage.get_paused_sources(role)

    if source_name in paused_sources:
        paused_sources.remove(source_name)
    else:
        paused_sources.append(source_name)

    await lead_storage.set_paused_sources(role, paused_sources)
    kv_cache.invalidate_role(role)
    return {"paused_sources": paused_sources, "toggled": source_name}


@router.post("/sources/run-only")
async def campaign_source_run_only(request: Request):
    """Sandbox mode: pause ALL sources except the given one so only its leads are dialed.

    Pass ``source=""`` (empty string) to exit sandbox mode and resume all sources.
    """
    role = _campaign_role(request)
    body = await request.json()
    source_name = (body.get("source") or "").strip()

    all_sources_data = await lead_storage.get_campaign_sources(role, [])
    all_source_names = [s["name"] for s in all_sources_data]

    if not source_name:
        # Exit sandbox: clear all pauses
        await lead_storage.set_paused_sources(role, [])
        kv_cache.invalidate_role(role)
        return {"mode": "all_running", "paused_sources": [], "active_source": None}

    if source_name not in all_source_names:
        raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")

    # Pause everything EXCEPT the selected source
    paused = [s for s in all_source_names if s != source_name]
    await lead_storage.set_paused_sources(role, paused)
    kv_cache.invalidate_role(role)
    logger.info(f"Sandbox mode: role={role} running only '{source_name}', paused {len(paused)} other sources")
    return {
        "mode": "sandbox",
        "active_source": source_name,
        "paused_sources": paused,
        "paused_count": len(paused),
    }


@router.post("/upload")
async def upload_leads(file: UploadFile = File(...), request: Request = None):
    try:
        role = _campaign_role(request) if request else "sales_1"
        content = await file.read()
        filename = (file.filename or "").lower()

        rows = []
        headers = []
        try:
            if filename.endswith('.xlsx'):
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
                ws = wb.active
                all_rows = list(ws.iter_rows(values_only=True))
                if all_rows:
                    headers = [str(c or f"col{i}").strip() for i, c in enumerate(all_rows[0])]
                    for row in all_rows[1:]:
                        rows.append({headers[i]: str(v or "") for i, v in enumerate(row) if i < len(headers)})
            elif filename.endswith('.xls'):
                import xlrd
                wb = xlrd.open_workbook(file_contents=content)
                ws = wb.sheet_by_index(0)
                if ws.nrows > 0:
                    headers = [str(ws.cell_value(0, c)).strip() for c in range(ws.ncols)]
                    for r in range(1, ws.nrows):
                        rows.append({headers[c]: str(ws.cell_value(r, c)) for c in range(ws.ncols)})
            else:
                decoded = None
                for enc in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
                    try:
                        decoded = content.decode(enc)
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue
                if not decoded:
                    decoded = content.decode('latin-1', errors='replace')
                decoded = decoded.replace('\r\n', '\n').replace('\r', '\n')
                reader = csv.DictReader(io.StringIO(decoded))
                rows = list(reader)
                headers = reader.fieldnames or (list(rows[0].keys()) if rows else [])
        except Exception as e:
            logger.error(f"File parse error: {e}")
            raise HTTPException(status_code=422, detail=f"Could not parse file: {e}")

        if not rows:
            return {"status": "ok", "count": 0, "leads": [], "headers": [], "error": "No data rows found"}

        rows = _sanitize_tabular_rows(rows)
        if not rows:
            return {"status": "ok", "count": 0, "leads": [], "headers": [], "error": "No data rows found"}

        col_map = detect_lead_columns(rows, upload_role=role)
        phone_col = col_map.get("phone")
        name_col = col_map.get("name")
        email_col = col_map.get("email")
        company_col = col_map.get("company")
        mapped_cols = {c for c in (phone_col, name_col, email_col, company_col) if c}

        original_filename = file.filename or "uploaded_leads"

        clean_leads = []
        for r in rows:
            ph = _extract_phone_cell(r, phone_col, _norm_phone_str)
            if not ph:
                continue
            raw_name = str(r.get(name_col, "") if name_col else "").strip()
            if role == "rfqs" and (not raw_name or _is_product_like_column(name_col or "")):
                raw_name = "Unknown"
            entry = {
                "name": raw_name or "Unknown",
                "phone": ph,
                "email": str(r.get(email_col, "") if email_col else "").strip(),
                "company": str(r.get(company_col, "") if company_col else "").strip(),
                "details": "",
                "upload_source": original_filename,
            }
            for col, val in r.items():
                if col in mapped_cols:
                    continue
                sv = str(val or "").strip()
                if sv:
                    entry[col] = sv
            clean_leads.append(entry)

        count = add_leads_bulk(role, clean_leads)
        logger.info(f"Upload complete for role '{role}': {count} leads saved to database.")
        recent: list = []
        if count:
            n = min(150, max(int(count), 1))
            recent_raw = await lead_storage.get_leads(role, limit=n)
            recent = [enrich_lead_for_console(dict(x)) for x in recent_raw]
        return {
            "status": "ok",
            "count": count,
            "recent": recent,
            "leads": clean_leads[:50],
            "headers": headers,
            "column_map": col_map,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Lead upload failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload leads")


@router.post("/toggle")
async def toggle_campaign(request: Request):
    """Legacy alternating start/stop. Prefer ``POST /api/campaign/start`` and ``/stop``.

    Mirrors intent flags used for auto-resume after restarts (see ``START``).
    """
    try:
        role = _campaign_role(request)

        if _CAMPAIGN_TASKS.get(role) and not _CAMPAIGN_TASKS[role].done():
            await lead_storage.set_campaign_want_running(role, False)
            await lead_storage.set_campaign_globally_paused(True)
            from core.state import _MANUALLY_STOPPED_ROLES
            _MANUALLY_STOPPED_ROLES.add(role)
            _CAMPAIGN_TASKS[role].cancel()
            _CAMPAIGN_TASKS[role] = None
            await release_orphaned_dialing_leads(role)
            logger.info(f"Stopped campaign for {role} (toggle).")
            return {"status": "stopped", "active": False, "campaign_paused": True}
        else:
            from core.worker import _schedule_preflight

            await lead_storage.set_campaign_globally_paused(False)
            err = await _schedule_preflight(role)
            if err:
                raise HTTPException(status_code=400, detail=err)
            from core.state import _MANUALLY_STOPPED_ROLES
            _MANUALLY_STOPPED_ROLES.discard(role)
            await lead_storage.set_campaign_want_running(role, True)
            _CAMPAIGN_TASKS[role] = asyncio.create_task(_campaign_worker_role(role))
            logger.info(f"Started campaign for {role} (toggle).")
            return {"status": "started", "active": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Toggle campaign failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to toggle campaign")


@router.post("/start")
async def start_campaign(request: Request):
    """Start the dialer for this role (**idempotent** — never stops an already-running worker).

    Historically `/start` mistakenly called toggle logic that would **stop** the campaign when a
    task was already alive, which confused the dashboard and halted runs on double-clicks/resync.
    """
    try:
        role = _campaign_role(request)
        run = _CAMPAIGN_TASKS.get(role)
        if run and not run.done():
            c = await lead_storage.get_lead_counts(role)
            return {
                "status": "already_running",
                "active": True,
                "pending": c.get("pending", 0),
                "dialing": c.get("dialing", 0),
            }
        from core.worker import _schedule_preflight

        await lead_storage.set_campaign_globally_paused(False)
        err = await _schedule_preflight(role)
        if err:
            raise HTTPException(status_code=400, detail=err)
        from core.state import _MANUALLY_STOPPED_ROLES
        _MANUALLY_STOPPED_ROLES.discard(role)
        await lead_storage.set_campaign_want_running(role, True)
        _CAMPAIGN_TASKS[role] = asyncio.create_task(_campaign_worker_role(role))
        try:
            from core.notifications import push_notification

            push_notification(role, "Campaign started", kind="campaign")
        except Exception as ne:
            logger.warning("Failed to push campaign-start notification: {}", ne)
        c = await lead_storage.get_lead_counts(role)
        return {
            "status": "started",
            "active": True,
            "pending": c.get("pending", 0),
            "dialing": c.get("dialing", 0),
            "campaign_paused": False,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Start campaign failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to start campaign")


@router.post("/stop")
async def stop_campaign(request: Request):
    try:
        role = _campaign_role(request)
        await lead_storage.set_campaign_want_running(role, False)
        await lead_storage.set_campaign_globally_paused(True)
        try:
            from core.notifications import push_notification

            push_notification(role, "Campaign stopped", kind="campaign")
        except Exception as ne:
            logger.warning("Failed to push campaign-stop notification: {}", ne)
        from core.state import _MANUALLY_STOPPED_ROLES
        _MANUALLY_STOPPED_ROLES.add(role)
        if _CAMPAIGN_TASKS.get(role):
            _CAMPAIGN_TASKS[role].cancel()
            _CAMPAIGN_TASKS[role] = None
        if role in _REANALYZE_ALL_PROGRESS:
            _REANALYZE_ALL_PROGRESS[role]["running"] = False
        released = await release_orphaned_dialing_leads(role)
        return {"status": "stopped", "active": False, "released_dialing": released, "campaign_paused": True}
    except Exception as e:
        logger.error(f"Stop campaign failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to stop campaign")


@router.post("/stop-all")
async def stop_all_campaigns(request: Request):
    """Stop outbound dialers for every console role and clear orphaned dialing rows."""
    caller = _campaign_role(request)
    if caller != "admin":
        raise HTTPException(status_code=403, detail="Admin role required to stop all campaigns")

    await lead_storage.set_campaign_globally_paused(True)
    from core.state import _MANUALLY_STOPPED_ROLES
    stopped: list[str] = []
    for r in _ROLES:
        _MANUALLY_STOPPED_ROLES.add(r)
        await lead_storage.set_campaign_want_running(r, False)
        task = _CAMPAIGN_TASKS.get(r)
        if task and not task.done():
            task.cancel()
        _CAMPAIGN_TASKS[r] = None
        released = await release_orphaned_dialing_leads(r)
        if r in _REANALYZE_ALL_PROGRESS:
            _REANALYZE_ALL_PROGRESS[r]["running"] = False
        stopped.append(r)
        logger.info("stop-all: role={} released_dialing={}", r, released)

    return {
        "status": "stopped_all",
        "roles": stopped,
        "active_campaigns": 0,
        "campaign_paused": True,
    }


@router.post("/reset")
async def reset_campaign(request: Request):
    try:
        role = _campaign_role(request)
        reset_leads(role)
        counts = await lead_storage.get_lead_counts(role)
        return {"status": "reset", "count": counts.get("total", 0)}
    except Exception as e:
        logger.error(f"Reset campaign failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to reset campaign")


@router.post("/wipe")
async def wipe_campaign(request: Request):
    try:
        role = _campaign_role(request)
        await lead_storage.set_campaign_want_running(role, False)
        if _CAMPAIGN_TASKS.get(role):
            _CAMPAIGN_TASKS[role].cancel()
            _CAMPAIGN_TASKS[role] = None
        wipe_leads(role)
        logger.info(f"Wipe complete for role: {role}")
        return {"status": "wiped"}
    except Exception as e:
        logger.error(f"Wipe campaign failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to wipe campaign")


@router.post("/lead/{lead_id}/status")
async def update_lead_status_route(lead_id: int, request: Request):
    try:
        role = _campaign_role(request)
        data = await request.json()
        new_status = data.get("status", "")
        VALID = {"pending", "completed", "failed", "not_interested", "callback_scheduled"}
        if new_status not in VALID:
            raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {VALID}")
        await update_lead_status(lead_id, new_status)
        logger.info(f"Lead {lead_id} marked as {new_status}")
        return {"status": "ok", "lead_id": lead_id, "new_status": new_status}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update lead status failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to update lead status")


@router.post("/lead/{lead_id}/whatsapp-sent")
async def mark_lead_whatsapp_sent_route(lead_id: int, request: Request):
    try:
        from core.storage import mark_whatsapp_sent
        await mark_whatsapp_sent(lead_id)
        logger.info(f"Lead {lead_id} manually marked as whatsapp_sent")
        return {"status": "ok", "lead_id": lead_id}
    except Exception as e:
        logger.error(f"Mark lead whatsapp sent failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to mark lead whatsapp sent")


class ManualSendBody(BaseModel):
    channel: str = "whatsapp"  # "whatsapp" or "email" or "both"


@router.post("/lead/{lead_id}/send-details")
async def manual_send_lead_details(lead_id: int, body: ManualSendBody, request: Request):
    """Manually trigger WhatsApp / Email send for a lead."""
    role = _campaign_role(request)
    row = await lead_storage.get_lead(role, lead_id)
    if not row:
        raise HTTPException(status_code=404, detail="Lead not found")
    phone = row.get("phone", "")
    email = row.get("email", "")
    name = row.get("name", "") or "there"
    results = []
    channel = body.channel
    try:
        if channel in ("whatsapp", "both") and phone:
            from services.whatsapp_leads import send_whatsapp_project_details
            wa = await send_whatsapp_project_details(phone, lead_name=name)
            if wa.get("sent"):
                from core.storage import mark_whatsapp_sent
                await mark_whatsapp_sent(lead_id)
            results.append(("whatsapp", wa))
        if channel in ("email", "both") and email and "@" in email:
            from services.email_leads import send_email_project_details
            em = await send_email_project_details(email, lead_name=name)
            if em.get("sent"):
                from core.storage import mark_email_sent
                await mark_email_sent(lead_id)
            results.append(("email", em))
    except Exception as e:
        logger.error(f"Manual send failed for lead {lead_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "ok", "lead_id": lead_id, "results": results}


class FreeformSendBody(BaseModel):
    phone: str = ""
    email: str = ""
    name: str = ""
    channel: str = "both"


@router.post("/send-details")
async def freeform_send_details(body: FreeformSendBody, request: Request):
    """Send project details to any phone/email (not tied to a lead)."""
    role = _campaign_role(request)
    results = []
    body_phone = (body.phone or "").strip()
    body_email = (body.email or "").strip()
    body_name = (body.name or "").strip() or "there"
    channel = body.channel or "both"
    try:
        if channel in ("whatsapp", "both") and body_phone:
            from services.whatsapp_leads import send_whatsapp_project_details
            wa = await send_whatsapp_project_details(body_phone, lead_name=body_name)
            results.append(("whatsapp", wa))
        if channel in ("email", "both") and body_email and "@" in body_email:
            from services.email_leads import send_email_project_details
            em = await send_email_project_details(body_email, lead_name=body_name)
            results.append(("email", em))
    except Exception as e:
        logger.error(f"Freeform send failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "ok", "channel": channel, "results": results}


def _norm_phone_digits(phone: object) -> str:
    return "".join(c for c in str(phone or "") if c.isdigit())[-10:]


async def _resolve_lead_session_log_id(role: str, row: dict) -> str:
    log_id = str(row.get("_log_id") or row.get("log_id") or "").strip()
    if log_id:
        return log_id
    phone_raw = row.get("phone")
    if not phone_raw:
        return ""
    
    from core.storage import _get_conn
    conn = _get_conn()
    phone_suffix = "%" + "".join(c for c in phone_raw if c.isdigit())[-10:]
    sibling_row = conn.execute(
        """
        SELECT _log_id FROM leads 
        WHERE role = %s AND phone LIKE %s AND _log_id IS NOT NULL AND _log_id != '' 
        LIMIT 1
        """,
        (role, phone_suffix)
    ).fetchone()
    if sibling_row and sibling_row[0]:
        return str(sibling_row[0]).strip()
    return ""


@router.get("/lead/{lead_id}/transcript")
async def campaign_lead_transcript(
    lead_id: int,
    request: Request,
    log_id: str | None = None,
):
    """Raw JSONL (same folder layout as analyzer / manual calls).
    Uses the lead's current log_id by default, or a specific historical log_id.
    """
    role = _campaign_role(request)
    if log_id:
        _log_id = log_id.strip()
    else:
        row = await lead_storage.get_lead(role, lead_id)
        if not row:
            raise HTTPException(status_code=404, detail="Lead not found")
        _log_id = await _resolve_lead_session_log_id(role, row)
        if not _log_id:
            raise HTTPException(status_code=404, detail="No transcript session for this lead")
    raw = _read_transcript_jsonl(role, _log_id)
    if not (raw or "").strip():
        raise HTTPException(status_code=404, detail="Transcript file missing or empty")
    return Response(content=raw, media_type="text/plain; charset=utf-8")


@router.get("/lead/{lead_id}/recording")
async def campaign_lead_recording(
    lead_id: int,
    request: Request,
    log_id: str | None = None,
):
    """Mixed 16 kHz WAV when CallRecorder captured this session.
    Uses the lead's current log_id by default, or a specific historical log_id.
    """
    role = _campaign_role(request)
    if log_id:
        _log_id = log_id.strip()
    else:
        row = await lead_storage.get_lead(role, lead_id)
        if not row:
            raise HTTPException(status_code=404, detail="Lead not found")
        _log_id = await _resolve_lead_session_log_id(role, row)
        if not _log_id:
            raise HTTPException(status_code=404, detail="No session log for recording lookup")
    rec = resolve_session_recording_path(_log_id)
    if not rec or not rec.is_file():
        raise HTTPException(status_code=404, detail="Recording not found")
    media_type = "audio/mpeg" if rec.name.endswith(".mp3") else "audio/wav"
    return range_file_response(rec, request, media_type)


@router.get("/lead/{lead_id}/attempts")
async def campaign_lead_attempts(lead_id: int, request: Request):
    """Return historical call attempts (including retakes) for a lead."""
    role = _campaign_role(request)
    from core.storage import get_call_attempts
    attempts = await get_call_attempts(lead_id)
    # Resolve recording/transcript URLs for each attempt that has a log_id
    role_key = normalize_console_role(role)
    for a in attempts:
        log_id = (a.get("log_id") or "").strip()
        if log_id:
            a["log_id"] = log_id
            try:
                rp = resolve_session_recording_path(log_id)
                a["recording_available"] = bool(rp and rp.is_file())
                if a["recording_available"]:
                    a["recording_url"] = f"/api/campaign/lead/{lead_id}/recording?role={role_key}&log_id={log_id}"
            except Exception:
                a["recording_available"] = False
            a["transcript_url"] = f"/api/campaign/lead/{lead_id}/transcript?role={role_key}&log_id={log_id}"
        else:
            a["recording_available"] = False
    return {"role": role, "lead_id": lead_id, "attempts": attempts}


@router.post("/lead/{lead_id}/analyze")
async def retrigger_analysis(
    lead_id: int,
    request: Request,
):
    try:
        role = _campaign_role(request)
        lead_row = await lead_storage.get_lead(role, lead_id)
        if not lead_row:
            raise HTTPException(status_code=404, detail="Lead not found")
        log_id = lead_row.get("_log_id")
        if not log_id:
            raise HTTPException(status_code=400, detail="No log ID found for this lead")
        await _analyze_and_update_lead(role, lead_id, log_id)
        refreshed = await lead_storage.get_lead(role, lead_id)
        if not refreshed:
            raise HTTPException(status_code=500, detail="Lead missing after analyze")
        return {"status": "ok", "lead": slim_lead_for_api(dict(refreshed), role=role)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Retrigger analysis failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to analyze call")


# ── Re-analyze All ────────────────────────────────────────────────────

_REANALYZE_ALL_PROGRESS: dict[str, dict] = {}

@router.post("/reanalyze-all")
async def campaign_reanalyze_all(request: Request):
    """Re-analyze every completed lead that has a log_id and recording."""
    role = _campaign_role(request)
    if role in _REANALYZE_ALL_PROGRESS and _REANALYZE_ALL_PROGRESS[role].get("running"):
        raise HTTPException(status_code=409, detail="Re-analyze already running for this role")

    leads = await lead_storage.get_leads(role, limit=20000)
    eligible = [l for l in leads if l.get("status") in ("completed", "failed", "not_interested") and l.get("_log_id")]
    if not eligible:
        raise HTTPException(status_code=400, detail="No eligible leads found (need completed/failed status + log_id)")

    total = len(eligible)
    _REANALYZE_ALL_PROGRESS[role] = {
        "running": True,
        "total": total,
        "completed": 0,
        "current": "",
        "errors": [],
    }

    async def _run():
        try:
            for idx, lead in enumerate(eligible):
                if not _REANALYZE_ALL_PROGRESS.get(role, {}).get("running"):
                    break
                lid = lead["id"]
                log_id = lead.get("_log_id", "")
                name = lead.get("name", f"#{lid}")
                _REANALYZE_ALL_PROGRESS[role]["current"] = f"{name} ({lead.get('phone','')})"
                try:
                    await _analyze_and_update_lead(role, lid, log_id)
                except Exception as e:
                    _REANALYZE_ALL_PROGRESS[role]["errors"].append(f"#{lid} {name}: {e}")
                _REANALYZE_ALL_PROGRESS[role]["completed"] = idx + 1
        finally:
            if role in _REANALYZE_ALL_PROGRESS:
                _REANALYZE_ALL_PROGRESS[role]["running"] = False

    asyncio.create_task(_run())
    return {"status": "started", "total": total}


@router.get("/reanalyze-all/progress")
async def campaign_reanalyze_all_progress(request: Request):
    role = _campaign_role(request)
    state = _REANALYZE_ALL_PROGRESS.get(role)
    if not state:
        return {"running": False, "total": 0, "completed": 0, "current": "", "errors": []}
    return {
        "running": state.get("running", False),
        "total": state.get("total", 0),
        "completed": state.get("completed", 0),
        "current": state.get("current", ""),
        "errors": state.get("errors", []),
    }


@router.post("/reanalyze-all/cancel")
async def campaign_reanalyze_all_cancel(request: Request):
    role = _campaign_role(request)
    if role in _REANALYZE_ALL_PROGRESS:
        _REANALYZE_ALL_PROGRESS[role]["running"] = False
    return {"status": "cancelled"}


@router.get("/manifest")
async def campaign_manifest_preview(
    request: Request,
    limit: int = Query(20000, ge=1, le=20_000, description="Max rows for dashboard Lead Manifest + call list"),
):
    """Lightweight full-row fetch for UI tables — avoids oversized ``/state`` payloads."""
    role = _campaign_role(request)
    rows = await lead_storage.get_leads(
        role, limit=min(int(limit), 20_000), order="activity"
    )
    enriched = [slim_lead_for_api(dict(r), role=role) for r in rows]
    return {"role": role, "returned": len(enriched), "leads": enriched}



@router.get("/state")
async def get_campaign_status(
    request: Request,
    chart_sample_limit: int = Query(250, ge=50, le=5000, description="Sample size for donut/callback charts embedded in state"),
    _skip_cache: bool = Query(False, alias="_skip_cache"),
):
    try:
        role = _campaign_role(request)

        # Serve from KV cache unless explicitly skipped
        if not _skip_cache:
            cached = kv_cache.state_get(role)
            if cached is not None:
                return cached

        # Serve from pre-computed materialized dashboard state (<5ms, no DB rebuild)
        from core.dashboard_state import build_api_payload_sync
        payload = build_api_payload_sync(role)
        if payload is None:
            # Fallback: build from scratch (first load / error)
            counts = await lead_storage.get_lead_counts(role)
            sample_cap = min(int(chart_sample_limit), 5000)
            chart_rows = await lead_storage.get_leads(role, limit=sample_cap)
            dash = build_campaign_state_dashboard_fields(role, chart_rows)
            chart_leads = [
                slim_lead_for_api(l, role=role) for l in dash.pop("leads_enriched", [])
            ]
            total_in_db = int(counts.get("total") or 0)
            dash["called_count"] = await lead_storage.count_leads_with_outbound_attempt(role)
            from core.storage import (
                is_strict_gap_core_role,
                STRICT_CORE_GAP_MIN_SEC,
                STRICT_CORE_GAP_MAX_SEC,
            )
            gap_strict = is_strict_gap_core_role(role)
            scheduled_cb_today = await lead_storage.count_scheduled_callbacks_due_today(role)
            completed_cb_today = await lead_storage.count_callbacks_completed_today(role)
            payload = {
                "active": bool(_CAMPAIGN_TASKS.get(role) and not _CAMPAIGN_TASKS[role].done()),
                "inter_call_gap_sec": inter_call_gap_seconds_for_role(role),
                "inter_call_gap_strict": gap_strict,
                "inter_call_gap_min_sec": int(STRICT_CORE_GAP_MIN_SEC) if gap_strict else None,
                "inter_call_gap_max_sec": int(STRICT_CORE_GAP_MAX_SEC) if gap_strict else None,
                **counts,
                **dash,
                "chart_sample": chart_leads,
                "leads": chart_leads,
                "manifest_fetch_hint": {"endpoint": "/api/campaign/manifest", "suggested_limit": min(2500, max(500, sample_cap))},
                "lead_list_truncated": total_in_db > len(chart_leads),
                "leads_returned": len(chart_leads),
                "active_calls": total_active_vobiz_calls(),
                "inbound_queue_count": get_inbound_queue_length(role),
                "campaign_hours": get_campaign_hours_status(role),
                "campaign_paused": await lead_storage.is_campaign_globally_paused(),
                "scheduled_callbacks_today": scheduled_cb_today,
                "completed_callbacks_today": completed_cb_today,
                "total_callbacks_today": scheduled_cb_today + completed_cb_today,
            }

        # Override the campaign_paused from the materialized state with the live async value
        payload["campaign_paused"] = await lead_storage.is_campaign_globally_paused()

        # Cache the payload for subsequent polls
        kv_cache.state_set(role, payload)

        return payload
    except Exception as e:
        logger.error(f"Get campaign status failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to get campaign status")


class InterCallGapBody(BaseModel):
    """Seconds to wait after each outbound leg before dialing the next lead (same role)."""
    seconds: float = Field(..., ge=0, le=1200, description="0 = back-to-back; max 20 minutes")


@router.post("/inter-call-gap")
async def set_inter_call_gap(body: InterCallGapBody, request: Request):
    """Persist pause between consecutive campaign calls for this role (``role_state.delay_sec``)."""
    try:
        from core.storage import (
            is_strict_gap_core_role,
            STRICT_CORE_GAP_SEC,
            STRICT_CORE_GAP_MIN_SEC,
            STRICT_CORE_GAP_MAX_SEC,
        )

        role = _campaign_role(request)
        if is_strict_gap_core_role(role):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Sellers, Buyers, RFQs, and Dariaan use a fixed {int(STRICT_CORE_GAP_SEC)}s pause "
                    f"({int(STRICT_CORE_GAP_MIN_SEC)}–{int(STRICT_CORE_GAP_MAX_SEC)}s carrier safety); "
                    "it cannot be changed."
                ),
            )
        sec = float(body.seconds)
        save_role_state(role, delay_sec=sec)
        logger.info(f"inter_call_gap_sec={sec} saved for role={role}")
        return {"status": "ok", "inter_call_gap_sec": sec}
    except Exception as e:
        logger.error(f"Set inter-call gap failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to save inter-call gap")


@router.get("/phone-numbers")
async def get_phone_numbers(request: Request):
    """Get configured phone numbers for the current role."""
    try:
        role = _campaign_role(request)
        state = get_state(role)
        v_cfg = state.get("vobiz", {}) or {}
        numbers = get_all_outbound_numbers(role, v_cfg)
        
        # Get round-robin state
        from core.worker import _PHONE_ROUND_ROBIN_STATE, _MAX_CALLS_PER_HOUR
        rr_state = _PHONE_ROUND_ROBIN_STATE.get(role, {})
        
        return {
            "role": role,
            "phone_numbers": numbers,
            "current_index": rr_state.get("phone_index", 0),
            "total_calls_this_hour": rr_state.get("total_calls_this_hour", 0),
            "max_calls_per_hour": _MAX_CALLS_PER_HOUR,
        }
    except Exception as e:
        logger.error(f"Get phone numbers failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to get phone numbers")


@router.get("/download")
async def download_leads(request: Request, filter: str = "all"):
    try:
        role = _campaign_role(request)
        leads = export_leads_csv(role, filter)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "S.NO",
            "date and time",
            "lead ID",
            "name",
            "phone number",
            "email id",
            "conversation timing",
            "answered and not answered",
            "interested and not interested",
            "rating",
            "summary",
            "whatsapp sent (yes or no)",
            "email sent (yes or no)",
            "call direction",
            "conversation transcript"
        ])
        for idx, l in enumerate(leads):
            s_no = idx + 1
            called_at = ""
            if l.get("start_time") and l["start_time"] > 0:
                try:
                    called_at = datetime.datetime.fromtimestamp(l["start_time"]).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    called_at = str(l.get("created_at") or "")
            else:
                called_at = str(l.get("created_at") or "")

            lead_id = l.get("id", "")
            name = l.get("name", "")
            phone_raw = l.get("phone", "")
            # Force Excel/Spreadsheets to treat phone as text to prevent scientific notation
            phone = f"\t{phone_raw}" if phone_raw else ""
            email = l.get("email", "")

            analysis_raw = l.get("analysis")
            analysis = {}
            if analysis_raw:
                try:
                    import json
                    analysis = json.loads(analysis_raw) if isinstance(analysis_raw, str) else analysis_raw
                except Exception:
                    analysis = {}

            duration_val = analysis.get("duration")
            if duration_val is not None:
                try:
                    conversation_timing = f"{round(float(duration_val))} seconds"
                except Exception:
                    conversation_timing = "0 seconds"
            else:
                conversation_timing = "0 seconds"

            status_lc = str(l.get("status") or "").lower().strip()
            if status_lc in ("completed", "site_visit", "callback_scheduled"):
                answered_not_answered = "Answered"
            else:
                answered_not_answered = "Not Answered"

            disp_lc = str(analysis.get("disposition") or "").lower().strip()
            site_visit_agreed = bool(analysis.get("site_visit_agreed"))
            if disp_lc == "interested" or site_visit_agreed or status_lc == "site_visit":
                interested_not_interested = "Interested"
            else:
                interested_not_interested = "Not Interested"

            # Retrieve rating from analysis (hide 0 values)
            rating_val = analysis.get("rating")
            rating = ""
            if rating_val is not None:
                try:
                    val = float(rating_val)
                    if val > 0:
                        rating = str(int(val)) if val.is_integer() else str(val)
                except Exception:
                    pass

            summary = analysis.get("summary") or l.get("error") or "Call did not connect."
            whatsapp_sent = "Yes" if l.get("whatsapp_sent") else "No"
            email_sent = "Yes" if l.get("email_sent") else "No"

            # Call direction resolution
            call_id = l.get("_call_id") or ""
            is_incoming = str(call_id).startswith("incoming_") or str(name).lower().startswith("inbound")
            call_direction = "Incoming" if is_incoming else "Outbound"

            # Transcript extraction
            log_id = l.get("_log_id")
            conversation_transcript = ""
            if log_id:
                try:
                    from core.worker import _read_transcript_jsonl
                    raw_tr = _read_transcript_jsonl(role, log_id)
                    if raw_tr:
                        lines_out = []
                        for line in raw_tr.splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                import json
                                obj = json.loads(line)
                                msg_role = obj.get("role") or obj.get("type", "")
                                msg_content = obj.get("content") or obj.get("text") or obj.get("message", "")
                                if msg_role in ("user", "assistant") and msg_content:
                                    lines_out.append(f"{msg_role.capitalize()}: {msg_content.strip()}")
                            except Exception:
                                lines_out.append(line)
                        conversation_transcript = "\n".join(lines_out)
                except Exception as tr_err:
                    logger.warning("Failed to read transcript for CSV download: {}", tr_err)

            writer.writerow([
                s_no,
                called_at,
                lead_id,
                name,
                phone,
                email,
                conversation_timing,
                answered_not_answered,
                interested_not_interested,
                rating,
                summary,
                whatsapp_sent,
                email_sent,
                call_direction,
                conversation_transcript
            ])

        csv_bytes = output.getvalue().encode("utf-8-sig")
        filename = f"leads_{role}_{filter}.csv"
        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        logger.error(f"Download leads failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to download leads")


@router.get("/kpi-summary")
async def campaign_kpi_summary(
    request: Request,
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
):
    """Return per-category KPI summary (used by the dashboard overview table)."""
    _jwt_payload_from_request(request)
    role = _campaign_role(request)
    result = get_report_kpi_summary(from_date=from_date, to_date=to_date, role=role)
    return result



