"""In-app notification store — Postgres-backed, per role.

Keeps a lightweight ``notifications`` table (created lazily via
``core.storage`` connections) and exposes helpers for listing / marking
read plus ``push_notification`` used by the voice-call event hooks.
"""

from __future__ import annotations

import time

from loguru import logger

# kind -> (material symbol icon name)
_KIND_ICONS = {
    "call": "phone_in_talk",
    "lead": "person_add",
    "campaign": "campaign",
    "whatsapp": "chat",
    "system": "info",
}

# Keep only the latest N rows per role so the table stays small.
_MAX_PER_ROLE = 200


def _ensure_table() -> None:
    from core.storage import _get_conn

    conn = _get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            role TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'info',
            title TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            read INTEGER NOT NULL DEFAULT 0,
            created_epoch DOUBLE PRECISION NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_role ON notifications(role, id DESC)"
    )


def push_notification(role: str, title: str, body: str = "", kind: str = "info") -> int:
    """Insert a notification for ``role``. Returns the new row id (0 on failure)."""
    try:
        from core.storage import _get_conn

        _ensure_table()
        conn = _get_conn()
        r = (role or "").strip().lower()
        cur = conn.execute(
            "INSERT INTO notifications (role, kind, title, body, created_epoch) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (r, (kind or "info").strip().lower(), (title or "").strip(), (body or "").strip(), float(time.time())),
        )
        nid = int(cur.fetchone()["id"])
        # Prune old rows beyond the per-role cap.
        conn.execute(
            """
            DELETE FROM notifications
            WHERE role = %s AND id NOT IN (
                SELECT id FROM notifications WHERE role = %s ORDER BY id DESC LIMIT %s
            )
            """,
            (r, r, int(_MAX_PER_ROLE)),
        )
        return nid
    except Exception as exc:
        logger.warning("push_notification failed for role={!r}: {}", role, exc)
        return 0


def list_notifications(role: str | None = None, limit: int = 50) -> tuple[list[dict], int]:
    """Return (items, unread_count). Items are newest-first."""
    from core.storage import _get_conn

    try:
        _ensure_table()
    except Exception as exc:
        logger.warning("list_notifications: table ensure failed: {}", exc)
        return [], 0

    try:
        conn = _get_conn()
        lim = max(1, min(int(limit), 200))
        if role and str(role).strip():
            rows = conn.execute(
                "SELECT id, role, kind, title, body, read, created_epoch FROM notifications "
                "WHERE role = %s ORDER BY id DESC LIMIT %s",
                (str(role).strip().lower(), lim),
            ).fetchall()
            unread = conn.execute(
                "SELECT COUNT(*) AS c FROM notifications WHERE role = %s AND read = 0",
                (str(role).strip().lower(),),
            ).fetchone()["c"]
        else:
            rows = conn.execute(
                "SELECT id, role, kind, title, body, read, created_epoch FROM notifications "
                "ORDER BY id DESC LIMIT %s",
                (lim,),
            ).fetchall()
            unread = conn.execute(
                "SELECT COUNT(*) AS c FROM notifications WHERE read = 0"
            ).fetchone()["c"]
    except Exception as exc:
        logger.warning("list_notifications failed: {}", exc)
        return [], 0

    items: list[dict] = []
    for row in rows:
        epoch = float(row["created_epoch"] or 0)
        try:
            from datetime import datetime, timezone, timedelta

            ts = datetime.fromtimestamp(epoch, tz=timezone(timedelta(hours=5, minutes=30)))
            time_str = ts.strftime("%H:%M")
        except Exception:
            time_str = ""
        items.append(
            {
                "id": int(row["id"]),
                "role": str(row["role"] or ""),
                "type": str(row["kind"] or "info"),
                "icon": _KIND_ICONS.get(str(row["kind"] or "info"), "info"),
                "title": str(row["title"] or ""),
                "desc": str(row["body"] or ""),
                "read": bool(row["read"]),
                "epoch": epoch,
                "time": time_str,
            }
        )
    return items, int(unread)


def mark_read(notif_id: int) -> bool:
    from core.storage import _get_conn

    try:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE notifications SET read = 1 WHERE id = %s", (int(notif_id),)
        )
        return bool(cur.rowcount)
    except Exception as exc:
        logger.warning("mark_read failed for id={}: {}", notif_id, exc)
        return False


def mark_all_read(role: str | None = None) -> int:
    from core.storage import _get_conn

    try:
        conn = _get_conn()
        if role and str(role).strip():
            cur = conn.execute(
                "UPDATE notifications SET read = 1 WHERE role = %s AND read = 0",
                (str(role).strip().lower(),),
            )
        else:
            cur = conn.execute("UPDATE notifications SET read = 1 WHERE read = 0")
        return int(cur.rowcount or 0)
    except Exception as exc:
        logger.warning("mark_all_read failed: {}", exc)
        return 0
