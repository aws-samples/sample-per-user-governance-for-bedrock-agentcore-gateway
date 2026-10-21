# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AgentCore Runtime entrypoint: an ALWAYS multi-agent Strands supervisor
(orchestrator + web-search sub-agent) that calls models THROUGH the AgentCore
Gateway per-user governance stack.

The runtime is unconditionally multi-agent with web search ALWAYS available.
There is no single-agent path, no LangGraph, no streaming, and no caller-supplied
mode/method. The caller supplies a MODEL id; the runtime DERIVES the gateway
wire door from it (see _method_for_model — the sole home of model->API routing):

  * "messages"  Anthropic Claude via the gateway /inference Messages door
                (AnthropicModel, Bearer auth).
  * "responses" OpenAI GPT non-OSS (gpt-5.6-*, gpt-6-*) via the /inference
                OpenAI Responses door (OpenAIResponsesModel).
  * "openai"    gpt-oss + open-weight via the /inference OpenAI Chat Completions
                door (OpenAIModel).

Each door runs a strands_multi orchestrator whose `web_search` tool is a SECOND
real Strands agent (its own model, from config websearch_agent.model, e.g.
Luna) that calls AgentCore web search and reasons over the results.

Governance note: model ENTITLEMENT is NOT enforced here — it is enforced at the
Gateway interceptor (per-user allowed_models), which every model call traverses
carrying the user's JWT. So a caller that bypasses the UI and invokes this
runtime directly is still governed: an unentitled model returns the Gateway's
403 model_not_allowed. The runtime authorizer only checks the JWT is valid.

Payload contract:
    {"prompt": str,           # the end user's prompt, executed as-is
     "model":  str,           # optional; defaults to DEFAULT_MODEL
     "base_url": str,         # <gateway_url>/inference (all doors ride /inference)
     "websearch_model": str,  # optional; the web-search sub-agent's model
     "websearch_max_tokens": int (optional),
     "websearch_system": str (optional),
     "max_tokens": int (optional),
     "jwt": str (optional)}   # fallback only; see JWT sourcing note below

Response: success {"method","model","text","timings"}; governance refusals and
errors {"method","model","error_status","error_type","error_message",
"retry_after","timings"}. The "timings" object carries time.time() floats:
received_at, gateway_call_start, done_at.

JWT sourcing (header-first, payload fallback)
---------------------------------------------
The bedrock-agentcore package behaves as follows
(bedrock_agentcore/runtime/app.py and context.py):

  * If the entrypoint function's SECOND parameter is literally named
    ``context``, the SDK passes a ``RequestContext`` pydantic model
    (``_takes_context`` checks ``params[1] == "context"``).
  * ``RequestContext.request_headers`` is a ``Dict[str, str]``. The SDK's
    ``_build_request_context`` normalises the inbound HTTP ``Authorization``
    header to the canonical key ``"Authorization"`` regardless of wire casing;
    other headers must pass an allowlist. ``BedrockAgentCoreContext
    .get_request_headers()`` exposes the same dict via a ContextVar.

