# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudFormation custom resource: HTTP passthrough target on a gateway.

Creates, updates, and deletes an HTTP passthrough target via raw SigV4-signed
calls to the bedrock-agentcore-control API. A raw HTTP client is used instead
of boto3 because the SDK bundled into the Lambda runtime may predate the
passthrough member of HttpTargetConfiguration; the wire shape below is the
one the bedrock-agentcore-control API accepts.
"""
import json
import time
import urllib.error
import urllib.request
from typing import Any

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from https_call import open_https

SESSION = boto3.Session()
REGION = SESSION.region_name or "us-east-1"
ENDPOINT = f"https://bedrock-agentcore-control.{REGION}.amazonaws.com"


def handler(event: dict[str, Any], context: Any) -> None:
    props = event["ResourceProperties"]
    gateway_id = props["GatewayIdentifier"]
    request_type = event["RequestType"]
    physical_id = event.get("PhysicalResourceId", "")

    try:
        if request_type == "Create":
            result = _signed("POST", f"/gateways/{gateway_id}/targets", _target_body(props))
            physical_id = result["targetId"]
            _wait_ready(gateway_id, physical_id)
        elif request_type == "Update":
            _signed("PUT", f"/gateways/{gateway_id}/targets/{physical_id}", _target_body(props))
            _wait_ready(gateway_id, physical_id)
        else:  # Delete
            try:
                _signed("DELETE", f"/gateways/{gateway_id}/targets/{physical_id}")
            except urllib.error.HTTPError as error:
                # Idempotent teardown: gone already (404), or a rollback of a
                # failed create where the physical id was never a target id
                # (400 validation). Both count as deleted.
                if error.code not in (400, 404):
                    raise
        _respond(event, context, "SUCCESS", physical_id or "none")
    except urllib.error.HTTPError as error:
        detail = error.read().decode()[:300]
        _respond(event, context, "FAILED", physical_id or "failed",
                 reason=f"HTTP {error.code}: {detail}")
    except Exception as error:  # noqa: BLE001 - surface everything to CFN
        _respond(event, context, "FAILED", physical_id or "failed",
                 reason=f"{type(error).__name__}: {error}"[:512])


def _target_body(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": props["TargetName"],
        "description": props.get(
            "Description",
            "HTTP passthrough to bedrock-runtime for InvokeModel and Converse",
        ),
        "targetConfiguration": {
            "http": {
                "passthrough": {
                    "endpoint": props["Endpoint"],
                    "protocolType": "CUSTOM",
                }
            }
        },
        "credentialProviderConfigurations": [
            {
                "credentialProviderType": "GATEWAY_IAM_ROLE",
                "credentialProvider": {
                    "iamCredentialProvider": {
                        "service": props.get("SigningService", "bedrock"),
                        "region": REGION,
                    }
                },
            }
        ],
    }


def _signed(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    creds = SESSION.get_credentials().get_frozen_credentials()
    url = f"{ENDPOINT}{path}"
    data = json.dumps(body) if body is not None else None
    aws_request = AWSRequest(
        method=method, url=url, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(aws_request)
    http_request = urllib.request.Request(
        url, data=data.encode() if data else None,
        headers=dict(aws_request.headers), method=method)
    # ENDPOINT is the fixed regional bedrock-agentcore-control https endpoint
    # built at module load, so nothing caller-controlled reaches the URL.
    # open_https re-checks scheme and host anyway, so a refactor cannot widen
    # this into a general-purpose fetch.
    with open_https(http_request, timeout=30) as response:
        payload = response.read()
        return json.loads(payload) if payload else {}


def _wait_ready(gateway_id: str, target_id: str, timeout_s: int = 180) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        target = _signed("GET", f"/gateways/{gateway_id}/targets/{target_id}")
        status = target.get("status")
        if status == "READY":
            return
        if status in ("FAILED", "CREATE_FAILED", "UPDATE_FAILED"):
            raise RuntimeError(f"target entered {status}: {target.get('statusReasons')}")
        # Deliberate poll interval: target creation transitions through
        # CREATING for tens of seconds and there is no waiter API.
        time.sleep(5)  # nosemgrep: arbitrary-sleep
    raise RuntimeError("timed out waiting for target READY")


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
            "Data": {"TargetId": physical_id},
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
