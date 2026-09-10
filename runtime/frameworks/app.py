# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AgentCore Runtime entrypoint hosting agent-framework client patterns
(Strands single-agent, Strands multi-agent, LangGraph) that call models THROUGH
the AgentCore Gateway per-user governance stack over four wire "methods":

  * "messages"  Anthropic Messages API via the gateway's /inference door
                (AnthropicModel / ChatAnthropic, Bearer auth).
  * "converse"  Bedrock Converse via the native /bedrock-runtime door
                (boto3 BedrockModel / ChatBedrockConverse, model id in the URL
                path, Bearer-swap event hook after SigV4 signing).
  * "invoke"    Bedrock InvokeModel via the native /bedrock-runtime door
                (boto3 invoke_model, Anthropic body format, model id in the URL
                path, same Bearer-swap event hook as converse).
  * "openai"    OpenAI-compatible Chat Completions API via the gateway's
                /inference door. The OpenAI SDK sends api_key as
                "Authorization: Bearer <key>", so api_key=<user JWT> authenticates
                against the gateway directly. base_url is <payload base_url>/v1
                and the SDK appends /chat/completions.

Payload contract:
    {"mode":   "strands" | "strands_multi" | "langgraph",
     "method": "messages" | "converse" | "invoke" | "openai",  # optional, default messages
     "prompt": str,          # the end user's prompt, executed as-is
     "model":  str,          # optional; per-method default applies when absent
     "base_url": str,        # messages/openai: <gateway_url>/inference
                             #   (openai: the SDK appends /v1/chat/completions)
                             # converse/invoke: <native_gateway_url>/bedrock-runtime
     "max_tokens": int (optional),
     "jwt": str (optional)}  # fallback only; see JWT sourcing note below

Supported (mode, method) combinations:
    strands       x {messages, converse, invoke, openai}
    strands_multi x {messages}
    langgraph     x {messages, converse, openai}
                    # converse requires langchain-aws in the image;
                    # openai requires langchain-openai in the image.
Unsupported combinations return
    {"error_status": 400, "error_type": "unsupported_combo",
     "error_message": "<mode> does not support <method>"}.

Per-method model defaults (explicit payload "model" always wins):
    messages          -> "anthropic.claude-haiku-4-5"
    converse / invoke -> "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    openai            -> "gpt-oss-120b"

Every response (success and error) carries a "timings" object of time.time()
floats: received_at (top of handler), gateway_call_start (immediately before
the model/agent call), first_token_at (first streamed chunk callback, or null
when the path does not stream), done_at (after completion). Converse responses
additionally carry model_latency_ms sourced from the Converse response
metrics.latencyMs when present.

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

import importlib.util
import json
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

DEFAULT_METHOD = "messages"

# Per-method model defaults. Messages rides the /inference door where the
# gateway expects the Anthropic-prefixed alias; converse/invoke ride the native
# /bedrock-runtime door where the model id is the inference-profile Bedrock id;
# openai rides the /inference door's OpenAI-compatible Chat Completions surface
# where the gateway expects an OpenAI-style model id.
MODEL_DEFAULTS = {
    "messages": "anthropic.claude-haiku-4-5",
    "converse": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "invoke": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "openai": "gpt-oss-120b",
}

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


