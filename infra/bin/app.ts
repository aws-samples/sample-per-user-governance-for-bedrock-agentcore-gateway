#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { App, Aspects } from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { GovernanceStack } from '../lib/governance-stack';
import { applyNagSuppressions } from '../lib/nag-suppressions';

const app = new App();
// Optional -c nameSuffix=<s> makes every physical name this app chooses
// unique, so two copies of the stack can share an account and region. The
// suffix reaches the stack name here and the gateway and project names where
// they are set; everything else is CloudFormation-generated and needs no help.
const nameSuffix = app.node.tryGetContext('nameSuffix') as string | undefined;
if (nameSuffix && !/^[A-Za-z0-9-]{1,20}$/.test(nameSuffix)) {
  throw new Error(
    'nameSuffix must be 1-20 characters of letters, digits, or hyphens',
  );
}
const stackId = nameSuffix
  ? `AgentCoreGovernanceSample-${nameSuffix}`
  : 'AgentCoreGovernanceSample';
const stack = new GovernanceStack(app, stackId, {
  description:
    'Sample: per-user governance for Amazon Bedrock AgentCore Gateway (sandbox only)',
});

// cdk-nag runs on every synth and deploy, so a rule regression fails the build
// instead of being found in review. Every remaining finding is suppressed
// explicitly with a reason in lib/nag-suppressions.ts; the goal state is zero
// unsuppressed errors and zero unsuppressed warnings.
applyNagSuppressions(stack);
Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
