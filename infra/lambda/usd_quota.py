# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""USD quota math (B4) — pure, unit-testable reservation & admission helpers.

Feature F3 (B4 USD Quota). The decision half of the USD-denominated budget: the
integer micro-USD conversions and the pre-flight **reservation / would-exceed**
logic the interceptor's CP-3 **Stage 8** uses to accept or reject a request
*before* it is routed upstream (REQ-QUOTA-005A: no partial processing). Pricing
(token→USD) lives in the sibling :mod:`pricing` module; this module composes it
into a budget decision.

The design points this module implements: micro-USD as an integer unit,
reserve-before-route with a 1% headroom, and an effective budget resolved
against a monthly window.

**No I/O and no boto3** — every function here is a deterministic integer
computation. The atomic DynamoDB conditional-write that actually *enforces*
:func:`would_exceed` (closing the TOCTOU between two concurrent requests) lives
in ``lambda_function.DynamoRepository``; this module supplies the exact same
comparison so the admit decision is testable in isolation and the transaction
condition and the Lambda-side check can never disagree.

Money unit — **integer micro-USD** (``1 USD = 1_000_000 µ$``), never floats
(design §2.1). Rendering to ``$0.00`` for humans (REQ-QUOTA-008) is a
divide-by-1e6 at the read edge only (:func:`render_usd`).
"""

from __future__ import annotations

from typing import Mapping

import pricing


MICRO_USD_PER_USD = pricing.MICRO_USD_PER_USD  # 1_000_000

# Default REQ-QUOTA-005A tolerance: admission may exceed the nominal budget by
# at most this percent, absorbing bounded output-estimate slack (design §2.5,
# ORG#/SETTINGS.budget_headroom_pct). Overridable per call/policy.
DEFAULT_HEADROOM_PCT = 1


def micro_usd(usd: float | int) -> int:
    """Convert a USD amount (e.g. the config ``default_quota_usd: 100.0``) to
    integer micro-USD.

    Rounds to the nearest micro-USD so a config value like ``100.0`` → exactly
    ``100_000_000`` with no float drift, and ``0.0000005`` rounds to ``1`` µ$
    rather than truncating a real sub-micro price to zero. This is the ONLY
    float→int boundary; every downstream computation is integer.

    Rationale for round-not-truncate: budgets and contract rates are entered by
    humans in USD; truncation would silently shave a hair off every budget.
    """
    # Scale then round half-up via +0.5 on the positive domain (budgets/prices
    # are non-negative). round() would use banker's rounding; explicit is
    # clearer and matches "nearest micro-USD".
    if usd < 0:
        raise ValueError("usd must be non-negative")
    return int(usd * MICRO_USD_PER_USD + 0.5)


def render_usd(amount_micro_usd: int) -> str:
    """Render integer micro-USD as a ``$0.00`` string for humans
    (REQ-QUOTA-008). Read-edge only — never used in a budget comparison."""
    dollars = amount_micro_usd / MICRO_USD_PER_USD
    return f"${dollars:,.2f}"


def budget_ceiling(budget_micro_usd: int, headroom_pct: int = DEFAULT_HEADROOM_PCT) -> int:
    """The admission ceiling: ``budget * (1 + headroom_pct/100)`` in integer µ$.

    This is the right-hand side of the admit compare (design §2.5 step 4). The
    headroom is the REQ-QUOTA-005A "within 1%" tolerance — it absorbs the
    bounded output-estimate slack of a single admitted request. Computed as
    ``budget + budget*headroom_pct//100`` so it stays exact integer math and a
    0% headroom yields exactly the budget (strict compare).
    """
    if budget_micro_usd < 0:
        raise ValueError("budget_micro_usd must be non-negative")
    if headroom_pct < 0:
        raise ValueError("headroom_pct must be non-negative")
    return budget_micro_usd + (budget_micro_usd * headroom_pct) // 100


def estimate_reservation(
    rate: "pricing.Rate",
    input_tokens_estimate: int,
    max_tokens: int,
    *,
    cache_write_tokens_estimate: int = 0,
) -> int:
    """Pre-flight USD reservation for one request, in integer micro-USD
    (REQ-QUOTA-005A, design §2.5 steps 1–3).

    The reservation is the request's **maximum** possible cost:

        reservation = input_tokens_estimate * input_rate
                    + max_tokens            * output_rate
                    + cache_write_estimate  * cache_write_rate

    ``input_tokens_estimate`` is the exact input count from Bedrock
    ``CountTokens`` at runtime (design §2.5 step 1); in unit tests it is a fixed
    integer. ``max_tokens`` is the OUTPUT ceiling the caller is bounded to —
    ``min(requested_max_tokens, policy.max_max_tokens)`` computed by the caller
    (design §2.5 step 2) — so the output slice of the reservation is bounded and
    a caller cannot reserve the whole budget with a huge ``max_tokens``.

    Reserving the output *ceiling* (not an estimate) is what makes the decision
    fully pre-route: worst case actual output < reserved, we over-reserved and
    release the slack at settlement — never an overspend (design §2.5 "bounded
    overshoot").

    BOUNDARY (B4-critique F1): a streaming request that sends no ``max_tokens``
    forces the caller to pass the ceiling (``policy.max_max_tokens``), so the
    reservation can be large relative to a small budget and the user's own
    concurrent request may be 429'd against this in-flight reservation. That is
    the documented self-429 risk — 005A ("no overspend") is HELD; the cost is a
    possible false-reject on small budgets under concurrent streaming. Cannot be
    removed at the pure-function layer (it is inherent to a hard pre-route
    reserve); mitigations (percentile output estimate, per-model ceiling) are
    design-level. We compute the honest ceiling here.

    BOUNDARY (B4-critique F5): ``input_tokens_estimate`` from CountTokens can
    differ from what the model actually bills (system-prompt injection, tool
    schemas, multimodal parts). That estimate error is ADDITIVE to the 1%
    output-slack headroom and is not budgeted for here — the caller must size
    ``headroom_pct`` to cover both, or calibrate empirically. Documented, not
    silently absorbed.
    """
    if input_tokens_estimate < 0 or max_tokens < 0 or cache_write_tokens_estimate < 0:
        raise ValueError("token counts must be non-negative")
    usage = {
        "input_tokens": input_tokens_estimate,
        "output_tokens": max_tokens,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": cache_write_tokens_estimate,
    }
    return pricing.cost_micro_usd(rate, usage)


def would_exceed(
    consumed_micro_usd: int,
    reserved_micro_usd: int,
    budget_micro_usd: int,
    incoming_micro_usd: int,
    *,
    headroom_pct: int = DEFAULT_HEADROOM_PCT,
) -> bool:
    """Return True iff admitting a request costing ``incoming_micro_usd`` would
    push the user over budget (design §2.5 step 4).

    Effective consumption for the compare = settled ``consumed`` + outstanding
    ``reserved`` (in-flight reservations), so a request is measured against both
    what is already spent AND what concurrent in-flight requests have reserved
    (design §2.2 "effective consumption"). This is the exact predicate the
    DynamoDB conditional write asserts server-side:

        admit  iff  consumed + reserved + incoming <= budget * (1 + hp/100)

    so ``would_exceed`` is the negation of the admit condition. Keeping the
    identical arithmetic here means the Lambda-side pre-read and the
    transaction's condition never diverge (B4-critique F4: the admit decision
    MUST be the transaction's condition result; this function IS that
    condition, evaluated on the same integers the transaction uses).

    Exactly-at-limit ADMITS (``<=``): a request that lands the user exactly on
    the ceiling is accepted; only strictly exceeding it is rejected.

    TOCTOU (B4-critique F4 / design §2.5): two concurrent requests can both see
    the same ``consumed``/``reserved`` here and both compute ``would_exceed ==
    False`` — that is expected. The DynamoDB ``TransactWriteItems`` re-evaluates
    this same condition against the *committed* item at commit time and atomic
    ``ADD``s the reservation, so the second transaction fails its condition (or
    TransactionConflict-retries and then fails) and does NOT overspend. The
    guard PREVENTS overspend server-side; it does not merely detect it. This
    function models the per-request view; the transaction serializes them.
    """
    if incoming_micro_usd < 0:
        raise ValueError("incoming_micro_usd must be non-negative")
    effective = consumed_micro_usd + reserved_micro_usd + incoming_micro_usd
    ceiling = budget_ceiling(budget_micro_usd, headroom_pct)
    return effective > ceiling


def within_tolerance(
    consumed_micro_usd: int,
    reserved_micro_usd: int,
    budget_micro_usd: int,
    incoming_micro_usd: int,
    *,
    headroom_pct: int = DEFAULT_HEADROOM_PCT,
) -> bool:
    """Convenience inverse of :func:`would_exceed`: True when the request is
    admissible within the ``headroom_pct`` tolerance (REQ-QUOTA-005A no-partial:
    a request within 1% of the budget is accepted whole, not partially
    processed). Provided so call sites and tests can read the positive form."""
    return not would_exceed(
        consumed_micro_usd,
        reserved_micro_usd,
        budget_micro_usd,
        incoming_micro_usd,
        headroom_pct=headroom_pct,
    )


def effective_budget_micro_usd(
    base_micro_usd: int,
    temp_increase_micro_usd: int,
    temp_increase_period: str,
    current_period: str,
) -> int:
    """Resolve the effective budget for the current window (design §2.8):
    ``base + temp_increase`` but ONLY when the temp increase is keyed to the
    current ``%Y-%m`` window; otherwise the increase has auto-expired
    (REQ-QUOTA-004/004A — no reset job, the calendar key IS the expiry).

    ``current_period`` is ``window.window_key(now, "month")``; a temp increase
    stamped with a past period is silently ignored, which is exactly the
    monthly-reset-of-the-increase behavior 004A requires.
    """
    if base_micro_usd < 0 or temp_increase_micro_usd < 0:
        raise ValueError("budget amounts must be non-negative")
    if temp_increase_period and temp_increase_period == current_period:
        return base_micro_usd + temp_increase_micro_usd
    return base_micro_usd


def resolve_effective_budget(
    user_budget_micro_usd: int | None,
    group_budgets_micro_usd: Mapping[str, int] | None,
    org_default_micro_usd: int,
) -> int:
    """Precedence resolution (REQ-QUOTA-001/003/003A, design §2.8):
    user override → group → org default, first match wins.

    ``user_budget_micro_usd`` is the ``USER#.budget_micro_usd`` override
    (``None`` = not set). ``group_budgets_micro_usd`` maps the user's group ids
    to their ``GROUP#.budget_micro_usd``; when the user is in several groups we
    take the **most generous (max)** budget. (B4-critique F8 flags "max/first"
    as ambiguous in the design; we resolve it explicitly to MAX here so a user
    is never under-served by an unrelated restrictive group, and document the
    choice — verification must cover a multi-group user.)

    Temp increases are applied by :func:`effective_budget_micro_usd` on top of
    whatever base this returns.
    """
    if user_budget_micro_usd is not None:
        return user_budget_micro_usd
    if group_budgets_micro_usd:
        return max(group_budgets_micro_usd.values())
    return org_default_micro_usd
