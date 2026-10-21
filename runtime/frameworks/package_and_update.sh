#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# package_and_update.sh — Docker-free CodeZip deploy of the frameworks
# AgentCore Runtime. Packages arm64 wheels + source into a zip, uploads to S3,
# and creates (or delete-recreates) the agent runtime pointing at that zip.
#
# This is the SIMPLER alternative to the container/ECR path in deploy.sh:
# no Docker, no ECR — just `uv pip install --target`, zip, s3 cp, and
# create-agent-runtime with codeConfiguration.code.s3. Mirrors the proven
# travel-ai-demo-agentcore/scripts/package_and_update.sh, adapted to:
#   * this repo's SDK entrypoint (app.py uses BedrockAgentCoreApp; no main.py),
#   * us-east-1 + the CUSTOM_JWT authorizer (end-user Cognito token),
#   * the fully-pinned requirements.txt already in this dir.
#
# Config is passed as ENV VARS at create time (stateless container; no config
# file baked in). The web-search sub-agent MODEL is resolved from
# config/config.yaml (websearch_agent.model) by THIS script and passed as an
# env default; per-request the server may still override it in the payload.
#
# Inputs (env vars; sensible defaults for this account/region):
#   ACCOUNT_ID        AWS account (default: from sts get-caller-identity)
#   REGION            default us-east-1 (must match the gateway region)
#   GATEWAY_URL       governance gateway base (…/ , no /inference suffix)
#   WEBSEARCH_GATEWAY_URL / WEBSEARCH_REGION / WEBSEARCH_TOOL_NAME
#   DISCOVERY_URL     Cognito OIDC discovery URL (for the JWT authorizer)
#   APP_CLIENT_ID     Cognito app client id allowed by the authorizer
#   ROLE_ARN          runtime execution role (default: the governance runtime role)
#
# Runtime name must not contain hyphens.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

REGION="${REGION:-us-east-1}"
export AWS_REGION="${REGION}" AWS_DEFAULT_REGION="${REGION}" AWS_PAGER=""
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"

RT_NAME="governance_sample_frameworks"
BUCKET="acv-runtime-code-${ACCOUNT_ID}-${REGION}"
# Timestamped key so every deploy is a DISTINCT S3 object. A same-key overwrite
# is not reliably picked up (AgentCore can serve the prior code artifact), which
# is why update-then-serve-stale was observed; a fresh key forces new code.
KEY="frameworks/deployment_package-$(date +%Y%m%d%H%M%S).zip"
ROLE_ARN="${ROLE_ARN:-arn:aws:iam::${ACCOUNT_ID}:role/agentcore-governance-runtime-role}"

# --- resolve the web-search sub-agent model from config.yaml (source of truth) ---
CONFIG_YAML="${REPO_ROOT}/config/config.yaml"
WEBSEARCH_AGENT_MODEL="${WEBSEARCH_AGENT_MODEL:-$(python3 -c "
import sys,yaml
try:
    d=yaml.safe_load(open('${CONFIG_YAML}')) or {}
    print((d.get('websearch_agent') or {}).get('model',''))
except Exception:
    print('')
" 2>/dev/null)}"
WEBSEARCH_AGENT_MODEL="${WEBSEARCH_AGENT_MODEL:-openai.gpt-5.6-luna}"
echo "web-search sub-agent model (from config.yaml): ${WEBSEARCH_AGENT_MODEL}"

[[ -n "${DISCOVERY_URL:-}" && -n "${APP_CLIENT_ID:-}" ]] || {
  echo "ERROR: DISCOVERY_URL and APP_CLIENT_ID are required (the JWT authorizer)." >&2
  exit 1
}

