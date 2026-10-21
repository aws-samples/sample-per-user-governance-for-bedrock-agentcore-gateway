#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgentCore web-search client.

Calls the AWS-managed **Bedrock AgentCore built-in web-search tool** through a
dedicated AgentCore Gateway (MCP connector target). Keyless — the search is a
managed AWS tool (`web-search.v1`), no Tavily/SerpApi key.

In this project we REUSE an existing web-search gateway already provisioned in
the account (auth ``AWS_IAM``), so the call is signed with **SigV4**
(service ``bedrock-agentcore``, region us-east-1) using the app server's own AWS
credentials. The web-search connector is only offered in us-east-1 / eu-west-1 /
ap-northeast-1, and a gateway is regional, hence the dedicated us-east-1 gateway
separate from the CUSTOM_JWT governance gateway.

Governance note: web search is triggered only by a logged-in user, and the
user's question is screened by the same Bedrock Guardrail (ApplyGuardrail) BEFORE
we search, so a blocked prompt never reaches the web. The search itself returns
public snippets (no model inference, no per-user token cost).

Config (environment variables):
  WEBSEARCH_GATEWAY_URL   full ``.../mcp`` endpoint of the web-search gateway
  WEBSEARCH_REGION        region to sign for (default us-east-1)
  WEBSEARCH_TOOL_NAME     namespaced tool (default web-search-tool___WebSearch)

Verified live 2026-09-16: ``web-search-tool___WebSearch`` with
``{"query": ..., "maxResults": 1..25}`` returns
``result.content[0].text`` as a JSON string ``{"id":..,"results":[{text,url,
title,publishedDate}, ...]}``.
"""
from __future__ import annotations

import json
import os
from typing import Any

try:  # boto3 + requests are used only for the signed MCP call.
    import boto3  # type: ignore
    from botocore.auth import SigV4Auth  # type: ignore
    from botocore.awsrequest import AWSRequest  # type: ignore

    _HAVE_BOTO3 = True
except Exception:  # pragma: no cover
    _HAVE_BOTO3 = False

try:
    import requests  # type: ignore

    _HAVE_REQUESTS = True
except Exception:  # pragma: no cover
    _HAVE_REQUESTS = False

# urllib fallback so the module works even without `requests`.
import urllib.error
import urllib.request

MAX_QUERY_CHARS = 200
DEFAULT_MAX_RESULTS = 6


def _cfg(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def available() -> bool:
    """True when web search is configured and its deps are importable."""
    return bool(_HAVE_BOTO3 and _cfg("WEBSEARCH_GATEWAY_URL"))


def _sigv4_headers(url: str, region: str, body: str) -> dict[str, str]:
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    req = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(creds, "bedrock-agentcore", region).add_auth(req)
    return dict(req.headers)


def _post(url: str, headers: dict[str, str], body: str, timeout: int = 25) -> tuple[int, str]:
    if _HAVE_REQUESTS:
        r = requests.post(url, data=body, headers=headers, timeout=timeout)
        return r.status_code, r.text
    req = urllib.request.Request(url, data=body.encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - scheme and host are fixed by operator config (GATEWAY_URL / WEBSEARCH_GATEWAY_URL) or a hardcoded https AWS endpoint; no request input selects them
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _parse_results(data: Any) -> list[dict[str, Any]]:
    """MCP success: data.result.content[0].text is a JSON string with a
    ``results`` list. Tolerant of shape drift."""
    try:
        result = data.get("result") or {}
        content = result.get("content") or []
        if content and isinstance(content[0], dict):
            inner = content[0].get("text")
            if isinstance(inner, str):
                obj = json.loads(inner)
                res = obj.get("results")
                if isinstance(res, list):
                    return [
                        {
                            "text": r.get("text", ""),
                            "url": r.get("url", ""),
                            "title": r.get("title", ""),
                            "publishedDate": r.get("publishedDate", ""),
                        }
                        for r in res
                        if isinstance(r, dict)
                    ]
    except Exception:
        pass
    return []


def search(query: str, max_results: int = DEFAULT_MAX_RESULTS) -> dict[str, Any]:
    """Run a web search. Returns::

        {ok, results: [{text,url,title,publishedDate}], note, error?}

    Never raises for a search/transport failure — returns ``ok=False`` with a
    note so the UI can render a clean message."""
    query = (query or "").strip()[:MAX_QUERY_CHARS]
    if not query:
        return {"ok": False, "results": [], "note": "empty query"}
    if not available():
        return {
            "ok": False,
            "results": [],
            "note": "web search not configured (WEBSEARCH_GATEWAY_URL unset or boto3 missing)",
        }
    url = _cfg("WEBSEARCH_GATEWAY_URL")
    region = _cfg("WEBSEARCH_REGION", "us-east-1")
    tool = _cfg("WEBSEARCH_TOOL_NAME", "web-search-tool___WebSearch")
    n = max(1, min(int(max_results or DEFAULT_MAX_RESULTS), 25))
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {"query": query, "maxResults": n}},
        }
    )
    try:
        headers = _sigv4_headers(url, region, body)
        status, text = _post(url, headers, body)
    except Exception as e:  # signing / network
        return {"ok": False, "results": [], "note": f"search transport error: {type(e).__name__}"}
    if status != 200:
        return {"ok": False, "results": [], "note": f"search gateway HTTP {status}", "error": text[:300]}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "results": [], "note": "search returned non-JSON"}
    if isinstance(data, dict) and data.get("error"):
        return {"ok": False, "results": [], "note": str(data["error"].get("message", "search error"))}
    results = _parse_results(data)
    note = f"{len(results)} web result(s) via AgentCore web search" if results else "no web results"
    return {"ok": bool(results), "results": results, "note": note}


def results_as_context(results: list[dict[str, Any]], limit: int = 6) -> str:
    """Format search results as a compact, model-facing context block. Snippets
    are trimmed; long digit runs are left intact here (the caller screens the
    USER question via the guardrail; these are public search snippets folded in
    as grounding, labelled clearly)."""
    lines = []
    for i, r in enumerate(results[:limit], 1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        snippet = " ".join((r.get("text") or "").split())[:400]
        lines.append(f"[{i}] {title} — {url}\n{snippet}")
    if not lines:
        return ""
    return (
        "Web search results (use these to answer; cite the [n] sources):\n"
        + "\n\n".join(lines)
    )
