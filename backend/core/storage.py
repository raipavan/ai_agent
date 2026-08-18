"""PostgreSQL-based persistent storage — replaces fragile JSON files."""

from __future__ import annotations

import json
import threading
import asyncio
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Any
from zoneinfo import ZoneInfo
from loguru import logger

import psycopg2
import psycopg2.extensions
from psycopg2.extras import RealDictCursor, RealDictRow

# SQLite stored timestamps as "YYYY-MM-DD HH:MM:SS" TEXT; core.campaign_payload
# and other callers parse that exact format, so keep producing it verbatim.
# (Postgres template note: "MI" = minutes, "HH24" = 00-23 hour.)
_NOW_SQL = "to_char(now(), 'YYYY-MM-DD HH24:MI:SS')"

_DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/vernika"


def _resolve_dsn() -> str:
    try:
        from config import settings

        dsn = getattr(settings, "database_url", "") or ""
    except Exception:
        dsn = ""
    return (dsn or "").strip() or _DEFAULT_DSN


class _CompatRealDictRow(RealDictRow):
    """``RealDictRow`` variant that also supports positional access (``row[0]``),
    matching sqlite3.Row behaviour used by a few external call sites."""

    __slots__ = ()

    def __getitem__(self, key):
        if isinstance(key, int):
            keys = list(dict.keys(self))
            if 0 <= key < len(keys):
                return dict.__getitem__(self, keys[key])
            raise IndexError(key)
        return dict.__getitem__(self, key)


class _CompatRealDictCursor(RealDictCursor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.row_factory = _CompatRealDictRow


class _Conn:
    """Thin shim exposing the sqlite3-style ``conn.execute(sql, params)`` API
    over a psycopg2 connection so existing call sites keep working unchanged."""

    def __init__(self, pg_conn: psycopg2.extensions.connection):
        self._conn = pg_conn

    @property
    def in_transaction(self) -> bool:
        try:
            return (
                self._conn.get_transaction_status()
                != psycopg2.extensions.TRANSACTION_STATUS_IDLE
            )
        except Exception:
            return False

    def execute(self, sql: str, params=None):
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params if params is not None else ())
        except psycopg2.ProgrammingError:
            # Compatibility fallback: a few external call sites (e.g. campaign
            # routes) still use sqlite3 ``?`` placeholders. Only rewrite when
            # the server rejected the statement, so literals never get mangled.
            if "?" not in sql:
                raise
            try:
                self._conn.rollback()
            except Exception:
                pass
            cur = self._conn.cursor()
            cur.execute(sql.replace("?", "%s"), params if params is not None else ())
        return cur

    def cursor(self):
        return self._conn.cursor()

    def commit(self) -> None:
        # No-op under autocommit; kept so existing call sites stay valid.
        if not self._conn.autocommit:
            self._conn.commit()

    def rollback(self) -> None:
        # No-op under autocommit; kept so existing call sites stay valid.
        if not self._conn.autocommit:
            self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._conn.__exit__(exc_type, exc_val, exc_tb)


def _executescript(conn: _Conn, script: str) -> None:
    """Run a multi-statement SQL string one statement at a time (psycopg2 has
    no ``executescript``)."""
    for stmt in script.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)


# Lazy import to avoid circular deps at module level
_INVALIDATED = False
def _invalidate_state_cache():
    global _INVALIDATED
    try:
        from core.kv_cache import invalidate_all as _do_invalidate
        _do_invalidate()
    except ImportError:
        pass
    conn = None
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            ("last_cache_invalidation_time", str(time.time())),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to save cache invalidation timestamp: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception as re:
                logger.error(f"Failed rollback in invalidate_state_cache: {re}")

_DB_PATH: Optional[str] = None
_LOCAL = threading.local()

# ── In-memory paused-sources store (shared across all threads in process) ──
# Avoids cross-thread snapshot issues where a fresh thread-local
# connection might not immediately see a just-committed write from another thread.
_PAUSED_SOURCES: dict[str, list[str]] = {}  # role -> list[filename]
_PAUSED_SOURCES_LOCK = threading.Lock()

# Inter-call gap (seconds) between outbound dials.
_GAP_LEGACY_DEFAULT_SEC = 120.0
_GAP_CORE_ROLE_NAMES = frozenset({"sales_1", "sales_2"})
STRICT_CORE_GAP_MIN_SEC = 5.0
STRICT_CORE_GAP_MAX_SEC = 30.0
STRICT_CORE_GAP_SEC = 5.0
_GAP_CORE_PRODUCT_ROLES_SEC = STRICT_CORE_GAP_SEC


def is_strict_gap_core_role(role: Optional[str]) -> bool:
    return (role or "sales_1").strip().lower() in _GAP_CORE_ROLE_NAMES


def default_inter_call_gap_sec(role: Optional[str]) -> float:
    r = (role or "sales_1").strip().lower()
    if r in _GAP_CORE_ROLE_NAMES:
        return float(_GAP_CORE_PRODUCT_ROLES_SEC)
    return float(_GAP_LEGACY_DEFAULT_SEC)


