// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { CfnOutput, Stack, StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { AcvConfig, loadConfig } from './config';
import { GovernanceGateway, LAMBDA_RUNTIME } from './governance-gateway';
import { GovernanceGuardrail } from './platform/guardrail';
import { IdentitySeed } from './platform/identity-seed';
import { InvocationLogging, ModeChoice } from './platform/invocation-logging';
import { MantleProject } from './platform/mantle-project';
import { GovernanceMemory } from './platform/memory';

export interface GovernanceStackProps extends StackProps {
  /**
   * Parsed `config/config.yaml`.
   *
   * @default - loaded from the repository copy
   */
  readonly config?: AcvConfig;
}

/**
 * The governance plane: gateway, ledger, interceptor, Cognito, guardrail,
 * memory, invocation logging, and the demo identities.
 *
 * Creating the guardrail out of band would take three passes — `cdk deploy`,
 * then an SDK call to create the guardrail and read its id back out, then
 * `cdk deploy` *again* with `-c guardrailId=... -c guardrailVersion=...` so the
 * interceptor could be told about it. The guardrail is created in this stack
 * instead, so its id and version are ordinary CloudFormation tokens and that
 * circular bootstrap does not arise: one `cdk deploy` produces a gateway that
 * governs, screens content, seeds its users, and debits their ledger.
 *
 * Resources this stack declares:
 * guardrail + version, AgentCore Memory, the account's Bedrock invocation
 * logging configuration, 41 demo identities with their `POLICY#`/`GROUP#`
 * ledger items, and the bedrock-mantle project.
 */
export class GovernanceStack extends Stack {
  /** The gateway construct. */
  public readonly governance: GovernanceGateway;
  /**
   * Input-screening guardrail created by this stack.
   *
   * Absent when `-c guardrailId`/`-c guardrailVersion` point the interceptor at
   * a guardrail managed elsewhere.
   */
  public readonly guardrail?: GovernanceGuardrail;
  /** Per-user conversation memory. */
  public readonly memory: GovernanceMemory;
  /** Demo identities and their ledger policies. Absent when using external OIDC. */
  public readonly identitySeed?: IdentitySeed;
  /** Parsed configuration, so dependent stacks reuse the same values. */
  public readonly config: AcvConfig;

