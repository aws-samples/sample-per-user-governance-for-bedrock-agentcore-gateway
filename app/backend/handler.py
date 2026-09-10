# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Demo API for per-user governance on the Bedrock AgentCore Gateway.

This backend runs no agents. Chat turns go browser -> AgentCore Runtime ->
gateway with the user's own JWT; this Lambda only serves the admin plane:
personas, policy reads/writes, and the audit views read back from the
governance DynamoDB table the gateway writes.

This backend never enforces anything. The gateway enforces; the app just
renders what the gateway recorded.

Routes (HTTP API v2, behind a Cognito JWT authorizer):
  GET  /personas     personas + their live policy and usage + capabilities
  GET  /policy       ?persona=  policy + today's usage for one persona
  PUT  /policy       {persona, budgetTokens, downgradeAtTokens,
                      fallbackModel, blocked} -> writes POLICY#<sub>
  GET  /events/mine  ?since=<epoch>  the caller's REQ rows, newest last
  GET  /fleet        ?groupBy=user|request  per-user aggregates (default) or
                     the raw REQ admission feed newest-first; both include
                     usage rows and config. Legacy shape kept for old UIs.
  GET  /timeline     ?requestId=  one request's admission timestamp and its
                     current async-settlement status (settled vs settling)

Data model: the gateway writes no inline EVENT items on this path. The
request feed reads REQ#<request_id> admission items; per-user totals read
USAGE#<sub>#<date> aggregates that a separate async Lambda debits seconds to
minutes after admission. Attribution is labeled "settling" until that debit
advances the user's USAGE item past the admission snapshot.

Auth: API Gateway validates the caller's Cognito JWT before this code runs.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from typing import Any, Callable

import cognito
import governance

_LOG = logging.getLogger(__name__)
_LOG.setLevel(logging.INFO)


def _warn(event: str, **fields: Any) -> None:
    """One structured line for a degraded-but-continuing path.

    Same discipline as the interceptor's log: bounded identifiers, counters, and
    exception class names only, never a raw exception message, so nothing a
    caller controls reaches the log group."""
    _LOG.warning(json.dumps({"event": event, **fields}, separators=(",", ":"), sort_keys=True))


# Fallback only ever hit in local runs; the deployed Lambda always has
# AWS_REGION set by the runtime. us-east-1 matches the deploy scripts' default.
_REGION = os.environ.get("AWS_REGION", "us-east-1")

GATEWAY_URL = os.environ.get("GATEWAY_URL", "")