def init_db(data_dir: Optional[Path | str] = None) -> str:
    """Initialize the PostgreSQL database. Call once at startup."""
    global _DB_PATH
    dsn = _resolve_dsn()
    _DB_PATH = dsn
    if isinstance(data_dir, str):
        data_dir = Path(data_dir)
    base = data_dir or Path(__file__).resolve().parent.parent / "data"
    base.mkdir(parents=True, exist_ok=True)

    conn = None
    try:
        conn = _get_conn()
    except Exception as e:
        logger.error(
            "Database init skipped — PostgreSQL unreachable at {} (schema setup "
            "will fail again on first real use): {}",
            dsn,
            e,
        )

    if conn is not None:
        try:
            _executescript(conn, f"""
                CREATE TABLE IF NOT EXISTS role_state (
                    role TEXT PRIMARY KEY,
                    prompt TEXT DEFAULT '',
                    rag TEXT DEFAULT '',
                    delay_sec DOUBLE PRECISION DEFAULT 5.0,
                    vobiz_config TEXT DEFAULT '{{}}',
                    updated_at TEXT DEFAULT {_NOW_SQL},
                    greeting_text TEXT DEFAULT ''
                );
            """)
            # Migration: add greeting_text if missing
            try:
                conn.execute("ALTER TABLE role_state ADD COLUMN IF NOT EXISTS greeting_text TEXT DEFAULT ''")
            except psycopg2.Error:
                pass  # Already exists

            # Migration: prompt section store (Prompt Management page).
            try:
                conn.execute("ALTER TABLE role_state ADD COLUMN IF NOT EXISTS prompt_parts TEXT DEFAULT '{}'")
            except psycopg2.Error:
                pass  # Already exists

            # Per-role campaign Cases. The operator defines one or more named "Cases"
            # (e.g. "April Steel Sheets Push", "Diwali Discount Drive") and **activates
            # exactly one** per role. The bridge appends the active case description
            # to the system prompt so the AI runs today's campaign without editing the
            # base persona prompt.
            _executescript(conn, f"""
                CREATE TABLE IF NOT EXISTS cases (
                    id SERIAL PRIMARY KEY,
                    role TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT DEFAULT {_NOW_SQL},
                    updated_at TEXT DEFAULT {_NOW_SQL}
                );
                CREATE INDEX IF NOT EXISTS idx_cases_role ON cases(role);
                CREATE INDEX IF NOT EXISTS idx_cases_active ON cases(role, active);
            """)
            conn.commit()

            # Per-role campaign schedules. The operator uploads leads, then schedules
            # the campaign to start automatically at a future date/time. A small
            # background loop in ``core.worker`` polls this table every 30 s and, when
            # ``run_at <= now`` and ``status='scheduled'``, kicks off the same worker
            # the Start Campaign button does. ``run_at`` is stored as epoch seconds
            # (UTC) so timezone math is trivial both server- and client-side.
            _executescript(conn, f"""
                CREATE TABLE IF NOT EXISTS schedules (
                    id SERIAL PRIMARY KEY,
                    role TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    run_at DOUBLE PRECISION NOT NULL,
                    status TEXT NOT NULL DEFAULT 'scheduled',
                    created_at TEXT DEFAULT {_NOW_SQL},
                    updated_at TEXT DEFAULT {_NOW_SQL},
                    started_at DOUBLE PRECISION,
                    error TEXT,
                    stop_at DOUBLE PRECISION
                );
                CREATE INDEX IF NOT EXISTS idx_schedules_role ON schedules(role);
                CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(status, run_at);
            """)
            conn.commit()
            # Migration: installs created before stop_at existed need the column added
            # *before* the index that references it can be created. Split the work so
            # CREATE INDEX never runs against a missing column.
            try:
                conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS stop_at DOUBLE PRECISION")
            except psycopg2.Error:
                pass  # Already exists
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_schedules_stop ON schedules(status, stop_at)"
            )
            conn.commit()

            _executescript(conn, f"""
                CREATE TABLE IF NOT EXISTS manual_calls (
                    id SERIAL PRIMARY KEY,
                    role TEXT NOT NULL,
                    camp_id TEXT NOT NULL UNIQUE,
                    to_phone TEXT NOT NULL DEFAULT '',
                    callee_name TEXT NOT NULL DEFAULT '',
                    log_id TEXT,
                    status TEXT NOT NULL DEFAULT 'dialing',
                    started_at TEXT DEFAULT {_NOW_SQL},
                    ended_at TEXT,
                    updated_at TEXT DEFAULT {_NOW_SQL},
                    duration_sec DOUBLE PRECISION,
                    disposition TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    next_steps TEXT DEFAULT '',
                    emotion_label TEXT DEFAULT '',
                    emotion_rationale TEXT DEFAULT '',
                    emotion_confidence DOUBLE PRECISION,
                    analysis_json TEXT DEFAULT '{{}}',
                    error TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_manual_calls_role_started
                    ON manual_calls(role, id DESC);
            """)
            conn.commit()

            _executescript(conn, f"""
                CREATE TABLE IF NOT EXISTS incoming_calls (
                    id SERIAL PRIMARY KEY,
                    role TEXT NOT NULL,
                    camp_id TEXT NOT NULL UNIQUE,
                    from_phone TEXT NOT NULL DEFAULT '',
                    caller_name TEXT NOT NULL DEFAULT '',
                    log_id TEXT,
                    status TEXT NOT NULL DEFAULT 'ringing',
                    started_at TEXT DEFAULT {_NOW_SQL},
                    ended_at TEXT,
                    updated_at TEXT DEFAULT {_NOW_SQL},
                    duration_sec DOUBLE PRECISION,
                    disposition TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    next_steps TEXT DEFAULT '',
                    emotion_label TEXT DEFAULT '',
                    emotion_rationale TEXT DEFAULT '',
                    emotion_confidence DOUBLE PRECISION,
                    analysis_json TEXT DEFAULT '{{}}',
                    error TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_incoming_calls_role_started
                    ON incoming_calls(role, id DESC);
            """)
            conn.commit()

            _executescript(conn, f"""
                CREATE TABLE IF NOT EXISTS leads (
                    id SERIAL PRIMARY KEY,
                    role TEXT NOT NULL,
                    name TEXT DEFAULT 'Unknown',
                    phone TEXT NOT NULL,
                    email TEXT DEFAULT '',
                    company TEXT DEFAULT '',
                    details TEXT DEFAULT '',
                    extra TEXT DEFAULT '{{}}',
                    status TEXT DEFAULT 'pending',
                    analysis TEXT DEFAULT '{{}}',
                    start_time DOUBLE PRECISION,
                    error TEXT,
                    _log_id TEXT,
                    _call_id TEXT,
                    created_at TEXT DEFAULT {_NOW_SQL},
                    updated_at TEXT DEFAULT {_NOW_SQL}
                );

                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    role TEXT NOT NULL DEFAULT 'factory',
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    voice TEXT DEFAULT 'Puck',
                    knowledge_files TEXT DEFAULT '[]',
                    created_at TEXT DEFAULT {_NOW_SQL},
                    updated_at TEXT DEFAULT {_NOW_SQL}
                );

                CREATE TABLE IF NOT EXISTS agent_leads (
                    id SERIAL PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    lead_id TEXT NOT NULL,
                    name TEXT DEFAULT 'Unknown',
                    phone TEXT NOT NULL,
                    email TEXT DEFAULT '',
                    company TEXT DEFAULT '',
                    created_at TEXT DEFAULT {_NOW_SQL},
                    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_leads_role ON leads(role);
                CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);
                CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(role, status);
                CREATE INDEX IF NOT EXISTS idx_agent_leads_agent ON agent_leads(agent_id);
                CREATE INDEX IF NOT EXISTS idx_leads_role_created ON leads(role, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_leads_role_start_time ON leads(role, start_time DESC);
            """)
            conn.commit()

            _executescript(conn, """
                CREATE INDEX IF NOT EXISTS idx_leads_role_status_created ON leads(role, status, created_at DESC);
            """)
            conn.commit()

            # ``extra``: JSON blob for CSV columns beyond name/phone/email/company.
            # IMPORTANT: ALTER must run *after* ``CREATE TABLE IF NOT EXISTS leads`` so new
            # installs get the column and older DBs (created before ``extra``) are migrated.
            try:
                conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS extra TEXT DEFAULT '{}'")
            except psycopg2.Error:
                pass  # Already exists

            # Migration: whatsapp_sent flag to prevent duplicate WhatsApp sends
            try:
                conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS whatsapp_sent INTEGER DEFAULT 0")
            except psycopg2.Error:
                pass

            # Migration: failed_call_retries to track retry attempts for unanswered calls
            try:
                conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS failed_call_retries INTEGER DEFAULT 0")
            except psycopg2.Error:
                pass

            # Migration: whatsapp_sent_at and whatsapp_reminder_sent for 24h follow-up messages
            try:
                conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS whatsapp_sent_at DOUBLE PRECISION")
            except psycopg2.Error:
                pass
            try:
                conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS whatsapp_reminder_sent INTEGER DEFAULT 0")
            except psycopg2.Error:
                pass

            # Migration: email_sent and email_sent_at for email deduplication
            try:
                conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS email_sent INTEGER DEFAULT 0")
            except psycopg2.Error:
                pass
            try:
                conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS email_sent_at DOUBLE PRECISION")
            except psycopg2.Error:
                pass

            # Migration: first_called_at to keep the anchor of the first outbound campaign attempt
            try:
                conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS first_called_at DOUBLE PRECISION")
                conn.commit()
            except psycopg2.Error:
                pass
            try:
                conn.execute("UPDATE leads SET first_called_at = start_time WHERE first_called_at IS NULL AND start_time IS NOT NULL")
                conn.commit()
            except Exception:
                pass

            # Migration: outbound_phone TEXT to track which phone number made the call
            try:
                conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS outbound_phone TEXT")
                conn.commit()
            except psycopg2.Error:
                pass

            # Migration: add role column to agents
            try:
                conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'factory'")
                conn.commit()
            except psycopg2.Error:
                pass

            # Seed default roles if empty
            for role in (
                "sales_1",
                "sales_2",
            ):
                conn.execute(
                    "INSERT INTO role_state (role) VALUES (%s) ON CONFLICT (role) DO NOTHING",
                    (role,)
                )
            conn.commit()

            _executescript(conn, """
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                );
            """)
            conn.commit()

            # Per-role scheduled callbacks. Agents can schedule individual callbacks
            # at a specific future time. The campaign worker picks these up at the
            # scheduled moment and calls them immediately (bypassing the normal
            # inter-call gap) or queues them if the role is currently on a call.
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS scheduled_callbacks (
                    id SERIAL PRIMARY KEY,
                    role TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    lead_id INTEGER,
                    scheduled_at DOUBLE PRECISION NOT NULL,
                    status TEXT NOT NULL DEFAULT 'scheduled',
                    error TEXT,
                    created_at TEXT DEFAULT {_NOW_SQL},
                    updated_at TEXT DEFAULT {_NOW_SQL}
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sc_role ON scheduled_callbacks(role)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sc_due ON scheduled_callbacks(role, status, scheduled_at)")
            conn.commit()

            # Migration: analysis columns for scheduled_callbacks (outcome tracking on dashboard)
            for _col_sql in (
                "ALTER TABLE scheduled_callbacks ADD COLUMN IF NOT EXISTS disposition TEXT DEFAULT ''",
                "ALTER TABLE scheduled_callbacks ADD COLUMN IF NOT EXISTS summary TEXT DEFAULT ''",
                "ALTER TABLE scheduled_callbacks ADD COLUMN IF NOT EXISTS rating DOUBLE PRECISION",
                "ALTER TABLE scheduled_callbacks ADD COLUMN IF NOT EXISTS next_action TEXT DEFAULT '{}'",
                "ALTER TABLE scheduled_callbacks ADD COLUMN IF NOT EXISTS analysis_json TEXT DEFAULT '{}'",
            ):
                try:
                    conn.execute(_col_sql)
                except psycopg2.Error:
                    pass

            # Virtual meet tracking (no calendar automation — pure tracking/display)
            _executescript(conn, f"""
                CREATE TABLE IF NOT EXISTS virtual_meets (
                    id SERIAL PRIMARY KEY,
                    lead_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    meet_date TEXT NOT NULL,
                    meet_time TEXT NOT NULL,
                    notes TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'scheduled',
                    rescheduled_from_id INTEGER,
                    created_at TEXT DEFAULT {_NOW_SQL},
                    updated_at TEXT DEFAULT {_NOW_SQL},
                    FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_vm_lead ON virtual_meets(lead_id);
                CREATE INDEX IF NOT EXISTS idx_vm_role ON virtual_meets(role);
            """)
            conn.commit()

            # WhatsApp conversation history for AI-based auto-replies
            _executescript(conn, """
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id SERIAL PRIMARY KEY,
                    phone TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversation_messages(phone);
                CREATE INDEX IF NOT EXISTS idx_conv_phone_created ON conversation_messages(phone, created_at);
            """)
            conn.commit()

            # Per-call attempt history — every call (including retakes) logs an entry
            # so the dashboard can show timing, recording, summary, and transcript per attempt.
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS call_attempts (
                    id SERIAL PRIMARY KEY,
                    lead_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL DEFAULT 1,
                    log_id TEXT,
                    status TEXT NOT NULL DEFAULT 'completed',
                    disposition TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    rating DOUBLE PRECISION,
                    duration_sec DOUBLE PRECISION,
                    callback_scheduled_at DOUBLE PRECISION,
                    error TEXT,
                    created_at TEXT DEFAULT {_NOW_SQL}
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ca_lead ON call_attempts(lead_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ca_role ON call_attempts(role)")
            conn.commit()

            # Initialize Vobiz accounts (if configured in .env)
            from config import settings
            role_vobiz_map = {
                "sales_1": {
                    "auth_id": settings.vobiz_sales_1_auth_id or settings.vobiz_auth_id,
                    "auth_token": settings.vobiz_sales_1_auth_token or settings.vobiz_auth_token,
                    "from_number": settings.vobiz_sales_1_phone_1 or settings.vobiz_from_number,
                    "public_url": settings.vobiz_public_base_url,
                    "phone_numbers": [
                        settings.vobiz_sales_1_phone_1 or "",
                        settings.vobiz_sales_1_phone_2 or "",
                    ],
                },
                "sales_2": {
                    "auth_id": settings.vobiz_sales_2_auth_id or settings.vobiz_auth_id,
                    "auth_token": settings.vobiz_sales_2_auth_token or settings.vobiz_auth_token,
                    "from_number": settings.vobiz_sales_2_phone_3 or settings.vobiz_from_number,
                    "public_url": settings.vobiz_public_base_url,
                    "phone_numbers": [
                        settings.vobiz_sales_2_phone_3 or "",
                        settings.vobiz_sales_2_phone_4 or "",
                    ],
                },
            }

            for role, vobiz_creds in role_vobiz_map.items():
                cur = conn.execute("SELECT vobiz_config FROM role_state WHERE role = %s", (role,))
                row = cur.fetchone()
                db_config_str = row["vobiz_config"] if row else "{}"
                try:
                    db_config = json.loads(db_config_str) if db_config_str else {}
                except Exception:
                    db_config = {}
                is_empty_db = not db_config or db_config == {}

                # Check if role-specific Vobiz environment credentials are set explicitly
                has_explicit_env = False
                if role == "sales_1":
                    has_explicit_env = bool(
                        settings.vobiz_sales_1_auth_id and settings.vobiz_sales_1_auth_token
                    )
                elif role == "sales_2":
                    has_explicit_env = bool(
                        settings.vobiz_sales_2_auth_id and settings.vobiz_sales_2_auth_token
                    )

                if is_empty_db or has_explicit_env:
                    if vobiz_creds["auth_id"] and vobiz_creds["auth_token"] and vobiz_creds["from_number"]:
                        vobiz_config = {
                            "auth_id": vobiz_creds["auth_id"],
                            "auth_token": vobiz_creds["auth_token"],
                            "from_number": vobiz_creds["from_number"],
                            "public_url": settings.vobiz_public_base_url,
                        }
                        conn.execute(
                            f"UPDATE role_state SET vobiz_config = %s, updated_at = {_NOW_SQL} WHERE role = %s",
                            (json.dumps(vobiz_config), role)
                        )
                        conn.commit()
                        logger.info(f"✅ Initialized role '{role}' in database")
                else:
                    logger.info(f"ℹ️  Preserving existing database configuration for role '{role}'")

            # ── Startup migration: re-queue historically stuck failed/error leads ──
            # Under the new persistent-retry policy, leads that were previously
            # capped at 2 retries and marked "failed" should get a fresh chance.
            # We reset any lead whose status is "failed" or "error" (but NOT
            # "not_interested" or other resolved statuses) back to "pending" so
            # the dialing loop picks them up again and aims for 600+/day connectivity.
            # Leads deliberately stopped by the 24h/48h no-answer policy are
            # excluded — they must stay failed (operator can reset manually).
            try:
                requeued = conn.execute(
                    f"""
                    UPDATE leads
                    SET status = 'pending',
                        error  = NULL,
                        updated_at = {_NOW_SQL}
                    WHERE status IN ('failed', 'error')
                      AND (error IS NULL OR error NOT ILIKE '%%no answer after 2 retries%%')
                    """
                ).rowcount
                conn.commit()
                if requeued:
                    logger.info(
                        "Startup migration: re-queued {} failed/error leads → pending "
                        "(persistent retry policy)",
                        requeued,
                    )
            except Exception as _mig_err:
                logger.warning("Startup migration (re-queue failed leads) skipped: {}", _mig_err)
        except Exception as e:
            logger.error("init_db: schema setup failed: {}", e)
            try:
                conn.rollback()
            except Exception:
                pass

    # Create role-specific data directories for prompt + RAG files
    for role in ("sales_1", "sales_2"):
        role_dir = base / role
        role_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"✅ Created directory: {role_dir}")

    close_db()
    logger.info(f"Database initialized: {_DB_PATH}")
    return _DB_PATH



def _get_conn() -> _Conn:
    """Thread-local PostgreSQL connection (psycopg2 connections are bound to
    the thread that created them, so every thread gets its own).

    Autocommit is enabled: every statement commits immediately, matching the
    implicit per-statement transactions of the original SQLite backend. This
    prevents idle-in-transaction connections from piling up and blocking DDL
    (schema init / ALTER migrations) with relation locks.
    """
    if not hasattr(_LOCAL, "conn") or _LOCAL.conn is None:
        if _DB_PATH is None:
            raise RuntimeError("Database not initialized. Call init_db() first.")
        raw = psycopg2.connect(
            _DB_PATH,
            connect_timeout=10,
            cursor_factory=_CompatRealDictCursor,
        )
        raw.autocommit = True
        _LOCAL.conn = _Conn(raw)
    else:
        try:
            if _LOCAL.conn.in_transaction:
                _LOCAL.conn.rollback()
        except Exception as e:
            logger.error(f"Failed to rollback orphaned transaction: {e}")
    return _LOCAL.conn


def close_db() -> None:
    if hasattr(_LOCAL, "conn") and _LOCAL.conn:
        _LOCAL.conn.close()
        _LOCAL.conn = None


def _dashboard_tz() -> ZoneInfo:
    try:
        from config import settings

        return ZoneInfo((settings.transcript_callback_tz or "Asia/Kolkata").strip() or "Asia/Kolkata")
    except Exception:
        return ZoneInfo("Asia/Kolkata")


# Operator clicked Start → survives process restart until Stop or graceful empty queue.
_CAMPAIGN_WANT_META_PREFIX = "campaign_want_running_v2"


def campaign_want_running_meta_key(role: str) -> str:
    return f"{_CAMPAIGN_WANT_META_PREFIX}:{(role or 'sellers').strip().lower()}"


async def set_campaign_want_running(role: str, wanted: bool) -> None:
    return await asyncio.to_thread(_set_campaign_want_running_sync, role, wanted)

def _set_campaign_want_running_sync(role: str, wanted: bool) -> None:
    conn = _get_conn()
    k = campaign_want_running_meta_key(role)
    if wanted:
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (k, "1"),
        )
    else:
        conn.execute("DELETE FROM app_meta WHERE key = %s", (k,))
    conn.commit()


async def roles_with_campaign_run_wanted() -> list[str]:
    return await asyncio.to_thread(_roles_with_campaign_run_wanted_sync)

def _roles_with_campaign_run_wanted_sync() -> list[str]:
    conn = _get_conn()
    prefix = f"{_CAMPAIGN_WANT_META_PREFIX}:"
    rows = conn.execute(
        """
        SELECT key FROM app_meta
        WHERE key LIKE %s
          AND trim(value) IN ('1', 'true', 'yes')
        """,
        (prefix + "%",),
    ).fetchall()
    out: list[str] = []
    for r in rows:
        key = str(r["key"] or "")
        if key.startswith(prefix):
            out.append(key[len(prefix):])
    return out


# Operator clicked Stop / stop-all — blocks auto-resume on deploy restart until Start.
_CAMPAIGN_PAUSED_META = "campaign_globally_paused_v1"


async def set_campaign_globally_paused(paused: bool) -> None:
    return await asyncio.to_thread(_set_campaign_globally_paused_sync, paused)


def _set_campaign_globally_paused_sync(paused: bool) -> None:
    conn = _get_conn()
    if paused:
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (_CAMPAIGN_PAUSED_META, "1"),
        )
    else:
        conn.execute("DELETE FROM app_meta WHERE key = %s", (_CAMPAIGN_PAUSED_META,))
    conn.commit()


async def is_campaign_globally_paused() -> bool:
    return await asyncio.to_thread(_is_campaign_globally_paused_sync)


def _is_campaign_globally_paused_sync() -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = %s",
        (_CAMPAIGN_PAUSED_META,),
    ).fetchone()
    return bool(row and str(row["value"] or "").strip().lower() in ("1", "true", "yes"))


# --- Role State ---

async def get_role_state(role: str) -> dict:
    return await asyncio.to_thread(_get_role_state_sync, role)

def _get_role_state_sync(role: str) -> dict:
    role_key = (role or "sellers").strip().lower()
    fallback_delay = default_inter_call_gap_sec(role_key)
    conn = _get_conn()
    row = conn.execute("SELECT * FROM role_state WHERE role = %s", (role_key,)).fetchone()
    if not row:
        return {
            "role": role_key,
            "prompt": "",
            "rag": "",
            "delay_sec": fallback_delay,
            "vobiz": {},
            "prompt_parts": {},
        }
    ds = row["delay_sec"]
    try:
        prompt_parts = json.loads(row["prompt_parts"] or "{}")
    except Exception:
        prompt_parts = {}
    return {
        "role": row["role"],
        "prompt": row["prompt"] or "",
        "rag": row["rag"] or "",
        "delay_sec": float(fallback_delay if ds is None else ds),
        "vobiz": json.loads(row["vobiz_config"] or "{}"),
        "greeting_text": row["greeting_text"] or "",
        "prompt_parts": prompt_parts,
    }


async def save_role_state(role: str, prompt: str = None, rag: str = None, vobiz_config: dict = None, delay_sec: float = None, greeting_text: str = None, prompt_parts: dict = None):
    return await asyncio.to_thread(_save_role_state_sync, role, prompt, rag, vobiz_config, delay_sec, greeting_text, prompt_parts)