def _has_module(name: str) -> bool:
    """True when an import spec exists for ``name`` (image capability probe)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _error_details(error: BaseException) -> dict[str, Any]:
    """Walk the exception chain and surface gateway refusals as structured
    data: {error_status, error_type, error_message, retry_after}.

    Handles both the anthropic APIStatusError path (messages method) and the
    botocore ClientError path (converse/invoke methods). retry_after comes from
    the response body when the gateway includes it (e.g. budget_exceeded 429s),
    with the standard Retry-After response header as a fallback.
    """
    import anthropic as anthropic_sdk

    cursor: BaseException | None = error
    while cursor is not None:
        if isinstance(cursor, anthropic_sdk.APIStatusError):
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
                "error_status": cursor.status_code,
                "error_type": err.get("type"),
                "error_message": str(err.get("message"))[:300],
                "retry_after": retry_after,
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


def _run_strands_messages(jwt: str, base_url: str, model: str, prompt: str,
                          max_tokens: int, timings: dict[str, Any]) -> str:
    from strands import Agent

    _cap_strands_retries()
    agent = Agent(
        model=_strands_model(jwt, base_url, model, max_tokens),
        system_prompt="You are a concise, helpful assistant.",
        callback_handler=None,
    )
    return _call_agent(agent, prompt, timings)


def _run_strands_multi_messages(jwt: str, base_url: str, model: str, prompt: str,
                                max_tokens: int, timings: dict[str, Any]) -> str:
    from strands import Agent, tool

    _cap_strands_retries()

    @tool
    def researcher(question: str) -> str:
        """Ask the researcher sub-agent a question and return its answer."""
        sub_agent = Agent(
            model=_strands_model(jwt, base_url, model, max_tokens),
            system_prompt="You answer questions briefly and factually.",
            callback_handler=None,
        )
        return str(sub_agent(question))

    orchestrator = Agent(
        model=_strands_model(jwt, base_url, model, max_tokens),
        tools=[researcher],
        system_prompt=(
            "Call the researcher tool exactly once with the user's request, "
            "then summarise its answer for the user."
        ),
        callback_handler=None,
    )
    return _call_agent(orchestrator, prompt, timings)


def _run_langgraph_messages(jwt: str, base_url: str, model: str, prompt: str,
                            max_tokens: int, timings: dict[str, Any]) -> str:
    from typing import TypedDict

    import anthropic as anthropic_sdk
    from langchain_anthropic import ChatAnthropic
    from langgraph.graph import END, StateGraph

    class GraphState(TypedDict):
        question: str
        draft: str
        final: str

    llm = ChatAnthropic(
        model=model,
        max_tokens=max_tokens,
        # Auth requirement: ChatAnthropic requires an api_key value, but the
        # gateway is Bearer-only. Give it a dummy key and inject Bearer-only
        # clients below. max_retries=0 to surface refusals immediately.
        api_key="unused-gateway-uses-bearer",
        base_url=base_url,
        max_retries=0,
        timeout=CLIENT_TIMEOUT_S,
    )
    # Auth requirement: ChatAnthropic always forwards api_key and the SDK then
    # sends x-api-key; the gateway rejects requests carrying both x-api-key
    # and Authorization. _client/_async_client are cached properties, so
    # inject Bearer-only anthropic clients directly into llm.__dict__.
    llm.__dict__["_client"] = anthropic_sdk.Client(
        auth_token=jwt, base_url=base_url, max_retries=0, timeout=CLIENT_TIMEOUT_S
    )
    llm.__dict__["_async_client"] = anthropic_sdk.AsyncClient(
        auth_token=jwt, base_url=base_url, max_retries=0, timeout=CLIENT_TIMEOUT_S
    )

    def _text(reply: Any) -> str:
        """Extract plain text from a LangChain AIMessage. Sonnet 5 returns a
        list of content blocks (thinking, text, ...); keep only text blocks."""
        content = reply.content
        if isinstance(content, str):
            return content
        parts: list[str] = []
        for block in content or []:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p).strip()

    def drafter(state: GraphState) -> dict:
        reply = llm.invoke("Draft a brief answer: " + state["question"])
        return {"draft": _text(reply)}

    def refiner(state: GraphState) -> dict:
        reply = llm.invoke(
            "Rewrite this draft as the final answer. If the draft already "
            "satisfies the user's request exactly, return it unchanged. "
            "Never add commentary.\nDraft:\n" + state["draft"]
        )
        return {"final": _text(reply)}

    graph = StateGraph(GraphState)
    graph.add_node("drafter", drafter)
    graph.add_node("refiner", refiner)
    graph.set_entry_point("drafter")
    graph.add_edge("drafter", "refiner")
    graph.add_edge("refiner", END)
    timings["gateway_call_start"] = time.time()
    output = graph.compile().invoke({"question": prompt})
    return str(output.get("final", ""))


# ---------------------------------------------------------------------------
# converse method: Bedrock Converse via the native /bedrock-runtime door
# ---------------------------------------------------------------------------
def _bedrock_session(jwt: str, timings: dict[str, Any]):
    """boto3 Session with the JWT Bearer-swap hook and a Converse latency
    capture hook registered on its event system. Clients created from the
    session inherit both hooks (same pattern as recipes/converse_sdk.py).

    boto3 always SigV4-signs its requests; the gateway wants the user's JWT
    instead, so request-created.bedrock-runtime rewrites Authorization after
    signing. after-call.bedrock-runtime.Converse reads metrics.latencyMs from
    the parsed Converse response into timings["model_latency_ms"].
    """
    import boto3

    session = boto3.Session(region_name=os.environ.get("AWS_REGION", "us-east-1"))

    def _swap_auth(request, **kwargs):
        request.headers["Authorization"] = f"Bearer {jwt}"

    def _capture_latency(parsed: Any = None, **kwargs):
        if isinstance(parsed, dict):
            metrics = parsed.get("metrics") or {}
            latency = metrics.get("latencyMs")
            if latency is not None:
                timings["model_latency_ms"] = latency

    session.events.register_last("request-created.bedrock-runtime", _swap_auth)
    session.events.register("after-call.bedrock-runtime.Converse", _capture_latency)
    return session


def _run_strands_converse(jwt: str, base_url: str, model: str, prompt: str,
                          max_tokens: int, timings: dict[str, Any]) -> str:
    """Strands with BedrockModel: the NATIVE Converse wire format through the
    passthrough gateway. The model id rides in the URL path, so this exercises
    the gateway's second protocol door."""
    from botocore.config import Config
    from strands import Agent
    from strands.models import BedrockModel

    _cap_strands_retries()

    # BedrockModel builds its own client from boto_session + endpoint_url
    # (there is no client= parameter; an unknown kwarg is silently ignored,
    # which would fall back to DIRECT Bedrock and bypass governance). Hooks
    # registered on the session's event system propagate to clients created
    # from it, so the JWT swap and latency capture attach at the session level.
    session = _bedrock_session(jwt, timings)
    agent = Agent(
        model=BedrockModel(
            boto_session=session,
            endpoint_url=base_url,
            boto_client_config=Config(
                read_timeout=CLIENT_TIMEOUT_S, retries={"max_attempts": 0}
            ),
            model_id=model,
            max_tokens=max_tokens,
            streaming=False,
        ),
        system_prompt="You are a concise, helpful assistant.",
        callback_handler=None,
    )
    return _call_agent(agent, prompt, timings)


