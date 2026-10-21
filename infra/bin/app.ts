#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The CDK app.
 *
 * `cdk deploy --all` stands up the whole system: the governance plane, the
 * Cognito user pool and its demo identities, the Bedrock guardrail, AgentCore
 * Memory, Bedrock invocation logging, the web-search gateway, and the agent
 * runtime. No out-of-band AWS CLI steps are needed.
 *
 * Three stacks, in dependency order:
 *
 *  1. **WebSearch** — the `AWS_IAM` gateway fronting the managed Web Search
 *     Tool. First because the runtime needs its endpoint. Skipped outside the
 *     three regions that offer the connector, or with `-c webSearch=false`.
 *  2. **Governance** — gateway, interceptor, ledger, Cognito, guardrail,
 *     memory, invocation logging, demo identities, mantle project.
 *  3. **Runtime** — the AgentCore Runtime hosting the Strands agent. Last
 *     because it needs the gateway URL, the Cognito discovery URL, and the
 *     web-search endpoint.
 *
 * Deploy a subset by naming stacks: `cdk deploy AgentCoreGovernanceSample`.
 */

import { App, Aspects } from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { loadConfig } from '../lib/config';
import { GovernanceStack } from '../lib/governance-stack';
import {
  applyNagSuppressions,
  applyRuntimeNagSuppressions,
  applyWebSearchNagSuppressions,
} from '../lib/nag-suppressions';
import { RuntimeStack } from '../lib/runtime-stack';
import { WebSearchStack } from '../lib/websearch-stack';
import { WEB_SEARCH_REGIONS } from '../lib/websearch/websearch-gateway';

const app = new App();

// Optional -c nameSuffix=<s> makes every physical name this app chooses
// unique, so two copies of the stack can share an account and region. The
// suffix reaches the stack names here and the gateway, memory, runtime and
// project names where they are set; everything else is CloudFormation-generated
// and needs no help.
const nameSuffix = app.node.tryGetContext('nameSuffix') as string | undefined;
if (nameSuffix && !/^[A-Za-z0-9-]{1,20}$/.test(nameSuffix)) {
  throw new Error(
    'nameSuffix must be 1-20 characters of letters, digits, or hyphens',
  );
}
const suffixPart = nameSuffix ? `-${nameSuffix}` : '';

// config.yaml is the single source of truth for regions, guardrail policy,
// memory strategies, groups and USD thresholds. Loaded once and shared, so the
// three stacks cannot disagree about it.
const config = loadConfig(app.node.tryGetContext('configPath') as string | undefined);

// Region resolution order: CDK's own environment variables (set by the CLI from
// the active profile), then config.yaml. An explicit -c region wins over both.
const region =
  (app.node.tryGetContext('region') as string | undefined) ??
  process.env.CDK_DEFAULT_REGION ??
  config.regions.primary;
const account = process.env.CDK_DEFAULT_ACCOUNT;
const env = { account, region };

// --- 1. Web search -----------------------------------------------------------
// Opt out with -c webSearch=false. Skipped automatically outside the connector's
// regions so a deploy to, say, eu-central-1 still brings up everything else
// rather than failing the whole app.
const webSearchRequested = app.node.tryGetContext('webSearch') !== 'false';
const webSearchSupported = WEB_SEARCH_REGIONS.includes(region);
let webSearchStack: WebSearchStack | undefined;

if (webSearchRequested && webSearchSupported) {
  webSearchStack = new WebSearchStack(app, `AgentCoreGovernanceWebSearch${suffixPart}`, {
    env,
    config,
    nameSuffix,
    connectorVersion: app.node.tryGetContext('webSearchConnectorVersion') as
      | string
      | undefined,
    description:
      'Web Search Tool gateway for the ACV LLM Gateway web-search sub-agent (sandbox only)',
  });
  applyWebSearchNagSuppressions(webSearchStack);
} else if (webSearchRequested && !webSearchSupported) {
  // Not an error: the rest of the system works without web search, and failing
  // here would block an otherwise valid deployment.
  console.warn(
    `[web search] Skipping the web-search stack: the Web Search Tool connector ` +
      `is not available in ${region}. Supported: ${WEB_SEARCH_REGIONS.join(', ')}. ` +
      'The agent deploys without web search.',
  );
}

// --- 2. Governance -----------------------------------------------------------
const governanceStack = new GovernanceStack(app, `AgentCoreGovernanceSample${suffixPart}`, {
  env,
  config,
  description:
    'Sample: per-user governance for Amazon Bedrock AgentCore Gateway (sandbox only)',
});

// --- 3. Runtime --------------------------------------------------------------
// Needs the pool this stack creates. With an external OIDC issuer there is no
// pool, so the runtime is left to the operator along with their identities.
const runtimeRequested = app.node.tryGetContext('agentRuntime') !== 'false';
if (runtimeRequested && governanceStack.governance.userPool && governanceStack.governance.userPoolClient) {
  const runtimeStack = new RuntimeStack(app, `AgentCoreGovernanceRuntime${suffixPart}`, {
    env,
    config,
    nameSuffix,
    gatewayUrl: governanceStack.governance.gatewayUrl,
    userPoolId: governanceStack.governance.userPool.userPoolId,
    appClientId: governanceStack.governance.userPoolClient.userPoolClientId,
    webSearch: webSearchStack?.webSearch,
    description:
      'AgentCore Runtime hosting the Strands orchestrator + web-search sub-agent (sandbox only)',
  });
  // Explicit, beyond the implicit dependency the token references already
  // create: the runtime's authorizer is useless until the pool and gateway
  // exist, and CloudFormation should not start it earlier.
  runtimeStack.addStackDependency(
    governanceStack,
    'the runtime forwards end-user tokens to the governance gateway and validates them against its Cognito pool',
  );
  if (webSearchStack) {
    runtimeStack.addStackDependency(
      webSearchStack,
      "the runtime's web-search sub-agent calls the web-search gateway",
    );
  }
  applyRuntimeNagSuppressions(runtimeStack);
} else if (runtimeRequested) {
  console.warn(
    '[runtime] Skipping the agent-runtime stack: it needs the Cognito pool this ' +
      'app creates, and an external OIDC issuer is configured.',
  );
}

// cdk-nag runs on every synth and deploy, so a rule regression fails the build
// instead of being found in review. Every remaining finding is suppressed
// explicitly with a reason in lib/nag-suppressions.ts; the goal state is zero
// unsuppressed errors and zero unsuppressed warnings.
applyNagSuppressions(governanceStack);
Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