So when the runtime is created with a customJWTAuthorizer and invoked over
HTTP with ``Authorization: Bearer <user-jwt>``, the user's token is available
here without ever being in the payload. When invoked via SigV4 (boto3
InvokeAgentRuntime) there is no bearer header, so we fall back to the
payload ``jwt`` key. The token lives in memory only and is never logged or
echoed back (see _redact).
"""
from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

# Default output cap when the payload does not specify one. Kept modest on
# purpose: an oversized cap wastes output budget on turns that do not need it,
# and the gateway records max_tokens on every request, so the demo's request
# rows read as if far more was asked for than was used.
DEFAULT_MAX_TOKENS = 4096
MAX_TOKENS_CEILING = 8192
CLIENT_TIMEOUT_S = 240.0  # heavy turns generate for minutes; do not give up at 60s

# The runtime is UNCONDITIONALLY multi-agent (orchestrator + web-search
# sub-agent) with web search ALWAYS available. There is no single-agent path and
# no "method" in the payload: the runtime DERIVES the door/API from the model id
# (see _method_for_model), which is the one place model->door routing lives.
#
# Per-family default model, applied when the caller omits "model":
#   messages  (Anthropic Claude)        -> the /inference Anthropic Messages door
#   responses (OpenAI GPT non-OSS)      -> the /inference OpenAI Responses door
#   openai    (gpt-oss + open-weight)   -> the /inference OpenAI Chat Completions door
DEFAULT_MODEL = "openai.gpt-5.6-sol"


def _method_for_model(model_id: str) -> str:
    """Derive the gateway wire door/method from the model id. This is the sole
    home of model->API routing (agreed placement: in the runtime, next to where
    the client is built, so every caller — UI, CLI, another agent — routes
    identically and there is no second source of truth to drift).

      * Anthropic Claude (anthropic.* / us.anthropic.*) -> "messages"
      * OpenAI GPT non-OSS (openai.gpt-5.*, openai.gpt-6*, and us. profiles),
        EXCLUDING gpt-oss                                -> "responses"
      * everything else (gpt-oss, open-weight nova/gemma/llama/qwen/...) -> "openai"
    """
    mid = (model_id or "").lower()
    if mid.startswith(("anthropic.", "us.anthropic.")):
        return "messages"
    if ("gpt-5" in mid or "gpt-6" in mid) and "oss" not in mid:
        return "responses"
    return "openai"

# The AgentCore gateway hostname shape, which is the only host this runtime
# needs to reach: <gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com.
# This is the default instead of a broad ".amazonaws.com" suffix because that
# suffix also covers hosts anyone can register under -- an S3 bucket
# (<bucket>.s3.amazonaws.com) or an API Gateway stage
# (<id>.execute-api.<region>.amazonaws.com) -- and base_url is where this
# container sends the caller's JWT.
_AGENTCORE_GATEWAY_HOST = re.compile(
    r"^[a-z0-9][a-z0-9-]*\.gateway\.bedrock-agentcore\."
    r"[a-z0-9-]+\.(amazonaws\.com(\.cn)?|api\.aws)$"
)

# For a gateway behind a custom domain. Each entry needs a leading dot so a
# lookalike host such as evil-amazonaws.com cannot pass. This setting replaces
# the host-shape check above rather than adding to it, so list every host this
# container may reach.
_ALLOWED_HOST_SUFFIXES = tuple(
    suffix.strip().lower()
    for suffix in os.environ.get("GATEWAY_HOST_SUFFIXES", "").split(",")
    if suffix.strip()
)

# Model ids differ per method: bare provider aliases on the /inference door,
# dated snapshot ids and regional inference profile ids on /bedrock-runtime,
# OpenAI-style ids on the compatible surface. The character set therefore has
# to stay broad. What this rejects is a value that would change the shape of
# the request built around it, which on converse/invoke means the URL path,
# since boto3 puts the model id there.
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")

# Long enough for any prompt the demo sends, short enough that one request
# cannot make the container build a body the gateway will only reject.
MAX_PROMPT_CHARS = 100_000


def _check_base_url(url: str) -> str:
    """Return url unchanged, or raise ValueError if it is not an AWS https URL.

    base_url decides where every client below sends the caller's JWT: the
    Anthropic and OpenAI SDKs take it with auth_token/api_key set to the token,
    and boto3 takes it as endpoint_url with the Bearer-swap hook attached.
    Unchecked, it turns this container into a credential relay to any host the
    caller names, and into a request source for anything reachable from here.
    Checking the scheme and the host closes both without constraining which
    gateway a deployer points the runtime at.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"base_url must be https, got {parts.scheme!r}")
    if parts.username or parts.password:
        raise ValueError("base_url must not carry credentials")
    host = (parts.hostname or "").lower()
    if _ALLOWED_HOST_SUFFIXES:
        if not any(host.endswith(suffix) for suffix in _ALLOWED_HOST_SUFFIXES):
            raise ValueError(f"base_url host is outside the allowlist: {host!r}")
    elif _AGENTCORE_GATEWAY_HOST.fullmatch(host) is None:
        raise ValueError(
            f"base_url host is outside the allowlist: {host!r}. Only an "
            "AgentCore gateway hostname is allowed by default; set "
            "GATEWAY_HOST_SUFFIXES to allow another host."
        )
    return url


