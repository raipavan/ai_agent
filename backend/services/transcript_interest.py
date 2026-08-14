"""Transcript interest detection (stub)."""

from __future__ import annotations


def soft_interest_in_text(text, *args, **kwargs) -> bool:
    """Stub: never flags interest."""
    return False


def apply_interest_disposition_override(analysis, transcript, *args, **kwargs):
    """Stub: leave analysis unchanged."""
    return analysis
