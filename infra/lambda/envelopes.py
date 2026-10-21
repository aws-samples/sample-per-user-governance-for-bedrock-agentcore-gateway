# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""B6 surface-aware error envelopes (feature F5) — pure, dependency-free.

This module renders a gateway error into the wire shape of the **API surface
the client called**. It is intentionally free of boto3,
network calls, and any AWS SDK so the rendering is deterministic and unit
testable in isolation (mirrors ``detectors.py``).

Background — what the sample did before F5: ``_error_output`` in
``lambda_function.py`` emitted ONE fixed shape for every surface:

    {"type": "error",
     "error": {"type": <code>, "message": <msg>, "request_id": ..., "retry_after": <n>}}

That top-level ``{"type":"error","error":{...}}`` is **Anthropic-shaped** and is
what the gateway already returns for ``/v1/messages``. It did NOT branch on the
already-computed ``openai_format`` flag. F5 keeps that Anthropic body verbatim
for Anthropic surfaces and adds the OpenAI shape for OpenAI surfaces.

Wire shapes (REQ-API-008):

* Anthropic surfaces (``/v1/messages``, count-tokens, models):
      {"type": "error", "error": {"type": <anthropic_type>, "message": <msg>, ...}}
  — unchanged from the sample, so no regression to existing Anthropic clients.

* OpenAI surfaces (``/v1/chat/completions``, ``/v1/responses``):
      {"error": {"message": <msg>, "type": <openai_type>, "code": <code>, "param": null, ...}}
  — no top-level ``type``; the OpenAI SDK reads ``error.type``/``error.code``.

