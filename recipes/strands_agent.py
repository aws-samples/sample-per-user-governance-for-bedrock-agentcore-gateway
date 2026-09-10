# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Strands agent through the governed gateway, on either door.

Variant A (AnthropicModel, the /inference door) is the shorter one and the
default here. The line that matters:
    client_args={"auth_token": JWT, "base_url": GATEWAY_URL + "/inference"}
    # auth_token, NOT api_key: api_key sends x-api-key and the gateway's JWT
    # authorizer returns 401 Missing Bearer token.

Variant B (BedrockModel, the /bedrock-runtime door) needs three things instead,
because boto3 sits underneath it: a Session carrying the JWT swap hook, signing
disabled, and endpoint_url pointed at the door. BedrockModel builds its own
client from the Session, so there is no client parameter to hand a prebuilt one
to. Model ids differ per door as well, bare on /inference and provider-form on
/bedrock-runtime.

Neither variant needs AWS credentials. The gateway signs the upstream call with
its own role; the client only presents a Bearer token.

Governance refusals arrive as HTTP 429 (budget_exceeded, rate_limit_exceeded).
Strands maps a 429 to ModelThrottledException and retries it up to six times
with exponential backoff, so a refusal shows up as a slow turn rather than an
error, and a per-minute rate limit can be waited out into the next minute's
bucket. Pass retry_strategy=None to surface the refusal immediately, as this
recipe does. Leave the default in place when you want the client to ride out
transient throttling. On the /bedrock-runtime door cap botocore's own retries
too, or they stack underneath Strands.

Setup:
    pip install "strands-agents[anthropic]"   # boto3 comes with strands-agents
    export GATEWAY_URL=...   # GatewayUrl stack output
    export GATEWAY_JWT=...   # see recipes/README.md for issuing one
    python3 strands_agent.py
"""
import os

from strands import Agent

GATEWAY_URL = os.environ["GATEWAY_URL"]
JWT = os.environ["GATEWAY_JWT"]
# Bare id for the /inference door. GATEWAY_MODEL is documented in this form, so
# it applies to variant A only.
MODEL = os.environ.get("GATEWAY_MODEL", "anthropic.claude-haiku-4-5")
# Provider-form id for the /bedrock-runtime door, which rejects the bare form.
NATIVE_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Shared by both variants. None means "no retries", so a 429 governance refusal
# is raised instead of being absorbed by backoff. Remove for production retry
# behavior.
AGENT_KWARGS = {
    "system_prompt": "Answer in one short sentence.",
    "retry_strategy": None,
    # The default handler streams the reply to stdout as it arrives, which would
    # print it a second time alongside the labelled result below. Streaming is
    # unaffected either way; only the printing changes.
    "callback_handler": None,
}


# --- Variant A: AnthropicModel on the /inference door ------------------------

def inference_door_agent() -> Agent:
    from strands.models.anthropic import AnthropicModel

    model = AnthropicModel(
        client_args={
            "auth_token": JWT,
            "base_url": GATEWAY_URL + "/inference",
        },
        model_id=MODEL,
        max_tokens=256,
    )
    return Agent(model=model, **AGENT_KWARGS)


# --- Variant B: BedrockModel on the /bedrock-runtime door --------------------

def passthrough_door_agent() -> Agent:
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    from strands.models import BedrockModel

    # The hook must be on the Session, because BedrockModel creates the client
    # itself. register_last puts it after the signer, so it wins either way.
    session = boto3.Session(region_name=os.environ.get("AWS_REGION", "us-east-1"))

    def _swap_auth(request, **kwargs):
        request.headers["Authorization"] = f"Bearer {JWT}"

    session.events.register_last("request-created.bedrock-runtime", _swap_auth)

    model = BedrockModel(
        boto_session=session,
        # UNSIGNED drops SigV4, so no AWS credentials are needed for the
        # signature the gateway would ignore anyway. The retry cap stops
        # botocore from absorbing 429 refusals underneath Strands.
        boto_client_config=Config(
            signature_version=UNSIGNED,
            retries={"max_attempts": 1, "mode": "standard"},
        ),
        endpoint_url=f"{GATEWAY_URL}/bedrock-runtime",
        model_id=NATIVE_MODEL,
        max_tokens=256,
    )
    return Agent(model=model, **AGENT_KWARGS)


if __name__ == "__main__":
    print("Variant A: AnthropicModel on /inference")
    print(" ", str(inference_door_agent()("Say hello and name the model you are.")).strip())

    print("Variant B: BedrockModel on /bedrock-runtime")
    print(" ", str(passthrough_door_agent()("Say hello and name the model you are.")).strip())