def _redact(text: str, jwt: str) -> str:
    """Never let the JWT appear in any response or log line."""
    return text.replace(jwt, "<redacted-jwt>") if jwt else text


def _jwt_from_context(context: Any) -> str:
    """Extract the bearer token from RequestContext.request_headers."""
    headers = getattr(context, "request_headers", None) or {}
    auth = str(headers.get("Authorization", ""))
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth.strip()


def _error_details(error: BaseException) -> dict[str, Any]:
    """Walk the exception chain and surface gateway refusals as structured
    data: {error_status, error_type, error_message, retry_after}.

    Handles both the anthropic APIStatusError path (messages method) and the
    botocore ClientError path (converse/invoke methods). retry_after comes from
    the response body when the gateway includes it (e.g. budget_exceeded 429s),
    with the standard Retry-After response header as a fallback.

    Defensive: each SDK is imported lazily and independently so a missing
    package (e.g. anthropic absent when only the openai doors are packaged)
    NEVER turns error formatting itself into a 500. The openai doors
    (responses / chat) raise openai.APIStatusError, so that path is handled
    explicitly — it is how the gateway's model_not_allowed / guardrail_intervened
    403s surface for GPT and open-weight models.
    """
    # Lazy, independent SDK error classes — any missing package is simply skipped.
    try:
        import anthropic as _anthropic_sdk
        _anthropic_err = _anthropic_sdk.APIStatusError
    except Exception:
        _anthropic_err = ()
    try:
        import openai as _openai_sdk
        _openai_err = _openai_sdk.APIStatusError
    except Exception:
        _openai_err = ()

    cursor: BaseException | None = error
    while cursor is not None:
        # Anthropic Messages door (body is a dict with nested error).
        if _anthropic_err and isinstance(cursor, _anthropic_err):
            body = getattr(cursor, "body", None)
            err = body.get("error", {}) if isinstance(body, dict) else {}
            retry_after = None
            if isinstance(body, dict):
                retry_after = body.get("retry_after") or err.get("retry_after")
            if retry_after is None:
                response = getattr(cursor, "response", None)
                header = getattr(response, "headers", {}) or {}
                retry_after = header.get("retry-after")
            return {
                "error_status": getattr(cursor, "status_code", None),
                "error_type": err.get("type") or err.get("code"),
                "error_message": str(err.get("message"))[:300],
                "retry_after": retry_after,
            }
        # OpenAI Responses / Chat Completions doors (gpt non-oss + gpt-oss +
        # open-weight). The gateway's error envelope rides under .body.error or
        # is reconstructable from the message; code carries model_not_allowed /
        # guardrail_intervened / insufficient_quota.
        if _openai_err and isinstance(cursor, _openai_err):
            body = getattr(cursor, "body", None)
            err = body.get("error", {}) if isinstance(body, dict) else {}
            response = getattr(cursor, "response", None)
            header = getattr(response, "headers", {}) or {}
            return {
                "error_status": getattr(cursor, "status_code", None),
                "error_type": err.get("code") or err.get("type"),
                "error_message": str(err.get("message") or cursor)[:300],
                "retry_after": header.get("retry-after"),
            }
        botocore_details = _botocore_error_details(cursor)
        if botocore_details is not None:
            return botocore_details
        cursor = cursor.__cause__ or cursor.__context__
    return {"error_status": None, "error_type": type(error).__name__,
            "error_message": str(error)[:300], "retry_after": None}


def _botocore_error_details(cursor: BaseException) -> dict[str, Any] | None:
    """Surface a botocore ClientError (converse/invoke door) as structured
    data, or None when ``cursor`` is not a botocore client error."""
    try:
        from botocore.exceptions import ClientError
    except ImportError:
        return None
    if not isinstance(cursor, ClientError):
        return None
    response = getattr(cursor, "response", None) or {}
    meta = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
    err = response.get("Error", {}) if isinstance(response, dict) else {}
    status = meta.get("HTTPStatusCode")
    headers = meta.get("HTTPHeaders", {}) or {}
    retry_after = headers.get("retry-after")
    return {
        "error_status": status,
        "error_type": err.get("Code") or "gateway_error",
        "error_message": str(err.get("Message", cursor))[:300],
        "retry_after": retry_after,
    }