def _run_langgraph_converse(jwt: str, base_url: str, model: str, prompt: str,
                            max_tokens: int, timings: dict[str, Any]) -> str:
    """LangGraph over the native Converse door via langchain-aws
    ChatBedrockConverse. Reachability of langchain-aws is checked before
    dispatch (the combo is otherwise rejected as unsupported)."""
    from typing import TypedDict

    from botocore.config import Config
    from langchain_aws import ChatBedrockConverse
    from langgraph.graph import END, StateGraph

    class GraphState(TypedDict):
        question: str
        draft: str
        final: str

    session = _bedrock_session(jwt, timings)
    client = session.client(
        "bedrock-runtime",
        endpoint_url=base_url,
        config=Config(read_timeout=CLIENT_TIMEOUT_S, retries={"max_attempts": 0}),
    )
    llm = ChatBedrockConverse(
        client=client,
        model=model,
        max_tokens=max_tokens,
    )

    def _text(reply: Any) -> str:
        content = reply.content
        if isinstance(content, str):
            return content
        parts: list[str] = []
        for block in content or []:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p).strip()

    def drafter(state: GraphState) -> dict:
        reply = llm.invoke("Draft a brief answer: " + state["question"])
        return {"draft": _text(reply)}

    def refiner(state: GraphState) -> dict:
        reply = llm.invoke(
            "Rewrite this draft as the final answer. If the draft already "
            "satisfies the user's request exactly, return it unchanged. "
            "Never add commentary.\nDraft:\n" + state["draft"]
        )
        return {"final": _text(reply)}

    graph = StateGraph(GraphState)
    graph.add_node("drafter", drafter)
    graph.add_node("refiner", refiner)
    graph.set_entry_point("drafter")
    graph.add_edge("drafter", "refiner")
    graph.add_edge("refiner", END)
    timings["gateway_call_start"] = time.time()
    output = graph.compile().invoke({"question": prompt})
    return str(output.get("final", ""))