def _save_role_state_sync(role: str, prompt: str = None, rag: str = None, vobiz_config: dict = None, delay_sec: float = None, greeting_text: str = None, prompt_parts: dict = None):
    conn = _get_conn()
    role = (role or "sellers").strip().lower()
    # Ensure a row exists — bare UPDATE silently affects 0 rows if the role was never inserted.
    conn.execute("INSERT INTO role_state (role) VALUES (%s) ON CONFLICT (role) DO NOTHING", (role,))
    updates = []
    params = []
    if prompt is not None:
        updates.append("prompt = %s")
        params.append(prompt)
    if rag is not None:
        updates.append("rag = %s")
        params.append(rag)
    if vobiz_config is not None:
        updates.append("vobiz_config = %s")
        params.append(json.dumps(vobiz_config))
    if delay_sec is not None:
        updates.append("delay_sec = %s")
        params.append(delay_sec)
    if greeting_text is not None:
        updates.append("greeting_text = %s")
        params.append(greeting_text)
    if prompt_parts is not None:
        updates.append("prompt_parts = %s")
        params.append(json.dumps(prompt_parts))

    if not updates:
        return

    updates.append(f"updated_at = {_NOW_SQL}")
    params.append(role)
    conn.execute(f"UPDATE role_state SET {', '.join(updates)} WHERE role = %s", params)
    conn.commit()


# --- Leads ---

async def get_lead(role: str, lead_id: int) -> Optional[dict]:
    return await asyncio.to_thread(_get_lead_sync, role, lead_id)

def _get_lead_sync(role: str, lead_id: int) -> Optional[dict]:
    """Single campaign lead row keyed by database ``id`` and ``role``."""
    conn = _get_conn()
    r = (role or "sellers").strip().lower()
    row = conn.execute(
        "SELECT * FROM leads WHERE role = %s AND id = %s",
        (r, int(lead_id)),
    ).fetchone()
    return _row_to_dict(row) if row else None


async def get_leads(
    role: str,
    status: str = None,
    limit: int = 1000,
    *,
    order: str = "created",
    modulo: int = None,
    remainder: int = None,
) -> list[dict]:
    return await asyncio.to_thread(_get_leads_sync, role, status, limit, order, modulo, remainder)

def _get_leads_sync(
    role: str,
    status: str = None,
    limit: int = 1000,
    order: str = "created",
    modulo: int = None,
    remainder: int = None,
) -> list[dict]:
    conn = _get_conn()
    query = "SELECT * FROM leads WHERE role = %s"
    params = [role]
    if status:
        query += " AND status = %s"
        params.append(status)
    if modulo is not None and remainder is not None:
        query += " AND id %% %s = %s"
        params.extend([modulo, remainder])
    if (order or "created").strip().lower() == "activity":
        query += """
         ORDER BY
             CASE WHEN start_time IS NOT NULL AND start_time > 0
                  THEN start_time ELSE 0.0 END DESC,
             updated_at DESC,
             created_at DESC
         LIMIT %s
        """
    else:
        query += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(r) for r in rows]


async def count_leads_with_outbound_attempt(role: str) -> int:
    return await asyncio.to_thread(_count_leads_with_outbound_attempt_sync, role)

def _count_leads_with_outbound_attempt_sync(role: str) -> int:
    """How many rows have evidence of at least one dial / bridge session started.

    Mirrors the dashboard ``isCalled`` heuristic without loading every row."""
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM leads WHERE role = %s
          AND (
                (COALESCE(trim(_log_id), '') != '')
             OR (start_time IS NOT NULL AND start_time > 0)
             OR (status NOT IN ('pending', 'dialing'))
          )
        """,
        ((role or "sellers").strip().lower(),),
    ).fetchone()
    return int(row["c"]) if row else 0


async def get_leads_with_outbound_activity(role: str, limit: int = 32000) -> list[dict]:
    return await asyncio.to_thread(_get_leads_with_outbound_activity_sync, role, limit)

def _get_leads_with_outbound_activity_sync(role: str, limit: int = 32000) -> list[dict]:
    """All campaign leads that have been bridged/outbound-dialed (log id or ``start_time``).

    Engagement timeline aggregates use this rather than the small ``chart_sample`` slice so
    activity on older CSV rows still appears alongside ``called_count``.
    """

    role = (role or "sellers").strip().lower()
    lim = max(1, min(int(limit), 50000))
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT * FROM leads WHERE role = %s
          AND (
                (COALESCE(TRIM(COALESCE(_log_id, '')), '') != '')
             OR (start_time IS NOT NULL AND start_time > 0)
          )
        ORDER BY
             CASE WHEN start_time IS NOT NULL AND start_time > 0
                  THEN start_time ELSE 0.0 END DESC,
             updated_at DESC
        LIMIT %s
        """,
        (role, lim),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


async def add_lead(role: str, name: str, phone: str, email: str = "", company: str = "", details: str = "") -> int:
    return await asyncio.to_thread(_add_lead_sync, role, name, phone, email, company, details)

def _add_lead_sync(role: str, name: str, phone: str, email: str = "", company: str = "", details: str = "") -> int:
    from core.dnc import is_phone_blocked
    if is_phone_blocked(phone):
        logger.warning(f"Blocked lead add: {phone} is in DNC list")
        return -1
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO leads (role, name, phone, email, company, details) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (role, name, phone, email, company, details)
    )
    conn.commit()
    _invalidate_state_cache()
    return int(cur.fetchone()["id"])


async def bulk_add_leads(role: str, leads: list[dict]) -> int:
    return await asyncio.to_thread(_bulk_add_leads_sync, role, leads)

def _bulk_add_leads_sync(role: str, leads: list[dict]) -> int:
    """Insert leads, persisting any **extra** caller fields (anything beyond
    name/phone/email/company/details/status) into the ``extra`` JSON column so
    the AI can reference them on the call.

    Deduplicates by (role, phone) — any lead whose phone already exists in
    ``pending`` status for this role is skipped.
    """
    conn = _get_conn()
    count = 0
    skipped = 0
    # Build set of existing pending phones for dedup
    existing = set(
        row["phone"] for row in conn.execute(
            "SELECT DISTINCT phone FROM leads WHERE role = %s AND status = 'pending'",
            (role,),
        ).fetchall()
    )
    # These keys map to dedicated columns; everything else goes into ``extra``
    # so we never silently drop info the operator uploaded.
    _RESERVED = {
        "name", "phone", "email", "company", "details",
        "status", "role", "id", "extra",
    }
    for lead in leads:
        phone = lead.get("phone", "").strip()
        if not phone:
            continue
        if phone in existing:
            skipped += 1
            continue
        from core.dnc import is_phone_blocked
        if is_phone_blocked(phone):
            logger.warning(f"Skipping bulk add for DNC blocked number: {phone}")
            continue
        # Pull any extra fields. Caller may pre-populate ``extra`` as a dict;
        # otherwise we sweep any keys that aren't reserved.
        raw_extra = lead.get("extra")
        if isinstance(raw_extra, dict):
            extras_dict = {k: v for k, v in raw_extra.items() if v not in (None, "")}
        else:
            extras_dict = {
                k: v for k, v in lead.items()
                if k not in _RESERVED and v not in (None, "")
            }
        # Stringify everything for safe serialization across CSV/Excel cells.
        extras_dict = {str(k): str(v) for k, v in extras_dict.items() if str(v).strip()}
        extra_json = json.dumps(extras_dict, ensure_ascii=False) if extras_dict else "{}"
        conn.execute(
            "INSERT INTO leads (role, name, phone, email, company, details, extra, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                role,
                lead.get("name", "Unknown"),
                phone,
                lead.get("email", ""),
                lead.get("company", ""),
                lead.get("details", ""),
                extra_json,
                "pending",
            )
        )
        count += 1
    conn.commit()
    _invalidate_state_cache()
    if skipped:
        logger.info(f"Bulk add dedup: skipped {skipped} existing pending leads for role={role}")
    return count


async def update_lead_status(lead_id: int, status: str, error: str = None, analysis: dict = None):
    return await asyncio.to_thread(_update_lead_status_sync, lead_id, status, error, analysis)

def _update_lead_status_sync(lead_id: int, status: str, error: str = None, analysis: dict = None):
    conn = _get_conn()
    s_lower = (status or "").lower()

    # Capture old status for DashboardState notification
    _old_status = "pending"
    try:
        _row = conn.execute("SELECT status FROM leads WHERE id = %s", (lead_id,)).fetchone()
        if _row:
            _old_status = str(_row["status"] or "pending").strip().lower()
    except Exception:
        pass

    # When a lead is released back to 'pending' or starts 'dialing',
    # clear its call markers so it is NOT counted as "called" and doesn't
    # carry stale analysis/ratings from previous attempts.
    clear_on_pending = (s_lower in ("pending", "dialing"))
    if analysis:
        if clear_on_pending:
            conn.execute(
                f"UPDATE leads SET status = %s, error = NULL, analysis = NULL, "
                f"start_time = NULL, _log_id = NULL, _call_id = NULL, updated_at = {_NOW_SQL} WHERE id = %s",
                (status, lead_id),
            )
        else:
            conn.execute(
                f"UPDATE leads SET status = %s, error = %s, analysis = %s, updated_at = {_NOW_SQL} WHERE id = %s",
                (status, error, json.dumps(analysis), lead_id)
            )
    else:
        if clear_on_pending:
            conn.execute(
                f"UPDATE leads SET status = %s, error = NULL, analysis = NULL, start_time = NULL, "
                f"_log_id = NULL, _call_id = NULL, updated_at = {_NOW_SQL} WHERE id = %s",
                (status, lead_id),
            )
        else:
            conn.execute(
                f"UPDATE leads SET status = %s, error = %s, updated_at = {_NOW_SQL} WHERE id = %s",
                (status, error, lead_id)
            )

    # Cancel pending scheduled retries if resolved
    if s_lower in ("completed", "not_interested", "callback_completed", "callback_scheduled", "site_visit", "site_visited", "interested"):
        conn.execute(
            "DELETE FROM scheduled_callbacks WHERE lead_id = %s AND status = 'scheduled'",
            (lead_id,)
        )

    conn.commit()
    _invalidate_state_cache()

    # Notify materialized dashboard state
    try:
        from core.dashboard_state import notify_lead_updated
        notify_lead_updated(
            role=str(conn.execute("SELECT role FROM leads WHERE id = %s", (lead_id,)).fetchone()["role"]),
            lead_id=lead_id, old_status=_old_status, new_status=s_lower,
            analysis_raw=analysis,
        )
    except Exception:
        pass


async def update_lead_call_info(lead_id: int, log_id: str = None, call_id: str = None, start_time: float = None, outbound_phone: str = None):
    return await asyncio.to_thread(_update_lead_call_info_sync, lead_id, log_id, call_id, start_time, outbound_phone)

def _update_lead_call_info_sync(lead_id: int, log_id: str = None, call_id: str = None, start_time: float = None, outbound_phone: str = None):
    conn = _get_conn()
    updates = []
    params = []
    if log_id is not None:
        updates.append("_log_id = %s")
        params.append(log_id)
    if call_id is not None:
        updates.append("_call_id = %s")
        params.append(call_id)
    if start_time is not None:
        updates.append("start_time = %s")
        params.append(start_time)
        updates.append("first_called_at = COALESCE(first_called_at, %s)")
        params.append(start_time)
    if outbound_phone is not None:
        updates.append("outbound_phone = %s")
        params.append(outbound_phone)
    updates.append(f"updated_at = {_NOW_SQL}")
    params.append(lead_id)
    conn.execute(f"UPDATE leads SET {', '.join(updates)} WHERE id = %s", params)
    conn.commit()
    _invalidate_state_cache()


async def update_lead_info(lead_id: int, name: str = None, email: str = None):
    return await asyncio.to_thread(_update_lead_info_sync, lead_id, name, email)

def _update_lead_info_sync(lead_id: int, name: str = None, email: str = None):
    """Patch a lead's name/email columns (used by incoming-call matching)."""
    conn = _get_conn()
    updates = []
    params = []
    if name is not None:
        updates.append("name = %s")
        params.append(name)
    if email is not None:
        updates.append("email = %s")
        params.append(email)
    if not updates:
        return
    updates.append(f"updated_at = {_NOW_SQL}")
    params.append(int(lead_id))
    conn.execute(f"UPDATE leads SET {', '.join(updates)} WHERE id = %s", params)
    conn.commit()
    _invalidate_state_cache()


async def reschedule_leads(
    role: str,
    from_date_iso: str,
    to_date_iso: str,
    outcomes: list[str],
    target_epoch: float,
) -> int:
    return await asyncio.to_thread(
        _reschedule_leads_sync, role, from_date_iso, to_date_iso, outcomes, target_epoch
    )


