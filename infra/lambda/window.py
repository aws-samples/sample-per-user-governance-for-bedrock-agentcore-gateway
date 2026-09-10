# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The budget-window bucket key, shared by the interceptor and the
attribution Lambda so the admission read and the settlement write can never
disagree on which counter a moment in time belongs to.

Windows are calendar-aligned in UTC, matching the API Gateway usage-plan and
LiteLLM proxy precedents (day resets at midnight UTC, week on Monday per
ISO-8601, month on the 1st). Fixed calendar buckets, not rolling windows:
every quota system surveyed (API Gateway, Cloudflare, GitHub, LiteLLM,
Portkey) uses fixed windows because they need only one atomic counter per
period. The trailing token of USAGE#<sub>#<period> is the only thing that
varies; the prefix and sub segment are stable, so per-user reads and
begins_with scans are unaffected by the window choice.
"""
import time

VALID_WINDOWS = ("hour", "day", "week", "month")

_FORMATS = {
    "hour": "%Y-%m-%dT%H",   # 2026-07-30T15
    "day": "%Y-%m-%d",       # 2026-07-30 (the original, default shape)
    "week": "%G-W%V",        # 2026-W31 (ISO week, Monday start)
    "month": "%Y-%m",        # 2026-07
}


def window_key(now: int, window: str) -> str:
    """The period token for the epoch second ``now`` under ``window``."""
    try:
        fmt = _FORMATS[window]
    except KeyError:
        raise ValueError(
            f"BUDGET_WINDOW must be one of {', '.join(VALID_WINDOWS)}; got {window!r}"
        ) from None
    return time.strftime(fmt, time.gmtime(now))
