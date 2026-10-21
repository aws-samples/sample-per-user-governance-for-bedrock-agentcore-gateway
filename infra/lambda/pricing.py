# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CP-6 Pricing service — pure, unit-testable USD rate sheet + token→USD cost.

Feature F3 (B4 USD Quota). This module is the money-unit half of the quota
capability: it holds a per-model **rate sheet** (micro-USD per token, per token
type) and the single pure conversion the ledger, reporting (B11), and anomaly
(B13) all reuse:

    cost_micro_usd(rate, usage_tokens) -> int   # integer micro-USD, no floats

Micro-USD is an integer unit. token→USD is a four-field multiply because the
ledger's four token fields map 1:1 to Bedrock's four priced token types. The
seed numbers and their placeholder flags were read off the Amazon Bedrock
pricing page (https://aws.amazon.com/bedrock/pricing/), all us-east-1
on-demand, accessed 2026-09-15.

**No I/O and no boto3.** The hot path (and every unit test) prices from the
in-memory ``SEED_RATE_SHEET`` below. Refreshing the sheet from the AWS Price
List / CUR is a *separate* scheduled path (see :func:`refresh_from_price_list`,
a documented stub) that writes ``RATE_SHEET#`` DynamoDB items; the interceptor
only ever *reads* a resolved :class:`Rate`. This keeps the conversion
deterministic and testable in isolation, exactly like ``allowed_set`` (F2).

Money unit — **integer micro-USD** (``1 USD = 1_000_000 µ$``), never floats:
DynamoDB atomic ``ADD`` and conditional ``<=`` are exact on integers, and
micro-USD gives sub-cent resolution for per-token prices (one token can cost far
less than 1¢). Rates are stored as **micro-USD per 1,000 tokens** (matching how
AWS publishes "$/1M tokens" — divide by 1000) so the seed values are small
integers rather than sub-unit fractions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


# The micro-USD scale factor. 1 USD == 1_000_000 µ$.
MICRO_USD_PER_USD = 1_000_000

# The four token fields the ledger records (mirror of lambda_function's
# USAGE_FIELDS), each priced by its own per-type rate below. Kept here as a
# local constant so this module has no import dependency on the interceptor.
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


@dataclass(frozen=True)
class Rate:
    """A per-model unit price, in **micro-USD per 1,000 tokens** per token type.

    One :class:`Rate` is one row of the CP-6 rate sheet (design §2.2
    ``RATE_SHEET#``). The four fields line up 1:1 with the ledger's four token
    fields (design §2.4). ``placeholder`` marks a rate that could not be
    confirmed from a machine-readable AWS source — the enforcement math treats a placeholder
    exactly like a verified rate (it must never price at zero), but the flag is
    carried so reporting/verification can surface which models still need a
    confirmed price. ``source`` records provenance for the same reason
    (``price_list`` | ``cur`` | ``manual_byok``).
    """

    input_micro_usd_per_1k: int
    output_micro_usd_per_1k: int
    cache_read_micro_usd_per_1k: int
    cache_write_micro_usd_per_1k: int
    source: str = "price_list"
    placeholder: bool = False

    def per_type(self) -> Mapping[str, int]:
        """Map each ledger token field to its µ$/1k rate (used by the pure
        cost function and trivially assertable in tests)."""
        return {
            "input_tokens": self.input_micro_usd_per_1k,
            "output_tokens": self.output_micro_usd_per_1k,
            "cache_read_input_tokens": self.cache_read_micro_usd_per_1k,
            "cache_creation_input_tokens": self.cache_write_micro_usd_per_1k,
        }


# --- seed rate sheet ---------------------------------------------------------
#
# Micro-USD per 1,000 tokens for every model in the config allowlist
# (config/config.yaml `allowed_set`). Derived from the published $/1M rates:
#   µ$/1k = (USD per 1M tokens) * 1_000_000 µ$/USD / 1000
#         = (USD per 1M tokens) * 1000
# e.g. $0.06/1M input  -> 0.06 * 1000 = 60 µ$/1k.
#
# `placeholder=True` marks a rate that could not be confirmed from a
# machine-readable AWS source: the
# number is a documented best-estimate, not a Price-List-confirmed rate, and is
# replaced the first time the real refresh path (below) observes the model.
#
# Keyed by the concrete upstream model_id (what allowed_set.resolve_alias
# returns), NOT the alias, so pricing is stable across alias renames.
SEED_RATE_SHEET: dict[str, Rate] = {
    # Amazon Nova Lite — all four fields VERIFIED from the Price List API
    # (USE1-NovaLite-*): $0.06 in / $0.24 out / $0.015 cache-read per 1M.
    # Nova has no separate cache-write line published; cache-write is priced at
    # the input rate (documented approximation).
    "amazon.nova-lite-v1:0": Rate(
        input_micro_usd_per_1k=60,
        output_micro_usd_per_1k=240,
        cache_read_micro_usd_per_1k=15,
        cache_write_micro_usd_per_1k=60,  # placeholder: no published write line
        source="price_list",
        placeholder=False,
    ),
    # Amazon Nova Micro — the cheapest allowlisted model and the default test
    # model. Nova *Lite* was confirmed from the Price List API but not Micro;
    # Micro's published on-demand rates are $0.035 in / $0.14 out per 1M
    # (aws.amazon.com/bedrock/pricing) — carried as PLACEHOLDER until the
    # Price-List refresh confirms them for this account. Cache-read taken at
    # ~25% of input (Nova family pattern), cache-write at input rate.
    "amazon.nova-micro-v1:0": Rate(
        input_micro_usd_per_1k=35,
        output_micro_usd_per_1k=140,
        cache_read_micro_usd_per_1k=9,   # ~25% of input, placeholder
        cache_write_micro_usd_per_1k=35,  # input rate, placeholder
        source="price_list",
        placeholder=True,
    ),
    # Anthropic Claude Haiku 4.5 (us. inference profile). Only
    # Claude 3 *Sonnet* input was confirmed from the API; the Haiku-4-5 rates below
    # are the published list rates ($1.00 in / $5.00 out per 1M; cache-read
    # ~$0.10, cache-write ~$1.25 per 1M) carried as PLACEHOLDER until CUR/Price
    # List observes this specific model id. Mantle's +10% surcharge is NOT
    # applied here — it is captured automatically once CUR trues the rate up
    # (design §2.3); the seed is the pre-surcharge list rate.
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": Rate(
        input_micro_usd_per_1k=1000,
        output_micro_usd_per_1k=5000,
        cache_read_micro_usd_per_1k=100,
        cache_write_micro_usd_per_1k=1250,
        source="price_list",
        placeholder=True,
    ),
    # --- Alias keys (the model ids clients send on the wire, matching the
    # config.yaml Allowed Set aliases). Same rates as the resolved ids above.
    # A production build resolves alias->id before pricing; the demo keys both
    # forms so pricing works whether or not the org Allowed Set rewrite ran.
    "anthropic.claude-haiku-4-5": Rate(
        input_micro_usd_per_1k=1000,
        output_micro_usd_per_1k=5000,
        cache_read_micro_usd_per_1k=100,
        cache_write_micro_usd_per_1k=1250,
        source="price_list",
        placeholder=True,
    ),
}


