# infra: CDK app for per-user gateway governance

TypeScript CDK app that deploys the governance engine as one stack: a
`GovernanceGateway` construct plus a thin stack with outputs.

## One-click deploy

```bash
./scripts/deploy.sh --region us-east-1
```

`deploy.sh` is idempotent and runs eight steps.

1. Prerequisite and credential checks, region resolution. Requires node, npm,
   aws, and python3, and valid AWS credentials.
2. `npm ci` (the locked dependency tree, not a fresh semver resolution),
   `cdk bootstrap` (tolerant of an already-bootstrapped environment), and
   `cdk deploy`.
3. Read the stack outputs and fail early if any are empty.
4. Configure account-level Bedrock model invocation logging. If a
   configuration already exists and delivers to a CloudWatch log group, the
   script asks whether to reuse that group (the default: the stack attaches
   only its attribution subscription filter, and nothing account-level
   changes) or overwrite the configuration with the stack-owned log group.
   An S3-only configuration gets its own prompt, since attribution needs a
   CloudWatch stream to subscribe to. With no existing configuration, the
   script enables logging pointed at the CDK-created group with every
   data-delivery flag off (no prompt, response, image, embedding, video, or
   audio bodies). `--reuse-logging` and `--force-logging` skip the prompt
   for CI. Without a CloudWatch invocation-log stream the stack can read,
   passthrough (bedrock-runtime) attribution is not recorded.
5. Create the demo Cognito user with a generated one-time password, printed once
   and never stored.
6. Seed the demo user's `POLICY#<sub>` item, keyed on the JWT `sub` (the Cognito
   UUID, not the username), with budget, downgrade threshold, rate limit,
   fallback model, and allowed models.
7. Create (or reuse) a mantle project for the demo user and record its id as
   `workspace_id` on the POLICY item, so the interceptor tags the user's mantle
   requests with `anthropic-workspace-id`. Reuse avoids burning a slot against the
   per-account mantle project limit (check current Bedrock service quotas).
8. Print both doors and a smoke-test command that mints a JWT and calls the
   inference door.

Requirements: Node.js 20 or later, AWS credentials for a sandbox account, and a
region where Amazon Bedrock AgentCore Gateway and the Anthropic Claude models are
available (for example, us-east-1).

### Deploying a second copy in the same region

Nothing in this stack is inherently one-per-region except its names. Pass
`--suffix <s>` (letters, digits, hyphens, up to 20 characters) and the deploy
creates an independent copy: the stack becomes
`AgentCoreGovernanceSample-<s>`, the gateway `per-user-governance-<s>`, and the
demo user's mantle project `governance-demo-user-<s>`. Every other resource is
CloudFormation-named and never collided.

```bash
./scripts/deploy.sh --region us-east-1 --suffix test2
./scripts/destroy.sh --region us-east-1 --suffix test2   # same value to tear down
```

The one genuinely shared thing is Bedrock invocation logging, which is an
account-level, per-region singleton. The second copy reuses the group the
existing configuration delivers to (the step-4 prompt, or `--reuse-logging`)
and attaches its own subscription filter. Mind the CloudWatch Logs
subscription-filter limit per log group (2 in most regions) when stacking
copies.

### Running cdk deploy directly

The invocation-logging detection in step 4 lives in `deploy.sh`, not in the CDK
app, so a bare `npx cdk deploy` skips it. If the account already has a log group
at `/bedrock/invocation-logs`, the deploy then fails in changeset validation
with `Resource of type 'AWS::Logs::LogGroup' with identifier
'/bedrock/invocation-logs' already exists`, because the stack tries to create a
group that is already there. Nothing is deployed when this happens. Reuse the
existing group instead:

```bash
npx cdk deploy \
  -c existingInvocationLogGroupName=/bedrock/invocation-logs \
  -c ownLoggingRoleForExistingGroup=true
```

The first flag attaches only the attribution subscription filter and creates no
log group. Add the second when the existing configuration is one an earlier run
of this stack created: plain reuse assumes the configuration belongs to someone
else and creates no delivery role, which on redeploy deletes the role the live
configuration still names, and Bedrock then silently delivers nothing. Run
`npx cdk diff` with the same flags first: on an update it should show Lambda
code changes and no replacements. Alternatively, run `./scripts/deploy.sh`,
which handles this configuration.

## Teardown

```bash
./scripts/destroy.sh --region us-east-1
```

It requires you to type the stack name to confirm, runs `cdk destroy`, offers to
disable the account-level invocation logging the deploy turned on (answer no if
another workload relies on it), and reminds you to remove the retained Cognito
user pool. The pool has deletion protection and is retained by the stack, so
delete it by hand once the stack is gone:

