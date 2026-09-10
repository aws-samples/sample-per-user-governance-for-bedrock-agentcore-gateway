// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { CfnOutput, Stack, StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { GovernanceGateway } from './governance-gateway';

/**
 * Thin stack around the GovernanceGateway construct with the default
 * everything-created configuration: new gateway, new Cognito user pool,
 * bedrock-mantle inference target, interceptor, and ledger table.
 *
 * The five outputs below are the plug-in contract for clients and operators.
 */
export class GovernanceStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    // Deployment-wide governance semantics, settable per deploy without
    // editing code: -c budgetWindow=week -c activeHours=09:00-17:00
    // -c activeHoursTz=America/New_York. Defaults reproduce the original
    // behavior (daily budget, no hours gate).
    const budgetWindow = this.node.tryGetContext('budgetWindow') as
      | 'hour' | 'day' | 'week' | 'month' | undefined;
    const activeHours = this.node.tryGetContext('activeHours') as string | undefined;
    const activeHoursTz = this.node.tryGetContext('activeHoursTz') as string | undefined;
    // Set by the deploy script when the account already has Bedrock
    // invocation logging configured: reuse that log group (attach only the
    // subscription filter) instead of creating one and redirecting the
    // account-level configuration.
    const existingInvocationLogGroupName = this.node.tryGetContext(
      'existingInvocationLogGroupName',
    ) as string | undefined;
    // Also set by the deploy script, when that existing configuration turns out
    // to be one a previous run of this stack created: import the group but keep
    // owning the delivery role, so a redeploy cannot delete the role the live
    // account-level configuration still names.
    const ownLoggingRoleForExistingGroup =
      this.node.tryGetContext('ownLoggingRoleForExistingGroup') === 'true' ||
      this.node.tryGetContext('ownLoggingRoleForExistingGroup') === true;
    // Downgrade targets for the users who have no POLICY item yet. One per
    // door and wire shape, because each rejects the others' id form with a
    // 400: the passthrough door takes the regional inference-profile form,
    // the mantle door's Anthropic Messages shape takes the bare provider id,
    // and its OpenAI shapes serve a disjoint OSS catalog. Override per deploy
    // with -c mantleFallbackModel=... -c openaiFallbackModel=...
    const mantleFallbackModel =
      (this.node.tryGetContext('mantleFallbackModel') as string | undefined) ??
      'anthropic.claude-haiku-4-5';
    const openaiFallbackModel =
      (this.node.tryGetContext('openaiFallbackModel') as string | undefined) ??
      'gpt-oss-20b';
    // Input screening with an existing Bedrock guardrail:
    //   -c guardrailId=abcd1234wxyz -c guardrailVersion=1
    // Both are required together; either one alone is a deploy-time error
    // rather than a silently disabled guardrail. Without them the interceptor
    // applies no content screening, which is the default.
    const guardrailId = this.node.tryGetContext('guardrailId') as string | undefined;
    const guardrailVersion = this.node.tryGetContext('guardrailVersion') as
      | string
      | undefined;
    if (Boolean(guardrailId) !== Boolean(guardrailVersion)) {
      throw new Error(
        'guardrailId and guardrailVersion must be set together: pass -c guardrailId=<id> -c guardrailVersion=<version>',
      );
    }
    // Gateway names are unique per account and region, so a second copy of
    // this stack needs its own. bin/app.ts validates the suffix and applies
    // the same one to the stack name.
    const nameSuffix = this.node.tryGetContext('nameSuffix') as string | undefined;

    const governance = new GovernanceGateway(this, 'Governance', {
      gatewayName: nameSuffix ? `per-user-governance-${nameSuffix}` : undefined,
      budgetWindow,
      activeHours,
      activeHoursTz,
      existingInvocationLogGroupName,
      ownLoggingRoleForExistingGroup,
      mantleFallbackModel,
      openaiFallbackModel,
      guardrail:
        guardrailId && guardrailVersion
          ? { id: guardrailId, version: guardrailVersion }
          : undefined,
    });

    new CfnOutput(this, 'GatewayUrl', {
      value: governance.gatewayUrl,
      description:
        'Base URL of the gateway. Inference clients use <GatewayUrl>/inference; native-SDK clients use <GatewayUrl>/bedrock-runtime/model/{modelId}/invoke or /converse',
    });
    new CfnOutput(this, 'UserPoolId', {
      value: governance.userPool?.userPoolId ?? 'n/a (external OIDC)',
      description: 'Cognito user pool that issues user JWTs',
    });
    new CfnOutput(this, 'AppClientId', {
      value: governance.userPoolClient?.userPoolClientId ?? 'n/a (external OIDC)',
      description: 'Cognito app client for admin-initiate-auth',
    });
    new CfnOutput(this, 'TableName', {
      value: governance.table.tableName,
      description: 'DynamoDB ledger table holding POLICY, USAGE, REQ, and EVENT items',
    });
    new CfnOutput(this, 'InterceptorLogGroup', {
      value: governance.logGroup.logGroupName,
      description: 'CloudWatch log group with the interceptor decision log',
    });
    new CfnOutput(this, 'InvocationLogGroupName', {
      value: governance.invocationLogGroupName ?? 'n/a (existing gateway)',
      description:
        'CloudWatch log group Bedrock invocation logs must be delivered to (point account-level logging here)',
    });
    new CfnOutput(this, 'BedrockLoggingRoleArn', {
      value: governance.bedrockLoggingRoleArn ?? 'n/a (reusing existing logging configuration)',
      description:
        'IAM role ARN the account-level Bedrock invocation logging configuration must reference',
    });
  }
}
