# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reads and writes for the shared governance DynamoDB table.

The table belongs to the infra module; this app consumes it only through
its documented item shapes, and only in the directions the story allows:

  POLICY#<sub>              written here (the policy editor), read by the gateway
  USAGE#<sub>#<YYYY-MM-DD>  written by the gateway, read here (meter, fleet)
  REQ#<request_id>          written by the gateway at admission, read here (feed)
  EVENT#<request_id>        per-request settlement row with actual token counts

Data model: the interceptor writes a REQ#<request_id>
item when it admits a request. Settlement then replaces that row with an
EVENT#<request_id> row carrying the actual token counts and debits the
per-user USAGE#<sub>#<date> aggregate, in one transaction: in-band by the
interceptor for downgrade replays, asynchronously by the attribution Lambda
from invocation-log records for every other /bedrock-runtime call. The
/inference (mantle) door has no per-request records; its usage reconciles
into USAGE from project metrics on a schedule. A request is "settling" while
its REQ row still stands.

The app never writes usage, request, or event items. Enforcement and
accounting happen on the gateway path; this module only renders what the
gateway recorded.
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any

import boto3

TABLE_NAME = os.environ.get("TABLE_NAME", "")
_REGION = os.environ.get("AWS_REGION", "us-east-1")
# Downgrade target for the mantle (/inference) door: bare provider id, the
# only form that door accepts (the passthrough door takes the prefixed form).
MANTLE_FALLBACK_MODEL = os.environ.get(
    "MANTLE_FALLBACK_MODEL", "anthropic.claude-haiku-4-5"
)
# Downgrade target for the mantle door's OpenAI shapes
# (/v1/chat/completions, /v1/responses). Those shapes serve only
# OpenAI-family ids, so the Anthropic-shape target above cannot stand in:
# rewriting a Chat Completions request to a Claude id returns
# 400 "does not support the '/v1/chat/completions' API".
OPENAI_FALLBACK_MODEL = os.environ.get("OPENAI_FALLBACK_MODEL", "gpt-oss-20b")

_ddb = boto3.client("dynamodb", region_name=_REGION)


def _n(item: dict, key: str) -> int:
    value = item.get(key, {})
    try:
        return int(value.get("N", "0")) if isinstance(value, dict) else 0
    except (TypeError, ValueError):
        return 0


def _s(item: dict, key: str) -> str:
    value = item.get(key, {})
    return value.get("S", "") if isinstance(value, dict) else ""


def _b(item: dict, key: str) -> bool:
    value = item.get(key, {})
    return bool(value.get("BOOL", False)) if isinstance(value, dict) else False


# The budget window the governance stack is deployed with; must match the infra
# module's BUDGET_WINDOW so the meter reads the bucket the gateway writes.
# Local copy of the infra window-key logic (the app deploys separately).
BUDGET_WINDOW = os.environ.get("BUDGET_WINDOW", "day").strip().lower()
_WINDOW_FORMATS = {
    "hour": "%Y-%m-%dT%H",
    "day": "%Y-%m-%d",
    "week": "%G-W%V",
    "month": "%Y-%m",
}


def today() -> str:
    """The current budget-window bucket token (named for the day default)."""
    fmt = _WINDOW_FORMATS.get(BUDGET_WINDOW, _WINDOW_FORMATS["day"])
    return dt.datetime.now(dt.timezone.utc).strftime(fmt)


def get_policy(sub: str) -> dict[str, Any] | None:
    resp = _ddb.get_item(TableName=TABLE_NAME, Key={"pk": {"S": f"POLICY#{sub}"}})
    item = resp.get("Item")
    if not item:
        return None
    allowed = item.get("allowed_models", {}).get("SS", [])
    return {
        "budgetTokens": _n(item, "budget_tokens"),
        "downgradeAtTokens": _n(item, "downgrade_at_tokens"),
        "fallbackModel": _s(item, "fallback_model"),
        # The mantle door's own downgrade target. Exposed so the editor can
        # show and set both doors' targets instead of writing one of them
        # blind (the two doors take different id forms, so a single field
        # cannot be correct for both).
        "fallbackModelMantle": _s(item, "fallback_model_mantle"),
        # The same door's OpenAI shape needs a third target: its catalog and
        # the Anthropic shape's catalog are disjoint.
        "fallbackModelOpenai": _s(item, "fallback_model_openai"),
        "blocked": _b(item, "blocked"),
        "allowedModels": sorted(allowed),
    }


