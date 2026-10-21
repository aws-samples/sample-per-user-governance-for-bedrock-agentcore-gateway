# Frameworks runtime — Docker-free CodeZip deploy

This documents the **CodeZip** deploy path for the `governance_sample_frameworks`
AgentCore Runtime (`frameworks/package_and_update.sh`). It is the **recommended**
way to deploy the frameworks runtime. The container/ECR path
(`frameworks/Dockerfile` + `frameworks/requirements.txt`) still exists as an
alternative, but there is no automated deploy script for it anymore — build and
push the image by hand if you need it. For most cases, use CodeZip.

## CodeZip vs container

Both paths produce the same AgentCore Runtime (`governance_sample_frameworks`,
`PYTHON_3_12`, `PUBLIC` network, `customJWTAuthorizer`, `Authorization` header
allowlisted) serving `app.py`'s `BedrockAgentCoreApp` entrypoint. The app is the
same in both: an always-on Strands multi-agent supervisor (orchestrator +
web-search sub-agent) that derives the gateway door from the model id. The two
paths differ only in how the code + dependencies reach AgentCore:

| | Container / ECR | CodeZip (`package_and_update.sh`) |
|---|---|---|
| Build tooling | Docker + buildx, ECR repo | none — `uv pip install --target`, `zip`, `s3 cp` |
| Artifact | linux/arm64 image in ECR | `deployment_package.zip` in S3 |
| Runtime config | `containerConfiguration` (image URI) | `codeConfiguration.code.s3` + `runtime: PYTHON_3_12` + `entryPoint: ["opentelemetry-instrument","app.py"]` |
| Dependencies | `frameworks/requirements.txt` (full set) | `frameworks/requirements-codezip.txt` (minimal) |
| Deploy automation | none (manual `docker build`/push, point a runtime at the image) | `frameworks/package_and_update.sh` |
| IAM (pull) | ECR auth + image-pull actions | S3 read of the code bucket (no ECR needed) |

The CodeZip artifact is arm64 wheels resolved for CPython 3.12 /
`aarch64-manylinux2014` plus `app.py` and `websearch_client.py`, zipped and
uploaded to a timestamped key under
`s3://acv-runtime-code-<account-id>-<region>/frameworks/`.
`opentelemetry-instrument` wraps the SDK app so Strands/Bedrock/tool spans still
export to the runtime's CloudWatch `spans` stream, matching the container path.

## Why CodeZip (Docker-free)

- **No Docker / no ECR.** The build is `uv pip install --target`, `zip`,
  `aws s3 cp`, and `create-agent-runtime`. Nothing needs a container daemon,
  buildx, or an ECR repository.
- **Avoids the numpy aarch64 wheel conflict.** The container path's
  `frameworks/requirements.txt` includes heavier dependencies (historically
  `langchain`/`langgraph`) that drag in a `numpy` pin. Under the CodeZip
  resolution (`uv pip install --only-binary=:all:` for `aarch64-manylinux2014`),
  that numpy pin has no usable prebuilt wheel and the resolution fails.
  `frameworks/requirements-codezip.txt` is therefore a **separate, minimal,
  fully-pinned set** containing only what `app.py`'s multi-agent path imports
  across all three doors (`messages` = anthropic, `responses`/`openai` = openai)
  plus AgentCore web search and ADOT — **no langchain, no langgraph, no numpy** —
  so the `--only-binary` install succeeds.

The app packaged on the CodeZip runtime is the full, current `app.py`: the
always-on Strands multi-agent supervisor with the web-search sub-agent, serving
all three doors (`messages` / `responses` / `openai`) via model-id derivation.
The trimmed dependency set drops only libraries `app.py` no longer imports.

## Deploy command and required env vars

Run from `runtime/frameworks/`. Config is passed as **env vars at create
time** (the container is stateless — no config file is baked in):

```bash
ACCOUNT_ID=<account-id> \
REGION=us-east-1 \
GATEWAY_URL="https://<gateway-host>/" \
WEBSEARCH_GATEWAY_URL="https://<websearch-gateway-host>/" \
WEBSEARCH_REGION=us-east-1 \
WEBSEARCH_TOOL_NAME="web-search-tool___WebSearch" \
DISCOVERY_URL="https://cognito-idp.us-east-1.amazonaws.com/<pool-id>/.well-known/openid-configuration" \
APP_CLIENT_ID=<cognito-app-client-id> \
./package_and_update.sh
```

