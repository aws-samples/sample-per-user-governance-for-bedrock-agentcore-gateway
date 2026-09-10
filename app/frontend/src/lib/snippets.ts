// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// The exact wiring each client needs. Two hops, one token: the browser
// POSTs to the AgentCore Runtime invocations endpoint with the user's JWT,
// and the agent inside the runtime reuses that same JWT against the
// governance gateway. The runtime containers run the same code, so what
// the drawer shows is the wiring each container uses.
import { ClientId } from "./types";

const BROWSER_FETCH = `// Browser: invoke the runtime directly with the user's JWT.
const url = \`https://bedrock-agentcore.\${REGION}.amazonaws.com/runtimes/\` +
  \`\${encodeURIComponent(RUNTIME_ARN)}/invocations?qualifier=DEFAULT\`;
await fetch(url, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sessionId, // 33+ chars
    Authorization: \`Bearer \${userJwt}\`, // the runtime forwards this
  },
  body: JSON.stringify(payload),
});`;

export const WIRING: Record<ClientId, { title: string; note: string; code: string }> = {
  strands_multi: {
    title: "Strands multi-agent on the runtime, through the gateway",
    note: "One turn, two governed calls: the orchestrator calls a researcher subagent built on the same gateway model, so BOTH model calls land as EVENT rows under the same user.",
    code: `${BROWSER_FETCH.replace("JSON.stringify(payload)", `JSON.stringify({
    mode: "strands_multi", prompt, model: PRIMARY_MODEL,
    base_url: GATEWAY_URL + "/inference",
  })`)}

# Runtime container: the subagent uses the SAME gateway model factory,
# so its call is governed and attributed exactly like the orchestrator's.
@tool
def researcher(question: str) -> str:
    """Ask the researcher sub-agent a question and return its answer."""
    sub_agent = Agent(model=gateway_model(user_jwt), ...)
    return str(sub_agent(question))

orchestrator = Agent(model=gateway_model(user_jwt), tools=[researcher], ...)
orchestrator(prompt)  # -> two EVENT rows, one user`,
  },
  strands: {
    title: "Strands on the runtime, through the gateway",
    note: "Browser sends the JWT to the runtime; Strands needs auth_token, never api_key.",
    code: `${BROWSER_FETCH.replace("JSON.stringify(payload)", `JSON.stringify({
    mode: "strands", prompt, model: PRIMARY_MODEL,
    base_url: GATEWAY_URL + "/inference",
  })`)}

# Runtime container: the same JWT arrives on the Authorization header
# (RequestContext.request_headers) and goes straight to the gateway.
from strands import Agent
from strands.models.anthropic import AnthropicModel

model = AnthropicModel(
    client_args={
        # auth_token sends Authorization: Bearer <user JWT>, which the
        # gateway's JWT authorizer requires. api_key would send x-api-key
        # and get a 401.
        "auth_token": user_jwt,
        "base_url": base_url,  # <gateway_url>/inference
        "max_retries": 0,      # surface 429/403 refusals immediately
    },
    model_id=model_id,
    max_tokens=700,
)
Agent(model=model)(prompt)  # every call lands as an EVENT row under this user`,
  },
  langgraph: {
    title: "LangGraph on the runtime, through the gateway",
    note: "Same browser call with mode langgraph; ChatAnthropic needs Bearer-only injected clients.",
    code: `${BROWSER_FETCH.replace("JSON.stringify(payload)", `JSON.stringify({
    mode: "langgraph", prompt, model: PRIMARY_MODEL,
    base_url: GATEWAY_URL + "/inference",
  })`)}

# Runtime container: reuse the inbound JWT with Bearer-only clients.
import anthropic
from langchain_anthropic import ChatAnthropic

llm = ChatAnthropic(
    model=model_id,
    base_url=base_url,
    # Placeholder; the real auth is Bearer-only via the injected clients.
    api_key="unused-gateway-uses-bearer",
)
# ChatAnthropic always forwards api_key, and the anthropic SDK sends
# x-api-key whenever api_key is set; the gateway rejects requests that
# carry both x-api-key and Authorization. _client/_async_client are
# cached properties, so inject Bearer-only SDK clients instead.
llm.__dict__["_client"] = anthropic.Client(
    auth_token=user_jwt, base_url=base_url)
llm.__dict__["_async_client"] = anthropic.AsyncClient(
    auth_token=user_jwt, base_url=base_url)`,
  },
  claudecode: {
    title: "Claude Code on the runtime, through the gateway",
    note: "Same browser call (no mode); inside the container it is environment only.",
    code: `${BROWSER_FETCH.replace("JSON.stringify(payload)", `JSON.stringify({
    prompt, model: PRIMARY_MODEL,
    base_url: GATEWAY_URL + "/inference",
  })`)}

# Runtime container: the inbound JWT becomes the CLI's environment.
export ANTHROPIC_BASE_URL="$BASE_URL"     # <gateway_url>/inference
export ANTHROPIC_AUTH_TOKEN="$USER_JWT"   # Bearer; never ANTHROPIC_API_KEY
export ANTHROPIC_MODEL="$MODEL_ID"
export ANTHROPIC_SMALL_FAST_MODEL="$MODEL_ID"
# The /inference door serves the stable Messages API, so skip the beta probes.
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

claude -p "..." --output-format json

# Budget sizing: Claude Code sends a very large request body (system
# prompt plus tool schemas), and the gateway estimates roughly one token
# per body byte at admission. Give Claude Code users a budget around
# 2,000,000 tokens per day, or every request 429s and the CLI retries
# until it times out.`,
  },
};