Scope note: **REQ-API-008A** (upstream provider-error passthrough translation on
the RESPONSE path) is OUT OF SCOPE for F5 — it is load-bearing / response-path
work governed by ARCHITECTURE §6 tiers and is deferred. This module only renders
**gateway-originated** errors (REQ-API-008).
"""

from __future__ import annotations

from typing import Any, Mapping

# --- surfaces ----------------------------------------------------------------

# The two client-facing wire families F5 renders. ``CONVERSE`` (bedrock-runtime
# door) keeps the pre-F5 neutral/Anthropic-ish body; a genuine Bedrock-SDK
# native shape is REQ-API-008A-adjacent and deferred with the response path.
SURFACE_OPENAI = "openai"
SURFACE_ANTHROPIC = "anthropic"

# Default shape when the called surface cannot be determined before the request
# body parses (stage-1 parse/auth failures on an unrecognized path). The sample
# emitted the Anthropic-shaped body for everything, so defaulting to Anthropic
# preserves the pre-F5 behavior exactly and is the least-surprising fallback for
# existing clients. (B6 design §2.6 proposes an ``errors.default_surface`` knob
# defaulting to openai; F5 intentionally defaults to Anthropic to match today's
# fixed shape — see B6-critique F4. Callers that CAN derive the surface from the
# path should pass it explicitly via ``surface_from_path``.)
DEFAULT_SURFACE = SURFACE_ANTHROPIC


def surface_from_path(path: str | None) -> str:
    """Best-effort surface discriminator from the gateway request path.

    The gateway routes on the URL path, so the surface is derivable from the
    path even when the body has not parsed (missing bearer, malformed JSON,
    413 body-too-large). OpenAI shapes live under ``/chat/completions`` and
    ``/responses``; everything else (``/v1/messages`` and unknown paths) falls
    to the Anthropic default, matching the pre-F5 fixed shape.
    """
    p = path or ""
    if "/chat/completions" in p or "/responses" in p:
        return SURFACE_OPENAI
    return DEFAULT_SURFACE


def surface_for(openai_format: bool) -> str:
    """Map the interceptor's already-computed ``openai_format`` flag to a
    surface. Reused by the post-parse error path so the shape is NOT recomputed
    from scratch — it rides the same flag ``_parse_request`` already set."""
    return SURFACE_OPENAI if openai_format else SURFACE_ANTHROPIC


# --- internal code -> family "type" vocabulary (REQ-API-008) -----------------
#
# One explicit, code-constant table (NOT runtime config, so it cannot be edited
# to violate the wire contract — B6 design §2.6). Maps each gateway internal
# ``GovernanceError.code`` to the conventional error ``type`` of each family.
# Keys are the internal codes raised across CP-3 stages 1-10; unknown codes fall
# back to ``_DEFAULT_*`` below (safe generic type, status is always preserved).
#
# OpenAI convention: authentication_error / invalid_request_error /
# permission_error (403) / insufficient_quota | rate_limit_exceeded (429) /
# api_error (5xx). Anthropic convention: authentication_error / permission_error
# / invalid_request_error / rate_limit_error (429) / api_error | overloaded_error.
#
# NOTE on 429 semantics (B6-critique F1): budget/USD-quota exhaustion maps to
# OpenAI ``insufficient_quota`` (a quota signal) and Anthropic ``rate_limit_error``;
# a pure per-minute rate cap maps to OpenAI ``rate_limit_exceeded``. Both carry
# ``retry_after`` in the body (the AgentCore Gateway strips custom response
# headers on short-circuit, so the hint cannot rely on the header alone).
_CODE_TYPES: dict[str, tuple[str, str]] = {
    # code:                       (openai_type,              anthropic_type)
    # -- 401 auth (REQ-API-013B / REQ-SEC-*) --
    "missing_bearer_token":       ("authentication_error",  "authentication_error"),
    "invalid_bearer_token":       ("authentication_error",  "authentication_error"),
    "revoked_key":                ("authentication_error",  "authentication_error"),
    # -- 403 entitlement / allowed-set / region / policy (B1/B3/B10) --
    "access_denied":              ("permission_error",      "permission_error"),
    "model_not_allowed":          ("permission_error",      "permission_error"),
    "region_not_allowed":         ("permission_error",      "permission_error"),
    "jurisdiction_denied":        ("permission_error",      "permission_error"),
    # -- 403 detector block (B7 / F4). Content-shaped fault -> invalid_request
    #    on both families; the message names only the detector CLASS, never the
    #    matched content (REQ-SEC-014). --
    "content_blocked":            ("invalid_request_error", "invalid_request_error"),
    # -- 400 malformed body / control fields (stage-1 validation) --
    "invalid_json":               ("invalid_request_error", "invalid_request_error"),
    "invalid_model":              ("invalid_request_error", "invalid_request_error"),
    "invalid_max_tokens":         ("invalid_request_error", "invalid_request_error"),
    "invalid_max_output_tokens":  ("invalid_request_error", "invalid_request_error"),
    "invalid_messages":           ("invalid_request_error", "invalid_request_error"),
    "invalid_input":              ("invalid_request_error", "invalid_request_error"),
    "invalid_stream":             ("invalid_request_error", "invalid_request_error"),
    "model_mismatch":             ("invalid_request_error", "invalid_request_error"),
    "workspace_header_not_allowed": ("invalid_request_error", "invalid_request_error"),
    "invalid_interceptor_event":  ("invalid_request_error", "invalid_request_error"),
    # -- 413 body too large (REQ-SEC-047) --
    "request_too_large":          ("invalid_request_error", "invalid_request_error"),
    # -- 429 quota / rate (B4 USD quota=F3, per-minute cap) (REQ-QUOTA-005) --
    "budget_exceeded":            ("insufficient_quota",    "rate_limit_error"),
    "usd_quota_exceeded":         ("insufficient_quota",    "rate_limit_error"),
    "rate_limit_exceeded":        ("rate_limit_exceeded",   "rate_limit_error"),
    "downgrade_unavailable":      ("insufficient_quota",    "rate_limit_error"),
    # -- 503 dependency / governance / policy (5xx server-side) --
    "governance_unavailable":     ("api_error",             "api_error"),
    "policy_invalid":             ("api_error",             "api_error"),
    "dependency_error":           ("api_error",             "api_error"),
}

# Unmapped codes: a safe generic type per family, chosen by status class so an
# unknown 4xx does not masquerade as a server fault (status is preserved either
# way; see ``render_error_body``).
_DEFAULT_OPENAI_4XX = "invalid_request_error"
_DEFAULT_ANTHROPIC_4XX = "invalid_request_error"
_DEFAULT_OPENAI_5XX = "api_error"
_DEFAULT_ANTHROPIC_5XX = "api_error"


def _family_type(code: str, status_code: int, surface: str) -> str:
    """Resolve the family-conventional ``type`` string for an internal code."""
    mapped = _CODE_TYPES.get(code)
    if mapped is not None:
        openai_type, anthropic_type = mapped
        return openai_type if surface == SURFACE_OPENAI else anthropic_type
    is_server = status_code >= 500
    if surface == SURFACE_OPENAI:
        return _DEFAULT_OPENAI_5XX if is_server else _DEFAULT_OPENAI_4XX
    return _DEFAULT_ANTHROPIC_5XX if is_server else _DEFAULT_ANTHROPIC_4XX


def render_error_body(
    status_code: int,
    error_code: str,
    message: str,
    surface: str,
    *,
    request_id: str | None = None,
    retry_after: int | None = None,
) -> dict[str, Any]:
    """Render a gateway error into the wire body for ``surface`` (REQ-API-008).

    Pure: returns only the JSON-serializable body dict. Status-code selection,
    base64 framing, and the best-effort ``Retry-After`` header stay in
    ``lambda_function._error_output`` so the transport envelope is unchanged.

    ``message`` is assumed already bounded and safe (the ``GovernanceError``
    contract); this renderer never adds internal detail or matched content.
    """
    family_type = _family_type(error_code, status_code, surface)
    if surface == SURFACE_OPENAI:
        # OpenAI shape: no top-level "type"; SDK reads error.type / error.code.
        error_obj: dict[str, Any] = {
            "message": message,
            "type": family_type,
            "code": error_code,
            "param": None,
        }
        if request_id is not None:
            error_obj["request_id"] = request_id
        if retry_after is not None:
            # OpenAI has no canonical body retry field; carry the machine hint
            # so a strict SDK backoff can find it even though AgentCore strips
            # the Retry-After header on short-circuit (B6-critique F1).
            error_obj["retry_after"] = retry_after
        return {"error": error_obj}

    # Anthropic shape (and CONVERSE / default) — byte-for-byte the pre-F5 body,
    # so existing /v1/messages clients see no change.
    error_obj = {
        "type": family_type,
        "message": message,
    }
    if request_id is not None:
        error_obj["request_id"] = request_id
    if retry_after is not None:
        error_obj["retry_after"] = retry_after
    return {"type": "error", "error": error_obj}


def code_type_table() -> Mapping[str, tuple[str, str]]:
    """Expose the code->(openai_type, anthropic_type) table (for tests)."""
    return dict(_CODE_TYPES)
