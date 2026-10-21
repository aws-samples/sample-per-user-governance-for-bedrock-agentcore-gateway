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
 *   - AwsSolutions-IAM5 Resource::* on that same bedrock-mantle statement: the
 *     resource is now a project ARN in this account and region. Only the
 *     project id stays wildcarded.
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

  // Phase 8: hosted OpenAI GPT (gpt-5.6-*, gpt-6-*) reachable only via their
  // us.openai.* cross-region inference profile through the Converse passthrough
  // door. Same rationale/bounds as the Claude wildcards.
  const openaiModelArns = [
    'Resource::arn:<AWS::Partition>:bedrock:*::foundation-model/openai.gpt-*',
    'Resource::arn:<AWS::Partition>:bedrock:*:<AWS::AccountId>:inference-profile/us.openai.gpt-*',
  ];
  const openaiModelReason = [
    'Resource wildcards are the model-name segment of Bedrock model ARNs, scoped to the OpenAI GPT family in this account.',
    'Hosted GPT-5.6/gpt-6 have no on-demand throughput and are invocable only via their us.openai.* cross-region inference profile; the family gains new dated ids/profiles per release, so exact ids cannot be enumerated at synth time.',
    'What still bounds this: only two Bedrock inference actions are granted, only the OpenAI GPT family is reachable, cross-account access is impossible because the account id is fixed on the profile ARN, and the effective per-request allowlist remains the interceptor allowed_models policy enforced before any model is called.',
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
      { id: 'AwsSolutions-IAM5', reason: openaiModelReason, appliesTo: openaiModelArns },
      {
        id: 'AwsSolutions-IAM5',
        reason: [
          'The bedrock-mantle statement enumerates only read and inference actions. Everything else in that namespace administers it or mutates something that outlives a request, and none of it is granted; because the list is explicit, actions the service adds later stay ungranted until someone adds them here.',
          'The resource is a project ARN in this account and this region, not a wildcard across the namespace. An earlier version of this suppression claimed the granted actions had no account-scoped ARN to name. That was wrong: the IAM service authorization reference for bedrock-mantle lists project as a required resource type for all nine actions granted here, including ListModels and GetModel, which read the model catalog within a project. See https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock-mantle.html',
          'The only wildcard left is the project id, and it cannot be narrowed at synthesis time. The per-user project is opt-in (-c mantleProject=true) and the service generates its id during deployment, while a request that names no project resolves to the account default project. Both have to be reachable, and neither id is known when this policy is written.',
          'What still bounds this: the role is assumable only by bedrock-agentcore.amazonaws.com, only from this account, and only for a gateway whose ARN matches the per-user-governance prefix; the statement cannot reach projects in another account or another region; and the only objects any granted action can create, cancel, or delete are single inferences the same call brought into being, so nothing that predates the request is mutable through this statement.',
        ].join(' '),
        appliesTo: [
          'Resource::arn:<AWS::Partition>:bedrock-mantle:<AWS::Region>:<AWS::AccountId>:project/*',
        ],
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

  // --- Phase-7 provisioning brought under CloudFormation -------------------
  // Four custom resources replaced scripts that ran with the deployer's own
  // credentials, which were almost always administrator. Each role below is
  // narrower than what the script it replaced actually used; the wildcards
  // that remain are ones the relevant API cannot express more tightly.

  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `${prefix}/InvocationLogging/HandlerRole/DefaultPolicy/Resource`,
    [
      {
        id: 'AwsSolutions-IAM5',
        reason: [
          'The three account-level Bedrock invocation-logging actions take no resource at all: Get/Put/DeleteModelInvocationLoggingConfiguration are authorized against the account, because the configuration is a per-account per-region singleton rather than an addressable resource. Resource "*" is the only form that authorizes the call.',
          'What still bounds this: the role holds nothing but those three actions plus writes to its own log group, and the handler refuses in code to overwrite or delete a configuration it does not recognise as this stack own, so even full access to the API cannot take over another workload logging.',
        ].join(' '),
        appliesTo: ['Resource::*'],
      },
    ],
  );

  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `${prefix}/IdentitySeed/HandlerRole/DefaultPolicy/Resource`,
    [
      {
        id: 'AwsSolutions-IAM5',
        reason: [
          'bedrock:ListFoundationModels has no resource types in the Bedrock service authorization reference, so Resource must be "*" for the call to be authorized.',
          'It is a read of the account model catalog and the only Bedrock action this role holds. The seed needs it because group entitlement is resolved against the live ACTIVE catalog rather than a checked-in model list, which is the entire point of the family globs in config.yaml.',
          'Every other grant on this role is resource-scoped: the Cognito admin actions name this stack user pool, and the DynamoDB writes name this stack ledger table.',
        ].join(' '),
        appliesTo: ['Resource::*'],
      },
    ],
  );

  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `${prefix}/IdentitySeed/DemoPassword/Resource`,
    [
      {
        id: 'AwsSolutions-SMG4',
        reason: [
          'Rotation is deliberately off. This is one shared password for 41 seeded demo identities, and the value is only meaningful while it matches what Cognito holds for those users. Rotating the secret would change the stored value without changing the Cognito passwords, so the next login would fail and the secret would be worse than useless - it would be actively misleading.',
          'Keeping them in step would need a rotation Lambda that also calls AdminSetUserPassword for every seeded user, which is machinery for a demo credential that intentionally has no production role.',
          'What still bounds this: the secret is generated by CloudFormation and never printed, typed into a shell, or committed; it grants nothing beyond signing in as a demo user whose own spend is capped by the governance plane; and the deployment documentation states these identities are not for production use.',
        ].join(' '),
      },
    ],
  );

  // The mantle project is opt-in (-c mantleProject=true) and absent from a
  // default deploy, because per-user attribution no longer needs one. cdk-nag
  // throws on a suppression whose path matches nothing, so this is applied only
  // when the construct is actually present.
  if (stack.node.tryFindChild('MantleProject') !== undefined) {
    NagSuppressions.addResourceSuppressionsByPath(
      stack,
      `${prefix}/MantleProject/HandlerRole/DefaultPolicy/Resource`,
      [
        {
          id: 'AwsSolutions-IAM5',
          reason: [
            'The wildcard is the project-id segment of the bedrock-mantle project ARN. CreateProject is authorized against a project resource even though the project does not exist until the call returns, so there is no id to name at synth time.',
            'What still bounds this: the ARN fixes the partition, region and account, so no project outside this account is reachable; the actions are CreateProject, GetProject and ListProjects only, with no update, archive, tag or inference action; and the handler creates at most one project and reuses the existing id on every later deploy.',
          ].join(' '),
          appliesTo: [
            'Resource::arn:<AWS::Partition>:bedrock-mantle:<AWS::Region>:<AWS::AccountId>:project/*',
          ],
        },
      ],
    );
  }
}

