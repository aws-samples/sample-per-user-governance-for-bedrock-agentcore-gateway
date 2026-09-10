# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AgentCore Runtime entrypoint that runs native Claude Code headless against
the AgentCore Gateway per-user governance stack.

Payload contract:
    {"prompt": str,          # the end user's prompt, executed as-is
     "model": str,           # model id in the form the door expects
     "base_url": str,        # <gateway_url>/inference
     "jwt": str (optional)}  # fallback only; see JWT sourcing note below

JWT sourcing (header-first, payload fallback)
---------------------------------------------
The bedrock-agentcore SDK defines this contract in its installed package
(bedrock_agentcore/runtime/app.py and context.py): if the entrypoint's second
parameter is literally named ``context``, the SDK passes a ``RequestContext``
whose ``request_headers`` dict carries the inbound HTTP ``Authorization``
header under the canonical key ``"Authorization"``. That path applies when the
runtime is created with a customJWTAuthorizer and invoked over HTTP with the
user's bearer token; SigV4 (boto3) invocations have no bearer header, so the
payload ``jwt`` key is the fallback.

Environment facts that MUST hold for Claude Code through the gateway:
  * ANTHROPIC_BASE_URL = <gateway_url>/inference
  * ANTHROPIC_AUTH_TOKEN = <user jwt>   (Bearer auth; never an api key)
  * CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1  (gateway rejects beta headers)
  * every ANTHROPIC_*_MODEL variable set to the model id the door expects
  * MAX_THINKING_TOKENS=0: extended thinking off. The API requires
    max_tokens to exceed thinking.budget_tokens, so a large budget with a
    small output cap 400s every request on thinking-capable models.
  * a generous per-user token budget in the POLICY item: Claude Code
    request bodies are large, so tight budgets refuse it at admission.

The JWT is placed in the child process environment only (never argv, never
logged); stdout/stderr are redacted before being returned.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
# The whole point of this runtime is to drive the Claude Code CLI, so subprocess
# is load-bearing, not incidental. Every call site is argv-exec with a fixed
# program (CLAUDE_BIN) and no shell; see _build_command.
import subprocess  # nosec B404
import time
from typing import Any
from urllib.parse import urlsplit

from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/usr/local/bin/claude")
SUBPROCESS_TIMEOUT_S = 300

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

# Model ids differ per door: bare provider ids on the mantle door, dated
# snapshot ids and regional inference profile ids on the passthrough door. The
# character set therefore has to stay broad. What this rejects is a value that
# an argv parser could read as an option instead of a value, which is why the
# first character must be alphanumeric.
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")

# Long enough for any prompt the demo sends, short enough that a caller cannot
# push the argv vector at the kernel limit, where exec fails with E2BIG instead
# of returning something a client can read.
MAX_PROMPT_CHARS = 100_000