# ---------------------------------------------------------------------------
# invoke method: Bedrock InvokeModel via the native /bedrock-runtime door
# ---------------------------------------------------------------------------
def _run_strands_invoke(jwt: str, base_url: str, model: str, prompt: str,
                        max_tokens: int, timings: dict[str, Any]) -> str:
    """boto3 invoke_model with the Anthropic body format. The model id rides in
    the URL path (modelId), and the same Bearer-swap event hook as converse
    replaces the SigV4 Authorization header with the user's JWT."""
    from botocore.config import Config

    session = _bedrock_session(jwt, timings)
    client = session.client(
        "bedrock-runtime",
        endpoint_url=base_url,
        config=Config(read_timeout=CLIENT_TIMEOUT_S, retries={"max_attempts": 0}),
    )
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "system": "You are a concise, helpful assistant.",
        "messages": [{"role": "user", "content": prompt}],
    }
    timings["gateway_call_start"] = time.time()
    response = client.invoke_model(modelId=model, body=json.dumps(body))
    payload = json.loads(response["body"].read())
    parts: list[str] = []
    for block in payload.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(p for p in parts if p).strip()


# ---------------------------------------------------------------------------
# openai method: OpenAI-compatible Chat Completions via the /inference door
# ---------------------------------------------------------------------------
def _openai_base_url(base_url: str) -> str:
    """The OpenAI SDK appends /chat/completions to base_url, so point it at the
    gateway's /inference/v1 root. Idempotent when the caller already added /v1."""
    trimmed = base_url.rstrip("/")
    return trimmed if trimmed.endswith("/v1") else trimmed + "/v1"


def _run_strands_openai(jwt: str, base_url: str, model: str, prompt: str,
                        max_tokens: int, timings: dict[str, Any]) -> str:
    """Strands with OpenAIModel: the OpenAI-compatible Chat Completions wire
    format through the gateway's /inference door.

    Auth requirement: the OpenAI SDK sends api_key as
    "Authorization: Bearer <key>", so passing the user's JWT as api_key
    authenticates against the gateway's JWT authorizer directly (no auth_token
    swap needed as with the Anthropic surface). client_args are forwarded
    verbatim to openai.AsyncOpenAI; max_tokens rides in params, which merge
    into the chat.completions request. max_retries=0 surfaces governance
    refusals immediately instead of backing off."""
    from strands import Agent
    from strands.models.openai import OpenAIModel

    _cap_strands_retries()
    agent = Agent(
        model=OpenAIModel(
            client_args={
                "api_key": jwt,
                "base_url": _openai_base_url(base_url),
                "timeout": CLIENT_TIMEOUT_S,
                "max_retries": 0,
            },
            model_id=model,
            params={"max_tokens": max_tokens},
        ),
        system_prompt="You are a concise, helpful assistant.",
        callback_handler=None,
    )
    return _call_agent(agent, prompt, timings)


def _run_langgraph_openai(jwt: str, base_url: str, model: str, prompt: str,
                          max_tokens: int, timings: dict[str, Any]) -> str:
    """LangGraph over the OpenAI-compatible Chat Completions door via
    langchain-openai ChatOpenAI. Reachability of langchain-openai is checked
    before dispatch (the combo is otherwise rejected as unsupported).

    Auth requirement: ChatOpenAI forwards api_key to the OpenAI SDK, which
    sends "Authorization: Bearer <key>"; the user's JWT as api_key therefore
    authenticates against the gateway directly. max_retries=0 surfaces
    governance refusals immediately."""
    from typing import TypedDict

    from langchain_openai import ChatOpenAI
    from langgraph.graph import END, StateGraph

    class GraphState(TypedDict):
        question: str
        draft: str
        final: str

    # langchain-openai (>=1.x) remaps the constructor `max_tokens` to
    # `max_completion_tokens` on the request body, but this gateway's Chat
    # Completions surface requires the literal `max_tokens` integer. Passing
    # it via extra_body lands it verbatim at the top level.
    llm = ChatOpenAI(
        model=model,
        api_key=jwt,
        base_url=_openai_base_url(base_url),
        max_retries=0,
        timeout=CLIENT_TIMEOUT_S,
        extra_body={"max_tokens": max_tokens},
    )

    def _text(reply: Any) -> str:
        content = reply.content
        if isinstance(content, str):
            return content
        parts: list[str] = []
        for block in content or []:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p).strip()

    def drafter(state: GraphState) -> dict:
        reply = llm.invoke("Draft a brief answer: " + state["question"])
        return {"draft": _text(reply)}

    def refiner(state: GraphState) -> dict:
        reply = llm.invoke(
            "Rewrite this draft as the final answer. If the draft already "
            "satisfies the user's request exactly, return it unchanged. "
            "Never add commentary.\nDraft:\n" + state["draft"]
        )
        return {"final": _text(reply)}

    graph = StateGraph(GraphState)
    graph.add_node("drafter", drafter)
    graph.add_node("refiner", refiner)
    graph.set_entry_point("drafter")
    graph.add_edge("drafter", "refiner")
    graph.add_edge("refiner", END)
    timings["gateway_call_start"] = time.time()
    output = graph.compile().invoke({"question": prompt})
    return str(output.get("final", ""))