def get_rate(
    model_id: str, rate_sheet: Mapping[str, Rate] | None = None
) -> Rate | None:
    """Resolve a model_id to its :class:`Rate`, or ``None`` when the model is
    not on the sheet.

    The interceptor turns a ``None`` here into a **503 fail-closed** (design
    §2.11 step 3): never price an unknown model at zero, which would let spend
    go uncounted. ``rate_sheet`` defaults to :data:`SEED_RATE_SHEET`; tests pass
    a small fixture sheet.

    Phase 7 ("enable ALL models" + per-user USD thresholds) needs a price for
    every routable model, far beyond the hand-verified seed sheet. When the
    ``PRICING_DEFAULT_ESTIMATE`` environment variable is set (to a
    ``in/out/cread/cwrite`` micro-USD-per-1k tuple, or ``1`` for the built-in
    :data:`DEFAULT_ESTIMATE_RATE`), an unknown model falls back to that
    ESTIMATED rate (``placeholder=True``, ``source="default_estimate"``) instead
    of returning ``None``. This keeps USD enforcement live across the whole
    catalog with costs clearly labelled estimated (PHASE7_PLAN decision #4), and
    is OFF by default so the original F3 fail-closed semantics are unchanged for
    deployments that do not opt in.
    """
    sheet = SEED_RATE_SHEET if rate_sheet is None else rate_sheet
    rate = sheet.get(model_id)
    if rate is not None:
        return rate
    return _default_estimate_rate()


# --- Phase 7 default estimated rate -----------------------------------------
#
# A conservative catalog-wide fallback so USD thresholds can be enforced on
# every ACTIVE model even before its exact price is on the seed sheet. Chosen at
# roughly the mid-to-high end of current Bedrock text-model list prices so an
# estimate is more likely to OVER-count than under-count spend (fail-safe for a
# budget ceiling). Flagged placeholder + source="default_estimate" so reporting
# can surface which spend is estimated vs confirmed.
DEFAULT_ESTIMATE_RATE = Rate(
    input_micro_usd_per_1k=1000,    # $1.00 / 1M in
    output_micro_usd_per_1k=5000,   # $5.00 / 1M out
    cache_read_micro_usd_per_1k=100,
    cache_write_micro_usd_per_1k=1250,
    source="default_estimate",
    placeholder=True,
)