```bash
aws cognito-idp update-user-pool --user-pool-id <UserPoolId> --deletion-protection INACTIVE
aws cognito-idp delete-user-pool --user-pool-id <UserPoolId>
```

## What the construct creates

- One `AWS::BedrockAgentCore::Gateway` (L1 `CfnGateway`, no protocolType, so it
  carries both an inference target and an HTTP passthrough target on a single
  gateway) with `CUSTOM_JWT` authorization. No anonymous access exists anywhere
  in this stack: no Lambda function URLs, no hosted Cognito UI, nothing public but
  the JWT-authorized gateway URL.
- Two targets on that gateway. The `governance-inference` target uses the
  `bedrock-mantle` inference connector (created through an `AwsCustomResource`
  calling `bedrock-agentcore-control` `CreateGatewayTarget`, because
  CloudFormation's `GatewayTarget` type does not model the inference connector in
  every region), and serves Anthropic Messages and both OpenAI formats. The
  `bedrock-runtime` target is an HTTP passthrough (created by a Lambda-backed
  custom resource that signs its own SigV4, so it works regardless of the bundled
  SDK's API model) and serves Converse, ConverseStream, InvokeModel, and
  InvokeModelWithResponseStream.
- One REQUEST-only Lambda interceptor (Python 3.12,
  `lambda/lambda_function.py`, configured through environment variables). It
  receives headers (`passRequestHeaders: true`) so it can read the validated JWT
  subject. There is no RESPONSE interceptor: attaching one forces the gateway
  to buffer the whole reply. Responses therefore stream.
- The invocation-log attribution pipeline: a CloudWatch log group for Bedrock
  invocation logs, a subscription filter, and the attribution Lambda that reads
  token counts and `requestMetadata` from each record (no bodies) and applies an
  atomic DynamoDB debit.
- The mantle attribution pipeline: a Lambda on a 5-minute EventBridge rule that
  reads `AWS/BedrockMantle` `TotalInputTokens` and `TotalOutputTokens` at the
  Project dimension and debits the same ledger, using per-workspace
  `MANTLE_HWM#<workspace>` high-water-mark items for idempotency. Mantle traffic
  is not written to Bedrock invocation logs, so this second pipe is what attributes
  it.
- A DynamoDB on-demand single table (`pk` partition key, TTL on `expires_at`,
  point-in-time recovery on) holding `POLICY#<sub>`, `USAGE#<sub>#<date>`,
  `REQ#<request id>`, `EVENT#<request id>`, and `MANTLE_HWM#<workspace>` items.
- A Cognito user pool that only administrators can create users in, with deletion
  protection, token revocation, prevented user-existence errors, and no hosted UI.
  Skipped when you bring your own OIDC issuer.

## Construct options

```ts
new GovernanceGateway(this, 'Governance', {
  // Bring your own OIDC issuer instead of the created Cognito pool.
  oidc: { discoveryUrl: 'https://issuer/.well-known/openid-configuration', clientId: 'abc' },

  // Or attach the interceptor and ledger to a gateway you already run. When set,
  // fallbackModel and existingGatewayRoleArn are both required: fallbackModel
  // must name a target that exists on your gateway, and the role ARN is the
  // gateway's own execution role, which the attach handler passes back to
  // UpdateGateway. Read it with:
  //   aws bedrock-agentcore-control get-gateway \
  //     --gateway-identifier my-gateway-abcdef1234 --query roleArn
  existingGatewayId: 'my-gateway-abcdef1234',
  existingGatewayRoleArn: 'arn:aws:iam::123456789012:role/my-gateway-role',

  // Model users are downgraded to near their budget. Passthrough door uses a
  // provider-form id like us.anthropic.claude-haiku-4-5-20251001-v1:0.
  fallbackModel: 'us.anthropic.claude-haiku-4-5-20251001-v1:0',

  // The mantle (/inference) door takes bare provider ids and rejects the
  // provider-form ones, so it keeps its own downgrade target; its OpenAI
  // shapes (Chat Completions, Responses) serve a disjoint OSS catalog and
  // need a third. Unset, the mantle door reuses fallbackModel and the
  // OpenAI shapes skip the downgrade rather than rewrite into a 400.
  mantleFallbackModel: 'anthropic.claude-haiku-4-5',
  openaiFallbackModel: 'gpt-oss-20b',

  // Budget weight of cache tokens as a percent of the input-token price,
  // applied identically at in-band settle and async attribution. Defaults
  // mirror Anthropic pricing (reads 10%, writes 125%); set both to 100 to
  // count cache tokens at full weight, or 0 to exclude them.
  cacheTokenWeights: { readPct: 10, writePct: 125 },

  // Per-user requests-per-minute cap for users without an explicit
  // rate_limit_per_minute in their POLICY item. 0 disables it.
  defaultRateLimitPerMinute: 0,

  // CloudWatch log group Bedrock invocation logs land in. deploy.sh points the
  // account-level logging config here and it is exported as an output.
  invocationLogGroupName: '/bedrock/invocation-logs',

  // Or reuse the log group an EXISTING account-level logging configuration
  // already delivers to: only the attribution subscription filter is
  // attached, and no log group, logging role, or account setting is created
  // or changed. deploy.sh sets this automatically when it detects an
  // existing configuration and you choose reuse.
  existingInvocationLogGroupName: '/your/existing/bedrock-logs',

  defaultBudgetTokens: 100000,

  // What "per" means for every budget_tokens value: hour | day | week | month,
  // calendar-aligned in UTC. See "Budget windows and active hours" below.
  budgetWindow: 'day',

  // Business-hours gate: requests outside this local-time range are refused
  // at admission with a retry hint. Unset (or equal start and end) = always
  // on; start later than end wraps overnight (22:00-06:00).
  activeHours: '09:00-17:00',
  activeHoursTz: 'America/New_York',
});
```

## Budget windows and active hours

Two deployment-wide knobs, also settable at deploy time without editing code
(`npx cdk deploy -c budgetWindow=week -c activeHours=09:00-17:00
-c activeHoursTz=America/New_York`).

`budgetWindow` sets what "per" means for every `budget_tokens` value, both
the deployment default and every per-user POLICY item. Windows are
calendar-aligned in UTC, matching API Gateway usage-plan quotas: `day`
resets at midnight UTC, `week` on Monday (ISO 8601), `month` on the 1st,
`hour` on the hour. They are fixed buckets, not rolling windows: a user can
spend a full budget just before the boundary and a fresh one just after.
Fixed buckets require only one atomic counter per period. Pair the budget with
`defaultRateLimitPerMinute` (or the per-user `rate_limit_per_minute`) for
burst control; the rate limit is enforced exactly at admission.

Changing the window on a live stack has three consequences:

- `budget_tokens` changes meaning. 500,000 per day becomes 500,000 per hour
  (24x looser) or per month (30x tighter). Rescale both the deployment default
  and every per-user POLICY item when changing the window.
- Existing counters are orphaned. The interceptor reads a new bucket key, so
  spend accumulated under the old window is not carried over. The old rows age
  out under their TTL.
- Enforcement precision is bounded by settlement lag: seconds on the
  /bedrock-runtime door, minutes on the /inference door. `hour` is the
  finest meaningful window, and on the /inference door an hourly budget can
  be overshot by a few minutes of spend at the boundary. The runtime door
  resolves each debit to the window the request was admitted in (via its
  REQ row), so its boundary attribution is exact.

`activeHours` is a separate, exact gate: a pure clock check at admission,
before any DynamoDB read. Outside the window every request gets a 429 with
`retry_after` and a message naming the opening time; inside it, normal
governance applies. The check is per request: a reply admitted at 16:59
streams to completion, and a multi-step agent mid-task at the boundary has
its next step refused. `activeHoursTz` takes an IANA zone and honors DST.
The budget window stays UTC regardless; if the budget reset and the gate
must share a clock, set `activeHoursTz: 'UTC'`.

`existingGatewayId` cannot be combined with `oidc`, because an existing gateway
keeps its own authorizer. The attach path uses a small custom resource
(`lambda/attach_interceptor.py`) that reads the gateway, merges in the
REQUEST-only interceptor entry, writes it back, and removes only its own entry on
delete. It also requires `existingGatewayRoleArn`: `UpdateGateway` echoes the
gateway's own `roleArn` back in the update payload, so the handler needs
`iam:PassRole` for that role. Specifying the ARN scopes that grant to one role
rather than `Resource: '*'`. Synth fails if `existingGatewayId` is set without
it.

## Input screening with a Bedrock guardrail

The interceptor can screen prompt text with an existing Amazon Bedrock guardrail
before the model runs, using `ApplyGuardrail`. It is off unless you pass both
context values at deploy time:

```bash
npx cdk deploy -c guardrailId=<guardrail-id> -c guardrailVersion=<version>
```

Both are required together; passing one without the other fails at synth rather
than deploying a silently disabled guardrail. When they are set, the stack
grants the interceptor `bedrock:ApplyGuardrail` on that guardrail only and
passes the id and version through as `GUARDRAIL_ID` and `GUARDRAIL_VERSION`.

What the screen does and does not cover:

- It runs on input only, before the budget check, so blocked prompts spend no
  tokens and no budget. There is no RESPONSE interceptor in this architecture,
  which is what keeps replies streaming, so model output is not screened by the
  gateway. Attach output guardrails in the calling application, or send
  `guardrailConfig` on the request body: the passthrough door forwards it to
  Bedrock, which applies the guardrail to the response as well.
- `ApplyGuardrail` is format-independent, so one code path covers all the
  request shapes both doors accept. The text extracted for screening is the
  user-authored content of the request, truncated to bound the call.
- A guardrail intervention is logged as a `guardrail_blocked` decision like any
  other, so it shows up in the interceptor log group next to budget and policy
  decisions. The caller receives 403 `guardrail_intervened`; the two names are
  the log-side and client-side halves of the same refusal.
- Create the guardrail separately (console, CLI, or your own CDK). The stack
  references an existing guardrail; it does not define content policy.

## Stack outputs

| Output | What a client or operator does with it |
|---|---|
| `GatewayUrl` | Base URL. Inference clients use `<GatewayUrl>/inference`, native-SDK clients use `<GatewayUrl>/bedrock-runtime/model/{id}/invoke` or `/converse`. Both doors are on this one gateway. |
| `UserPoolId` | Create users and issue their JWTs with `admin-initiate-auth`. |
| `AppClientId` | The Cognito client id used when issuing those JWTs. |
| `TableName` | Write per-user `POLICY#<sub>` items to set budgets, blocks, rate limits, and `workspace_id`. |
| `InterceptorLogGroup` | Every governance decision is written here as one JSON log line. On a 403, read the exact requested model from it. |
| `InvocationLogGroupName` | The log group Bedrock invocation logs must be delivered to. Point account-level logging here. |
| `BedrockLoggingRoleArn` | The IAM role ARN the account-level Bedrock invocation logging configuration must reference. |

See `../recipes/` for runnable client examples and the policy item shape.

## Checks

`npm run check` synthesizes both configurations of the construct and asserts
four invariants: that `iam:PassRole` and the `bedrock-mantle` actions stay
scoped, that every function is on the Python runtime cdk-nag's AwsSolutions-L1
rule expects, that `existingGatewayId` without `existingGatewayRoleArn` is
rejected at synth, and that the guardrail context reaches the interceptor's
environment. It exits non-zero on the first failed assertion. No AWS access
needed.

```bash
cd infra
npm run check
```

`cdk synth` is a check in its own right: `bin/app.ts` applies cdk-nag's
`AwsSolutionsChecks` on every synth. In `lib/nag-suppressions.ts`, a
suppression whose construct path no longer matches causes synth to fail.

## IAM requirements

- The interceptor role carries exactly the six DynamoDB actions a
  `TransactWriteItems` needs: `GetItem`, `PutItem`, `UpdateItem`, `DeleteItem`,
  `ConditionCheckItem`, and `TransactWriteItems`. All six are required: a
  missing `ConditionCheckItem` causes an HTTP 503, from an
  `AccessDeniedException` inside the admission transaction.
- The L1 `CfnGateway` needs an explicit `lambda:InvokeFunction` grant on the
  interceptor. The L2 Gateway construct added this implicitly. With the L1 it must
  be spelled out, or every request fails with "Access denied while invoking Lambda
  function".
- The gateway role also needs the `bedrock-mantle` namespace, because the
  inference connector calls the `bedrock-mantle.<region>.api.aws` endpoint. The
  statement enumerates only the inference lifecycle plus the read side of the
  catalog, on `Resource: '*'`, because the mantle catalog is service-owned and
  has no account-scoped ARN to name. The remaining actions in that namespace
  either administer it or mutate something that outlives a request, and none of
  them is granted. The list is explicit, so actions added to the namespace later
  are not granted. The role also needs `bedrock:InvokeModel` and
  `bedrock:InvokeModelWithResponseStream` on Claude foundation models and on
  the `us.anthropic`, `global.anthropic` inference profiles (Claude Code in
  Bedrock mode defaults to `global.*`).

## Not included: the AgentCore Gateway Policy Engine

This CDK stack does not create a Policy Engine. Everything else in the
architecture (gateway, both targets, interceptor, both attribution pipelines,
DynamoDB, Cognito) is deployed by the one stack.

The Policy Engine and this interceptor cover different requirements, and the two
compose. Cedar evaluates each request in isolation, which covers per-user allow
and deny, model and operation allowlists, request-argument constraints, and time
windows. Cumulative token budgets, rate limits, and usage settlement need state
across requests, which is what the interceptor and its DynamoDB ledger provide.
If you attach a Policy Engine to this gateway, a passthrough target
needs a schema for `ENFORCE` mode to resolve actions against it.