def put_policy(
    sub: str,
    *,
    budget_tokens: int,
    downgrade_at_tokens: int,
    fallback_model: str,
    blocked: bool,
    allowed_models: list[str],
    fallback_model_mantle: str | None = None,
    fallback_model_openai: str | None = None,
) -> None:
    """Update the POLICY#<sub> fields this editor owns, preserving the rest.

    The infra deploy seeds fields the app never edits (workspace_id for
    mantle attribution, rate_limit_per_minute). A PutItem here would erase
    them and silently break those features, so this is an UpdateItem on the
    editable fields only. A PutItem would erase fallback_model_mantle and the
    other seeded fields, so mantle downgrades would start failing.

    Each gateway door accepts only its own model-id form, so the mantle
    (/inference) door has its own bare-id downgrade target. Pass
    fallback_model_mantle to set it; omit it and any existing value is kept
    (seeded from MANTLE_FALLBACK_MODEL the first time). The mantle door's
    OpenAI shapes need a third target for the same reason one level down:
    they serve a disjoint catalog from its Anthropic shape.
    """
    set_mantle = (
        ":mantle_fallback"
        if fallback_model_mantle
        else "if_not_exists(fallback_model_mantle, :mantle_fallback)"
    )
    set_openai = (
        ":openai_fallback"
        if fallback_model_openai
        else "if_not_exists(fallback_model_openai, :openai_fallback)"
    )
    _ddb.update_item(
        TableName=TABLE_NAME,
        Key={"pk": {"S": f"POLICY#{sub}"}},
        UpdateExpression=(
            "SET item_type = :t, user_id = :u, blocked = :b, "
            "budget_tokens = :budget, downgrade_at_tokens = :downgrade, "
            "fallback_model = :fallback, allowed_models = :allowed, "
            f"fallback_model_mantle = {set_mantle}, "
            f"fallback_model_openai = {set_openai}"
        ),
        ExpressionAttributeValues={
            ":t": {"S": "POLICY"},
            ":u": {"S": sub},
            ":b": {"BOOL": bool(blocked)},
            ":budget": {"N": str(int(budget_tokens))},
            ":downgrade": {"N": str(int(downgrade_at_tokens))},
            ":fallback": {"S": fallback_model},
            ":allowed": {"SS": sorted(set(allowed_models))},
            ":mantle_fallback": {
                "S": fallback_model_mantle or MANTLE_FALLBACK_MODEL
            },
            ":openai_fallback": {
                "S": fallback_model_openai or OPENAI_FALLBACK_MODEL
            },
        },
    )


def get_usage(sub: str, date: str | None = None) -> dict[str, Any]:
    """Read the USAGE#<sub>#<date> aggregate the gateway maintains.

    debitTokens/inputTokens/outputTokens advance asynchronously
    (invocation-log or mantle Lambda), so this aggregate
    is the settlement signal the request feed and timeline watch. updatedAt
    is the epoch of the last in-band mutation (the admission write or an
    in-band finalize). Both async debit paths also set updated_at; it is the
    settlement heartbeat that _settlement_status reads.
    """
    date = date or today()
    resp = _ddb.get_item(TableName=TABLE_NAME, Key={"pk": {"S": f"USAGE#{sub}#{date}"}})
    item = resp.get("Item") or {}
    return {
        "date": date,
        "debitTokens": _n(item, "budget_debit_tokens"),
        "inputTokens": _n(item, "input_tokens"),
        "outputTokens": _n(item, "output_tokens"),
        "cacheReadTokens": _n(item, "cache_read_input_tokens"),
        "cacheWriteTokens": _n(item, "cache_creation_input_tokens"),
        "actualTokens": _n(item, "actual_tokens"),
        "acceptedRequests": _n(item, "accepted_requests"),
        "updatedAt": _n(item, "updated_at"),
    }


def _scan(filter_expression: str, names: dict, values: dict) -> list[dict]:
    items: list[dict] = []
    kwargs: dict[str, Any] = {
        "TableName": TABLE_NAME,
        "FilterExpression": filter_expression,
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }
    while True:
        resp = _ddb.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def _request_summary(item: dict) -> dict[str, Any]:
    """Project a REQ#<request_id> admission item to the feed row shape.

    A REQ item exists only while the request is in flight or awaiting the
    async debit; no per-request output tokens are recorded,
    so the settled token cost lands on the user's USAGE aggregate, not here.
    downgraded is derived: the interceptor rewrote the model at admission.
    """
    original = _s(item, "original_model")
    effective = _s(item, "effective_model")
    return {
        "requestId": _s(item, "request_id"),
        "userId": _s(item, "user_id"),
        "originalModel": original,
        "effectiveModel": effective,
        "downgraded": bool(original and effective and original != effective),
        "requestedMaxTokens": _n(item, "requested_max_tokens"),
        "inputEstimateTokens": _n(item, "input_estimate_tokens"),
        "action": _s(item, "action"),
        "streaming": _b(item, "streaming"),
        "usageDate": _s(item, "usage_date"),
        "createdAt": _n(item, "created_at"),
        # Self-reported by the interceptor: wall-clock ms it spent up to the
        # admission write. 0 = written by an older interceptor build.
        "interceptorMs": _n(item, "interceptor_ms"),
        # REQ rows never carry token counts (they settle
        # asynchronously onto USAGE); zeros keep the feed row shape
        # uniform with in-band EVENT rows so client-side sums stay numeric.
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "totalTokens": 0,
        "settledInBand": False,
    }


