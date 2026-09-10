# Agents on AgentCore Runtime, through the governed gateway

Optional module. The gateway governs any client without it; deploy this module
for the demo app, or as an example of a server-side agent.

This folder holds coding-agent clients that run on Amazon Bedrock AgentCore
Runtime and call Anthropic models through the AgentCore Gateway per-user
governance stack. Every model call carries the end user's identity, so per-user
attribution and budget enforcement apply when the agent runs server-side.

## What each runtime hosts

| Runtime | Image | Hosts |
|---|---|---|
| `governance_sample_frameworks` | `frameworks/` | Strands single-agent, Strands multi-agent (orchestrator + tool sub-agent), and a two-node LangGraph graph |
| `governance_sample_claudecode` | `claudecode/` | Native Claude Code, headless (`claude -p`), configured entirely through environment variables |

Both images are linux/arm64, run as a non-root user, and listen on port 8080
(the AgentCore Runtime contract). Agent runtime names cannot contain hyphens,
so these use underscores.

## Reproducible, patched images

Both Dockerfiles pin their inputs:

- The base is the Amazon ECR Public mirror of `python:3.12-slim`, pinned by the
  digest of its multi-arch index, so a rebuild resolves the same image bytes.
  Refresh it with
  `docker buildx imagetools inspect public.ecr.aws/docker/library/python:3.12-slim`.
- Each image applies `apt-get upgrade` at build time. A digest-pinned base is
  only rebuilt on the upstream cadence, so this clears the OS CVEs that already
  have fixed packages without giving up the pin. CVEs with no fixed package
  upstream are inherited. The slim variant is used because it carries a far
  smaller package set than the full `-bookworm` image.
- Python dependencies are pinned exactly, transitive set included, in each
  image's `requirements.txt`. The header of each file records the version
  ranges the pins were resolved from and the command to move them forward.
- The Claude Code CLI is pinned to a single release through the
  `CLAUDE_CODE_VERSION` build argument (`--build-arg CLAUDE_CODE_VERSION=...`
  to override). It is installed with `https://claude.ai/install.sh`, which is
  Anthropic's own installer for the CLI and therefore a third-party download in
  this repository's build: it fetches the requested release from
  `downloads.claude.ai` and verifies it against a published SHA256 before
  installing. The CLI is not distributed on PyPI or as a container image, so
  there is no first-party registry alternative. If your organization forbids
  network installs during image builds, mirror the verified binary internally
  and `COPY` it in instead.

The version ranges the pins were resolved from permit major versions these apps
have not been run against. When the pins or the base digest move, rebuild both
images and exercise every mode (`strands`, `strands_multi`, `langgraph`, and the
Claude Code runtime).

## Payload contract

Frameworks runtime:

```json
{
  "mode": "strands | strands_multi | langgraph",
  "prompt": "the user's prompt, executed as-is",
  "model": "<model id in the form the door expects: bare id on /inference, inference-profile id on /bedrock-runtime>",
  "base_url": "https://<gateway-host>/inference",
  "jwt": "optional fallback, see JWT flow"
}
```

Claude Code runtime: the same, minus `mode`.

Success responses: `{"mode": ..., "text": ...}` (frameworks) or
`{"return_code": 0, "timed_out": false, "stdout_tail": ..., "stderr_tail": ...}`
(claudecode). Gateway refusals (budget 429s, policy 403s) are surfaced as
structured data instead of exceptions:

```json
{"error_status": 429, "error_type": "budget_exceeded",
 "error_message": "Daily token budget exceeded", "retry_after": "3600"}
```

### What the entrypoints validate before acting on a payload

`prompt`, `model` and `base_url` arrive from the caller, and each one then
reaches something that trusts it. Both entrypoints validate all three before
either app builds a command or constructs a client, and return
`{"error_status": 400, "error_type": "bad_request"}` on a failure:

- `base_url` must be an `https` URL, carry no userinfo section, and have a host
  matching the AgentCore gateway hostname shape
  (`<id>.gateway.bedrock-agentcore.<region>.amazonaws.com`, or the
  `.amazonaws.com.cn` and `.api.aws` equivalents). This prevents the container
  from relaying the caller's JWT to a caller-specified host: the claudecode
  image puts `base_url` in the child process environment next to
  `ANTHROPIC_AUTH_TOKEN`, and the frameworks image hands it to the SDK clients
  as the endpoint with the token attached. A broader `.amazonaws.com` suffix
  would also admit caller-registrable hosts such as an S3 bucket or an API
  Gateway stage. For a gateway behind a custom domain, set
  `GATEWAY_HOST_SUFFIXES` to a comma-separated list of host suffixes, each with
  a leading dot. This replaces the default check, so list every host the
  container may reach.
- `model` must match `^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$`. The character set
  is broad because the two doors take different id forms. The leading character
  class prevents the value being read as an option by an argv parser
  (claudecode passes `--model <model>`) or as a path segment in a URL (boto3
  puts the id in the path on converse and invoke).
- `prompt` is capped at 100,000 characters, below the limits where an oversized
  value fails as an exec or transport error rather than a readable refusal.

Validation runs in the entrypoint rather than at each call site, so a rejected
payload never reaches an exec or an outbound call.

## JWT flow

The end user's Cognito JWT is the single identity for the whole chain:

1. The browser (or test client) authenticates against Cognito and gets a JWT.
2. It invokes the runtime with `Authorization: Bearer <jwt>`. The runtimes are
   created with a `customJWTAuthorizer` (`discoveryUrl` + `allowedClients`),
   so AgentCore validates that token at the front door.
