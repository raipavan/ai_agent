"""Audio helpers — PCM16 mono resampling (linear interpolation)."""

from __future__ import annotations


def pcm_resample(pcm: bytes, src_rate: int, dst_rate: int):
    """Resample raw PCM16 mono audio. Returns (bytes, dst_rate).

    Pass-through when rates match; empty input passes through untouched.
    """
    if not pcm:
        return pcm, int(dst_rate or 16000)
    src_rate = int(src_rate or 8000)
    dst_rate = int(dst_rate or 16000)
    if src_rate == dst_rate:
        return pcm, dst_rate
    try:
        import numpy as np

        arr = np.frombuffer(pcm, dtype=np.int16)
        if len(arr) < 2:
            return pcm, dst_rate
        n_out = max(1, int(round(len(arr) * dst_rate / src_rate)))
        x = np.linspace(0, len(arr) - 1, n_out)
        x0 = np.floor(x).astype(np.int64)
        x1 = np.minimum(x0 + 1, len(arr) - 1)
        frac = (x - x0).astype(np.float32)
        out = (arr[x0].astype(np.float32) * (1.0 - frac) + arr[x1].astype(np.float32) * frac)
        return out.astype(np.int16).tobytes(), dst_rate
    except Exception:
        return pcm, dst_rate
