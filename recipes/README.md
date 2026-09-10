# recipes: clients through the governed gateway

Runnable clients for both doors of the gateway. Each one begins with a comment
identifying the lines that must change to move an existing Anthropic- or
Bedrock-compatible client onto the governed gateway.

The `/inference` door (bedrock-mantle inference connector) speaks the Anthropic
Messages and OpenAI wire formats. The `/bedrock-runtime` door (HTTP passthrough)
speaks the native Bedrock Converse and InvokeModel formats.

Every client is shown on both doors, so you can pick the door and keep the
client. Each Python recipe runs its two variants back to back in one go; Claude
Code gets one script per door, because the door is selected by environment
variables that cannot both be set at once.

| Client | Recipe | `/inference` (mantle) | `/bedrock-runtime` (passthrough) |
|---|---|---|---|
| `anthropic` SDK | `raw_sdk.py` | `anthropic.Anthropic(auth_token=JWT, base_url=...)` | `anthropic.AnthropicBedrock(api_key=JWT, base_url=...)` |
| Strands | `strands_agent.py` | `AnthropicModel(client_args={"auth_token": ...})` | `BedrockModel(boto_session=..., endpoint_url=...)` |
| LangGraph | `langgraph_agent.py` | `ChatAnthropic` plus Bearer-only clients injected into `__dict__` | `ChatBedrockConverse(client=<governed boto3 client>)` |
| Claude Code | `claude_code.sh`, `claude_code_bedrock_mode.sh` | `ANTHROPIC_BASE_URL` plus `ANTHROPIC_AUTH_TOKEN` | `CLAUDE_CODE_USE_BEDROCK=1` plus `CLAUDE_CODE_SKIP_BEDROCK_AUTH=1` |
| boto3 / plain HTTPS Converse | `converse_sdk.py` | not reachable, see below | endpoint at the gateway, SigV4 swapped for a Bearer JWT |

The one client with a single door is the native Bedrock one. The Converse wire
format has no route on the `/inference` door: the mantle connector serves the
Anthropic Messages and OpenAI shapes only, and answers a Converse path with 400
`invalid_request_error`, "Unsupported inference path". A native Bedrock client
therefore reaches the governed gateway through `/bedrock-runtime`.

Watch the parameter names on the `anthropic` SDK, because they invert between
its two client classes. On `Anthropic`, `api_key` means `x-api-key` and
`auth_token` means Bearer. On `AnthropicBedrock`, `api_key` means Bearer (it is
the same mechanism as `AWS_BEARER_TOKEN_BEDROCK`) and there is no `auth_token`.
Both doors want the Bearer form, so the correct parameter is `auth_token` for
one class and `api_key` for the other.

Every recipe reads `GATEWAY_URL` and `GATEWAY_JWT`:

```bash
export GATEWAY_URL="https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com"  # GatewayUrl output
export GATEWAY_JWT="eyJ..."                                                                  # see below
export GATEWAY_MODEL="anthropic.claude-haiku-4-5"                                            # /inference variants only
```

The JWT is the only credential any client needs. No recipe uses AWS credentials,
an AWS profile, or SigV4, and that includes every variant that speaks the native
Bedrock formats: the gateway signs the upstream call with its own role, so the
client just presents a Bearer token. Each `/bedrock-runtime` variant reaches that
state a different way. The boto3-based ones (`converse_sdk.py`,
`langgraph_agent.py`, `strands_agent.py`) pass
`Config(signature_version=UNSIGNED)` and set the header from a `request-created`
hook. `raw_sdk.py` relies on `AnthropicBedrock` skipping SigV4 whenever
`api_key` is set. `claude_code_bedrock_mode.sh` uses
`CLAUDE_CODE_SKIP_BEDROCK_AUTH=1`. None of the Python recipes read AWS
configuration. Callers present an identity token rather than cloud credentials.

`GATEWAY_MODEL` is an optional override honored only by the `/inference`
variants, because its value is a bare inference-door id. The `/bedrock-runtime`
variants ignore it and set their own provider-form ids: the Python recipes pin
one in the script, and `claude_code_bedrock_mode.sh` uses Claude Code's own
`ANTHROPIC_MODEL` and `ANTHROPIC_SMALL_FAST_MODEL`. A recipe that runs both
variants therefore sends two different model ids in one run, so give the user
both forms in `allowed_models`.