  constructor(scope: Construct, id: string, props: GovernanceStackProps = {}) {
    super(scope, id, props);

    const config = props.config ?? loadConfig();
    this.config = config;

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
    // The gateway name and the saved Logs Insights query names are unique per
    // account and region, so a second copy of this stack needs its own.
    // bin/app.ts validates the suffix and applies the same one to the stack
    // name.
    const nameSuffix = this.node.tryGetContext('nameSuffix') as string | undefined;
    // What to do if the account's single Bedrock invocation-logging slot is
    // already taken: -c invocationLoggingMode=force to overwrite.
    const invocationLoggingMode =
      (this.node.tryGetContext('invocationLoggingMode') as ModeChoice | undefined) ??
      (ownLoggingRoleForExistingGroup ? 'adopt' : 'reuse');
    // Opt out of the demo user population with -c seedIdentities=false, for a
    // deployment that brings its own users.
    const seedIdentities = this.node.tryGetContext('seedIdentities') !== 'false';
    // A bedrock-mantle project per user is OFF by default, and a deployment
    // should not turn it on without reading the next paragraph.
    //
    // It used to be created automatically, because the mantle door is the only
    // way to attribute usage there: Bedrock writes model invocation logs only
    // for the bedrock-runtime endpoint, and the mantle door's finest billing
    // grain is per-project-per-day. Per-user metering therefore needed one
    // project each — which does not survive an enterprise user base, since
    // projects are a limited per-account resource and would have to be created
    // for every user in every account.
    //
    // Per-user attribution now comes from per-request metadata on the
    // bedrock-runtime door instead, which needs no project at all and settles
    // exactly rather than as a five-minute aggregate. Users are entitled on
    // that door automatically (see IdentitySeed: the entitlement carries each
    // model's `us.`/`global.` inference-profile form whenever one exists).
    //
    // Turn this on with -c mantleProject=true only for a single-user or demo
    // deployment that specifically wants mantle-door billing records. Nothing
    // requires it, and an operator can still set workspace_id on a POLICY#
    // item by hand: the interceptor and the reconciliation path both still
    // honour one if it is present.
    const createMantleProject = this.node.tryGetContext('mantleProject') === 'true';

    // --- Input screening -----------------------------------------------------
    // By default the guardrail is created here, before the gateway, so its id
    // and version are handed straight to the interceptor as tokens. That is
    // what removes the old three-pass deploy.
    //
    // The escape hatch is kept: pass -c guardrailId=<id> -c guardrailVersion=<v>
    // to screen with a guardrail this stack does not own, for an organisation
    // that manages guardrails centrally. Both are required together, because a
    // half-configured guardrail would silently screen nothing.
    const externalGuardrailId = this.node.tryGetContext('guardrailId') as string | undefined;
    const externalGuardrailVersion = this.node.tryGetContext('guardrailVersion') as
      | string
      | undefined;
    if (Boolean(externalGuardrailId) !== Boolean(externalGuardrailVersion)) {
      throw new Error(
        'guardrailId and guardrailVersion must be set together: pass -c guardrailId=<id> -c guardrailVersion=<version>',
      );
    }
    const useExternalGuardrail = Boolean(externalGuardrailId && externalGuardrailVersion);

    if (!useExternalGuardrail) {
      this.guardrail = new GovernanceGuardrail(this, 'Guardrail', {
        config: config.guardrail,
        nameSuffix,
      });
    }
    const guardrail = useExternalGuardrail
      ? { id: externalGuardrailId!, version: externalGuardrailVersion! }
      : { id: this.guardrail!.guardrailId, version: this.guardrail!.guardrailVersion };

    // --- Per-user memory -----------------------------------------------------
    // Consumed by the app server, not the interceptor, so nothing in this stack
    // depends on it; it is here so that it is created and destroyed with
    // everything else.
    this.memory = new GovernanceMemory(this, 'Memory', {
      config: config.memory,
      nameSuffix,
    });

    this.governance = new GovernanceGateway(this, 'Governance', {
      nameSuffix,
      budgetWindow,
      activeHours,
      activeHoursTz,
      existingInvocationLogGroupName,
      ownLoggingRoleForExistingGroup,
      mantleFallbackModel,
      openaiFallbackModel,
      guardrail,
      // Phase 7: default -c pricingDefaultEstimate=1 (estimate unknown-model
      // prices) so USD thresholds work across the full catalog; -c
      // pricingDefaultEstimate='' restores F3 fail-closed.
      pricingDefaultEstimate:
        (this.node.tryGetContext('pricingDefaultEstimate') as string | undefined) ?? '1',
    });

    // --- Account-level Bedrock invocation logging ----------------------------
    // Without this the gateway governs requests but Bedrock never writes the
    // invocation logs the attribution Lambda reads, so no one's ledger moves.
    // It was previously applied by the deploy script via the CLI.
    new InvocationLogging(this, 'InvocationLogging', {
      logGroup: this.governance.invocationLogGroup,
      roleArn: this.governance.bedrockLoggingRoleArn,
      mode: invocationLoggingMode,
      runtime: LAMBDA_RUNTIME,
    });

    // --- Demo identities and their ledger policies ---------------------------
    // Only when this stack owns the user pool. With an external OIDC issuer
    // there is no pool to create users in, and the operator supplies identities.
    if (seedIdentities && this.governance.userPool) {
      this.identitySeed = new IdentitySeed(this, 'IdentitySeed', {
        userPool: this.governance.userPool,
        table: this.governance.table,
        identity: config.identity,
        costThresholds: config.costThresholds,
        textExcludeSubstrings: config.allowedSet.textExcludeSubstrings,
        runtime: LAMBDA_RUNTIME,
      });
      // The seed writes POLICY# items, so the table and the Cognito groups must
      // exist first. The groups are created inside the gateway construct.
      this.identitySeed.node.addDependency(this.governance.table);

      if (createMantleProject) {
        // Opt-in only (-c mantleProject=true). Writes workspace_id onto the
        // demo user's POLICY# item, so it must run after the seed has created
        // that item. This grants mantle-door billing records for ONE user; it
        // is not a per-user metering strategy and deliberately does not scale
        // to a real user base. See the flag's definition above.
        const mantle = new MantleProject(this, 'MantleProject', {
          table: this.governance.table,
          demoSub: this.identitySeed.demoSub,
          projectName: nameSuffix
            ? `governance-demo-user-${nameSuffix}`
            : 'governance-demo-user',
          runtime: LAMBDA_RUNTIME,
        });
        mantle.node.addDependency(this.identitySeed);
      }
    }

    // --- Outputs: the plug-in contract for clients and operators -------------
    new CfnOutput(this, 'GatewayUrl', {
      value: this.governance.gatewayUrl,
      description:
        'Base URL of the gateway. Inference clients use <GatewayUrl>/inference; native-SDK clients use <GatewayUrl>/bedrock-runtime/model/{modelId}/invoke or /converse',
    });
    new CfnOutput(this, 'UserPoolId', {
      value: this.governance.userPool?.userPoolId ?? 'n/a (external OIDC)',
      description: 'Cognito user pool that issues user JWTs',
    });
    new CfnOutput(this, 'AppClientId', {
      value: this.governance.userPoolClient?.userPoolClientId ?? 'n/a (external OIDC)',
      description: 'Cognito app client for admin-initiate-auth',
    });
    new CfnOutput(this, 'TableName', {
      value: this.governance.table.tableName,
      description: 'DynamoDB ledger table holding POLICY, USAGE, REQ, and EVENT items',
    });
    new CfnOutput(this, 'InterceptorLogGroup', {
      value: this.governance.logGroup.logGroupName,
      description: 'CloudWatch log group with the interceptor decision log',
    });
    // CP-16 vended gateway observability (REQ-ADMIN-015A/015B): the
    // /aws/vendedlogs/ group the gateway delivers its OWN OTEL structured logs
    // to. Absent when observability is disabled (-c observability=false) or an
    // existing gateway is adopted. See infra/OBSERVABILITY.md.
    new CfnOutput(this, 'GatewayVendedLogGroup', {
      value: this.governance.vendedLogGroupName ?? 'n/a (observability disabled or existing gateway)',
      description:
        'CloudWatch vended-logs group for the gateway OTEL logs (CP-16); traces go to X-Ray/Transaction Search (aws/spans)',
    });
    new CfnOutput(this, 'InvocationLogGroupName', {
      value: this.governance.invocationLogGroupName ?? 'n/a (existing gateway)',
      description:
        'CloudWatch log group Bedrock invocation logs are delivered to',
    });
    new CfnOutput(this, 'BedrockLoggingRoleArn', {
      value: this.governance.bedrockLoggingRoleArn ?? 'n/a (reusing existing logging configuration)',
      description:
        'IAM role ARN the account-level Bedrock invocation logging configuration references',
    });
    // Values a client application needs in order to talk to this deployment.
    // Emitted as stack outputs so `cdk deploy --outputs-file` is enough to
    // configure one, with no out-of-band lookups.
    new CfnOutput(this, 'BillingCycleStartDay', {
      value: String(config.quota.billingCycleStartDayOfMonth),
      description: 'Day of month the USD billing cycle the UI reports against starts on',
    });
    new CfnOutput(this, 'SessionTagKey', {
      value: config.sessionTags.metadataKey,
      description: 'requestMetadata key used for business-unit cost attribution',
    });
    // The InvocationLogging construct already publishes its own outcome as the
    // `InvocationLoggingAction` output; re-exporting it here would put the same
    // string in the stack twice under two names.
  }
}