def _reschedule_leads_sync(
    role: str,
    from_date_iso: str,
    to_date_iso: str,
    outcomes: list[str],
    target_epoch: float,
) -> int:
    """Reschedule historical campaign leads for a future callback.

    ``from_date_iso`` and ``to_date_iso`` are inclusive dates (YYYY-MM-DD).
    ``outcomes`` maps from frontend-friendly keys to DB filters:
      - failed_no_answer  -> status in ('failed', 'error', 'no answer')
      - interested        -> disposition == 'Interested'
      - cut_in_middle     -> status in ('failed', 'completed') with a start_time
      - not_interested    -> status == 'not_interested' or disposition == 'Not Interested'
    """
    from datetime import datetime, time, timezone

    role = (role or "sales_1").strip().lower()
    tz = timezone.utc
    try:
        from_dt = datetime.strptime(from_date_iso, "%Y-%m-%d").replace(tzinfo=tz)
        to_dt = datetime.strptime(to_date_iso, "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        raise ValueError("Dates must be YYYY-MM-DD")

    from_epoch = from_dt.timestamp()
    to_epoch = datetime.combine(to_dt.date(), time.max, tzinfo=tz).timestamp()

    if not outcomes:
        return 0

    outcome_conditions = []
    for oc in outcomes:
        if oc == "failed_no_answer":
            outcome_conditions.append("status IN ('failed', 'error', 'no answer')")
        elif oc == "interested":
            outcome_conditions.append(
                "(analysis::json->>'disposition' = 'Interested' OR status = 'completed')"
            )
        elif oc == "cut_in_middle":
            outcome_conditions.append(
                "(status IN ('failed', 'completed') AND start_time IS NOT NULL AND start_time > 0)"
            )
        elif oc == "not_interested":
            outcome_conditions.append(
                "(status = 'not_interested' OR analysis::json->>'disposition' = 'Not Interested')"
            )

    if not outcome_conditions:
        return 0

    where_sql = "role = %s AND start_time IS NOT NULL AND start_time >= %s AND start_time <= %s AND (" + " OR ".join(outcome_conditions) + ")"

    conn = _get_conn()
    # Load matching rows so we can merge callback_reminder_epoch into their analysis JSON.
    rows = conn.execute(
        f"SELECT id, analysis FROM leads WHERE {where_sql}",
        (role, from_epoch, to_epoch),
    ).fetchall()

    updated = 0
    for row in rows:
        try:
            analysis = json.loads(row["analysis"] or "{}") if row["analysis"] else {}
        except Exception:
            analysis = {}
        analysis["callback_reminder_epoch"] = float(target_epoch)
        analysis["rescheduled_at_epoch"] = time.time()
        analysis["rescheduled_from_status"] = "campaign_reschedule"
        conn.execute(
            f"UPDATE leads SET status = 'callback_scheduled', analysis = %s, updated_at = {_NOW_SQL} WHERE id = %s",
            (json.dumps(analysis), row["id"]),
        )
        updated += 1

    conn.commit()
    _invalidate_state_cache()
    logger.info(
        "Rescheduled {} lead(s) for role={} between {} and {} to callback epoch {}",
        updated, role, from_date_iso, to_date_iso, target_epoch
    )
    return updated


async def retry_all_failed_leads(role: str) -> int:
    return await asyncio.to_thread(_retry_all_failed_leads_sync, role)

def _retry_all_failed_leads_sync(role: str) -> int:
    role = (role or "sales_1").strip().lower()
    conn = _get_conn()

    rows = conn.execute(
        """
        SELECT id, extra, analysis FROM leads 
        WHERE role = %s 
          AND (
               status IN ('failed', 'error', 'busy', 'no answer', 'no response', 'no_response')
            OR analysis::json->>'disposition' IN ('Failed', 'No Answer', 'Busy', 'Wrong Number', 'Not Available', 'Voicemail', 'No Response')
          )
        """,
        (role,)
    ).fetchall()

    updated = 0
    for row in rows:
        lead_id = row["id"]
        try:
            extra = json.loads(row["extra"] or "{}") if row["extra"] else {}
        except Exception:
            extra = {}
        try:
            analysis = json.loads(row["analysis"] or "{}") if row["analysis"] else {}
        except Exception:
            analysis = {}

        extra["failed_call_retries"] = 0

        # Reset the callback reminders if any
        if "callback_reminder_epoch" in analysis:
            del analysis["callback_reminder_epoch"]
        if "requested_callback_datetime_iso" in analysis:
            del analysis["requested_callback_datetime_iso"]

        # Delete all pending/scheduled callbacks for this lead to avoid duplicates when redialing
        conn.execute(
            "DELETE FROM scheduled_callbacks WHERE lead_id = %s AND status IN ('pending', 'scheduled', 'queued')",
            (lead_id,)
        )

        conn.execute(
            f"""
            UPDATE leads 
            SET status = 'pending', 
                start_time = NULL, 
                error = NULL, 
                _log_id = NULL, 
                _call_id = NULL, 
                extra = %s, 
                analysis = '{{}}', 
                updated_at = {_NOW_SQL}
            WHERE id = %s
            """,
            (json.dumps(extra), lead_id)
        )
        updated += 1

    conn.commit()
    _invalidate_state_cache()
    logger.info("Reset {} failed lead(s) to pending for role={}", updated, role)
    return updated


async def promote_due_scheduled_callbacks(now_epoch: float | None = None) -> int:
    return await asyncio.to_thread(_promote_due_scheduled_callbacks_sync, now_epoch)

def _promote_due_scheduled_callbacks_sync(now_epoch: float | None = None) -> int:
    """Move leads whose defer-until epoch has passed.

    Promotes:
      - ``callback_scheduled`` → ``pending``
      - ``busy`` / ``failed`` / ``no answer`` → ``pending`` when callback_reminder_epoch is due.
    """

    t = float(now_epoch if now_epoch is not None else time.time())
    conn = None
    try:
        conn = _get_conn()
        # 1. Classic callback_scheduled → pending
        cur = conn.execute(
            f"""
            UPDATE leads SET status = 'pending',
                   updated_at = {_NOW_SQL}
             WHERE status = 'callback_scheduled'
               AND analysis::json->>'callback_reminder_epoch' IS NOT NULL
               AND NULLIF(analysis::json->>'callback_reminder_epoch', '')::DOUBLE PRECISION > 0
               AND NULLIF(analysis::json->>'callback_reminder_epoch', '')::DOUBLE PRECISION <= %s
            """,
            (t,),
        )
        n1 = int(cur.rowcount or 0)

        # 2. Busy / failed / no-answer leads whose retry cooldown has expired
        cur2 = conn.execute(
            f"""
            UPDATE leads SET status = 'pending',
                   updated_at = {_NOW_SQL}
             WHERE status IN ('busy', 'failed', 'no answer')
               AND analysis::json->>'callback_reminder_epoch' IS NOT NULL
               AND NULLIF(analysis::json->>'callback_reminder_epoch', '')::DOUBLE PRECISION > 0
               AND NULLIF(analysis::json->>'callback_reminder_epoch', '')::DOUBLE PRECISION <= %s
            """,
            (t,),
        )
        n2 = int(cur2.rowcount or 0)

        conn.commit()
        n = n1 + n2
        if n > 0:
            logger.info(f"Promoted {n1} callback_scheduled + {n2} busy/failed/no-answer → pending (due recall)")
            _invalidate_state_cache()
        return n
    except Exception as e:
        logger.error(f"Failed to promote due scheduled callbacks: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception as re:
                logger.error(f"Failed rollback in promote_due_scheduled_callbacks: {re}")
        return 0


async def role_has_future_callback_scheduled(role: str, now_epoch: float) -> bool:
    return await asyncio.to_thread(_role_has_future_callback_scheduled_sync, role, now_epoch)

def _role_has_future_callback_scheduled_sync(role: str, now_epoch: float) -> bool:
    """True if ``role`` has at least one lead waiting for a future transcript-requested recall."""

    from core.state import normalize_console_role as _norm

    rid = _norm(role)
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT 1 FROM leads
        WHERE role = %s
          AND status = 'callback_scheduled'
          AND analysis::json->>'callback_reminder_epoch' IS NOT NULL
          AND NULLIF(analysis::json->>'callback_reminder_epoch', '')::DOUBLE PRECISION > %s
        LIMIT 1
        """,
        (rid, float(now_epoch)),
    ).fetchone()
    return row is not None


async def role_has_pending_scheduled_callbacks(role: str) -> bool:
    """True if ``role`` has any pending/scheduled items in the scheduled_callbacks table.

    Keeps the campaign sub-worker alive even when the pending leads queue is empty,
    so overdue callbacks (failed-call retries, user-requested recalls) are always
    executed instead of being orphaned when the queue drains.
    """
    return await asyncio.to_thread(_role_has_pending_scheduled_callbacks_sync, role)

def _role_has_pending_scheduled_callbacks_sync(role: str) -> bool:
    from core.state import normalize_console_role as _norm
    rid = _norm(role)
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT 1 FROM scheduled_callbacks
        WHERE role = %s
          AND status IN ('scheduled', 'queued', 'pending')
        LIMIT 1
        """,
        (rid,),
    ).fetchone()
    return row is not None


async def reset_leads(role: str):
    return await asyncio.to_thread(_reset_leads_sync, role)

def _reset_leads_sync(role: str):
    conn = _get_conn()
    conn.execute(f"UPDATE leads SET status = 'pending', error = NULL, updated_at = {_NOW_SQL} WHERE role = %s", (role,))
    conn.commit()
    _invalidate_state_cache()


async def wipe_leads(role: str):
    return await asyncio.to_thread(_wipe_leads_sync, role)

def _wipe_leads_sync(role: str):
    conn = _get_conn()
    conn.execute("DELETE FROM leads WHERE role = %s", (role,))
    conn.commit()
    _invalidate_state_cache()


async def get_lead_counts(role: str) -> dict:
    return await asyncio.to_thread(_get_lead_counts_sync, role)

def _get_lead_counts_sync(role: str) -> dict:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT status, COUNT(*) as count FROM leads WHERE role = %s GROUP BY status",
        (role,)
    ).fetchall()
    counts = {"total": 0, "pending": 0, "dialing": 0, "completed": 0, "failed": 0, "not_interested": 0}
    for row in rows:
        status = row["status"]
        count = row["count"]
        counts[status] = count if status in counts else count
        counts["total"] += count
    # WhatsApp & Email sent counts
    wa_row = conn.execute(
        "SELECT COUNT(*) as c FROM leads WHERE role = %s AND whatsapp_sent = 1", (role,)
    ).fetchone()
    counts["whatsapp_sent_count"] = int(wa_row["c"] or 0) if wa_row else 0
    em_row = conn.execute(
        "SELECT COUNT(*) as c FROM leads WHERE role = %s AND email_sent = 1", (role,)
    ).fetchone()
    counts["email_sent_count"] = int(em_row["c"] or 0) if em_row else 0
    return counts


async def count_scheduled_callbacks_due_today(role: str) -> int:
    """Count leads with ``callback_scheduled`` status whose callback is due today or earlier."""
    return await asyncio.to_thread(_count_scheduled_callbacks_due_today_sync, role)

def _count_scheduled_callbacks_due_today_sync(role: str) -> int:
    conn = _get_conn()
    now = datetime.now(_dashboard_tz())
    tomorrow_start = datetime.combine(
        now.date() + timedelta(days=1),
        datetime.min.time(),
        tzinfo=_dashboard_tz(),
    ).timestamp()
    row = conn.execute(
        """
        SELECT COUNT(*) as c FROM leads
        WHERE role = %s AND status = 'callback_scheduled'
          AND analysis::json->>'callback_reminder_epoch' IS NOT NULL
          AND NULLIF(analysis::json->>'callback_reminder_epoch', '')::DOUBLE PRECISION > 0
          AND NULLIF(analysis::json->>'callback_reminder_epoch', '')::DOUBLE PRECISION < %s
          AND NOT EXISTS (
              SELECT 1 FROM scheduled_callbacks sc
              WHERE sc.lead_id = leads.id
                AND sc.status IN ('scheduled', 'queued', 'calling')
          )
        """,
        (role, tomorrow_start),
    ).fetchone()
    lead_due = int(row["c"] or 0) if row else 0
    row = conn.execute(
        """
        SELECT COUNT(*) as c FROM scheduled_callbacks
        WHERE role = %s AND status IN ('scheduled', 'queued', 'calling')
          AND scheduled_at > 0 AND scheduled_at < %s
        """,
        (role, tomorrow_start),
    ).fetchone()
    scheduled_due = int(row["c"] or 0) if row else 0
    return lead_due + scheduled_due


async def count_callbacks_completed_today(role: str) -> int:
    """Count leads that were completed (disposition set) today."""
    return await asyncio.to_thread(_count_callbacks_completed_today_sync, role)

def _count_callbacks_completed_today_sync(role: str) -> int:
    conn = _get_conn()
    now = datetime.now(_dashboard_tz())
    today_start = datetime.combine(
        now.date(),
        datetime.min.time(),
        tzinfo=_dashboard_tz(),
    ).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        """
        SELECT COUNT(*) as c FROM leads
        WHERE role = %s AND status IN ('callback_completed', 'completed')
          AND updated_at >= %s
          AND (
                analysis::json->>'callback_reminder_epoch' IS NOT NULL
             OR analysis::json->>'requested_callback_datetime_iso' IS NOT NULL
          )
          AND NOT EXISTS (
              SELECT 1 FROM scheduled_callbacks sc
              WHERE sc.lead_id = leads.id
                AND sc.status = 'completed'
                AND sc.updated_at >= %s
          )
        """,
        (role, today_start, today_start),
    ).fetchone()
    lead_completed = int(row["c"] or 0) if row else 0
    row = conn.execute(
        """
        SELECT COUNT(*) as c FROM scheduled_callbacks
        WHERE role = %s AND status = 'completed' AND updated_at >= %s
        """,
        (role, today_start),
    ).fetchone()
    scheduled_completed = int(row["c"] or 0) if row else 0
    return lead_completed + scheduled_completed


async def export_leads_csv(role: str, status_filter: str = "all") -> list[dict]:
    return await asyncio.to_thread(_export_leads_csv_sync, role, status_filter)

def _export_leads_csv_sync(role: str, status_filter: str = "all") -> list[dict]:
    conn = _get_conn()
    query = "SELECT id, name, phone, email, status, start_time, created_at, analysis, whatsapp_sent, email_sent, error, _call_id, _log_id FROM leads WHERE role = %s"
    params = [role]
    if status_filter != "all":
        filter_map = {
            "responded": "completed",
            "not_responded": "IN ('failed', 'pending', 'dialing')",
            "not_interested": "not_interested",
        }
        status_val = filter_map.get(status_filter, status_filter)
        if "IN" in status_val:
            query += f" AND status {status_val}"
        else:
            query += " AND status = %s"
            params.append(status_val)
    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


async def find_lead_by_phone(role: str, raw_phone: str, status: Optional[str] = None) -> Optional[dict]:
    return await asyncio.to_thread(_find_lead_by_phone_sync, role, raw_phone, status)

def _find_lead_by_phone_sync(role: str, raw_phone: str, status: Optional[str] = None) -> Optional[dict]:
    """Match a lead row by normalized or last-10-digit phone for any campaign role.
    If *status* is provided, only match leads with that status.
    """
    from core.utils import _norm_phone_str

    role = (role or "sellers").strip().lower()
    norm = _norm_phone_str(raw_phone or "")
    conn = _get_conn()

    def _query(where: str, params: tuple) -> Optional[dict]:
        if status:
            row = conn.execute(
                f"SELECT * FROM leads WHERE {where} AND status = %s ORDER BY updated_at DESC LIMIT 1",
                (*params, status),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT * FROM leads WHERE {where} ORDER BY updated_at DESC LIMIT 1",
                params,
            ).fetchone()
        return _row_to_dict(row) if row else None

    if norm:
        result = _query("role = %s AND phone = %s", (role, norm))
        if result:
            return result
    digits = "".join(c for c in str(raw_phone or "") if c.isdigit())
    if len(digits) < 10:
        return None
    tail = digits[-10:]
    if len(tail) == 10:
        return _query("role = %s AND phone LIKE %s", (role, f"%{tail}"))
    return None


async def find_or_create_callback_lead(role: str, phone: str, name: str = "") -> int:
    """Reuse an existing lead row with ``callback_scheduled`` status for the same
    phone+role, or create a new one if none exists.

    Returns the lead ID (existing or newly created).
    """
    return await asyncio.to_thread(_find_or_create_callback_lead_sync, role, phone, name)

def _find_or_create_callback_lead_sync(role: str, phone: str, name: str = "") -> int:
    existing = _find_lead_by_phone_sync(role, phone, status="callback_scheduled")
    if existing:
        return existing["id"]
    existing = _find_lead_by_phone_sync(role, phone, status="failed")
    if existing:
        return existing["id"]
    existing = _find_lead_by_phone_sync(role, phone)
    if existing:
        return existing["id"]
    return _add_lead_sync(role, name or "Callback", phone)


