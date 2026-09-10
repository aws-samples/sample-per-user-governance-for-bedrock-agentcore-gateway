#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Deploy the two AgentCore Runtime containers (frameworks, claudecode) that
# call Anthropic models through the AgentCore Gateway governance stack.
#
# Inputs (env vars or flags; flags win):
#   ACCOUNT_ID      --account-id      AWS account id (required)
#   REGION          --region          AWS region (default: us-east-1)
#   REPO_NAME       --repo-name       ECR repository name (default: agentcore-governance-runtimes)
#   DISCOVERY_URL   --discovery-url   Cognito user pool OIDC discovery URL (required)
#                                     e.g. https://cognito-idp.<region>.amazonaws.com/<pool-id>/.well-known/openid-configuration
#   APP_CLIENT_ID   --app-client-id   Cognito app client id allowed by the JWT authorizer (required)
#
# What it does, idempotently:
#   1. Creates the ECR repository if missing.
#   2. Builds and pushes both images (linux/arm64, --provenance=false).
#   3. Creates or reuses the runtime IAM role from iam/trust.json + iam/permissions.json.
#   4. Creates each agent runtime with a customJWTAuthorizer (so the runtimes
#      accept the end user's Cognito JWT directly) and networkMode PUBLIC;
#      if a runtime with the same name already exists, updates it instead.
#   5. Echoes both runtime ARNs.
#
# Note: agent runtime names must not contain hyphens; underscores are used.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ACCOUNT_ID="${ACCOUNT_ID:-}"
REGION="${REGION:-us-east-1}"
REPO_NAME="${REPO_NAME:-agentcore-governance-runtimes}"
DISCOVERY_URL="${DISCOVERY_URL:-}"
APP_CLIENT_ID="${APP_CLIENT_ID:-}"

usage() {
  sed -n '2,15p' "${BASH_SOURCE[0]}"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account-id)    ACCOUNT_ID="$2";    shift 2 ;;
    --region)        REGION="$2";        shift 2 ;;
    --repo-name)     REPO_NAME="$2";     shift 2 ;;
    --discovery-url) DISCOVERY_URL="$2"; shift 2 ;;
    --app-client-id) APP_CLIENT_ID="$2"; shift 2 ;;
    -h|--help)       usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

[[ -n "$ACCOUNT_ID" && -n "$DISCOVERY_URL" && -n "$APP_CLIENT_ID" ]] || {
  echo "ERROR: ACCOUNT_ID, DISCOVERY_URL and APP_CLIENT_ID are required." >&2
  usage
}

REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
ROLE_NAME="agentcore-governance-runtime-role"
POLICY_NAME="agentcore-governance-runtime-policy"
RUNTIME_FRAMEWORKS="governance_sample_frameworks"
RUNTIME_CLAUDECODE="governance_sample_claudecode"

# --- 1. ECR repository (create if missing) ----------------------------------
if ! aws ecr describe-repositories --region "$REGION" \
    --repository-names "$REPO_NAME" >/dev/null 2>&1; then
  echo "Creating ECR repository $REPO_NAME"
  aws ecr create-repository --region "$REGION" \
    --repository-name "$REPO_NAME" \
    --tags Key=Purpose,Value=agentcore-gateway-governance-sample >/dev/null
else
  echo "ECR repository $REPO_NAME already exists"
fi

# --- 2. Build and push both images (linux/arm64) -----------------------------
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

docker buildx build --platform linux/arm64 --provenance=false \
  -t "${REGISTRY}/${REPO_NAME}:frameworks" --push "${SCRIPT_DIR}/frameworks"
docker buildx build --platform linux/arm64 --provenance=false \
  -t "${REGISTRY}/${REPO_NAME}:claudecode" --push "${SCRIPT_DIR}/claudecode"

# --- 3. Runtime IAM role (create or reuse) ------------------------------------
RENDER_DIR="$(mktemp -d)"
trap 'rm -rf "$RENDER_DIR"' EXIT
for doc in trust permissions; do
  # '#' as the s/// delimiter, not '/': ECR repository names may be namespaced
  # (team/governance-runtimes), and a slash inside the replacement would end
  # the expression early ("unknown option to `s'") after the images have
  # already been pushed. None of these three values can contain '#'.
  sed -e "s#ACCOUNT_ID#${ACCOUNT_ID}#g" \
      -e "s#REGION#${REGION}#g" \
      -e "s#REPO_NAME#${REPO_NAME}#g" \
      "${SCRIPT_DIR}/iam/${doc}.json" > "${RENDER_DIR}/${doc}.json"
