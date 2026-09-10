#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Claude Code in BEDROCK MODE through the governed gateway's passthrough target.
#
# This is the second of Claude Code's two ways onto the gateway. The first
# (see claude_code.sh) uses the Anthropic API mode against the inference
# connector. This one uses Bedrock mode against the HTTP passthrough target:
# Claude Code sends POST /model/{modelId}/invoke-with-response-stream (it
# streams by default) with the Anthropic Messages body, exactly what
# bedrock-runtime expects, and the gateway forwards it after the interceptor's
# budget check.
#
# The four lines that matter:
#     export CLAUDE_CODE_USE_BEDROCK=1
#     export ANTHROPIC_BEDROCK_BASE_URL="${GATEWAY_URL}/bedrock-runtime"
#     export CLAUDE_CODE_SKIP_BEDROCK_AUTH=1
#     export ANTHROPIC_AUTH_TOKEN="${GATEWAY_JWT}"
#
# What each does:
#
# 1. CLAUDE_CODE_USE_BEDROCK=1 switches Claude Code to the Bedrock wire
#    format: model id in the URL path (/model/{id}/invoke), Anthropic body
#    with anthropic_version injected, "model" stripped from the body.
#
# 2. ANTHROPIC_BEDROCK_BASE_URL points the SDK at the gateway instead of
#    bedrock-runtime directly. Path prefixes are preserved, so the target
#    name (/bedrock-runtime) rides along and the gateway routes by it.
#
# 3. CLAUDE_CODE_SKIP_BEDROCK_AUTH=1 disables SigV4 signing. Without it the
#    SDK signs with local AWS credentials and OVERWRITES the Authorization
#    header ("signed headers take precedence"), so the JWT never arrives.
#
# 4. ANTHROPIC_AUTH_TOKEN rides as "Authorization: Bearer <jwt>", which is
#    what the gateway's CUSTOM_JWT authorizer validates. The interceptor
#    reads the JWT sub and applies that user's budget.
#
# Model ids must be provider-form BEDROCK ids, matched VERBATIM against the
# user's allowed_models (no prefix stripping, no aliasing). Three constraints
# follow:
#
# a. Claude Code defaults to GLOBAL cross-region profiles
#    (global.anthropic.claude-*), and a managed build can ignore ANTHROPIC_MODEL
#    entirely and request its built-in default (a global.anthropic.claude-*
#    profile). The user's allowed_models must include whatever id actually
#    leaves the client, and the gateway role needs InvokeModel on the matching
#    inference-profile ARNs (both are in the CDK sample). On a 403, read the
#    exact requested id from the interceptor log rather than guessing which
#    form the client sent.
# b. Claude Code streams by default (/invoke-with-response-stream), and this
#    variant preserves it: there is NO RESPONSE interceptor, so the stream goes
#    straight to the client. Usage is attributed ASYNCHRONOUSLY off Bedrock model
#    invocation logs (the interceptor injects requestMetadata on the passthrough
#    path), not inline. The debit trails the call by a few seconds.
# c. On a 403/429 from the gateway, Claude Code retries silently several
#    times before surfacing the error, which looks like a hang. Check the
#    interceptor log group for the actual decision.
#
# Governance behavior on this path:
#   allow      -> forwarded to bedrock-runtime, streamed, usage attributed
#                 async from the invocation log
#   downgrade  -> interceptor calls Bedrock directly with the fallback model
#                 and short-circuits the response (the model id in the URL
#                 path cannot be rewritten, so the request never reaches the
#                 target). Buffered, not streamed.
#   429        -> budget_exceeded or rate_limit_exceeded, before any Bedrock
#                 call; the body carries retry_after
#   403        -> access_denied or model_not_allowed, before any Bedrock call;
#                 no retry_after, since retrying cannot help
#
# Budget sizing note: like Anthropic-API mode, Claude Code sends large
# request bodies (system prompt plus tool schemas). Give Claude Code users
# a daily budget of about 2,000,000 tokens (POLICY item budget_tokens).
set -euo pipefail

: "${GATEWAY_URL:?set GATEWAY_URL to the gateway base URL (CDK output GatewayUrl)}"
: "${GATEWAY_JWT:?set GATEWAY_JWT to a Cognito access token (see README, admin-initiate-auth)}"

export CLAUDE_CODE_USE_BEDROCK=1
export ANTHROPIC_BEDROCK_BASE_URL="${GATEWAY_URL}/bedrock-runtime"
export CLAUDE_CODE_SKIP_BEDROCK_AUTH=1
export ANTHROPIC_AUTH_TOKEN="${GATEWAY_JWT}"

# Bedrock model ids for this mode. Claude Code defaults to global.* profiles;
# pin them explicitly so the ids match the policy allowlist.
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-global.anthropic.claude-sonnet-5}"
export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-global.anthropic.claude-haiku-4-5-20251001-v1:0}"

exec claude "$@"