# --- Sandbox Agents ---

async def create_agent(name: str, prompt: str, voice: str = "Puck", role: str = "factory") -> str:
    return await asyncio.to_thread(_create_agent_sync, name, prompt, voice, role)

def _create_agent_sync(name: str, prompt: str, voice: str = "Puck", role: str = "factory") -> str:
    import uuid
    agent_id = str(uuid.uuid4())
    conn = _get_conn()
    conn.execute(
        "INSERT INTO agents (id, role, name, prompt, voice) VALUES (%s, %s, %s, %s, %s)",
        (agent_id, role, name, prompt, voice)
    )
    conn.commit()
    return agent_id


async def get_agent(agent_id: str) -> Optional[dict]:
    return await asyncio.to_thread(_get_agent_sync, agent_id)

def _get_agent_sync(agent_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM agents WHERE id = %s", (agent_id,)).fetchone()
    if not row:
        return None
    result = _row_to_dict(row)
    result["knowledge_files"] = json.loads(result.get("knowledge_files", "[]"))
    return result


async def list_agents(role: Optional[str] = None) -> list[dict]:
    return await asyncio.to_thread(_list_agents_sync, role)

def _list_agents_sync(role: Optional[str] = None) -> list[dict]:
    conn = _get_conn()
    if role:
        rows = conn.execute("SELECT * FROM agents WHERE role = %s ORDER BY created_at DESC", (role,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM agents ORDER BY created_at DESC").fetchall()
    result = []
    for row in rows:
        r = _row_to_dict(row)
        r["knowledge_files"] = json.loads(r.get("knowledge_files", "[]"))
        result.append(r)
    return result


async def update_agent(agent_id: str, name: str = None, prompt: str = None, voice: str = None):
    return await asyncio.to_thread(_update_agent_sync, agent_id, name, prompt, voice)

def _update_agent_sync(agent_id: str, name: str = None, prompt: str = None, voice: str = None):
    conn = _get_conn()
    updates = []
    params = []
    if name is not None:
        updates.append("name = %s")
        params.append(name)
    if prompt is not None:
        updates.append("prompt = %s")
        params.append(prompt)
    if voice is not None:
        updates.append("voice = %s")
        params.append(voice)
    if not updates:
        return
    updates.append(f"updated_at = {_NOW_SQL}")
    params.append(agent_id)
    conn.execute(f"UPDATE agents SET {', '.join(updates)} WHERE id = %s", params)
    conn.commit()


async def delete_agent(agent_id: str) -> bool:
    return await asyncio.to_thread(_delete_agent_sync, agent_id)

def _delete_agent_sync(agent_id: str) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM agents WHERE id = %s", (agent_id,))
    conn.commit()
    return cur.rowcount > 0


async def add_agent_knowledge_file(agent_id: str, file_id: str, filename: str, extracted_text: str):
    return await asyncio.to_thread(_add_agent_knowledge_file_sync, agent_id, file_id, filename, extracted_text)

def _add_agent_knowledge_file_sync(agent_id: str, file_id: str, filename: str, extracted_text: str):
    conn = _get_conn()
    row = conn.execute("SELECT knowledge_files FROM agents WHERE id = %s", (agent_id,)).fetchone()
    if not row:
        return
    files = json.loads(row["knowledge_files"] or "[]")
    files.append({
        "file_id": file_id,
        "filename": filename,
        "extracted_text": extracted_text,
        "added_at": datetime.now(timezone.utc).isoformat(),
    })
    conn.execute(
        f"UPDATE agents SET knowledge_files = %s, updated_at = {_NOW_SQL} WHERE id = %s",
        (json.dumps(files), agent_id)
    )
    conn.commit()


async def add_agent_lead(agent_id: str, lead: dict) -> str:
    return await asyncio.to_thread(_add_agent_lead_sync, agent_id, lead)

def _add_agent_lead_sync(agent_id: str, lead: dict) -> str:
    import uuid
    lead_id = str(uuid.uuid4())
    conn = _get_conn()
    conn.execute(
        "INSERT INTO agent_leads (agent_id, lead_id, name, phone, email, company) VALUES (%s, %s, %s, %s, %s, %s)",
        (agent_id, lead_id, lead.get("name", "Unknown"), lead.get("phone", ""), lead.get("email", ""), lead.get("company", ""))
    )
    conn.commit()
    return lead_id


async def get_agent_leads(agent_id: str) -> list[dict]:
    return await asyncio.to_thread(_get_agent_leads_sync, agent_id)

def _get_agent_leads_sync(agent_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM agent_leads WHERE agent_id = %s ORDER BY created_at DESC",
        (agent_id,)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# --- Campaign Cases ---

async def list_cases(role: str) -> list[dict]:
    return await asyncio.to_thread(_list_cases_sync, role)

def _list_cases_sync(role: str) -> list[dict]:
    """All cases for a role, newest first. Each row is a plain dict."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, role, name, description, active, created_at, updated_at "
        "FROM cases WHERE role = %s ORDER BY active DESC, created_at DESC",
        (role,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = _row_to_dict(r)
        d["active"] = bool(d.get("active"))
        out.append(d)
    return out


from typing import Optional, Union, List

async def get_active_case(role: str) -> Optional[dict]:
    return await asyncio.to_thread(_get_active_case_sync, role)

def _get_active_case_sync(role: str) -> Optional[dict]:
    """Return the (single) active case for a role, or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, role, name, description, active, created_at, updated_at "
        "FROM cases WHERE role = %s AND active = 1 LIMIT 1",
        (role,),
    ).fetchone()
    if not row:
        return None
    d = _row_to_dict(row)
    d["active"] = True
    return d


async def add_case(role: str, name: str, description: str = "") -> int:
    return await asyncio.to_thread(_add_case_sync, role, name, description)

def _add_case_sync(role: str, name: str, description: str = "") -> int:
    """Insert a new case (inactive by default). Returns the new id."""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO cases (role, name, description, active) VALUES (%s, %s, %s, 0) RETURNING id",
        (role, name.strip(), description or ""),
    )
    conn.commit()
    return int(cur.fetchone()["id"])


async def update_case(case_id: int, name: Optional[str] = None, description: Optional[str] = None) -> bool:
    return await asyncio.to_thread(_update_case_sync, case_id, name, description)

def _update_case_sync(case_id: int, name: Optional[str] = None, description: Optional[str] = None) -> bool:
    """Update a case's name and/or description. Returns True if a row changed."""
    conn = _get_conn()
    sets: list[str] = []
    params: list = []
    if name is not None:
        sets.append("name = %s")
        params.append(name.strip())
    if description is not None:
        sets.append("description = %s")
        params.append(description)
    if not sets:
        return False
    sets.append(f"updated_at = {_NOW_SQL}")
    params.append(case_id)
    cur = conn.execute(
        f"UPDATE cases SET {', '.join(sets)} WHERE id = %s",
        params,
    )
    conn.commit()
    return cur.rowcount > 0


async def delete_case(case_id: int) -> bool:
    return await asyncio.to_thread(_delete_case_sync, case_id)

def _delete_case_sync(case_id: int) -> bool:
    """Delete a case. Returns True if a row was removed."""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM cases WHERE id = %s", (case_id,))
    conn.commit()
    return cur.rowcount > 0


async def set_active_case(role: str, case_id: Optional[int]) -> bool:
    return await asyncio.to_thread(_set_active_case_sync, role, case_id)

def _set_active_case_sync(role: str, case_id: Optional[int]) -> bool:
    """Activate exactly one case for ``role`` (or none if ``case_id`` is None).

    Always deactivates any currently-active case for that role first so the
    invariant "at most one active case per role" cannot be violated.
    """
    conn = _get_conn()
    conn.execute(
        f"UPDATE cases SET active = 0, updated_at = {_NOW_SQL} "
        "WHERE role = %s AND active = 1",
        (role,),
    )
    if case_id is None:
        conn.commit()
        return True
    cur = conn.execute(
        f"UPDATE cases SET active = 1, updated_at = {_NOW_SQL} "
        "WHERE id = %s AND role = %s",
        (case_id, role),
    )
    conn.commit()
    return cur.rowcount > 0


# --- Campaign Schedules ---

# Allowed status transitions:
#   scheduled -> running | cancelled | failed
#   running   -> completed | failed
# Anything else is a bug; we still let the row update but the API/UI never
# surfaces those transitions.
_SCHEDULE_VALID_STATUSES = {
    "scheduled", "running", "completed", "failed", "cancelled",
}


async def add_schedule(
    role: str,
    run_at: float,
    name: str = "",
    stop_at: float | None = None,
) -> int:
    return await asyncio.to_thread(_add_schedule_sync, role, run_at, name, stop_at)

def _add_schedule_sync(
    role: str,
    run_at: float,
    name: str = "",
    stop_at: float | None = None,
) -> int:
    """Schedule a campaign run for ``role`` at epoch ``run_at`` (UTC seconds).

    If ``stop_at`` (also epoch-UTC) is given, the worker auto-stops the campaign
    at that moment — useful for "run from 9 AM to 5 PM only" windows.

    Returns the new schedule id. ``name`` is an optional human label
    (e.g. "Friday morning blast").
    """
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO schedules (role, name, run_at, stop_at, status) "
        "VALUES (%s, %s, %s, %s, 'scheduled') RETURNING id",
        (
            role,
            (name or "").strip(),
            float(run_at),
            float(stop_at) if stop_at is not None else None,
        ),
    )
    conn.commit()
    return int(cur.fetchone()["id"])


# Column list re-used across SELECTs so adding fields stays a one-line change.
_SCHEDULE_COLS = (
    "id, role, name, run_at, stop_at, status, "
    "created_at, updated_at, started_at, error"
)


async def list_schedules(role: str, limit: int = 100) -> list[dict]:
    return await asyncio.to_thread(_list_schedules_sync, role, limit)

def _list_schedules_sync(role: str, limit: int = 100) -> list[dict]:
    """All schedules for ``role``, soonest first (active/scheduled on top)."""
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE role = %s "
        "ORDER BY CASE status "
        "    WHEN 'running'   THEN 0 "
        "    WHEN 'scheduled' THEN 1 "
        "    ELSE 2 END, "
        "run_at ASC LIMIT %s",
        (role, int(limit)),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_schedule(schedule_id: int) -> dict | None:
    return await asyncio.to_thread(_get_schedule_sync, schedule_id)

def _get_schedule_sync(schedule_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE id = %s",
        (schedule_id,),
    ).fetchone()
    return _row_to_dict(row) if row else None


async def cancel_schedule(schedule_id: int) -> bool:
    return await asyncio.to_thread(_cancel_schedule_sync, schedule_id)

def _cancel_schedule_sync(schedule_id: int) -> bool:
    """Mark a *scheduled* (not-yet-started) run as cancelled. Returns True on success."""
    conn = _get_conn()
    cur = conn.execute(
        f"UPDATE schedules SET status = 'cancelled', updated_at = {_NOW_SQL} "
        "WHERE id = %s AND status = 'scheduled'",
        (schedule_id,),
    )
    conn.commit()
    return cur.rowcount > 0


async def mark_schedule_status(
    schedule_id: int,
    status: str,
    error: str | None = None,
    started_at: float | None = None,
) -> bool:
    return await asyncio.to_thread(_mark_schedule_status_sync, schedule_id, status, error, started_at)

def _mark_schedule_status_sync(
    schedule_id: int,
    status: str,
    error: str | None = None,
    started_at: float | None = None,
) -> bool:
    """Update a schedule's lifecycle status. Returns True if a row changed."""
    if status not in _SCHEDULE_VALID_STATUSES:
        return False
    conn = _get_conn()
    sets = ["status = %s", f"updated_at = {_NOW_SQL}"]
    params: list = [status]
    if error is not None:
        sets.append("error = %s")
        params.append(error)
    if started_at is not None:
        sets.append("started_at = %s")
        params.append(float(started_at))
    params.append(schedule_id)
    cur = conn.execute(
        f"UPDATE schedules SET {', '.join(sets)} WHERE id = %s",
        params,
    )
    conn.commit()
    return cur.rowcount > 0


async def due_schedules(now_epoch: float, lookahead_sec: float = 0.0) -> list[dict]:
    return await asyncio.to_thread(_due_schedules_sync, now_epoch, lookahead_sec)

def _due_schedules_sync(now_epoch: float, lookahead_sec: float = 0.0) -> list[dict]:
    """All schedules that are eligible to fire at ``now_epoch``.

    ``lookahead_sec`` is for callers that want to peek slightly in the future
    (e.g. to warn the user). The worker always passes 0.
    """
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_SCHEDULE_COLS} FROM schedules "
        "WHERE status = 'scheduled' AND run_at <= %s ORDER BY run_at ASC",
        (float(now_epoch) + float(lookahead_sec),),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


async def expired_running_schedules(now_epoch: float) -> list[dict]:
    return await asyncio.to_thread(_expired_running_schedules_sync, now_epoch)

def _expired_running_schedules_sync(now_epoch: float) -> list[dict]:
    """All ``running`` schedules whose ``stop_at`` has passed.

    Used by the scheduler loop to enforce the auto-stop window even after a
    server restart (which would have orphaned the inline stop watcher).
    """
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_SCHEDULE_COLS} FROM schedules "
        "WHERE status = 'running' AND stop_at IS NOT NULL AND stop_at <= %s "
        "ORDER BY stop_at ASC",
        (float(now_epoch),),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# --- Manual calls (console "Make a Call") ---


async def insert_manual_call(role: str, camp_id: str, to_phone: str, callee_name: str) -> int:
    return await asyncio.to_thread(_insert_manual_call_sync, role, camp_id, to_phone, callee_name)

def _insert_manual_call_sync(role: str, camp_id: str, to_phone: str, callee_name: str) -> int:
    conn = _get_conn()
    cur = conn.execute(
        """
        INSERT INTO manual_calls (role, camp_id, to_phone, callee_name, status)
        VALUES (%s, %s, %s, %s, 'dialing') RETURNING id
        """,
        (role, camp_id, to_phone or "", callee_name or ""),
    )
    conn.commit()
    return int(cur.fetchone()["id"])


async def mark_manual_call_failed(camp_id: str, message: str = "") -> None:
    return await asyncio.to_thread(_mark_manual_call_failed_sync, camp_id, message)

