# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudFormation custom resource: account-level Bedrock invocation logging.

Configures the account's Bedrock model-invocation logging as part of the
deploy, rather than asking the operator an interactive question and shelling
out to `aws bedrock put-model-invocation-logging-configuration`.

### Why this needs a custom resource

Bedrock model-invocation logging is configured **once per account per region**
-- it is a singleton with no CloudFormation resource type. That single slot is
also the thing that makes this risky: a naive deploy into an account that
already streams Bedrock logs somewhere will silently repoint them at this
stack's log group and break whatever was consuming them. The shell script
handled that by prompting a human. A CloudFormation deploy cannot prompt, so
the decision is encoded as an explicit ``Mode``.

### Modes

``reuse`` (default)
    Never touch an existing configuration. If the slot is empty, claim it; if
    something else owns it, leave it alone and report that via ``Action`` so the
    stack still succeeds. This is the safe default for a customer account,
    because the stack's own subscription filter is attached to the log group
    separately and additively -- attribution keeps working either way.

``force``
    Always write this stack's configuration, overwriting whatever is there.
    The caller is asserting the existing configuration is expendable.

``adopt``
    Overwrite, but only because the existing configuration was recognised as
    belonging to a previous run of this stack (a dead log group, or a delivery
    role named after the stack). Behaves like ``force`` at the API level; the
    distinction exists so the logs say which judgement was made.

### Delete

Delete clears the configuration **only if it still points at this stack's log
group**. If someone has since repointed Bedrock logging elsewhere, tearing down
this stack must not take their configuration with it.

Data delivery is metadata-only: all five body-delivery flags are false, so
prompts and completions are never written to CloudWatch. Only token counts and
request metadata -- which is all the attribution Lambda needs.
"""
from __future__ import annotations

from typing import Any

import boto3
from botocore.exceptions import ClientError

from cfn_response import log, respond

SESSION = boto3.Session()
REGION = SESSION.region_name or "us-east-1"
BEDROCK = SESSION.client("bedrock", region_name=REGION)

#: Physical id for this resource. The configuration is an account+region
#: singleton, so the id is derived from exactly that and never changes --
#: a stable id keeps CloudFormation from issuing a spurious Delete on update.
_PHYSICAL_PREFIX = "bedrock-invocation-logging"


def handler(event: dict[str, Any], context: Any) -> None:
    props = event.get("ResourceProperties", {})
    request_type = event["RequestType"]
    log_group = props.get("LogGroupName", "")
    role_arn = props.get("RoleArn", "")
    mode = (props.get("Mode") or "reuse").lower()
    physical_id = f"{_PHYSICAL_PREFIX}-{REGION}"

    try:
        if request_type == "Delete":
            action = _on_delete(log_group)
        else:
            action = _on_create_or_update(log_group, role_arn, mode)
        respond(
            event,
            context,
            "SUCCESS",
            physical_id,
            reason=f"invocation logging: {action}",
            data={"Action": action, "LogGroupName": log_group, "Mode": mode},
        )
    except Exception as error:  # noqa: BLE001 - every failure must reach CFN
        log(
            "invocation_logging_failed",
            request_type=request_type,
            error=f"{type(error).__name__}: {error}",
        )
        respond(
            event,
            context,
            "FAILED",
            physical_id,
            reason=f"{type(error).__name__}: {error}",
        )


def _current_config() -> dict[str, Any] | None:
    """The live logging configuration, or None when the slot is empty.

    Bedrock raises ValidationException rather than returning an empty document
    when nothing is configured, so that is translated to None here.
    """
    try:
        response = BEDROCK.get_model_invocation_logging_configuration()
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in (
            "ValidationException",
            "ResourceNotFoundException",
        ):
            return None
        raise
    config = response.get("loggingConfig")
    return config or None


def _cw_destination(config: dict[str, Any] | None) -> tuple[str, str]:
    """Extract ``(logGroupName, roleArn)`` from a configuration, or ``('','')``."""
    if not config:
        return "", ""
    cw = config.get("cloudWatchConfig") or {}
    return cw.get("logGroupName", ""), cw.get("roleArn", "")


def _apply(log_group: str, role_arn: str) -> None:
    """Point account-level invocation logging at this stack's group and role.

    Metadata-only on purpose: the attribution pipeline reads token counts, and
    writing prompt/response bodies into CloudWatch would turn an operational log
    into a data-retention liability.
    """
    BEDROCK.put_model_invocation_logging_configuration(
        loggingConfig={
            "cloudWatchConfig": {"logGroupName": log_group, "roleArn": role_arn},
            "textDataDeliveryEnabled": False,
            "imageDataDeliveryEnabled": False,
            "embeddingDataDeliveryEnabled": False,
            "videoDataDeliveryEnabled": False,
            "audioDataDeliveryEnabled": False,
        }
    )


def _on_create_or_update(log_group: str, role_arn: str, mode: str) -> str:
    if not log_group or not role_arn:
        # Reuse-an-existing-group deployments hand the stack no group of its
        # own to configure, which is a no-op rather than an error.
        log("invocation_logging_skipped", reason="no log group or role supplied")
        return "skipped_no_destination"

    config = _current_config()
    current_group, current_role = _cw_destination(config)

    if config is None:
        _apply(log_group, role_arn)
        log("invocation_logging_applied", log_group=log_group, reason="slot was empty")
        return "configured"

    if current_group == log_group:
        # Already ours. Re-apply so a changed delivery role is picked up, and so
        # the resource converges if the body-delivery flags were edited by hand.
        _apply(log_group, role_arn)
        log("invocation_logging_reapplied", log_group=log_group)
        return "reapplied"

    if mode in ("force", "adopt"):
        _apply(log_group, role_arn)
        log(
            "invocation_logging_overwritten",
            mode=mode,
            previous_group=current_group,
            previous_role=current_role,
            new_group=log_group,
        )
        return f"overwritten_{mode}"

    # mode == reuse, and a foreign configuration owns the slot. Leave it.
    # The stack's subscription filter still attaches to whichever group is
    # live, so per-user attribution is unaffected by this choice.
    log(
        "invocation_logging_left_alone",
        existing_group=current_group,
        existing_role=current_role,
        note="pass Mode=force to overwrite",
    )
    return "left_existing"


def _on_delete(log_group: str) -> str:
    """Clear the configuration only when it is still the one this stack set."""
    if not log_group:
        return "skipped_no_destination"
    try:
        config = _current_config()
    except ClientError as error:
        # A teardown must not fail because the read was denied; the worst case
        # is a leftover configuration pointing at a deleted log group.
        log("invocation_logging_delete_read_failed", error=str(error))
        return "skipped_read_failed"

    current_group, _ = _cw_destination(config)
    if config is None:
        return "already_cleared"
    if current_group != log_group:
        log(
            "invocation_logging_delete_skipped",
            existing_group=current_group,
            our_group=log_group,
            note="configuration no longer ours; leaving it in place",
        )
        return "left_foreign_config"

    BEDROCK.delete_model_invocation_logging_configuration()
    log("invocation_logging_cleared", log_group=log_group)
    return "cleared"
