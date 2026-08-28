"""Call recording: capture and lookup."""

from __future__ import annotations

import audioop
import logging
import struct
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from config import settings as _settings
    _RECORDING_BASE = Path(getattr(_settings, "call_recording_dir", "") or "")
except Exception:
    _RECORDING_BASE = None


class CallRecorder:
    """Capture inbound (caller) and outbound (agent) 16 kHz mono s16le PCM,
    then mix + compress to MP3 on close()."""

    def __init__(self, role: str, session_id: str):
        if not _RECORDING_BASE:
            self._dir = Path(__file__).resolve().parent.parent / "data" / "call_recordings"
        else:
            self._dir = _RECORDING_BASE
        self._role = role or "unknown"
        self._session_id = session_id or "unknown"
        self._in_buffer = bytearray()
        self._out_buffer = bytearray()
        self._lock = threading.Lock()
        self._closed = False
        self._dir.mkdir(parents=True, exist_ok=True)
        logger.info("CallRecorder init: role=%s session=%s dir=%s", role, session_id, self._dir)

    def add_inbound(self, pcm: bytes) -> None:
        with self._lock:
            self._in_buffer.extend(pcm)

    def add_outbound(self, pcm: bytes) -> None:
        with self._lock:
            self._out_buffer.extend(pcm)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        t = threading.Thread(target=self._write_mixed, daemon=True)
        t.start()

    def _write_mixed(self) -> None:
        try:
            with self._lock:
                in_data = bytes(self._in_buffer)
                out_data = bytes(self._out_buffer)
                self._in_buffer.clear()
                self._out_buffer.clear()

            if not in_data and not out_data:
                logger.warning("CallRecorder: no audio data to save for %s", self._session_id)
                return

            logger.info("CallRecorder: processing %d inbound + %d outbound bytes for %s",
                        len(in_data), len(out_data), self._session_id)

            # Align: pad shorter stream with silence
            in_samples = len(in_data) // 2
            out_samples = len(out_data) // 2
            if in_samples < out_samples:
                in_data += b'\x00\x00' * (out_samples - in_samples)
            elif out_samples < in_samples:
                out_data += b'\x00\x00' * (in_samples - out_samples)

            # Mix via audioop
            mixed = audioop.add(in_data, out_data, 2)

            # Write mixed WAV
            wav_path = self._dir / f"{self._session_id}_mixed.wav"
            self._write_wav(wav_path, mixed)
            logger.info("CallRecorder: wrote WAV %s (%d bytes)", wav_path, wav_path.stat().st_size)

            # Compress to MP3
            mp3_path = self._dir / f"{self._session_id}.mp3"
            try:
                result = subprocess.run(
                    ["ffmpeg", "-y", "-f", "s16le", "-ar", "16000", "-ac", "1",
                     "-i", str(wav_path), "-acodec", "libmp3lame", "-b:a", "32k", str(mp3_path)],
                    capture_output=True, timeout=120,
                )
                if mp3_path.is_file():
                    wav_path.unlink(missing_ok=True)
                    logger.info("CallRecorder saved MP3: %s (%d KB)",
                                mp3_path, mp3_path.stat().st_size // 1024)
                    return
                else:
                    logger.warning("CallRecorder ffmpeg did not produce MP3, stderr=%s",
                                   result.stderr.decode("utf-8", errors="replace")[:500])
            except Exception as ffmpeg_err:
                logger.warning("CallRecorder ffmpeg failed: %s", ffmpeg_err)

            # WAV fallback
            final_wav = self._dir / f"{self._session_id}.wav"
            if wav_path.is_file():
                if final_wav.exists():
                    final_wav.unlink()
                wav_path.rename(final_wav)
                logger.info("CallRecorder saved WAV fallback: %s", final_wav)
            else:
                self._write_wav(final_wav, mixed)
                logger.info("CallRecorder saved WAV directly: %s", final_wav)

        except Exception:
            logger.exception("CallRecorder write error for %s", self._session_id)

    def _write_wav(self, path: Path, pcm_data: bytes) -> None:
        sample_rate = 16000
        num_channels = 1
        bits_per_sample = 16
        byte_rate = sample_rate * num_channels * bits_per_sample // 8
        block_align = num_channels * bits_per_sample // 8
        data_size = len(pcm_data)
        with open(path, "wb") as f:
            f.write(b"RIFF")
            f.write(struct.pack("<I", 36 + data_size))
            f.write(b"WAVE")
            f.write(b"fmt ")
            f.write(struct.pack("<I", 16))
            f.write(struct.pack("<H", 1))
            f.write(struct.pack("<H", num_channels))
            f.write(struct.pack("<I", sample_rate))
            f.write(struct.pack("<I", byte_rate))
            f.write(struct.pack("<H", block_align))
            f.write(struct.pack("<H", bits_per_sample))
            f.write(b"data")
            f.write(struct.pack("<I", data_size))
            f.write(pcm_data)


def resolve_session_recording_path(log_id: str):
    """Resolve a saved call-recording file for a call."""
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

    # Exact match at root
    for ext in (".mp3", ".wav"):
        exact = base / f"{log_id}{ext}"
        if exact.is_file():
            return exact

    # Search role subdirectories
    try:
        for pattern in (
            f"*/{log_id}.mp3", f"*/{log_id}.wav",
            f"*/campaign/{log_id}.mp3", f"*/manual/{log_id}.mp3",
            f"*/campaign/{log_id}.wav", f"*/manual/{log_id}.wav",
        ):
            hits = sorted(base.glob(pattern))
            if hits:
                return hits[0]
    except Exception:
        pass

    # Recursive fallback
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
