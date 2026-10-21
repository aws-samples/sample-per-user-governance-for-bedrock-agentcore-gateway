// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import * as path from 'node:path';
import {
  Aws,
  CustomResource,
  Duration,
  RemovalPolicy,
  Stack,
} from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as logsDestinations from 'aws-cdk-lib/aws-logs-destinations';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';

/** Name of the inference target this construct creates on the gateway. */
export const TARGET_NAME = 'governance-inference';

/**
 * Fallback model for budget downgrades. Uses the fully-qualified cross-region
 * inference profile ID so it works on both the inference connector (MCP
 * gateway, where the target strips it) and the passthrough gateway (where the
 * interceptor calls Bedrock directly with this ID for path-model downgrades).
 */
export const DEFAULT_FALLBACK_MODEL = 'us.anthropic.claude-haiku-4-5-20251001-v1:0';

/**
 * One runtime for every function in this construct: the newest Python runtime
 * aws-cdk-lib knows about, which is what cdk-nag's AwsSolutions-L1 rule
 * compares against. Lambda itself may already offer a newer one than the
 * installed aws-cdk-lib exposes, so this tracks the CDK's ceiling rather than
 * the service's; L1 is satisfied either way. A CDK upgrade that adds a newer
 * Python causes synth to fail. Update this constant and re-run
 * `npm run check` rather than suppressing the rule: that script asserts every
 * function in the synthesized template is on this runtime, so a partial bump
 * also fails.
 *
 * The handler code is standard library plus the runtime's bundled boto3, with
 * no dependency pinned to a particular Python, so the runtime is swappable.
 */
export const LAMBDA_RUNTIME = lambda.Runtime.PYTHON_3_14;

/** Bring-your-own identity provider. Any OIDC issuer the gateway can validate. */
export interface OidcConfig {
  /** OIDC discovery URL, must end with `/.well-known/openid-configuration`. */
  readonly discoveryUrl: string;
  /** Client id that must appear in incoming JWTs. */
  readonly clientId: string;
}

export interface GovernanceGatewayProps {
  /**
   * Use an existing OIDC identity provider instead of creating a Cognito
   * user pool. The JWT `sub` claim becomes the governance user id.
   * @default - a locked-down Cognito user pool and app client are created
   */
  readonly oidc?: OidcConfig;

  /**
   * Attach the interceptor and ledger table to a gateway you already run
   * instead of creating a new gateway, Cognito pool, and inference target.
   * When set, `fallbackModel` and `existingGatewayRoleArn` are required.
   * @default - a new CUSTOM_JWT gateway with a bedrock-mantle target is created
   */
  readonly existingGatewayId?: string;

  /**
   * Execution role ARN of the gateway named by `existingGatewayId`, readable
   * with `aws bedrock-agentcore-control get-gateway --gateway-identifier <id>
   * --query roleArn`. Attaching the interceptor is a read-merge-write on the
   * gateway, and UpdateGateway echoes the gateway's own roleArn back, so the
   * attach handler needs iam:PassRole for exactly that one role. Requiring the
   * ARN here is what keeps that grant off `Resource: '*'`.
   */
  readonly existingGatewayRoleArn?: string;

  /**
   * Model id users are downgraded to near their budget, in the regional
   * inference-profile form the passthrough door serves, for example
   * `us.anthropic.claude-haiku-4-5-20251001-v1:0`. Target-qualified ids are
   * not accepted: the value is matched against `allowed_models` as written.
   * @default DEFAULT_FALLBACK_MODEL
   */
  readonly fallbackModel?: string;

  /**
   * Downgrade target for the mantle door's Anthropic Messages shape, in that
   * door's bare provider form (for example `anthropic.claude-haiku-4-5`). The
   * two doors accept different id forms and reject each other's with a 400,
   * so a single `fallbackModel` cannot serve both.
   * @default - the mantle door reuses fallbackModel
   */
  readonly mantleFallbackModel?: string;

  /**
   * Downgrade target for the mantle door's OpenAI shapes (Chat Completions and
   * Responses), which serve only OpenAI-family ids (for example
   * `gpt-oss-20b`). Rewriting one of those requests to a Claude id returns a
   * 400 naming /v1/messages instead.
   * @default - OpenAI-shape requests are not downgraded
   */
  readonly openaiFallbackModel?: string;

  /**
   * Token budget per window applied to users without a POLICY item in the
   * table. What "per window" means is set by budgetWindow.
   * @default 100000
   */
  readonly defaultBudgetTokens?: number;

  /**
   * The budget window: what "per" means for every budget_tokens value,
   * deployment-wide. Calendar-aligned in UTC (day resets at midnight UTC,
   * week on Monday, month on the 1st), matching API Gateway usage-plan
   * quota semantics. Changing it on a live stack re-buckets usage: spend
   * accumulated under the old window is forgiven, and budget_tokens values
   * change meaning (500k/day becomes 500k/hour), so rescale budgets when
   * changing this.
   * @default 'day'
   */
  readonly budgetWindow?: 'hour' | 'day' | 'week' | 'month';

  /**
   * Active-hours gate, "HH:MM-HH:MM" in activeHoursTz local time. Requests
   * outside the window are refused at admission with a retry hint; inside
   * it, normal governance applies. Equal start and end (or unset) disables
   * the gate; start later than end wraps overnight (22:00-06:00).
   * @default undefined (always on)
   */
  readonly activeHours?: string;

  /**
   * IANA timezone for activeHours (for example America/New_York); DST is
   * honored automatically. The budget window is always UTC; set this to UTC
   * if the budget reset and the gate need to share a clock.
   * @default 'UTC'
   */
  readonly activeHoursTz?: string;

  /**
   * Budget weight of cache tokens as a percent of the input-token price,
   * applied identically at in-band settle and async attribution. Defaults
   * mirror Anthropic pricing: reads 10, writes 125. Set both to 100 to
   * count cache tokens at full weight, or 0 to exclude them.
   * @default { readPct: 10, writePct: 125 }
   */
  readonly cacheTokenWeights?: { readonly readPct: number; readonly writePct: number };

  /**
   * Ceiling on a request's `max_tokens` / `maxTokens` / `max_output_tokens`.
   * The Anthropic Messages and OpenAI Chat Completions doors REJECT a request
   * above this with 400 `invalid_max_tokens`; only the Responses door clamps.
   *
   * The default is deliberately higher than the interceptor's own 16384
   * fallback because agentic CLI clients ask for far more than a chat client
   * does: Claude Code against the mantle door was rejected outright at 16384
   * with `400 max_tokens must be between 1 and 16384` and retried for 278s
   * before failing. Lower it only if every client is known to ask for less.
   * @default 64000
   */
  readonly maxMaxTokens?: number;

  /**
   * Bedrock guardrail applied to request text at admission (input screening
   * via ApplyGuardrail). Blocked content is refused with the guardrail's
   * message before any model call. Both fields must be set together.
   * @default undefined (no guardrail screening)
   */
  readonly guardrail?: { readonly id: string; readonly version: string };

  /**
   * Phase 7: value for the interceptor's PRICING_DEFAULT_ESTIMATE env var.
   * "1" (or an "in/out/cread/cwrite" micro-USD-per-1k tuple) makes USD quota
   * price models absent from the seed rate sheet at a conservative ESTIMATED
   * rate instead of failing closed with 503 pricing_unavailable — required so
   * per-user USD thresholds enforce across the full "enable ALL models"
   * catalog. Empty (default) preserves the original F3 fail-closed behavior.
   * @default '' (fail closed on unknown models)
   */
  readonly pricingDefaultEstimate?: string;

