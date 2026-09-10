# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudFormation custom resource: attach the governance interceptor to an
existing AgentCore Gateway that this stack does not own.

UpdateGateway is a full replace, so the handler reads the gateway with
GetGateway, merges this function's REQUEST interceptor entry into the existing
interceptorConfigurations, writes the result back, and waits for the gateway to
return to READY. Only a REQUEST interceptor is
attached, because a RESPONSE interceptor forces the gateway to buffer the whole
response and defeats streaming (attribution runs asynchronously off Bedrock
invocation logs instead). On Delete it removes only the entries that point at
this stack's interceptor and leaves everything else untouched.

Requires a boto3 whose bedrock-agentcore-control model includes
interceptorConfigurations. If the managed runtime's bundled boto3 is older,
the resource fails cleanly with a ParamValidationError and the stack rolls
back without modifying the gateway.
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

import boto3
from botocore.exceptions import ClientError

from https_call import open_https

# Keys GetGateway returns that UpdateGateway accepts back.
_CARRY_KEYS = (
    "name",
    "description",
    "roleArn",
    "protocolType",
    "protocolConfiguration",
    "authorizerType",
    "authorizerConfiguration",
    "exceptionLevel",
    "kmsKeyArn",
)

_client = boto3.client("bedrock-agentcore-control")


def handler(event: dict[str, Any], context: Any) -> None:
    request_type = event["RequestType"]
    properties = event.get("ResourceProperties", {})
    gateway_id = properties["GatewayIdentifier"]
    function_arn = properties["FunctionArn"]
    physical_id = f"{gateway_id}-governance-interceptor"
    try:
        if request_type in ("Create", "Update"):
            stale = (
                event.get("OldResourceProperties", {}).get("FunctionArn")
                if request_type == "Update"
                else None
            )
            _apply(gateway_id, attach_arn=function_arn, detach_arns=[stale])
        elif request_type == "Delete":
            try:
                _apply(gateway_id, attach_arn=None, detach_arns=[function_arn])
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                    raise
        _respond(event, context, "SUCCESS", physical_id)
    except Exception as error:  # noqa: BLE001 - report every failure to CFN
        _respond(event, context, "FAILED", physical_id, reason=f"{type(error).__name__}: {error}"[:512])


def _apply(gateway_id: str, *, attach_arn: str | None, detach_arns: list[str | None]) -> None:
    gateway = _client.get_gateway(gatewayIdentifier=gateway_id)
    remove = {arn for arn in detach_arns if arn}
    if attach_arn:
        remove.add(attach_arn)
    configurations = [
        entry
        for entry in gateway.get("interceptorConfigurations") or []
        if _interceptor_arn(entry) not in remove
    ]
    if attach_arn:
        # REQUEST only. A RESPONSE interceptor would force the gateway to
        # buffer the entire response and break streaming; attribution runs
        # asynchronously off Bedrock invocation logs instead.
        configurations.append(
            {
                "interceptor": {"lambda": {"arn": attach_arn}},
                "interceptionPoints": ["REQUEST"],
                "inputConfiguration": {"passRequestHeaders": True},
            }
        )
    update: dict[str, Any] = {
        key: gateway[key] for key in _CARRY_KEYS if gateway.get(key) is not None
    }
    update["gatewayIdentifier"] = gateway_id
    update["interceptorConfigurations"] = configurations
    _client.update_gateway(**update)
    _wait_ready(gateway_id)


def _interceptor_arn(entry: dict[str, Any]) -> str | None:
    return entry.get("interceptor", {}).get("lambda", {}).get("arn")


def _wait_ready(gateway_id: str, timeout_seconds: int = 240) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = _client.get_gateway(gatewayIdentifier=gateway_id).get("status")
        if status == "READY":
            return
        if status == "FAILED":
            raise RuntimeError("gateway entered FAILED after the interceptor update")
        # Deliberate poll interval: gateway updates transition through
        # UPDATING for tens of seconds and there is no waiter API.
        time.sleep(5)  # nosemgrep: arbitrary-sleep
    raise TimeoutError("gateway did not return to READY in time")


def _respond(
    event: dict[str, Any],
    context: Any,
    status: str,
    physical_id: str,
    *,
    reason: str = "",
) -> None:
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason or f"See {context.log_stream_name}",
            "PhysicalResourceId": physical_id,
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "NoEcho": False,
            "Data": {},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        event["ResponseURL"],
        data=body,
        method="PUT",
        headers={"Content-Type": "", "Content-Length": str(len(body))},
    )
    # The only URL ever opened here is the CloudFormation presigned response
    # URL from the custom-resource event. open_https checks the scheme and host
    # and cannot open any scheme but https, so a malformed event cannot send
    # this PUT somewhere else.
    with open_https(request):
        pass
