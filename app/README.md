# Demo app: per-user governance for the AgentCore Gateway

Optional module. A React app and small backend that demonstrate the governance stack. Agent turns run through the live gateway with per-user JWTs, and all displayed values are read from the governance DynamoDB table. The app performs no enforcement: the gateway enforces, and the app renders the result.

## Architecture

Two paths, one token:

- Chat: the browser invokes the AgentCore Runtime directly (`POST https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<arn>/invocations`) with the signed-in user's Cognito JWT on the `Authorization` header. The agent inside the runtime (Strands, LangGraph, or Claude Code) reuses that same JWT against the gateway's `/inference` endpoint, so every model call is attributed to the user who typed the prompt. The Lambda backend is not in this path at all.
- Admin: personas, policy edits, per-user events, and the fleet view go browser -> API Gateway (Cognito JWT authorizer) -> Lambda -> DynamoDB. The Lambda runs no agents and holds no agent SDKs; it only reads the governance table (usage, events) and writes `POLICY#<sub>` items.

## What it shows

Two tabs.

- Live Demo: one chat pane with a framework selector (Strands, Strands
  multi-agent, LangGraph, Claude Code), a wire-format selector (Anthropic API,
  OpenAI API, Converse, Invoke -- each labeled with the gateway door it uses),
  and a model dropdown listing what the selected door actually serves (the
  backend reports each door's catalog; ids the door serves but the policy
  forbids are marked rather than hidden). Replies stream. Beside the chat, a
  live architecture map plays each hop of the real request, with a per-stage
  timeline view, and the DynamoDB card is a working policy editor: saving
  writes the real `POLICY#<sub>` item, one downgrade target per door and
  wire shape, and the gateway obeys it on the very next request. The enforcement ladder is all visible live: warn band on the
  meter, model downgrade past the threshold (a badge on the reply), `429
  budget_exceeded` with `retry_after` at the limit, `403 access_denied` for a
  hard-blocked user.
- Fleet: per-user daily aggregates from `USAGE#<sub>#<date>` and the recent
  request feed (admission rows plus in-band settlements), straight from the
  governance table.

## Prerequisites

1. The infra module of this repo deployed first. This app consumes exactly four of its CloudFormation outputs and nothing else: `GatewayUrl`, `UserPoolId`, `AppClientId`, `TableName`. It works against any stack that exposes those four outputs.
2. The frameworks runtime module (`runtime/frameworks`) deployed, created with a customJWTAuthorizer trusting the same user pool. Its ARN becomes `VITE_FRAMEWORKS_RUNTIME_ARN` in the frontend build; without it the Strands and LangGraph panes stay disabled.
3. AWS credentials for the same account and region, plus `node`, `npm`, and `docker` (the Lambda bundles its Python code at deploy time).
4. Anthropic model access enabled in the region for the models the gateway targets.
5. The Cognito app client must allow `ALLOW_USER_PASSWORD_AUTH` (the user signs in from the browser). The infra module enables it.

Optional: the claude-code runtime module (`runtime/claudecode`). Pass its runtime ARN at deploy time to enable the third pane. Without it, the pane is disabled and displays the reason.

## Quickstart

```bash
cd app
FRAMEWORKS_RUNTIME_ARN=<frameworks runtime ARN> \
CLAUDECODE_RUNTIME_ARN=<optional claude-code runtime ARN> \
./scripts/deploy.sh
```

The script runs two automated passes: it reads the infra stack outputs, deploys the app stack (API Lambda, demo Cognito users, static site), then builds the frontend against the new API URL with the runtime ARNs and gateway URL baked in and publishes it. Leave an ARN unset and the script prompts for it (Enter to skip).

Then set the demo user's password once. The app does not store or read it:

```bash
aws cognito-idp admin-set-user-password \
  --user-pool-id <UserPoolId> --username demo-presenter \
  --password '<choose a strong one>' --permanent
```

