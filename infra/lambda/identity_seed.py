# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudFormation custom resource: demo identities and their ledger policies.

Creates the demo Cognito users and the `POLICY#`/`GROUP#` ledger items that go
with them. One resource does the whole job because the
pieces are not separable: a `POLICY#` item is keyed on the Cognito `sub`, which
only exists once the user has been created. Splitting users and policies into
two custom resources would mean shuttling 41 opaque subs between them through
CloudFormation outputs.

What it provisions:

  1. **Group members** — `users_per_group` users for each group in
     `identity.groups`, named `<prefix>_userNN` per `model_catalog.username_for`.
  2. **The demo user** — one extra identity (default `demo-user`) that belongs
     to no group, matching what deploy.sh step 5 created.
  3. **`GROUP#<name>` items** — the per-group USD threshold and resolved model
     count the admin console reads.
  4. **`POLICY#<sub>` items** — per-user entitlement and budgets, with
     `allowed_models` resolved against the account's *live* ACTIVE catalog.

### Two deliberate changes from the Python

**Passwords come from Secrets Manager, never from CloudFormation.** The Python
read `DEMO_PASSWORD` from the environment. Passing it as a custom-resource
property instead would write it into the template, the change set, and the stack
events in plaintext. The handler is given a secret ARN and resolves the value at
runtime.

**`POLICY#` items are updated, not overwritten.** An unconditional `put_item`
would silently erase any attribute this handler does not know
about -- including `workspace_id`, the mantle project binding that another part
of the deploy writes to the very same item, which would break mantle
attribution on every redeploy. This uses `update_item` with an explicit SET list, so unmanaged
attributes survive. Config-managed fields (budgets, entitlement) are still
authoritative and do overwrite, because the config file is the declared state.
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Sequence

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

import model_catalog as mc
import usd_quota
from cfn_response import log, respond

# Cognito's admin APIs are throttled per user pool, and this resource makes
# roughly four calls per identity. Let botocore absorb the throttles with
# adaptive retries rather than hand-rolling a backoff loop.
_BOTO = BotoConfig(retries={"max_attempts": 10, "mode": "adaptive"})

SESSION = boto3.Session()
REGION = SESSION.region_name or "us-east-1"
COGNITO = SESSION.client("cognito-idp", region_name=REGION, config=_BOTO)
DDB = SESSION.client("dynamodb", region_name=REGION, config=_BOTO)
BEDROCK = SESSION.client("bedrock", region_name=REGION, config=_BOTO)
SECRETS = SESSION.client("secretsmanager", region_name=REGION, config=_BOTO)

#: Token budget seeded onto every policy. Matches DEMO_BUDGET_TOKENS in
#: scripts/deploy.sh.
BUDGET_TOKENS = 500_000
#: Soft threshold at which the interceptor downgrades to a cheaper model.
DOWNGRADE_AT_TOKENS = 450_000
#: Per-user requests-per-minute ceiling.
RATE_LIMIT_PER_MINUTE = 120

