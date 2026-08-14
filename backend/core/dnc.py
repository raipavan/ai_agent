"""DNC (Do Not Call) list backed by PostgreSQL."""

import psycopg2
from loguru import logger


def _dsn() -> str:
    try:
        from config import settings
        return (settings.database_url or "").strip() or "postgresql://postgres:postgres@localhost:5432/vernika"
    except Exception:
        return "postgresql://postgres:postgres@localhost:5432/vernika"


# Kept for backwards compatibility (legacy callers treated this as a file path).
DB_PATH = _dsn()


def init_dnc_table():
    """Ensure the DNC (Do Not Call) table exists in PostgreSQL."""
    try:
        conn = psycopg2.connect(_dsn(), connect_timeout=10)
        try:
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS dnc_list ("
                "phone TEXT PRIMARY KEY, "
                "created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')"
                ")"
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error("Failed to initialize DNC table: {}", e)

def add_to_dnc(phone: str):
    """Add a phone number to the DNC list."""
    if not phone:
        return
    # Normalize phone: extract only digits
    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        return

    init_dnc_table()
    try:
        conn = psycopg2.connect(_dsn(), connect_timeout=10)
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO dnc_list (phone) VALUES (%s) ON CONFLICT (phone) DO NOTHING",
                (phone.strip(),),
            )
            conn.commit()
        finally:
            conn.close()
        logger.info("Added phone to DNC list: {}", phone)
    except Exception as e:
        logger.error("Failed to add to DNC: {}", e)

def is_phone_blocked(phone: str) -> bool:
    """Check if a phone number matches any blocked DNC number or hardcoded list."""
    if not phone:
        return False

    # Extract only digits for suffix comparison
    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        return False

    # 1. Hardcoded blocks (e.g. Chinmay's number)
    if digits.endswith("7204955388"):
        return True

    # 2. Database DNC check
    init_dnc_table()
    try:
        conn = psycopg2.connect(_dsn(), connect_timeout=10)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT phone FROM dnc_list")
            rows = cursor.fetchall()
        finally:
            conn.close()

        for (blocked,) in rows:
            blocked_digits = "".join(c for c in blocked if c.isdigit())
            if blocked_digits and digits.endswith(blocked_digits):
                return True
    except Exception as e:
        logger.error("DNC lookup error: {}", e)

    return False
