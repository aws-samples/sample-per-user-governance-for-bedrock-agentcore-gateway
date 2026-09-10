#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import * as cdk from "aws-cdk-lib";
import { AwsSolutionsChecks } from "cdk-nag";
import { GovernanceDemoAppStack } from "../lib/stack";
import { applyNagSuppressions } from "../lib/nag-suppressions";

const app = new cdk.App();
const stack = new GovernanceDemoAppStack(app, "GovernanceDemoAppStack", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION || "us-east-1",
  },
  description:
    "Demo app for per-user governance on the Bedrock AgentCore Gateway",
});

// cdk-nag runs on every synth and deploy. Findings this demo app accepts are
// suppressed one at a time with a reason in lib/nag-suppressions.ts; anything
// new fails synth.
applyNagSuppressions(stack);
cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