#: Fallbacks for the demo user, whose entitlement is a fixed list rather than a
#: group resolution. Verbatim from deploy.sh lines 50-82.
DEMO_FALLBACK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
DEMO_FALLBACK_MANTLE = "anthropic.claude-haiku-4-5"
DEMO_FALLBACK_OPENAI = "gpt-oss-20b"
DEMO_ALLOWED_MODELS = (
    "anthropic.claude-haiku-4-5",
    "anthropic.claude-sonnet-5",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-sonnet-5",
    "gpt-oss-120b",
    "gpt-oss-20b",
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def handler(event: dict[str, Any], context: Any) -> None:
    props = event.get("ResourceProperties", {})
    request_type = event["RequestType"]
    pool_id = props.get("UserPoolId", "")
    physical_id = event.get("PhysicalResourceId") or f"identity-seed-{pool_id}"

    try:
        if request_type == "Delete":
            result = {"Summary": _on_delete(props), "DemoSub": ""}
        else:
            result = _on_create_or_update(props)
        respond(
            event,
            context,
            "SUCCESS",
            physical_id,
            reason=f"identity seed: {result['Summary']}",
            # DemoSub is exported because the mantle-project resource keys its
            # workspace_id write on it. Publishing it here avoids a second
            # Cognito lookup and makes the dependency explicit in the template.
            data=result,
        )
    except Exception as error:  # noqa: BLE001 - every failure must reach CFN
        log(
            "identity_seed_failed",
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


# ---------------------------------------------------------------------------
# Catalog resolution
# ---------------------------------------------------------------------------
def _active_model_ids() -> list[str]:
    """Every ACTIVE foundation-model id in this account and region.

    Entitlement is resolved against the live catalog rather than a checked-in
    list, which is the point of the `families` globs: a model that becomes
    available (or is retired) is reflected on the next deploy.
    """
    ids: list[str] = []
    response = BEDROCK.list_foundation_models()
    for summary in response.get("modelSummaries", []):
        if (summary.get("modelLifecycle") or {}).get("status") == "ACTIVE":
            ids.append(summary["modelId"])
    return ids


def _inference_profile_ids() -> set[str]:
    """Every cross-region inference-profile id in this account and region.

    These are the ids the ``/bedrock-runtime`` door serves. Entitling a user on
    that door is what removes the need for a bedrock-mantle project per user:
    Bedrock writes model invocation logs only for the bedrock-runtime endpoint,
    so it is the only door where a request can be attributed to the user who
    made it. The mantle door's finest grain is per-project-per-day, which is
    why per-user metering there used to require one project each.

    Read live rather than assumed, because profile availability varies by
    region and by model. A failure here is non-fatal: the entitlement falls
    back to mantle-door forms only, which is the pre-existing behaviour.
    """
    ids: set[str] = set()
    try:
        paginator = BEDROCK.get_paginator("list_inference_profiles")
        for page in paginator.paginate():
            for summary in page.get("inferenceProfileSummaries", []):
                profile_id = summary.get("inferenceProfileId")
                if isinstance(profile_id, str) and profile_id:
                    ids.add(profile_id)
    except Exception as exc:  # noqa: BLE001 -- degrade, never fail the deploy
        log("inference_profiles_unavailable", error=type(exc).__name__)
        return set()
    return ids


def _pick_fallbacks(models: Sequence[str]) -> tuple[str, str, str]:
    """Per-door downgrade targets, chosen from the group's own entitlement.

    Each gateway door accepts a different id form, so one fallback cannot serve
    all three. Every fallback must also appear in `allowed_models`, or the
    interceptor rejects the whole policy with `503 policy_invalid` -- hence
    picking from `models` rather than from a global default.

    Note the OpenAI-door predicate chain has no catch-all: a group with no
    OpenAI-family model falls through to `models[0]`.
    """

    def first(predicates: Iterable[Any]) -> str:
        for predicate in predicates:
            for model in models:
                if predicate(model):
                    return model
        return models[0] if models else "anthropic.claude-haiku-4-5"

    generic = first(
        [
            lambda m: "haiku" in m,
            lambda m: "nova-micro" in m,
            lambda m: "nova" in m,
            lambda m: True,
        ]
    )
    mantle = first(
        [
            lambda m: m.startswith("anthropic.") and "haiku" in m,
            lambda m: m.startswith("anthropic."),
            lambda m: m.startswith("amazon.nova"),
            lambda m: True,
        ]
    )
    openai = first(
        [
            lambda m: "gpt-oss-20b" in m,
            lambda m: "gpt-oss" in m,
            lambda m: "openai" in m,
        ]
    )
    return generic, mantle, openai


# ---------------------------------------------------------------------------
# Cognito
# ---------------------------------------------------------------------------
def _password(secret_arn: str) -> str:
    """Resolve the shared demo password from Secrets Manager."""
    if not secret_arn:
        raise ValueError(
            "DemoPasswordSecretArn is required: the shared demo password must "
            "come from Secrets Manager, not from a CloudFormation property"
        )
    response = SECRETS.get_secret_value(SecretId=secret_arn)
    secret = response.get("SecretString") or ""
    if not secret:
        raise ValueError(f"secret {secret_arn} has no SecretString value")
    return secret


def _ensure_user(pool_id: str, username: str, password: str, group: str | None) -> str:
    """Create (or converge) one user and return its Cognito `sub`.

    `MessageAction=SUPPRESS` keeps Cognito from emailing a demo account, and the
    password is set `Permanent=True` so there is no FORCE_CHANGE_PASSWORD state
    for the app to negotiate. Re-running resets the password, which is what
    makes the whole resource idempotent.
    """
    try:
        COGNITO.admin_create_user(
            UserPoolId=pool_id, Username=username, MessageAction="SUPPRESS"
        )
    except COGNITO.exceptions.UsernameExistsException:
        pass  # Converge the existing user rather than failing the deploy.

    COGNITO.admin_set_user_password(
        UserPoolId=pool_id, Username=username, Password=password, Permanent=True
    )
    if group:
        COGNITO.admin_add_user_to_group(
            UserPoolId=pool_id, Username=username, GroupName=group
        )
    attributes = COGNITO.admin_get_user(UserPoolId=pool_id, Username=username)[
        "UserAttributes"
    ]
    for attribute in attributes:
        if attribute["Name"] == "sub":
            return attribute["Value"]
    raise RuntimeError(f"user {username} has no 'sub' attribute")


def _delete_user(pool_id: str, username: str) -> bool:
    """Delete one user, tolerating an already-absent user or pool."""
    try:
        COGNITO.admin_delete_user(UserPoolId=pool_id, Username=username)
        return True
    except COGNITO.exceptions.UserNotFoundException:
        return False
    except COGNITO.exceptions.ResourceNotFoundException:
        # The pool itself is gone (deleted earlier in the same teardown).
        return False


# ---------------------------------------------------------------------------
# DynamoDB ledger items
# ---------------------------------------------------------------------------
def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(int(value))}


def _put_group_item(
    table: str, group_name: str, budget_micro_usd: int, families: Sequence[str], model_count: int
) -> None:
    """Write `GROUP#<name>`: the group's threshold and resolved catalog size.

    A plain put_item is correct here -- unlike POLICY#, nothing else in the
    system writes to a GROUP# item, so there is nothing to preserve.
    """
    DDB.put_item(
        TableName=table,
        Item={
            "pk": _s(f"GROUP#{group_name}"),
            "item_type": _s("GROUP"),
            "budget_micro_usd": _n(budget_micro_usd),
            "model_count": _n(model_count),
            "families": {"SS": list(families) or ["*"]},
        },
    )


def _update_policy_item(
    table: str,
    sub: str,
    *,
    group_name: str | None,
    budget_micro_usd: int | None,
    allowed_models: Sequence[str],
    fallback_model: str,
    fallback_mantle: str,
    fallback_openai: str,
) -> None:
    """Write the config-managed fields of `POLICY#<sub>`, preserving the rest.

    This is the fix for the Python's `put_item`: `workspace_id` is written onto
    this same item by the mantle-project resource, and a blind overwrite erased
    it on every redeploy, quietly breaking mantle-door attribution. An explicit
    SET list touches only what this resource owns.
    """
    sets = [
        "#item_type = :item_type",
        "#blocked = :blocked",
        "#budget_tokens = :budget_tokens",
        "#downgrade_at = :downgrade_at",
        "#rate_limit = :rate_limit",
        "#fallback_model = :fallback_model",
        "#fallback_mantle = :fallback_mantle",
        "#fallback_openai = :fallback_openai",
    ]
    names = {
        "#item_type": "item_type",
        "#blocked": "blocked",
        "#budget_tokens": "budget_tokens",
        "#downgrade_at": "downgrade_at_tokens",
        "#rate_limit": "rate_limit_per_minute",
        "#fallback_model": "fallback_model",
        "#fallback_mantle": "fallback_model_mantle",
        "#fallback_openai": "fallback_model_openai",
    }
    values: dict[str, Any] = {
        ":item_type": _s("POLICY"),
        ":blocked": {"BOOL": False},
        ":budget_tokens": _n(BUDGET_TOKENS),
        ":downgrade_at": _n(DOWNGRADE_AT_TOKENS),
        ":rate_limit": _n(RATE_LIMIT_PER_MINUTE),
        ":fallback_model": _s(fallback_model),
        ":fallback_mantle": _s(fallback_mantle),
        ":fallback_openai": _s(fallback_openai),
    }

    if allowed_models:
        sets.append("#allowed_models = :allowed_models")
        names["#allowed_models"] = "allowed_models"
        # A DynamoDB string set must be non-empty, which is why this is
        # conditional rather than always written.
        values[":allowed_models"] = {"SS": sorted(set(allowed_models))}
    if group_name:
        sets.append("#group_name = :group_name")
        names["#group_name"] = "group_name"
        values[":group_name"] = _s(group_name)
    if budget_micro_usd is not None:
        sets.append("#budget_micro_usd = :budget_micro_usd")
        names["#budget_micro_usd"] = "budget_micro_usd"
        values[":budget_micro_usd"] = _n(budget_micro_usd)

    DDB.update_item(
        TableName=table,
        Key={"pk": _s(f"POLICY#{sub}")},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def _delete_item(table: str, pk: str) -> None:
    try:
        DDB.delete_item(TableName=table, Key={"pk": _s(pk)})
    except ClientError as error:
        # The table may already be gone during a stack teardown.
        if error.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise


# ---------------------------------------------------------------------------
# Create / Update
# ---------------------------------------------------------------------------
def _on_create_or_update(props: dict[str, Any]) -> dict[str, str]:
    pool_id = props["UserPoolId"]
    table = props["TableName"]
    groups: list[dict[str, Any]] = props.get("Groups") or []
    users_per_group = int(props.get("UsersPerGroup") or 0)
    exclude = props.get("TextExcludeSubstrings") or []
    demo_username = props.get("DemoUsername") or "demo-user"
    password = _password(props.get("DemoPasswordSecretArn", ""))

    started = time.time()
    active = _active_model_ids()
    # Fetched once for the whole run and threaded into every entitlement, so
    # each user is granted both the mantle-door id forms and the
    # bedrock-runtime inference-profile forms of the same models.
    profiles = _inference_profile_ids()
    resolved = mc.resolve_group_models(groups, active, exclude)
    log(
        "catalog_resolved",
        active_models=len(active),
        inference_profiles=len(profiles),
        groups={name: len(ids) for name, ids in resolved.items()},
    )

    org_default_micro = int(props.get("OrgDefaultMicroUsd") or 0)
    user_count = 0

    for group in groups:
        group_name = str(group.get("name", "")).strip()
        if not group_name:
            continue
        models = resolved.get(group_name, [])
        entitlement = mc.entitlement_ids(models, profiles)
        # The group's own threshold if config set one, else the org default.
        # resolve_effective_budget takes the max across a user's groups; each
        # seeded user belongs to exactly one, so the map has a single entry.
        group_micro = int(
            group.get("budgetMicroUsd")
            if group.get("budgetMicroUsd") is not None
            else org_default_micro
        )
        effective_micro = usd_quota.resolve_effective_budget(
            None, {group_name: group_micro}, org_default_micro
        )
        families = [str(f) for f in (group.get("families") or ["*"])]

        _put_group_item(table, group_name, group_micro, families, len(models))

        generic, mantle, openai = _pick_fallbacks(models)
        for index in range(1, users_per_group + 1):
            username = mc.username_for(group_name, index)
            sub = _ensure_user(pool_id, username, password, group_name)
            _update_policy_item(
                table,
                sub,
                group_name=group_name,
                budget_micro_usd=effective_micro,
                allowed_models=entitlement,
                fallback_model=generic,
                fallback_mantle=mantle,
                fallback_openai=openai,
            )
            user_count += 1

    # The demo user: no group, and a fixed entitlement rather than a resolved
    # one, so the smoke test in the deploy docs keeps working unchanged.
    demo_sub = _ensure_user(pool_id, demo_username, password, None)
    _update_policy_item(
        table,
        demo_sub,
        group_name=None,
        budget_micro_usd=None,
        allowed_models=DEMO_ALLOWED_MODELS,
        fallback_model=DEMO_FALLBACK_MODEL,
        fallback_mantle=DEMO_FALLBACK_MANTLE,
        fallback_openai=DEMO_FALLBACK_OPENAI,
    )
    user_count += 1

    elapsed = round(time.time() - started, 1)
    summary = (
        f"{user_count} users, {len(groups)} groups, "
        f"{len(active)} active models, {elapsed}s"
    )
    log("identity_seed_complete", summary=summary, demo_user=demo_username)
    return {"Summary": summary, "DemoSub": demo_sub}


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
def _on_delete(props: dict[str, Any]) -> str:
    """Remove the identities and ledger items this resource created.

    Orphaned demo users were one of the resources the script-based deploy left
    behind, so teardown is deliberately thorough. Every call tolerates an
    already-deleted pool or table, because CloudFormation may have removed them
    earlier in the same teardown.
    """
    pool_id = props.get("UserPoolId", "")
    table = props.get("TableName", "")
    groups: list[dict[str, Any]] = props.get("Groups") or []
    users_per_group = int(props.get("UsersPerGroup") or 0)
    demo_username = props.get("DemoUsername") or "demo-user"

    deleted_users = 0
    for group in groups:
        group_name = str(group.get("name", "")).strip()
        if not group_name:
            continue
        for index in range(1, users_per_group + 1):
            username = mc.username_for(group_name, index)
            # Read the sub before deleting so the POLICY# item can be removed.
            sub = _sub_for(pool_id, username)
            if _delete_user(pool_id, username):
                deleted_users += 1
            if sub and table:
                _delete_item(table, f"POLICY#{sub}")
        if table:
            _delete_item(table, f"GROUP#{group_name}")

    demo_sub = _sub_for(pool_id, demo_username)
    if _delete_user(pool_id, demo_username):
        deleted_users += 1
    if demo_sub and table:
        _delete_item(table, f"POLICY#{demo_sub}")

    summary = f"deleted {deleted_users} users and their ledger items"
    log("identity_seed_deleted", summary=summary)
    return summary


def _sub_for(pool_id: str, username: str) -> str | None:
    """The user's `sub`, or None when the user or pool no longer exists."""
    if not pool_id:
        return None
    try:
        attributes = COGNITO.admin_get_user(UserPoolId=pool_id, Username=username)[
            "UserAttributes"
        ]
    except (
        COGNITO.exceptions.UserNotFoundException,
        COGNITO.exceptions.ResourceNotFoundException,
    ):
        return None
    for attribute in attributes:
        if attribute["Name"] == "sub":
            return attribute["Value"]
    return None
