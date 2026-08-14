"""In-memory KV cache with TTL for dashboard state.

Reduces repeated DB queries and enrichment when the frontend polls
``/api/campaign/state`` every few seconds.  Cache is invalidated
whenever a lead is updated / added / reset / wiped.

Multi-VPS note: explicit invalidation writes ``last_cache_invalidation_time``
to SQLite so that the *other* VPS process clears its local cache on the next
``get()`` call (within ~2 s). Regular ``set()`` is local only; otherwise each
dashboard poll would invalidate the other VPS and defeat the cache.
"""

import time
from threading import Lock

_cache: dict[str, tuple[float, object]] = {}
_lock = Lock()
_DEFAULT_TTL = 5.0  # seconds — pre-computed materialized state is always fresh
_last_invalidation_time = 0.0


def _write_invalidation_ts_to_db(ts: float) -> None:
    """Persist the invalidation timestamp so other VPS processes pick it up."""
    global _last_invalidation_time
    _lock.acquire()
    try:
        _last_invalidation_time = ts
    finally:
        _lock.release()

    conn = None
    try:
        from core.storage import _get_conn
        conn = _get_conn()
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            ("last_cache_invalidation_time", str(ts)),
        )
        conn.commit()
    except Exception as e:
        from loguru import logger
        logger.error(f"Failed to write cache invalidation TS to DB: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception as re:
                logger.error(f"Failed to rollback in write_invalidation_ts: {re}")


def get(key: str) -> object | None:
    global _last_invalidation_time
    # Cross-VPS invalidation: read the last invalidation timestamp from SQLite.
    db_val = None
    try:
        from core.storage import _get_conn
        conn = _get_conn()
        row = conn.execute("SELECT value FROM app_meta WHERE key = %s", ("last_cache_invalidation_time",)).fetchone()
        if row:
            db_val = float(row["value"])
    except Exception as e:
        from loguru import logger
        logger.error(f"Failed to read cache invalidation TS from DB: {e}")

    _lock.acquire()
    try:
        if db_val is not None and db_val != _last_invalidation_time:
            _last_invalidation_time = db_val
            _cache.clear()

        entry = _cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() > expires_at:
            del _cache[key]
            return None
        return value
    finally:
        _lock.release()


def set(key: str, value: object, ttl: float | None = None) -> None:
    expires_at = time.monotonic() + (ttl if ttl is not None else _DEFAULT_TTL)
    _lock.acquire()
    try:
        _cache[key] = (expires_at, value)
    finally:
        _lock.release()


def delete(key: str) -> None:
    _lock.acquire()
    try:
        _cache.pop(key, None)
    finally:
        _lock.release()


def clear() -> None:
    _lock.acquire()
    try:
        _cache.clear()
    finally:
        _lock.release()
    _write_invalidation_ts_to_db(time.time())


def invalidate_cross_vps() -> None:
    """Signal the other VPS to clear its cache without touching our own local cache.

    Call this when a lead is updated on this process — our own cache is already
    cleared by ``_invalidate_state_cache()`` in storage, but the other process
    still needs the SQLite signal.
    """
    _write_invalidation_ts_to_db(time.time())


def invalidate_role(role: str) -> None:
    """Remove cached entries for a given role (e.g. ``campaign_state:maruti``)."""
    prefix = f"campaign_state:{role}"
    _lock.acquire()
    try:
        for k in list(_cache.keys()):
            if k.startswith(prefix):
                del _cache[k]
    finally:
        _lock.release()
    # Notify other VPS
    _write_invalidation_ts_to_db(time.time())


def invalidate_all() -> None:
    """Remove every cached campaign state entry (all roles)."""
    _lock.acquire()
    try:
        for k in list(_cache.keys()):
            if k.startswith("campaign_state:"):
                del _cache[k]
    finally:
        _lock.release()
    # Notify other VPS
    _write_invalidation_ts_to_db(time.time())



def state_cache_key(role: str) -> str:
    return f"campaign_state:{role}"


def state_set(role: str, value: object, ttl: float | None = None) -> None:
    # Materialized state is always fresh; longer TTL means less cache-miss rebuild
    set(state_cache_key(role), value, ttl if ttl is not None else _DEFAULT_TTL)


def state_get(role: str) -> object | None:
    return get(state_cache_key(role))