3. Inside the container, the bedrock-agentcore SDK exposes the inbound
   `Authorization` header to the entrypoint via its `RequestContext`
   (second parameter named `context`, `context.request_headers["Authorization"]`).
   The apps read the JWT header-first, with the payload `jwt` key as a
   fallback for SigV4 invocations (boto3 `InvokeAgentRuntime` sends no bearer
   header).
4. The same token is reused as the Bearer credential on every Anthropic call
   to the gateway, which is how the gateway attributes usage to the user and
   enforces that user's budget.

The JWT lives in memory only. It is never logged, never written to disk,
never placed in argv, and every response is passed through a redaction helper
before being returned.

### Client auth requirements

Each requirement below addresses a specific failure mode.

- Strands `AnthropicModel`: `client_args` must use `auth_token`, not
  `api_key`. `auth_token` sends `Authorization: Bearer`; `api_key` sends
  `x-api-key` and gets a 401 from the gateway.
- LangChain `ChatAnthropic`: requires an `api_key` value, so it gets a dummy
  one, and Bearer-only `anthropic.Client(auth_token=...)` /
  `AsyncClient(auth_token=...)` instances are injected into
  `llm.__dict__["_client"]` and `llm.__dict__["_async_client"]` (they are
  cached properties). Otherwise the SDK sends `x-api-key` alongside
  `Authorization` and the gateway rejects the request.
- Retries: `max_retries=0` on every SDK client, and Strands throttle retries
  are capped by setting `MAX_ATTEMPTS = 1` on both
  `strands.event_loop.event_loop` and `strands.agent.agent` (the constant is
  from-imported into both namespaces, so patching one is not enough). This
  makes budget refusals surface in seconds instead of minutes.
- Claude Code through the gateway needs, at minimum:
  `ANTHROPIC_BASE_URL=<gateway>/inference`, `ANTHROPIC_AUTH_TOKEN=<jwt>`,
  `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`, every `ANTHROPIC_*_MODEL`
  variable set to the model id form the door expects, and a large token budget
  (~2M) for the calling user, because Claude Code pre-estimates roughly
  1 token per byte of its large request body.

## Files

```
runtime/
  frameworks/app.py           Strands + LangGraph entrypoint
  frameworks/Dockerfile
  frameworks/requirements.txt exact pins, transitive set included
  claudecode/app.py           Claude Code headless entrypoint
  claudecode/Dockerfile
  claudecode/requirements.txt exact pins, transitive set included
  iam/trust.json          bedrock-agentcore.amazonaws.com trust, account/region scoped
  iam/permissions.json    least privilege: ECR auth on *, ECR pull on the one repo, CloudWatch Logs
  deploy.sh               idempotent build + push + role + create/update runtimes
```

`iam/*.json` contain the placeholders `ACCOUNT_ID`, `REGION` and `REPO_NAME`;
`deploy.sh` substitutes them at deploy time. If you apply the policies by
hand, replace the placeholders yourself. The policy grants
`ecr:GetAuthorizationToken` on `*` (the API requires it), image-pull actions on
the single repository ARN, and CloudWatch Logs create and write scoped to
`/aws/bedrock-agentcore/*`.

## Deploy

Requirements: AWS CLI v2 with bedrock-agentcore-control support, Docker with
buildx, and credentials for the target account.

```bash
ACCOUNT_ID=123456789012 \
REGION=us-east-1 \
REPO_NAME=agentcore-governance-runtimes \
DISCOVERY_URL="https://cognito-idp.us-east-1.amazonaws.com/<pool-id>/.well-known/openid-configuration" \
APP_CLIENT_ID=<cognito-app-client-id> \
./deploy.sh
```

or with flags:

```bash
./deploy.sh --account-id 123456789012 --region us-east-1 \
  --repo-name agentcore-governance-runtimes \
  --discovery-url "https://cognito-idp.us-east-1.amazonaws.com/<pool-id>/.well-known/openid-configuration" \
  --app-client-id <cognito-app-client-id>
```

The script is idempotent: it creates the ECR repo, IAM role and runtimes only
if missing, and switches to `update-agent-runtime` when a runtime with the
same name already exists. It prints both runtime ARNs at the end. Runtimes
take two to three minutes to reach `READY`; poll with:

```bash
aws bedrock-agentcore-control get-agent-runtime --region $REGION \
  --agent-runtime-id <id> --query status
```

## Invoke

```bash
aws bedrock-agentcore invoke-agent-runtime --region $REGION \
  --agent-runtime-arn <frameworks-arn> \
  --runtime-session-id "sample-$(uuidgen | tr -d - )" \
  --qualifier DEFAULT \
  --payload '{"mode":"strands","prompt":"Say hello","model":"<model>","base_url":"https://<gateway-host>/inference","jwt":"<user-jwt>"}' \
  /dev/stdout
```

This is a SigV4 invocation, so the JWT rides in the payload. HTTP invocations
with `Authorization: Bearer <jwt>` need no payload `jwt`.

## Teardown

```bash
# 1. Delete both runtimes (find ids with list-agent-runtimes)
aws bedrock-agentcore-control delete-agent-runtime --region $REGION --agent-runtime-id <frameworks-id>
aws bedrock-agentcore-control delete-agent-runtime --region $REGION --agent-runtime-id <claudecode-id>

# 2. Delete the ECR repository and its images
aws ecr delete-repository --region $REGION --repository-name $REPO_NAME --force

# 3. Detach the policy, then delete role and policy
aws iam detach-role-policy --role-name agentcore-governance-runtime-role \
  --policy-arn arn:aws:iam::$ACCOUNT_ID:policy/agentcore-governance-runtime-policy
aws iam delete-role --role-name agentcore-governance-runtime-role
aws iam delete-policy --policy-arn arn:aws:iam::$ACCOUNT_ID:policy/agentcore-governance-runtime-policy
```
