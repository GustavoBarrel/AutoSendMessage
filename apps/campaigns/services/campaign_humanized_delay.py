"""Humanized delay calculation for campaign sends."""

from __future__ import annotations

import random

MIN_DELAY_SECONDS = 30
LOWER_RATIO = 0.9
UPPER_RATIO = 1.6
MODE_RATIO = 1.15


def calculate_humanized_delay_seconds(base_seconds: int) -> int:
    """
    Return a delay with conservative variation around the base value.

    The range allows at most 10% earlier than the configured delay, but pulls most
    values above the base. With a 120s base: 108s-192s, mode 138s.
    """
    safe_base = max(MIN_DELAY_SECONDS, int(base_seconds or 0))
    min_delay = max(MIN_DELAY_SECONDS, int(safe_base * LOWER_RATIO))
    max_delay = max(min_delay, int(safe_base * UPPER_RATIO))
    mode_delay = min(max_delay, max(min_delay, int(safe_base * MODE_RATIO)))
    return int(random.triangular(min_delay, max_delay, mode_delay))