def _gateway_door(door: str) -> str:
    """Derive a door URL (e.g. /inference, /bedrock-runtime) from GATEWAY_URL.

    The single gateway exposes two doors: /inference (Anthropic Messages and
    OpenAI shapes) and /bedrock-runtime (Converse/Invoke). GATEWAY_URL is the
    bare gateway base; a stray trailing /mcp or existing door suffix is
    stripped before the requested door is appended."""
    base = GATEWAY_URL.rstrip("/")
    for suffix in ("/mcp", "/inference", "/bedrock-runtime"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base + door if base else ""


# The Anthropic/OpenAI inference door and the native-SDK Bedrock door; kept
# here for the config payload the UI displays.
INFERENCE_BASE_URL = _gateway_door("/inference")
BEDROCK_RUNTIME_BASE_URL = _gateway_door("/bedrock-runtime")
PRIMARY_MODEL = os.environ.get("PRIMARY_MODEL", "anthropic.claude-sonnet-5")
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL", "anthropic.claude-haiku-4-5")
MANTLE_FALLBACK_MODEL = os.environ.get(
    "MANTLE_FALLBACK_MODEL", "anthropic.claude-haiku-4-5"
)
# The mantle door's OpenAI shapes serve only OpenAI-family ids, so they need
# their own downgrade target: rewriting a Chat Completions request to a Claude
# id returns 400 "does not support the '/v1/chat/completions' API".
OPENAI_FALLBACK_MODEL = os.environ.get("OPENAI_FALLBACK_MODEL", "gpt-oss-20b")


# Provider-form (regional) inference-profile ids carry a us./eu./apac./global.
# prefix. The /bedrock-runtime passthrough door requires that form; the mantle
# /inference door rejects it and takes bare provider ids. Mirrors
# NATIVE_MODEL_ID in the frontend's types.ts.
_NATIVE_ID = re.compile(r"^(us|eu|apac|global)\.")


def _env_list(name: str, default: str) -> list[str]:
    return [m.strip() for m in os.environ.get(name, default).split(",") if m.strip()]


# --- Per-door model catalogs ------------------------------------------------
# The two doors have different catalogs AND different id spellings, so "which
# models can I pick" has two different answers and the UI must ask per door:
#
#   /inference, Anthropic Messages shape: bare provider ids only. An id the
#     account has not been granted answers 404 "not found on any target"; a
#     versioned id answers 400 "Model ID contains invalid characters".
#   /inference, OpenAI Chat Completions shape: a disjoint OSS-family catalog
#     (see OPENAI_MODELS); ids outside it answer 404.
#   /bedrock-runtime passthrough: whatever inference profile the gateway role
#     may invoke, so this one is discovered at runtime instead of listed.
#
# The mantle connector exposes no list API to callers (there is no
# bedrock-mantle client in botocore), so its two catalogs stay as env defaults
# rather than pretending to discover them.
MANTLE_MODELS = _env_list(
    "MANTLE_MODELS",
    "anthropic.claude-haiku-4-5,"
    "anthropic.claude-sonnet-5,"
    "anthropic.claude-opus-4-7,"
    "anthropic.claude-opus-4-8",
)
OPENAI_MODELS = _env_list("OPENAI_MODELS", "gpt-oss-120b,gpt-oss-20b,qwen3-32b")


def _is_openai_id(model: str) -> bool:
    """Whether an id belongs to the mantle door's OpenAI shapes. Decided by
    membership in that door's catalog, not by a name pattern: qwen3-32b is
    served there and looks nothing like an OpenAI id."""
    return model in OPENAI_MODELS or model == OPENAI_FALLBACK_MODEL


def _model_form(model: str) -> str:
    """Which door and wire shape an id is spelled for: "native" for the
    passthrough door, "openai" for the mantle door's Chat Completions and
    Responses shapes, "mantle" for its Anthropic Messages shape.

    Three forms rather than two because a downgrade target has to match the
    shape as well as the door, and an unknown bare id is far more likely to be
    an Anthropic-shape id than an OSS one, so "mantle" is the fallthrough.
    """
    if _NATIVE_ID.match(model):
        return "native"
    if _is_openai_id(model):
        return "openai"
    return "mantle"



# Static answer for the passthrough door, used when discovery is unavailable
# (no bedrock:ListInferenceProfiles grant, or the call fails).
NATIVE_MODELS = _env_list(
    "NATIVE_MODELS",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0,"
    "us.anthropic.claude-sonnet-5,"
    "us.anthropic.claude-sonnet-4-6,"
    "us.anthropic.claude-opus-5",
)
# Discovery scope for the passthrough door. Only Anthropic profiles are
# offered: the gateway role's model grant is scoped to anthropic.claude-*, so
# every Nova/Llama/Mistral profile in the account answers 403 AccessDenied
# through this gateway, and the Invoke shape the runtime
# containers send is the Anthropic body shape anyway.
NATIVE_MODEL_PREFIXES = _env_list("NATIVE_MODEL_PREFIXES", "us.anthropic.claude-")
# Substrings that exclude a discovered profile. The claude-3 generation,
# sonnet-4 and opus-4-1 answer 404 "marked
# by provider as Legacy ... not actively using" or end-of-life, and every
# fable-5 profile answers 400 "data retention mode 'default' is not available
# for this model". Offering any of them would put a guaranteed error in the
# dropdown.
NATIVE_MODEL_DENY = _env_list(
    "NATIVE_MODEL_DENY",
    "claude-3-,claude-sonnet-4-2025,claude-opus-4-1,fable",
)
# Cheapest first, then newest within a family: the same order both dropdowns
# show, so the fallback picker and the chat picker agree.
_FAMILY_RANK = {"haiku": 0, "sonnet": 1, "opus": 2}


def _model_sort_key(model: str) -> tuple:
    tail = model.split("/")[-1]
    family = next((f for f in _FAMILY_RANK if f in tail), None)
    version = re.search(r"(?:haiku|sonnet|opus)-(\d+)(?:-(\d+))?", tail)
    major = int(version.group(1)) if version else 0
    minor = int(version.group(2)) if version and version.group(2) else 0
    # Negative version so newer sorts first inside a family.
    return (_FAMILY_RANK.get(family, 9) if family else 9, -major, -minor, tail)


_native_cache: list[str] | None = None


def _discover_native_models() -> list[str]:
    """Inference profiles the passthrough door can actually serve.

    Asks Bedrock rather than hardcoding a list, so a fresh account offers what
    it really has. Cached for the life of the execution environment (the
    catalog changes on Bedrock's release cadence, not per request), and falls
    back to the static NATIVE_MODELS list on any failure so a missing
    bedrock:ListInferenceProfiles grant degrades the dropdown instead of the
    whole API."""
    global _native_cache
    if _native_cache is not None:
        return _native_cache
    try:
        import boto3

        client = boto3.client("bedrock", region_name=_REGION)
        found: list[str] = []
        token = None
        while True:
            page = client.list_inference_profiles(
                maxResults=100, **({"nextToken": token} if token else {})
            )
            for profile in page.get("inferenceProfileSummaries", []):
                model_id = profile.get("inferenceProfileId", "")
                if profile.get("status") != "ACTIVE":
                    continue
                if not any(model_id.startswith(p) for p in NATIVE_MODEL_PREFIXES):
                    continue
                if any(deny in model_id for deny in NATIVE_MODEL_DENY):
                    continue
                found.append(model_id)
            token = page.get("nextToken")
            if not token:
                break
        _native_cache = sorted(set(found or NATIVE_MODELS), key=_model_sort_key)
    except Exception:
        # Discovery is a convenience; the demo must still serve its policy and
        # audit views without it.
        _native_cache = sorted(set(NATIVE_MODELS), key=_model_sort_key)
    return _native_cache


def _available_models() -> dict[str, list[str]]:
    """What each wire format may be pointed at, keyed by the frontend's method
    ids ("anthropic"/"openai" are the mantle door, "native" covers
    converse/invoke on the passthrough door)."""
    # The deployment's own primary/fallback ids join the mantle catalog only
    # when they are in that door's bare form; a regional-profile id belongs to
    # the passthrough catalog and would 400 on this door.
    configured = [
        m for m in (PRIMARY_MODEL, FALLBACK_MODEL, MANTLE_FALLBACK_MODEL)
        if m and not _NATIVE_ID.match(m) and not _is_openai_id(m)
    ]
    openai = list(OPENAI_MODELS)
    if OPENAI_FALLBACK_MODEL and OPENAI_FALLBACK_MODEL not in openai:
        openai.append(OPENAI_FALLBACK_MODEL)
    return {
        "anthropic": sorted(set(MANTLE_MODELS + configured), key=_model_sort_key),
        "openai": openai,
        "native": _discover_native_models(),
    }


def _default_native_fallback() -> str:
    """The passthrough door's downgrade target.

    FALLBACK_MODEL may hold a single bare id meant for the mantle door, and the
    bare form cannot be served by this door. Rather than seed a policy whose
    downgrade is guaranteed to fail, derive the cheapest native id the account
    actually offers."""
    if _NATIVE_ID.match(FALLBACK_MODEL):
        return FALLBACK_MODEL
    native = _discover_native_models()
    return next(
        (m for m in native if "haiku" in m),
        native[0] if native else FALLBACK_MODEL,
    )


def _default_mantle_fallback() -> str:
    """The mantle door's Anthropic-shape downgrade target: whichever configured
    value is already in the bare form this door needs."""
    if not _NATIVE_ID.match(FALLBACK_MODEL) and not _is_openai_id(FALLBACK_MODEL):
        return FALLBACK_MODEL
    return MANTLE_FALLBACK_MODEL


def _default_openai_fallback() -> str:
    """The OpenAI shape's downgrade target: the cheapest id that shape serves.
    Ordered by the catalog, whose env default lists gpt-oss-120b first, so the
    configured value wins and the catalog only supplies a last resort."""
    if OPENAI_FALLBACK_MODEL:
        return OPENAI_FALLBACK_MODEL
    openai = _available_models()["openai"]
    return openai[-1] if openai else ""


def _all_available_models() -> list[str]:
    """Flat allowlist: every id any door offers. Seeded into POLICY items so a
    pick from either dropdown is not refused by the allowlist before the door
    ever sees it."""
    catalog = _available_models()
    return sorted({m for models in catalog.values() for m in models})


MY_EVENTS_MAX_ROWS = 20


def _resp(status: int, body: Any) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _config_payload() -> dict:
    return {
        "region": _REGION,
        "gatewayUrl": GATEWAY_URL,
        "inferenceUrl": INFERENCE_BASE_URL,
        "bedrockRuntimeUrl": BEDROCK_RUNTIME_BASE_URL,
        "tableName": governance.TABLE_NAME,
        "dataSource": "dynamodb",
        "primaryModel": PRIMARY_MODEL,
        "fallbackModel": _default_native_fallback(),
        "mantleFallbackModel": _default_mantle_fallback(),
        "openaiFallbackModel": _default_openai_fallback(),
        # The window label the meter shows ("<user>'s day" / week / ...).
        "budgetWindow": governance.BUDGET_WINDOW,
        # Per-door catalogs so the chat picker and the fallback picker offer
        # only ids the selected door actually serves.
        "availableModels": _available_models(),
    }


# True when this module is running inside Lambda, where API Gateway's JWT
# authorizer is in front of every route and always supplies verified claims.
# Set by the Lambda runtime itself, so it cannot be spoofed by a caller.
_IN_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def _decode_jwt_claims(token: str) -> dict:
    """Decode a JWT payload WITHOUT verifying its signature, to read
    sub/username when no authorizer context is present. That only happens on a
    local run; deployed, the JWT authorizer has already verified the token and
    put the claims in the request context, so this path is never taken and
    _caller refuses to take it (see _IN_LAMBDA).

    An unverified decode trusts whatever the caller sent, so if a route were
    added or reconfigured without an authorizer, this function alone would let
    anyone assume any sub by hand-writing a token. Failing closed in Lambda
    surfaces that mistake as a 401 rather than as silent impersonation."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, binascii.Error, json.JSONDecodeError):
        return {}


def _caller(event: dict) -> dict:
    """Resolve the signed-in user from the request: sub and username. The
    API sits behind a Cognito JWT authorizer, so claims arrive in the
    request context. Outside Lambda only, fall back to decoding the
    Authorization header so the handler can be driven locally; in Lambda the
    absence of authorizer claims is treated as no caller at all, which the
    routes turn into a 401."""
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else auth.strip()
    claims = (
        ((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}
    ).get("claims") or {}
    if not claims and not _IN_LAMBDA:
        claims = _decode_jwt_claims(token)
    sub = claims.get("sub", "")
    username = claims.get("username") or claims.get("cognito:username") or ""
    return {"token": token, "sub": sub, "username": username}


def _resolve_persona(caller: dict) -> dict:
    """Map the signed-in user to roster metadata (name, role, default budget)
    for display and default-policy seeding; fall back to a generic record."""
    return cognito.persona_by_username(caller.get("username", "")) or cognito.generic_persona(
        caller.get("username") or (caller.get("sub", "")[:8] or "user")
    )


def _persona_by_sub(sub: str) -> dict | None:
    """Roster persona whose Cognito sub matches, or None for an off-roster
    user. Resolves each roster user's sub via the cognito module's cache."""
    if not sub:
        return None
    for persona in cognito.PERSONAS:
        try:
            if cognito.ensure_user(persona) == sub:
                return persona
        except Exception as exc:
            # A Cognito throttle or a missing permission must not fail the whole
            # request, but it must not vanish either: skipping silently would
            # make an on-roster caller look off-roster with nothing to explain
            # why.
            _warn(
                "persona_lookup_skipped",
                persona_id=persona.get("id"),
                error=type(exc).__name__,
            )
            continue
    return None


def _ensure_default_policy(persona: dict, sub: str) -> dict:
    policy = governance.get_policy(sub)
    if policy is not None:
        # An existing item is authoritative, including a zero budget. Seed
        # only when the item is missing.
        return policy
    defaults = persona["defaults"]
    governance.put_policy(
        sub,
        budget_tokens=defaults["budgetTokens"],
        downgrade_at_tokens=defaults["downgradeAtTokens"],
        fallback_model=_default_native_fallback(),
        fallback_model_mantle=_default_mantle_fallback(),
        fallback_model_openai=_default_openai_fallback(),
        blocked=False,
        allowed_models=_all_available_models(),
    )
    return governance.get_policy(sub) or {}


def _persona_state(persona: dict, sub: str | None = None) -> dict:
    if not sub:
        sub = cognito.ensure_user(persona)
    policy = _ensure_default_policy(persona, sub)
    usage = governance.get_usage(sub)
    return {
        "persona": {
            "id": persona["id"],
            "name": persona["name"],
            "role": persona["role"],
            "sub": sub,
        },
        "policy": policy,
        "usage": usage,
    }


def _get_personas(caller: dict) -> dict:
    persona = _resolve_persona(caller)
    return {
        "personas": [_persona_state(persona, caller.get("sub") or None)],
        # The browser invokes the runtimes directly, so runtime availability
        # is a frontend build setting (VITE_*_RUNTIME_ARN), not a backend one.
        "capabilities": {"strands": True, "langgraph": True, "claudecode": False},
        "config": _config_payload(),
    }


def _get_policy(caller: dict) -> dict:
    persona = _resolve_persona(caller)
    return _persona_state(persona, caller.get("sub") or None)


def _put_policy(caller: dict, body: dict) -> dict:
    persona = _resolve_persona(caller)
    sub = caller.get("sub") or cognito.ensure_user(persona)
    budget = int(body["budgetTokens"])
    downgrade_at = int(body["downgradeAtTokens"])
    current = governance.get_policy(sub) or {}
    # Omitting "blocked" keeps whatever the item already says. Defaulting to
    # False instead would let a request that only changes the budget silently
    # unblock a user an operator had blocked.
    blocked = bool(body.get("blocked", current.get("blocked", False)))
    if budget < 0 or downgrade_at < 0:
        raise ValueError("budgetTokens and downgradeAtTokens must be >= 0")
    # A downgrade target must be spelled for the door AND the wire shape the
    # request came in on, so there are three of them. The passthrough door
    # needs the regional inference-profile form; the mantle door's Anthropic
    # Messages shape needs a bare provider id; its OpenAI shapes serve a
    # disjoint catalog and answer 400 "does not support the
    # '/v1/chat/completions' API" for a Claude id. Writing the
    # wrong form is invisible until a downgrade actually fires and then fails
    # at the door, so a value the caller sent in the wrong form is refused
    # here. A wrong form merely inherited from an older policy item is
    # repaired instead of refused: an item may hold one bare id across
    # several fields, and that should not make every later edit fail.
    _FORM_HELP = {
        "native": "a regional inference-profile id (us./global. prefix) for "
        "the /bedrock-runtime door",
        "mantle": "a bare provider id (no us./global. prefix) that the "
        "/inference door's Anthropic Messages shape serves",
        "openai": "an id the /inference door's OpenAI shape serves (see "
        "config.availableModels.openai)",
    }

    def _resolve(key: str, saved: str, default: str, want: str) -> str:
        sent = str(body.get(key) or "")
        if sent:
            if _model_form(sent) != want:
                raise ValueError(f"{key} must be {_FORM_HELP[want]}")
            return sent
        if saved and _model_form(saved) == want:
            return saved
        return default

    fallback = _resolve(
        "fallbackModel",
        str(current.get("fallbackModel") or ""),
        _default_native_fallback(),
        want="native",
    )
    fallback_mantle = _resolve(
        "fallbackModelMantle",
        str(current.get("fallbackModelMantle") or ""),
        _default_mantle_fallback(),
        want="mantle",
    )
    fallback_openai = _resolve(
        "fallbackModelOpenai",
        str(current.get("fallbackModelOpenai") or ""),
        _default_openai_fallback(),
        want="openai",
    )
    # An existing allowlist is preserved rather than refreshed to the whole
    # catalog. Rewriting it here would mean any budget edit silently re-widens a
    # narrowed allowlist back to every model the account offers, which is the
    # opposite of what an operator who narrowed it asked for. A missing item is
    # the only case that needs a starting value.
    saved_allowed = [
        model for model in (current.get("allowedModels") or []) if isinstance(model, str)
    ]
    # The interceptor refuses the whole policy with 503 policy_invalid when any
    # fallback sits outside allowed_models, so all three are unioned in
    # explicitly rather than assumed to be in the allowlist.
    allowed_models = [
        *(saved_allowed or _all_available_models()),
        fallback,
        fallback_mantle,
        fallback_openai,
    ]
    governance.put_policy(
        sub,
        budget_tokens=budget,
        downgrade_at_tokens=downgrade_at,
        fallback_model=fallback,
        fallback_model_mantle=fallback_mantle,
        fallback_model_openai=fallback_openai,
        blocked=blocked,
        allowed_models=allowed_models,
    )
    return _persona_state(persona, sub)


def _get_my_events(caller: dict, query: dict) -> dict:
    """REQ admission rows the gateway wrote for the signed-in user, newest
    last. Optional ?since=<epoch seconds> filters to rows created at or after
    it; capped at the newest MY_EVENTS_MAX_ROWS rows. The response key is
    "events" for frontend compatibility, but these are REQ admission rows: the
    gateway records no EVENT items on this path."""
    sub = caller.get("sub") or ""
    if not sub:
        raise ValueError("missing caller identity")
    since_raw = (query or {}).get("since")
    since = int(since_raw) if since_raw not in (None, "") else None
    events = governance.events_for_user(sub)  # oldest first
    if since is not None:
        events = [e for e in events if e["createdAt"] >= since]
    return {"events": events[-MY_EVENTS_MAX_ROWS:]}


def _fleet_usage_rows(requests: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """Per-user daily aggregate rows plus a sub->persona lookup.

    The by-user rollup must cover every user who appears in the request feed,
    not just the static roster: the signed-in demo user (and any real SSO user)
    is off-roster, so iterating PERSONAS alone would hide their spend in By-user
    while By-request shows it, and would make the stat tiles undercount. We
    union the roster subs with the distinct user_ids already present in the REQ
    feed passed in (no extra scan) and emit a row per sub. Off-roster subs get a
    synthetic persona labeled by Cognito username when resolvable, else the
    short sub; that same synthetic entry lets _label_requests tag their REQ rows
    consistently."""
    sub_to_persona: dict[str, dict] = {}
    ordered_subs: list[str] = []

    # Roster personas first, in roster order.
    for persona in cognito.PERSONAS:
        try:
            sub = cognito.ensure_user(persona)
        except Exception as exc:
            # The persona drops out of the fleet view for this call. Say so,
            # otherwise a throttled Cognito looks identical to a persona that
            # has never spent anything.
            _warn(
                "fleet_row_skipped",
                persona_id=persona.get("id"),
                error=type(exc).__name__,
            )
            continue
        if sub in sub_to_persona:
            continue
        sub_to_persona[sub] = persona
        ordered_subs.append(sub)

    # Then any off-roster user seen in the request feed.
    for req in requests:
        sub = req.get("userId") or ""
        if not sub or sub in sub_to_persona:
            continue
        label = cognito.username_for_sub(sub) or sub[:8]
        sub_to_persona[sub] = {"id": sub, "name": label, "role": "Signed-in user"}
        ordered_subs.append(sub)

    rows: list[dict] = []
    for sub in ordered_subs:
        persona = sub_to_persona[sub]
        policy = governance.get_policy(sub) or {}
        usage = governance.get_usage(sub)
        rows.append(
            {
                "personaId": persona["id"],
                "name": persona["name"],
                "sub": sub,
                "budgetTokens": policy.get("budgetTokens", 0),
                "downgradeAtTokens": policy.get("downgradeAtTokens", 0),
                "blocked": policy.get("blocked", False),
                "usage": usage,
            }
        )
    return rows, sub_to_persona


def _settlement_status(admitted_at: int, usage_updated_at: int) -> str:
    """The request's async-debit state. The token cost lands on the user's
    USAGE aggregate seconds to minutes after admission; the USAGE item's
    updatedAt advancing past the admission timestamp is the signal that a
    debit has landed. Until then the row is "settling"."""
    return "settled" if usage_updated_at > admitted_at else "settling"


def _usage_cache() -> Callable[[str, str], dict]:
    """Memoize governance.get_usage by (user, date) so labeling a feed of REQ
    rows costs one USAGE read per distinct user per day, not one per row."""
    cache: dict[tuple[str, str], dict] = {}

    def lookup(sub: str, date: str) -> dict:
        key = (sub, date or "")
        if key not in cache:
            cache[key] = governance.get_usage(sub, date=date or None)
        return cache[key]

    return lookup


def _label_requests(requests: list[dict], sub_to_persona: dict[str, dict]) -> None:
    """Tag each REQ feed row with its persona and its settlement status.

    Settlement is computed per row against the row user's USAGE aggregate
    (cached), so a row whose async debit already landed is shown settled even
    though the REQ item lingers until its TTL expires."""
    usage_of = _usage_cache()
    for req in requests:
        persona = sub_to_persona.get(req["userId"])
        req["personaId"] = persona["id"] if persona else None
        req["personaName"] = persona["name"] if persona else req["userId"][:8]
        if req.get("settledInBand"):
            # An EVENT row is a completed in-band settlement (the downgrade
            # short-circuit); its debit landed in the same transaction that
            # wrote the row, so it is settled by construction.
            req["usageUpdatedAt"] = req["createdAt"]
            req["settlementStatus"] = "settled"
            continue
        usage = usage_of(req["userId"], req.get("usageDate", ""))
        usage_updated_at = usage.get("updatedAt", 0)
        req["usageUpdatedAt"] = usage_updated_at or None
        req["settlementStatus"] = _settlement_status(
            req["createdAt"], usage_updated_at
        )


def _get_fleet(query: dict | None = None) -> dict:
    """Fleet view. ?groupBy=user (default) returns per-user aggregates;
    ?groupBy=request returns the raw REQ admission feed newest-first. Both
    responses carry usage rows and config. "events" aliases "requests" so
    frontends that read "events" keep working."""
    group_by = ((query or {}).get("groupBy") or "user").strip().lower()
    if group_by not in ("user", "request"):
        raise ValueError("groupBy must be 'user' or 'request'")
    # Fetch the feed once and reuse it for the by-user rollup so off-roster
    # users seen in the feed also get a per-user row (no second scan).
    requests = governance.recent_requests(limit=50)
    rows, sub_to_persona = _fleet_usage_rows(requests)
    _label_requests(requests, sub_to_persona)
    return {
        "groupBy": group_by,
        "requests": requests,
        # Alias kept so old frontends that read "events" do not break.
        "events": requests,
        "usage": rows,
        "config": _config_payload(),
    }


def _get_timeline(query: dict) -> dict:
    """Settlement timeline for one request. Returns the REQ item's admission
    timestamp and whether the user's USAGE debit has advanced past that
    admission snapshot yet (the token cost settles asynchronously, seconds to
    minutes after admission).

    settled is True once the REQ admission item is gone (settled in-band or
    expired under its TTL) or the user's USAGE item was updated after
    admission; while the
    REQ item is still present and USAGE has not moved, the request is
    "settling". usageUpdatedAt is the USAGE item's last-mutation epoch."""
    request_id = (query or {}).get("requestId") or ""
    since_raw = (query or {}).get("since") or ""
    caller_sub = (query or {}).get("_callerSub") or ""
    if not request_id and since_raw and caller_sub:
        # Fallback for clients that never learn the gateway request id (the
        # runtime containers do not echo it): resolve the caller's newest
        # REQ admission at or after `since`. Turns are sequential per user
        # in the demo, so newest-since is unambiguous.
        try:
            since = int(float(since_raw))
        except ValueError:
            raise ValueError("invalid since") from None
        rows = governance.requests_for_user(caller_sub)  # oldest first
        match = next((r for r in reversed(rows) if r["createdAt"] >= since), None)
        if match is None:
            return {
                "requestId": None,
                "found": False,
                "settled": False,
                "settlementStatus": "pending_admission",
                "admittedAt": None,
                "usageUpdatedAt": None,
            }
        request_id = match["requestId"]
    if not request_id:
        raise ValueError("missing requestId (or since)")
    req = governance.get_request(request_id)
    if req is None:
        # No live REQ item: it settled in-band and was deleted, or expired
        # under its TTL. Treat as settled; the token cost is on the aggregate.
        return {
            "requestId": request_id,
            "found": False,
            "settled": True,
            "settlementStatus": "settled",
            "admittedAt": None,
            "usageUpdatedAt": None,
        }
    usage = governance.get_usage(req["userId"], date=req.get("usageDate") or None)
    admitted_at = req["createdAt"]
    usage_updated_at = usage.get("updatedAt", 0)
    status = _settlement_status(admitted_at, usage_updated_at)
    persona = _persona_by_sub(req["userId"])
    return {
        "requestId": request_id,
        "found": True,
        "settled": status == "settled",
        "settlementStatus": status,
        "userId": req["userId"],
        "personaId": persona["id"] if persona else None,
        "personaName": persona["name"] if persona else req["userId"][:8],
        "originalModel": req["originalModel"],
        "effectiveModel": req["effectiveModel"],
        "downgraded": req["downgraded"],
        "streaming": req["streaming"],
        "interceptorMs": req.get("interceptorMs", 0),
        "admittedAt": admitted_at,
        "usageUpdatedAt": usage_updated_at or None,
        "usage": usage,
    }


def lambda_handler(event, _context):
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "GET")
    path = event.get("rawPath", "/")
    body = json.loads(event.get("body") or "{}")
    caller = _caller(event)

    # Every route below is per-user, so a request with no resolved subject has
    # no meaning: it would key the ledger on an empty sub and hand back a
    # generic persona. Deployed, the JWT authorizer guarantees a sub, so this
    # only fires if a route is ever reachable without one, and it fails closed.
    if not caller.get("sub"):
        return _resp(401, {"error": "unauthorized: no verified subject claim"})

    try:
        if path == "/personas" and method == "GET":
            return _resp(200, _get_personas(caller))
        if path == "/policy" and method == "GET":
            return _resp(200, _get_policy(caller))
        if path == "/policy" and method == "PUT":
            return _resp(200, _put_policy(caller, body))
        if path == "/events/mine" and method == "GET":
            return _resp(200, _get_my_events(caller, event.get("queryStringParameters") or {}))
        if path == "/fleet" and method == "GET":
            return _resp(200, _get_fleet(event.get("queryStringParameters") or {}))
        if path == "/timeline" and method == "GET":
            timeline_query = dict(event.get("queryStringParameters") or {})
            # The since-fallback resolves against the AUTHENTICATED caller's
            # requests only; the sub comes from the verified JWT, never from
            # the query string.
            timeline_query["_callerSub"] = caller.get("sub", "")
            return _resp(200, _get_timeline(timeline_query))
        return _resp(404, {"error": f"no route {method} {path}"})
    except KeyError as error:
        # The message is the missing body field's name, and ValueError below is
        # only ever raised by this module's own validation, so both are safe to
        # hand back: they describe the caller's request, not this deployment.
        return _resp(400, {"error": f"missing required field: {error}"})
    except ValueError as error:
        return _resp(400, {"error": str(error)})
    except Exception as error:
        # Anything reaching here is unexpected, which in practice means a
        # botocore exception whose message quotes the table name, the operation
        # ARN, and sometimes the account id. Those go to CloudWatch, where an
        # operator can read them, and never into an HTTP body.
        _LOG.exception("unhandled error serving %s %s", method, path)
        return _resp(500, {"error": "internal error", "type": type(error).__name__})
