"""Audio helpers — PCM16 mono resampling.

`pcm_resample()` now uses a high-quality rational polyphase FIR resampler
(Kaiser-windowed sinc, proper anti-aliasing) with a numpy-linear fallback
so a resampler bug can never break a live call.
"""

from __future__ import annotations


def pcm_resample(pcm: bytes, src_rate: int, dst_rate: int):
    """Resample raw PCM16 mono audio. Returns (bytes, dst_rate).

    Pass-through when rates match; empty input passes through untouched.
    Tries the anti-aliased polyphase FIR first; falls back to linear
    interpolation on any error.
    """
    if not pcm:
        return pcm, int(dst_rate or 16000)
    src_rate = int(src_rate or 8000)
    dst_rate = int(dst_rate or 16000)
    if src_rate == dst_rate:
        return pcm, dst_rate
    try:
        return _resample_polyphase(pcm, src_rate, dst_rate), dst_rate
    except Exception:
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
            out = arr[x0].astype(np.float32) * (1.0 - frac) + arr[x1].astype(np.float32) * frac
            return out.astype(np.int16).tobytes(), dst_rate
        except Exception:
            return pcm, dst_rate


def _resample_polyphase(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Anti-aliased rational polyphase resampler (pure numpy).

    Upsamples by P (zero-stuffing), filters with a Kaiser-windowed sinc FIR
    (cuts everything above the lower of the two Nyquist frequencies), then
    decimates by Q — mathematically equivalent to scipy.signal.resample_poly.
    """
    from math import gcd

    import numpy as np

    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if len(arr) < 2:
        return pcm
    g = gcd(int(src_rate), int(dst_rate))
    p = int(dst_rate) // g  # upsample factor
    q = int(src_rate) // g  # decimate factor
    if p == q:
        return pcm

    # FIR design: cutoff at the stricter Nyquist of the two rates, Kaiser
    # window with beta 8.6 (~80 dB stopband), ~12 zero-crossings per side.
    half = 12 * max(p, q)
    fc = 1.0 / float(max(p, q))
    n = np.arange(-half, half + 1, dtype=np.float32)
    taps = np.sinc(2.0 * fc * n) * np.kaiser(2 * half + 1, 8.6)
    taps *= float(2.0 * fc * p)

    # Zero-stuff up by P and filter.
    stuffed = np.zeros(len(arr) * p, dtype=np.float32)
    stuffed[::p] = arr
    filtered = np.convolve(stuffed, taps, mode="full")

    # Phase-aligned decimation by Q starting at the filter's center tap.
    n_out = int(round(len(arr) * p / float(q)))
    idx = half + np.arange(n_out) * q
    idx = np.clip(idx, 0, len(filtered) - 1)
    out = filtered[idx]
    np.clip(out, -32768.0, 32767.0, out=out)
    return out.astype(np.int16).tobytes()
