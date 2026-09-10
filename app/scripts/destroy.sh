#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Tear down the demo app stack only. The infra module (gateway, interceptor,
# governance table, user pool) is owned separately and is not touched.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE/cdk"

read -r -p "Region [us-east-1]: " REGION
REGION="${REGION:-us-east-1}"
export AWS_REGION="$REGION" CDK_DEFAULT_REGION="$REGION"

npm ci --no-audit --no-fund >/dev/null
npx cdk destroy GovernanceDemoAppStack --force

echo "App stack destroyed. Infra module resources were not touched."