  /**
   * Per-user requests-per-minute cap applied to users without an explicit
   * rate_limit_per_minute in their POLICY item. Enforced before the budget
   * check, so a burst is turned away cheaply. 0 disables it.
   * @default 0
   */
  readonly defaultRateLimitPerMinute?: number;

  /**
   * Suffix appended to every name this construct picks that AWS scopes to the
   * account and region: the gateway (`per-user-governance-<s>`) and the saved
   * Logs Insights queries (`governance/decisions-per-user-<s>`). A second copy
   * of this construct in the same region must set it, or those creates fail as
   * already-existing. The stack reads it from the -c nameSuffix context value.
   * @default none, names are unsuffixed
   */
  readonly nameSuffix?: string;

  /**
   * CloudWatch log group name that Bedrock model invocation logs land in.
   * The deploy script points the account-level Bedrock invocation logging
   * configuration at this group, so it is exported as a stack output.
   * @default '/bedrock/invocation-logs'
   */
  readonly invocationLogGroupName?: string;

  /**
   * Reuse an EXISTING Bedrock invocation-log group instead of creating one.
   * Bedrock invocation logging is an account-level, per-region singleton;
   * if it is already configured, redirecting it to this stack's group would
   * break whatever consumes the current group. Set this to the log group
   * name from the existing configuration and the stack attaches its
   * attribution subscription filter to that group, creating no log group,
   * no logging role, and changing no account-level settings. The existing
   * configuration's data-delivery flags are irrelevant to attribution: the
   * Lambda reads token counts and requestMetadata and ignores any logged
   * bodies. Mind the CloudWatch Logs subscription-filter limit per log
   * group (2 in most regions; check current CloudWatch Logs quotas) when the
   * group already feeds other consumers.
   * @default - a new log group is created and logging must be enabled on it
   */
  readonly existingInvocationLogGroupName?: string;

  /**
   * Create the Bedrock delivery role even though `existingInvocationLogGroupName`
   * is set, and export its ARN so the deploy script can repoint the
   * account-level configuration at (existing group, this role).
   *
   * Needed when the existing configuration is one a previous run of THIS stack
   * created. Plain reuse assumes the configuration belongs to someone else and
   * so creates no role, which on a redeploy deletes the role the live
   * configuration still names -- Bedrock then silently delivers nothing. Here
   * the group is imported (so a group of that name that the stack no longer
   * owns cannot collide) while the role is owned and rebuilt.
   * @default false - reuse assumes the existing configuration has its own role
   */
  readonly ownLoggingRoleForExistingGroup?: boolean;
}

/**
 * Per-user governance for an Amazon Bedrock AgentCore Gateway.
 *
 * Creates the engine: a REQUEST + RESPONSE Lambda interceptor
 * keyed on the validated JWT subject, a DynamoDB ledger, and (by default) a
 * CUSTOM_JWT gateway fronting Amazon Bedrock through the `bedrock-mantle`
 * inference connector, with a Cognito user pool as the identity provider.
 *
 * Usage is debited on two paths. The RESPONSE interceptor settles the mantle
 * (`/inference`) door in band, which is the only way to meter it per user.
 * The `bedrock-runtime` door is debited asynchronously by an attribution
 * Lambda fed from Bedrock model invocation logs via a subscription filter.
 * Both commit through a conditional put on `EVENT#<request_id>`, so whichever
 * arrives second is a no-op.
 *
 * STREAMING TRADEOFF (measured, us-east-1): registering a RESPONSE
 * interceptor makes the gateway buffer the whole upstream response before
 * relaying it. With REQUEST+RESPONSE, time-to-first-byte equals
 * time-to-last-byte on both doors; with REQUEST only, bytes arrive
 * progressively and match a direct bedrock-runtime call. Responses are still
 * correct and still carry event-stream framing, but they are not delivered
 * incrementally. That matters for interactive CLI clients: Claude Code's
 * gateway protocol requires incremental relay and aborts a turn after five
 * minutes with no bytes, so a single generation long enough to exceed that
 * window will fail. At ~112 output tokens/sec (haiku-4-5, measured) five
 * minutes is roughly 34k output tokens.
 *
 * Enforcement behavior: 403 for blocked users, 429 with
 * `retry_after` in the JSON body when the daily budget is exceeded, model
 * downgrade that rewrites only `model` and preserves streaming, and
 * attribution debits that match the model's reported token counts.
 */
export class GovernanceGateway extends Construct {
  /** Single-table ledger holding POLICY, USAGE, REQ, and EVENT items. */
  public readonly table: dynamodb.TableV2;
  /** The governance interceptor Lambda function. */
  public readonly interceptor: lambda.Function;
  /** Log group receiving the interceptor's structured JSON log lines. */
  public readonly logGroup: logs.LogGroup;
  /** Id of the governed gateway (created or existing). */
  public readonly gatewayId: string;
  /** HTTPS endpoint of the governed gateway. Clients call `<url>/inference`. */
  public readonly gatewayUrl: string;
  /**
   * The single L1 gateway this construct creates. It carries no protocolType,
   * so it accepts both the bedrock-mantle inference target and the
   * bedrock-runtime HTTP passthrough target. Absent when `existingGatewayId`
   * is used.
   */
  public readonly gateway?: agentcore.CfnGateway;
  /** Created Cognito user pool, absent with `oidc` or `existingGatewayId`. */
  public readonly userPool?: cognito.UserPool;
  /** Created Cognito app client, absent with `oidc` or `existingGatewayId`. */
  public readonly userPoolClient?: cognito.UserPoolClient;
  /** Log group Bedrock invocation logs land in (attribution pipeline source). */
  public invocationLogGroup?: logs.LogGroup;
  /** Name of the Bedrock invocation log group (for account-level logging config). */
  public invocationLogGroupName?: string;
  /** Role the account-level Bedrock logging configuration must reference. */
  public bedrockLoggingRole?: iam.Role;
  /** ARN of the role the account-level Bedrock logging configuration references. */
  public bedrockLoggingRoleArn?: string;
  /** '-<nameSuffix>' or '', appended to the account-scoped names we choose. */
  private readonly suffix: string;
  /**
   * Name of the vended-logs CloudWatch group the gateway delivers its
   * APPLICATION_LOGS to (CP-16). Absent when observability is disabled or an
   * existing gateway is adopted (this stack does not own that gateway's
   * delivery wiring). Exported by the stack as a CfnOutput.
   */
  public vendedLogGroupName?: string;