## Model ids are matched verbatim, and differ per door

The interceptor matches the requested model against the user's `allowed_models`
as an exact string. It does no prefix stripping and no aliasing: the id a client
sends must appear in `allowed_models` unchanged. There are no target-qualified
ids, so a value such as
`governance-inference/anthropic.claude-haiku-4-5` will not match.

The two doors take different forms of the id:

- `/inference` (mantle) takes bare ids: `anthropic.claude-haiku-4-5`,
  `anthropic.claude-sonnet-5`, `gpt-oss-120b`. The `/inference` variants default
  to `anthropic.claude-haiku-4-5`.
- `/bedrock-runtime` (passthrough) takes provider-form ids:
  `us.anthropic.claude-haiku-4-5-20251001-v1:0`. Every `/bedrock-runtime` variant
  uses that form. Claude Code in Bedrock mode defaults to `global.*` cross-region
  profiles, so those ids must be in `allowed_models` too (see
  `claude_code_bedrock_mode.sh`).

Because each Python recipe exercises both doors in one run, the user running it
needs both id forms allowed. The `allowed_models` example at the end of this file
already lists them.

On a 403 `model_not_allowed`, read the exact requested model from the interceptor
CloudWatch log and match `allowed_models` to that string rather than inferring
which form the client sent. Claude Code in particular can substitute a model
id before it leaves the machine: a `settings.json` `availableModels` allowlist
falls back to the account default when the configured id does not match it
verbatim, and `enforceAvailableModels: false` does not disable that
substitution.

## Streaming is preserved on every method

There is no RESPONSE interceptor anywhere in this architecture, so a
streaming request streams end to end; governance runs entirely at
admission. This holds for every streaming method: Anthropic Messages and
OpenAI Chat Completions on the `/inference` door, ConverseStream and
InvokeModelWithResponseStream on the `/bedrock-runtime` door, and Claude
Code in either mode.

A non-streaming request (Converse, InvokeModel, `stream: false`) returns one
buffered body because the client requested one. Two exceptions apply: a budget
downgrade on a streamed
`/bedrock-runtime` request is answered by the interceptor in valid
eventstream framing but arrives as one burst, and agents wrapping a CLI as a
subprocess must relay the CLI's streaming output themselves
(`runtime/claudecode/app.py` shows the pattern for Claude Code's
`--output-format stream-json`).

Verify on your own deployment: send a streaming request and watch chunk
arrival times.

```bash
curl -sN -X POST "$GATEWAY_URL/inference/v1/messages" \
  -H "Authorization: Bearer $GATEWAY_JWT" -H "Content-Type: application/json" \
  -d '{"anthropic_version":"bedrock-2023-05-31","model":"anthropic.claude-haiku-4-5",
       "max_tokens":300,"stream":true,
       "messages":[{"role":"user","content":"Count from 1 to 30, one number per line."}]}' \
  | while IFS= read -r line; do echo "$(date +%s.%N) $line"; done | head -20
```

Timestamps spread across the response mean streaming; all timestamps within
a few milliseconds of each other mean something buffered the reply.

## Get a JWT from the created Cognito pool

Self sign-up is disabled, so users are created with admin calls.
`UserPoolId` and `AppClientId` are stack outputs.

```bash
POOL_ID=<UserPoolId>
CLIENT_ID=<AppClientId>
USERNAME=demo-engineer
PASSWORD='use-a-16+char-password-with-Aa1!'

# One-time user creation.
aws cognito-idp admin-create-user \
  --user-pool-id "$POOL_ID" --username "$USERNAME" --message-action SUPPRESS
aws cognito-idp admin-set-user-password \
  --user-pool-id "$POOL_ID" --username "$USERNAME" \
  --password "$PASSWORD" --permanent

# Issue a JWT (valid 60 minutes).
export GATEWAY_JWT=$(aws cognito-idp admin-initiate-auth \
  --user-pool-id "$POOL_ID" --client-id "$CLIENT_ID" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters USERNAME="$USERNAME",PASSWORD="$PASSWORD" \
  --query 'AuthenticationResult.AccessToken' --output text)
```

