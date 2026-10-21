# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared CloudFormation custom-resource response helper.

`create_passthrough_target.py` and `attach_interceptor.py` each carry their own
copy of this logic. The handlers added to bring Phase-7 provisioning under
CloudFormation would have made that four copies, so the response plumbing lives
here once.

The contract is the one CloudFormation documents: PUT a JSON body to the
presigned `ResponseURL` from the event. Getting this wrong is the classic cause
of a stack wedged in `CREATE_IN_PROGRESS` for an hour, so `respond` is written
to be impossible to skip -- every exit path from a handler calls it, including
the failure path.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Mapping

from https_call import open_https

# CloudFormation truncates Reason at 4096 bytes, but the console shows far less
# and a giant traceback buries the useful first line. Keep it short enough to
# read in the events table.
_MAX_REASON = 512


def respond(
    event: Mapping[str, Any],
    context: Any,
    status: str,
    physical_id: str,
    *,
    reason: str = "",
    data: Mapping[str, Any] | None = None,
) -> None:
    """Send a custom-resource result to CloudFormation.

    Args:
        event: the raw custom-resource event (supplies ResponseURL, StackId,
            RequestId, LogicalResourceId).
        context: the Lambda context, used to name the log stream in the default
            reason so an operator can find the detail.
        status: ``SUCCESS`` or ``FAILED``.
        physical_id: the resource's physical id. Must be stable across updates,
            or CloudFormation issues a Delete for the old id and the resource is
            torn down behind you.
        reason: human-readable explanation, surfaced in the stack events.
        data: values to expose via ``Fn::GetAtt`` on the custom resource.
    """
    body = json.dumps(
        {
            "Status": status,
            "Reason": (reason or f"See {context.log_stream_name}")[:_MAX_REASON],
            "PhysicalResourceId": physical_id,
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "NoEcho": False,
            "Data": dict(data or {}),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        event["ResponseURL"],
        data=body,
        method="PUT",
        # CloudFormation's presigned URL rejects a signed request that carries a
        # Content-Type, so it is deliberately blanked rather than omitted.
        headers={"Content-Type": "", "Content-Length": str(len(body))},
    )
    # The only URL opened here is the presigned response URL from the event.
    # open_https enforces the scheme and host, so a tampered event cannot
    # redirect this PUT somewhere else.
    with open_https(request):
        pass


def log(message: str, **fields: Any) -> None:
    """Emit one structured JSON log line.

    Custom-resource failures are debugged from CloudWatch after the fact, so
    every decision these handlers make is logged as JSON rather than prose --
    it is the only record of why a deploy chose to skip, adopt, or overwrite.
    """
    print(json.dumps({"message": message, **fields}, default=str))