Env vars used by the script:

- `ACCOUNT_ID` — AWS account. Optional; defaults to `sts get-caller-identity`.
- `REGION` — defaults to `us-east-1`; must match the gateway region. The script
  exports it as `AWS_REGION`/`AWS_DEFAULT_REGION`.
- `GATEWAY_URL` — governance gateway base (trailing `/`, no `/inference`
  suffix). Written into the runtime's env as `GATEWAY_URL`.
- `WEBSEARCH_GATEWAY_URL` — websearch gateway base for the web-search sub-agent.
- `WEBSEARCH_REGION` — defaults to `us-east-1`.
- `WEBSEARCH_TOOL_NAME` — defaults to `web-search-tool___WebSearch`.
- `DISCOVERY_URL` — Cognito OIDC discovery URL for the JWT authorizer.
  **Required** (the script exits if missing).
- `APP_CLIENT_ID` — Cognito app client id allowed by the authorizer.
  **Required** (the script exits if missing).
- `ROLE_ARN` — optional; runtime execution role. Defaults to
  `arn:aws:iam::<account-id>:role/agentcore-governance-runtime-role`.

The runtime execution role needs `bedrock-agentcore:InvokeGateway` on the
websearch gateway for the sub-agent's web search — this is the
`InvokeWebSearchGateway` statement in `iam/permissions.json` (placeholders
`REGION`/`ACCOUNT_ID`/`WEBSEARCH_GATEWAY_ID` substituted at apply time).

### Web-search sub-agent model comes from config.yaml

The web-search sub-agent's own model is **not** hard-coded in the script. At
deploy time `package_and_update.sh` reads `config/config.yaml` and resolves
`websearch_agent.model`, passing it into the runtime env as
`WEBSEARCH_AGENT_MODEL` (falling back to `openai.gpt-5.6-luna` if the key is
absent or unreadable). In the repo today `config/config.yaml` sets:

```yaml
websearch_agent:
  model: openai.gpt-5.6-luna
```

To change the sub-agent's model, edit `config/config.yaml` and re-run the
deploy. Per request, the caller may still override the sub-agent model in the
invoke payload (`websearch_model`); when empty, the sub-agent reuses the
orchestrator's model.

## Poll until READY

```bash
aws bedrock-agentcore-control list-agent-runtimes --region us-east-1 \
  --query "agentRuntimes[?agentRuntimeName=='governance_sample_frameworks'].status" \
  --output text
```

The script prints this command on completion. Wait until the status reads
`READY`. If `UpdateAgentRuntime` is denied for the calling identity, the script
automatically deletes and recreates the runtime, so an existing runtime may
briefly disappear from the list during a redeploy.

## Invoke

The runtime derives the gateway door from the model id, so a single invoke shape
covers every family — just change `model`:

```bash
aws bedrock-agentcore invoke-agent-runtime --region us-east-1 \
  --agent-runtime-arn <frameworks-arn> \
  --runtime-session-id "sample-$(uuidgen | tr -d - )" \
  --qualifier DEFAULT \
  --payload '{"prompt":"What are the latest AWS re:Invent keynote dates?","model":"openai.gpt-5.6-sol","base_url":"https://<gateway-host>/inference","jwt":"<user-jwt>"}' \
  /dev/stdout
```

- `base_url` must be the gateway `/inference` root (the OpenAI SDK appends
  `/v1/responses` or `/v1/chat/completions`; the Anthropic SDK appends
  `/v1/messages`). It must pass the `app.py` host check (AgentCore gateway
  hostname shape, or a `GATEWAY_HOST_SUFFIXES` entry).
- The web-search sub-agent is always available; the orchestrator calls it when
  the prompt needs current information. No flag selects it.
- SigV4 invocations (as above) carry the user JWT in the payload `jwt` field;
  HTTP invocations with `Authorization: Bearer <jwt>` need no payload `jwt`.
- Omit `model` to use the default (`openai.gpt-5.6-sol`, the `responses` door);
  pass an `anthropic.*` id for the `messages` door or a `gpt-oss`/open-weight id
  for the `openai` door.