The governance identity is the JWT `sub` claim, a Cognito-generated UUID.
Read it with:

```bash
aws cognito-idp admin-get-user --user-pool-id "$POOL_ID" --username "$USERNAME" \
  --query "UserAttributes[?Name=='sub'].Value" --output text
```

## Token expiry and refresh

Access tokens are short-lived; the sample's Cognito pool issues 60-minute
tokens, and most identity providers default to something similar. When the token
expires, the gateway returns 401 at its door. The recipes above are one-shot
scripts, so each run requires a new token. A long-running agent needs a refresh
path, and where it belongs depends on who holds the token:

- Interactive apps: refresh where the user session lives, before handing the
  token to the agent. The demo app does this in the browser with the Cognito
  refresh token and passes the agent a current access token on every turn.
  Agent turns are short compared to token lifetimes, so a token check at
  turn start is usually sufficient.
- Headless or long-running agents: wrap token acquisition in a small
  provider that caches the token and re-authenticates (or exchanges the
  refresh token) when the expiry is near; read expiry from the JWT `exp`
  claim rather than a fixed timer. Most SDKs accept a per-request header, so
  the provider can be consulted on each call. For agents that stream long
  responses, refresh with headroom (a couple of minutes before `exp`): the
  gateway validates the token at admission, so a token that is valid when
  the request is admitted stays good for the whole streamed response.
- On 401 from the gateway, re-authenticate and retry once; do not retry with
  the same token.

Token lifetime affects revocation latency. The gateway validates the token on
every request, so shorter tokens revoke access faster when a user is
off-boarded, at the cost of more refresh traffic. The governed identity is the
`sub` claim, which is stable across refreshes, so budgets and attribution are
unaffected by rotation.

For long-running interactive Claude Code sessions in Bedrock mode, use an
`apiKeyHelper` rather than exporting a token: point `apiKeyHelper` in
`~/.claude/settings.json` at a script that prints a fresh access token.
Claude Code caches the value in memory for a few minutes and re-runs the
helper on a 401, so a short token lifetime never strands a session, and no
token is written to disk. In Bedrock mode the helper's value is sent as the
Bearer Authorization header, which is the only carrier the gateway accepts.
Do not use `apiKeyHelper` with `ANTHROPIC_BASE_URL` (Anthropic API mode):
there the CLI sends the value as `x-api-key`, which never authenticates.
x-api-key alone is refused by the gateway's authorizer with 401 "Missing
Bearer token", and x-api-key sent alongside a valid Authorization header is
refused on the `/inference` door with 401 `authentication_error`, "request
must not include both 'authorization' and 'x-api-key' headers".
Anthropic-API-mode sessions use `ANTHROPIC_AUTH_TOKEN`, re-exported when it
expires.

Send the **access** token, not the id token. The authorizer is configured with
`allowedClients`, matched against the token's `client_id` claim; a Cognito id
token carries `aud` instead and is refused with 403 `insufficient_scope`
("requires higher privileges than provided by the access token"). That
refusal is about the token type, not about headers.

## Troubleshooting

### A 401 at the door looks like an AWS credentials problem

A missing or mistyped token source makes Claude Code sit silent for minutes,
then blame AWS credentials, with the real verdict buried at the end of the
output:
`401 {"success":false,"error":"Missing Bearer token"}`. No decision log entry
is written for these requests: the gateway's authorizer rejects them before the
interceptor runs. An empty decision log combined with a credentials error means
the token never reached the gateway; fix the token source and restart the
session.

### 429 and 403 surface as a retry ladder, not an immediate error

Claude Code retries governance refusals with backoff, printing lines like
`429 Token budget for this day exceeded, Retrying in 29s (attempt 7/10)` and
giving up with `API Error: Request rejected (429)` only after the attempts
run out. The interceptor reads policy on every request, so lifting a block
or raising a budget while the countdown is running lets the very next retry
succeed in the same session. Non-interactive scripts should cap this
(`CLAUDE_CODE_MAX_RETRIES=1`) so a refusal surfaces in seconds.

### Agent SDKs absorb 429 refusals the same way

