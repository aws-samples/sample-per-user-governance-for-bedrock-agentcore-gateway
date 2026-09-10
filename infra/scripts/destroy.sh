#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

#
# Teardown for the per-user governance gateway stack.
#
# Destroys the CloudFormation stack, optionally disables the account-level
# Bedrock invocation logging this stack turned on, and reminds you about the
# retained Cognito user pool (it has deletion protection and is NOT removed by
# the stack delete).
#
# Usage:
#   ./scripts/destroy.sh [--region us-east-1] [--suffix <name-suffix>]
# Environment:
#   AWS_REGION     region (overridden by --region)
#   AWS_PROFILE    passed through to every aws / cdk call
#
# Pass the same --suffix the stack was deployed with.
set -euo pipefail

STACK_NAME="AgentCoreGovernanceSample"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

REGION="${AWS_REGION:-}"
NAME_SUFFIX=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --region=*) REGION="${1#*=}"; shift ;;
    --suffix) NAME_SUFFIX="$2"; shift 2 ;;
    --suffix=*) NAME_SUFFIX="${1#*=}"; shift ;;
    -h|--help)
      grep '^#' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
REGION="${REGION:-us-east-1}"
if [ -n "$NAME_SUFFIX" ]; then
  STACK_NAME="${STACK_NAME}-${NAME_SUFFIX}"
fi
export AWS_REGION="$REGION"
export AWS_DEFAULT_REGION="$REGION"
export CDK_DEFAULT_REGION="$REGION"

command -v aws >/dev/null || { echo "ERROR: aws CLI is required"; exit 1; }
command -v npx >/dev/null || { echo "ERROR: npx (Node.js) is required"; exit 1; }
aws sts get-caller-identity >/dev/null || {
  echo "ERROR: AWS credentials are not valid."; exit 1;
}

echo "This will DESTROY stack '$STACK_NAME' in $REGION."
read -r -p "Type the stack name to confirm: " CONFIRM
if [ "$CONFIRM" != "$STACK_NAME" ]; then
  echo "Confirmation did not match. Aborting."
  exit 1
fi

# Capture the retained user pool id (for the reminder) before the stack goes.
USER_POOL_ID="$(aws cloudformation describe-stacks \
  --region "$REGION" --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue | [0]" \
  --output text 2>/dev/null || echo "")"

echo ""
echo "Destroying the stack..."
# The context must match the deploy, or the app synthesizes the unsuffixed
# stack id and cdk destroy cannot find this one.
if [ -n "$NAME_SUFFIX" ]; then
  npx cdk destroy "$STACK_NAME" --force --context "nameSuffix=$NAME_SUFFIX"
else
  npx cdk destroy "$STACK_NAME" --force
fi

echo ""
echo "Disable the account-level Bedrock invocation logging this stack enabled?"
echo "  It is an account-level, per-region setting. Only disable it if this stack"
echo "  was the one that configured it (deploy.sh points it at this stack's log"
echo "  group). If another workload relies on it, answer no."
read -r -p "Disable Bedrock invocation logging in $REGION? [y/N]: " DISABLE_LOGGING
if [ "$DISABLE_LOGGING" = "y" ] || [ "$DISABLE_LOGGING" = "Y" ]; then
  aws bedrock delete-model-invocation-logging-configuration --region "$REGION"
  echo "  invocation logging configuration deleted."
else
  echo "  left invocation logging configuration in place."
fi

echo ""
echo "REMINDER: the Cognito user pool is retained (deletion protection is on)."
if [ -n "$USER_POOL_ID" ] && [ "$USER_POOL_ID" != "None" ]; then
  echo "  Remove it manually when you are done:"
  echo "    aws cognito-idp update-user-pool --region $REGION --user-pool-id $USER_POOL_ID --deletion-protection INACTIVE"
  echo "    aws cognito-idp delete-user-pool --region $REGION --user-pool-id $USER_POOL_ID"
else
  echo "  Find it with: aws cognito-idp list-user-pools --region $REGION --max-results 60"
fi
echo ""
echo "Teardown complete."
