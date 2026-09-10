# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""LangGraph agent through the governed gateway, on either door.

The graph is the same either way; only the chat model changes.

Variant A, the /inference door, needs a workaround:
    llm.__dict__["_client"] = anthropic.Client(auth_token=JWT, base_url=BASE_URL)
    llm.__dict__["_async_client"] = anthropic.AsyncClient(auth_token=JWT, base_url=BASE_URL)

Why: ChatAnthropic always forwards an api_key, and the anthropic SDK sends
x-api-key whenever api_key is set. The JWT is only accepted as a Bearer
Authorization header, and the two headers cannot both be present: the
/inference door refuses that pair with 401 authentication_error, "request
must not include both 'authorization' and 'x-api-key' headers". x-api-key on
its own never authenticates either, since the gateway's authorizer rejects
the request with 401 "Missing Bearer token" before it reaches a target.
_client and _async_client are cached properties on ChatAnthropic,
so constructing it with a dummy api_key and injecting Bearer-only SDK clients
into __dict__ is the working pattern. This is a documented workaround for a
library gap: langchain-anthropic has no auth_token passthrough.

Variant B, the /bedrock-runtime door, needs no workaround, because
ChatBedrockConverse accepts a prebuilt boto3 client:
    ChatBedrockConverse(client=governed_bedrock_client(), model=NATIVE_MODEL)

The boto3 client is the one from converse_sdk.py: signing disabled, endpoint
pointed at the door, and the JWT set by a request-created hook.

Neither variant needs AWS credentials. The gateway signs the upstream call with
its own role; the client only presents a Bearer token. Model ids differ per
door, bare on /inference and provider-form on /bedrock-runtime.

Setup:
    pip install langgraph langchain-anthropic langchain-aws
    export GATEWAY_URL=...   # GatewayUrl stack output
    export GATEWAY_JWT=...   # see recipes/README.md for issuing one
    python3 langgraph_agent.py
"""
import os
from typing import TypedDict

from langgraph.graph import END, StateGraph

GATEWAY_URL = os.environ["GATEWAY_URL"]
JWT = os.environ["GATEWAY_JWT"]
# Bare id for the /inference door. GATEWAY_MODEL is documented in this form, so
# it applies to variant A only.
MODEL = os.environ.get("GATEWAY_MODEL", "anthropic.claude-haiku-4-5")
# Provider-form id for the /bedrock-runtime door, which rejects the bare form.
NATIVE_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


# --- Variant A: ChatAnthropic on the /inference door -------------------------

def inference_door_llm():
    import anthropic
    from langchain_anthropic import ChatAnthropic

    base_url = GATEWAY_URL + "/inference"
    llm = ChatAnthropic(
        model=MODEL,
        max_tokens=256,
        api_key="unused-gateway-uses-bearer",  # placeholder, never sent
        base_url=base_url,
    )
    llm.__dict__["_client"] = anthropic.Client(auth_token=JWT, base_url=base_url)
    llm.__dict__["_async_client"] = anthropic.AsyncClient(auth_token=JWT, base_url=base_url)
    return llm


# --- Variant B: ChatBedrockConverse on the /bedrock-runtime door -------------

def passthrough_door_llm():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    from langchain_aws import ChatBedrockConverse

    client = boto3.client(
        "bedrock-runtime",
        # boto3 requires a region to build any client, even when endpoint_url
        # names the host outright. It is not used to authenticate anything here.
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        endpoint_url=f"{GATEWAY_URL}/bedrock-runtime",
        # UNSIGNED drops SigV4, so no AWS credentials are needed for a signature
        # the gateway ignores anyway. The retry cap stops botocore from absorbing
        # 429 governance refusals.
        config=Config(signature_version=UNSIGNED, retries={"max_attempts": 1, "mode": "standard"}),
    )

    def _swap_auth(request, **kwargs):
        request.headers["Authorization"] = f"Bearer {JWT}"

    # register_last puts the hook after the signer, so it wins either way.
    client.meta.events.register_last("request-created.bedrock-runtime", _swap_auth)

    return ChatBedrockConverse(client=client, model=NATIVE_MODEL, max_tokens=256)


class State(TypedDict):
    question: str
    draft: str
    final: str


def build_graph(llm):
    def drafter(state: State) -> dict:
        reply = llm.invoke("Answer in one sentence: " + state["question"])
        return {"draft": str(reply.content)}

    def refiner(state: State) -> dict:
        reply = llm.invoke("Rewrite this more plainly: " + state["draft"][:200])
        return {"final": str(reply.content)}

    graph = StateGraph(State)
    graph.add_node("drafter", drafter)
    graph.add_node("refiner", refiner)
    graph.set_entry_point("drafter")
    graph.add_edge("drafter", "refiner")
    graph.add_edge("refiner", END)
    return graph.compile()


if __name__ == "__main__":
    question = "What does a token budget protect against?"

    print("Variant A: ChatAnthropic on /inference")
    print(" ", build_graph(inference_door_llm()).invoke({"question": question})["final"].strip())

    print("Variant B: ChatBedrockConverse on /bedrock-runtime")
    print(" ", build_graph(passthrough_door_llm()).invoke({"question": question})["final"].strip())