done

if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "Creating IAM role $ROLE_NAME"
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "file://${RENDER_DIR}/trust.json" \
    --tags Key=Purpose,Value=agentcore-gateway-governance-sample >/dev/null
  NEW_ROLE=1
else
  echo "IAM role $ROLE_NAME already exists"
  NEW_ROLE=0
fi

POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${POLICY_NAME}"
if ! aws iam get-policy --policy-arn "$POLICY_ARN" >/dev/null 2>&1; then
  echo "Creating IAM policy $POLICY_NAME"
  aws iam create-policy --policy-name "$POLICY_NAME" \
    --policy-document "file://${RENDER_DIR}/permissions.json" \
    --tags Key=Purpose,Value=agentcore-gateway-governance-sample >/dev/null
else
  echo "IAM policy $POLICY_NAME already exists (delete it and re-run to pick up edits)"
fi
aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN"

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
if [[ "$NEW_ROLE" == "1" ]]; then
  echo "Waiting 10s for IAM role propagation"
  sleep 10
fi

# --- 4. Create or update both agent runtimes ---------------------------------
AUTHORIZER_JSON=$(printf \
  '{"customJWTAuthorizer":{"discoveryUrl":"%s","allowedClients":["%s"]}}' \
  "$DISCOVERY_URL" "$APP_CLIENT_ID")
NETWORK_JSON='{"networkMode":"PUBLIC"}'
# Forward the caller's Authorization header to the container. Without this
# allowlist the runtime consumes the header for its own JWT authorizer and the
# agent never sees the user's token (RequestContext.request_headers is empty).
HEADERS_JSON='{"requestHeaderAllowlist":["Authorization"]}'

deploy_runtime() {
  local name="$1" tag="$2"
  local artifact_json
  artifact_json=$(printf \
    '{"containerConfiguration":{"containerUri":"%s"}}' \
    "${REGISTRY}/${REPO_NAME}:${tag}")

  local existing_id
  existing_id=$(aws bedrock-agentcore-control list-agent-runtimes \
    --region "$REGION" \
    --query "agentRuntimes[?agentRuntimeName=='${name}'].agentRuntimeId" \
    --output text)

  if [[ -n "$existing_id" && "$existing_id" != "None" ]]; then
    echo "Runtime ${name} exists (${existing_id}); updating" >&2
    aws bedrock-agentcore-control update-agent-runtime \
      --region "$REGION" \
      --agent-runtime-id "$existing_id" \
      --agent-runtime-artifact "$artifact_json" \
      --role-arn "$ROLE_ARN" \
      --network-configuration "$NETWORK_JSON" \
      --authorizer-configuration "$AUTHORIZER_JSON" \
      --request-header-configuration "$HEADERS_JSON" \
      --query agentRuntimeArn --output text
  else
    echo "Creating runtime ${name}" >&2
    aws bedrock-agentcore-control create-agent-runtime \
      --region "$REGION" \
      --agent-runtime-name "$name" \
      --agent-runtime-artifact "$artifact_json" \
      --role-arn "$ROLE_ARN" \
      --network-configuration "$NETWORK_JSON" \
      --authorizer-configuration "$AUTHORIZER_JSON" \
      --request-header-configuration "$HEADERS_JSON" \
      --query agentRuntimeArn --output text
  fi
}

FRAMEWORKS_ARN=$(deploy_runtime "$RUNTIME_FRAMEWORKS" "frameworks")
CLAUDECODE_ARN=$(deploy_runtime "$RUNTIME_CLAUDECODE" "claudecode")

# --- 5. Report ----------------------------------------------------------------
echo ""
echo "Frameworks runtime ARN: ${FRAMEWORKS_ARN}"
echo "Claude Code runtime ARN: ${CLAUDECODE_ARN}"
echo "Runtimes start in CREATING/UPDATING; poll get-agent-runtime until READY (~2-3 min)."