Open the printed site URL and sign in as `demo-presenter`.

Deploying by hand instead of the script:

```bash
cd app/cdk && npm ci
npx cdk deploy GovernanceDemoAppStack \
  --context gatewayUrl=<GatewayUrl> \
  --context userPoolId=<UserPoolId> \
  --context appClientId=<AppClientId> \
  --context tableName=<TableName> \
  --context frameworksRuntimeArn=<frameworks runtime ARN> \
  --context claudecodeRuntimeArn=<optional claude-code runtime ARN>
# then build the frontend with VITE_API_URL, VITE_AWS_REGION,
# VITE_APP_CLIENT_ID, VITE_GATEWAY_URL, VITE_PRIMARY_MODEL,
# VITE_FRAMEWORKS_RUNTIME_ARN, and VITE_CLAUDECODE_RUNTIME_ARN
# (plus VITE_USER_POOL_ID and VITE_TABLE_NAME) and deploy again to publish.
```

If the gateway names its models differently, override the model context keys and set `PRIMARY_MODEL` when running `deploy.sh` so the frontend build matches. Each door and wire shape takes its own downgrade target, so there are three fallback keys rather than one:

| Context key | Default | Used for |
| --- | --- | --- |
| `primaryModel` | `anthropic.claude-sonnet-5` | the model the demo calls first |
| `fallbackModel` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | downgrade on the passthrough door, which serves the regional inference-profile form |
| `mantleFallbackModel` | `anthropic.claude-haiku-4-5` | downgrade on the `/inference` door's Anthropic Messages shape, which serves the bare provider id |
| `openaiFallbackModel` | `gpt-oss-20b` | downgrade on the `/inference` door's OpenAI shapes, which serve a separate OSS catalog |

## Authentication and authorization

One token, three validators:

1. The browser signs in against the infra user pool with `USER_PASSWORD_AUTH` and holds only that user's access token.
2. Chat: the AgentCore Runtime is created with a customJWTAuthorizer trusting the same pool, so the browser's `Authorization: Bearer` header is validated at the runtime's front door. The runtime hands the same token to the agent, which presents it to the gateway; the gateway's JWT authorizer validates it again and governs by the token's `sub`.
3. Admin: every demo API route sits behind an API Gateway JWT authorizer that validates the same token against the same pool. There is no anonymous route anywhere.

The JWT never leaves the user's own request path, and the Lambda never mints or forwards tokens for anyone else.

## Backend API

| Route | What it does |
| --- | --- |
| `GET /personas` | The caller's persona with live policy and usage, capabilities, config |
| `GET /policy?persona=` | The caller's policy plus today's usage |
| `PUT /policy` | Writes `POLICY#<sub>`: `blocked`, `budget_tokens`, `downgrade_at_tokens`, `fallback_model`, `fallback_model_mantle`, `fallback_model_openai`, `allowed_models` |
| `GET /events/mine?since=` | The caller's `EVENT` rows at or after the epoch-seconds cutoff, newest last, max 20. The UI calls this after each turn to prove attribution |
| `GET /fleet` | Recent admission (REQ) rows merged with in-band EVENT rows plus per-persona daily aggregates. Fleet-wide, not caller-scoped: see below |
| `GET /timeline` | Settlement timeline for one request. `?requestId=` resolves any request id in the table; the `?since=` fallback is caller-scoped |

### Authorization model: authentication only

Every route requires a valid token from the pool, and no route is anonymous.
Authentication is, however, the whole of the access control: there is no role, no
admin claim, and no per-route check on who the caller is. This has two
consequences:

- **Two routes read the whole fleet, for any signed-in user.** `GET /fleet`
  returns every user's daily token usage, budget, block state, and recent
  admission rows. `GET /timeline?requestId=` resolves any request id in the
  table, whoever it belongs to, and answers with that request's `userId` and
  persona name. A demo user can therefore see what every other demo user spent.
  The demo's fleet view depends on this. Do not reuse it outside a demo.