def _mark_manual_call_failed_sync(camp_id: str, message: str = "") -> None:
    conn = _get_conn()
    conn.execute(
        f"""
        UPDATE manual_calls SET status = 'failed', error = %s, updated_at = {_NOW_SQL}
        WHERE camp_id = %s AND status != 'completed'
        """,
        ((message or "")[:2000], camp_id),
    )
    conn.commit()


async def manual_call_row_by_camp_id(camp_id: str) -> Optional[dict]:
    return await asyncio.to_thread(_manual_call_row_by_camp_id_sync, camp_id)

def _manual_call_row_by_camp_id_sync(camp_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM manual_calls WHERE camp_id = %s", (camp_id,)).fetchone()
    return dict(row) if row else None


async def lead_row_by_call_id(call_id: str) -> Optional[dict]:
    return await asyncio.to_thread(_lead_row_by_call_id_sync, call_id)


def _lead_row_by_call_id_sync(call_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM leads WHERE _call_id = %s", (call_id,)).fetchone()
    return dict(row) if row else None


async def manual_call_exists_for_camp(camp_id: str) -> bool:
    return await asyncio.to_thread(_manual_call_exists_for_camp_sync, camp_id)

def _manual_call_exists_for_camp_sync(camp_id: str) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT 1 FROM manual_calls WHERE camp_id = %s", (camp_id,)).fetchone()
    return row is not None


async def finalize_manual_call_record(
    camp_id: str,
    log_id: str,
    duration_sec: Optional[float],
    analysis: dict[str, Any],
) -> None:
    return await asyncio.to_thread(_finalize_manual_call_record_sync, camp_id, log_id, duration_sec, analysis)

def _finalize_manual_call_record_sync(
    camp_id: str,
    log_id: str,
    duration_sec: Optional[float],
    analysis: dict[str, Any],
) -> None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, status, log_id FROM manual_calls WHERE camp_id = %s",
        (camp_id,),
    ).fetchone()
    if not row:
        return
    # The hangup webhook may already have flipped the row to 'completed'
    # before the WS finalize runs. When that happens, still record the
    # log_id (needed for transcript/recording lookup) but never clobber an
    # analysis that the post-call analyzer wrote.
    if (row["status"] or "") == "completed":
        if (log_id or "").strip() and not (row.get("log_id") or "").strip():
            conn.execute(
                f"UPDATE manual_calls SET log_id = %s, updated_at = {_NOW_SQL} WHERE camp_id = %s",
                (log_id, camp_id),
            )
            conn.commit()
        return
    aj = json.dumps(analysis, ensure_ascii=False)
    conf = analysis.get("emotion_confidence")
    try:
        conf_f = float(conf) if conf is not None and str(conf).strip() != "" else None
    except (TypeError, ValueError):
        conf_f = None
    conn.execute(
        f"""
        UPDATE manual_calls SET
            log_id = %s,
            status = 'completed',
            ended_at = {_NOW_SQL},
            duration_sec = %s,
            disposition = %s,
            summary = %s,
            next_steps = %s,
            emotion_label = %s,
            emotion_rationale = %s,
            emotion_confidence = %s,
            analysis_json = %s,
            updated_at = {_NOW_SQL}
        WHERE camp_id = %s
        """,
        (
            log_id or "",
            duration_sec,
            str(analysis.get("disposition") or ""),
            str(analysis.get("summary") or ""),
            str(analysis.get("next_steps") or ""),
            str(analysis.get("emotion_label") or ""),
            str(analysis.get("emotion_rationale") or ""),
            conf_f,
            aj,
            camp_id,
        ),
    )
    conn.commit()


async def update_manual_call_analysis_by_id(call_id: int, analysis: dict[str, Any]) -> bool:
    return await asyncio.to_thread(_update_manual_call_analysis_by_id_sync, call_id, analysis)

def _update_manual_call_analysis_by_id_sync(call_id: int, analysis: dict[str, Any]) -> bool:
    """Rewrite analyzer fields on a manual_calls row (e.g. Re-analyze button)."""
    conn = _get_conn()
    row = conn.execute("SELECT id FROM manual_calls WHERE id = %s", (int(call_id),)).fetchone()
    if not row:
        return False
    aj = json.dumps(analysis, ensure_ascii=False)
    conf = analysis.get("emotion_confidence")
    try:
        conf_f = float(conf) if conf is not None and str(conf).strip() != "" else None
    except (TypeError, ValueError):
        conf_f = None
    conn.execute(
        f"""
        UPDATE manual_calls SET
            disposition = %s,
            summary = %s,
            next_steps = %s,
            emotion_label = %s,
            emotion_rationale = %s,
            emotion_confidence = %s,
            analysis_json = %s,
            error = '',
            updated_at = {_NOW_SQL}
        WHERE id = %s
        """,
        (
            str(analysis.get("disposition") or ""),
            str(analysis.get("summary") or ""),
            str(analysis.get("next_steps") or ""),
            str(analysis.get("emotion_label") or ""),
            str(analysis.get("emotion_rationale") or ""),
            conf_f,
            aj,
            int(call_id),
        ),
    )
    conn.commit()
    return True


async def get_manual_call_by_id(call_id: int) -> Optional[dict]:
    return await asyncio.to_thread(_get_manual_call_by_id_sync, call_id)

def _get_manual_call_by_id_sync(call_id: int) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM manual_calls WHERE id = %s", (int(call_id),)).fetchone()
    return dict(row) if row else None


async def list_recent_manual_calls(role: str, limit: int = 15) -> list[dict]:
    return await asyncio.to_thread(_list_recent_manual_calls_sync, role, limit)

def _list_recent_manual_calls_sync(role: str, limit: int = 15) -> list[dict]:
    conn = _get_conn()
    lim = max(1, min(int(limit), 50))
    rows = conn.execute(
        """
        SELECT * FROM manual_calls WHERE role = %s
        ORDER BY id DESC LIMIT %s
        """,
        (role, lim),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Incoming calls (customer call-backs) ---


async def insert_incoming_call(role: str, camp_id: str, from_phone: str, caller_name: str) -> int:
    return await asyncio.to_thread(_insert_incoming_call_sync, role, camp_id, from_phone, caller_name)


def _insert_incoming_call_sync(role: str, camp_id: str, from_phone: str, caller_name: str) -> int:
    conn = _get_conn()
    cur = conn.execute(
        """
        INSERT INTO incoming_calls (role, camp_id, from_phone, caller_name, status)
        VALUES (%s, %s, %s, %s, 'ringing') RETURNING id
        """,
        (role, camp_id, from_phone or "", caller_name or ""),
    )
    conn.commit()
    return int(cur.fetchone()["id"])


async def mark_incoming_call_failed(camp_id: str, message: str = "") -> None:
    return await asyncio.to_thread(_mark_incoming_call_failed_sync, camp_id, message)


def _mark_incoming_call_failed_sync(camp_id: str, message: str = "") -> None:
    conn = _get_conn()
    conn.execute(
        f"""
        UPDATE incoming_calls SET status = 'failed', error = %s, updated_at = {_NOW_SQL}
        WHERE camp_id = %s AND status != 'completed'
        """,
        ((message or "")[:2000], camp_id),
    )
    conn.commit()


async def update_incoming_call_status(camp_id: str, status: str) -> None:
    return await asyncio.to_thread(_update_incoming_call_status_sync, camp_id, status)


def _update_incoming_call_status_sync(camp_id: str, status: str) -> None:
    conn = _get_conn()
    conn.execute(
        f"UPDATE incoming_calls SET status = %s, updated_at = {_NOW_SQL} WHERE camp_id = %s AND status != 'completed'",
        (status, camp_id),
    )
    conn.commit()


async def incoming_call_row_by_camp_id(camp_id: str) -> Optional[dict]:
    return await asyncio.to_thread(_incoming_call_row_by_camp_id_sync, camp_id)


def _incoming_call_row_by_camp_id_sync(camp_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM incoming_calls WHERE camp_id = %s", (camp_id,)).fetchone()
    return dict(row) if row else None


async def finalize_incoming_call_record(
    camp_id: str,
    log_id: str,
    duration_sec: Optional[float],
    analysis: dict[str, Any],
) -> None:
    return await asyncio.to_thread(_finalize_incoming_call_record_sync, camp_id, log_id, duration_sec, analysis)


def _finalize_incoming_call_record_sync(
    camp_id: str,
    log_id: str,
    duration_sec: Optional[float],
    analysis: dict[str, Any],
) -> None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, status, log_id FROM incoming_calls WHERE camp_id = %s",
        (camp_id,),
    ).fetchone()
    if not row:
        return
    # The hangup webhook may already have flipped the row to 'completed'
    # before the WS finalize runs. When that happens, still record the
    # log_id (needed for transcript/recording lookup) but never clobber an
    # analysis that the post-call analyzer wrote.
    if (row["status"] or "") == "completed":
        if (log_id or "").strip() and not (row.get("log_id") or "").strip():
            conn.execute(
                f"UPDATE incoming_calls SET log_id = %s, updated_at = {_NOW_SQL} WHERE camp_id = %s",
                (log_id, camp_id),
            )
            conn.commit()
        return
    aj = json.dumps(analysis, ensure_ascii=False)
    conf = analysis.get("emotion_confidence")
    try:
        conf_f = float(conf) if conf is not None and str(conf).strip() != "" else None
    except (TypeError, ValueError):
        conf_f = None
    conn.execute(
        f"""
        UPDATE incoming_calls SET
            log_id = %s,
            status = 'completed',
            ended_at = {_NOW_SQL},
            duration_sec = %s,
            disposition = %s,
            summary = %s,
            next_steps = %s,
            emotion_label = %s,
            emotion_rationale = %s,
            emotion_confidence = %s,
            analysis_json = %s,
            updated_at = {_NOW_SQL}
        WHERE camp_id = %s
        """,
        (
            log_id or "",
            duration_sec,
            str(analysis.get("disposition") or ""),
            str(analysis.get("summary") or ""),
            str(analysis.get("next_steps") or ""),
            str(analysis.get("emotion_label") or ""),
            str(analysis.get("emotion_rationale") or ""),
            conf_f,
            aj,
            camp_id,
        ),
    )
    conn.commit()


async def update_incoming_call_analysis_by_id(call_id: int, analysis: dict[str, Any]) -> bool:
    return await asyncio.to_thread(_update_incoming_call_analysis_by_id_sync, call_id, analysis)


def _update_incoming_call_analysis_by_id_sync(call_id: int, analysis: dict[str, Any]) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT id FROM incoming_calls WHERE id = %s", (int(call_id),)).fetchone()
    if not row:
        return False
    aj = json.dumps(analysis, ensure_ascii=False)
    conf = analysis.get("emotion_confidence")
    try:
        conf_f = float(conf) if conf is not None and str(conf).strip() != "" else None
    except (TypeError, ValueError):
        conf_f = None
    conn.execute(
        f"""
        UPDATE incoming_calls SET
            disposition = %s,
            summary = %s,
            next_steps = %s,
            emotion_label = %s,
            emotion_rationale = %s,
            emotion_confidence = %s,
            analysis_json = %s,
            updated_at = {_NOW_SQL}
        WHERE id = %s
        """,
        (
            str(analysis.get("disposition") or ""),
            str(analysis.get("summary") or ""),
            str(analysis.get("next_steps") or ""),
            str(analysis.get("emotion_label") or ""),
            str(analysis.get("emotion_rationale") or ""),
            conf_f,
            aj,
            int(call_id),
        ),
    )
    conn.commit()
    return True


async def get_incoming_call_by_id(call_id: int) -> Optional[dict]:
    return await asyncio.to_thread(_get_incoming_call_by_id_sync, call_id)


def _get_incoming_call_by_id_sync(call_id: int) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM incoming_calls WHERE id = %s", (int(call_id),)).fetchone()
    return dict(row) if row else None


async def list_recent_incoming_calls(role: str, limit: int = 15) -> list[dict]:
    return await asyncio.to_thread(_list_recent_incoming_calls_sync, role, limit)


def _list_recent_incoming_calls_sync(role: str, limit: int = 15) -> list[dict]:
    conn = _get_conn()
    lim = max(1, min(int(limit), 5000))
    rows = conn.execute(
        """
        SELECT * FROM incoming_calls WHERE role = %s
        ORDER BY id DESC LIMIT %s
        """,
        (role, lim),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Helpers ---

def _row_to_dict(row) -> dict:
    out = {key: row[key] for key in row.keys()}
    # Decode the leads.extra JSON blob so callers get a normal dict and can
    # use it as ``lead["extra"]["rfq_subject"]`` without re-parsing.
    raw = out.get("extra")
    if raw is not None and isinstance(raw, str):
        if raw.strip():
            try:
                parsed = json.loads(raw)
                out["extra"] = parsed if isinstance(parsed, dict) else {}
            except Exception:
                out["extra"] = {}
        else:
            out["extra"] = {}
    return out


def migrate_from_json(data_dir: Path = None) -> dict:
    """One-time migration from JSON files to the database. Returns migration summary."""
    base = data_dir or Path(__file__).resolve().parent.parent / "data"
    migrated = {"roles": 0, "leads": 0, "agents": 0}

    # Migrate role states
    for role in (
        "maruti",
    ):
        json_path = base / role / "state.json"
        if json_path.exists():
            try:
                with open(json_path) as f:
                    data = json.load(f)
                _save_role_state_sync(
                    role,
                    prompt=data.get("prompt", ""),
                    rag=data.get("rag", ""),
                    vobiz_config=data.get("vobiz", {}),
                    delay_sec=data.get("delay_sec", default_inter_call_gap_sec(role)),
                )
                migrated["roles"] += 1
            except Exception as e:
                logger.warning(f"Failed to migrate role state for {role}: {e}")

    # Migrate sandbox agents
    agents_json = Path(__file__).resolve().parent.parent / "sandbox" / "agents.json"
    if agents_json.exists():
        try:
            with open(agents_json) as f:
                agents = json.load(f)
            for agent in agents:
                agent_id = _create_agent_sync(
                    name=agent.get("name", "Unnamed"),
                    prompt=agent.get("prompt", ""),
                    voice=agent.get("voice", "Puck"),
                )
                for kf in agent.get("knowledge_files", []):
                    _add_agent_knowledge_file_sync(
                        agent_id,
                        kf.get("file_id", "unknown"),
                        kf.get("filename", "unknown"),
                        kf.get("extracted_text", ""),
                    )
                for lead in agent.get("leads", []):
                    _add_agent_lead_sync(agent_id, lead)
                migrated["agents"] += 1
        except Exception as e:
            logger.warning(f"Failed to migrate agents: {e}")

    logger.info(f"Migration complete: {migrated}")
    return migrated


# --- Callback batch processing ---

async def get_pending_callbacks(role: str, limit: int = 50) -> list[dict]:
    return await asyncio.to_thread(_get_pending_callbacks_sync, role, limit)

def _get_pending_callbacks_sync(role: str, limit: int = 50) -> list[dict]:
    """Return leads with status 'callback_scheduled' that are due for callback."""
    conn = _get_conn()
    r = (role or "sales_1").strip().lower()
    now = time.time()
    rows = conn.execute(
        """
        SELECT * FROM leads
        WHERE role = %s AND status = 'callback_scheduled'
          AND NULLIF(analysis::json->>'callback_reminder_epoch', '')::DOUBLE PRECISION <= %s
        ORDER BY created_at ASC LIMIT %s
        """,
        (r, now, int(limit)),
    ).fetchall()
    results = []
    for row in rows:
        d = _row_to_dict(row)
        analysis = d.get("analysis", {})
        if isinstance(analysis, str):
            try:
                analysis = json.loads(analysis)
            except Exception:
                analysis = {}
        d["from_phone"] = d.get("phone", "")
        d["matched_name"] = d.get("name", "")
        d["matched_company"] = d.get("company", "")
        results.append(d)
    return results


async def mark_callback_processed(callback_id: int, role: str) -> None:
    return await asyncio.to_thread(_mark_callback_processed_sync, callback_id, role)

def _mark_callback_processed_sync(callback_id: int, role: str) -> None:
    """Mark a callback lead as completed after processing."""
    conn = _get_conn()
    conn.execute(
        f"UPDATE leads SET status = 'callback_completed', updated_at = {_NOW_SQL} WHERE id = %s AND role = %s",
        (int(callback_id), (role or "sales_1").strip().lower()),
    )
    conn.commit()
    _invalidate_state_cache()


async def mark_callback_calling(callback_id: int, role: str) -> None:
    return await asyncio.to_thread(_mark_callback_calling_sync, callback_id, role)

def _mark_callback_calling_sync(callback_id: int, role: str) -> None:
    """Mark a callback lead as actively being called to prevent duplicate attempts."""
    conn = _get_conn()
    conn.execute(
        f"UPDATE leads SET status = 'callback_calling', updated_at = {_NOW_SQL} WHERE id = %s AND role = %s",
        (int(callback_id), (role or "sales_1").strip().lower()),
    )
    conn.commit()
    _invalidate_state_cache()


# --- Agent-Scheduled Callbacks ---

_SCHEDULED_CALLBACK_COLS = (
    "id, role, phone, name, lead_id, scheduled_at, status, error, "
    "disposition, summary, rating, next_action, analysis_json, "
    "created_at, updated_at"
)


async def add_scheduled_callback(
    role: str,
    phone: str,
    name: str = "",
    scheduled_at: float = 0,
    lead_id: int | None = None,
) -> int:
    return await asyncio.to_thread(
        _add_scheduled_callback_sync, role, phone, name, scheduled_at, lead_id
    )


def _add_scheduled_callback_sync(
    role: str,
    phone: str,
    name: str = "",
    scheduled_at: float = 0,
    lead_id: int | None = None,
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO scheduled_callbacks (role, phone, name, lead_id, scheduled_at, status) "
        "VALUES (%s, %s, %s, %s, %s, 'scheduled') RETURNING id",
        (
            (role or "sales_1").strip().lower(),
            phone,
            (name or "").strip(),
            lead_id,
            float(scheduled_at),
        ),
    )
    conn.commit()
    _invalidate_state_cache()
    return int(cur.fetchone()["id"])


async def has_pending_callback_for_phone(role: str, phone: str) -> bool:
    return await asyncio.to_thread(_has_pending_callback_for_phone_sync, role, phone)


def _has_pending_callback_for_phone_sync(role: str, phone: str) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM scheduled_callbacks WHERE role = %s AND phone = %s AND status = 'scheduled' LIMIT 1",
        (role.strip().lower(), phone.strip()),
    ).fetchone()
    return bool(row)



async def cancel_scheduled_callbacks_for_lead(lead_id: int) -> int:
    return await asyncio.to_thread(_cancel_scheduled_callbacks_for_lead_sync, lead_id)


def _cancel_scheduled_callbacks_for_lead_sync(lead_id: int) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "DELETE FROM scheduled_callbacks WHERE lead_id = %s AND status = 'scheduled'",
        (lead_id,),
    )
    conn.commit()
    _invalidate_state_cache()
    return cur.rowcount


async def list_scheduled_callbacks(role: str, limit: int = 100) -> list[dict]:
    return await asyncio.to_thread(_list_scheduled_callbacks_sync, role, limit)


def _list_scheduled_callbacks_sync(role: str, limit: int = 100) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_SCHEDULED_CALLBACK_COLS} FROM scheduled_callbacks "
        "WHERE role = %s ORDER BY CASE status "
        "    WHEN 'scheduled' THEN 0 "
        "    WHEN 'queued'    THEN 1 "
        "    WHEN 'calling'   THEN 2 "
        "    ELSE 3 END, "
        "scheduled_at ASC LIMIT %s",
        ((role or "sales_1").strip().lower(), int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


async def get_due_scheduled_callbacks(role: str, now_epoch: float) -> list[dict]:
    return await asyncio.to_thread(_get_due_scheduled_callbacks_sync, role, now_epoch)


def _get_due_scheduled_callbacks_sync(role: str, now_epoch: float) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_SCHEDULED_CALLBACK_COLS} FROM scheduled_callbacks "
        "WHERE role = %s AND status = 'scheduled' AND scheduled_at <= %s "
        "ORDER BY scheduled_at ASC LIMIT 10",
        ((role or "sales_1").strip().lower(), float(now_epoch)),
    ).fetchall()
    return [dict(r) for r in rows]