def _cap_strands_retries() -> None:
    """Fail fast on gateway 429s instead of retrying with backoff.

    MAX_ATTEMPTS defaults to 6 in strands.event_loop.event_loop and is
    from-imported by name into strands.agent.agent, so the constant exists as
    a separate binding in BOTH namespaces and BOTH must be patched. Without
    this, a budget-refused request spends ~2 minutes in throttle retries
    before the 429 surfaces.
    """
    import strands.agent.agent as strands_agent
    import strands.event_loop.event_loop as strands_event_loop

    strands_event_loop.MAX_ATTEMPTS = 1
    strands_agent.MAX_ATTEMPTS = 1


# ---------------------------------------------------------------------------
# messages method: Anthropic Messages API via the /inference door (Bearer)
# ---------------------------------------------------------------------------
def _strands_model(jwt: str, base_url: str, model: str,
                   max_tokens: int = DEFAULT_MAX_TOKENS):
    from strands.models.anthropic import AnthropicModel

    # Auth requirement: client_args must use auth_token, NOT api_key.
    # auth_token sends "Authorization: Bearer <jwt>", which the gateway's JWT
    # authorizer requires; api_key would send x-api-key and get a 401.
    # max_retries=0: surface governance refusals immediately, never retry.
    return AnthropicModel(
        client_args={
            "auth_token": jwt,
            "base_url": base_url,
            "timeout": CLIENT_TIMEOUT_S,
            "max_retries": 0,
        },
        model_id=model,
        max_tokens=max_tokens,
    )


def _partial_from_agent(agent: Any) -> str:
    """Recover the partial assistant text strands appends to agent.messages
    when generation stops at the max_tokens output cap.

    Each level of the shape is checked rather than wrapped in a broad except.
    The only thing that can go wrong here is strands changing the message
    shape, and a type guard states the shape this expects instead of
    swallowing every exception on the way through it."""
    messages = getattr(agent, "messages", None)
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("text")
        ]
        if parts:
            return "".join(parts)
    return ""


def _call_agent(agent: Any, prompt: str, timings: dict[str, Any]) -> str:
    """Run a strands agent; a max_tokens stop returns the partial reply with
    stopped_reason recorded in timings instead of leaking the raw exception
    (which carries framework doc URLs) to the client."""
    from strands.types.exceptions import MaxTokensReachedException

    timings["gateway_call_start"] = time.time()
    try:
        text = str(agent(prompt))
        timings["stopped_reason"] = "end_turn"
        return text
    except MaxTokensReachedException:
        timings["stopped_reason"] = "max_tokens"
        return _partial_from_agent(agent)


def _run_strands_multi_messages(jwt: str, base_url: str, model: str, prompt: str,
                                max_tokens: int, timings: dict[str, Any]) -> str:
    """Orchestrator (Anthropic Claude via the /inference Messages door) with the
    web-search sub-agent wired as a tool. Web search is ALWAYS available."""
    from strands import Agent

    _cap_strands_retries()
    orchestrator = Agent(
        model=_strands_model(jwt, base_url, model, max_tokens),
        tools=[_make_websearch_tool(jwt, base_url, timings)],
        system_prompt=_ORCHESTRATOR_SYSTEM,
        callback_handler=None,
    )
    return _call_agent(orchestrator, _seed_prompt(prompt), timings)

# ---------------------------------------------------------------------------
# openai method: OpenAI-compatible Chat Completions via the /inference door
# ---------------------------------------------------------------------------
def _openai_base_url(base_url: str) -> str:
    """The OpenAI SDK appends /chat/completions to base_url, so point it at the
    gateway's /inference/v1 root. Idempotent when the caller already added /v1."""
    trimmed = base_url.rstrip("/")
    return trimmed if trimmed.endswith("/v1") else trimmed + "/v1"


