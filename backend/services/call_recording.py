"""Call recording: capture and lookup."""

from __future__ import annotations

import audioop
import logging
import struct
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from config import settings as _settings
    _RECORDING_BASE = Path(getattr(_settings, "call_recording_dir", "") or "")
except Exception:
    _RECORDING_BASE = None

# 1 second of silence at 16kHz mono s16le
_SILENCE_1S = b'\x00\x00' * 16000


def _apply_gain(pcm_data: bytes, gain: float) -> bytes:
    if gain == 1.0 or not pcm_data:
        return pcm_data
    return audioop.mul(pcm_data, 2, gain)


def _peak_level(pcm_data: bytes) -> int:
    if len(pcm_data) < 2:
        return 0
    peak = 0
    for i in range(0, len(pcm_data) - 1, 2):
        sample = struct.unpack_from('<h', pcm_data, i)[0]
        abs_val = abs(sample)
        if abs_val > peak:
            peak = abs_val
    return peak


def _detect_turns_and_insert_gaps(caller_pcm: bytes, agent_pcm: bytes, gap_ms: int = 1000, threshold: int = 200):
    """Detect caller turns in the inbound track and insert silence gaps
    in the outbound track after each caller turn ends.
    
    This makes the recording sound like: caller speaks -> gap -> agent responds.
    The live conversation is NOT affected.
    """
    sample_rate = 16000
    bytes_per_sample = 2
    frame_ms = 20  # analyze in 20ms frames
    frame_bytes = sample_rate * bytes_per_sample * frame_ms // 1000  # 640 bytes
    gap_bytes = sample_rate * bytes_per_sample * gap_ms // 1000  # 32000 bytes for 1s

    # Detect caller speech activity: find frames where caller is speaking
    caller_active = []  # list of (start_frame, end_frame) ranges
    in_speech = False
    speech_start = 0
    
    for i in range(0, len(caller_pcm) - frame_bytes, frame_bytes):
        frame = caller_pcm[i:i + frame_bytes]
        peak = _peak_level(frame)
        frame_idx = i // frame_bytes
        
        if peak > threshold:
            if not in_speech:
                speech_start = frame_idx
                in_speech = True
        else:
            if in_speech:
                caller_active.append((speech_start, frame_idx))
                in_speech = False
    if in_speech:
        caller_active.append((speech_start, len(caller_pcm) // frame_bytes))

    if not caller_active:
        # No caller speech detected — return as-is
        return agent_pcm

    logger.info("CallRecorder: detected %d caller turns, inserting %dms gaps", len(caller_active), gap_ms)

    # For each caller turn end, insert gap_bytes of silence into agent PCM
    # at the corresponding position
    result = bytearray(agent_pcm)
    offset = 0  # cumulative byte offset from inserted gaps
    
    for turn_start, turn_end in caller_active:
        # Position in bytes where this turn ends (in original timeline)
        pos_bytes = turn_end * frame_bytes
        # Adjust for previously inserted gaps
        insert_pos = pos_bytes + offset
        # Don't insert gap before agent audio starts or after it ends
        if insert_pos < 0:
            insert_pos = 0
        if insert_pos > len(result):
            insert_pos = len(result)
        # Insert silence
        result[insert_pos:insert_pos] = _SILENCE_1S[:gap_bytes]
        offset += gap_bytes

    return bytes(result)


class CallRecorder:
    """Capture inbound (caller) and outbound (agent) 16 kHz mono s16le PCM,
    produce a stereo MP3 (left=caller, right=agent) on close().
    
    Tracks real-time gaps in the outbound stream and inserts silence so the
    agent channel reflects actual wall-clock timing (greeting -> silence while
    caller speaks -> agent response).
    """

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
        self._first_out_ts: float = 0.0
        self._first_in_ts: float = 0.0
        self._last_out_ts: float = 0.0
        self._last_in_ts: float = 0.0
        logger.info("CallRecorder init: role=%s session=%s dir=%s", role, session_id, self._dir)

    def add_inbound(self, pcm: bytes) -> None:
        with self._lock:
            now = time.monotonic()
            if not self._in_buffer and pcm:
                self._first_in_ts = now
            # Insert silence for gaps in inbound stream
            if self._last_in_ts > 0 and pcm:
                gap = now - self._last_in_ts
                if gap > 0.1:
                    gap_bytes = int(gap * 16000 * 2)
                    gap_bytes = (gap_bytes // 2) * 2
                    if gap_bytes > 0:
                        self._in_buffer.extend(b'\x00\x00' * (gap_bytes // 2))
            self._last_in_ts = now
            self._in_buffer.extend(pcm)

    def add_outbound(self, pcm: bytes) -> None:
        with self._lock:
            now = time.monotonic()
            if not self._out_buffer and pcm:
                self._first_out_ts = now
            # Insert silence for gaps in outbound stream (e.g. greeting ends,
            # agent waits for caller, then Gemini responds)
            if self._last_out_ts > 0 and pcm:
                gap = now - self._last_out_ts
                if gap > 0.1:
                    gap_bytes = int(gap * 16000 * 2)
                    gap_bytes = (gap_bytes // 2) * 2
                    if gap_bytes > 0:
                        self._out_buffer.extend(b'\x00\x00' * (gap_bytes // 2))
            self._last_out_ts = now
            self._out_buffer.extend(pcm)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        t = threading.Thread(target=self._write_recording, daemon=True)
        t.start()

    def _write_mono_wav(self, path: Path, pcm_data: bytes) -> None:
        sample_rate = 16000
        byte_rate = sample_rate * 2
        data_size = len(pcm_data)
        with open(path, "wb") as f:
            f.write(b"RIFF")
            f.write(struct.pack("<I", 36 + data_size))
            f.write(b"WAVEfmt ")
            f.write(struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16))
            f.write(b"data")
            f.write(struct.pack("<I", data_size))
            f.write(pcm_data)

    def _write_recording(self) -> None:
        try:
            with self._lock:
                in_data = bytes(self._in_buffer)
                out_data = bytes(self._out_buffer)
                first_out = self._first_out_ts
                first_in = self._first_in_ts
                self._in_buffer.clear()
                self._out_buffer.clear()

            if not in_data and not out_data:
                logger.warning("CallRecorder: no audio data to save for %s", self._session_id)
                return

            logger.info("CallRecorder: %d inbound + %d outbound bytes for %s",
                        len(in_data), len(out_data), self._session_id)

            # Normalize each track
            TARGET = 14000
            in_peak = _peak_level(in_data) if in_data else 0
            out_peak = _peak_level(out_data) if out_data else 0
            logger.info("CallRecorder peaks: inbound=%d outbound=%d", in_peak, out_peak)

            if in_peak > 0:
                gain = min(TARGET / in_peak, 8.0)
                if gain != 1.0:
                    in_data = _apply_gain(in_data, gain)
            if out_peak > 0:
                gain = min(TARGET / out_peak, 4.0)
                if gain != 1.0:
                    out_data = _apply_gain(out_data, gain)

            # Sync: prepend silence to inbound to align with outbound timing
            if first_out > 0 and first_in > 0 and first_in > first_out:
                delay_sec = first_in - first_out
                delay_bytes = int(delay_sec * 16000 * 2)
                delay_bytes = (delay_bytes // 2) * 2
                if delay_bytes > 0:
                    logger.info("CallRecorder: syncing inbound delay=%.1fs", delay_sec)
                    in_data = b'\x00\x00' * (delay_bytes // 2) + in_data
            elif first_in == 0 and first_out > 0 and out_data:
                in_data = b'\x00' * len(out_data)

            # Align to same length
            in_samples = len(in_data) // 2
            out_samples = len(out_data) // 2
            max_samples = max(in_samples, out_samples)
            if in_samples < max_samples:
                in_data += b'\x00\x00' * (max_samples - in_samples)
            elif out_samples < max_samples:
                out_data += b'\x00\x00' * (max_samples - out_samples)

            # Write mono WAVs
            in_wav = self._dir / f"{self._session_id}_caller.wav"
            out_wav = self._dir / f"{self._session_id}_agent.wav"
            self._write_mono_wav(in_wav, in_data)
            self._write_mono_wav(out_wav, out_data)

            # ffmpeg merge into stereo MP3
            mp3_path = self._dir / f"{self._session_id}.mp3"
            try:
                result = subprocess.run(
                    ["ffmpeg", "-y",
                     "-i", str(in_wav),
                     "-i", str(out_wav),
                     "-filter_complex",
                     "[0:a][1:a]amerge=inputs=2,pan=stereo|c0=c0|c1=c1[out]",
                     "-map", "[out]",
                     "-acodec", "libmp3lame", "-b:a", "64k",
                     str(mp3_path)],
                    capture_output=True, timeout=120,
                )
                in_wav.unlink(missing_ok=True)
                out_wav.unlink(missing_ok=True)

                if mp3_path.is_file():
                    logger.info("CallRecorder saved stereo MP3: %s (%d KB)",
                                mp3_path, mp3_path.stat().st_size // 1024)
                    return
                else:
                    logger.warning("CallRecorder ffmpeg stderr: %s",
                                   result.stderr.decode("utf-8", errors="replace")[:500])
            except Exception as ffmpeg_err:
                logger.warning("CallRecorder ffmpeg failed: %s", ffmpeg_err)
                in_wav.unlink(missing_ok=True)
                out_wav.unlink(missing_ok=True)

            # WAV fallback
            final_wav = self._dir / f"{self._session_id}.wav"
            interleaved = bytearray(max_samples * 4)
            for i in range(max_samples):
                offset = i * 4
                interleaved[offset:offset+2] = in_data[i*2:i*2+2]
                interleaved[offset+2:offset+4] = out_data[i*2:i*2+2]

            data_size = len(interleaved)
            with open(final_wav, "wb") as f:
                f.write(b"RIFF")
                f.write(struct.pack("<I", 36 + data_size))
                f.write(b"WAVEfmt ")
                f.write(struct.pack("<IHHIIHH", 16, 1, 2, 16000, 64000, 4, 16))
                f.write(b"data")
                f.write(struct.pack("<I", data_size))
                f.write(interleaved)
            logger.info("CallRecorder saved stereo WAV fallback: %s", final_wav)

        except Exception:
            logger.exception("CallRecorder write error for %s", self._session_id)


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

    for ext in (".mp3", ".wav"):
        exact = base / f"{log_id}{ext}"
        if exact.is_file():
            return exact

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
