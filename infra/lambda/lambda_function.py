# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-user governance for AgentCore Gateway HTTP inference interception.

The Lambda runtime dependency set is Python 3.12 plus the boto3 and botocore
versions included in the managed Lambda runtime.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Protocol



import window as _window

_BEDROCK_REGION = os.environ.get("AWS_REGION", "us-east-1")
# Leave 10 seconds of headroom below the Lambda timeout so we can catch
# Bedrock timeouts, close out the request row, and return a clean 429.
_LAMBDA_TIMEOUT = int(os.environ.get("AWS_LAMBDA_FUNCTION_TIMEOUT", "120"))
_BEDROCK_READ_TIMEOUT = max(10, _LAMBDA_TIMEOUT - 10)

# Bounded server-side retry for a transient DynamoDB TransactionConflict on
# the hot per-user USAGE# item. Claude Code routinely issues concurrent
# requests under one identity (a background Haiku call alongside the main
# turn), so two TransactWriteItems can collide on the same item; the
# collision clears on retry. These bounds keep added latency under a second.
_TRANSACT_MAX_ATTEMPTS = 5
_TRANSACT_BACKOFF_BASE = 0.025
_TRANSACT_BACKOFF_CAP = 0.2
# Retry jitter. Drawn from SystemRandom rather than the shared Mersenne Twister
# so static analysis does not flag a non-cryptographic default; this path only
# runs after a conflict, so the cost is immaterial.
_jitter = random.SystemRandom()


def _bedrock_client():
    """Build a bedrock-runtime client with a bounded read timeout."""
    import boto3 as _boto3
    from botocore.config import Config as _BotoConfig
    return _boto3.client(
        "bedrock-runtime",
        region_name=_BEDROCK_REGION,
        config=_BotoConfig(read_timeout=_BEDROCK_READ_TIMEOUT, retries={"max_attempts": 0}),
    )