# (mode, method) -> runner. The keys are the full set of supported combos.
RUNNERS = {
    ("strands", "messages"): _run_strands_messages,
    ("strands", "converse"): _run_strands_converse,
    ("strands", "invoke"): _run_strands_invoke,
    ("strands", "openai"): _run_strands_openai,
    ("strands_multi", "messages"): _run_strands_multi_messages,
    ("langgraph", "messages"): _run_langgraph_messages,
    ("langgraph", "converse"): _run_langgraph_converse,
    ("langgraph", "openai"): _run_langgraph_openai,
}


# ---------------------------------------------------------------------------
# Streaming runners. Each is an async generator yielding text fragments as the
# model produces them. The AgentCore Runtime SDK relays an async-generator
# entrypoint as SSE automatically (one `data: {json}` event per yielded dict,
# Content-Type text/event-stream); yield plain dicts only and never
# self-serialize, or events get double-wrapped. This follows the SDK's
# async-generator relay (bedrock_agentcore/runtime/app.py) and the response
# streaming guide.
# ---------------------------------------------------------------------------
def _strands_stream_agent(mode: str, method: str, jwt: str, base_url: str,
                          model: str, max_tokens: int):
    """Build the same Agent the sync runner uses for this (mode, method)."""
    from strands import Agent

    _cap_strands_retries()
    if method == "messages":
        agent_model = _strands_model(jwt, base_url, model, max_tokens)
    elif method == "openai":
        from strands.models.openai import OpenAIModel
        agent_model = OpenAIModel(
            client_args={
                "api_key": jwt,
                "base_url": _openai_base_url(base_url),
                "timeout": CLIENT_TIMEOUT_S,
                "max_retries": 0,
            },
            model_id=model,
            params={"max_tokens": max_tokens},
        )
    else:  # converse
        from botocore.config import Config
        from strands.models import BedrockModel
        # Same session-level JWT swap as the sync converse runner; streaming
        # on so ConverseStream serves the token deltas, which stream through
        # the gateway.
        agent_model = BedrockModel(
            boto_session=_bedrock_session(jwt, {}),
            endpoint_url=base_url,
            boto_client_config=Config(
                read_timeout=CLIENT_TIMEOUT_S, retries={"max_attempts": 0}
            ),
            model_id=model,
            max_tokens=max_tokens,
            streaming=True,
        )
    return Agent(
        model=agent_model,
        system_prompt="You are a concise, helpful assistant.",
        callback_handler=None,
    )


async def _stream_strands(mode: str, method: str, jwt: str, base_url: str,
                          model: str, prompt: str, max_tokens: int,
                          timings: dict[str, Any]):
    """Yield text fragments from a streaming strands run. Text deltas arrive
    on stream_async events as the string under the "data" key; other event
    kinds (tool use, lifecycle) are skipped. A max_tokens stop mid-stream
    ends the generator cleanly with stopped_reason recorded; the fragments
    already yielded ARE the partial reply, so nothing is lost."""
    from strands.types.exceptions import MaxTokensReachedException

    agent = _strands_stream_agent(mode, method, jwt, base_url, model, max_tokens)
    timings["gateway_call_start"] = time.time()
    try:
        async for event in agent.stream_async(prompt):
            data = event.get("data") if isinstance(event, dict) else None
            if isinstance(data, str) and data:
                if timings["first_token_at"] is None:
                    timings["first_token_at"] = time.time()
                yield data
        timings["stopped_reason"] = "end_turn"
    except MaxTokensReachedException:
        timings["stopped_reason"] = "max_tokens"


# Streaming-capable combos. invoke is request/response by API contract, and
# strands_multi's orchestration and langgraph's two-node graph return whole
# replies, so those run the sync runner and emit a final-only stream.
STREAM_RUNNERS = {
    ("strands", "messages"): _stream_strands,
    ("strands", "openai"): _stream_strands,
    ("strands", "converse"): _stream_strands,
}


