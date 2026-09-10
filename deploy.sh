#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

#
# Top-level deploy for the per-user governance sample.
#
# Two shapes:
#
#   Gateway only (default)   The governance engine: gateway, interceptor,
#                            attribution pipeline, ledger table, Cognito
#                            user pool. Plug in your own agents afterward;
#                            recipes/ shows the exact wiring for Strands,
#                            LangGraph, Claude Code, the Anthropic and
#                            OpenAI SDKs, and boto3.
#
#   Gateway + demo app       Everything above, plus the two AgentCore
#                            Runtime containers (Strands/LangGraph and
#                            Claude Code) and the demo web app (CloudFront)
#                            with the live chat and fleet views.
#
# Usage:
#   ./deploy.sh                          # interactive: asks which shape
#   ./deploy.sh --infra-only             # gateway infra, no prompt
#   ./deploy.sh --with-demo              # infra + runtimes + demo app
#   ./deploy.sh --with-demo --region us-east-1 --reuse-logging
#   ./deploy.sh --infra-only --suffix test2   # second, independent copy
#
# Flags are passed through to infra/scripts/deploy.sh where relevant
# (--region, --reuse-logging, --force-logging, --suffix). AWS_PROFILE and
# AWS_REGION are honored. --suffix deploys an independent copy of the gateway
# infra next to an existing one (infra shape only; the demo shape uses fixed
# names).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

MODE=""
INFRA_ARGS=()
REGION="${AWS_REGION:-}"
NAME_SUFFIX=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --infra-only) MODE="infra"; shift ;;
    --with-demo)  MODE="demo";  shift ;;
    --region)     REGION="$2"; INFRA_ARGS+=("--region" "$2"); shift 2 ;;
    --region=*)   REGION="${1#*=}"; INFRA_ARGS+=("$1"); shift ;;
    --force-logging) INFRA_ARGS+=("--force-logging"); shift ;;
    --reuse-logging) INFRA_ARGS+=("--reuse-logging"); shift ;;
    --suffix)   NAME_SUFFIX="$2"; INFRA_ARGS+=("--suffix" "$2"); shift 2 ;;
    --suffix=*) NAME_SUFFIX="${1#*=}"; INFRA_ARGS+=("$1"); shift ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1 (see --help)"; exit 1 ;;
  esac
done

if [ -z "$MODE" ]; then
  echo "What do you want to deploy?"
  echo ""
  echo "  1) Gateway infra only -- the governance engine. Wire your own"
  echo "     agents to it afterward (see recipes/)."
  echo "  2) Gateway infra + demo app -- also builds the two AgentCore"
  echo "     Runtime containers and the demo web app (needs Docker)."
  echo ""
  read -r -p "Choice [1]: " CHOICE
  case "${CHOICE:-1}" in
    2) MODE="demo" ;;
    *) MODE="infra" ;;
  esac
fi

REGION="${REGION:-us-east-1}"
export AWS_REGION="$REGION"

# ---------------------------------------------------------------------------
# Step 1 (both shapes): the governance gateway infra
# ---------------------------------------------------------------------------
"$HERE/infra/scripts/deploy.sh" ${INFRA_ARGS[@]+"${INFRA_ARGS[@]}"}

if [ "$MODE" = "infra" ]; then
  echo ""
  echo "Gateway infra deployed. Point your agents at the gateway URL above"
  echo "with a user's Cognito JWT; recipes/ has copy-paste wiring for each"
  echo "SDK. To add the demo app later: ./deploy.sh --with-demo"
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 2 (demo shape): the AgentCore Runtime containers
# ---------------------------------------------------------------------------
command -v docker >/dev/null || { echo "docker is required for the demo runtimes"; exit 1; }

if [ -n "$NAME_SUFFIX" ]; then
  # The runtimes and the demo app stack still use fixed names
  # (governance_sample_frameworks, GovernanceDemoAppStack), so a second demo
  # copy would collide with the first. The gateway infra above deployed fine;
  # wire your own clients to it (recipes/), or add the demo app to the
  # unsuffixed deployment instead.
  echo "--suffix supports the gateway infra only; the demo shape uses fixed names." >&2
  exit 2
fi

STACK_NAME="AgentCoreGovernanceSample"
out() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
USER_POOL_ID="$(out UserPoolId)"
APP_CLIENT_ID="$(out AppClientId)"
DISCOVERY_URL="https://cognito-idp.${REGION}.amazonaws.com/${USER_POOL_ID}/.well-known/openid-configuration"

echo ""
echo "Deploying the AgentCore Runtime containers..."
ACCOUNT_ID="$ACCOUNT_ID" REGION="$REGION" \
  DISCOVERY_URL="$DISCOVERY_URL" APP_CLIENT_ID="$APP_CLIENT_ID" \
  "$HERE/runtime/deploy.sh"

FRAMEWORKS_RUNTIME_ARN="$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
  --query "agentRuntimes[?agentRuntimeName=='governance_sample_frameworks'].agentRuntimeArn | [0]" --output text)"
CLAUDECODE_RUNTIME_ARN="$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
  --query "agentRuntimes[?agentRuntimeName=='governance_sample_claudecode'].agentRuntimeArn | [0]" --output text)"
[ "$FRAMEWORKS_RUNTIME_ARN" = "None" ] && FRAMEWORKS_RUNTIME_ARN=""
[ "$CLAUDECODE_RUNTIME_ARN" = "None" ] && CLAUDECODE_RUNTIME_ARN=""
if [ -z "$FRAMEWORKS_RUNTIME_ARN" ]; then
  echo "Could not resolve the frameworks runtime ARN after deploy; check runtime/deploy.sh output." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Step 3 (demo shape): the demo web app
# ---------------------------------------------------------------------------
echo ""
echo "Deploying the demo app..."
REGION="$REGION" INFRA_STACK="$STACK_NAME" \
  FRAMEWORKS_RUNTIME_ARN="$FRAMEWORKS_RUNTIME_ARN" \
  CLAUDECODE_RUNTIME_ARN="$CLAUDECODE_RUNTIME_ARN" \
  "$HERE/app/scripts/deploy.sh"