def _converse_direct(
    model_id: str,
    body: dict[str, Any],
    request_metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Call Bedrock Converse directly from the interceptor for downgrades."""
    client = _bedrock_client()
    kwargs: dict[str, Any] = {"modelId": model_id}
    if "messages" in body:
        kwargs["messages"] = body["messages"]
    if "system" in body:
        kwargs["system"] = body["system"]
    if "inferenceConfig" in body:
        kwargs["inferenceConfig"] = body["inferenceConfig"]
    if "toolConfig" in body:
        kwargs["toolConfig"] = body["toolConfig"]
    if "guardrailConfig" in body:
        kwargs["guardrailConfig"] = body["guardrailConfig"]
    # Stamp the same identity the passthrough path injects, so Bedrock's
    # invocation log attributes the replayed call to the user. Without it the
    # audit log goes blind on exactly the calls governance rewrote. The
    # attribution Lambda cannot double-debit a stamped replay: its settlement
    # transaction is conditional on the EVENT#<request_id> row not existing,
    # and the in-band settle below writes that row first.
    if request_metadata:
        kwargs["requestMetadata"] = request_metadata
    resp = client.converse(**kwargs)
    resp.pop("ResponseMetadata", None)
    return resp


# The downgrade short-circuit replays a client's body through InvokeModel,
# which rejects request shapes the streaming endpoint tolerates and shapes
# the fallback model does not support. Rather than maintaining a static
# field allowlist (which goes stale as clients evolve), the replay reacts
# to Bedrock's own 400: "X: Extra inputs are not permitted" names the field
# path to drop, and capability rejections that name no path (Haiku refuses
# thinking) map to the field that carries them. Bounded so a
# pathological body cannot loop.
_REPLAY_FIELD_DROP_LIMIT = 6
_REJECTED_FIELD_RE = re.compile(r"([A-Za-z0-9_.\[\]]+): Extra inputs are not permitted")
# Per-model parameter rejections phrased as prose rather than a field path:
# "This model does not support the effort parameter." Newer Claude tiers accept
# sampling and reasoning controls that the cheaper downgrade targets do not, so
# the parameter name is read out of Bedrock's own sentence rather than kept in a
# static list that goes stale as models are added.
_REJECTED_PARAM_RE = re.compile(
    r"does not support the ([A-Za-z0-9_]+) parameter", re.IGNORECASE
)
_REJECTED_CAPABILITY_FIELDS = (
    ("adaptive thinking is not supported", "thinking"),
    ("thinking is not supported", "thinking"),
    # Claude Code's Bedrock mode sends an anthropic_beta array in the BODY;
    # the non-streaming /invoke endpoint accepts most flags there but 400s
    # "invalid beta flag" (no field path) on some (prompt-caching-2024-07-31,
    # oauth-2025-04-20). The flags are advisory for a plain
    # replay, so drop the field.
    ("invalid beta flag", "anthropic_beta"),
)


def _drop_body_path(body: dict[str, Any], path: str) -> bool:
    """Remove a field path Bedrock named as unsupported. Returns True when
    something was removed.

    Bedrock's validation paths are Pydantic-style with numeric segments for
    list positions ("messages.0.content.1.field"), and most of the Anthropic
    body is array-nested, so segments must traverse lists as well as dicts.
    Bracket forms ("tools[3].x") normalize to the dotted form first.
    """
    normalized = path.replace("[", ".").replace("]", "")
    parts = [p for p in normalized.split(".") if p]
    if not parts:
        return False
    node: Any = body
    for part in parts[:-1]:
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return False
    leaf = parts[-1]
    if isinstance(node, dict) and leaf in node:
        node.pop(leaf, None)
        return True
    if isinstance(node, list) and leaf.isdigit() and int(leaf) < len(node):
        # The rejection names a list element itself; remove that element.
        node.pop(int(leaf))
        return True
    return False


def _drop_body_key_anywhere(node: Any, key: str) -> bool:
    """Remove every occurrence of a bare field name at any depth.

    Prose-form rejections ("This model does not support the effort parameter.")
    name the parameter but not where it sits, and these fields are not always
    top-level: effort arrives nested under thinking on some Claude Code
    versions and top-level on others. Dropping by name at every depth is what
    makes the retry work without hard-coding a schema that the next CLI
    release invalidates.
    """
    removed = False
    if isinstance(node, dict):
        if key in node:
            node.pop(key, None)
            removed = True
        for value in node.values():
            removed = _drop_body_key_anywhere(value, key) or removed
    elif isinstance(node, list):
        for value in node:
            removed = _drop_body_key_anywhere(value, key) or removed
    return removed


def _normalize_tools_for_fallback(body: dict[str, Any]) -> dict[str, Any]:
    """Strip tool-search shapes so the body is valid on any fallback model.

    Tool search (a tool_search_tool_* entry plus defer_loading flags on
    custom tools) is a per-model capability: Sonnet-tier accepts it on
    InvokeModel, Haiku rejects the whole request (no header
    changes it). Claude Code sends this shape whenever it runs with a
    large toolset, so a downgrade replay must remove the search tool and
    the defer flags -- every tool then loads up-front, which costs a few
    input tokens and loses nothing functionally.
    """
    tools = body.get("tools")
    if not isinstance(tools, list):
        return body
    normalized: list[Any] = []
    changed = False
    for tool in tools:
        if isinstance(tool, Mapping):
            if str(tool.get("type", "")).startswith("tool_search_tool"):
                changed = True
                continue
            if "defer_loading" in tool:
                tool = {k: v for k, v in tool.items() if k != "defer_loading"}
                changed = True
        normalized.append(tool)
    if not changed:
        return body
    result = dict(body)
    if normalized:
        result["tools"] = normalized
    else:
        result.pop("tools", None)
    return result


def _lift_system_messages(body: dict[str, Any]) -> dict[str, Any]:
    """Move role:"system" messages into the top-level system parameter.

    Some clients (Claude Code's Bedrock mode) put the system
    prompt in the messages array as role "system". The streaming endpoint
    tolerates it; InvokeModel rejects it ("The Messages API accepts a
    top-level system parameter"), so every replay path needs this. Merges
    with any existing top-level system, preserving order.
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not any(
        isinstance(m, Mapping) and m.get("role") == "system" for m in messages
    ):
        return body
    system_blocks: list[Any] = []
    existing = body.get("system")
    if isinstance(existing, str):
        system_blocks.append({"type": "text", "text": existing})
    elif isinstance(existing, list):
        system_blocks.extend(existing)
    remaining = []
    for m in messages:
        if isinstance(m, Mapping) and m.get("role") == "system":
            content = m.get("content")
            if isinstance(content, str):
                system_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                system_blocks.extend(content)
        else:
            remaining.append(m)
    result = dict(body)
    result["messages"] = remaining
    if system_blocks:
        result["system"] = system_blocks
    return result


def _invoke_model_direct(
    model_id: str,
    body: dict[str, Any],
    anthropic_beta: str = "",
    request_metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Replay a client body through InvokeModel for the downgrade
    short-circuit, over signed HTTPS.

    Signed HTTPS rather than boto3's typed API for two reasons: the client's
    anthropic-beta header must be forwarded verbatim (beta-gated request
    shapes are valid only with it, and the SDK cannot send it), and the
    drop-retry loop below needs to re-send an edited body, which the typed
    API also cannot express.

    The body passes through byte-faithful except: the transport-only
    "stream" flag (the non-streaming endpoint rejects it), tool-search
    shapes (see _normalize_tools_for_fallback), and system-role messages
    (lifted to the top-level parameter). Anything else the fallback model
    rejects is dropped by name from Bedrock's own 400 and retried, bounded
    by _REPLAY_FIELD_DROP_LIMIT.

    request_metadata is stamped into the replay body the same way the
    passthrough transform stamps it, so Bedrock's invocation log attributes
    the replayed call to the user rather than recording it identity-blind.
    The attribution Lambda cannot double-debit the stamped record: its
    settlement transaction is conditional on the EVENT#<request_id> row not
    existing, and the in-band settle after this replay writes that row first.
    """
    import urllib.error
    import urllib.request

    import boto3 as _boto3
    import https_call
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    replay_body = {k: v for k, v in body.items() if k != "stream"}
    replay_body = _normalize_tools_for_fallback(replay_body)
    replay_body = _lift_system_messages(replay_body)
    if request_metadata:
        replay_body["requestMetadata"] = request_metadata
    host = f"bedrock-runtime.{_BEDROCK_REGION}.amazonaws.com"
    url = f"https://{host}/model/{model_id}/invoke"
    credentials = _boto3.Session().get_credentials().get_frozen_credentials()
    for _ in range(_REPLAY_FIELD_DROP_LIMIT + 1):
        payload = json.dumps(replay_body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if anthropic_beta:
            headers["anthropic-beta"] = anthropic_beta
        aws_request = AWSRequest(method="POST", url=url, data=payload, headers=headers)
        SigV4Auth(credentials, "bedrock", _BEDROCK_REGION).add_auth(aws_request)
        http_request = urllib.request.Request(
            url, data=payload, headers=dict(aws_request.headers), method="POST"
        )
        try:
            # Fixed regional bedrock-runtime https endpoint; https_call is an
            # opener with no handler for any other scheme (see its docstring).
            with https_call.open_https(
                http_request, timeout=_BEDROCK_READ_TIMEOUT
            ) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read()[:300].decode("utf-8", "replace")
            dropped = False
            if error.code == 400:
                lowered = detail.lower()
                # The header is forwarded verbatim for beta-gated body
                # shapes, but the non-streaming /invoke endpoint rejects
                # flags the streaming endpoint tolerates (Claude Code's
                # Bedrock-mode flags: "invalid beta flag").
                # Retry without the header; any body field that needed it
                # is then named by a subsequent 400 and dropped below.
                if "invalid beta flag" in lowered and anthropic_beta:
                    anthropic_beta = ""
                    dropped = True
                for path in _REJECTED_FIELD_RE.findall(detail):
                    dropped = _drop_body_path(replay_body, path) or dropped
                for phrase, field in _REJECTED_CAPABILITY_FIELDS:
                    if phrase in lowered:
                        dropped = (
                            _drop_body_key_anywhere(replay_body, field) or dropped
                        )
                for param in _REJECTED_PARAM_RE.findall(detail):
                    dropped = (
                        _drop_body_key_anywhere(replay_body, param) or dropped
                    )
            if not dropped:
                raise RuntimeError(
                    f"invoke replay failed ({error.code}): {detail}"
                    f" [body keys: {sorted(replay_body)}]"
                ) from None
    raise RuntimeError("invoke replay failed: unsupported fields exceeded drop limit")


def _eventstream_frame(event_type: str, payload: Mapping[str, Any]) -> bytes:
    """Encode one application/vnd.amazon.eventstream frame with valid CRCs.

    The downgrade short-circuit answers streaming requests (converse-stream,
    invoke-with-response-stream) from inside the interceptor, so the response
    must carry the same binary framing the SDK's eventstream parser expects:
    prelude (total length, headers length, prelude CRC32), headers, JSON
    payload, message CRC32. Returning plain JSON here makes the client parse
    text as frame fields and fail with a checksum mismatch.
    """
    import struct
    headers = b""
    for name, value in (
        (":message-type", "event"),
        (":event-type", event_type),
        (":content-type", "application/json"),
    ):
        encoded_name = name.encode("utf-8")
        encoded_value = value.encode("utf-8")
        headers += (
            struct.pack(">B", len(encoded_name))
            + encoded_name
            + b"\x07"  # header value type 7: string
            + struct.pack(">H", len(encoded_value))
            + encoded_value
        )
    body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    total_length = 12 + len(headers) + len(body) + 4
    prelude = struct.pack(">II", total_length, len(headers))
    prelude_crc = struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF)
    message = prelude + prelude_crc + headers + body
    return message + struct.pack(">I", binascii.crc32(message) & 0xFFFFFFFF)


def _converse_stream_body(resp: Mapping[str, Any]) -> bytes:
    """Re-frame a buffered Converse response as ConverseStream events.

    The event sequence mirrors what bedrock-runtime emits (messageStart,
    contentBlockDelta, contentBlockStop, messageStop, metadata); the whole
    text arrives in one delta because the interceptor already holds the
    complete response.
    """
    text = ""
    output_message = resp.get("output", {}).get("message", {})
    for block in output_message.get("content", []):
        if isinstance(block, Mapping) and isinstance(block.get("text"), str):
            text += block["text"]
    frames = [
        _eventstream_frame("messageStart", {"role": "assistant"}),
        _eventstream_frame(
            "contentBlockDelta",
            {"contentBlockIndex": 0, "delta": {"text": text}},
        ),
        _eventstream_frame("contentBlockStop", {"contentBlockIndex": 0}),
        _eventstream_frame(
            "messageStop", {"stopReason": resp.get("stopReason", "end_turn")}
        ),
    ]
    metadata: dict[str, Any] = {}
    if isinstance(resp.get("usage"), Mapping):
        metadata["usage"] = dict(resp["usage"])
    if isinstance(resp.get("metrics"), Mapping):
        metadata["metrics"] = dict(resp["metrics"])
    if metadata:
        frames.append(_eventstream_frame("metadata", metadata))
    return b"".join(frames)


def _invoke_stream_body(resp: Mapping[str, Any]) -> bytes:
    """Re-frame a buffered InvokeModel (Anthropic) response as
    invoke-with-response-stream chunk events carrying Anthropic stream JSON."""

    def chunk(event: Mapping[str, Any]) -> bytes:
        payload = {
            "bytes": base64.b64encode(
                json.dumps(event, separators=(",", ":"), default=str).encode("utf-8")
            ).decode("ascii")
        }
        return _eventstream_frame("chunk", payload)

    text = "".join(
        block.get("text", "")
        for block in resp.get("content", [])
        if isinstance(block, Mapping) and block.get("type") == "text"
    )
    usage = resp.get("usage", {}) if isinstance(resp.get("usage"), Mapping) else {}
    message_start = {
        "type": "message_start",
        "message": {
            "id": resp.get("id", ""),
            "type": "message",
            "role": "assistant",
            "model": resp.get("model", ""),
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": 0,
            },
        },
    }
    return b"".join(
        [
            chunk(message_start),
            chunk(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            ),
            chunk(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                }
            ),
            chunk({"type": "content_block_stop", "index": 0}),
            chunk(
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": resp.get("stop_reason", "end_turn"),
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": usage.get("output_tokens", 0)},
                }
            ),
            chunk({"type": "message_stop"}),
        ]
    )


def _apply_guardrail_input(
    guardrail_id: str, guardrail_version: str, text: str
) -> tuple[bool, str | None]:
    """Screen input text with Bedrock ApplyGuardrail before any tokens are spent.

    Returns (intervened, reason). ApplyGuardrail is format-independent: the
    interceptor extracts the prompt text from whichever wire format the request
    used (Anthropic Messages, Converse, or InvokeModel) and screens it in one
    synchronous call that invokes no model. INPUT source screens the prompt, so
    a blocked request never reaches Bedrock and costs nothing.
    """
    client = _bedrock_client()
    resp = client.apply_guardrail(
        guardrailIdentifier=guardrail_id,
        guardrailVersion=guardrail_version,
        source="INPUT",
        content=[{"text": {"text": text}}],
    )
    if resp.get("action") != "GUARDRAIL_INTERVENED":
        return False, None
    # Summarize the first tripped policy for the audit record, without echoing
    # the offending content back to the caller.
    reason = "content_policy"
    for assessment in resp.get("assessments", []):
        content_policy = assessment.get("contentPolicy", {})
        filters = content_policy.get("filters", [])
        if filters:
            reason = str(filters[0].get("type", reason)).lower()
            break
        pii = assessment.get("sensitiveInformationPolicy", {})
        if pii.get("piiEntities"):
            reason = "sensitive_information"
            break
    return True, reason

OUTPUT_VERSION = "1.0"
USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
# Bedrock mantle project id (for example proj_example1234567890). Bounded
# and free of CR/LF so it can never inject into the outbound header.
WORKSPACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
# Headers that carry mantle project attribution. Only the interceptor may set
# them, because whichever project they name is the one AWS/BedrockMantle bills
# and the attribution Lambda debits. A request that arrives with one is
# refused; see the mantle-door branch of _parse_request.
_CLIENT_FORBIDDEN_HEADERS = frozenset({"anthropic-workspace-id", "openai-project"})
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
JWT_PATTERN = re.compile(
    r"^Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)$"
)
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


class GovernanceError(Exception):
    """An error whose bounded text is safe to return to the caller."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.safe_message = message
        self.retry_after = retry_after


class ConditionalWriteFailed(Exception):
    """A DynamoDB transaction condition failed without committing writes."""


@dataclass(frozen=True)
class Policy:
    blocked: bool
    budget_tokens: int
    downgrade_at_tokens: int
    fallback_model: str
    allowed_models: frozenset[str] | None = None
    # Per-user requests-per-minute cap. 0 disables the check (budget-only).
    rate_limit_per_minute: int = 0
    # Bedrock mantle project (workspace) id this user's traffic is attributed
    # to. When set, the interceptor sets the anthropic-workspace-id header on
    # mantle-path requests so AWS/BedrockMantle CloudWatch metrics break the
    # user's usage out at the Project dimension. Never trusted from the caller:
    # a request that arrives carrying that header is refused outright.
    # Empty leaves the request untagged, so it falls to the mantle "default"
    # project and this user's mantle-door spend is NOT debited to their budget.
    # Per-user metering on that door therefore requires a project per user, and
    # two users must never share one, or the first reconciled is charged for
    # both.
    workspace_id: str = ""
    # Optional per-door fallback for downgrades on the mantle (/inference)
    # door. The two doors accept DIFFERENT model-id forms (the
    # converse door 400s on bare ids, the mantle door 400s on provider-form
    # ids), so a single fallback_model cannot be valid for both. When set,
    # mantle-door downgrades rewrite to this id and fallback_model serves
    # the bedrock-runtime door only. When empty, mantle-door downgrades use
    # fallback_model as before (correct for deployments that only use one
    # door or whose fallback id happens to be valid there).
    fallback_model_mantle: str = ""
    # Optional downgrade target for the mantle door's OpenAI shape
    # (/v1/chat/completions and /v1/responses). That shape serves only
    # OpenAI-family ids: rewriting a Chat Completions request to a Claude id
    # returns a 400 reporting that the model does not support the
    # '/v1/chat/completions' API and to use '/v1/messages' instead, so
    # the Anthropic-shape target cannot stand in here. Empty means "no
    # shape-compatible target", and the interceptor then leaves the request on
    # its requested model rather than breaking it.
    fallback_model_openai: str = ""

    def validate(self) -> None:
        if self.budget_tokens < 0:
            raise ValueError("budget_tokens must be non-negative")
        if self.rate_limit_per_minute < 0:
            raise ValueError("rate_limit_per_minute must be non-negative")
        if not 0 <= self.downgrade_at_tokens <= self.budget_tokens:
            raise ValueError("downgrade_at_tokens must be within budget_tokens")
        _validate_model(self.fallback_model)
        if self.fallback_model_mantle:
            _validate_model(self.fallback_model_mantle)
        if self.fallback_model_openai:
            _validate_model(self.fallback_model_openai)
        if self.allowed_models is not None:
            if not self.allowed_models:
                raise ValueError("allowed_models cannot be empty")
            for model in self.allowed_models:
                _validate_model(model)
            if self.fallback_model not in self.allowed_models:
                raise ValueError("fallback_model must be in allowed_models")
            if (
                self.fallback_model_mantle
                and self.fallback_model_mantle not in self.allowed_models
            ):
                raise ValueError("fallback_model_mantle must be in allowed_models")
            if (
                self.fallback_model_openai
                and self.fallback_model_openai not in self.allowed_models
            ):
                raise ValueError("fallback_model_openai must be in allowed_models")
        if self.workspace_id and WORKSPACE_ID_PATTERN.fullmatch(self.workspace_id) is None:
            raise ValueError("workspace_id is not an allowed identifier")

    def fallback_for(
        self, converse_path: str | None, openai_format: bool = False
    ) -> str:
        """The downgrade target for the door and wire shape this request
        entered through, or "" when no compatible target is configured.

        converse_path is set for /bedrock-runtime requests and None for the
        mantle door; openai_format marks the mantle door's Chat Completions
        and Responses shapes, which serve only OpenAI-family ids."""
        if converse_path is None:
            if openai_format:
                return self.fallback_model_openai
            if self.fallback_model_mantle:
                return self.fallback_model_mantle
        return self.fallback_model


@dataclass(frozen=True)
class Settings:
    table_name: str
    default_policy: Policy
    max_request_bytes: int = 1_048_576
    max_response_bytes: int = 4_000_000
    max_max_tokens: int = 16_384
    input_bytes_per_token: int = 3
    input_message_overhead_tokens: int = 8
    minimum_input_tokens: int = 16
    max_json_depth: int = 32
    max_json_nodes: int = 50_000
    max_string_bytes: int = 262_144
    max_authorization_bytes: int = 16_384
    context_ttl_seconds: int = 3_600
    event_ttl_seconds: int = 7_776_000
    usage_ttl_seconds: int = 34_560_000
    retry_after_seconds: int = 60
    # Budget weight of cache tokens, as a percent of the input-token price.
    # Defaults mirror Anthropic pricing (reads 10%, writes 125%).
    cache_read_weight_pct: int = 10
    cache_write_weight_pct: int = 125
    # TTL on the per-user-per-minute rate counter; a few minutes is enough for
    # the minute bucket to be self-cleaning.
    rate_limit_ttl_seconds: int = 300
    # Optional Bedrock guardrail for input screening. When both id and version
    # are set, the interceptor screens the prompt text with ApplyGuardrail
    # before the budget check, so blocked input never reaches a model. Empty
    # disables the safety pillar (the checkpoint still enforces the others).
    guardrail_id: str = ""
    guardrail_version: str = ""
    # Budget window: what "per" means for budget_tokens. Calendar-aligned in
    # UTC (hour/day/week/month); "day" reproduces the original behavior
    # exactly. Changing it re-buckets usage, which forgives spend accumulated
    # under the old window (documented amnesty), and changes what
    # budget_tokens means -- rescale budgets when changing the window.
    budget_window: str = "day"
    # Active-hours gate: requests are admitted only inside this local-time
    # window. Minutes-of-day; start == end means always on (the default).
    # start > end wraps overnight (22:00-06:00). Checked at admission with
    # zero DynamoDB cost; a request admitted before closing streams to
    # completion past it.
    active_hours_start: int = 0
    active_hours_end: int = 0
    active_hours_tz: str = "UTC"

    @classmethod
    def from_env(cls) -> "Settings":
        table_name = _required_env("TABLE_NAME")
        fallback_model = _required_env("DEFAULT_FALLBACK_MODEL")
        allowed_raw = os.environ.get("DEFAULT_ALLOWED_MODELS")
        allowed_models = None
        if allowed_raw is not None:
            allowed_models = frozenset(
                model.strip() for model in allowed_raw.split(",") if model.strip()
            )
        default_policy = Policy(
            blocked=False,
            budget_tokens=_env_int("DEFAULT_BUDGET_TOKENS", 100_000, 0),
            downgrade_at_tokens=_env_int("DEFAULT_DOWNGRADE_AT_TOKENS", 80_000, 0),
            fallback_model=fallback_model,
            allowed_models=allowed_models,
            rate_limit_per_minute=_env_int("DEFAULT_RATE_LIMIT_PER_MINUTE", 0, 0),
            # Per-door and per-shape downgrade targets for users with no
            # POLICY item. Empty is valid: the mantle door then reuses
            # fallback_model, and the OpenAI shape skips the downgrade rather
            # than rewrite a Chat Completions request to a Claude id.
            fallback_model_mantle=os.environ.get(
                "DEFAULT_FALLBACK_MODEL_MANTLE", ""
            ).strip(),
            fallback_model_openai=os.environ.get(
                "DEFAULT_FALLBACK_MODEL_OPENAI", ""
            ).strip(),
        )
        default_policy.validate()
        return cls(
            table_name=table_name,
            default_policy=default_policy,
            max_request_bytes=_env_int("MAX_REQUEST_BYTES", 1_048_576, 1),
            max_response_bytes=_env_int("MAX_RESPONSE_BYTES", 4_000_000, 1),
            max_max_tokens=_env_int("MAX_MAX_TOKENS", 16_384, 1),
            # About 4 bytes per token for English text; 3 keeps a safety
            # margin for dense scripts. Only feeds the recorded input estimate,
            # which is informational: admission tests the recorded debit.
            input_bytes_per_token=_env_int("INPUT_BYTES_PER_TOKEN", 3, 1),
            input_message_overhead_tokens=_env_int(
                "INPUT_MESSAGE_OVERHEAD_TOKENS", 8, 0
            ),
            minimum_input_tokens=_env_int("MINIMUM_INPUT_TOKENS", 16, 0),
            max_json_depth=_env_int("MAX_JSON_DEPTH", 32, 4),
            max_json_nodes=_env_int("MAX_JSON_NODES", 50_000, 100),
            max_string_bytes=_env_int("MAX_STRING_BYTES", 262_144, 1),
            max_authorization_bytes=_env_int(
                "MAX_AUTHORIZATION_BYTES", 16_384, 128
            ),
            context_ttl_seconds=_env_int("CONTEXT_TTL_SECONDS", 3_600, 60),
            event_ttl_seconds=_env_int("EVENT_TTL_SECONDS", 7_776_000, 3_600),
            usage_ttl_seconds=_env_int("USAGE_TTL_SECONDS", 34_560_000, 86_400),
            retry_after_seconds=_env_int("BUDGET_RETRY_AFTER_SECONDS", 60, 1),
            cache_read_weight_pct=_env_int("CACHE_READ_WEIGHT_PCT", 10, 0),
            cache_write_weight_pct=_env_int("CACHE_WRITE_WEIGHT_PCT", 125, 0),
            guardrail_id=os.environ.get("GUARDRAIL_ID", "").strip(),
            guardrail_version=os.environ.get("GUARDRAIL_VERSION", "").strip(),
            budget_window=_env_budget_window(),
            **_env_active_hours(),
        )


@dataclass(frozen=True)
class ParsedRequest:
    body: dict[str, Any]
    user_id: str
    original_model: str
    requested_max_tokens: int
    input_estimate_tokens: int
    streaming: bool
    fingerprint: str
    converse_path: str | None = None
    guardrail_text: str = ""
    openai_format: bool = False
    # The client's anthropic-beta header, forwarded verbatim on downgrade
    # replays. Beta-gated request fields (for example tool schemas with
    # defer_loading, which Claude Code sends) are only valid when this
    # header accompanies them, and boto3's typed InvokeModel API cannot
    # carry it, so the replay signs its own HTTP request instead.
    anthropic_beta: str = ""


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    user_id: str
    usage_pk: str
    usage_date: str
    original_model: str
    effective_model: str
    requested_max_tokens: int
    input_estimate_tokens: int
    action: str
    streaming: bool
    fingerprint: str
    created_at: int
    expires_at: int
    # Wall-clock milliseconds the interceptor spent on this request up to the
    # admission write, self-reported so the demo timeline can show the
    # governance checkpoint's real cost instead of guessing. 0 = not measured.
    interceptor_ms: int = 0


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return sum(getattr(self, field) for field in USAGE_FIELDS)

    def billable_tokens(self, read_pct: int, write_pct: int) -> int:
        """Budget-weighted total. Cache reads and writes are real tokens but
        cost a fraction of the input price (Anthropic pricing: reads 10%,
        writes 125%), so debiting them at full weight makes one large-prompt
        client (Claude Code writes its ~116KB system prompt to cache every
        session, 30k+ tokens) consume a whole normal budget in one turn.
        Weights are percentages of input-token cost."""
        return (
            self.input_tokens
            + self.output_tokens
            + (self.cache_read_input_tokens * read_pct) // 100
            + (self.cache_creation_input_tokens * write_pct) // 100
        )

    def as_dict(self) -> dict[str, int]:
        values = {field: getattr(self, field) for field in USAGE_FIELDS}
        values["total_tokens"] = self.total_tokens
        return values


class Repository(Protocol):
    def get_policy(self, user_id: str, defaults: Policy) -> Policy: ...

    def get_usage_debit(self, usage_pk: str) -> int: ...

    def check_rate_limit(self, user_id: str, limit: int, now: int, ttl: int) -> bool: ...

    def get_request(self, request_id: str) -> RequestContext | None: ...

    def event_exists(self, request_id: str) -> bool: ...

    def admit(
        self,
        request_context: RequestContext,
        policy: Policy,
        *,
        threshold_limit: int | None,
        usage_expires_at: int,
    ) -> None: ...

    def finalize(
        self,
        request_context: RequestContext,
        usage: Usage | None,
        *,
        status_code: int,
        reason: str,
        event_expires_at: int,
        now: int,
        billable_total: int | None = None,
    ) -> None: ...


class DynamoRepository:
    """Single-table repository using the low-level boto3 DynamoDB client."""

    def __init__(self, client: Any, table_name: str) -> None:
        self.client = client
        self.table_name = table_name

    def get_policy(self, user_id: str, defaults: Policy) -> Policy:
        response = self.client.get_item(
            TableName=self.table_name,
            Key={"pk": _s(f"POLICY#{user_id}")},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not isinstance(item, Mapping):
            return defaults
        allowed_models = defaults.allowed_models
        if "allowed_models" in item:
            allowed_models = _string_collection(item["allowed_models"], "allowed_models")
        policy = Policy(
            blocked=_optional_bool(item, "blocked", defaults.blocked),
            budget_tokens=_optional_int(item, "budget_tokens", defaults.budget_tokens),
            downgrade_at_tokens=_optional_int(
                item, "downgrade_at_tokens", defaults.downgrade_at_tokens
            ),
            fallback_model=_optional_string(
                item, "fallback_model", defaults.fallback_model
            ),
            allowed_models=allowed_models,
            rate_limit_per_minute=_optional_int(
                item, "rate_limit_per_minute", defaults.rate_limit_per_minute
            ),
            workspace_id=_optional_string(
                item, "workspace_id", defaults.workspace_id
            ),
            fallback_model_mantle=_optional_string(
                item, "fallback_model_mantle", defaults.fallback_model_mantle
            ),
            fallback_model_openai=_optional_string(
                item, "fallback_model_openai", defaults.fallback_model_openai
            ),
        )
        policy.validate()
        return policy

    def check_rate_limit(self, user_id: str, limit: int, now: int, ttl: int) -> bool:
        """Atomically increment the user's requests-this-minute counter and
        return True if the request is within the limit. The counter key is
        per user per minute; the ADD creates it at 1 on the first request of
        the minute and the conditional guard admits only while count < limit.
        Returns False (over the limit) on the conditional failure."""
        minute = now // 60
        key = f"RATE#{user_id}#{minute}"
        try:
            self.client.update_item(
                TableName=self.table_name,
                Key={"pk": _s(key)},
                UpdateExpression="SET expires_at = :exp ADD request_count :one",
                ConditionExpression="attribute_not_exists(request_count) OR request_count < :limit",
                ExpressionAttributeValues={
                    ":one": _n(1),
                    ":limit": _n(limit),
                    ":exp": _n(now + ttl),
                },
            )
            return True
        except self.client.exceptions.ConditionalCheckFailedException:
            return False

    def get_usage_debit(self, usage_pk: str) -> int:
        response = self.client.get_item(
            TableName=self.table_name,
            Key={"pk": _s(usage_pk)},
            ConsistentRead=True,
            ProjectionExpression="budget_debit_tokens",
        )
        item = response.get("Item")
        if not isinstance(item, Mapping) or "budget_debit_tokens" not in item:
            return 0
        return _number(item["budget_debit_tokens"], "budget_debit_tokens")

    def get_request(self, request_id: str) -> RequestContext | None:
        response = self.client.get_item(
            TableName=self.table_name,
            Key={"pk": _s(f"REQ#{request_id}")},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not isinstance(item, Mapping):
            return None
        return RequestContext(
            request_id=_string(item, "request_id"),
            user_id=_string(item, "user_id"),
            usage_pk=_string(item, "usage_pk"),
            usage_date=_string(item, "usage_date"),
            original_model=_string(item, "original_model"),
            effective_model=_string(item, "effective_model"),
            requested_max_tokens=_integer(item, "requested_max_tokens"),
            input_estimate_tokens=_integer(item, "input_estimate_tokens"),
            action=_string(item, "action"),
            streaming=_boolean(item, "streaming"),
            fingerprint=_string(item, "fingerprint"),
            created_at=_integer(item, "created_at"),
            expires_at=_integer(item, "expires_at"),
        )

    def event_exists(self, request_id: str) -> bool:
        response = self.client.get_item(
            TableName=self.table_name,
            Key={"pk": _s(f"EVENT#{request_id}")},
            ConsistentRead=True,
            ProjectionExpression="pk",
        )
        return isinstance(response.get("Item"), Mapping)

    def admit(
        self,
        request_context: RequestContext,
        policy: Policy,
        *,
        threshold_limit: int | None,
        usage_expires_at: int,
    ) -> None:
        """Admit a request: nothing is debited here, only checked and recorded.

        Admission is the industry pattern (LiteLLM, Kong, Portkey): the request
        is allowed when the debit ALREADY recorded for this window is under
        budget, and the actual usage is debited afterwards in `finalize`. So
        this writes no token amount at all -- it asserts the budget condition,
        stamps the window's metadata, counts the request, and creates the REQ
        row, all in one transaction.
        """
        condition = (
            "(attribute_not_exists(#debit) OR #debit <= :budget_limit) "
            "AND (attribute_not_exists(#user_id) OR #user_id = :user_id)"
        )
        values = {
            ":budget_limit": _n(policy.budget_tokens),
            ":user_id": _s(request_context.user_id),
            ":usage_date": _s(request_context.usage_date),
            ":usage_type": _s("USAGE"),
            ":usage_expiry": _n(usage_expires_at),
            ":budget": _n(policy.budget_tokens),
            ":now": _n(request_context.created_at),
            ":one": _n(1),
        }
        if threshold_limit is not None:
            condition += (
                " AND (attribute_not_exists(#debit) OR "
                "#debit <= :threshold_limit)"
            )
            values[":threshold_limit"] = _n(threshold_limit)
        update_usage = {
            "TableName": self.table_name,
            "Key": {"pk": _s(request_context.usage_pk)},
            "UpdateExpression": (
                "SET #user_id = if_not_exists(#user_id, :user_id), "
                "#usage_date = if_not_exists(#usage_date, :usage_date), "
                "#item_type = if_not_exists(#item_type, :usage_type), "
                "#expires_at = :usage_expiry, #budget = :budget, #updated_at = :now "
                "ADD #accepted :one"
            ),
            "ConditionExpression": condition,
            "ExpressionAttributeNames": {
                "#user_id": "user_id",
                "#usage_date": "usage_date",
                "#item_type": "item_type",
                "#expires_at": "expires_at",
                "#budget": "budget_tokens",
                "#updated_at": "updated_at",
                "#debit": "budget_debit_tokens",
                "#accepted": "accepted_requests",
            },
            "ExpressionAttributeValues": values,
        }
        put_request = {
            "TableName": self.table_name,
            "Item": _request_item(request_context),
            "ConditionExpression": "attribute_not_exists(#pk)",
            "ExpressionAttributeNames": {"#pk": "pk"},
        }
        completed_event_absent = {
            "TableName": self.table_name,
            "Key": {"pk": _s(f"EVENT#{request_context.request_id}")},
            "ConditionExpression": "attribute_not_exists(#pk)",
            "ExpressionAttributeNames": {"#pk": "pk"},
        }
        # Invariant: absence of a completed event, the budget condition, and
        # request-context creation commit together. A finalized request ID can
        # never be recreated and charged again during a precheck race.
        self._transact(
            [
                {"ConditionCheck": completed_event_absent},
                {"Update": update_usage},
                {"Put": put_request},
            ]
        )

    def finalize(
        self,
        request_context: RequestContext,
        usage: Usage | None,
        *,
        status_code: int,
        reason: str,
        event_expires_at: int,
        now: int,
        billable_total: int | None = None,
    ) -> None:
        recorded = usage or Usage()
        # billable_total is the budget-weighted debit (cache tokens at their
        # price weight, see Usage.billable_tokens); the EVENT row still
        # records the raw per-field counts unchanged.
        if usage is None:
            actual_total = 0
        elif billable_total is not None:
            actual_total = billable_total
        else:
            actual_total = recorded.total_tokens
        event_item = {
            "pk": _s(f"EVENT#{request_context.request_id}"),
            "item_type": _s("EVENT"),
            "request_id": _s(request_context.request_id),
            "user_id": _s(request_context.user_id),
            "usage_pk": _s(request_context.usage_pk),
            "usage_date": _s(request_context.usage_date),
            "original_model": _s(request_context.original_model),
            "effective_model": _s(request_context.effective_model),
            "action": _s(request_context.action),
            "streaming": _b(request_context.streaming),
            "status_code": _n(status_code),
            "input_tokens": _n(recorded.input_tokens),
            "output_tokens": _n(recorded.output_tokens),
            "cache_read_input_tokens": _n(recorded.cache_read_input_tokens),
            "cache_creation_input_tokens": _n(recorded.cache_creation_input_tokens),
            "total_tokens": _n(actual_total),
            "finalization_reason": _s(reason),
            "created_at": _n(now),
            "expires_at": _n(event_expires_at),
        }
        put_event = {
            "TableName": self.table_name,
            "Item": event_item,
            "ConditionExpression": "attribute_not_exists(#pk)",
            "ExpressionAttributeNames": {"#pk": "pk"},
        }
        names = {
            "#user_id": "user_id",
            "#updated_at": "updated_at",
            "#debit": "budget_debit_tokens",
            "#input": "input_tokens",
            "#output": "output_tokens",
            "#cache_read": "cache_read_input_tokens",
            "#cache_creation": "cache_creation_input_tokens",
            "#actual": "actual_tokens",
            "#completed": "completed_requests",
        }
        values = {
            ":user_id": _s(request_context.user_id),
            ":now": _n(now),
            ":debit": _n(actual_total),
            ":input": _n(recorded.input_tokens),
            ":output": _n(recorded.output_tokens),
            ":cache_read": _n(recorded.cache_read_input_tokens),
            ":cache_creation": _n(recorded.cache_creation_input_tokens),
            ":actual": _n(actual_total),
            ":one": _n(1),
        }
        add_parts = [
            "#debit :debit",
            "#input :input",
            "#output :output",
            "#cache_read :cache_read",
            "#cache_creation :cache_creation",
            "#actual :actual",
            "#completed :one",
        ]
        if usage is None:
            names["#released"] = "released_without_usage"
            add_parts.append("#released :one")
        update_usage = {
            "TableName": self.table_name,
            "Key": {"pk": _s(request_context.usage_pk)},
            "UpdateExpression": "SET #updated_at = :now ADD " + ", ".join(add_parts),
            # The USAGE row must already belong to this user: admission created
            # it, so a settle that cannot match it is settling against a window
            # that was never admitted.
            "ConditionExpression": "#user_id = :user_id",
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
        delete_request = {
            "TableName": self.table_name,
            "Key": {"pk": _s(f"REQ#{request_context.request_id}")},
            "ConditionExpression": "#user_id = :user_id AND #fingerprint = :fingerprint",
            "ExpressionAttributeNames": {
                "#user_id": "user_id",
                "#fingerprint": "fingerprint",
            },
            "ExpressionAttributeValues": {
                ":user_id": _s(request_context.user_id),
                ":fingerprint": _s(request_context.fingerprint),
            },
        }
        # Invariant: the idempotency event, the aggregate debit, and request
        # deletion commit together or not at all.
        self._transact(
            [
                {"Put": put_event},
                {"Update": update_usage},
                {"Delete": delete_request},
            ]
        )

    def _transact(self, items: list[dict[str, Any]]) -> None:
        attempt = 0
        while True:
            try:
                self.client.transact_write_items(TransactItems=items)
                return
            except Exception as error:
                # A genuine condition failure is how the accounting path
                # signals a refusal (budget exceeded, duplicate request).
                # Never retry it.
                if _is_conditional_failure(error):
                    raise ConditionalWriteFailed() from error
                attempt += 1
                # A pure TransactionConflict is a transient collision between
                # concurrent writers on the hot USAGE# item, not a refusal.
                # Retry with bounded backoff and full jitter. The transaction
                # is atomic, so nothing was written and the retry cannot
                # double-charge; condition expressions are re-evaluated, so a
                # retry that now genuinely exceeds the budget still raises
                # ConditionalWriteFailed above.
                if _is_transaction_conflict(error) and attempt < _TRANSACT_MAX_ATTEMPTS:
                    time.sleep(
                        _jitter.uniform(
                            0,
                            min(
                                _TRANSACT_BACKOFF_CAP,
                                _TRANSACT_BACKOFF_BASE * (2 ** (attempt - 1)),
                            ),
                        )
                    )
                    continue
                raise


@dataclass
class GovernanceService:
    settings: Settings
    repository: Repository
    now: Callable[[], float] = time.time
    logger: Callable[[str], None] = print

    def handle(self, event: Mapping[str, Any], context: Any) -> dict[str, Any]:
        # This deployment attaches the interceptor at REQUEST only (see
        # governance-gateway.ts and attach_interceptor.py) because a RESPONSE
        # interceptor forces the gateway to buffer streamed replies. The
        # RESPONSE branch below is therefore unreachable as shipped; it is
        # retained, with its usage parsers, for operators who deliberately
        # attach a RESPONSE interceptor to trade streaming for in-band
        # settlement instead of the async attribution pipeline.
        if _interception_point(event) == "RESPONSE":
            return self._handle_response(event, context)
        return self._handle_request(event, context)

    def _handle_request(
        self, event: Mapping[str, Any], context: Any
    ) -> dict[str, Any]:
        handling_started = time.monotonic()
        request_id: str | None = None
        user_id: str | None = None
        model: str | None = None
        # MCP protocol events (tools/list and tools/call for connector
        # targets such as web-search) carry an "mcp" schema, not the HTTP
        # inference shape, and invoke no model through the inference surface.
        # They pass through ungoverned; the HTTP inference surface below
        # stays fail-closed. The passthrough mirrors the event's protocol key
        # because the gateway expects the output block to match the input.
        if not isinstance(event.get("http"), Mapping):
            if isinstance(event.get("mcp"), Mapping):
                return {"interceptorOutputVersion": OUTPUT_VERSION, "mcp": {}}
            return _passthrough_output()
        # Read-only metadata routes (GET /v1/models and similar) carry no body
        # and invoke no model, so they pass through ungoverned. Every
        # model-invoking route on the inference surface is a POST with a JSON
        # body, so anything that is not explicitly a POST passes through and
        # POST stays fail-closed. GET events do not reliably
        # carry httpMethod, so match on "not POST" rather than "is GET".
        http = event.get("http")
        gateway_request = (
            http.get("gatewayRequest") if isinstance(http, Mapping) else None
        )
        if isinstance(gateway_request, Mapping):
            method = str(gateway_request.get("httpMethod", "")).upper()
            body = gateway_request.get("body")
            if method != "POST" and not body:
                return _passthrough_output()
        try:
            request_id = _request_id(context, required=True)
            parsed = _parse_request(event, self.settings)
            user_id = parsed.user_id
            model = parsed.original_model
            policy = self.repository.get_policy(user_id, self.settings.default_policy)
            policy.validate()
            if policy.blocked:
                raise GovernanceError(403, "access_denied", "Access denied by user policy")
            # Active-hours gate: a pure clock check, so out-of-hours traffic is
            # turned away before any DynamoDB read. Admission-time only: a
            # request admitted at closing time streams to completion.
            self._check_active_hours(int(self.now()))
            if policy.allowed_models is not None and model not in policy.allowed_models:
                raise GovernanceError(
                    403, "model_not_allowed", "Requested model is not allowed"
                )
            # Per-user rate limit (requests per minute). Runs before the budget
            # check so a burst is turned away for cents of DynamoDB, not tokens.
            # 0 disables it. The counter is idempotent-safe: a gateway retry of
            # the same request_id re-checks below and returns the recorded
            # decision, so a retry does not double-count against the limit.
            if policy.rate_limit_per_minute > 0 and not self.repository.event_exists(
                request_id
            ):
                now_rl = int(self.now())
                within = self.repository.check_rate_limit(
                    user_id,
                    policy.rate_limit_per_minute,
                    now_rl,
                    self.settings.rate_limit_ttl_seconds,
                )
                if not within:
                    raise GovernanceError(
                        429,
                        "rate_limit_exceeded",
                        "Per-user requests-per-minute limit exceeded",
                        retry_after=60,
                    )

            # Safety guardrail. Screen the prompt text with Bedrock
            # ApplyGuardrail before the budget check, so blocked input never
            # reaches a model and costs nothing. Format-independent: the text
            # was extracted above from whichever wire format the request used.
            # Runs only when a guardrail is configured and there is text to
            # screen; a gateway retry of a completed request skips it.
            if (
                self.settings.guardrail_id
                and self.settings.guardrail_version
                and parsed.guardrail_text
                and not self.repository.event_exists(request_id)
            ):
                intervened, reason = _apply_guardrail_input(
                    self.settings.guardrail_id,
                    self.settings.guardrail_version,
                    parsed.guardrail_text,
                )
                if intervened:
                    self._log(
                        "guardrail_blocked",
                        request_id=request_id,
                        user_id=user_id,
                        action="guardrail_blocked",
                        original_model=model,
                        reason=reason,
                    )
                    raise GovernanceError(
                        403,
                        "guardrail_intervened",
                        "Request blocked by content safety policy",
                    )

            existing = self.repository.get_request(request_id)
            if existing is not None:
                return self._request_retry_output(existing, parsed, policy.workspace_id)
            if self.repository.event_exists(request_id):
                raise GovernanceError(
                    409,
                    "request_id_conflict",
                    "Request identifier was already completed",
                )

            now = int(self.now())
            usage_date = _window.window_key(now, self.settings.budget_window)
            usage_pk = f"USAGE#{user_id}#{usage_date}"
            current_debit = self.repository.get_usage_debit(usage_pk)
            if current_debit > policy.budget_tokens:
                raise self._budget_error()
            # The downgrade target must match the door's model-id form and the
            # wire shape's model family (each rejects the other's with a 400).
            # An empty target means this shape has no compatible
            # fallback configured: leave the request on its requested model
            # rather than rewrite it into a 400.
            door_fallback = policy.fallback_for(
                parsed.converse_path, parsed.openai_format
            )
            over_threshold = current_debit > policy.downgrade_at_tokens
            if over_threshold and not door_fallback:
                self._log(
                    "downgrade_skipped",
                    request_id=request_id,
                    user_id=user_id,
                    action="downgrade_skipped_no_target",
                    model=model,
                    openai_format=parsed.openai_format,
                )
            downgrade = (
                bool(door_fallback) and model != door_fallback and over_threshold
            )
            effective_model = door_fallback if downgrade else model
            request_context = self._context(
                request_id,
                parsed,
                usage_pk,
                usage_date,
                effective_model,
                now,
                interceptor_ms=int((time.monotonic() - handling_started) * 1000),
            )
            # The threshold condition exists to catch the race where a
            # concurrent request pushes usage past the downgrade line between
            # this read and this write; it is only useful when there is a
            # target to retry on.
            threshold = (
                policy.downgrade_at_tokens
                if door_fallback and effective_model == model and model != door_fallback
                else None
            )
            try:
                self.repository.admit(
                    request_context,
                    policy,
                    threshold_limit=threshold,
                    usage_expires_at=now + self.settings.usage_ttl_seconds,
                )
            except ConditionalWriteFailed:
                duplicate = self.repository.get_request(request_id)
                if duplicate is not None:
                    return self._request_retry_output(duplicate, parsed, policy.workspace_id)
                if self.repository.event_exists(request_id):
                    raise GovernanceError(
                        409,
                        "request_id_conflict",
                        "Request identifier was already completed",
                    )
                current_debit = self.repository.get_usage_debit(usage_pk)
                if current_debit > policy.budget_tokens:
                    raise self._budget_error()
                if (
                    door_fallback
                    and model != door_fallback
                    and current_debit > policy.downgrade_at_tokens
                ):
                    request_context = self._context(
                        request_id,
                        parsed,
                        usage_pk,
                        usage_date,
                        door_fallback,
                        now,
                    )
                    try:
                        # threshold_limit=None relaxes only the model threshold.
                        # Repository.admit always retains the atomic budget
                        # condition, so this fallback retry cannot overspend.
                        self.repository.admit(
                            request_context,
                            policy,
                            threshold_limit=None,
                            usage_expires_at=now + self.settings.usage_ttl_seconds,
                        )
                    except ConditionalWriteFailed:
                        duplicate = self.repository.get_request(request_id)
                        if duplicate is not None:
                            return self._request_retry_output(duplicate, parsed, policy.workspace_id)
                        if self.repository.event_exists(request_id):
                            raise GovernanceError(
                                409,
                                "request_id_conflict",
                                "Request identifier was already completed",
                            )
                        raise self._budget_error()
                else:
                    raise self._budget_error()

            self._log(
                "request_admitted",
                request_id=request_id,
                user_id=user_id,
                action=request_context.action,
                original_model=model,
                effective_model=request_context.effective_model,
                requested_max_tokens=parsed.requested_max_tokens,
                input_estimate_tokens=parsed.input_estimate_tokens,
                streaming=parsed.streaming,
            )
            # For Converse downgrades: the interceptor cannot rewrite the URL
            # path (where the model id lives), so it calls Bedrock directly with
            # the fallback model and returns the response as a short-circuit.
            # The request never reaches the passthrough target.
            if parsed.converse_path and model != request_context.effective_model:
                try:
                    is_invoke_model = "/invoke" in parsed.converse_path and "/converse" not in parsed.converse_path
                    # The identity the passthrough transform would have
                    # stamped; the replay must carry it too or the invocation
                    # log records the downgraded call with no user attached.
                    downgrade_metadata = {
                        "user": request_context.user_id,
                        "request_id": request_context.request_id,
                    }
                    if is_invoke_model:
                        bedrock_resp = _invoke_model_direct(
                            request_context.effective_model,
                            parsed.body,
                            parsed.anthropic_beta,
                            downgrade_metadata,
                        )
                    else:
                        bedrock_resp = _converse_direct(
                            request_context.effective_model,
                            parsed.body,
                            downgrade_metadata,
                        )
                    # Settle immediately (we have the real usage)
                    usage_obj = None
                    raw_usage = bedrock_resp.get("usage")
                    if isinstance(raw_usage, dict):
                        # The converse door returns camelCase usage keys
                        # (inputTokens); the invoke door returns the Anthropic
                        # shape in snake_case (input_tokens).
                        # Parse both, or invoke-door downgrades settle zeros
                        # and the tokens never debit.
                        def _tok(*keys: str) -> int:
                            for key in keys:
                                value = raw_usage.get(key)
                                if isinstance(value, (int, float)) and not isinstance(value, bool):
                                    return int(value)
                            return 0
                        usage_obj = Usage(
                            input_tokens=_tok("inputTokens", "input_tokens"),
                            output_tokens=_tok("outputTokens", "output_tokens"),
                            cache_read_input_tokens=_tok(
                                "cacheReadInputTokenCount", "cacheReadInputTokens", "cache_read_input_tokens"
                            ),
                            cache_creation_input_tokens=_tok(
                                "cacheWriteInputTokenCount", "cacheWriteInputTokens", "cache_creation_input_tokens"
                            ),
                        )
                    self.repository.finalize(
                        request_context, usage_obj,
                        status_code=200, reason="converse_downgrade_direct",
                        event_expires_at=int(self.now()) + self.settings.event_ttl_seconds,
                        now=int(self.now()),
                        billable_total=(
                            usage_obj.billable_tokens(
                                self.settings.cache_read_weight_pct,
                                self.settings.cache_write_weight_pct,
                            )
                            if usage_obj is not None
                            else None
                        ),
                    )
                    # Return the Bedrock response as a short-circuit. A
                    # streaming request (converse-stream or
                    # invoke-with-response-stream) expects eventstream binary
                    # framing, not JSON: the SDK parses the body as frames and
                    # a JSON body fails its CRC check.
                    if parsed.streaming:
                        if is_invoke_model:
                            raw = _invoke_stream_body(bedrock_resp)
                        else:
                            raw = _converse_stream_body(bedrock_resp)
                        return {
                            "interceptorOutputVersion": OUTPUT_VERSION,
                            "http": {"transformedGatewayResponse": {
                                "statusCode": 200,
                                "contentType": "application/vnd.amazon.eventstream",
                                "body": base64.b64encode(raw).decode("ascii"),
                            }},
                        }
                    resp_body = json.dumps(bedrock_resp, separators=(",", ":"), default=str)
                    encoded_resp = base64.b64encode(resp_body.encode("utf-8")).decode("ascii")
                    return {
                        "interceptorOutputVersion": OUTPUT_VERSION,
                        "http": {"transformedGatewayResponse": {
                            "statusCode": 200,
                            "contentType": "application/json",
                            "body": encoded_resp,
                        }},
                    }
                except Exception as direct_err:
                    # On timeout or error: close out the request with no usage
                    # (nothing was held, so nothing is debited), then return 429
                    # so the client can retry (possibly with the cheaper model
                    # explicitly).
                    from botocore.exceptions import ReadTimeoutError, ConnectTimeoutError
                    is_timeout = isinstance(direct_err, (ReadTimeoutError, ConnectTimeoutError, TimeoutError))
                    self._log(
                        "converse_direct_failed",
                        request_id=request_id,
                        user_id=user_id,
                        action="downgrade_direct_failed",
                        # Long enough to keep the denied resource visible: an
                        # AccessDenied on a downgrade names the model ARN well
                        # past 200 characters.
                        error=str(direct_err)[:600],
                        is_timeout=is_timeout,
                    )
                    try:
                        self.repository.finalize(
                            request_context, None,
                            status_code=504 if is_timeout else 502,
                            reason="downgrade_timeout" if is_timeout else "downgrade_error",
                            event_expires_at=int(self.now()) + self.settings.event_ttl_seconds,
                            now=int(self.now()),
                        )
                    except Exception as finalize_err:
                        # The 429 below is still the right answer for the
                        # caller, so this must not raise. But a failed finalize
                        # leaves the REQ row behind, and that is invisible
                        # unless it is logged: the TTL will clear it, and until
                        # then this request id cannot be reused.
                        self._log(
                            "finalize_after_downgrade_failure_failed",
                            request_id=request_id,
                            user_id=user_id,
                            action="request_close_failed",
                            error=type(finalize_err).__name__,
                        )
                    raise GovernanceError(
                        429,
                        "downgrade_unavailable",
                        "Model downgrade timed out; retry with the fallback model directly",
                        retry_after=5,
                    )
            metadata = (
                {"user": user_id, "request_id": request_id}
                if parsed.converse_path
                else None
            )
            # Mantle-path requests (converse_path is None, model is in the body)
            # are tagged with the user's project so AWS/BedrockMantle metrics
            # attribute the usage. _request_output injects the header only on the
            # mantle path and always overwrites any client-supplied value.
            return _request_output(
                parsed.body, model, request_context.effective_model,
                converse_path=parsed.converse_path,
                request_metadata=metadata,
                workspace_id=policy.workspace_id,
                openai_format=parsed.openai_format,
            )
        except GovernanceError as error:
            self._log(
                "request_rejected",
                request_id=request_id,
                user_id=user_id,
                action=error.code,
                status_code=error.status_code,
                model=model,
            )
            return _error_output(error, request_id)
        except (TypeError, ValueError) as error:
            self._log(
                "request_rejected",
                request_id=request_id,
                user_id=user_id,
                action="policy_invalid",
                status_code=503,
                model=model,
                # Exception class and bounded message only; policy values and
                # request content never appear in str() of these validators.
                error_type=type(error).__name__,
                error_detail=str(error)[:200],
            )
            return _error_output(
                GovernanceError(503, "policy_invalid", "Governance policy is invalid"),
                request_id,
            )
        except Exception as error:
            self._log(
                "request_failed",
                request_id=request_id,
                user_id=user_id,
                action="dependency_error",
                status_code=503,
                model=model,
                error_type=type(error).__name__,
                error_detail=str(error)[:300],
            )
            return _error_output(
                GovernanceError(
                    503, "governance_unavailable", "Governance service unavailable"
                ),
                request_id,
            )

    def _handle_response(
        self, event: Mapping[str, Any], context: Any
    ) -> dict[str, Any]:
        request_id: str | None = None
        try:
            request_id = _request_id(context, required=False)
            if request_id is None:
                self._log(
                    "response_reconciliation_required",
                    request_id=None,
                    user_id=None,
                    action="missing_request_id",
                )
                return _passthrough_output()
            request_context = self.repository.get_request(request_id)
            if request_context is None:
                action = (
                    "idempotent"
                    if self.repository.event_exists(request_id)
                    else "request_context_missing"
                )
                self._log(
                    "response_passthrough",
                    request_id=request_id,
                    user_id=None,
                    action=action,
                )
                return _passthrough_output()

            response = _gateway_response(event)
            status_code = _response_status(response)
            usage: Usage | None = None
            if 200 <= status_code < 300:
                usage = _parse_response_usage(
                    response, self.settings.max_response_bytes
                )
                reason = "usage_recorded" if usage is not None else "usage_unavailable"
            else:
                reason = "upstream_error"
            now = int(self.now())
            try:
                self.repository.finalize(
                    request_context,
                    usage,
                    status_code=status_code,
                    reason=reason,
                    event_expires_at=now + self.settings.event_ttl_seconds,
                    now=now,
                    billable_total=(
                        usage.billable_tokens(
                            self.settings.cache_read_weight_pct,
                            self.settings.cache_write_weight_pct,
                        )
                        if usage is not None
                        else None
                    ),
                )
                action = "settled" if usage is not None else "released"
            except ConditionalWriteFailed:
                if not self.repository.event_exists(request_id):
                    raise
                action = "idempotent"
            log_fields: dict[str, Any] = {
                "status_code": status_code,
                "reason": reason,
                "original_model": request_context.original_model,
                "effective_model": request_context.effective_model,
            }
            if usage is not None:
                log_fields.update(usage.as_dict())
            self._log(
                "response_attributed",
                request_id=request_id,
                user_id=request_context.user_id,
                action=action,
                **log_fields,
            )
        except GovernanceError as error:
            self._log(
                "response_reconciliation_required",
                request_id=request_id,
                user_id=None,
                action=error.code,
            )
        except Exception as error:
            # Invariant: attribution failures never expose or replace the
            # upstream response. Logs contain only the exception class, not its message.
            self._log(
                "response_finalize_failed",
                request_id=request_id,
                user_id=None,
                action="passthrough",
                error_type=type(error).__name__,
            )
        return _passthrough_output()

    def _context(
        self,
        request_id: str,
        parsed: ParsedRequest,
        usage_pk: str,
        usage_date: str,
        effective_model: str,
        now: int,
        interceptor_ms: int = 0,
    ) -> RequestContext:
        return RequestContext(
            request_id=request_id,
            user_id=parsed.user_id,
            usage_pk=usage_pk,
            usage_date=usage_date,
            original_model=parsed.original_model,
            effective_model=effective_model,
            requested_max_tokens=parsed.requested_max_tokens,
            input_estimate_tokens=parsed.input_estimate_tokens,
            action=(
                "downgrade"
                if effective_model != parsed.original_model
                else "allow"
            ),
            streaming=parsed.streaming,
            fingerprint=parsed.fingerprint,
            created_at=now,
            expires_at=now + self.settings.context_ttl_seconds,
            interceptor_ms=interceptor_ms,
        )

    def _request_retry_output(
        self,
        existing: RequestContext,
        parsed: ParsedRequest,
        workspace_id: str = "",
    ) -> dict[str, Any]:
        if (
            existing.user_id != parsed.user_id
            or existing.original_model != parsed.original_model
            or existing.requested_max_tokens != parsed.requested_max_tokens
            or existing.input_estimate_tokens != parsed.input_estimate_tokens
            or existing.streaming != parsed.streaming
            or existing.fingerprint != parsed.fingerprint
        ):
            raise GovernanceError(
                409, "request_id_conflict", "Request identifier was already used"
            )
        # A recorded downgrade on the passthrough door cannot be retried by
        # forwarding. The interceptor cannot rewrite the URL path, so the gateway
        # would route the retry on the originally requested model while the
        # rewritten body named the fallback, and Bedrock refuses that mismatch
        # with its own 400. The first attempt avoids this by replaying the
        # downgrade directly, which a retry must not do: that would be a second
        # billable call whose settlement the first attempt's EVENT row blocks.
        # The request has already been admitted and is either in flight or
        # finished, so the interceptor returns 409 rather than re-admitting it.
        if parsed.converse_path and existing.effective_model != parsed.original_model:
            raise GovernanceError(
                409,
                "request_id_conflict",
                "Request identifier was already admitted as a downgrade",
            )
        self._log(
            "request_admitted",
            request_id=existing.request_id,
            user_id=existing.user_id,
            action="idempotent",
            original_model=existing.original_model,
            effective_model=existing.effective_model,
            streaming=existing.streaming,
        )
        # A gateway retry reaches the model exactly as a first attempt does, so it
        # has to carry the same identity stamp. Without it the invocation log
        # records the retried call with no user attached, the async attribution
        # pipeline skips the record, and that call's tokens never debit.
        return _request_output(
            parsed.body, parsed.original_model, existing.effective_model,
            converse_path=parsed.converse_path,
            request_metadata=(
                {"user": existing.user_id, "request_id": existing.request_id}
                if parsed.converse_path
                else None
            ),
            workspace_id=workspace_id,
            openai_format=parsed.openai_format,
        )

    def _budget_error(self) -> GovernanceError:
        return GovernanceError(
            429,
            "budget_exceeded",
            f"Token budget for this {self.settings.budget_window} exceeded",
            retry_after=self.settings.retry_after_seconds,
        )

    def _check_active_hours(self, now: int) -> None:
        """Refuse requests outside the deployment's active hours. Equal start
        and end means the gate is off. start > end wraps overnight (the
        22:00-06:00 shift), tested as the OR of the two ranges on the local
        wall clock, which also keeps DST correct for named zones."""
        s = self.settings
        if s.active_hours_start == s.active_hours_end:
            return
        from zoneinfo import ZoneInfo

        local = datetime.fromtimestamp(now, ZoneInfo(s.active_hours_tz))
        minute = local.hour * 60 + local.minute
        if s.active_hours_start < s.active_hours_end:
            active = s.active_hours_start <= minute < s.active_hours_end
        else:
            active = minute >= s.active_hours_start or minute < s.active_hours_end
        if not active:
            opens = f"{s.active_hours_start // 60:02d}:{s.active_hours_start % 60:02d}"
            raise GovernanceError(
                429,
                "outside_active_hours",
                f"Requests are admitted between active hours only; opens at {opens} {s.active_hours_tz}",
                retry_after=self.settings.retry_after_seconds,
            )

    def _log(
        self,
        event_name: str,
        *,
        request_id: str | None,
        user_id: str | None,
        action: str,
        **fields: Any,
    ) -> None:
        record = {
            "event": event_name,
            "request_id": request_id,
            "user_id": user_id,
            "action": action,
        }
        record.update(fields)
        # Invariant: the log schema accepts bounded identifiers, counters,
        # status values, exception class names, and length-capped AWS service
        # error messages. JWTs, claims, prompts, completions, and headers are
        # excluded, and no caller-supplied value is logged verbatim.
        #
        # The service error message is the one deliberate exception, and only on
        # the in-band downgrade path (see converse_direct_failed): the denied
        # resource in an AccessDenied is the whole diagnostic, so the message is
        # kept and capped at 600 characters instead of dropped. It is text the
        # AWS SDK generated, not text a caller sent, so a caller cannot choose
        # what lands here, only which service error is raised.
        self.logger(json.dumps(record, separators=(",", ":"), sort_keys=True))


def _parse_request(event: Mapping[str, Any], settings: Settings) -> ParsedRequest:
    http = event.get("http")
    gateway_request = http.get("gatewayRequest") if isinstance(http, Mapping) else None
    if not isinstance(gateway_request, Mapping):
        raise GovernanceError(
            400, "invalid_interceptor_event", "Gateway request is missing"
        )
    headers = gateway_request.get("headers")
    if not isinstance(headers, Mapping):
        raise GovernanceError(401, "missing_bearer_token", "Bearer token is required")
    user_id = _extract_subject(headers, settings)
    anthropic_beta = str(
        headers.get("anthropic-beta") or headers.get("Anthropic-Beta") or ""
    ).strip()[:1024]
    raw_body = _decode_body(gateway_request.get("body"), settings.max_request_bytes)
    body = _strict_json(raw_body)
    if not isinstance(body, dict):
        raise GovernanceError(400, "invalid_json", "Request body must be a JSON object")
    _validate_json_limits(body, settings)
    converse_path_value: str | None = None
    # Dual-format parsing: Anthropic/OpenAI (body.model) vs Bedrock Converse
    # (model in URL path, inferenceConfig.maxTokens, /converse-stream for SSE).
    model = body.get("model")
    # Detect OpenAI-format requests from the gateway path. The mantle endpoint
    # requires different workspace headers per API format: anthropic-workspace-id
    # for /v1/messages, openai-project for /v1/chat/completions and /v1/responses.
    request_path = str(gateway_request.get("path") or gateway_request.get("uri") or "")
    is_openai_format = "/chat/completions" in request_path or "/responses" in request_path
    is_responses_api = "/responses" in request_path
    # Which door a request arrived through is decided by the URL path, never by
    # the body. The gateway routes on the path, so the model named there is the
    # one that actually runs, and the Bedrock Converse API accepts an unknown
    # top-level "model" member and ignores it (HTTP 200, the path
    # model served the call). Trusting a body value on this door would let a
    # caller show an allowed model to the checks below while the path selected a
    # different one, and would leave converse_path unset, so the request also
    # went out unstamped and the async attribution pipeline never debited it.
    # Only the Bedrock passthrough door carries /model/{id}/ in its path, which
    # makes that the discriminator.
    path_model = _extract_model_from_path(request_path)
    if path_model is not None:
        if isinstance(model, str) and model != path_model:
            raise GovernanceError(
                400,
                "model_mismatch",
                "Body model does not match the model in the request path",
            )
        # Clearing it selects the path-derived branches below, which set
        # converse_path so the request is stamped and settled. A body model that
        # agrees with the path is what the Bedrock SDKs send on InvokeModel, so
        # it is accepted and then re-derived rather than rejected.
        model = None
    else:
        # Mantle door. The project header is what AWS/BedrockMantle attributes
        # spend to, so it has to come from the POLICY item and nowhere else. A
        # request that carries its own is refused rather than repaired: the
        # interceptor only injects a project when the user's policy names one,
        # and it cannot rely on a transform to overwrite a header it is not
        # injecting. Refusing is also independent of whether a transformed
        # request replaces the inbound headers or merges with them, which is a
        # gateway detail this sample should not depend on for a security
        # property. Legitimate clients never send these; the interceptor adds
        # the right one for the API format.
        client_project_header = next(
            (
                name
                for name in headers
                if isinstance(name, str)
                and name.lower() in _CLIENT_FORBIDDEN_HEADERS
            ),
            None,
        )
        if client_project_header is not None:
            raise GovernanceError(
                400,
                "workspace_header_not_allowed",
                f"{client_project_header} is set by the gateway, not the client",
            )
    if model is not None and is_responses_api:
        # OpenAI Responses API format: top-level "input" (string or list)
        # replaces "messages", and "max_output_tokens" replaces "max_tokens".
        # Validating with chat-completions field names here rejected every
        # spec-compliant Responses request with 400.
        try:
            _validate_model(model)
        except ValueError:
            raise GovernanceError(400, "invalid_model", "model is not an allowed identifier") from None
        responses_input = body.get("input")
        if not isinstance(responses_input, (str, list)) or not responses_input:
            raise GovernanceError(
                400, "invalid_input", "input must be a non-empty string or list"
            )
        max_tokens = body.get("max_output_tokens", settings.max_max_tokens)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise GovernanceError(
                400, "invalid_max_output_tokens", "max_output_tokens must be an integer"
            )
        max_tokens = max(1, min(max_tokens, settings.max_max_tokens))
        # Normalize input to the message-list shape the shared estimate and
        # guardrail extraction below expect.
        messages = (
            [{"role": "user", "content": responses_input}]
            if isinstance(responses_input, str)
            else responses_input
        )
        streaming = body.get("stream", False)
        if not isinstance(streaming, bool):
            raise GovernanceError(400, "invalid_stream", "stream must be a boolean")
    elif model is not None:
        # Anthropic Messages / OpenAI Chat Completions format (existing path)
        try:
            _validate_model(model)
        except ValueError:
            raise GovernanceError(400, "invalid_model", "model is not an allowed identifier") from None
        max_tokens = body.get("max_tokens")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise GovernanceError(400, "invalid_max_tokens", "max_tokens must be an integer")
        if not 1 <= max_tokens <= settings.max_max_tokens:
            raise GovernanceError(
                400,
                "invalid_max_tokens",
                f"max_tokens must be between 1 and {settings.max_max_tokens}",
            )
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise GovernanceError(
                400, "invalid_messages", "messages must be a non-empty list"
            )
        streaming = body.get("stream", False)
        if not isinstance(streaming, bool):
            raise GovernanceError(400, "invalid_stream", "stream must be a boolean")
    elif body.get("max_tokens") is not None:
        # Bedrock InvokeModel format (Claude Code Bedrock mode): model in
        # path, max_tokens in body, streaming by /invoke-with-response-stream.
        path = request_path
        model = path_model
        if not model:
            raise GovernanceError(400, "invalid_model", "model is not an allowed identifier") from None
        max_tokens = body.get("max_tokens")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise GovernanceError(400, "invalid_max_tokens", "max_tokens must be an integer")
        if not 1 <= max_tokens <= settings.max_max_tokens:
            raise GovernanceError(
                400, "invalid_max_tokens",
                f"max_tokens must be between 1 and {settings.max_max_tokens}",
            )
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise GovernanceError(
                400, "invalid_messages", "messages must be a non-empty list"
            )
        streaming = "/invoke-with-response-stream" in path
        converse_path_value = path
    else:
        # Bedrock Converse format: model is in the URL path, max_tokens is
        # nested in inferenceConfig, streaming is by path suffix.
        path = request_path
        model = path_model
        if not model:
            raise GovernanceError(400, "invalid_model", "model is not an allowed identifier") from None
        inference_config = body.get("inferenceConfig") or {}
        max_tokens = inference_config.get("maxTokens", settings.max_max_tokens)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            max_tokens = settings.max_max_tokens
        max_tokens = max(1, min(max_tokens, settings.max_max_tokens))
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise GovernanceError(
                400, "invalid_messages", "messages must be a non-empty list"
            )
        streaming = "/converse-stream" in path
        converse_path_value = path
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    input_estimate = max(
        settings.minimum_input_tokens,
        math.ceil(len(canonical) / settings.input_bytes_per_token)
        + len(messages) * settings.input_message_overhead_tokens,
    )
    converse_path_final = converse_path_value
    guardrail_text = _extract_prompt_text(
        messages, body.get("system"), settings.max_string_bytes
    )
    return ParsedRequest(
        body=body,
        user_id=user_id,
        original_model=model,
        requested_max_tokens=max_tokens,
        input_estimate_tokens=input_estimate,
        streaming=streaming,
        fingerprint=hashlib.sha256(canonical).hexdigest(),
        converse_path=converse_path_final,
        guardrail_text=guardrail_text,
        openai_format=is_openai_format,
        anthropic_beta=anthropic_beta,
    )



def _extract_prompt_text(messages: Any, system: Any, limit: int) -> str:
    """Collect user-authored text from a request body for guardrail screening.

    Works across all three wire formats because each carries text in one of a
    few shapes: a bare string (Anthropic content), a list of {"text": ...}
    blocks (Converse content), or a system string or list of {"text": ...}.
    Only text is screened; non-text blocks (images, tool results) are skipped.
    The result is truncated to `limit` characters to bound the guardrail call.
    """
    parts: list[str] = []

    def _add(value: Any) -> None:
        if isinstance(value, str) and value:
            parts.append(value)

    if isinstance(system, str):
        _add(system)
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, Mapping):
                _add(block.get("text"))

    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            content = message.get("content")
            if isinstance(content, str):
                _add(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, Mapping):
                        _add(block.get("text"))

    joined = "\n".join(parts)
    return joined[:limit]


def _extract_model_from_path(path: str) -> str | None:
    """Extract model id from Converse URL path like /model/{modelId}/converse."""
    # Patterns: /{target}/model/{modelId}/converse or /model/{modelId}/converse
    parts = path.split("/")
    for i, part in enumerate(parts):
        if part == "model" and i + 1 < len(parts):
            # model id is everything between /model/ and /converse or end
            model_id = parts[i + 1]
            if MODEL_PATTERN.match(model_id):
                return model_id
    return None

def _extract_subject(headers: Mapping[str, Any], settings: Settings) -> str:
    authorization: str | None = None
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == "authorization":
            authorization = value if isinstance(value, str) else None
            break
    if authorization is None:
        raise GovernanceError(401, "missing_bearer_token", "Bearer token is required")
    if len(authorization.encode("utf-8")) > settings.max_authorization_bytes:
        raise GovernanceError(401, "invalid_bearer_token", "Bearer token is malformed")
    match = JWT_PATTERN.fullmatch(authorization)
    if match is None:
        raise GovernanceError(401, "invalid_bearer_token", "Bearer token is malformed")
    payload_segment = match.group(1).split(".")[1]
    try:
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        raise GovernanceError(401, "invalid_bearer_token", "Bearer token is malformed") from None
    # Invariant: AgentCore CUSTOM_JWT validates the signature, issuer,
    # expiry, and client before invocation. This source-restricted Lambda reads only
    # sub and never logs or persists the token or full claims.
    subject = claims.get("sub") if isinstance(claims, Mapping) else None
    if not isinstance(subject, str) or USER_ID_PATTERN.fullmatch(subject) is None:
        raise GovernanceError(
            403,
            "invalid_user_id",
            "JWT subject is not an allowed user identifier",
        )
    return subject


def _request_id(context: Any, *, required: bool) -> str | None:
    client_context = getattr(context, "client_context", None)
    if isinstance(client_context, Mapping):
        custom = client_context.get("custom")
    else:
        custom = getattr(client_context, "custom", None)
    request_id = custom.get("REQUEST_ID") if isinstance(custom, Mapping) else None
    if request_id is None and not required:
        return None
    if not isinstance(request_id, str) or REQUEST_ID_PATTERN.fullmatch(request_id) is None:
        raise GovernanceError(
            403,
            "missing_request_id",
            "Trusted gateway request identifier is required",
        )
    return request_id


def _strict_json(raw: bytes) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise GovernanceError(
                    400, "duplicate_json_key", "Request JSON has duplicate keys"
                )
            result[key] = value
        return result

    def reject_constant(_: str) -> Any:
        raise GovernanceError(
            400, "invalid_json_number", "Request JSON numbers must be finite"
        )

    try:
        return json.loads(
            raw, object_pairs_hook=object_pairs, parse_constant=reject_constant
        )
    except GovernanceError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise GovernanceError(
            400, "invalid_json", "Request body must be UTF-8 JSON"
        ) from None


def _validate_json_limits(value: Any, settings: Settings) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > settings.max_json_nodes:
            raise GovernanceError(
                400, "request_too_complex", "Request JSON has too many values"
            )
        if depth > settings.max_json_depth:
            raise GovernanceError(
                400, "request_too_deep", "Request JSON is nested too deeply"
            )
        if isinstance(current, str):
            if len(current.encode("utf-8")) > settings.max_string_bytes:
                raise GovernanceError(
                    400, "request_string_too_large", "Request string is too large"
                )
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise GovernanceError(
                    400, "invalid_json_number", "Request JSON numbers must be finite"
                )
        elif isinstance(current, Mapping):
            for key, nested in current.items():
                if not isinstance(key, str) or len(key.encode("utf-8")) > 256:
                    raise GovernanceError(
                        400, "invalid_request_field", "Request field name is invalid"
                    )
                stack.append((nested, depth + 1))
        elif isinstance(current, list):
            stack.extend((nested, depth + 1) for nested in current)
        elif current is not None and not isinstance(current, (bool, int)):
            raise GovernanceError(
                400, "invalid_json", "Request contains an unsupported JSON value"
            )


def _parse_response_usage(response: Mapping[str, Any], max_bytes: int) -> Usage | None:
    body = response.get("body")
    if not isinstance(body, str) or not body:
        return None
    try:
        raw = _decode_body(body, max_bytes)
    except GovernanceError:
        return None
    content_type = response.get("contentType")
    # Bedrock streaming (InvokeModelWithResponseStream, ConverseStream)
    # arrives as binary AWS EventStream framing, not text SSE. Detect it by
    # content type or by the frame prelude before attempting UTF-8.
    if (
        isinstance(content_type, str)
        and "vnd.amazon.eventstream" in content_type.lower()
    ) or _looks_like_eventstream(raw):
        return _parse_eventstream_usage(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if (
        isinstance(content_type, str)
        and "event-stream" in content_type.lower()
    ) or text.lstrip().startswith(("event:", "data:")):
        return _parse_sse_usage(text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return _usage_from_mapping(payload.get("usage")) if isinstance(payload, Mapping) else None


def _looks_like_eventstream(raw: bytes) -> bool:
    """Heuristic: an EventStream frame starts with a plausible big-endian
    total length that does not exceed the body size."""
    if len(raw) < 16:
        return False
    total_len = int.from_bytes(raw[0:4], "big")
    headers_len = int.from_bytes(raw[4:8], "big")
    return 16 <= total_len <= len(raw) and headers_len < total_len


def _parse_eventstream_usage(raw: bytes) -> Usage | None:
    """Extract usage from AWS binary EventStream frames.

    Frame layout: 4B total length, 4B headers length, 4B prelude CRC,
    headers, payload, 4B message CRC. Payloads are JSON like
    {"bytes": "<base64>"} where the base64 decodes to the model event
    (message_start carries input usage, message_delta carries output usage
    for the Anthropic format; Converse stream metadata carries camelCase
    usage). Maximums across events are taken, mirroring _parse_sse_usage.
    """
    maximums = {field: 0 for field in USAGE_FIELDS}
    found = False
    offset = 0
    while offset + 16 <= len(raw):
        total_len = int.from_bytes(raw[offset : offset + 4], "big")
        headers_len = int.from_bytes(raw[offset + 4 : offset + 8], "big")
        if total_len < 16 or offset + total_len > len(raw):
            break
        payload_start = offset + 12 + headers_len
        payload_end = offset + total_len - 4
        offset += total_len
        if payload_start >= payload_end:
            continue
        try:
            envelope = json.loads(raw[payload_start:payload_end])
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(envelope, Mapping):
            continue
        # InvokeModel streams wrap the event in {"bytes": base64}; Converse
        # streams put the event JSON directly in the payload.
        event: Any = envelope
        encoded = envelope.get("bytes")
        if isinstance(encoded, str):
            try:
                event = json.loads(base64.b64decode(encoded))
            except (ValueError, json.JSONDecodeError):
                continue
        if not isinstance(event, Mapping):
            continue
        candidates = [event.get("usage")]
        message = event.get("message")
        if isinstance(message, Mapping):
            candidates.append(message.get("usage"))
        metadata = event.get("metadata")
        if isinstance(metadata, Mapping):
            candidates.append(metadata.get("usage"))
        for candidate in candidates:
            usage = _usage_from_mapping(candidate)
            if usage is None:
                continue
            found = True
            for field in USAGE_FIELDS:
                maximums[field] = max(maximums[field], getattr(usage, field))
    return Usage(**maximums) if found else None


def _parse_sse_usage(text: str) -> Usage | None:
    maximums = {field: 0 for field in USAGE_FIELDS}
    found = False
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, Mapping):
            continue
        candidates = [payload.get("usage")]
        message = payload.get("message")
        if isinstance(message, Mapping):
            candidates.append(message.get("usage"))
        for candidate in candidates:
            usage = _usage_from_mapping(candidate)
            if usage is None:
                continue
            found = True
            for field in USAGE_FIELDS:
                maximums[field] = max(maximums[field], getattr(usage, field))
    return Usage(**maximums) if found else None


# Converse response uses camelCase; Anthropic uses snake_case.
_CONVERSE_FIELD_MAP: dict[str, str] = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_input_tokens": "cacheReadInputTokenCount",
    "cache_creation_input_tokens": "cacheWriteInputTokenCount",
}


def _usage_from_mapping(value: Any) -> Usage | None:
    if not isinstance(value, Mapping):
        return None
    found = False
    values: dict[str, int] = {}
    for field in USAGE_FIELDS:
        # Try snake_case first (Anthropic), then camelCase (Converse)
        token_count = value.get(field)
        if token_count is None:
            token_count = value.get(_CONVERSE_FIELD_MAP.get(field, ""), 0)
        if token_count is None:
            token_count = 0
        if isinstance(token_count, bool) or not isinstance(token_count, int) or token_count < 0:
            return None
        values[field] = token_count
        found = found or (field in value or _CONVERSE_FIELD_MAP.get(field, "") in value)
    return Usage(**values) if found else None


def _decode_body(value: Any, max_bytes: int) -> bytes:
    if not isinstance(value, str) or not value:
        raise GovernanceError(400, "invalid_body", "A base64 body is required")
    if len(value) > math.ceil(max_bytes / 3) * 4 + 4:
        raise GovernanceError(413, "request_too_large", "Body exceeds configured limit")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise GovernanceError(400, "invalid_body", "Body must be valid base64") from None
    if len(decoded) > max_bytes:
        raise GovernanceError(413, "request_too_large", "Body exceeds configured limit")
    return decoded


def _request_output(
    body: Mapping[str, Any], original_model: str, effective_model: str,
    *, converse_path: str | None = None,
    request_metadata: dict[str, str] | None = None,
    workspace_id: str | None = None,
    openai_format: bool = False,
) -> dict[str, Any]:
    # workspace_id tags mantle-path requests (converse_path is None) with the
    # user's project so AWS/BedrockMantle metrics attribute the usage. It is
    # only meaningful on the mantle path; the passthrough (converse) path uses
    # invocation-log requestMetadata instead, so ignore it there.
    inject_workspace = bool(workspace_id) and converse_path is None
    needs_transform = (
        original_model != effective_model
        or (request_metadata and converse_path)
        or inject_workspace
    )
    if not needs_transform:
        return _passthrough_output()
    transformed = dict(body)
    if "model" in transformed:
        transformed["model"] = effective_model
    if request_metadata and converse_path:
        transformed["requestMetadata"] = request_metadata
    encoded = base64.b64encode(
        json.dumps(
            transformed, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).decode("ascii")
    transformed_request: dict[str, Any] = {"body": encoded}
    headers: dict[str, str] = {}
    if converse_path and original_model != effective_model:
        headers["X-Effective-Model"] = effective_model
    # This value comes from the user's POLICY item, so it is authenticated. A
    # caller cannot substitute another user's project, because _parse_request
    # refuses any mantle-door request that arrives carrying one of these
    # headers; that refusal, not this injection, is what makes the attribution
    # unspoofable.
    # The mantle endpoint requires different header names per API format:
    # anthropic-workspace-id for Anthropic Messages, openai-project for
    # OpenAI Chat Completions and Responses.
    if inject_workspace:
        header_name = "openai-project" if openai_format else "anthropic-workspace-id"
        headers[header_name] = workspace_id  # type: ignore[assignment]
    if headers:
        transformed_request["headers"] = headers
    return {
        "interceptorOutputVersion": OUTPUT_VERSION,
        "http": {"transformedGatewayRequest": transformed_request},
    }


def _error_output(error: GovernanceError, request_id: str | None) -> dict[str, Any]:
    error_body: dict[str, Any] = {
        "type": error.code,
        "message": error.safe_message,
        "request_id": request_id,
    }
    if error.retry_after is not None:
        # The AgentCore Gateway strips custom response headers on a short-circuit,
        # so the machine-readable retry hint is carried in the body as well as the
        # (best-effort) Retry-After header below.
        error_body["retry_after"] = error.retry_after
    payload = {
        "type": "error",
        "error": error_body,
    }
    transformed: dict[str, Any] = {
        "statusCode": error.status_code,
        "contentType": "application/json",
        "body": base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii"),
    }
    if error.retry_after is not None:
        transformed["headers"] = {"Retry-After": str(error.retry_after)}
    return {
        "interceptorOutputVersion": OUTPUT_VERSION,
        "http": {"transformedGatewayResponse": transformed},
    }


def _passthrough_output() -> dict[str, Any]:
    return {"interceptorOutputVersion": OUTPUT_VERSION, "http": {}}


def _interception_point(event: Mapping[str, Any]) -> str:
    http = event.get("http")
    if (
        isinstance(http, Mapping)
        and http.get("gatewayRequest") is None
        and ("gatewayRequest" in http or "gatewayResponse" in http)
    ):
        return "RESPONSE"
    return "REQUEST"


def _gateway_response(event: Mapping[str, Any]) -> Mapping[str, Any]:
    http = event.get("http")
    response = http.get("gatewayResponse") if isinstance(http, Mapping) else None
    return response if isinstance(response, Mapping) else {}


def _response_status(response: Mapping[str, Any]) -> int:
    value = response.get("statusCode", 502)
    return value if isinstance(value, int) and not isinstance(value, bool) else 502


def _validate_model(model: Any) -> None:
    if not isinstance(model, str) or MODEL_PATTERN.fullmatch(model) is None:
        raise ValueError("model is invalid")


def _request_item(context: RequestContext) -> dict[str, dict[str, Any]]:
    return {
        "pk": _s(f"REQ#{context.request_id}"),
        "item_type": _s("REQUEST"),
        "request_id": _s(context.request_id),
        "user_id": _s(context.user_id),
        "usage_pk": _s(context.usage_pk),
        "usage_date": _s(context.usage_date),
        "original_model": _s(context.original_model),
        "effective_model": _s(context.effective_model),
        "requested_max_tokens": _n(context.requested_max_tokens),
        "input_estimate_tokens": _n(context.input_estimate_tokens),
        "action": _s(context.action),
        "streaming": _b(context.streaming),
        "fingerprint": _s(context.fingerprint),
        "created_at": _n(context.created_at),
        "expires_at": _n(context.expires_at),
        "interceptor_ms": _n(context.interceptor_ms),
    }


def _is_transaction_conflict(error: Exception) -> bool:
    """True only for a pure DynamoDB TransactionConflict cancellation.

    That reason means another writer held the item, which clears on retry. A
    ConditionalCheckFailed reason is handled by _is_conditional_failure and
    must never be routed here, because it carries a governance decision.
    """
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return False
    details = response.get("Error")
    code = details.get("Code") if isinstance(details, Mapping) else None
    if code != "TransactionCanceledException":
        return False
    reasons = response.get("CancellationReasons")
    return isinstance(reasons, list) and any(
        isinstance(reason, Mapping) and reason.get("Code") == "TransactionConflict"
        for reason in reasons
    )


def _is_conditional_failure(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return False
    details = response.get("Error")
    code = details.get("Code") if isinstance(details, Mapping) else None
    if code == "ConditionalCheckFailedException":
        return True
    if code != "TransactionCanceledException":
        return False
    reasons = response.get("CancellationReasons")
    if isinstance(reasons, list) and any(
        isinstance(reason, Mapping) and reason.get("Code") == "ConditionalCheckFailed"
        for reason in reasons
    ):
        return True
    message = details.get("Message") if isinstance(details, Mapping) else None
    return isinstance(message, str) and "ConditionalCheckFailed" in message


def _string(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, Mapping) or set(value) != {"S"} or not isinstance(value["S"], str):
        raise ValueError(f"{key} must be a DynamoDB string")
    return value["S"]


def _integer(item: Mapping[str, Any], key: str) -> int:
    value = item.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be a DynamoDB number")
    return _number(value, key)


def _number(value: Mapping[str, Any], key: str) -> int:
    if set(value) != {"N"} or not isinstance(value["N"], str):
        raise ValueError(f"{key} must be a DynamoDB number")
    try:
        parsed = Decimal(value["N"])
    except InvalidOperation:
        raise ValueError(f"{key} must be an integer") from None
    if parsed != parsed.to_integral_value():
        raise ValueError(f"{key} must be an integer")
    return int(parsed)


def _boolean(item: Mapping[str, Any], key: str) -> bool:
    value = item.get(key)
    if not isinstance(value, Mapping) or set(value) != {"BOOL"} or not isinstance(value["BOOL"], bool):
        raise ValueError(f"{key} must be a DynamoDB boolean")
    return value["BOOL"]


def _optional_string(item: Mapping[str, Any], key: str, default: str) -> str:
    return default if key not in item else _string(item, key)


def _optional_int(item: Mapping[str, Any], key: str, default: int) -> int:
    return default if key not in item else _integer(item, key)


def _optional_bool(item: Mapping[str, Any], key: str, default: bool) -> bool:
    return default if key not in item else _boolean(item, key)


def _string_collection(value: Any, key: str) -> frozenset[str]:
    if isinstance(value, Mapping) and set(value) == {"SS"}:
        raw = value["SS"]
        if isinstance(raw, list) and raw and all(isinstance(item, str) for item in raw):
            return frozenset(raw)
    if isinstance(value, Mapping) and set(value) == {"L"}:
        raw = value["L"]
        if isinstance(raw, list) and raw:
            values: list[str] = []
            for item in raw:
                if not isinstance(item, Mapping) or set(item) != {"S"} or not isinstance(item["S"], str):
                    break
                values.append(item["S"])
            else:
                return frozenset(values)
    raise ValueError(f"{key} must be a non-empty string set or string list")


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _b(value: bool) -> dict[str, bool]:
    return {"BOOL": value}


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name)
    value = default if raw is None else int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _env_budget_window() -> str:
    """BUDGET_WINDOW, validated fail-fast at cold start. A typo in a
    four-value enum should stop the deploy, not silently fall back."""
    value = os.environ.get("BUDGET_WINDOW", "day").strip().lower()
    if value not in _window.VALID_WINDOWS:
        raise ValueError(
            f"BUDGET_WINDOW must be one of {', '.join(_window.VALID_WINDOWS)}; got {value!r}"
        )
    return value


def _env_active_hours() -> dict[str, Any]:
    """ACTIVE_HOURS ("HH:MM-HH:MM", local wall time) and ACTIVE_HOURS_TZ
    (IANA name). Empty ACTIVE_HOURS, or equal start and end, disables the
    gate. Malformed values raise at cold start; an invalid timezone must not
    silently widen access hours."""
    raw = os.environ.get("ACTIVE_HOURS", "").strip()
    tz = os.environ.get("ACTIVE_HOURS_TZ", "UTC").strip() or "UTC"
    if raw:
        match = re.fullmatch(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})", raw)
        if not match:
            raise ValueError("ACTIVE_HOURS must look like HH:MM-HH:MM")
        sh, sm, eh, em = (int(g) for g in match.groups())
        if sh > 23 or eh > 23 or sm > 59 or em > 59:
            raise ValueError("ACTIVE_HOURS contains an invalid time")
        start, end = sh * 60 + sm, eh * 60 + em
    else:
        start = end = 0
    from zoneinfo import ZoneInfo

    ZoneInfo(tz)  # raises on an unknown zone: fail loud, not open
    return {
        "active_hours_start": start,
        "active_hours_end": end,
        "active_hours_tz": tz,
    }


_SERVICE: GovernanceService | None = None


def _build_service() -> GovernanceService:
    # boto3 comes from the Lambda runtime, so nothing is bundled. Importing it
    # here rather than at module scope keeps the module importable with the
    # standard library alone, so it can be read or exercised outside Lambda.
    import boto3  # type: ignore[import-not-found]
    from botocore.config import Config  # type: ignore[import-not-found]

    settings = Settings.from_env()
    # Invariant: use the Lambda execution role and regional SDK endpoint.
    # Credentials and caller-controlled endpoints are never accepted from environment.
    client = boto3.client(
        "dynamodb",
        config=Config(
            retries={"mode": "standard", "total_max_attempts": 4},
            connect_timeout=2,
            read_timeout=5,
        ),
    )
    return GovernanceService(settings, DynamoRepository(client, settings.table_name))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle one AgentCore Gateway REQUEST or RESPONSE interception event."""

    global _SERVICE
    try:
        if _SERVICE is None:
            _SERVICE = _build_service()
        return _SERVICE.handle(event, context)
    except Exception as error:
        # This path covers initialization only. It never logs event content or errors.
        print(
            json.dumps(
                {
                    "event": "handler_initialization_failed",
                    "action": "passthrough"
                    if _interception_point(event) == "RESPONSE"
                    else "fail_closed",
                    "error_type": type(error).__name__,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        if _interception_point(event) == "RESPONSE":
            return _passthrough_output()
        return _error_output(
            GovernanceError(
                503, "governance_unavailable", "Governance service unavailable"
            ),
            None,
        )
