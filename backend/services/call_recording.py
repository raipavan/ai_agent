"""Call recording lookup."""

from __future__ import annotations

from pathlib import Path


def resolve_session_recording_path(log_id: str):
    """Resolve a saved call-recording WAV for a call.

    Recordings are persisted by the WS bridge to
    ``<CALL_RECORDING_DIR>/<role>/<camp_id>.wav`` where ``log_id`` == ``camp_id``
    for WS-bridged calls. Fall back to a case-insensitive scan of the recording
    dir so historical/stale log ids can still resolve. Returns a Path or None.
    """
    try:
        from config import settings

        base = Path(settings.call_recording_dir)
    except Exception:
        base = Path(__file__).resolve().parent.parent / "data" / "call_recordings"

    if not base.is_dir():
        return None

    log_id = (log_id or "").strip()
    if not log_id:
        return None

    exact = base / f"{log_id}.wav"
    if exact.is_file():
        return exact

    # Recursive scan (by role subdirs) as a fallback.
    try:
        for p in base.rglob(f"*{log_id}*"):
            if p.is_file() and p.suffix.lower() in (".wav", ".mp3"):
                return p
    except Exception:
        pass
    return None