def _openai_model(jwt: str, base_url: str, model: str,
                  max_tokens: int = DEFAULT_MAX_TOKENS):
    """Build a Strands OpenAIModel (Chat Completions) pointed at the gateway
    /inference/v1 door. For gpt-oss + open-weight models.

    Auth: the OpenAI SDK sends api_key as "Authorization: Bearer <key>", so the
    user's JWT as api_key authenticates against the gateway's JWT authorizer
    directly. max_retries=0 surfaces governance refusals immediately."""
    from strands.models.openai import OpenAIModel

    return OpenAIModel(
        client_args={
            "api_key": jwt,
            "base_url": _openai_base_url(base_url),
            "timeout": CLIENT_TIMEOUT_S,
            "max_retries": 0,
        },
        model_id=model,
        params={"max_tokens": max_tokens},
    )


def _run_strands_multi_openai(jwt: str, base_url: str, model: str, prompt: str,
                              max_tokens: int, timings: dict[str, Any]) -> str:
    """Orchestrator (gpt-oss / open-weight via the /inference Chat Completions
    door) with the web-search sub-agent wired as a tool. Web search is ALWAYS
    available."""
    from strands import Agent

    _cap_strands_retries()
    orchestrator = Agent(
        model=_openai_model(jwt, base_url, model, max_tokens),
        tools=[_make_websearch_tool(jwt, base_url, timings)],
        system_prompt=_ORCHESTRATOR_SYSTEM,
        callback_handler=None,
    )
    return _call_agent(orchestrator, _seed_prompt(prompt), timings)


# ---------------------------------------------------------------------------
# responses method: OpenAI Responses API via the /inference door (GPT non-OSS)
# ---------------------------------------------------------------------------
# Hosted GPT non-OSS models (openai.gpt-5.6-*, gpt-6-*) are served by the gateway
# inference target on the OpenAI *Responses* door (/inference/v1/responses),
# verified live 2026-09-16. The matching Strands provider is OpenAIResponsesModel
# (NOT the Chat-Completions OpenAIModel). Same auth model as the openai method:
# the OpenAI SDK sends api_key as "Authorization: Bearer", so the user's JWT as
# api_key authenticates against the gateway directly.

# Per-invocation web-search sub-agent config, resolved from config.yaml by the
# CALLER (server) and passed in the payload. Held in a ContextVar so it reaches
# the fixed-signature runner without threading a new positional through every
# runner. Stateless: set at the top of invoke(), read inside the multi runner.
import contextvars as _contextvars

_WEBSEARCH_MODEL: _contextvars.ContextVar[str] = _contextvars.ContextVar(
    "websearch_model", default=""
)
_WEBSEARCH_MAX_TOKENS: _contextvars.ContextVar[int] = _contextvars.ContextVar(
    "websearch_max_tokens", default=1024
)
_WEBSEARCH_SYSTEM: _contextvars.ContextVar[str] = _contextvars.ContextVar(
    "websearch_system",
    default=(
        "You are a focused web-research assistant. Use the web search tool to "
        "gather current information, then answer concisely and factually, "
        "grounding every claim in the results and citing source URLs."
    ),
)

# Orchestrator steering, sent as the top-level system prompt. This is now safe:
# the gateway's PROBABILISTIC guardrail (ApplyGuardrail) was scoped (2026-09-17,
# Option A) to screen user/message content only and EXEMPT the trusted system
# field, so legitimate steering no longer false-positive-trips PROMPT_ATTACK.
# (User input, memory, and web-search results still ride in `messages` and are
# still screened; the deterministic injection detector also still scans system
# for literal jailbreak phrases, which this benign steering does not contain.)
_ORCHESTRATOR_SYSTEM = (
    "You are a helpful assistant. When the user's request needs current, "
    "real-world, or post-training information, use the web_search tool with a "
    "focused query and ground your answer on what it returns, citing sources. "
    "For anything you already know, answer directly and concisely."
)


def _seed_prompt(prompt: str) -> str:
    """Kept as a single seam for any future prompt shaping. Returns the user
    prompt unchanged; orchestrator steering now rides the system field again."""
    return prompt


