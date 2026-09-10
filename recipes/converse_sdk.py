# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Native Bedrock SDK clients through the governed gateway's passthrough target.

Two variants of the same idea: point the SDK's endpoint at the gateway and
carry the user's JWT. The SDK keeps speaking its native Converse wire format
(model id in the URL path, inferenceConfig in the body); the gateway's
interceptor reads the JWT sub, enforces the budget, and forwards allowed
requests to bedrock-runtime with SigV4 from the gateway role.

The interceptor handles allow, transparent downgrade (Sonnet requested,
Haiku served), 429 budget_exceeded, and 403 access_denied. On a plain allow
it does not settle usage in band: the request is forwarded and the debit
arrives seconds later from the Bedrock invocation log. It reads Converse's
camelCase usage fields only when it serves a downgraded call itself.

boto3 signs every request with SigV4 by default, and the gateway expects a
Bearer JWT instead. The most direct native
client is therefore plain HTTPS (variant A). Variant B is a boto3
bedrock-runtime client with signing turned off and the JWT set by a
request-created hook.

Unlike the other recipes, this one has no /inference variant, because the
Converse wire format has no route on that door: the mantle connector serves the
Anthropic Messages and OpenAI shapes only, and answers a Converse path with 400
"Unsupported inference path". A native Bedrock client reaches the governed
gateway through /bedrock-runtime or not at all. To put an Anthropic-shaped
client on the mantle door instead, see raw_sdk.py.

Usage:
    export GATEWAY_URL="https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com"
    export GATEWAY_JWT="<cognito access token>"
    python3 recipes/converse_sdk.py
"""
import json
import os
import urllib.parse
import urllib.request

GATEWAY_URL = os.environ["GATEWAY_URL"]
GATEWAY_JWT = os.environ["GATEWAY_JWT"]
TARGET = "bedrock-runtime"
# Fixed rather than read from GATEWAY_MODEL. That variable is documented with a
# bare inference-door id (anthropic.claude-haiku-4-5), which this door rejects:
# the passthrough door takes provider-form ids only.
MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# The JWT travels in an Authorization header, so it must never leave over a
# non-https scheme. urllib.request.urlopen uses the default global opener,
# which handles file:, ftp: and data: as well as http(s), and opens whatever
# scheme the URL names. This opener is built from an empty OpenerDirector with
# only the HTTPS handlers added, so https is the only scheme it can open at
# all; UnknownHandler turns anything else into a raised URLError. The stack's
# Lambdas use the same https-only technique in infra/lambda/https_call.py, which
# additionally holds every redirect hop to an allowlist of AWS hosts. This
# opener does not, so it follows a redirect anywhere as long as it is https.
_HTTPS_ONLY = urllib.request.OpenerDirector()
for _handler in (
    urllib.request.HTTPSHandler(),
    urllib.request.HTTPRedirectHandler(),
    urllib.request.HTTPErrorProcessor(),
    urllib.request.HTTPDefaultErrorHandler(),
    urllib.request.UnknownHandler(),
):
    _HTTPS_ONLY.add_handler(_handler)


# --- Variant A: plain HTTPS, the exact Converse wire format -----------------
# This is what any Converse client sends; the only additions are the gateway
# host, the target-name path prefix, and the Bearer JWT.

def converse_via_gateway(prompt: str, max_tokens: int = 256) -> dict:
    url = f"{GATEWAY_URL}/{TARGET}/model/{MODEL}/converse"
    body = {
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GATEWAY_JWT}",
        },
    )
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("GATEWAY_URL must be https")
    with _HTTPS_ONLY.open(request, timeout=120) as response:
        return json.loads(response.read())


# --- Variant B: boto3 with a request-created hook ---------------------------
# boto3 SigV4-signs every request by default, and the signer needs credentials
# even though the gateway ignores the signature and wants the JWT. Two changes
# make that work with a JWT and no AWS credentials at all: UNSIGNED skips the
# signer, and a request-created hook sets the Authorization header. The hook is
# registered with register_last so it still wins if you drop UNSIGNED and let
# the signer run.
#
# This client is reusable as-is by any framework that accepts a prebuilt boto3
# client: langgraph_agent.py hands it to ChatBedrockConverse(client=...).
# Strands is the exception, because BedrockModel builds its own client and has no
# client parameter; there the hook goes on a Session instead, which
# strands_agent.py shows.

def make_governed_bedrock_client():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    client = boto3.client(
        "bedrock-runtime",
        # boto3 requires a region to build any client, even when endpoint_url
        # names the host outright. It is not used to authenticate anything here.
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        endpoint_url=f"{GATEWAY_URL}/{TARGET}",
        config=Config(signature_version=UNSIGNED),
    )

    def _swap_auth(request, **kwargs):
        request.headers["Authorization"] = f"Bearer {GATEWAY_JWT}"

    client.meta.events.register_last("request-created.bedrock-runtime", _swap_auth)
    return client


if __name__ == "__main__":
    print("Variant A: plain HTTPS Converse through the gateway")
    result = converse_via_gateway("Say exactly: GOVERNED")
    text = result["output"]["message"]["content"][0]["text"]
    usage = result["usage"]
    print(f"  reply: {text}")
    print(f"  usage: in={usage['inputTokens']} out={usage['outputTokens']}")

    print("Variant B: boto3 client, signing off, JWT set by hook")
    client = make_governed_bedrock_client()
    result = client.converse(
        modelId=MODEL,
        messages=[{"role": "user", "content": [{"text": "Say exactly: GOVERNED-BOTO3"}]}],
        inferenceConfig={"maxTokens": 32},
    )
    print(f"  reply: {result['output']['message']['content'][0]['text']}")
    print(
        f"  usage: {result['usage']['totalTokens']} tokens"
        " (debited to this user asynchronously from the Bedrock invocation log)"
    )