async def get_queued_scheduled_callbacks(role: str) -> list[dict]:
    return await asyncio.to_thread(_get_queued_scheduled_callbacks_sync, role)


def _get_queued_scheduled_callbacks_sync(role: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_SCHEDULED_CALLBACK_COLS} FROM scheduled_callbacks "
        "WHERE role = %s AND status = 'queued' "
        "ORDER BY scheduled_at ASC LIMIT 10",
        ((role or "sales_1").strip().lower(),),
    ).fetchall()
    return [dict(r) for r in rows]


async def get_next_immediate_callback(
    role: str,
    now_epoch: float,
    modulo: int = None,
    remainder: int = None,
) -> dict | None:
    return await asyncio.to_thread(_get_next_immediate_callback_sync, role, now_epoch, modulo, remainder)


def _get_next_immediate_callback_sync(
    role: str,
    now_epoch: float,
    modulo: int = None,
    remainder: int = None,
) -> dict | None:
    """Return the most urgent callback: either a due scheduled one or a queued one."""
    conn = _get_conn()
    query = f"SELECT {_SCHEDULED_CALLBACK_COLS} FROM scheduled_callbacks WHERE role = %s"
    params = [(role or "sales_1").strip().lower()]
    if modulo is not None and remainder is not None:
        query += " AND COALESCE(lead_id, id) %% %s = %s"
        params.extend([modulo, remainder])
    query += " AND ((status = 'scheduled' AND scheduled_at <= %s) OR status = 'queued') ORDER BY scheduled_at ASC LIMIT 1"
    params.append(float(now_epoch))
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


async def update_scheduled_callback_status(
    callback_id: int, status: str, error: str | None = None
) -> bool:
    return await asyncio.to_thread(
        _update_scheduled_callback_status_sync, callback_id, status, error
    )


def _update_scheduled_callback_status_sync(
    callback_id: int, status: str, error: str | None = None
) -> bool:
    conn = _get_conn()
    if error is not None:
        cur = conn.execute(
            f"UPDATE scheduled_callbacks SET status = %s, error = %s, updated_at = {_NOW_SQL} WHERE id = %s",
            (status, error[:500], int(callback_id)),
        )
    else:
        cur = conn.execute(
            f"UPDATE scheduled_callbacks SET status = %s, updated_at = {_NOW_SQL} WHERE id = %s",
            (status, int(callback_id)),
        )
    conn.commit()
    _invalidate_state_cache()
    return cur.rowcount > 0


async def cancel_scheduled_callback(callback_id: int) -> bool:
    return await asyncio.to_thread(_cancel_scheduled_callback_sync, callback_id)


def _cancel_scheduled_callback_sync(callback_id: int) -> bool:
    """Cancel a callback that hasn't started yet."""
    conn = _get_conn()
    cur = conn.execute(
        f"UPDATE scheduled_callbacks SET status = 'cancelled', updated_at = {_NOW_SQL} "
        "WHERE id = %s AND status IN ('scheduled', 'queued')",
        (int(callback_id),),
    )
    conn.commit()
    _invalidate_state_cache()
    return cur.rowcount > 0


async def get_scheduled_callback(callback_id: int) -> dict | None:
    return await asyncio.to_thread(_get_scheduled_callback_sync, callback_id)


def _get_scheduled_callback_sync(callback_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        f"SELECT {_SCHEDULED_CALLBACK_COLS} FROM scheduled_callbacks WHERE id = %s",
        (int(callback_id),),
    ).fetchone()
    return dict(row) if row else None


# ── WhatsApp dedup ────────────────────────────────────────────────

async def mark_whatsapp_sent(lead_id: int) -> None:
    return await asyncio.to_thread(_mark_whatsapp_sent_sync, lead_id)


def _mark_whatsapp_sent_sync(lead_id: int) -> None:
    conn = _get_conn()
    conn.execute(
        f"UPDATE leads SET whatsapp_sent = 1, whatsapp_sent_at = %s, updated_at = {_NOW_SQL} WHERE id = %s",
        (time.time(), int(lead_id)),
    )
    conn.commit()


# ── Failed Call Retry and WhatsApp Reminder Helpers ──────────────────

async def update_lead_retry_state(lead_id: int, status: str, extra: dict, analysis: dict) -> None:
    return await asyncio.to_thread(_update_lead_retry_state_sync, lead_id, status, extra, analysis)


def _update_lead_retry_state_sync(lead_id: int, status: str, extra: dict, analysis: dict) -> None:
    conn = _get_conn()

    # Capture old status for DashboardState notification
    _old_status = "dialing"
    try:
        _row = conn.execute("SELECT role, status FROM leads WHERE id = %s", (lead_id,)).fetchone()
        if _row:
            _old_status = str(_row["status"] or "dialing").strip().lower()
    except Exception:
        pass

    conn.execute(
        f"UPDATE leads SET status = %s, extra = %s, analysis = %s, updated_at = {_NOW_SQL} WHERE id = %s",
        (status, json.dumps(extra), json.dumps(analysis), int(lead_id))
    )

    # Cancel pending scheduled retries if resolved
    s_lower = (status or "").lower()
    if s_lower in ("completed", "not_interested", "callback_completed", "callback_scheduled", "site_visit", "site_visited", "interested"):
        conn.execute(
            "DELETE FROM scheduled_callbacks WHERE lead_id = %s AND status = 'scheduled'",
            (lead_id,)
        )

    # Also persist duration to the dedicated column so CSV exports can read it directly
    dur = analysis.get("duration")
    if dur is not None:
        try:
            conn.execute(
                "UPDATE leads SET duration_sec = %s WHERE id = %s",
                (round(float(dur), 1), int(lead_id))
            )
        except Exception:
            pass
    conn.commit()
    _invalidate_state_cache()

    # Notify materialized dashboard state
    try:
        from core.dashboard_state import notify_lead_updated
        _role = "sellers"
        try:
            _rrow = conn.execute("SELECT role FROM leads WHERE id = %s", (lead_id,)).fetchone()
            if _rrow:
                _role = str(_rrow["role"])
        except Exception:
            pass
        notify_lead_updated(
            role=_role, lead_id=lead_id,
            old_status=_old_status, new_status=str(status or "").strip().lower(),
            analysis_raw=analysis,
        )
    except Exception:
        pass



async def get_due_whatsapp_reminders(before_epoch: float) -> list[dict]:
    return await asyncio.to_thread(_get_due_whatsapp_reminders_sync, before_epoch)


