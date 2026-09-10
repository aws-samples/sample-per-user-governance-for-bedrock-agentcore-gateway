// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { Stack } from 'aws-cdk-lib';
import { NagSuppressions } from 'cdk-nag';

/**
 * Every cdk-nag finding this stack does not fix, with the reason it is not
 * fixed. bin/app.ts applies AwsSolutionsChecks on every synth, so the goal
 * state is zero unsuppressed errors and zero unsuppressed warnings: a new
 * finding fails the build instead of surfacing in review.
 *
 * The rule of thumb used here: a finding is FIXED whenever the fix is a
 * configuration change that keeps the sample working. That is why this file is
 * short. Fixed rather than suppressed:
 *   - AwsSolutions-L1: all functions run the newest Python runtime aws-cdk-lib
 *     knows about, and `npm run check` asserts that against the synthesized
 *     template so a partial bump cannot pass.
 *   - AwsSolutions-COG8: the user pool is on the Plus feature plan.
 *   - AwsSolutions-IAM4 on the attribution Lambda: it has an explicit role
 *     scoped to its own log group instead of the default managed policy.
 *   - AwsSolutions-IAM5 Action::bedrock-mantle:*: the wildcard action is now an
 *     enumerated list of nine read and inference actions.
 *   - iam:PassRole on Resource '*' in the attach-to-existing-gateway path: the
 *     deployer now supplies the gateway role ARN, so the grant names one role.
 *
 * Each entry below records what the wildcard is, why it cannot be narrowed,
 * and what still bounds the blast radius. `appliesTo` pins the suppression to
 * the exact action or resource string, so an unrelated wildcard added later to
 * the same policy is still reported.
 */