/**
 * Suppressions for the web-search gateway stack.
 *
 * The one wildcard here is unavoidable by construction: the gateway's service
 * role has to exist before the gateway, because `CreateGateway` takes the role
 * ARN as an input, so the role cannot name the gateway it serves.
 */
export function applyWebSearchNagSuppressions(stack: Stack): void {
  const prefix = `/${stack.node.id}`;
  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `${prefix}/WebSearch/ServiceRole/DefaultPolicy/Resource`,
    [
      {
        id: 'AwsSolutions-IAM5',
        reason: [
          'The wildcard is the gateway-id segment of a gateway ARN in this account and region. It cannot be narrowed to the gateway this role serves because the dependency runs the other way: CreateGateway consumes the role ARN, so the role is created first and the gateway id does not exist yet.',
          'What still bounds this: the ARN fixes partition, region and account so no gateway outside this account is reachable; the role carries exactly two actions, InvokeGateway and InvokeWebSearch; the trust policy lets only bedrock-agentcore.amazonaws.com assume it, only from this account, and only for a gateway ARN (aws:SourceArn), so a different service or account cannot borrow it; and the AWS documentation for the Web Search Tool connector specifies this exact pair of statements for the gateway service role.',
        ].join(' '),
        appliesTo: [
          'Resource::arn:<AWS::Partition>:bedrock-agentcore:<AWS::Region>:<AWS::AccountId>:gateway/*',
        ],
      },
    ],
  );
}

/**
 * Suppressions for the agent-runtime stack.
 *
 * All of these sit on one role, and all of them are shapes AgentCore and the
 * CDK asset helper impose rather than choices made here.
 */
export function applyRuntimeNagSuppressions(stack: Stack): void {
  const prefix = `/${stack.node.id}`;
  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `${prefix}/Frameworks/ExecutionRole/DefaultPolicy/Resource`,
    [
      {
        id: 'AwsSolutions-IAM5',
        reason: [
          'The wildcards are the log-group and log-stream segments of the AgentCore managed log namespace. AgentCore creates a log group per runtime under /aws/bedrock-agentcore/ and a new stream per invocation, so neither name exists at synth time. DescribeLogGroups additionally has no resource-level filter in CloudWatch Logs - it enumerates by prefix - so it must be granted on log-group:*.',
          'What still bounds this: the grants are confined to the /aws/bedrock-agentcore/ prefix apart from the enumerate-only DescribeLogGroups, the actions are create and write only with no read of log content, and this role holds no Bedrock permission at all because the agent reaches models through the governance gateway carrying the end user JWT.',
        ].join(' '),
        appliesTo: [
          'Resource::arn:<AWS::Partition>:logs:<AWS::Region>:<AWS::AccountId>:log-group:/aws/bedrock-agentcore/*',
          'Resource::arn:<AWS::Partition>:logs:<AWS::Region>:<AWS::AccountId>:log-group:*',
          'Resource::arn:<AWS::Partition>:logs:<AWS::Region>:<AWS::AccountId>:log-group:/aws/bedrock-agentcore/*:log-stream:*',
        ],
      },
      {
        id: 'AwsSolutions-IAM5',
        reason: [
          'These are the action and resource patterns emitted by the aws-cdk-lib asset grantRead() helper, which is how a CodeZip runtime is given permission to read its own deployment artifact. The expansions (s3:GetObject*, s3:GetBucket*, s3:List*) and the object-key wildcard on the bootstrap assets bucket are chosen by the CDK, not by this construct.',
          'The permission itself is mandatory: an AgentCore CodeZip runtime cannot start without reading its artifact, which is why this grant is an addition over the checked-in permissions.json template - that file was written for the container path and grants no S3 access at all.',
          'What still bounds this: access is confined to the CDK bootstrap assets bucket for this account and region, the actions are read-only with no Put or Delete, and the role is assumable only by bedrock-agentcore.amazonaws.com from this account.',
        ].join(' '),
        appliesTo: [
          'Action::s3:GetObject*',
          'Action::s3:GetBucket*',
          'Action::s3:List*',
          `Resource::arn:aws:s3:::cdk-hnb659fds-assets-${stack.account}-${stack.region}/*`,
        ],
      },
    ],
  );
}