def _get_due_whatsapp_reminders_sync(before_epoch: float) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT id, role, name, phone, status, extra, analysis FROM leads
        WHERE whatsapp_sent = 1
          AND whatsapp_reminder_sent = 0
          AND whatsapp_sent_at IS NOT NULL
          AND whatsapp_sent_at <= %s
          AND (status != 'not_interested' OR role IN ('sales_1', 'sales_2'))
        """,
        (float(before_epoch),),
    ).fetchall()
    return [dict(r) for r in rows]


async def mark_whatsapp_reminder_sent(lead_id: int) -> None:
    return await asyncio.to_thread(_mark_whatsapp_reminder_sent_sync, lead_id)


def _mark_whatsapp_reminder_sent_sync(lead_id: int) -> None:
    conn = _get_conn()
    conn.execute(
        f"UPDATE leads SET whatsapp_reminder_sent = 1, updated_at = {_NOW_SQL} WHERE id = %s",
        (int(lead_id),),
    )
    conn.commit()
    _invalidate_state_cache()


async def get_lead_whatsapp_sent(lead_id: int) -> bool:
    return await asyncio.to_thread(_get_lead_whatsapp_sent_sync, lead_id)


def _get_lead_whatsapp_sent_sync(lead_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT whatsapp_sent FROM leads WHERE id = %s",
        (int(lead_id),),
    ).fetchone()
    return bool(row and row["whatsapp_sent"])


# ── Email dedup ───────────────────────────────────────────────────

async def mark_email_sent(lead_id: int) -> None:
    return await asyncio.to_thread(_mark_email_sent_sync, lead_id)


def _mark_email_sent_sync(lead_id: int) -> None:
    conn = _get_conn()
    conn.execute(
        f"UPDATE leads SET email_sent = 1, email_sent_at = %s, updated_at = {_NOW_SQL} WHERE id = %s",
        (time.time(), int(lead_id)),
    )
    conn.commit()


async def update_lead_email_sent_in_db(lead_id: int, email: str) -> None:
    return await asyncio.to_thread(_update_lead_email_sent_in_db_sync, lead_id, email)


def _update_lead_email_sent_in_db_sync(lead_id: int, email: str) -> None:
    conn = _get_conn()
    row = conn.execute("SELECT extra FROM leads WHERE id = %s", (int(lead_id),)).fetchone()
    extra_data = {}
    if row and row["extra"]:
        try:
            import json
            extra_data = json.loads(row["extra"])
        except Exception:
            pass
    extra_data["_email_sent"] = True
    extra_data["email"] = email

    import json
    conn.execute(
        f"UPDATE leads SET email = %s, email_sent = 1, email_sent_at = %s, extra = %s, updated_at = {_NOW_SQL} WHERE id = %s",
        (email, time.time(), json.dumps(extra_data), int(lead_id)),
    )
    conn.commit()


async def get_lead_email_sent(lead_id: int) -> bool:
    return await asyncio.to_thread(_get_lead_email_sent_sync, lead_id)


def _get_lead_email_sent_sync(lead_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT email_sent FROM leads WHERE id = %s",
        (int(lead_id),),
    ).fetchone()
    return bool(row and row["email_sent"])


# ── Scheduled callback analysis ───────────────────────────────────

async def update_scheduled_callback_analysis(
    callback_id: int,
    disposition: str = "",
    summary: str = "",
    rating: float | None = None,
    next_action: dict | None = None,
    analysis_json: dict | None = None,
) -> None:
    return await asyncio.to_thread(
        _update_scheduled_callback_analysis_sync,
        callback_id, disposition, summary, rating, next_action, analysis_json,
    )


def _update_scheduled_callback_analysis_sync(
    callback_id: int,
    disposition: str = "",
    summary: str = "",
    rating: float | None = None,
    next_action: dict | None = None,
    analysis_json: dict | None = None,
) -> None:
    import json as _json
    conn = _get_conn()
    conn.execute(
        f"UPDATE scheduled_callbacks SET "
        f"disposition = %s, summary = %s, rating = %s, "
        f"next_action = %s, analysis_json = %s, updated_at = {_NOW_SQL} "
        "WHERE id = %s",
        (
            (disposition or "")[:500],
            (summary or "")[:2000],
            rating,
            _json.dumps(next_action or {}),
            _json.dumps(analysis_json or {}),
            int(callback_id),
        ),
    )
    conn.commit()
    _invalidate_state_cache()


# ── Call Attempt History CRUD ─────────────────────────────────────

_CA_COLS = "id, lead_id, role, attempt_number, log_id, status, disposition, summary, rating, duration_sec, callback_scheduled_at, error, created_at"

_CA_COLS_LIST = "id, lead_id, role, attempt_number, log_id, status, disposition, summary, rating, duration_sec, callback_scheduled_at, error, created_at"


async def add_call_attempt(
    lead_id: int,
    role: str,
    attempt_number: int = 1,
    log_id: str = "",
    status: str = "completed",
    disposition: str = "",
    summary: str = "",
    rating: float | None = None,
    duration_sec: float | None = None,
    callback_scheduled_at: float | None = None,
    error: str = "",
) -> int:
    return await asyncio.to_thread(
        _add_call_attempt_sync,
        lead_id, role, attempt_number, log_id, status,
        disposition, summary, rating, duration_sec, callback_scheduled_at, error,
    )


def _add_call_attempt_sync(
    lead_id: int,
    role: str,
    attempt_number: int = 1,
    log_id: str = "",
    status: str = "completed",
    disposition: str = "",
    summary: str = "",
    rating: float | None = None,
    duration_sec: float | None = None,
    callback_scheduled_at: float | None = None,
    error: str = "",
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO call_attempts "
        "(lead_id, role, attempt_number, log_id, status, disposition, summary, rating, duration_sec, callback_scheduled_at, error) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            int(lead_id),
            role,
            int(attempt_number),
            (log_id or "")[:200],
            (status or "completed")[:50],
            (disposition or "")[:200],
            (summary or "")[:3000],
            rating,
            duration_sec,
            callback_scheduled_at,
            (error or "")[:500],
        ),
    )
    conn.commit()
    _invalidate_state_cache()
    return int(cur.fetchone()["id"])


async def get_call_attempts(lead_id: int) -> list[dict]:
    return await asyncio.to_thread(_get_call_attempts_sync, lead_id)


def _get_call_attempts_sync(lead_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_CA_COLS_LIST} FROM call_attempts WHERE lead_id = %s ORDER BY attempt_number ASC, id ASC",
        (int(lead_id),),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Virtual Meet CRUD ───────────────────────────────────────────

_VM_COLS = "id, lead_id, role, meet_date, meet_time, notes, status, rescheduled_from_id, created_at, updated_at"


async def add_virtual_meet(
    lead_id: int,
    role: str,
    meet_date: str,
    meet_time: str,
    notes: str = "",
) -> int:
    return await asyncio.to_thread(_add_virtual_meet_sync, lead_id, role, meet_date, meet_time, notes)


def _add_virtual_meet_sync(
    lead_id: int,
    role: str,
    meet_date: str,
    meet_time: str,
    notes: str = "",
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO virtual_meets (lead_id, role, meet_date, meet_time, notes, status) "
        "VALUES (%s, %s, %s, %s, %s, 'scheduled') RETURNING id",
        (int(lead_id), (role or "").strip().lower(), meet_date.strip(), meet_time.strip(), (notes or "").strip()),
    )
    conn.commit()
    return int(cur.fetchone()["id"])


async def get_virtual_meet_for_lead(lead_id: int) -> dict | None:
    return await asyncio.to_thread(_get_virtual_meet_for_lead_sync, lead_id)


def _get_virtual_meet_for_lead_sync(lead_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        f"SELECT {_VM_COLS} FROM virtual_meets WHERE lead_id = %s ORDER BY id DESC LIMIT 1",
        (int(lead_id),),
    ).fetchone()
    return dict(row) if row else None


async def reschedule_virtual_meet(
    meet_id: int,
    new_date: str,
    new_time: str,
    new_notes: str = "",
) -> bool:
    return await asyncio.to_thread(_reschedule_virtual_meet_sync, meet_id, new_date, new_time, new_notes)


def _reschedule_virtual_meet_sync(
    meet_id: int,
    new_date: str,
    new_time: str,
    new_notes: str = "",
) -> bool:
    """Create a new virtual_meet row with status='scheduled' and link back to the old one as rescheduled_from_id."""
    conn = _get_conn()
    old = conn.execute("SELECT * FROM virtual_meets WHERE id = %s", (int(meet_id),)).fetchone()
    if not old:
        return False
    conn.execute(
        f"UPDATE virtual_meets SET status = 'rescheduled', updated_at = {_NOW_SQL} WHERE id = %s",
        (int(meet_id),),
    )
    cur = conn.execute(
        "INSERT INTO virtual_meets (lead_id, role, meet_date, meet_time, notes, status, rescheduled_from_id) "
        "VALUES (%s, %s, %s, %s, %s, 'scheduled', %s)",
        (old["lead_id"], old["role"], new_date.strip(), new_time.strip(), (new_notes or "").strip(), int(meet_id)),
    )
    conn.commit()
    return cur.rowcount > 0


async def cancel_virtual_meet(meet_id: int) -> bool:
    return await asyncio.to_thread(_cancel_virtual_meet_sync, meet_id)


def _cancel_virtual_meet_sync(meet_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        f"UPDATE virtual_meets SET status = 'cancelled', updated_at = {_NOW_SQL} WHERE id = %s",
        (int(meet_id),),
    )
    conn.commit()
    return cur.rowcount > 0


async def is_duplicate_lead(role: str, phone: str, lead_id: int) -> bool:
    return await asyncio.to_thread(_is_duplicate_lead_sync, role, phone, lead_id)


def _is_duplicate_lead_sync(role: str, phone: str, lead_id: int) -> bool:
    digits = "".join(c for c in str(phone) if c.isdigit())
    if len(digits) < 10:
        return False
    tail = digits[-10:]
    conn = _get_conn()

    # Check if any other lead with the same last 10 digits in the same role has been processed or is active
    rows = conn.execute(
        """
        SELECT id, status FROM leads 
        WHERE role = %s 
          AND phone LIKE %s 
          AND id != %s
        """,
        ((role or "").strip().lower(), f"%{tail}", int(lead_id))
    ).fetchall()

    for r in rows:
        other_id = r["id"]
        other_status = r["status"]
        if other_status in ('dialing', 'completed', 'failed', 'not_interested', 'callback_scheduled', 'site_visit', 'callback_completed'):
            return True
        if other_status == 'pending' and other_id < lead_id:
            return True

    return False


async def get_daily_call_count_for_phone(phone_number: str) -> int:
    return await asyncio.to_thread(_get_daily_call_count_for_phone_sync, phone_number)


def _get_daily_call_count_for_phone_sync(phone_number: str) -> int:
    import time
    from datetime import datetime
    import zoneinfo

    tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(tz)
    midnight_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_epoch = midnight_ist.timestamp()

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS c FROM leads WHERE outbound_phone = %s AND start_time >= %s",
        (phone_number, midnight_epoch)
    )
    count = cur.fetchone()["c"]
    return int(count or 0)


async def get_daily_call_count_for_source(role: str, source_name: str) -> int:
    return await asyncio.to_thread(_get_daily_call_count_for_source_sync, role, source_name)


def _get_daily_call_count_for_source_sync(role: str, source_name: str) -> int:
    from datetime import datetime
    import zoneinfo

    tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(tz)
    midnight_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_epoch = midnight_ist.timestamp()

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT COUNT(*) AS c FROM leads
           WHERE role = %s
             AND extra::json->>'upload_source' = %s
             AND start_time >= %s""",
        (role, source_name, midnight_epoch),
    )
    return int(cur.fetchone()["c"] or 0)


async def get_campaign_sources(role: str, paused_sources: list) -> list[dict]:
    return await asyncio.to_thread(_get_campaign_sources_sync, role, paused_sources)


def _get_campaign_sources_sync(role: str, paused_sources: list) -> list[dict]:
    conn = _get_conn()
    query = """
        SELECT 
            extra::json->>'upload_source' as src_name,
            COUNT(*) as total,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN status != 'pending' THEN 1 ELSE 0 END) as called,
            SUM(CASE WHEN analysis::json->>'disposition' ILIKE '%%Interested%%' OR COALESCE(analysis::json->>'outcome_from_transcript' IN ('1', 'true'), FALSE) THEN 1 ELSE 0 END) as interested,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
        FROM leads
        WHERE role = %s AND extra::json->>'upload_source' IS NOT NULL AND extra::json->>'upload_source' != ''
        GROUP BY src_name
    """
    rows = conn.execute(query, [role]).fetchall()
    result = []
    for r in rows:
        src_name = r["src_name"]
        stats = {
            "name": src_name,
            "total": r["total"],
            "pending": r["pending"],
            "called": r["called"],
            "interested": r["interested"],
            "failed": r["failed"],
            "paused": src_name in paused_sources
        }
        result.append(stats)
    result.sort(key=lambda x: x["name"])
    return result


async def get_recent_call_outcomes_for_phone(phone_number: str, since_epoch: float) -> list[dict]:
    return await asyncio.to_thread(_get_recent_call_outcomes_for_phone_sync, phone_number, since_epoch)


def _get_recent_call_outcomes_for_phone_sync(phone_number: str, since_epoch: float) -> list[dict]:
    from core.phone_norm import norm_phone_str
    norm = norm_phone_str(phone_number)

    conn = _get_conn()
    query = """
        SELECT status, error, analysis, extra 
        FROM leads 
        WHERE outbound_phone = %s AND start_time >= %s
        ORDER BY start_time DESC
    """
    rows = conn.execute(query, [norm, since_epoch]).fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_recent_call_outcomes_for_role(role: str, since_epoch: float) -> list[dict]:
    return await asyncio.to_thread(_get_recent_call_outcomes_for_role_sync, role, since_epoch)


def _get_recent_call_outcomes_for_role_sync(role: str, since_epoch: float) -> list[dict]:
    from core.state import normalize_console_role
    r = normalize_console_role(role)

    conn = _get_conn()
    query = """
        SELECT status, error, analysis, extra 
        FROM leads 
        WHERE role = %s AND start_time >= %s
        ORDER BY start_time DESC
    """
    rows = conn.execute(query, [r, since_epoch]).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_paused_sources_sync(role: str) -> list[str]:
    """Return paused sources for role — reads from in-memory cache first."""
    with _PAUSED_SOURCES_LOCK:
        if role in _PAUSED_SOURCES:
            return list(_PAUSED_SOURCES[role])
    # Not in memory yet — load from the database once and populate cache
    conn = _get_conn()
    key = f"paused_sources:{role}"
    row = conn.execute("SELECT value FROM app_meta WHERE key = %s", (key,)).fetchone()
    result: list[str] = []
    if row and row["value"]:
        try:
            val = json.loads(row["value"])
            if isinstance(val, list):
                result = [str(s) for s in val]
        except Exception:
            result = [s.strip() for s in row["value"].split(",") if s.strip()]
    with _PAUSED_SOURCES_LOCK:
        _PAUSED_SOURCES[role] = result
    return list(result)


async def get_paused_sources(role: str) -> list[str]:
    """Async wrapper — reads from in-memory cache (no thread dispatch needed)."""
    return get_paused_sources_sync(role)


def set_paused_sources_sync(role: str, sources: list[str]) -> None:
    """Update paused sources in memory and persist to the database."""
    # 1. Update in-memory cache immediately so all threads see it right away
    with _PAUSED_SOURCES_LOCK:
        _PAUSED_SOURCES[role] = list(sources)
    # 2. Persist for restart durability
    conn = _get_conn()
    key = f"paused_sources:{role}"
    val_str = json.dumps(sources)
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, val_str)
    )
    conn.commit()


async def set_paused_sources(role: str, sources: list[str]) -> None:
    """Async wrapper — updates in-memory cache synchronously then persists."""
    set_paused_sources_sync(role, sources)


async def claim_next_immediate_callback(role: str, now_epoch: float) -> dict | None:
    return await asyncio.to_thread(_claim_next_immediate_callback_sync, role, now_epoch)


def _claim_next_immediate_callback_sync(role: str, now_epoch: float) -> dict | None:
    """Atomically fetch and claim the next due scheduled callback for a role."""
    conn = _get_conn()
    with conn:
        query = f"""
            SELECT {_SCHEDULED_CALLBACK_COLS} FROM scheduled_callbacks 
            WHERE role = %s AND ((status = 'scheduled' AND scheduled_at <= %s) OR status = 'queued')
            ORDER BY scheduled_at ASC LIMIT 1
        """
        row = conn.execute(query, ((role or "").strip().lower(), float(now_epoch))).fetchone()
        if not row:
            return None
        cb = dict(row)
        # Update status to 'calling' atomically to claim it
        conn.execute(
            f"UPDATE scheduled_callbacks SET status = 'calling', updated_at = {_NOW_SQL} WHERE id = %s",
            (cb["id"],)
        )
        return cb