def _responses_model(jwt: str, base_url: str, model: str,
                     max_tokens: int = DEFAULT_MAX_TOKENS):
    """Build a Strands OpenAIResponsesModel pointed at the gateway /inference/v1
    door. client_args go verbatim to openai.AsyncOpenAI; the JWT rides as
    api_key (Bearer). max_output_tokens is the Responses-API output cap."""
    from strands.models.openai_responses import OpenAIResponsesModel

    return OpenAIResponsesModel(
        client_args={
            "api_key": jwt,
            "base_url": _openai_base_url(base_url),
            "timeout": CLIENT_TIMEOUT_S,
            "max_retries": 0,
        },
        model_id=model,
        params={"max_output_tokens": max_tokens},
    )


def _make_websearch_tool(jwt: str, base_url: str, timings: dict[str, Any]):
    """Return a Strands @tool that IS a second real Strands agent.

    The sub-agent:
      * reasons with ITS OWN model (websearch_model from the payload/config,
        default the main model) over the OpenAI Responses door, through the
        SAME governance gateway (user JWT), and
      * uses AgentCore web search (websearch_client, a SigV4 MCP call to the
        websearch gateway) as its own tool.

    This is the "agents as tools" pattern: the sub-agent is wrapped as a single
    @tool the orchestrator can call. Its model is configurable so it can differ
    from the orchestrator's (config websearch_agent.model, e.g. Luna)."""
    from strands import Agent, tool

    sub_model_id = _WEBSEARCH_MODEL.get() or ""
    sub_max = _WEBSEARCH_MAX_TOKENS.get() or 1024

    @tool
    def agentcore_web_search(query: str) -> str:
        """Search the live web via AgentCore web search and return grounded
        result snippets with source URLs.

        Args:
            query: A focused search query (<= 200 chars).
        """
        try:
            import websearch_client  # copied into the image alongside app.py
        except Exception as e:  # pragma: no cover - image capability
            return f"NO_RESULTS: web search unavailable ({type(e).__name__})"
        out = websearch_client.search(query, max_results=6)
        if not out.get("ok") or not out.get("results"):
            return "NO_RESULTS: the web search returned nothing usable."
        return websearch_client.results_as_context(out["results"])

    @tool
    def web_search(query: str) -> str:
        """Research a question using live web search and return a grounded,
        cited answer.

        Use this for current, real-world, or post-training information (news,
        prices, latest releases, "today", specifics you are unsure of). Not for
        math or general knowledge you already have.

        Args:
            query: What to research (a question or focused query).
        """
        # The sub-agent reasons over search results with its own model, also
        # through the governed gateway. Falls back to the orchestrator's model
        # when no sub-agent model was supplied. No system_prompt (guardrail-safe,
        # like the orchestrator); the agentcore_web_search tool description drives
        # it. The query is seeded with a light research framing folded into the
        # user turn.
        model_id = sub_model_id or model
        sub_agent = Agent(
            model=_responses_model(jwt, base_url, model_id, sub_max),
            tools=[agentcore_web_search],
            callback_handler=None,
        )
        return str(sub_agent(query))

    return web_search


def _run_strands_multi_responses(jwt: str, base_url: str, model: str, prompt: str,
                                 max_tokens: int, timings: dict[str, Any]) -> str:
    """Orchestrator (main Strands agent, GPT non-OSS via the Responses door) with
    the web-search sub-agent wired as a tool. Web search is ALWAYS available."""
    from strands import Agent

    _cap_strands_retries()
    orchestrator = Agent(
        model=_responses_model(jwt, base_url, model, max_tokens),
        tools=[_make_websearch_tool(jwt, base_url, timings)],
        system_prompt=_ORCHESTRATOR_SYSTEM,
        callback_handler=None,
    )
    return _call_agent(orchestrator, _seed_prompt(prompt), timings)


# Method -> multi-agent runner. The runtime is ALWAYS multi-agent (orchestrator
# + web-search sub-agent); the method is DERIVED from the model id by
# _method_for_model. There is deliberately no single-agent path, no langgraph,
# no streaming, and no caller-supplied mode/method.
MULTI_RUNNERS = {
    "messages": _run_strands_multi_messages,    # Anthropic Claude
    "responses": _run_strands_multi_responses,  # OpenAI GPT non-OSS
    "openai": _run_strands_multi_openai,        # gpt-oss + open-weight
}