  constructor(scope: Construct, id: string, props: GovernanceGatewayProps = {}) {
    super(scope, id);

    this.suffix = props.nameSuffix ? `-${props.nameSuffix}` : '';

    if (props.existingGatewayId && props.oidc) {
      throw new Error(
        'oidc cannot be combined with existingGatewayId: an existing gateway keeps its own authorizer',
      );
    }
    if (props.existingGatewayId && !props.fallbackModel) {
      throw new Error(
        'fallbackModel is required with existingGatewayId: it must be qualified with a target name that exists on your gateway',
      );
    }
    if (props.existingGatewayId && !props.existingGatewayRoleArn) {
      throw new Error(
        'existingGatewayRoleArn is required with existingGatewayId: the attach handler passes that exact role back to UpdateGateway, and naming it keeps iam:PassRole off a wildcard resource',
      );
    }
    const budgetTokens = props.defaultBudgetTokens ?? 100_000;
    if (!Number.isInteger(budgetTokens) || budgetTokens <= 0) {
      throw new Error('defaultBudgetTokens must be a positive integer');
    }
    const fallbackModel = props.fallbackModel ?? DEFAULT_FALLBACK_MODEL;
    const budgetWindow = props.budgetWindow ?? 'day';
    const activeHours = props.activeHours ?? '';
    if (activeHours && !/^\d{2}:\d{2}-\d{2}:\d{2}$/.test(activeHours)) {
      throw new Error('activeHours must look like HH:MM-HH:MM');
    }
    const activeHoursTz = props.activeHoursTz ?? 'UTC';
    const invocationLogGroupName =
      props.invocationLogGroupName ?? '/bedrock/invocation-logs';
    const stack = Stack.of(this);

    // Ledger: one on-demand table, pk only, TTL on expires_at. USAGE items
    // aggregate per user per day, REQ items hold in-flight request context,
    // EVENT items make settlement idempotent, POLICY items are operator-managed.
    this.table = new dynamodb.TableV2(this, 'Table', {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: 'expires_at',
      billing: dynamodb.Billing.onDemand(),
      // AWS managed encryption is fine for this sample. If you switch to a
      // customer managed key, the interceptor role additionally needs
      // kms:Decrypt, kms:GenerateDataKey, and kms:DescribeKey on that key.
      encryption: dynamodb.TableEncryptionV2.awsManagedKey(),
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      // Sample posture: ledger data is TTL bounded, so the table is removed
      // on destroy. Retain it in production.
      removalPolicy: RemovalPolicy.DESTROY,
    });

    this.logGroup = new logs.LogGroup(this, 'InterceptorLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const interceptorRole = new iam.Role(this, 'InterceptorRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Least-privilege role for the governance interceptor',
    });
    // Least privilege. A TransactWriteItems call is
    // authorized against the per-item actions PutItem, UpdateItem, DeleteItem,
    // and ConditionCheckItem, not just dynamodb:TransactWriteItems. Omitting
    // dynamodb:ConditionCheckItem causes an HTTP 503
    // (AccessDeniedException inside the admission transaction), so keep all six.
    interceptorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'GovernanceTable',
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:ConditionCheckItem',
          'dynamodb:TransactWriteItems',
        ],
        resources: [this.table.tableArn],
      }),
    );
    interceptorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InterceptorLogs',
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        // LogGroup.logGroupArn already ends with :* and so covers log streams.
        resources: [this.logGroup.logGroupArn],
      }),
    );
    // The interceptor calls Bedrock directly when downgrading path-model
    // requests (InvokeModel/Converse format where the model id lives in the
    // URL path and cannot be rewritten by the interceptor). Scoped to the
    // Anthropic Claude family: the interceptor only ever invokes the id the
    // persona's policy names as its downgrade target, and an operator may set
    // that to any model this gateway serves, so a haiku-only grant turns a
    // legitimate downgrade into a 403 and then a 429. Naming the
    // family rather than '*' still keeps a compromised interceptor away from
    // other providers and from non-inference Bedrock APIs.
    interceptorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockDowngrade',
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          // Cross-region profiles route to the foundation model in whichever
          // region serves the call, so the foundation-model ARN stays
          // region-wildcarded alongside the profile ARN.
          `arn:${Aws.PARTITION}:bedrock:*::foundation-model/anthropic.claude-*`,
          `arn:${Aws.PARTITION}:bedrock:*:${Aws.ACCOUNT_ID}:inference-profile/us.anthropic.claude-*`,
          `arn:${Aws.PARTITION}:bedrock:*:${Aws.ACCOUNT_ID}:inference-profile/global.anthropic.claude-*`,
        ],
      }),
    );
    this.interceptor = new lambda.Function(this, 'Interceptor', {
      description: 'Per-user governance interceptor for AgentCore Gateway',
      runtime: LAMBDA_RUNTIME,
      handler: 'lambda_function.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda')),
      role: interceptorRole,
      logGroup: this.logGroup,
      memorySize: 256,
      // 120s accommodates the direct Bedrock call on path-model downgrades
      // (InvokeModel/Converse). The Lambda code sets a botocore read_timeout
      // 10s shorter so it can catch timeouts, close out the request row, and
      // return a 429 rather than being killed mid-flight.
      timeout: Duration.seconds(120),
      environment: {
        TABLE_NAME: this.table.tableName,
        DEFAULT_FALLBACK_MODEL: fallbackModel,
        DEFAULT_FALLBACK_MODEL_MANTLE: props.mantleFallbackModel ?? '',
        DEFAULT_FALLBACK_MODEL_OPENAI: props.openaiFallbackModel ?? '',
        DEFAULT_BUDGET_TOKENS: String(budgetTokens),
        // Downgrade to the fallback model at 80 percent of the budget.
        DEFAULT_DOWNGRADE_AT_TOKENS: String(Math.floor(budgetTokens * 0.8)),
        // Per-user requests-per-minute cap for users without an explicit
        // rate_limit_per_minute in their POLICY item. 0 disables it.
        DEFAULT_RATE_LIMIT_PER_MINUTE: String(props.defaultRateLimitPerMinute ?? 0),
        BUDGET_WINDOW: budgetWindow,
        CACHE_READ_WEIGHT_PCT: String(props.cacheTokenWeights?.readPct ?? 10),
        CACHE_WRITE_WEIGHT_PCT: String(props.cacheTokenWeights?.writePct ?? 125),
        // Must exceed what agentic clients request or they are refused at
        // admission; see the maxMaxTokens prop for the measured Claude Code
        // failure at the interceptor's 16384 default.
        MAX_MAX_TOKENS: String(props.maxMaxTokens ?? 64_000),
        ACTIVE_HOURS: activeHours,
        ACTIVE_HOURS_TZ: activeHoursTz,
        GUARDRAIL_ID: props.guardrail?.id ?? '',
        GUARDRAIL_VERSION: props.guardrail?.version ?? '',
        // Phase 7: price unknown models at a conservative ESTIMATED rate so
        // per-user USD thresholds enforce across the whole "enable ALL models"
        // catalog (costs labelled estimated). Empty string preserves the
        // original F3 fail-closed (503 pricing_unavailable) behavior.
        PRICING_DEFAULT_ESTIMATE: props.pricingDefaultEstimate ?? '',
      },
    });
    if (props.guardrail) {
      interceptorRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'ApplyGuardrail',
          actions: ['bedrock:ApplyGuardrail'],
          resources: [
            `arn:aws:bedrock:${Aws.REGION}:${Aws.ACCOUNT_ID}:guardrail/${props.guardrail.id}`,
          ],
        }),
      );
    }

    if (props.existingGatewayId) {
      this.gatewayId = props.existingGatewayId;
      const gatewayArn = stack.formatArn({
        service: 'bedrock-agentcore',
        resource: 'gateway',
        resourceName: props.existingGatewayId,
      });
      this.gatewayUrl = `https://${props.existingGatewayId}.gateway.bedrock-agentcore.${Aws.REGION}.amazonaws.com`;
      this.allowGatewayInvoke(gatewayArn);
      this.attachToExistingGateway(
        props.existingGatewayId,
        gatewayArn,
        props.existingGatewayRoleArn!,
      );
      return;
    }

    // Identity provider: bring-your-own OIDC, or a locked-down Cognito pool.
    let discoveryUrl: string;
    let clientId: string;
    if (props.oidc) {
      discoveryUrl = props.oidc.discoveryUrl;
      clientId = props.oidc.clientId;
    } else {
      this.userPool = new cognito.UserPool(this, 'UserPool', {
        // Admin-only: nobody can sign themselves up.
        selfSignUpEnabled: false,
        signInCaseSensitive: false,
        deletionProtection: true,
        // Plus tier so the pool that fronts a governed model gateway gets
        // threat protection and compromised-credential checks rather than
        // password rules alone. At demo scale (a handful of monthly active
        // users) the tier difference is cents; the sample would otherwise ship
        // a pool that fails AwsSolutions-COG8 for everyone who deploys it.
        featurePlan: cognito.FeaturePlan.PLUS,
        passwordPolicy: {
          // Demo convenience: an 8-char minimum so presenters can use a
          // short, memorable password. Production deployments should raise
          // this (14+) and prefer a hosted UI or federated identity.
          minLength: 8,
          requireLowercase: true,
          requireUppercase: true,
          requireDigits: true,
          requireSymbols: true,
          tempPasswordValidity: Duration.days(1),
        },
        // Deletion protection blocks stack deletes, so retain the pool on
        // destroy and remove it manually (see the README teardown section).
        removalPolicy: RemovalPolicy.RETAIN,
      });
      // No hosted UI: no user pool domain is created, so there is nothing to
      // browse to. Both password flows are enabled so the demo frontend can
      // sign in directly and scripts can use admin-initiate-auth.
      this.userPoolClient = this.userPool.addClient('AppClient', {
        authFlows: { userPassword: true, adminUserPassword: true },
        generateSecret: false,
        preventUserExistenceErrors: true,
        enableTokenRevocation: true,
        accessTokenValidity: Duration.minutes(60),
        idTokenValidity: Duration.minutes(60),
        refreshTokenValidity: Duration.days(30),
      });
      discoveryUrl = `https://cognito-idp.${Aws.REGION}.amazonaws.com/${this.userPool.userPoolId}/.well-known/openid-configuration`;
      clientId = this.userPoolClient.userPoolClientId;

      // Phase 7 (UI req #1/#2): Cognito groups that determine model-family
      // entitlement. Membership is the only thing that varies per user; the
      // group -> allowed_models mapping is resolved from the live ACTIVE model
      // catalog at deploy time (see lambda/model_catalog.py)
      // and written onto each member's POLICY# item. The group names here MUST
      // match config.yaml identity.groups[].name and the interceptor never
      // reads cognito:groups directly (entitlement is the seeded per-user
      // allowed_models set), so these groups are for identity/admin grouping
      // and are the source of truth the seeder maps from.
      for (const groupName of ['claude_group', 'gpt_group', 'ow_group', 'admin']) {
        new cognito.CfnUserPoolGroup(this, `Group-${groupName}`, {
          userPoolId: this.userPool.userPoolId,
          groupName,
          description: `Phase 7 entitlement group: ${groupName}`,
        });
      }
    }

    // The gateway execution role. The L2 Gateway construct would create this
    // for us, but the L2 always injects an MCP protocol configuration, and
    // HTTP passthrough targets are rejected on MCP-protocol gateways (the
    // control plane returns 400). Omitting protocolType entirely is
    // the one shape that carries BOTH an inference target and a bedrock-runtime
    // passthrough target on a single gateway, so this construct uses the L1
    // CfnGateway and creates the role explicitly. The trust policy mirrors what
    // the L2 built: bedrock-agentcore may assume it, scoped by account and by
    // gateway-ARN prefix on the gateway name.
    const gatewayName = `per-user-governance${this.suffix}`;
    const gatewayRole = new iam.Role(this, 'GatewayServiceRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': Aws.ACCOUNT_ID },
          ArnLike: {
            'aws:SourceArn': `arn:${Aws.PARTITION}:bedrock-agentcore:${Aws.REGION}:${Aws.ACCOUNT_ID}:gateway/${gatewayName}*`,
          },
        },
      }),
      description: `Service role for the ${gatewayName} AgentCore Gateway`,
    });
    // Upstream permissions for the gateway role. The bedrock-mantle inference
    // connector calls the bedrock-mantle.<region>.api.aws endpoint and needs
    // its own IAM namespace in addition to the bedrock:InvokeModel* actions:
    // with no bedrock-mantle grant the connector cannot serve any model and is
    // denied on bedrock-mantle:ListModels. Model access is scoped to
    // Anthropic Claude foundation models and us./global. cross-region
    // inference profiles.
    gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeClaude',
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          `arn:${Aws.PARTITION}:bedrock:*::foundation-model/anthropic.claude-*`,
          `arn:${Aws.PARTITION}:bedrock:*:${Aws.ACCOUNT_ID}:inference-profile/us.anthropic.claude-*`,
          // Claude Code in Bedrock mode defaults to global.* cross-region
          // inference profiles; without this ARN the passthrough target gets
          // AccessDenied on InvokeModelWithResponseStream.
          `arn:${Aws.PARTITION}:bedrock:*:${Aws.ACCOUNT_ID}:inference-profile/global.anthropic.claude-*`,
          // Phase 7/8: hosted OpenAI GPT (gpt-5.6-*, gpt-6-*) have NO on-demand
          // throughput and are NOT served by the mantle /inference OpenAI door;
          // they are only invocable via their `us.openai.*` cross-region
          // inference profile through the /bedrock-runtime Converse passthrough
          // target. Grant that profile + its backing foundation models so the
          // gpt_group's hosted models resolve instead of 404/403. (gpt-oss-*
          // still goes through the mantle door and needs no grant here.)
          `arn:${Aws.PARTITION}:bedrock:*::foundation-model/openai.gpt-*`,
          `arn:${Aws.PARTITION}:bedrock:*:${Aws.ACCOUNT_ID}:inference-profile/us.openai.gpt-*`,
        ],
      }),
    );
    // The bedrock-mantle grant, enumerated rather than wildcarded. The
    // connector needs the inference lifecycle and the read side of the catalog,
    // so this statement names those actions and leaves out the rest of the
    // namespace. For the current full action set, see the IAM console policy
    // editor's action list for the bedrock-mantle namespace; it grows as the
    // service adds features, and anything added after this comment was written
    // is excluded by default because this list is explicit.
    //
    // Every action left out either administers the namespace or mutates
    // something that outlives a request: CreateProject, ArchiveProject,
    // UpdateProject, the customized-model and fine-tuning families, Files,
    // Reservations, TagResource/UntagResource, and PutAccountDataRetention.
    // ListTagsForResource is granted and is a read. Projects are created out of
    // band by an operator, not by the gateway role, so the connector only ever
    // reads them.
    //
    // The resource scope is below, and it is not '*'. Every action granted here
    // takes a project ARN.
    //
    // To re-derive this list for your account: look up the namespace in the
    // IAM console policy editor for the full action set, then run
    //   aws cloudtrail lookup-events \
    //     --lookup-attributes AttributeKey=EventSource,AttributeValue=bedrock-mantle.amazonaws.com
    // and keep the actions your gateway role is recorded calling. CloudTrail
    // records mantle management actions only, so the inference actions below
    // will not appear there.
    gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'MantleConnector',
        actions: [
          'bedrock-mantle:CreateInference',
          'bedrock-mantle:GetInference',
          'bedrock-mantle:CancelInference',
          'bedrock-mantle:DeleteInference',
          'bedrock-mantle:ListModels',
          'bedrock-mantle:GetModel',
          'bedrock-mantle:ListProjects',
          'bedrock-mantle:GetProject',
          'bedrock-mantle:ListTagsForResource',
        ],
        // Scoped to projects in THIS account and region.
        //
        // Correcting an earlier comment here, which said these actions had no
        // account-scoped ARN to name and granted them on '*'. The IAM service
        // authorization reference for bedrock-mantle says otherwise: all nine
        // actions above take `project` as a required resource type, including
        // ListModels and GetModel, which read the catalog "in a project"
        // rather than a service-wide catalog. ListTagsForResource accepts
        // project among its resource types and is used here only for projects.
        //
        // On '*' the role could create, cancel and delete inferences in every
        // project in the account, including projects belonging to unrelated
        // workloads. Naming project/* in this account and region removes that.
        // The wildcard that remains is the project id, which has to stay: the
        // per-user project is opt-in (-c mantleProject=true) and its id is
        // generated by the service at deploy time, and requests made without a
        // project resolve to the account's default project. Both must be
        // reachable, and neither id is known when this policy is written.
        //
        // No statement here needs '*'. The only actions in this namespace with
        // no resource type are GetAccountDataRetention and
        // PutAccountDataRetention, and neither is granted.
        //
        // https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock-mantle.html
        resources: [
          `arn:${Aws.PARTITION}:bedrock-mantle:${Aws.REGION}:${Aws.ACCOUNT_ID}:project/*`,
        ],
      }),
    );
    // The gateway invokes the interceptor Lambda on every request. The L2
    // construct's LambdaInterceptor.bind added this grant automatically; with
    // the L1 CfnGateway it must be explicit. Without it every request fails
    // with "Access denied while invoking Lambda function".
    gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeInterceptor',
        actions: ['lambda:InvokeFunction'],
        resources: [this.interceptor.functionArn],
      }),
    );

    // The gateway itself. CUSTOM_JWT means every request must carry a valid
    // bearer token from the configured issuer; there is no anonymous access.
    // No protocolType is set, which is the shape that accepts both
    // the bedrock-mantle inference target and the bedrock-runtime HTTP
    // passthrough target on the same gateway. The interceptor reads headers
    // (passRequestHeaders) to recover the validated JWT subject.
    //
    // Interception points are gateway-wide, not per-target, so the RESPONSE
    // point needed to meter the mantle door also applies to the
    // bedrock-runtime passthrough -- which is why response buffering cannot
    // be limited to just the door that needs it. Splitting the doors across
    // two gateways is the only way to keep one of them streaming.
    this.gateway = new agentcore.CfnGateway(this, 'Gateway', {
      name: gatewayName,
      description: 'JWT-authorized inference gateway with per-user governance',
      roleArn: gatewayRole.roleArn,
      authorizerType: 'CUSTOM_JWT',
      authorizerConfiguration: {
        customJwtAuthorizer: {
          discoveryUrl,
          allowedClients: [clientId],
        },
      },
      interceptorConfigurations: [
        {
          interceptor: { lambda: { arn: this.interceptor.functionArn } },
          // REQUEST governs admission. RESPONSE settles usage in band, which is
          // the ONLY way to meter the mantle (`/inference`) door per user.
          //
          // AWS writes Bedrock model invocation logs only for the
          // bedrock-runtime endpoint, so the asynchronous invocation-log pipe
          // never sees mantle traffic. The mantle endpoint's own billing records
          // are per-project-per-day, which is what used to force a
          // bedrock-mantle project per user -- unworkable for an enterprise,
          // where projects are a limited per-account resource.
          //
          // The responses themselves carry the answer: every mantle door
          // returns a `usage` block (verified live -- Anthropic Messages returns
          // input_tokens/output_tokens plus the cache counters, OpenAI Chat
          // Completions returns prompt_tokens/completion_tokens). Reading it
          // here settles the exact per-request cost against the calling user's
          // ledger with no project involved.
          //
          // Double settlement is not a risk: runtime-door traffic can also be
          // settled by the invocation-log pipe, and both paths commit through a
          // conditional put on EVENT#<request_id>, so whichever arrives second
          // is a no-op.
          interceptionPoints: ['REQUEST', 'RESPONSE'],
          inputConfiguration: { passRequestHeaders: true },
        },
      ],
    });
    this.gateway.node.addDependency(gatewayRole);
    this.gatewayId = this.gateway.attrGatewayIdentifier;
    // Clients need the BASE url so they can append /inference or
    // /bedrock-runtime/... . Build it from the gateway id, which is stable.
    this.gatewayUrl = `https://${this.gateway.attrGatewayIdentifier}.gateway.bedrock-agentcore.${Aws.REGION}.amazonaws.com`;

    // Lock a resource policy on the interceptor to the exact gateway ARN so
    // only the AgentCore service, only from this account, only on behalf of
    // this gateway, can invoke it.
    this.allowGatewayInvoke(this.gateway.attrGatewayArn);

    // Both targets live on this single gateway. The inference target serves
    // Anthropic Messages, OpenAI chat completions, and mantle-native traffic
    // through the bedrock-mantle connector; the passthrough target serves the
    // native bedrock-runtime SDK formats (InvokeModel, Converse, ConverseStream)
    // used by Claude Code in Bedrock mode, Strands BedrockModel, and boto3.
    // One authorizer, one interceptor, one ledger: one governance system with
    // two doors.
    this.createInferenceTarget(this.gateway);
    this.createPassthroughTarget(this.gateway);

    // Vended gateway observability (CP-16 / REQ-ADMIN-015A telemetry view,
    // REQ-ADMIN-015B metrics/OTEL export). A bare AgentCore gateway emits
    // NOTHING to CloudWatch until the log-delivery trio below exists. Gated by
    // the `observability` context flag (default ON for the demo) so it is
    // reversible. See ARCHITECTURE.md section 10 and OBSERVABILITY.md.
    this.addObservability(this.gateway);

    // Attribution pipeline: Bedrock invocation logs -> subscription filter ->
    // attribution Lambda -> DynamoDB debit. This replaces the inline RESPONSE
    // interceptor and is what makes streaming possible. The attribution Lambda
    // reads only token counts and requestMetadata (no prompt/response text)
    // from the invocation log and debits the same USAGE items the REQUEST
    // interceptor checks on admission.
    this.createAttributionPipeline(
      invocationLogGroupName,
      budgetWindow,
      {
        readPct: props.cacheTokenWeights?.readPct ?? 10,
        writePct: props.cacheTokenWeights?.writePct ?? 125,
      },
      props.existingInvocationLogGroupName,
      props.ownLoggingRoleForExistingGroup ?? false,
    );

    // Mantle attribution pipeline: AWS/BedrockMantle CloudWatch metrics ->
    // scheduled Lambda -> DynamoDB debit. Mantle traffic is not written to
    // Bedrock invocation logs, so per-user usage is recovered from the
    // Project-dimensioned mantle metrics. The interceptor tags each mantle
    // request with anthropic-workspace-id=<workspace_id> so the metrics break
    // the user's usage out by project; this Lambda reconciles those metrics
    // into the same USAGE items the interceptor checks on admission.
  }

  /**
   * Restrict who can invoke the interceptor: only the AgentCore service,
   * only from this account, and only on behalf of this exact gateway.
   */
  private allowGatewayInvoke(gatewayArn: string): void {
    this.interceptor.addPermission('GatewayInvoke', {
      principal: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      action: 'lambda:InvokeFunction',
      sourceAccount: Aws.ACCOUNT_ID,
      sourceArn: gatewayArn,
    });
  }

  /**
   * Create the Amazon Bedrock inference target on the gateway.
   *
   * Why a custom resource: CloudFormation's GatewayTarget does not model the
   * inference connector union consistently across regions. An L1 escape hatch
   * that sets TargetConfiguration.Inference.Connector.Source.ConnectorId to
   * bedrock-mantle is accepted in some regions and rejected in others.
   * Calling the bedrock-agentcore-control API directly works in any region.
   */
  private createInferenceTarget(gateway: agentcore.CfnGateway): void {
    const targetConfiguration = {
      inference: { connector: { source: { connectorId: 'bedrock-mantle' } } },
    };
    const credentialProviderConfigurations = [
      { credentialProviderType: 'GATEWAY_IAM_ROLE' },
    ];
    const target = new cr.AwsCustomResource(this, 'InferenceTarget', {
      resourceType: 'Custom::AgentCoreInferenceTarget',
      // The SDK bundled into the Lambda runtime may predate the inference
      // connector union member. Installing the latest SDK at cold start
      // guarantees the API model knows it. Requires npm egress at deploy time.
      installLatestAwsSdk: true,
      onCreate: {
        service: 'bedrock-agentcore-control',
        action: 'CreateGatewayTarget',
        parameters: {
          gatewayIdentifier: gateway.attrGatewayIdentifier,
          name: TARGET_NAME,
          description: 'Amazon Bedrock inference through the bedrock-mantle connector',
          targetConfiguration,
          credentialProviderConfigurations,
        },
        physicalResourceId: cr.PhysicalResourceId.fromResponse('targetId'),
      },
      onUpdate: {
        service: 'bedrock-agentcore-control',
        action: 'UpdateGatewayTarget',
        parameters: {
          gatewayIdentifier: gateway.attrGatewayIdentifier,
          targetId: new cr.PhysicalResourceIdReference(),
          name: TARGET_NAME,
          description: 'Amazon Bedrock inference through the bedrock-mantle connector',
          targetConfiguration,
          credentialProviderConfigurations,
        },
        physicalResourceId: cr.PhysicalResourceId.fromResponse('targetId'),
      },
      onDelete: {
        service: 'bedrock-agentcore-control',
        action: 'DeleteGatewayTarget',
        parameters: {
          gatewayIdentifier: gateway.attrGatewayIdentifier,
          targetId: new cr.PhysicalResourceIdReference(),
        },
        // Idempotent teardown: a target already deleted out of band is fine.
        ignoreErrorCodesMatching: 'ResourceNotFoundException',
      },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: [
            'bedrock-agentcore:CreateGatewayTarget',
            'bedrock-agentcore:UpdateGatewayTarget',
            'bedrock-agentcore:DeleteGatewayTarget',
            'bedrock-agentcore:GetGatewayTarget',
          ],
          resources: [gateway.attrGatewayArn, `${gateway.attrGatewayArn}/*`],
        }),
      ]),
      timeout: Duration.minutes(2),
    });
    target.node.addDependency(gateway);
  }

  /**
   * Create the HTTP passthrough target for native SDK clients.
   *
   * Serves Claude Code (Bedrock mode: /model/{id}/invoke) and Converse
   * clients (/model/{id}/converse). The gateway role signs outbound with
   * SigV4 service=bedrock so bedrock-runtime accepts the forwarded request.
   * Downgrades for these path-model formats are handled by the interceptor
   * calling Bedrock directly (the target is only reached on allow).
   *
   * Why a Lambda-backed custom resource with raw SigV4 instead of
   * AwsCustomResource: the SDK bundled into the custom-resource runtime may
   * predate the passthrough member of HttpTargetConfiguration, in which case
   * the create call fails and CloudFormation falls back to a log-stream-name
   * physical id that then poisons the delete. The handler signs its own
   * requests, so it works regardless of the bundled SDK's API model.
   */
  private createPassthroughTarget(gateway: agentcore.CfnGateway): void {
    const handlerRole = new iam.Role(this, 'PassthroughHandlerRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Creates the bedrock-runtime passthrough target on the gateway',
    });
    const handlerLogs = new logs.LogGroup(this, 'PassthroughHandlerLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    handlerRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [handlerLogs.logGroupArn],
      }),
    );
    handlerRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'bedrock-agentcore:CreateGatewayTarget',
          'bedrock-agentcore:UpdateGatewayTarget',
          'bedrock-agentcore:DeleteGatewayTarget',
          'bedrock-agentcore:GetGatewayTarget',
        ],
        resources: [gateway.attrGatewayArn, `${gateway.attrGatewayArn}/*`],
      }),
    );
    const handler = new lambda.Function(this, 'PassthroughHandler', {
      description: 'Custom resource: HTTP passthrough target via raw SigV4',
      runtime: LAMBDA_RUNTIME,
      handler: 'create_passthrough_target.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda')),
      role: handlerRole,
      logGroup: handlerLogs,
      memorySize: 256,
      timeout: Duration.minutes(5),
    });
    const passthroughTarget = new CustomResource(this, 'PassthroughTarget', {
      resourceType: 'Custom::AgentCorePassthroughTarget',
      serviceToken: handler.functionArn,
      properties: {
        GatewayIdentifier: gateway.attrGatewayIdentifier,
        TargetName: 'bedrock-runtime',
        Endpoint: `https://bedrock-runtime.${Aws.REGION}.amazonaws.com`,
        SigningService: 'bedrock',
      },
    });
    passthroughTarget.node.addDependency(gateway);
  }

  /**
   * Vended gateway observability (CP-16). Turn on the AgentCore gateway's OWN
   * OTEL spans + structured logs delivery to CloudWatch/X-Ray via the CDK
   * log-delivery trio. This is INFRA only — no interceptor code and no app-side
   * OTEL SDK is involved. It gives operators two things:
   *   - a view of telemetry, latency and error trends, and
   *   - metrics/OTEL export to a downstream sink such as Datadog (vended OTEL
   *     spans export cleanly).
   *
   * Two planes, deliberately separate (ARCHITECTURE.md section 10.1): this is
   * OPERATIONAL telemetry ("how is the system performing"). It does NOT replace
   * the CP-11 compliance audit (the content-free usage record delivered to SIEM,
   * emitted separately by the interceptor's structured decision-log lines).
   *
   * A bare gateway emits NOTHING until this trio exists. What it creates:
   *   - APPLICATION_LOGS: CfnDeliverySource(gateway) -> CfnDeliveryDestination
   *     (CWL, a /aws/vendedlogs/ log group) -> CfnDelivery. Vended logs carry
   *     trace_id/span_id and a `routing` block (targetName + resolvedModelId).
   *   - TRACES: CfnDeliverySource(gateway) -> CfnDeliveryDestination(XRAY) ->
   *     CfnDelivery. OTEL vended spans (kind:SERVER gateway hop + kind:CLIENT
   *     target hop) then flow to CloudWatch Transaction Search (aws/spans).
   *
   * Reversible: gated behind the `observability` CDK context flag (default ON
   * for the demo). Disable per deploy with `-c observability=false`; traces
   * alone can be turned off with `-c observabilityTraces=false`.
   *
   * GOTCHAS (encoded here and in OBSERVABILITY.md):
   *   - The log group name MUST start with `/aws/vendedlogs/` or delivery fails.
   *   - Delivery depends on the gateway existing (explicit node dependency).
   *   - TRACES silently produce no spans unless account-level CloudWatch
   *     Transaction Search is ACTIVE — a documented deploy prerequisite, NOT
   *     something CDK can enable here.
   *   - Spans lag logs by ~1-2 min; logs land in seconds. Treat logs as the
   *     primary interactive signal, spans as async enrichment.
   */
  private addObservability(gateway: agentcore.CfnGateway): void {
    // Default ON for the demo; `-c observability=false` turns the whole trio
    // off and the gateway reverts to emitting nothing.
    const enabledCtx = this.node.tryGetContext('observability');
    const enabled = enabledCtx === undefined || enabledCtx === true || enabledCtx === 'true';
    if (!enabled) {
      return;
    }
    // Traces sub-gate: default ON, `-c observabilityTraces=false` keeps
    // APPLICATION_LOGS but drops the TRACES->XRAY delivery (e.g. when
    // Transaction Search is not ACTIVE in the target account yet).
    const tracesCtx = this.node.tryGetContext('observabilityTraces');
    const enableTraces =
      tracesCtx === undefined || tracesCtx === true || tracesCtx === 'true';

    const name = `per-user-governance${this.suffix}`;
    const gatewayArn = gateway.attrGatewayArn;

    // Vended-logs group. The name MUST start with /aws/vendedlogs/ or the
    // CfnDelivery to a CWL destination fails at deploy time. The gateway id is
    // resolved at deploy time (a token here), so the group name is built from
    // it rather than hard-coded.
    const vendedLogGroupName =
      `/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/${gateway.attrGatewayIdentifier}`;
    const vendedLogGroup = new logs.LogGroup(this, 'GatewayVendedLogs', {
      logGroupName: vendedLogGroupName,
      retention: logs.RetentionDays.ONE_MONTH,
      // Sample posture: telemetry is disposable, so tear down on destroy.
      removalPolicy: RemovalPolicy.DESTROY,
    });
    this.vendedLogGroupName = vendedLogGroupName;

    // --- APPLICATION_LOGS: source(gateway) -> CWL destination -> delivery ---
    const logsSource = new logs.CfnDeliverySource(this, 'GatewayLogsSource', {
      name: `${name}-logs-source`,
      logType: 'APPLICATION_LOGS',
      resourceArn: gatewayArn,
    });
    logsSource.node.addDependency(gateway);
    const logsDest = new logs.CfnDeliveryDestination(this, 'GatewayLogsDestination', {
      name: `${name}-logs-destination`,
      // CWL destination -> the /aws/vendedlogs/ CloudWatch Logs group.
      deliveryDestinationType: 'CWL',
      destinationResourceArn: vendedLogGroup.logGroupArn,
    });
    const logsDelivery = new logs.CfnDelivery(this, 'GatewayLogsDelivery', {
      deliverySourceName: logsSource.name,
      deliveryDestinationArn: logsDest.attrArn,
    });
    logsDelivery.node.addDependency(logsSource);
    logsDelivery.node.addDependency(logsDest);

    // --- TRACES: source(gateway) -> XRAY destination -> delivery ---
    // XRAY delivery needs account-level CloudWatch Transaction Search ACTIVE
    // (documented prerequisite; not enabled from CDK). Without it the delivery
    // is created but silently yields no spans.
    if (enableTraces) {
      const tracesSource = new logs.CfnDeliverySource(this, 'GatewayTracesSource', {
        name: `${name}-traces-source`,
        logType: 'TRACES',
        resourceArn: gatewayArn,
      });
      tracesSource.node.addDependency(gateway);
      const tracesDest = new logs.CfnDeliveryDestination(this, 'GatewayTracesDestination', {
        name: `${name}-traces-destination`,
        // XRAY destination -> spans flow to CloudWatch Transaction Search
        // (aws/spans). No destinationResourceArn for the XRAY type.
        deliveryDestinationType: 'XRAY',
      });
      const tracesDelivery = new logs.CfnDelivery(this, 'GatewayTracesDelivery', {
        deliverySourceName: tracesSource.name,
        deliveryDestinationArn: tracesDest.attrArn,
      });
      tracesDelivery.node.addDependency(tracesSource);
      tracesDelivery.node.addDependency(tracesDest);
    }
  }

  /**
   * Attach the interceptor to a gateway this stack does not own. An
   * AwsCustomResource cannot read, merge, and write in one call, so a small
   * Python handler performs GetGateway, merges the interceptor configuration,
   * and calls UpdateGateway. On delete it removes only this function's entry.
   */
  private attachToExistingGateway(
    gatewayId: string,
    gatewayArn: string,
    gatewayRoleArn: string,
  ): void {
    const handlerRole = new iam.Role(this, 'AttachHandlerRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Attaches the governance interceptor to an existing gateway',
    });
    const handlerLogs = new logs.LogGroup(this, 'AttachHandlerLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    handlerRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [handlerLogs.logGroupArn],
      }),
    );
    handlerRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['bedrock-agentcore:GetGateway', 'bedrock-agentcore:UpdateGateway'],
        resources: [gatewayArn],
      }),
    );
    // UpdateGateway echoes the gateway's own roleArn back, which requires
    // iam:PassRole. Scoped to that single role ARN (supplied by the deployer as
    // existingGatewayRoleArn, since a gateway this stack does not own has a
    // role it cannot resolve at synth time) and further constrained to the
    // AgentCore service, so this handler cannot pass any other role anywhere
    // else.
    handlerRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'PassGatewayRoleBackToUpdateGateway',
        actions: ['iam:PassRole'],
        resources: [gatewayRoleArn],
        conditions: {
          StringEquals: { 'iam:PassedToService': 'bedrock-agentcore.amazonaws.com' },
        },
      }),
    );
    const handler = new lambda.Function(this, 'AttachHandler', {
      description: 'Custom resource: attach governance interceptor to an existing gateway',
      runtime: LAMBDA_RUNTIME,
      handler: 'attach_interceptor.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda')),
      role: handlerRole,
      logGroup: handlerLogs,
      memorySize: 256,
      timeout: Duration.minutes(5),
    });
    new CustomResource(this, 'InterceptorAttachment', {
      resourceType: 'Custom::GatewayInterceptorAttachment',
      serviceToken: handler.functionArn,
      properties: {
        GatewayIdentifier: gatewayId,
        FunctionArn: this.interceptor.functionArn,
      },
    });
  }

  /**
   * Async attribution pipeline: Bedrock invocation logs -> subscription filter
   * -> attribution Lambda -> DynamoDB.
   *
   * Bedrock model invocation logging is an account-level setting. This method
   * creates the CloudWatch Logs group the logs land in, the attribution Lambda
   * that processes them, and the subscription filter that connects the two.
   * The account-level logging configuration itself (textDataDeliveryEnabled:
   * false, pointed at this log group) is a documented prerequisite that the
   * deployer enables once, because it affects all Bedrock usage in the account
   * and region.
   */
  private createAttributionPipeline(
    invocationLogGroupName: string,
    budgetWindow: string,
    cacheWeights: { readPct: number; writePct: number },
    existingInvocationLogGroupName?: string,
    ownLoggingRoleForExistingGroup = false,
  ): void {
    // Two shapes. Reuse: the account already has Bedrock invocation logging
    // pointed at a customer-owned log group, so import that group by name,
    // attach only the subscription filter, and leave the account-level
    // configuration and the group itself untouched. Create: no existing
    // configuration, so create the group and the logging role and let the
    // deploy script point account-level logging at them.
    let invocationLogGroup: logs.ILogGroup;
    let ownLoggingRole: boolean;
    if (existingInvocationLogGroupName) {
      invocationLogGroup = logs.LogGroup.fromLogGroupName(
        this, 'InvocationLogs', existingInvocationLogGroupName,
      );
      this.invocationLogGroupName = existingInvocationLogGroupName;
      ownLoggingRole = ownLoggingRoleForExistingGroup;
    } else {
      const created = new logs.LogGroup(this, 'InvocationLogs', {
        logGroupName: invocationLogGroupName,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: RemovalPolicy.DESTROY,
      });
      // Exported so the deploy script can point the account-level Bedrock
      // invocation logging configuration at this exact group.
      this.invocationLogGroup = created;
      this.invocationLogGroupName = invocationLogGroupName;
      invocationLogGroup = created;
      ownLoggingRole = true;
    }

    // Role for Bedrock to write to the log group, named by the account-level
    // logging config. Skipped only for plain reuse of a configuration this
    // stack did not create, which already carries its own delivery role.
    if (ownLoggingRole) {
      const loggingRole = new iam.Role(this, 'BedrockLoggingRole', {
        assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com', {
          conditions: {
            StringEquals: { 'aws:SourceAccount': Aws.ACCOUNT_ID },
          },
        }),
        description: 'Allows Bedrock model invocation logging to write to CloudWatch Logs',
      });
      invocationLogGroup.grantWrite(loggingRole);
      // Exported so the deploy script can pass roleArn to
      // PutModelInvocationLoggingConfiguration.
      this.bedrockLoggingRole = loggingRole;
      this.bedrockLoggingRoleArn = loggingRole.roleArn;
    }

    // The ONE attribution Lambda for both doors. Its handler dispatches on
    // event shape: a CloudWatch Logs subscription delivery (awslogs key)
    // debits the ledger from Bedrock invocation-log records (the
    // /bedrock-runtime door); an EventBridge schedule tick reconciles
    // AWS/BedrockMantle project metrics into the same ledger (the /inference
    // door, which invocation logging does not capture).
    const attributionLogs = new logs.LogGroup(this, 'AttributionLambdaLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    // Explicit role, like the interceptor's: the default Lambda role attaches
    // the AWSLambdaBasicExecutionRole managed policy, which grants logs:* on
    // every log group in the account. Two statements on this function's own
    // group are all it needs, and the group already exists here so the ARN is
    // known at synth time.
    const attributionRole = new iam.Role(this, 'AttributionLambdaRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Least-privilege role for the attribution Lambda',
    });
    attributionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'AttributionLogs',
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        // LogGroup.logGroupArn already ends with :* and so covers log streams.
        resources: [attributionLogs.logGroupArn],
      }),
    );
    const attributionFn = new lambda.Function(this, 'AttributionLambda', {
      description:
        'Debit the DynamoDB ledger: invocation-log records (subscription) and mantle project metrics (schedule)',
      runtime: LAMBDA_RUNTIME,
      handler: 'attribution_lambda.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda')),
      role: attributionRole,
      logGroup: attributionLogs,
      memorySize: 256,
      // The mantle reconciliation path iterates workspace policies and calls
      // GetMetricStatistics per user; 2 minutes bounds a large fleet.
      timeout: Duration.minutes(2),
      environment: {
        TABLE_NAME: this.table.tableName,
        // The settlement write must bucket usage exactly like the admission
        // read, so the window is threaded to both Lambdas from one prop --
        // and the cache weights must match the interceptor's so both
        // settlement paths debit identically. EVENT rows written here age
        // out on the same clock as the interceptor's in-band settles: both
        // Lambdas share the EVENT_TTL_SECONDS code default; override it on
        // both or neither.
        BUDGET_WINDOW: budgetWindow,
        CACHE_READ_WEIGHT_PCT: String(cacheWeights.readPct),
        CACHE_WRITE_WEIGHT_PCT: String(cacheWeights.writePct),
      },
    });
    // Split rather than grantReadWriteData, which would hand this function
    // PutItem/UpdateItem/DeleteItem over every item in the single-table
    // ledger, POLICY items included. This function is driven by invocation-log
    // content it does not author, and it only ever reads POLICY items: one
    // projected Scan recovers each user's workspace_id. Writing them is not
    // something it needs, so the write grant is keyed to the four prefixes it
    // does write.
    //
    // Read is table-wide because dynamodb:LeadingKeys does not constrain Scan,
    // and a Scan is how the workspace_id lookup works. Read of the ledger was
    // never the exposure; write to POLICY was.
    attributionFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'GovernanceTableRead',
        actions: ['dynamodb:GetItem', 'dynamodb:Scan'],
        resources: [this.table.tableArn],
      }),
    );
    attributionFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'GovernanceTableSettle',
        // TransactWriteItems is authorized against the per-item actions, so
        // Put/Update/Delete are all required for the settlement transaction
        // (Put EVENT#, Update USAGE#, Delete REQ#). ConditionCheckItem is
        // included for the same reason it is on the interceptor role: omitting
        // it surfaces as an AccessDeniedException inside the transaction.
        actions: [
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:ConditionCheckItem',
          'dynamodb:TransactWriteItems',
        ],
        resources: [this.table.tableArn],
        conditions: {
          // USAGE# debits, REQ# admission rows this path deletes on settle,
          // EVENT# settled rows, MANTLE_HWM# the mantle cursor. POLICY# is
          // deliberately absent.
          'ForAllValues:StringLike': {
            'dynamodb:LeadingKeys': [
              'USAGE#*',
              'REQ#*',
              'EVENT#*',
              'MANTLE_HWM#*',
            ],
          },
        },
      }),
    );
    // CloudWatch GetMetricStatistics is not resource-scoped (no ARN exists).
    attributionFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'MantleMetrics',
        actions: ['cloudwatch:GetMetricStatistics'],
        resources: ['*'],
      }),
    );

    // Trigger 1: subscription filter streams invocation log records to the
    // Lambda in batches.
    new logs.SubscriptionFilter(this, 'AttributionFilter', {
      logGroup: invocationLogGroup,
      destination: new logsDestinations.LambdaDestination(attributionFn),
      filterPattern: logs.FilterPattern.allEvents(),
    });

    // Trigger 2: the mantle reconciliation tick, every 5 minutes. The
    // high-water-mark items keep the overlapping read window idempotent.
    new events.Rule(this, 'MantleAttributionSchedule', {
      schedule: events.Schedule.rate(Duration.minutes(5)),
      targets: [new targets.LambdaFunction(attributionFn)],
    });

    // Saved Logs Insights queries: the audit story as one-click console
    // queries instead of prose in a README. Two sources, two truths: the
    // interceptor decision log is what governance DECIDED (every request,
    // including refusals that never reached a model); the invocation log is
    // what Bedrock actually SERVED (token counts, stamped with the identity
    // the interceptor injected -- downgrade replays included).
    //
    // Saved query names are unique per account and region, so they carry the
    // deployment suffix; without it a second copy fails to create.
    new logs.CfnQueryDefinition(this, 'QueryDecisionsPerUser', {
      name: `governance/decisions-per-user${this.suffix}`,
      logGroupNames: [this.logGroup.logGroupName],
      queryString: [
        'fields @timestamp, user_id, action, model, effective_model, error_type',
        '| filter ispresent(user_id)',
        '| stats count() as requests by user_id, action',
        '| sort requests desc',
      ].join('\n'),
    });
    new logs.CfnQueryDefinition(this, 'QueryTokensPerUser', {
      name: `governance/tokens-per-user${this.suffix}`,
      logGroupNames: [invocationLogGroupName],
      queryString: [
        'fields requestMetadata.user as user',
        '| filter ispresent(requestMetadata.user)',
        '| stats sum(input.inputTokenCount) as input_tokens,',
        '        sum(output.outputTokenCount) as output_tokens,',
        '        sum(input.cacheReadInputTokenCount) as cache_read,',
        '        sum(input.cacheWriteInputTokenCount) as cache_write,',
        '        count() as calls by user',
        '| sort input_tokens desc',
      ].join('\n'),
    });
    new logs.CfnQueryDefinition(this, 'QueryModelsPerUser', {
      name: `governance/models-per-user${this.suffix}`,
      logGroupNames: [invocationLogGroupName],
      queryString: [
        'fields requestMetadata.user as user, modelId',
        '| filter ispresent(requestMetadata.user)',
        '| stats count() as calls,',
        '        sum(output.outputTokenCount) as output_tokens by user, modelId',
        '| sort user, calls desc',
      ].join('\n'),
    });
  }

}
