"""Call recording lookup."""

from __future__ import annotations

from pathlib import Path


def resolve_session_recording_path(log_id: str):
    """Resolve a saved call-recording file for a call.

    Recordings are persisted by the WS bridge to
    ``<CALL_RECORDING_DIR>/<role>/<camp_id>.mp3`` (preferred) or
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

    exact_mp3 = base / f"{log_id}.mp3"
    if exact_mp3.is_file():
        return exact_mp3
    exact = base / f"{log_id}.wav"
    if exact.is_file():
        return exact

    # Recursive scan (by role subdirs) as a fallback — prefer MP3 over WAV.
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