async def _streaming_invoke(mode: str, method: str, jwt: str, base_url: str,
                            model: str, prompt: str, max_tokens: int,
                            timings: dict[str, Any]):
    """The async-generator entrypoint result for stream:true payloads.

    Yields {"delta": fragment} per text fragment, then exactly one final
    event {"mode", "method", "final": true, "text", "timings"}. When the
    combo has no streaming runner, the sync runner produces the whole reply
    and only the final event is emitted; the frontend handles both shapes.
    A mid-stream failure is surfaced as a final-shaped error event so the
    client always receives structured details.
    """
    parts: list[str] = []
    try:
        stream_runner = STREAM_RUNNERS.get((mode, method))
        if stream_runner is not None:
            async for fragment in stream_runner(
                mode, method, jwt, base_url, model, prompt, max_tokens, timings
            ):
                parts.append(fragment)
                yield {"delta": _redact(fragment, jwt)}
            text = "".join(parts)
        else:
            text = RUNNERS[(mode, method)](
                jwt, base_url, model, prompt, max_tokens, timings
            )
        timings["done_at"] = time.time()
        yield {"mode": mode, "method": method, "final": True,
               "text": _redact(text, jwt), "timings": timings}
    except Exception as error:  # surface governance refusals (429/403) as data
        details = _error_details(error)
        details["mode"] = mode
        details["method"] = method
        details["final"] = True
        details["error_message"] = _redact(str(details.get("error_message")), jwt)
        timings["done_at"] = time.time()
        details["timings"] = timings
        yield details


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
    timings = _new_timings()
    mode = str(payload.get("mode", ""))
    method = str(payload.get("method", "") or DEFAULT_METHOD)
    prompt = str(payload.get("prompt", ""))
    base_url = str(payload.get("base_url", ""))
    # Explicit payload model wins; otherwise fall back to the per-method default.
    model = str(payload.get("model", "") or MODEL_DEFAULTS.get(method, ""))
    raw_max = payload.get("max_tokens", DEFAULT_MAX_TOKENS)
    try:
        max_tokens = max(1, min(int(raw_max), MAX_TOKENS_CEILING))
    except (TypeError, ValueError):
        max_tokens = DEFAULT_MAX_TOKENS
    jwt = _jwt_from_context(context) or str(payload.get("jwt", ""))

    if not prompt or not jwt or not base_url or not model:
        timings["done_at"] = time.time()
        return {"mode": mode, "method": method, "error_status": None,
                "error_type": "bad_request",
                "error_message": ("payload requires mode/method/prompt/base_url "
                                  "and a JWT (Authorization header or payload)"),
                "retry_after": None, "timings": timings}

    runner = RUNNERS.get((mode, method))
    if runner is None or (
        mode == "langgraph" and method == "converse" and not _has_module("langchain_aws")
    ) or (
        mode == "langgraph" and method == "openai" and not _has_module("langchain_openai")
    ):
        timings["done_at"] = time.time()
        return {"mode": mode, "method": method, "error_status": 400,
                "error_type": "unsupported_combo",
                "error_message": f"{mode} does not support {method}",
                "retry_after": None, "timings": timings}

    # Everything below this point treats prompt, model and base_url as trusted:
    # base_url becomes the endpoint each client sends the JWT to, and on
    # converse/invoke the model id becomes a segment of the request path.
    # Validate them here, once, so no runner has to.
    def _bad_request(message: str) -> dict[str, Any]:
        timings["done_at"] = time.time()
        return {"mode": mode, "method": method, "error_status": 400,
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

    # stream:true returns an async generator; the SDK relays each yielded
    # dict as one SSE data event. A plain return NEVER streams (the SDK does
    # not inspect accept headers), so the generator path is the only way to
    # stream and the sync path below is the only way not to.
    if bool(payload.get("stream")):
        return _streaming_invoke(
            mode, method, jwt, base_url, model, prompt, max_tokens, timings
        )

    try:
        text = runner(jwt, base_url, model, prompt, max_tokens, timings)
        timings["done_at"] = time.time()
        return {"mode": mode, "method": method, "text": _redact(text, jwt),
                "timings": timings}
    except Exception as error:  # surface governance refusals (429/403) as data
        details = _error_details(error)
        details["mode"] = mode
        details["method"] = method
        details["error_message"] = _redact(str(details.get("error_message")), jwt)
        timings["done_at"] = time.time()
        details["timings"] = timings
        return details


if __name__ == "__main__":
    app.run()