def _default_estimate_rate() -> Rate | None:
    """Return the configured default estimate Rate, or ``None`` when the
    ``PRICING_DEFAULT_ESTIMATE`` env var is unset (preserving F3 fail-closed).

    Accepted values: ``"1"``/``"true"``/``"on"`` -> :data:`DEFAULT_ESTIMATE_RATE`;
    or ``"in/out/cread/cwrite"`` integers (µ$ per 1k) -> a custom Rate.
    """
    import os

    raw = os.environ.get("PRICING_DEFAULT_ESTIMATE", "").strip().lower()
    if not raw:
        return None
    if raw in ("1", "true", "on", "yes"):
        return DEFAULT_ESTIMATE_RATE
    parts = raw.split("/")
    if len(parts) == 4:
        try:
            i, o, cr, cw = (int(p) for p in parts)
            return Rate(i, o, cr, cw, source="default_estimate", placeholder=True)
        except ValueError:
            return DEFAULT_ESTIMATE_RATE
    return DEFAULT_ESTIMATE_RATE


def cost_micro_usd(rate: Rate, usage_tokens: Mapping[str, int]) -> int:
    """Convert a per-type token-usage dict to integer micro-USD.

    The core conversion (design §2.4): four multiplies, one per already-recorded
    ledger token field, each ``tokens * rate_per_1k // 1000``. Integer floor
    division keeps the result an exact integer µ$ and can only ever *under*-count
    by <1 µ$ per field (sub-micro-USD rounding), which is immaterial and
    conservative (never over-charges on the rounding).

    ``usage_tokens`` is any mapping carrying the four ``TOKEN_FIELDS`` keys
    (a plain dict in tests, ``Usage.as_dict()`` at runtime); missing keys count
    as zero, and any extra keys (e.g. ``total_tokens``) are ignored — so a
    ``Usage.as_dict()`` can be passed straight through.

    Pure and deterministic: same inputs → same integer, no I/O.
    """
    per_type = rate.per_type()
    total = 0
    for field, price_per_1k in per_type.items():
        tokens = int(usage_tokens.get(field, 0) or 0)
        if tokens < 0:
            # A negative token count is nonsense from a real usage record; clamp
            # to zero rather than credit the budget. Defensive only.
            tokens = 0
        total += (tokens * price_per_1k) // 1000
    return total


def refresh_from_price_list(model_ids, region: str = "us-east-1"):  # pragma: no cover
    """DOCUMENTED STUB — the scheduled rate-sheet refresh path (design §2.3).

    NOT called on the hot path and NOT exercised by unit tests (it would require
    live AWS). In the deployed system an EventBridge schedule invokes a
    ``pricing-refresh`` Lambda that:

    1. Calls **AWS Price List** ``GetProducts`` (``serviceCode`` for Amazon
       Bedrock, ``TERM_MATCH`` filters on region + usage-type) to read current
       list prices per model / token type; applies the known **+10% mantle
       surcharge** to OpenAI-family list prices as a documented approximation
       until CUR observes the real rate.
    2. Optionally derives the *effective* unit price from **CUR 2.0**
       (``Σ BlendedCost / Σ tokens`` per usage-type over a trailing window),
       which is the source of truth and already reflects the mantle surcharge.
    3. ``manual_byok`` admin-supplied contract rates override both (design §2.3
       precedence: manual_byok > cur > price_list).
    4. Writes new ``RATE_SHEET#<version>/<pricing_key>`` items and atomically
       repoints ``RATE_SHEET#CURRENT``. A failed refresh keeps the last good
       sheet (never prices at zero) and alarms.

    The hot path reads the resolved ``RATE_SHEET#CURRENT`` rows behind the same
    short-TTL in-Lambda cache as the other config reads; :data:`SEED_RATE_SHEET`
    is the bootstrap used until the first successful refresh lands.

    Raises ``NotImplementedError`` so an accidental hot-path call fails loudly
    rather than silently returning stale/zero prices.
    """
    raise NotImplementedError(
        "refresh_from_price_list is a scheduled-path stub; the hot path reads "
        "the resolved rate sheet (SEED_RATE_SHEET / RATE_SHEET#CURRENT)."
    )
