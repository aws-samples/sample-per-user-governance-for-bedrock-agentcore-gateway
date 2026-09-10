# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Raw Anthropic SDK through the governed gateway, on either door.

The same SDK reaches both doors, through two different client classes.

Variant A, the /inference door:
    anthropic.Anthropic(auth_token=JWT, base_url=GATEWAY_URL + "/inference")
    # auth_token sends Authorization: Bearer <JWT>, which the gateway requires.
    # Use auth_token, never api_key: api_key sends x-api-key instead, which the
    # gateway's authorizer rejects with 401 "Missing Bearer token".

Variant B, the /bedrock-runtime door:
    anthropic.AnthropicBedrock(api_key=JWT, base_url=GATEWAY_URL + "/bedrock-runtime")
    # Here api_key IS the Bearer header. AnthropicBedrock sets
    # Authorization: Bearer <api_key> and skips SigV4 entirely, so this needs no
    # AWS credentials. It is the same mechanism as AWS_BEARER_TOKEN_BEDROCK,
    # which the SDK reads when api_key is not passed.

The parameter names invert
between the two classes: on Anthropic, api_key means x-api-key and auth_token
means Bearer; on AnthropicBedrock, api_key means Bearer and there is no
auth_token. Passing api_key to AnthropicBedrock alongside explicit AWS
credential arguments raises ValueError, but ambient credentials in the
environment or a profile are not explicit arguments, so this works unchanged on
a machine that has AWS credentials configured.

The two classes also send different wire formats, which is why the doors differ:
Anthropic posts Anthropic Messages to {base_url}/v1/messages, while
AnthropicBedrock posts to {base_url}/model/{model}/invoke (or
/invoke-with-response-stream). Model ids differ per door as well, bare on
/inference and provider-form on /bedrock-runtime.

Governance refusals arrive as HTTP 429 or 403. The SDK retries 429 twice by
default, so a refusal shows up as a slow call rather than an error; max_retries=0
surfaces it immediately, as both variants do here.

Setup:
    pip install anthropic     # variant B also needs boto3, which anthropic pulls
                              # in as its [bedrock] extra: pip install anthropic[bedrock]
    export GATEWAY_URL=...    # GatewayUrl stack output
    export GATEWAY_JWT=...    # see recipes/README.md for issuing one
    python3 raw_sdk.py
"""
import os

import anthropic

GATEWAY_URL = os.environ["GATEWAY_URL"]
JWT = os.environ["GATEWAY_JWT"]
# Bare id for the /inference door. GATEWAY_MODEL is documented in this form, so
# it applies to variant A only.
MODEL = os.environ.get("GATEWAY_MODEL", "anthropic.claude-haiku-4-5")
# Provider-form id for the /bedrock-runtime door, which rejects the bare form.
NATIVE_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def inference_door_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(
        auth_token=JWT,
        base_url=GATEWAY_URL + "/inference",
        max_retries=0,
    )


def passthrough_door_client() -> "anthropic.AnthropicBedrock":
    return anthropic.AnthropicBedrock(
        api_key=JWT,
        base_url=GATEWAY_URL + "/bedrock-runtime",
        # AnthropicBedrock warns and defaults to us-east-1 when no region is
        # given. base_url already names the host, so the region only silences
        # the warning; nothing is signed with it.
        aws_region=os.environ.get("AWS_REGION", "us-east-1"),
        max_retries=0,
    )


def demo(label: str, client, model: str) -> None:
    print(label)

    message = client.messages.create(
        model=model,
        max_tokens=128,
        messages=[{"role": "user", "content": "Say hello in five words."}],
    )
    print("  reply:", message.content[0].text.strip())
    print(f"  usage: in={message.usage.input_tokens} out={message.usage.output_tokens}")

    # Streaming works on both doors; a policy downgrade rewrites only the model
    # and the stream still arrives incrementally.
    print("  stream:", end=" ", flush=True)
    with client.messages.stream(
        model=model,
        max_tokens=128,
        messages=[{"role": "user", "content": "Count to five."}],
    ) as stream:
        for text in stream.text_stream:
            print(text.replace("\n", " "), end="", flush=True)
    print()


if __name__ == "__main__":
    demo("Variant A: anthropic.Anthropic on /inference", inference_door_client(), MODEL)
    demo(
        "Variant B: anthropic.AnthropicBedrock on /bedrock-runtime",
        passthrough_door_client(),
        NATIVE_MODEL,
    )