- **Each user is a full self-service administrator of their own policy.**
  `PUT /policy` writes only the caller's own item (the persona is resolved from
  the caller's `sub`, so nobody can edit anyone else), but within that item there
  is no ceiling: a user can raise their own budget, widen their own allowlist,
  and clear their own hard block. The walkthrough steps that adjust policy values
  from the UI depend on this. In a real deployment, policy writes belong to
  an operator, on a separate authenticated surface, with the values bounded and
  the writes logged.

Before reusing this backend for anything but the demo, split the operator routes
from the user routes and gate them on a group or scope claim, and make the fleet
read operator-only.

There is no chat route. The frontend resolves `strands` and `langgraph` to the frameworks runtime (payload `{mode, prompt, model, base_url}`) and `claudecode` to the claude-code runtime (payload `{prompt, model, base_url}`), then reads the runtime's JSON response: `{text}` on success, or `{error_status, error_type, error_message, retry_after}` when the gateway refused. Refusals render with the gateway's own `error_type`, so a `429` shows as `budget_exceeded`, `rate_limit_exceeded`, or `outside_active_hours` as appropriate, with the body's `retry_after`, and a `403` as `access_denied`, `model_not_allowed`, or `guardrail_intervened`. `budget_exceeded` and `access_denied` double as the fallback labels, used when the runtime returns no `error_type`.

## Walkthrough

Sign in; the app opens on Live Demo:

1. Cheap baseline. Send the Small turn prompt with Strands on the Anthropic
   API format. The reply streams, the model badge names what actually served
   the turn, and the meter settles a few seconds later from the ledger.
2. Show the wiring. Click the Agent card in the request-flow map: the
   browser-to-runtime call with the Bearer JWT, then the exact gateway wiring
   inside the container for whichever framework is selected. Strands is one
   argument (`auth_token`); LangGraph needs the Bearer-only injected clients;
   Claude Code is environment variables only.
3. Switch doors. Send the same prompt over Converse (runtime); the model
   dropdown flips to inference-profile ids because each door takes a
   different id form. The Bedrock and Attribution cards update to the door's
   passthrough target and settlement pipe.
4. Burn budget. Run the Heavy turn once or twice and watch the meter cross
   the warn band.
5. Downgrade. Open the DynamoDB card, drag the downgrade threshold below
   current usage, save, and send a turn requesting the larger model. The
   reply badge shows the fallback with a "downgraded" tag, and the turn still
   streams.
6. Refusal. Drag the budget below current usage, save, send anything: a real
   `429 budget_exceeded` card with `retry_after`, zero spend.
7. Hard block. Toggle hard block, save, send: `403 access_denied`. Toggle it
   off; the next request goes through. Policies are table rows, not deploys.
8. Multi-agent attribution. Run the multi-agent fan-out prompt: one turn,
   several admission rows, all under the same user. Open Fleet to see the
   per-user attribution for the run.

## Cost note

The demo drives small Anthropic models with capped output tokens. A full run-through is typically well under a dollar of model usage; the largest single item is the token-heavy summarize prompt. The app stack itself (HTTP API, one small Lambda, S3 and CloudFront) costs cents at demo traffic; the runtimes bill per invocation. The Claude Code runtime requires a budget of approximately 2,000,000 tokens per user, because the CLI sends large request bodies.

## Teardown

```bash
cd app
./scripts/destroy.sh
```

Removes only the app stack (API, Lambda, demo users, site). The gateway, interceptor, governance table, user pool, and the runtimes belong to other modules and are not touched; tear those down from their own modules when you are done with the whole sample.

## Layout

```
app/
  backend/    Lambda: personas, policy reads/writes, event/fleet reads (no agents)
  cdk/        the app stack (consumes the four infra outputs as context)
  frontend/   React + Vite UI; invokes the AgentCore Runtimes directly for chat
  scripts/    deploy.sh / destroy.sh
```