def _new_timings() -> dict[str, Any]:
    return {
        "received_at": time.time(),
        "gateway_call_start": None,
        # Time to first token, a latency measurement, not a credential. B105
        # only fires when a name on its word list sits next to a string
        # literal, and this one is None, so no suppression is needed here.
        "first_token_at": None,
        "done_at": None,
    }


@app.entrypoint
def invoke(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
    # The second parameter MUST be named "context" for the SDK to pass the
    # RequestContext (see module docstring). Header-first, payload fallback.
    #
    # The runtime is UNCONDITIONALLY multi-agent with web search always on. The
    # caller supplies a MODEL; the runtime DERIVES the door/method from it
    # (_method_for_model) and dispatches the matching orchestrator+sub-agent
    # runner. There is no mode/method/stream in the contract anymore.
    timings = _new_timings()
    prompt = str(payload.get("prompt", ""))
    base_url = str(payload.get("base_url", ""))
    model = str(payload.get("model", "") or DEFAULT_MODEL)
    # Web-search sub-agent config. The CALLER (server) resolves these from
    # config.yaml websearch_agent.* and passes them in the payload; the container
    # consumes them as read-only per-invocation input (stateless — no config file
    # baked in, no self-mutation). Empty model => sub-agent reuses the default.
    _WEBSEARCH_MODEL.set(str(payload.get("websearch_model", "") or ""))
    try:
        _WEBSEARCH_MAX_TOKENS.set(int(payload.get("websearch_max_tokens", 1024)))
    except (TypeError, ValueError):
        _WEBSEARCH_MAX_TOKENS.set(1024)
    _ws_sys = str(payload.get("websearch_system", "") or "")
    if _ws_sys:
        _WEBSEARCH_SYSTEM.set(_ws_sys)
    raw_max = payload.get("max_tokens", DEFAULT_MAX_TOKENS)
    try:
        max_tokens = max(1, min(int(raw_max), MAX_TOKENS_CEILING))
    except (TypeError, ValueError):
        max_tokens = DEFAULT_MAX_TOKENS
    jwt = _jwt_from_context(context) or str(payload.get("jwt", ""))

    method = _method_for_model(model)

    if not prompt or not jwt or not base_url or not model:
        timings["done_at"] = time.time()
        return {"method": method, "error_status": None,
                "error_type": "bad_request",
                "error_message": ("payload requires prompt/base_url/model and a "
                                  "JWT (Authorization header or payload)"),
                "retry_after": None, "timings": timings}

    # prompt/model/base_url are trusted below (base_url is where the JWT is sent).
    # Validate once here.
    def _bad_request(message: str) -> dict[str, Any]:
        timings["done_at"] = time.time()
        return {"method": method, "error_status": 400,
                "error_type": "bad_request", "error_message": message,
                "retry_after": None, "timings": timings}

    if len(prompt) > MAX_PROMPT_CHARS:
        return _bad_request(f"prompt exceeds {MAX_PROMPT_CHARS} characters "
                            f"({len(prompt)} given)")
    if not _MODEL_RE.match(model):
        return _bad_request("model is not a well-formed model id")
    try:
        _check_base_url(base_url)
    except ValueError as rejected:
        return _bad_request(str(rejected))

    runner = MULTI_RUNNERS[method]  # method is always one of the three keys
    try:
        text = runner(jwt, base_url, model, prompt, max_tokens, timings)
        timings["done_at"] = time.time()
        return {"method": method, "model": model,
                "text": _redact(text, jwt), "timings": timings}
    except Exception as error:  # surface governance refusals (429/403) as data
        details = _error_details(error)
        details["method"] = method
        details["model"] = model
        details["error_message"] = _redact(str(details.get("error_message")), jwt)
        timings["done_at"] = time.time()
        details["timings"] = timings
        return details


if __name__ == "__main__":
    app.run()
