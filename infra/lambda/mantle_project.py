# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudFormation custom resource: the bedrock-mantle project (workspace).

Creates the project as part of the deploy, so no out-of-band AWS CLI call is
needed.

The mantle inference door attributes spend to a *project* rather than to an IAM
principal, so a request carries an `anthropic-workspace-id` header naming that
project. The interceptor stamps the header from `POLICY#<sub>.workspace_id`, and
without a project id there is nothing to stamp -- mantle-door traffic ends up
unattributed. Creating the project is therefore part of standing the system up,
not an optional extra.

### Why a custom resource rather than an SDK call

`bedrock-mantle` has no CloudFormation resource type, and at the time of writing
no boto3 client either. The control plane is a plain REST API, so this handler
signs its own SigV4 requests -- the same approach `create_passthrough_target.py`
already takes for the gateway target, and for the same reason: not depending on
what the Lambda runtime's bundled SDK happens to know about.

### Deliberately non-fatal

Mantle enforces a per-account project limit. The shell script treated a failed
create as a warning and carried on, because the rest of the stack is perfectly
usable without the mantle door. That behaviour is preserved: a create failure
logs, reports `Status=unavailable`, and still returns SUCCESS to CloudFormation.
Failing the stack over an optional attribution path would be the wrong trade.

### Idempotency

Projects are not addressable by name, so re-creating on every deploy would burn
through the account limit. Before creating, the handler reads `workspace_id`
back off the demo user's `POLICY#` item and reuses it if present -- exactly the
check the shell script did with `aws dynamodb get-item`.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

from cfn_response import log, respond
from https_call import open_https

SESSION = boto3.Session()
REGION = SESSION.region_name or "us-east-1"
DDB = SESSION.client("dynamodb", region_name=REGION)

#: The mantle control-plane host. Note `.api.aws`, not `.amazonaws.com`.
_ENDPOINT = f"https://bedrock-mantle.{REGION}.api.aws"
_PROJECTS_PATH = "/v1/organization/projects"
#: SigV4 signing name for the mantle control plane.
_SIGNING_SERVICE = "bedrock-mantle"
_TIMEOUT_SECONDS = 30


def handler(event: dict[str, Any], context: Any) -> None:
    props = event.get("ResourceProperties", {})
    request_type = event["RequestType"]
    table = props.get("TableName", "")
    demo_sub = props.get("DemoSub", "")
    project_name = props.get("ProjectName", "governance-demo-user")
    physical_id = event.get("PhysicalResourceId") or f"mantle-project-{project_name}"

    try:
        if request_type == "Delete":
            # Mantle projects are intentionally retained. They hold the
            # historical attribution record for spend already incurred, and the
            # per-account project limit makes deleting and recreating them on
            # every stack cycle actively harmful. The README documents the
            # manual removal step.
            log("mantle_project_retained", project_name=project_name)
            respond(
                event,
                context,
                "SUCCESS",
                physical_id,
                reason="mantle project retained by design",
                data={"Status": "retained", "WorkspaceId": ""},
            )
            return

        workspace_id, status = _ensure_project(table, demo_sub, project_name)
        respond(
            event,
            context,
            "SUCCESS",
            physical_id if workspace_id == "" else f"mantle-project-{workspace_id}",
            reason=f"mantle project: {status}",
            data={"Status": status, "WorkspaceId": workspace_id},
        )
    except Exception as error:  # noqa: BLE001
        # Even an unexpected failure must not break the deploy: the mantle door
        # is one of three, and the other two need nothing from this resource.
        log(
            "mantle_project_failed",
            request_type=request_type,
            error=f"{type(error).__name__}: {error}",
        )
        respond(
            event,
            context,
            "SUCCESS",
            physical_id,
            reason=f"mantle project unavailable: {type(error).__name__}",
            data={"Status": "unavailable", "WorkspaceId": ""},
        )


# ---------------------------------------------------------------------------
# Project lifecycle
# ---------------------------------------------------------------------------
def _ensure_project(table: str, demo_sub: str, project_name: str) -> tuple[str, str]:
    """Return ``(workspace_id, status)``, creating the project only if needed."""
    if not table or not demo_sub:
        log("mantle_project_skipped", reason="no table or demo sub supplied")
        return "", "skipped"

    existing = _existing_workspace_id(table, demo_sub)
    if existing:
        log("mantle_project_reused", workspace_id=existing)
        return existing, "reused"

    workspace_id = _create_project(project_name)
    if not workspace_id:
        return "", "unavailable"

    _record_workspace_id(table, demo_sub, workspace_id)
    log("mantle_project_created", workspace_id=workspace_id, project_name=project_name)
    return workspace_id, "created"


def _existing_workspace_id(table: str, demo_sub: str) -> str:
    """Read ``workspace_id`` off the demo user's POLICY# item, if set."""
    try:
        response = DDB.get_item(
            TableName=table,
            Key={"pk": {"S": f"POLICY#{demo_sub}"}},
            ProjectionExpression="workspace_id",
            ConsistentRead=True,
        )
    except ClientError as error:
        log("mantle_project_read_failed", error=str(error))
        return ""
    item = response.get("Item") or {}
    return (item.get("workspace_id") or {}).get("S", "")


def _create_project(project_name: str) -> str:
    """POST a new mantle project and return its id, or '' when unavailable."""
    body = json.dumps({"name": project_name}).encode("utf-8")
    try:
        response_body = _signed_post(_PROJECTS_PATH, body)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        # The common case is the per-account project limit. It is expected in a
        # re-used demo account and must not fail the deploy.
        log("mantle_project_create_rejected", http_status=error.code, detail=detail)
        return ""
    except Exception as error:  # noqa: BLE001
        log("mantle_project_create_failed", error=f"{type(error).__name__}: {error}")
        return ""

    payload = json.loads(response_body or "{}")
    project_id = payload.get("id") or ""
    if not project_id:
        log("mantle_project_no_id", payload_keys=sorted(payload.keys()))
    return project_id


def _record_workspace_id(table: str, demo_sub: str, workspace_id: str) -> None:
    """Attach the project id to the demo user's policy so the interceptor stamps it."""
    DDB.update_item(
        TableName=table,
        Key={"pk": {"S": f"POLICY#{demo_sub}"}},
        UpdateExpression="SET workspace_id = :w",
        ExpressionAttributeValues={":w": {"S": workspace_id}},
    )


# ---------------------------------------------------------------------------
# SigV4
# ---------------------------------------------------------------------------
def _signed_post(path: str, body: bytes) -> str:
    """SigV4-sign and send a POST to the mantle control plane."""
    credentials = SESSION.get_credentials()
    if credentials is None:
        raise RuntimeError("no AWS credentials available to sign the mantle request")
    frozen = credentials.get_frozen_credentials()

    url = f"{_ENDPOINT}{path}"
    aws_request = AWSRequest(
        method="POST",
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(frozen, _SIGNING_SERVICE, REGION).add_auth(aws_request)

    http_request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers=dict(aws_request.headers.items()),
    )
    # open_https pins the scheme and host, so a tampered endpoint cannot
    # redirect this signed request elsewhere.
    with open_https(http_request, timeout=_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8")
