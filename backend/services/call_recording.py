"""Call recording lookup."""

from __future__ import annotations

from pathlib import Path


def resolve_session_recording_path(log_id: str):
    """Resolve a saved call-recording file for a call.

    Recordings are persisted by the WS bridge to
    ``<CAMPAIGN_RECORDING_DIR>/<role>/<camp_id>.mp3`` (preferred) or ``.wav``
    for campaign calls, and ``<CALL_RECORDING_DIR>/<role>/manual/…`` for
    manual/incoming calls, where ``log_id`` == ``camp_id``. Legacy recordings
    may sit directly under ``<role>/``. Fast single-level globs run first; a
    full recursive scan stays as the last fallback for historical or stale log
    ids. Returns a Path or None.
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

    exact_mp3 = base / f"{log_id}.mp3"
    if exact_mp3.is_file():
        return exact_mp3
    exact = base / f"{log_id}.wav"
    if exact.is_file():
        return exact

    # Campaign tree first (``CAMPAIGN_RECORDING_DIR``, default backend/campaign).
    try:
        from config import settings as _settings

        camp_base = Path(getattr(_settings, "campaign_recording_dir", "") or "")
    except Exception:
        camp_base = None
    if camp_base and camp_base.is_dir():
        try:
            for pattern in (f"*/{log_id}.mp3", f"*/{log_id}.wav", f"{log_id}.mp3", f"{log_id}.wav"):
                hits = sorted(camp_base.glob(pattern))
                if hits:
                    return hits[0]
        except Exception:
            pass

    # Known subfolder layout next (cheap, single-level glob) — prefer MP3.
    try:
        for pattern in (
            f"*/campaign/{log_id}.mp3",
            f"*/manual/{log_id}.mp3",
            f"*/campaign/{log_id}.wav",
            f"*/manual/{log_id}.wav",
            f"*/{log_id}.mp3",
            f"*/{log_id}.wav",
        ):
            hits = sorted(base.glob(pattern))
            if hits:
                return hits[0]
    except Exception:
        pass

    # Recursive scan as the final fallback so historical/stale log ids still resolve.
    try:
        mp3_matches = sorted(base.rglob(f"{log_id}.mp3"))
        if mp3_matches:
            return mp3_matches[0]
        wav_matches = sorted(base.rglob(f"{log_id}.wav"))
        if wav_matches:
            return wav_matches[0]
    except Exception:
        pass
    return None