def _check_base_url(url: str) -> str:
    """Return url unchanged, or raise ValueError if it is not an AWS https URL.

    base_url decides where the CLI sends the caller's JWT, because it becomes
    ANTHROPIC_BASE_URL or ANTHROPIC_BEDROCK_BASE_URL in the child environment.
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


def _build_env(jwt: str, model: str, base_url: str, method: str, home: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home,
        # A fresh config dir every run: a populated settings.json can carry an
        # availableModels allowlist that silently overrides ANTHROPIC_MODEL,
        # so model pinning requires starting clean.
        "CLAUDE_CONFIG_DIR": home,
        "ANTHROPIC_AUTH_TOKEN": jwt,
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_SMALL_FAST_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "2048",
        # Fail fast on governance refusals. Without a cap the CLI retries a
        # 429 with exponential backoff for minutes against an always-429
        # endpoint, which the demo user sees as a hung reply. One retry
        # absorbs transient blips; a real budget refusal surfaces as a clean
        # 429 result in <1s.
        "CLAUDE_CODE_MAX_RETRIES": "1",
        "API_TIMEOUT_MS": "60000",
        # Extended thinking off. The Anthropic API requires max_tokens to
        # EXCEED thinking.budget_tokens, so any nonzero budget here must stay
        # below the output cap; a large value 400s every request on models
        # that enable thinking ("max_tokens must be greater than
        # thinking.budget_tokens"). Short demo answers do not need thinking.
        "MAX_THINKING_TOKENS": "0",
    }
    if method == "invoke":
        env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        env["ANTHROPIC_BEDROCK_BASE_URL"] = base_url
        env["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] = "1"
    else:
        env["ANTHROPIC_BASE_URL"] = base_url
    return env


def _build_command(prompt: str, model: str, *, stream: bool) -> list[str]:
    command = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "stream-json" if stream else "json",
        "--dangerously-skip-permissions",
    ]
    if stream:
        # stream-json requires --verbose in print mode, and
        # --include-partial-messages adds the per-chunk stream_event lines
        # (raw Anthropic SSE events) between the turn-level messages.
        command += ["--verbose", "--include-partial-messages"]
    return command


def _gateway_error_fields(text: str) -> tuple[str | None, int | None]:
    """Recover the gateway's own refusal code and retry hint from the CLI's
    error text.

    The interceptor answers a refusal with a body shaped
    {"type":"error","error":{"type":<code>,"message":...,"retry_after":<n>}},
    and the CLI surfaces that body inside its result string. Reading the code
    back matters because the status alone is ambiguous: budget_exceeded,
    rate_limited, and downgrade_unavailable are all 429s with different
    causes, and reporting every one of them as budget_exceeded points the
    reader at the wrong control. Scans each '{' with raw_decode rather than a
    regex, so a brace inside the message cannot truncate the match.
    """
    decoder = json.JSONDecoder()
    index = text.find("{")
    while index != -1:
        try:
            doc, _ = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        error = doc.get("error") if isinstance(doc, dict) else None
        if isinstance(error, dict):
            code = error.get("type")
            retry = error.get("retry_after")
            if isinstance(code, str) and code:
                return code, retry if isinstance(retry, int) else None
        index = text.find("{", index + 1)
    return None, None


def _result_from_doc(doc: dict[str, Any], jwt: str,
                     timings: dict[str, Any]) -> dict[str, Any]:
    """Map the CLI's final result document to the shared runtime contract:
    {"text": ...} on success, {error_status, error_type, error_message,
    retry_after} on refusal."""
    if not doc.get("is_error"):
        return {"text": _redact(str(doc.get("result", "")), jwt),
                "timings": timings}
    status = doc.get("api_error_status")
    result_text = str(doc.get("result", ""))
    # The gateway's own code wins when the CLI preserved the refusal body; the
    # status-derived name is only a fallback for errors that never reached the
    # interceptor.
    gateway_code, gateway_retry = _gateway_error_fields(result_text)
    error_type = gateway_code or (
        "budget_exceeded" if status == 429
        else "access_denied" if status == 403
        else "gateway_error" if status
        else "claude_code_error"
    )
    retry_after = doc.get("retry_after")
    if retry_after is None:
        retry_after = gateway_retry
    return {
        "error_status": status,
        "error_type": error_type,
        "error_message": _redact(result_text[:300], jwt),
        "retry_after": retry_after,
        "timings": timings,
    }


async def _streaming_invoke(prompt: str, jwt: str, base_url: str, model: str,
                            method: str, home: str, timings: dict[str, Any]):
    """Relay Claude Code's stream-json output as delta events.

    With --include-partial-messages the CLI emits one JSON object per stdout
    line; text arrives as stream_event lines wrapping the raw Anthropic SSE
    events (content_block_delta carrying text_delta), and the run ends with a
    type:"result" document identical to json mode. Yields {"delta": ...} per
    text fragment then one final event, matching the frameworks container's
    contract, so the app's SSE reader needs no changes.
    """
    env = _build_env(jwt, model, base_url, method, home)
    command = _build_command(prompt, model, stream=True)
    # argv-exec, no shell: the user's prompt is a plain argument and can
    # never be interpreted as a command. CLAUDE_BIN comes from the image
    # env, not the request, and the entrypoint has already rejected a model id
    # that an argv parser could read as an option. The rule id has to be the
    # full dotted one; the short form does not suppress anything.
    proc = await asyncio.create_subprocess_exec(  # nosemgrep: python.lang.security.audit.dangerous-asyncio-create-exec-audit
        *command, env=env, cwd=home,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    parts: list[str] = []
    result_doc: dict[str, Any] | None = None
    timings["gateway_call_start"] = time.time()
    try:
        if proc.stdout is None:
            # Unreachable with stdout=PIPE above; an explicit raise rather than
            # an assert, which python -O strips.
            raise RuntimeError("subprocess was created without a stdout pipe")
        deadline = time.monotonic() + SUBPROCESS_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            if not line:
                break
            try:
                event = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "stream_event":
                inner = event.get("event") or {}
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta") or {}
                    text = delta.get("text", "")
                    if text:
                        if timings["first_token_at"] is None:
                            timings["first_token_at"] = time.time()
                        parts.append(text)
                        yield {"delta": _redact(text, jwt)}
            elif kind == "result":
                result_doc = event
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        timings["done_at"] = time.time()
        yield {"error_status": None, "error_type": "timeout",
               "error_message": "claude code run exceeded the time limit",
               "retry_after": None, "final": True, "timings": timings}
        return
    timings["done_at"] = time.time()
    if result_doc is not None:
        final = _result_from_doc(result_doc, jwt, timings)
    else:
        stderr_tail = b""
        if proc.stderr is not None:
            try:
                stderr_tail = await asyncio.wait_for(proc.stderr.read(), timeout=5)
            except asyncio.TimeoutError:
                pass
        final = {
            "error_status": None,
            "error_type": "claude_code_error",
            "error_message": _redact(
                stderr_tail.decode("utf-8", "replace")[-300:], jwt),
            "retry_after": None,
            "timings": timings,
        }
    # The streamed text is authoritative for the reply body; the result doc's
    # text matches it, but prefer the accumulation in case the doc truncates.
    if "text" in final and parts:
        final["text"] = _redact("".join(parts), jwt)
    final["final"] = True
    yield final


def _sync_invoke(prompt: str, jwt: str, base_url: str, model: str,
                 method: str, home: str, timings: dict[str, Any]) -> dict[str, Any]:
    env = _build_env(jwt, model, base_url, method, home)
    command = _build_command(prompt, model, stream=False)
    try:
        # argv-exec, no shell: the user's prompt is a plain argument and
        # can never be interpreted as a command. B603 asks whether untrusted
        # input reaches the command; it reaches it only as argv[2], and the
        # entrypoint has already rejected a model id that an argv parser could
        # read as an option. The semgrep rule id has to be the full dotted one;
        # the short form does not suppress anything.
        proc = subprocess.run(  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            command,
            env=env,
            cwd=home,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
        return_code: int | None = proc.returncode
        stdout, stderr = proc.stdout, proc.stderr
        timed_out = False
    except subprocess.TimeoutExpired as expired:
        return_code = None
        timed_out = True

        def _as_text(stream: Any) -> str:
            if isinstance(stream, bytes):
                return stream.decode("utf-8", "replace")
            return stream or ""

        stdout = _as_text(expired.stdout)
        stderr = _as_text(expired.stderr)
    # claude -p --output-format json prints one JSON document on stdout:
    # {"type":"result","subtype":"success","result":"...","is_error":bool,
    #  "api_error_status":429,...}
    try:
        doc = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except (json.JSONDecodeError, IndexError):
        doc = {}
    timings["done_at"] = time.time()
    if not timed_out and isinstance(doc, dict) and doc.get("type") == "result":
        if doc.get("is_error") or return_code != 0:
            doc["is_error"] = True
        return _result_from_doc(doc, jwt, timings)
    return {
        "error_status": None,
        "error_type": "timeout" if timed_out else "claude_code_error",
        "error_message": _redact((stderr or stdout)[-300:], jwt),
        "retry_after": None,
        "return_code": return_code,
        "timings": timings,
    }


@app.entrypoint
def invoke(payload: dict[str, Any], context: Any = None):
    # Second parameter MUST be named "context" for the SDK to pass the
    # RequestContext (see module docstring). Header-first, payload fallback.
    timings: dict[str, Any] = {
        "received_at": time.time(),
        "gateway_call_start": None,
        # Time to first token, a latency measurement, not a credential. B105
        # only fires when a name on its word list sits next to a string
        # literal, and this one is None, so no suppression is needed here.
        "first_token_at": None,
        "done_at": None,
    }
    prompt = str(payload.get("prompt", ""))
    base_url = str(payload.get("base_url", ""))
    model = str(payload.get("model", ""))
    # "anthropic" (default): Claude Code API mode against the gateway's
    # /inference door. "invoke": Claude Code Bedrock mode against the
    # /bedrock-runtime door (its wire format is InvokeModelWithResponseStream,
    # which streams through the gateway).
    method = str(payload.get("method", "") or "anthropic")
    jwt = _jwt_from_context(context) or str(payload.get("jwt", ""))
    if not prompt or not jwt or not base_url or not model:
        timings["done_at"] = time.time()
        return {"error": ("payload requires prompt/base_url/model and a JWT "
                          "(Authorization header or payload)"),
                "timings": timings}
    if method not in ("anthropic", "invoke"):
        timings["done_at"] = time.time()
        return {"error_status": 400, "error_type": "unsupported_combo",
                "error_message": f"claude code does not support {method}",
                "timings": timings}
    # Everything below this point treats prompt, model and base_url as trusted:
    # model is interpolated into the CLI argv and base_url becomes the endpoint
    # the child process sends the JWT to. Validate them here, once, so the two
    # exec call sites have nothing caller-shaped left in them.
    if len(prompt) > MAX_PROMPT_CHARS:
        timings["done_at"] = time.time()
        return {"error_status": 400, "error_type": "bad_request",
                "error_message": (f"prompt exceeds {MAX_PROMPT_CHARS} characters "
                                  f"({len(prompt)} given)"),
                "timings": timings}
    # The prompt occupies the CLI's positional argument slot, and the option
    # parser reads any argv element beginning with "-" as an option rather than
    # as that positional. A prompt starting with "-" would therefore be parsed
    # as a flag, and the argv already carries --dangerously-skip-permissions,
    # so an option such as --mcp-config would reach a shell. Passing the prompt
    # as one argv element prevents it being re-split; it does not stop it being
    # read as an option, which is what this check covers.
    if prompt.lstrip()[:1] == "-":
        timings["done_at"] = time.time()
        return {"error_status": 400, "error_type": "bad_request",
                "error_message": "prompt must not begin with '-'",
                "timings": timings}
    if not _MODEL_RE.match(model):
        timings["done_at"] = time.time()
        return {"error_status": 400, "error_type": "bad_request",
                "error_message": "model is not a well-formed model id",
                "timings": timings}
    try:
        _check_base_url(base_url)
    except ValueError as rejected:
        timings["done_at"] = time.time()
        return {"error_status": 400, "error_type": "bad_request",
                "error_message": str(rejected),
                "timings": timings}

    # Fixed path inside this single-tenant container (one invocation at a
    # time, /tmp is container-private); not a shared-host temp dir.
    home = "/tmp/claude-home"  # nosec B108
    os.makedirs(home, exist_ok=True)
    # stream:true returns an async generator; the SDK relays each yielded
    # dict as one SSE data event (same contract as the frameworks
    # container). The CLI's stream-json mode supplies the deltas.
    if bool(payload.get("stream")):
        return _streaming_invoke(prompt, jwt, base_url, model, method, home, timings)
    return _sync_invoke(prompt, jwt, base_url, model, method, home, timings)


if __name__ == "__main__":
    app.run()