export function applyNagSuppressions(stack: Stack): void {
  // Paths are rooted at the stack's construct id, which -c nameSuffix
  // changes, so the prefix is derived rather than written out.
  const prefix = `/${stack.node.id}`;
  const claudeModelArns = [
    'Resource::arn:<AWS::Partition>:bedrock:*::foundation-model/anthropic.claude-*',
    'Resource::arn:<AWS::Partition>:bedrock:*:<AWS::AccountId>:inference-profile/us.anthropic.claude-*',
    'Resource::arn:<AWS::Partition>:bedrock:*:<AWS::AccountId>:inference-profile/global.anthropic.claude-*',
  ];
  const claudeModelReason = [
    'Resource wildcards are the model-name segment of Bedrock model ARNs, deliberately scoped to the Anthropic Claude family in this account.',
    'They cannot be enumerated: Bedrock publishes a new dated snapshot id and a new cross-region inference profile for every Claude release, and an operator sets each persona POLICY item and each per-shape downgrade target to whichever of those the gateway serves.',
    'Pinning exact ids turns a legitimate downgrade into an AccessDenied and then a budget refusal for the end user.',
    'What still bounds this: only two Bedrock inference actions are granted, only the Anthropic Claude family is reachable, cross-account model access is impossible for inference profiles because the account id is fixed, and the effective per-request allowlist is the interceptor allowed_models policy, which is data an operator controls and which is enforced before any model is called.',
  ].join(' ');

  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `${prefix}/Governance/InterceptorRole/DefaultPolicy/Resource`,
    [{ id: 'AwsSolutions-IAM5', reason: claudeModelReason, appliesTo: claudeModelArns }],
  );
  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `${prefix}/Governance/GatewayServiceRole/DefaultPolicy/Resource`,
    [
      { id: 'AwsSolutions-IAM5', reason: claudeModelReason, appliesTo: claudeModelArns },
      {
        id: 'AwsSolutions-IAM5',
        reason: [
          'The bedrock-mantle statement enumerates only read and inference actions. Everything else in that namespace administers it or mutates something that outlives a request, and none of it is granted; because the list is explicit, actions the service adds later stay ungranted until someone adds them here. The granted actions have no account-scoped resource ARN to name.',
          'The mantle model catalog is service-owned, so ListModels and GetModel are authorized against resources this account does not hold, and the inference actions act on an inference that does not exist until the call creates it.',
          'What still bounds this: the role is assumable only by bedrock-agentcore.amazonaws.com, only from this account, and only for a gateway whose ARN matches the per-user-governance prefix; and the only objects any granted action can create, cancel, or delete are single inferences the same call brought into being, so nothing that predates the request is mutable through this statement.',
        ].join(' '),
        appliesTo: ['Resource::*'],
      },
    ],
  );
  // Only in the adopt path, where the stack owns the Bedrock delivery role but
  // imports the log group it writes to. When the stack creates the group, the
  // grant renders as a GetAtt and no literal wildcard reaches the rule; an
  // imported group resolves to a literal ARN whose trailing segment is a
  // wildcard, so the same grant becomes a finding.
  const adoptedLogGroupName = stack.node.tryGetContext('existingInvocationLogGroupName') as
    | string
    | undefined;
  const ownsRoleForAdoptedGroup =
    stack.node.tryGetContext('ownLoggingRoleForExistingGroup') === 'true' ||
    stack.node.tryGetContext('ownLoggingRoleForExistingGroup') === true;
  if (adoptedLogGroupName && ownsRoleForAdoptedGroup) {
    NagSuppressions.addResourceSuppressionsByPath(
      stack,
      `${prefix}/Governance/BedrockLoggingRole/DefaultPolicy/Resource`,
      [
        {
          id: 'AwsSolutions-IAM5',
          reason: [
            'The wildcard is the log-stream segment of one named log group ARN. CloudWatch Logs authorizes PutLogEvents against log-group:<name>:log-stream:<stream> and Bedrock creates a new stream per delivery, so the stream segment cannot be enumerated at synth time.',
            'What still bounds this: the grant names the single log group the account-level invocation logging configuration delivers to, and carries only logs:CreateLogStream and logs:PutLogEvents. It is the same grant the stack makes when it creates that group itself; only the ARN form differs, because an imported group resolves to a literal string.',
          ].join(' '),
          appliesTo: [
            `Resource::arn:<AWS::Partition>:logs:<AWS::Region>:<AWS::AccountId>:log-group:${adoptedLogGroupName}:*`,
          ],
        },
      ],
    );
  }

  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `${prefix}/Governance/AttributionLambdaRole/DefaultPolicy/Resource`,
    [
      {
        id: 'AwsSolutions-IAM5',
        reason: [
          'cloudwatch:GetMetricStatistics has no resource types in the CloudWatch service authorization reference, so Resource must be "*" for the call to be authorized at all.',
          'It is a read of metric aggregates and the only CloudWatch action this role holds; the metrics read are the AWS/BedrockMantle per-project token counters the attribution path reconciles into the ledger.',
        ].join(' '),
        appliesTo: ['Resource::*'],
      },
    ],
  );

  const gatewayTargetReason = [
    'The wildcard is the sub-resource segment of this stack own gateway ARN, needed because target operations are authorized against arn:<gateway>/target/<id> and the control plane assigns the target id at create time, after synth.',
    'The grant names the gateway ARN this stack creates, so it reaches no other gateway, and the actions are the target create, read, update, delete set used by the custom resources that install this stack own gateway targets (the bedrock-mantle inference target and the bedrock-runtime passthrough target).',
  ].join(' ');
  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `${prefix}/Governance/InferenceTarget/CustomResourcePolicy/Resource`,
    [
      {
        id: 'AwsSolutions-IAM5',
        reason: gatewayTargetReason,
        appliesTo: ['Resource::<GovernanceGatewayC909DD5D.GatewayArn>/*'],
      },
    ],
  );
  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `${prefix}/Governance/PassthroughHandlerRole/DefaultPolicy/Resource`,
    [
      {
        id: 'AwsSolutions-IAM5',
        reason: gatewayTargetReason,
        appliesTo: ['Resource::<GovernanceGatewayC909DD5D.GatewayArn>/*'],
      },
    ],
  );

  // The AwsCustomResource provider function, created and owned by aws-cdk-lib
  // (the construct id is a hash of the provider's code). Its role and the
  // managed policy on it are not configurable from here; the only way to avoid
  // the finding would be to hand-write a Lambda-backed custom resource in place
  // of AwsCustomResource, which would add more code than it removes risk.
  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `${prefix}/AWS679f53fac002430cb0da5b7982bd2287/ServiceRole/Resource`,
    [
      {
        id: 'AwsSolutions-IAM4',
        reason:
          'Role and managed policy are generated by the aws-cdk-lib AwsCustomResource provider framework and cannot be replaced through construct props. AWSLambdaBasicExecutionRole grants CloudWatch Logs write only; the provider policy granting the actual API calls is separate and is scoped by this stack.',
        appliesTo: [
          'Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole',
        ],
      },
    ],
  );

  // Requiring MFA is the one Cognito control this sample cannot adopt. Every
  // token in the demo comes from admin-initiate-auth in a script or in the demo
  // app backend; with MFA required, that call returns an MFA challenge instead
  // of tokens and there is no second factor to answer it with, because the
  // pool has no hosted UI and the users are created non-interactively.
  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `${prefix}/Governance/UserPool/Resource`,
    [
      {
        id: 'AwsSolutions-COG2',
        reason:
          'The pool exists to issue JWTs to non-interactive demo clients through admin-initiate-auth, which cannot satisfy an MFA challenge. Sign-up is disabled, there is no hosted UI or user pool domain, the pool is on the Plus feature plan, and the README states that a real deployment should bring its own OIDC issuer through the oidc prop instead of using this pool. See the Security and responsible AI and Known limitations sections of the README.',
      },
    ],
  );
}