This is not specific to Claude Code. Strands maps a 429 to
`ModelThrottledException` and retries it up to six
times with exponential backoff; the Anthropic SDK and botocore each retry on
their own layer as well. The visible symptom is a slow turn instead of an error,
and for a per-minute rate limit the backoff can outlast the minute and land in a
fresh bucket, so the turn eventually succeeds. A test that asserts enforcement
must therefore disable retries first: `Agent(retry_strategy=None)` for Strands,
`max_retries=0` on an Anthropic
client, `Config(retries={"max_attempts": 1})` for boto3. Confirm any verdict
against the interceptor log group rather than the client's view. botocore also
drops the response body on the passthrough door's unmodeled 429, so the
gateway's `error_type` is unreadable from boto3; read it from the log or from a
plain HTTPS call.

### Downgrades leave ValidationException records in the invocation log

The interceptor replays a downgraded call and drops body fields the fallback
model rejects, one per Bedrock 400; each rejected attempt writes an
invocation-log record with a ValidationException status and zero tokens. The
attribution Lambda ignores zero-token records, and the attempt that succeeded
carries the same `requestMetadata` and settles normally.

## Set a user's policy

Users without a policy item get the stack defaults. To govern a specific
user, write one item to the table (`TableName` output), keyed by their sub.
`allowed_models` can hold both door forms so the same user can call either door,
and every fallback must be one of the listed ids (the interceptor refuses the
whole policy with 503 `policy_invalid` otherwise):

```bash
aws dynamodb put-item --table-name <TableName> --item '{
  "pk":                    {"S": "POLICY#<sub>"},
  "blocked":               {"BOOL": false},
  "budget_tokens":         {"N": "500000"},
  "downgrade_at_tokens":   {"N": "450000"},
  "fallback_model":        {"S": "us.anthropic.claude-haiku-4-5-20251001-v1:0"},
  "fallback_model_mantle": {"S": "anthropic.claude-haiku-4-5"},
  "fallback_model_openai": {"S": "gpt-oss-20b"},
  "allowed_models":        {"SS": [
    "anthropic.claude-haiku-4-5",
    "anthropic.claude-sonnet-5",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-sonnet-5",
    "global.anthropic.claude-sonnet-5",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "gpt-oss-120b",
    "gpt-oss-20b"
  ]}
}'
```

Field meanings:

- `blocked`: `true` returns 403 `access_denied` on every request.
- `budget_tokens`: daily token budget. Exceeding it returns 429
  `budget_exceeded` with `retry_after` (seconds) in the JSON error body.
- `downgrade_at_tokens`: once the day's debit passes this, requests are
  rewritten to the door's fallback (only the `model` field changes; streaming
  is preserved).
- `fallback_model`: the downgrade destination for the `/bedrock-runtime` door,
  in that door's provider form (`us.` prefixed). Must be in `allowed_models`
  when that is set.
- `fallback_model_mantle`: the downgrade destination for the `/inference`
  door's Anthropic Messages shape, in that door's bare form. Absent, the door
  reuses `fallback_model` (which only works when that id is valid there).
- `fallback_model_openai`: the downgrade destination for the `/inference`
  door's OpenAI shapes (Chat Completions, Responses), which serve a disjoint
  OSS catalog and reject Claude ids. Absent, those requests are not
  downgraded rather than rewritten into a 400.
- `allowed_models`: optional allowlist; other models get 403
  `model_not_allowed`. Matched verbatim, so list every id form your clients
  send. The two `global.anthropic.*` entries above are the ids
  `claude_code_bedrock_mode.sh` sends by default; drop them only if no one uses
  Claude Code in Bedrock mode.
- `rate_limit_per_minute`: optional per-user requests-per-minute cap. A burst
  past it returns 429 `rate_limit_exceeded`. `0` or absent disables it.
- `workspace_id`: the user's `bedrock-mantle` project id (`proj_...`), used for
  `/inference`-door attribution. One project maps to one user, because the
  `AWS/BedrockMantle` metrics the attribution Lambda reads are dimensioned by
  project, not by user.

All governance fields are optional and fall back to the stack defaults. Claude
Code sends large request bodies (system prompt plus tool schemas) and can
generate long outputs, so give its users a generous daily budget, on the order
of 2,000,000 `budget_tokens`, rather than a chat-sized one.
