#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Demo app deploy. Deploy the infra module FIRST; this script consumes its
# CloudFormation outputs (GatewayUrl, UserPoolId, AppClientId, TableName)
# and nothing else from it. Chat goes browser -> AgentCore Runtime ->
# gateway directly, so pass the runtime ARNs (from the recipes modules) as
# env vars: FRAMEWORKS_RUNTIME_ARN and CLAUDECODE_RUNTIME_ARN. Everything
# the app does at runtime is real: agent turns through the live gateway,
# reads from the live governance table.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

echo "governance demo app deploy (all real, no simulation)"
echo "-----------------------------------------------------"

command -v node >/dev/null || { echo "node is required"; exit 1; }
command -v npm >/dev/null || { echo "npm is required"; exit 1; }
command -v docker >/dev/null || { echo "docker is required (Lambda dependency bundling)"; exit 1; }
aws sts get-caller-identity >/dev/null || { echo "AWS credentials are required (aws configure / sso login)"; exit 1; }

# Env vars first (the top-level deploy.sh chains this script); interactive
# prompts only when run standalone without them.
if [ -z "${REGION:-}" ]; then
  read -r -p "Region [us-east-1]: " REGION
fi
REGION="${REGION:-us-east-1}"
export AWS_REGION="$REGION" CDK_DEFAULT_REGION="$REGION"

if [ -z "${INFRA_STACK:-}" ]; then
  read -r -p "Infra stack name [AgentCoreGovernanceSample]: " INFRA_STACK
fi
INFRA_STACK="${INFRA_STACK:-AgentCoreGovernanceSample}"

out() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$INFRA_STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

GATEWAY_URL="$(out GatewayUrl)"
USER_POOL_ID="$(out UserPoolId)"
APP_CLIENT_ID="$(out AppClientId)"
TABLE_NAME="$(out TableName)"
for v in GATEWAY_URL USER_POOL_ID APP_CLIENT_ID TABLE_NAME; do
  if [ -z "${!v}" ] || [ "${!v}" = "None" ]; then
    echo "Missing $v in $INFRA_STACK outputs. Deploy the infra module first."
    exit 1
  fi
done
echo "  GatewayUrl:  $GATEWAY_URL"
echo "  UserPoolId:  $USER_POOL_ID"
echo "  AppClientId: $APP_CLIENT_ID"
echo "  TableName:   $TABLE_NAME"

# Runtime ARNs: env vars first, interactive prompt as fallback. The browser
# invokes these runtimes directly; without an ARN the pane stays disabled.
# Prompt only when the variable is UNSET: the top-level deploy.sh chains this
# script with the variables set (possibly empty), and an empty set value
# means "skip" rather than "ask".
if [ -z "${FRAMEWORKS_RUNTIME_ARN+x}" ]; then
  read -r -p "Frameworks runtime ARN (Strands/LangGraph; Enter to skip): " FRAMEWORKS_RUNTIME_ARN
fi
if [ -z "${CLAUDECODE_RUNTIME_ARN+x}" ]; then
  read -r -p "Claude Code runtime ARN (optional, Enter to skip): " CLAUDECODE_RUNTIME_ARN
fi
FRAMEWORKS_RUNTIME_ARN="${FRAMEWORKS_RUNTIME_ARN:-}"
CLAUDECODE_RUNTIME_ARN="${CLAUDECODE_RUNTIME_ARN:-}"
PRIMARY_MODEL="${PRIMARY_MODEL:-anthropic.claude-sonnet-5}"
echo "  FrameworksRuntimeArn: ${FRAMEWORKS_RUNTIME_ARN:-<none>}"
echo "  ClaudecodeRuntimeArn: ${CLAUDECODE_RUNTIME_ARN:-<none>}"
echo "  PrimaryModel:         $PRIMARY_MODEL"

CTX=(--context "gatewayUrl=$GATEWAY_URL" --context "userPoolId=$USER_POOL_ID" \
     --context "appClientId=$APP_CLIENT_ID" --context "tableName=$TABLE_NAME" \
     --context "primaryModel=$PRIMARY_MODEL")
if [ -n "$FRAMEWORKS_RUNTIME_ARN" ]; then
  CTX+=(--context "frameworksRuntimeArn=$FRAMEWORKS_RUNTIME_ARN")
fi
if [ -n "$CLAUDECODE_RUNTIME_ARN" ]; then
  CTX+=(--context "claudecodeRuntimeArn=$CLAUDECODE_RUNTIME_ARN")
fi

echo ""
echo "Pass 1/2: deploying the app stack..."
cd cdk
# npm ci, not npm install: the lockfile is the tested and scanned dependency
# tree, and ci installs exactly it instead of re-resolving semver ranges.
npm ci --no-audit --no-fund >/dev/null
npx cdk bootstrap >/dev/null 2>&1 || true
npx cdk deploy GovernanceDemoAppStack --require-approval never "${CTX[@]}" \
  --outputs-file /tmp/governance-demo-outputs.json

API_URL=$(node -e "console.log(require('/tmp/governance-demo-outputs.json').GovernanceDemoAppStack.ApiUrl)")

echo ""
echo "Pass 2/2: building the frontend against $API_URL and publishing..."
cd ../frontend
npm ci --no-audit --no-fund >/dev/null
# Write the build inputs to .env.local instead of passing them inline only.
# Vite reads .env.local on every build, so a leftover file from an earlier
# deploy (different account, deleted stack) would otherwise keep driving any
# plain "npm run build" and publish a site pointed at dead infrastructure.
# Regenerating it here keeps the file and this deploy in agreement.
cat > .env.local <<EOF
VITE_API_URL=$API_URL
VITE_AWS_REGION=$REGION
VITE_APP_CLIENT_ID=$APP_CLIENT_ID
VITE_USER_POOL_ID=$USER_POOL_ID
VITE_TABLE_NAME=$TABLE_NAME
VITE_GATEWAY_URL=$GATEWAY_URL
VITE_PRIMARY_MODEL=$PRIMARY_MODEL
VITE_FRAMEWORKS_RUNTIME_ARN=$FRAMEWORKS_RUNTIME_ARN
VITE_CLAUDECODE_RUNTIME_ARN=$CLAUDECODE_RUNTIME_ARN
EOF
npm run build
cd ../cdk
npx cdk deploy GovernanceDemoAppStack --require-approval never "${CTX[@]}" \
  --outputs-file /tmp/governance-demo-outputs.json

SITE_URL=$(node -e "console.log(require('/tmp/governance-demo-outputs.json').GovernanceDemoAppStack.SiteUrl)")

echo ""
echo "One-time presenter password (never stored by the app):"
echo "  aws cognito-idp admin-set-user-password --region $REGION \\"
echo "    --user-pool-id $USER_POOL_ID --username demo-presenter \\"
echo "    --password '<choose a strong one>' --permanent"
echo ""
echo "Done."
echo "  Site: $SITE_URL"
echo "  API:  $API_URL   (Cognito JWT required on every route)"
echo ""
echo "Teardown when finished: scripts/destroy.sh (the gateway, user pool,"
echo "governance table, and the runtimes belong to other modules and are"
echo "NOT touched)."
