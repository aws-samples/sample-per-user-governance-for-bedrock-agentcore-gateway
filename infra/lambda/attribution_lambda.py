# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Attribution Lambda: debit the DynamoDB ledger from Bedrock invocation logs.

Receives batched log records from a CloudWatch Logs subscription filter. Each
record carries the actual token counts and the requestMetadata the interceptor
injected (user id and gateway request id). Every record settles through one
DynamoDB transaction: a conditional EVENT#<request_id> put, the per-user USAGE
debit, and the REQ admission row's deletion, committing together or not at all
(the same transaction the interceptor's in-band finalize uses).

The conditional EVENT put is what makes settlement exactly-once. CloudWatch
Logs subscription delivery is at-least-once, so a redelivered batch replays
the same records; their EVENT rows already exist, the puts fail their
condition, the transactions cancel whole, and nothing debits twice. The same
condition lets the two settlement pipes coexist: a downgraded call is settled
in-band by the interceptor (which writes the EVENT row first), so the
invocation-log record for the replayed call, which carries the same request
id, is recognized as settled and skipped. Each request therefore debits
exactly once, whichever pipe reaches the ledger first, and the invocation log
stays fully attributed, downgrades included.

The cost of exactly-once is one transaction per model call instead of one
ADD per user per batch. That also buys a per-request EVENT audit row for
every passthrough call, which the demo feed and the fleet view read.

This Lambda is invoked asynchronously by the subscription filter. It does not
sit on the request path and adds no latency to the client.
"""
import base64
import gzip
import json
import os
import random
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.types import TypeDeserializer

import window as _window

TABLE_NAME = os.environ["TABLE_NAME"]
# Must match the interceptor's BUDGET_WINDOW: the admission read and the
# settlement write have to agree on the counter a moment belongs to. The CDK
# threads the same value into both Lambdas.
BUDGET_WINDOW = os.environ.get("BUDGET_WINDOW", "day").strip().lower()
# Budget weight of cache tokens, percent of the input-token price. Must match
# the interceptor's values so both settlement paths debit identically.
CACHE_READ_WEIGHT_PCT = int(os.environ.get("CACHE_READ_WEIGHT_PCT", "10"))
CACHE_WRITE_WEIGHT_PCT = int(os.environ.get("CACHE_WRITE_WEIGHT_PCT", "125"))
if BUDGET_WINDOW not in _window.VALID_WINDOWS:
    raise ValueError(f"BUDGET_WINDOW must be one of {_window.VALID_WINDOWS}")
_USAGE_TTL_SECONDS = int(os.environ.get("USAGE_TTL_SECONDS", "34560000"))
_HWM_TTL_SECONDS = _USAGE_TTL_SECONDS
# EVENT settlement rows age out on the same clock as the interceptor's
# in-band settles, so the audit trail is uniform whichever pipe wrote it.
_EVENT_TTL_SECONDS = int(os.environ.get("EVENT_TTL_SECONDS", "7776000"))
# Bounded retry for a transient DynamoDB TransactionConflict on the hot
# per-user USAGE item (concurrent settles under one identity collide and
# clear on retry; a failed EVENT condition is a decision, never retried).
_TRANSACT_MAX_ATTEMPTS = 5
_TRANSACT_BACKOFF_BASE = 0.025
_TRANSACT_BACKOFF_CAP = 0.2
# Retry jitter. Drawn from SystemRandom rather than the shared Mersenne Twister
# so static analysis does not flag a non-cryptographic default; this path only
# runs after a conflict, so the cost is immaterial.
_jitter = random.SystemRandom()
_client = boto3.client("dynamodb")


def _s(v: str) -> dict:
    return {"S": v}


def _n(v: int) -> dict:
    return {"N": str(v)}


def _process_invocation_logs(event, context):
    raw = event.get("awslogs", {}).get("data")
    if not raw:
        return {"ok": False, "reason": "no_awslogs_data"}

    decoded = gzip.decompress(base64.b64decode(raw))
    payload = json.loads(decoded)

    if payload.get("messageType") == "CONTROL_MESSAGE":
        return {"ok": True, "reason": "control_message"}

    log_events = payload.get("logEvents", [])
    now = int(time.time())
    current_bucket = _window.window_key(now, BUDGET_WINDOW)

    processed = 0
    skipped = 0
    already_settled = 0
    errors = 0

    for le in log_events:
        msg = le.get("message", "")
        try:
            record = json.loads(msg)
        except (json.JSONDecodeError, TypeError):
            skipped += 1
            continue

        if record.get("schemaType") != "ModelInvocationLog":
            skipped += 1
            continue

        metadata = record.get("requestMetadata") or {}
        user_id = metadata.get("user")
        request_id = metadata.get("request_id") or record.get("requestId")

        if not user_id or not request_id:
            skipped += 1
            continue

        inp = record.get("input", {})
        out = record.get("output", {})
        usage = {
            "input_tokens": inp.get("inputTokenCount", 0),
            "output_tokens": out.get("outputTokenCount", 0),
            "cache_read": inp.get("cacheReadInputTokenCount", 0),
            "cache_write": inp.get("cacheWriteInputTokenCount", 0),
        }
        # Zero-token records are failed invocations (a downgrade replay's
        # drop-retry attempts leave ValidationException records, and they
        # carry the same requestMetadata as the attempt that finally
        # succeeded). Writing their EVENT row would mark the request settled
        # with nothing debited, so the successful record must win instead.
        if not any(usage.values()):
            skipped += 1
            continue

        outcome = _settle_record(
            user_id=user_id,
            request_id=request_id,
            model_id=str(record.get("modelId", "")),
            usage=usage,
            current_bucket=current_bucket,
            now=now,
        )
        if outcome == "settled":
            processed += 1
        elif outcome == "already_settled":
            already_settled += 1
        else:
            errors += 1

    result = {
        "ok": errors == 0,
        "processed": processed,
        "skipped": skipped,
        "already_settled": already_settled,
        "errors": errors,
    }
    if processed or already_settled or errors:
        print(json.dumps({"event": "attribution_batch", **result}))
    return result


def _settle_record(
    *,
    user_id: str,
    request_id: str,
    model_id: str,
    usage: dict,
    current_bucket: str,
    now: int,
) -> str:
    """Settle one invocation-log record exactly once.

    One transaction: a conditional EVENT#<request_id> put (the idempotency
    marker and the audit row), the per-user USAGE debit, and the REQ admission
    row's deletion, committing together or not at all. Returns "settled",
    "already_settled" (the EVENT row exists: an earlier delivery of this
    record, or the interceptor's in-band settle for a downgrade replay), or
    "error".

    The debit is the budget-weighted total: cache tokens count at their price
    weight, the same formula the interceptor's in-band settle uses, so both
    settlement paths debit identically. Raw per-field counts are recorded
    unweighted on both the EVENT row and the USAGE aggregate.
    """
    billable = (
        usage["input_tokens"]
        + usage["output_tokens"]
        + (usage["cache_read"] * CACHE_READ_WEIGHT_PCT) // 100
        + (usage["cache_write"] * CACHE_WRITE_WEIGHT_PCT) // 100
    )

    req_row = _request_row(request_id)
    bucket = req_row.get("usage_date") or current_bucket
    usage_pk = f"USAGE#{user_id}#{bucket}"

    event_item = {
        "pk": _s(f"EVENT#{request_id}"),
        "item_type": _s("EVENT"),
        "request_id": _s(request_id),
        "user_id": _s(user_id),
        "usage_pk": _s(usage_pk),
        "usage_date": _s(bucket),
        # The admission row knows what the client asked for and what the
        # interceptor decided; the log record only knows the model that ran.
        "original_model": _s(req_row.get("original_model") or model_id),
        "effective_model": _s(req_row.get("effective_model") or model_id),
        "action": _s(req_row.get("action") or "allow"),
        "streaming": {"BOOL": req_row.get("streaming", False)},
        "status_code": _n(200),
        "input_tokens": _n(usage["input_tokens"]),
        "output_tokens": _n(usage["output_tokens"]),
        "cache_read_input_tokens": _n(usage["cache_read"]),
        "cache_creation_input_tokens": _n(usage["cache_write"]),
        "total_tokens": _n(billable),
        "finalization_reason": _s("invocation_log"),
        "created_at": _n(now),
        "expires_at": _n(now + _EVENT_TTL_SECONDS),
    }
    transact_items = [
        {
            "Put": {
                "TableName": TABLE_NAME,
                "Item": event_item,
                "ConditionExpression": "attribute_not_exists(#pk)",
                "ExpressionAttributeNames": {"#pk": "pk"},
            }
        },
        {
            "Update": {
                "TableName": TABLE_NAME,
                "Key": {"pk": _s(usage_pk)},
                "UpdateExpression": (
                    # updated_at is the settlement heartbeat: the demo API's
                    # /timeline reads it to decide settled-vs-settling, so
                    # every debit must advance it.
                    "SET user_id = if_not_exists(user_id, :u), "
                    "expires_at = :exp, updated_at = :now "
                    "ADD budget_debit_tokens :total, "
                    "input_tokens :inp, output_tokens :out, "
                    "cache_read_input_tokens :cr, "
                    "cache_creation_input_tokens :cw"
                ),
                "ExpressionAttributeValues": {
                    ":u": _s(user_id),
                    ":total": _n(billable),
                    ":inp": _n(usage["input_tokens"]),
                    ":out": _n(usage["output_tokens"]),
                    ":cr": _n(usage["cache_read"]),
                    ":cw": _n(usage["cache_write"]),
                    ":exp": _n(now + _USAGE_TTL_SECONDS),
                    ":now": _n(now),
                },
            }
        },
        {
            # Settling replaces the admission row with the EVENT row, exactly
            # like the interceptor's in-band finalize, so the feed carries one
            # row per request at any moment. The delete is unconditional:
            # unlike finalize, this path can run after the REQ row's TTL, and a
            # missing row must not cancel the debit.
            "Delete": {
                "TableName": TABLE_NAME,
                "Key": {"pk": _s(f"REQ#{request_id}")},
            }
        },
    ]

    attempt = 0
    while True:
        try:
            _client.transact_write_items(TransactItems=transact_items)
            return "settled"
        except Exception as e:  # noqa: BLE001 -- classified below
            if _event_exists_cancellation(e):
                return "already_settled"
            attempt += 1
            if _is_transaction_conflict(e) and attempt < _TRANSACT_MAX_ATTEMPTS:
                time.sleep(
                    _jitter.uniform(
                        0, min(_TRANSACT_BACKOFF_CAP, _TRANSACT_BACKOFF_BASE * (2 ** attempt))
                    )
                )
                continue
            print(json.dumps({
                "event": "attribution_write_failed",
                "user_id": user_id,
                "request_id": request_id,
                "error": type(e).__name__,
                "total_tokens": billable,
            }))
            return "error"


def _event_exists_cancellation(error: Exception) -> bool:
    """True when the transaction cancelled because the EVENT put's condition
    failed: the request is already settled. The put is the first transact
    item, so only the first cancellation reason carries its verdict."""
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    details = response.get("Error") or {}
    if details.get("Code") != "TransactionCanceledException":
        return False
    reasons = response.get("CancellationReasons")
    if not isinstance(reasons, list) or not reasons:
        return False
    first = reasons[0]
    return isinstance(first, dict) and first.get("Code") == "ConditionalCheckFailed"


def _is_transaction_conflict(error: Exception) -> bool:
    """True only for a pure DynamoDB TransactionConflict cancellation, the
    transient collision between concurrent writers on the hot USAGE item."""
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    details = response.get("Error") or {}
    if details.get("Code") != "TransactionCanceledException":
        return False
    reasons = response.get("CancellationReasons")
    return isinstance(reasons, list) and any(
        isinstance(reason, dict) and reason.get("Code") == "TransactionConflict"
        for reason in reasons
    )


def _request_row(request_id: str) -> dict:
    """The admission row's settlement-relevant fields, empty when the row is
    gone. The REQ item stores the bucket the request was admitted against
    (usage_date), which keeps a debit that crosses a window boundary in the
    admitted window, plus the model pair and decision the EVENT row records.
    The row lives far longer (1h TTL) than log delivery takes (seconds), so
    the lookup nearly always hits; falling back to empty is honest for a
    record settling after the row expired."""
    try:
        response = _client.get_item(
            TableName=TABLE_NAME,
            Key={"pk": _s(f"REQ#{request_id}")},
            ProjectionExpression=(
                "usage_date, original_model, effective_model, #a, streaming"
            ),
            ExpressionAttributeNames={"#a": "action"},
        )
        item = response.get("Item") or {}
        fields: dict = {}
        for key in ("usage_date", "original_model", "effective_model", "action"):
            value = item.get(key, {})
            if isinstance(value, dict) and value.get("S"):
                fields[key] = value["S"]
        streaming = item.get("streaming", {})
        if isinstance(streaming, dict) and "BOOL" in streaming:
            fields["streaming"] = bool(streaming["BOOL"])
        return fields
    except Exception:
        return {}


# --- mantle metrics reconciliation ---

_NAMESPACE = "AWS/BedrockMantle"
_INPUT_METRIC = "TotalInputTokens"
_OUTPUT_METRIC = "TotalOutputTokens"
# Metric datapoints are read at 1-minute grain over the non-overlapping
# interval since the last run's cursor (see _reconcile_workspace). The lag
# keeps the window's trailing edge behind CloudWatch's ingestion delay so a
# datapoint that has not landed yet is read by the NEXT run instead of being
# skipped forever; 10 minutes is comfortably beyond the typical
# delivery lag of AWS/BedrockMantle metrics.
_METRIC_PERIOD_SECONDS = 60
_METRIC_LAG_SECONDS = 600
# First run for a workspace starts the cursor this far back rather than at
# the epoch, bounding the initial catch-up read.
_FIRST_RUN_LOOKBACK_SECONDS = 3600
# GetMetricStatistics returns at most 1,440 datapoints per call and drops the
# rest without an error, so at 1-minute grain a window wider than 24 hours would
# under-debit silently. Each run reads at most that many datapoints and advances
# the cursor only over what it read, so a backlog (the schedule paused, the
# function failing for a day) drains across consecutive runs instead of being
# lost. Reaching the clamp is logged, because it means this run debited less than
# the wall-clock gap suggests.
_MAX_DATAPOINTS_PER_CALL = 1440
_MAX_WINDOW_SECONDS = _MAX_DATAPOINTS_PER_CALL * _METRIC_PERIOD_SECONDS

_ddb = boto3.client("dynamodb")
_cw = boto3.client("cloudwatch")
_deserialize = TypeDeserializer().deserialize






def _reconcile_mantle(event, context):
    """Reconcile mantle project metrics into the ledger for every mapped user."""
    del event, context
    now = int(time.time())
    # The mantle door has no per-request join (project metrics are
    # aggregates), so its debits always land on the current bucket. The
    # boundary smear is bounded by the 5-minute schedule: negligible for
    # day and up, documented for the hour window.
    today = _window.window_key(now, BUDGET_WINDOW)
    reconciled = 0
    errors = 0

    for policy in _iter_workspace_policies():
        user_id = policy["user_id"]
        workspace_id = policy["workspace_id"]
        try:
            input_delta, output_delta, new_cursor = _reconcile_workspace(
                user_id, workspace_id, today, now
            )
            _log(
                "mantle_attribution",
                user_id=user_id,
                workspace_id=workspace_id,
                input_delta=input_delta,
                output_delta=output_delta,
                cursor=new_cursor,
            )
            reconciled += 1
        except Exception as exc:  # noqa: BLE001 -- one bad user must not stop the run
            errors += 1
            _log(
                "mantle_attribution_failed",
                user_id=user_id,
                workspace_id=workspace_id,
                error=type(exc).__name__,
            )

    result = {"reconciled": reconciled, "errors": errors}
    _log("mantle_attribution_run", **result)
    return result


def _iter_workspace_policies():
    """Yield {user_id, workspace_id} for every POLICY item with a workspace_id.

    POLICY items are keyed pk=POLICY#<user_id>; the user id is recovered from
    the key rather than a separate attribute so the scan projects only the two
    fields it needs.
    """
    paginator = _ddb.get_paginator("scan")
    pages = paginator.paginate(
        TableName=TABLE_NAME,
        FilterExpression=(
            "begins_with(pk, :prefix) AND attribute_exists(workspace_id) "
            "AND workspace_id <> :empty"
        ),
        ExpressionAttributeValues={
            ":prefix": {"S": "POLICY#"},
            ":empty": {"S": ""},
        },
        ProjectionExpression="pk, workspace_id",
    )
    for page in pages:
        for item in page.get("Items", []):
            pk = _deserialize(item["pk"])
            workspace_id = _deserialize(item["workspace_id"])
            if not isinstance(pk, str) or not pk.startswith("POLICY#"):
                continue
            user_id = pk[len("POLICY#"):]
            if not user_id or not isinstance(workspace_id, str) or not workspace_id:
                continue
            yield {"user_id": user_id, "workspace_id": workspace_id}


def _read_metric_sum(
    workspace_id: str, metric_name: str, start_epoch: int, end_epoch: int
) -> int:
    """Sum the AWS/BedrockMantle Project-dimension metric over
    [start_epoch, end_epoch). Half-open on aligned minute boundaries, so
    consecutive runs never double-read a datapoint and never skip one.

    Raises ValueError if the window would exceed what one GetMetricStatistics
    call can return; callers clamp first (see _MAX_WINDOW_SECONDS). The check is
    here rather than only at the call site so a future caller cannot reintroduce
    a silently truncated read."""
    if end_epoch <= start_epoch:
        return 0
    if end_epoch - start_epoch > _MAX_WINDOW_SECONDS:
        raise ValueError(
            f"metric window of {end_epoch - start_epoch}s exceeds the "
            f"{_MAX_DATAPOINTS_PER_CALL}-datapoint GetMetricStatistics limit "
            f"at a {_METRIC_PERIOD_SECONDS}s period; clamp the window first"
        )
    response = _cw.get_metric_statistics(
        Namespace=_NAMESPACE,
        MetricName=metric_name,
        Dimensions=[{"Name": "Project", "Value": workspace_id}],
        StartTime=datetime.fromtimestamp(start_epoch, timezone.utc),
        EndTime=datetime.fromtimestamp(end_epoch, timezone.utc),
        Period=_METRIC_PERIOD_SECONDS,
        Statistics=["Sum"],
    )
    total = 0.0
    for point in response.get("Datapoints", []):
        total += float(point.get("Sum", 0.0))
    return int(total)


def _minute_floor(epoch: int) -> int:
    return epoch - (epoch % 60)


def _reconcile_workspace(
    user_id: str,
    workspace_id: str,
    today: str,
    now: int,
) -> tuple[int, int, int]:
    """Debit the tokens emitted in the non-overlapping interval since this
    workspace's cursor, then advance the cursor to the interval's end.

    The cursor (metric_cursor_at on the MANTLE_HWM item) marks the exclusive
    end of the last interval already debited. Each run reads
    [cursor, now - lag) on minute boundaries: non-overlapping, so a plain ADD
    is correct without any high-water mark, and the lag keeps the trailing
    edge behind CloudWatch's ingestion delay so late datapoints fall into the
    next run's interval instead of being missed. Returns
    (input_delta, output_delta, new_cursor)."""
    raw_cursor = _get_cursor(workspace_id)
    cursor = raw_cursor
    window_end = _minute_floor(now - _METRIC_LAG_SECONDS)
    if cursor <= 0:
        cursor = _minute_floor(now - _METRIC_LAG_SECONDS - _FIRST_RUN_LOOKBACK_SECONDS)
    if window_end <= cursor:
        return 0, 0, cursor
    if window_end - cursor > _MAX_WINDOW_SECONDS:
        clamped_end = cursor + _MAX_WINDOW_SECONDS
        _log(
            "mantle_metric_window_clamped",
            workspace_id=workspace_id,
            cursor=cursor,
            requested_end=window_end,
            clamped_end=clamped_end,
            backlog_seconds=window_end - clamped_end,
        )
        window_end = clamped_end

    input_delta = _read_metric_sum(workspace_id, _INPUT_METRIC, cursor, window_end)
    output_delta = _read_metric_sum(workspace_id, _OUTPUT_METRIC, cursor, window_end)
    total_delta = input_delta + output_delta

    usage_pk = f"USAGE#{user_id}#{today}"
    if total_delta > 0:
        _ddb.update_item(
            TableName=TABLE_NAME,
            Key={"pk": _s(usage_pk)},
            UpdateExpression=(
                # updated_at is the settlement heartbeat the demo API's
                # /timeline reads to decide settled-vs-settling.
                "SET user_id = if_not_exists(user_id, :u), "
                "item_type = if_not_exists(item_type, :t), "
                "expires_at = :exp, updated_at = :now "
                "ADD budget_debit_tokens :total, "
                "input_tokens :inp, output_tokens :out"
            ),
            ExpressionAttributeValues={
                ":u": _s(user_id),
                ":t": _s("USAGE"),
                ":total": _n(total_delta),
                ":inp": _n(input_delta),
                ":out": _n(output_delta),
                ":exp": _n(now + _USAGE_TTL_SECONDS),
                ":now": _n(now),
            },
        )

    # Advance the cursor to the interval's end. The condition accepts only
    # the cursor value this run started from (or a missing attribute on the
    # first run), so if two runs ever overlap, the second one's advance
    # fails and surfaces as an error in the run report instead of silently
    # re-debiting the same interval on a later run. Note the debit above is
    # not transactional with this advance; on a crash between the two, the
    # interval is re-debited once on the next run. That trade matches the
    # invocation-log pipe's at-least-once delivery and errs toward charging
    # the user rather than losing spend.
    _ddb.update_item(
        TableName=TABLE_NAME,
        Key={"pk": _s(f"MANTLE_HWM#{workspace_id}")},
        UpdateExpression=(
            "SET item_type = if_not_exists(item_type, :t), "
            "user_id = :u, workspace_id = :w, "
            "metric_cursor_at = :cursor_end, "
            "last_reconciled_at = :now, expires_at = :exp"
        ),
        ConditionExpression=(
            "attribute_not_exists(metric_cursor_at) OR metric_cursor_at = :cursor_start"
        ),
        ExpressionAttributeValues={
            ":t": _s("MANTLE_HWM"),
            ":u": _s(user_id),
            ":w": _s(workspace_id),
            ":cursor_end": _n(window_end),
            ":cursor_start": _n(raw_cursor),
            ":now": _n(now),
            ":exp": _n(now + _HWM_TTL_SECONDS),
        },
    )
    return input_delta, output_delta, window_end


def _get_cursor(workspace_id: str) -> int:
    """The exclusive end of the last debited interval, 0 when absent."""
    response = _ddb.get_item(
        TableName=TABLE_NAME,
        Key={"pk": _s(f"MANTLE_HWM#{workspace_id}")},
        ConsistentRead=True,
        ProjectionExpression="metric_cursor_at",
    )
    item = response.get("Item")
    if not item:
        return 0
    return _int_field(item, "metric_cursor_at")


def _int_field(item: dict, key: str) -> int:
    value = item.get(key)
    if not isinstance(value, dict) or "N" not in value:
        return 0
    try:
        return int(value["N"])
    except (TypeError, ValueError):
        return 0


def _log(event_name: str, **fields) -> None:
    record = {"event": event_name}
    record.update(fields)
    print(json.dumps(record, separators=(",", ":"), sort_keys=True))


def handler(event, context):
    """One attribution Lambda, two triggers, dispatched by event shape.

    - CloudWatch Logs subscription delivery (awslogs in the event): debit the
      ledger from Bedrock invocation-log records (the /bedrock-runtime door).
    - EventBridge schedule (no awslogs): reconcile AWS/BedrockMantle project
      metrics into the same ledger (the /inference door).
    """
    if isinstance(event, dict) and "awslogs" in event:
        return _process_invocation_logs(event, context)
    return _reconcile_mantle(event, context)