def _event_summary(item: dict) -> dict[str, Any]:
    """Project an EVENT#<request_id> settlement row to the feed row shape.

    The interceptor writes EVENT rows for requests it settles in-band (the
    downgrade short-circuit path), deleting the REQ row in the same
    transaction. Without these rows the feed loses exactly the downgraded
    turns, so the model badge would keep showing the requested model.
    Settled rows carry real token counts; REQ rows never do.
    """
    original = _s(item, "original_model")
    effective = _s(item, "effective_model")
    return {
        "requestId": _s(item, "request_id"),
        "userId": _s(item, "user_id"),
        "originalModel": original,
        "effectiveModel": effective,
        "downgraded": bool(original and effective and original != effective),
        "action": _s(item, "action"),
        "streaming": _b(item, "streaming"),
        "usageDate": _s(item, "usage_date"),
        "createdAt": _n(item, "created_at"),
        "inputTokens": _n(item, "input_tokens"),
        "outputTokens": _n(item, "output_tokens"),
        "cacheReadTokens": _n(item, "cache_read_input_tokens"),
        "cacheWriteTokens": _n(item, "cache_creation_input_tokens"),
        "totalTokens": _n(item, "total_tokens"),
        "statusCode": _n(item, "status_code"),
        "finalizationReason": _s(item, "finalization_reason"),
        "settledInBand": True,
    }


def recent_requests(limit: int = 50) -> list[dict[str, Any]]:
    """Newest admission and in-band settlement rows across all users, newest
    first: live REQ items plus EVENT items (a request settled in-band has its
    REQ row deleted and an EVENT row written in the same transaction, so the
    two sets never double-count one request). A Scan is fine at demo scale. If you adapt this for production,
    # replace it: add a GSI on user_id (or query by usage_pk) so reads do not
    # grow with total table size.
    """
    req_items = _scan(
        "begins_with(#pk, :req)",
        {"#pk": "pk"},
        {":req": {"S": "REQ#"}},
    )
    event_items = _scan(
        "begins_with(#pk, :ev)",
        {"#pk": "pk"},
        {":ev": {"S": "EVENT#"}},
    )
    rows = [_request_summary(i) for i in req_items] + [
        _event_summary(i) for i in event_items
    ]
    rows.sort(key=lambda x: x["createdAt"], reverse=True)
    return rows[:limit]


def requests_for_user(sub: str) -> list[dict[str, Any]]:
    """The caller's REQ admission rows plus in-band EVENT settlements,
    oldest first (the per-user feed served at /events/mine)."""
    req_items = _scan(
        "begins_with(#pk, :req) AND #u = :u",
        {"#pk": "pk", "#u": "user_id"},
        {":req": {"S": "REQ#"}, ":u": {"S": sub}},
    )
    event_items = _scan(
        "begins_with(#pk, :ev) AND #u = :u",
        {"#pk": "pk", "#u": "user_id"},
        {":ev": {"S": "EVENT#"}, ":u": {"S": sub}},
    )
    rows = [_request_summary(i) for i in req_items] + [
        _event_summary(i) for i in event_items
    ]
    rows.sort(key=lambda x: x["createdAt"])
    return rows


def get_request(request_id: str) -> dict[str, Any] | None:
    """Read one REQ#<request_id> admission item, or None if it has settled
    in-band and been deleted (or expired). Powers the /timeline hop view."""
    resp = _ddb.get_item(
        TableName=TABLE_NAME, Key={"pk": {"S": f"REQ#{request_id}"}}
    )
    item = resp.get("Item")
    return _request_summary(item) if item else None


# Backwards-compatible aliases: older frontends read EVENT items via these
# names. The feed merges REQ admission rows (requests still
# settling) with EVENT settlement rows (requests with actual counts).
def events_for_user(sub: str) -> list[dict[str, Any]]:
    return requests_for_user(sub)


def recent_events(limit: int = 50) -> list[dict[str, Any]]:
    return recent_requests(limit=limit)