# --- 1. S3 bucket for the code zip (create if missing) -----------------------
if ! aws s3api head-bucket --bucket "${BUCKET}" >/dev/null 2>&1; then
  echo "Creating S3 bucket ${BUCKET}"
  if [[ "${REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${BUCKET}" >/dev/null
  else
    aws s3api create-bucket --bucket "${BUCKET}" \
      --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
  fi
fi

# --- 2. Package arm64 wheels + source (no Docker) ----------------------------
BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT
echo "Packaging arm64 deps from the pinned CodeZip requirements (no Docker)..."
uv pip install \
  --python-platform aarch64-manylinux2014 --python-version 3.12 \
  --target="${BUILD}/pkg" --only-binary=:all: \
  -r "${SCRIPT_DIR}/requirements-codezip.txt" >/dev/null 2>&1
cp "${SCRIPT_DIR}/app.py" "${SCRIPT_DIR}/websearch_client.py" "${BUILD}/pkg/"
( cd "${BUILD}/pkg" && zip -qr "${BUILD}/deployment_package.zip" . )
echo "  zip: $(du -m "${BUILD}/deployment_package.zip" | cut -f1) MB"

echo "Uploading to s3://${BUCKET}/${KEY} ..."
aws s3 cp "${BUILD}/deployment_package.zip" "s3://${BUCKET}/${KEY}" >/dev/null

# --- 3. Artifact + env + authorizer ------------------------------------------
# entryPoint: opentelemetry-instrument wraps our SDK app so Strands/Bedrock/tool
# spans export to the runtime's CloudWatch 'spans' stream (same as travel-ai).
# app.py uses BedrockAgentCoreApp().run(), which serves /invocations + /ping.
cat > "${BUILD}/artifact.json" <<JSON
{"codeConfiguration":{"code":{"s3":{"bucket":"${BUCKET}","prefix":"${KEY}"}},"runtime":"PYTHON_3_12","entryPoint":["opentelemetry-instrument","app.py"]}}
JSON

cat > "${BUILD}/env.json" <<JSON
{"AWS_REGION":"${REGION}","GATEWAY_URL":"${GATEWAY_URL:-}","WEBSEARCH_GATEWAY_URL":"${WEBSEARCH_GATEWAY_URL:-}","WEBSEARCH_REGION":"${WEBSEARCH_REGION:-us-east-1}","WEBSEARCH_TOOL_NAME":"${WEBSEARCH_TOOL_NAME:-web-search-tool___WebSearch}","WEBSEARCH_AGENT_MODEL":"${WEBSEARCH_AGENT_MODEL}"}
JSON

cat > "${BUILD}/authorizer.json" <<JSON
{"customJWTAuthorizer":{"discoveryUrl":"${DISCOVERY_URL}","allowedClients":["${APP_CLIENT_ID}"]}}
JSON

# --- 4. Create or delete-recreate the runtime --------------------------------
# UpdateAgentRuntime may be denied for some identities (travel-ai hit this);
# probe first, fall back to delete + recreate.
_rt_id() { aws bedrock-agentcore-control list-agent-runtimes --output json 2>/dev/null | \
  python3 -c "import sys,json;rs=[r for r in json.load(sys.stdin).get('agentRuntimes',[]) if r['agentRuntimeName']=='${RT_NAME}'];print(rs[0]['agentRuntimeId'] if rs else '')"; }

RID="$(_rt_id)"
COMMON=( --region "${REGION}"
  --agent-runtime-artifact "file://${BUILD}/artifact.json"
  --role-arn "${ROLE_ARN}"
  --network-configuration '{"networkMode":"PUBLIC"}'
  --authorizer-configuration "file://${BUILD}/authorizer.json"
  --request-header-configuration '{"requestHeaderAllowlist":["Authorization"]}'
  --environment-variables "file://${BUILD}/env.json" )

if [[ -n "${RID}" ]]; then
  echo "Runtime ${RT_NAME} exists (${RID}); trying update..."
  if aws bedrock-agentcore-control update-agent-runtime --agent-runtime-id "${RID}" \
       "${COMMON[@]}" --query agentRuntimeArn --output text 2>/tmp/acv_rt_update_err; then
    echo "  updated."
  else
    echo "  update denied/failed ($(head -1 /tmp/acv_rt_update_err)); delete + recreate..."
    aws bedrock-agentcore-control delete-agent-runtime --region "${REGION}" \
      --agent-runtime-id "${RID}" >/dev/null 2>&1 || true
    for _ in $(seq 1 30); do [[ -z "$(_rt_id)" ]] && break; sleep 5; done
    aws bedrock-agentcore-control create-agent-runtime \
      --agent-runtime-name "${RT_NAME}" "${COMMON[@]}" \
      --description "ACV frameworks runtime (CodeZip): Strands main agent + web-search sub-agent, GPT non-OSS via gateway Responses door" \
      --query agentRuntimeArn --output text
  fi
else
  echo "Creating runtime ${RT_NAME} (CodeZip)..."
  aws bedrock-agentcore-control create-agent-runtime \
    --agent-runtime-name "${RT_NAME}" "${COMMON[@]}" \
    --description "ACV frameworks runtime (CodeZip): Strands main agent + web-search sub-agent, GPT non-OSS via gateway Responses door" \
    --query agentRuntimeArn --output text
fi

echo "Done. Poll until READY:"
echo "  aws bedrock-agentcore-control list-agent-runtimes --region ${REGION} --query \"agentRuntimes[?agentRuntimeName=='${RT_NAME}'].status\" --output text"
