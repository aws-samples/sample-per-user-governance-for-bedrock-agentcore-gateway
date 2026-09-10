#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Native Claude Code through the governed gateway.
#
# The two lines that matter:
#     export ANTHROPIC_BASE_URL="${GATEWAY_URL}/inference"
#     export ANTHROPIC_AUTH_TOKEN="${GATEWAY_JWT}"
#
# Required settings and behavior to expect:
#
# 1. CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 is REQUIRED. Without it Claude
#    Code sends first-party anthropic-beta flags that Bedrock rejects with
#    400 "invalid beta flag", on every request.
#
# 2. Budget sizing: Claude Code sends a very large request body (system
#    prompt plus tool schemas), around 116 KB per request. The interceptor's
#    conservative estimator reads that as roughly one token per three body
#    bytes, about 39,000 input tokens for a single request. Nothing is held
#    before the call, so a small budget is not refused up front; it is emptied
#    within a handful of turns. Once the budget is gone every request comes
#    back 429 and Claude Code retries silently until it times out. Give Claude
#    Code users a daily budget of about 2,000,000 tokens (POLICY item
#    budget_tokens).
#
# 3. Request volume: Claude Code issues background Haiku calls (topic
#    detection and similar) alongside each main turn, under the same user
#    identity. Expect roughly 2x the request count you'd predict from turns
#    alone; the extra calls debit the same budget and appear as additional
#    decision-log lines. The concurrent calls collide on the same per-user
#    usage item, so the interceptor retries transient DynamoDB
#    TransactionConflict.
#
# Usage:
#     export GATEWAY_URL=...   # GatewayUrl stack output
#     export GATEWAY_JWT=...   # see recipes/README.md for issuing one
#     ./claude_code.sh "Summarize what this repo does in one sentence."
set -euo pipefail

: "${GATEWAY_URL:?set GATEWAY_URL to the GatewayUrl stack output}"
: "${GATEWAY_JWT:?set GATEWAY_JWT to a user JWT}"

MODEL="${GATEWAY_MODEL:-anthropic.claude-haiku-4-5}"

export ANTHROPIC_BASE_URL="${GATEWAY_URL}/inference"
export ANTHROPIC_AUTH_TOKEN="${GATEWAY_JWT}"
# The /inference (mantle) door takes bare model ids, matched VERBATIM against
# the user's allowed_models. Pin both the main and the small fast model so
# every internal call stays on the gateway. Claude Code can substitute the id
# before it leaves the machine (a settings.json availableModels allowlist falls
# back to the account default when the winner does not match); if you see a 403
# model_not_allowed, read the exact requested id from the interceptor log rather
# than guessing which form the client sent.
export ANTHROPIC_MODEL="${MODEL}"
export ANTHROPIC_SMALL_FAST_MODEL="${MODEL}"
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
# Cap the retry ladder. This script is one-shot and non-interactive, and against
# an always-429 endpoint the default ladder backs off for minutes before giving
# up, which reads as a hang. One retry absorbs a transient blip; a real budget
# or rate-limit refusal surfaces in seconds. Drop this for interactive sessions,
# where riding out the backoff is usually what you want.
export CLAUDE_CODE_MAX_RETRIES=1

exec claude -p "${1:-Reply with exactly: GATEWAY-OK}" --model "${MODEL}"
