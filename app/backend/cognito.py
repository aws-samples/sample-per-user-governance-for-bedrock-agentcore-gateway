# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Demo user roster and identity helpers.

The demo users are real Cognito users in the infra module's user pool. You
sign in to the app AS one of them, and the app forwards YOUR own access
token to the gateway (see handler.py). The gateway governs by the JWT `sub`
of whoever is signed in: auth flows cognito -> app -> gateway with a single
token, no server-side impersonation.

This module no longer mints tokens for the turn path. It keeps the roster
(names, roles, default budgets) and helpers to resolve the signed-in user
to roster metadata for display and default-policy seeding.
"""
from __future__ import annotations

import os
from typing import Any

import boto3

USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
APP_CLIENT_ID = os.environ.get("APP_CLIENT_ID", "")
# Fallback only ever hit in local runs; the deployed Lambda always has
# AWS_REGION set by the runtime. us-east-1 matches the deploy scripts' default.
_REGION = os.environ.get("AWS_REGION", "us-east-1")

_idp = boto3.client("cognito-idp", region_name=_REGION)

# The demo roster. Each entry is a real Cognito user you can sign in as.
# `username` is the Cognito username; `id` is the opaque id the UI uses.
# Budgets are in tokens because that is what the gateway enforces.
#
# These roster personas are demo seed data; operators should edit them to
# match their own users. The first entry maps to the "demo-presenter" Cognito
# user the CDK stack provisions and that the deploy script tells you to sign
# in as. Per-persona `defaults` (and GENERIC_DEFAULTS below) are demo seed
# budgets, not env-driven; edit them here or in the policy editor UI.
PERSONAS: list[dict[str, Any]] = [
    {
        "id": "presenter",
        "username": "demo-presenter",
        "name": "Presenter",
        "role": "Signed-in user",
        "defaults": {"budgetTokens": 30_000, "downgradeAtTokens": 20_000},
    },
    {
        "id": "engineer",
        "username": "demo-engineer",
        "name": "Platform Engineer",
        "role": "Engineering, generous budget",
        "defaults": {"budgetTokens": 50_000, "downgradeAtTokens": 45_000},
    },
    {
        "id": "analyst",
        "username": "demo-analyst",
        "name": "Data Analyst",
        "role": "Analytics, moderate budget",
        "defaults": {"budgetTokens": 20_000, "downgradeAtTokens": 12_000},
    },
    {
        "id": "contractor",
        "username": "demo-contractor",
        "name": "Contractor",
        "role": "External, tight budget",
        "defaults": {"budgetTokens": 3_000, "downgradeAtTokens": 2_000},
    },
]

# Default budget seeded for a signed-in user who is not in the roster.
GENERIC_DEFAULTS = {"budgetTokens": 20_000, "downgradeAtTokens": 15_000}

_sub_cache: dict[str, str] = {}


def persona_by_id(persona_id: str) -> dict[str, Any] | None:
    return next((p for p in PERSONAS if p["id"] == persona_id), None)


def persona_by_username(username: str) -> dict[str, Any] | None:
    lowered = (username or "").lower()
    return next((p for p in PERSONAS if p["username"].lower() == lowered), None)


def generic_persona(username: str) -> dict[str, Any]:
    """A roster-shaped record for a signed-in user who is not a demo user."""
    label = (username or "user").split("@")[0]
    return {
        "id": label,
        "username": username,
        "name": label,
        "role": "Signed-in user",
        "defaults": GENERIC_DEFAULTS,
    }


def username_for(persona: dict[str, Any]) -> str:
    return persona["username"]


def username_for_sub(sub: str) -> str:
    """Best-effort reverse lookup: the Cognito username for a JWT sub, or "".

    Used to label off-roster users (signed-in users not in PERSONAS, e.g. the
    demo user the deploy script provisions) in the fleet view. Reverses the
    ensure_user sub cache first, then falls back to a Cognito ListUsers filter.
    """
    if not sub or not USER_POOL_ID:
        return ""
    for username, cached in _sub_cache.items():
        if cached == sub:
            return username
    try:
        resp = _idp.list_users(
            UserPoolId=USER_POOL_ID,
            Filter=f'sub = "{sub}"',
            Limit=1,
        )
        users = resp.get("Users", [])
        if users:
            username = users[0].get("Username", "")
            if username:
                _sub_cache[username] = sub
            return username
    except Exception:
        return ""
    return ""


def ensure_user(persona: dict[str, Any]) -> str:
    """Create the roster user if missing; return its sub.

    The app CDK stack declares the demo users too; this is a belt-and-braces
    path so a pool wipe or a manual deletion never breaks the demo.
    """
    username = username_for(persona)
    if username in _sub_cache:
        return _sub_cache[username]
    try:
        _idp.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=username,
            MessageAction="SUPPRESS",
        )
    except _idp.exceptions.UsernameExistsException:
        pass
    user = _idp.admin_get_user(UserPoolId=USER_POOL_ID, Username=username)
    sub = next(
        (a["Value"] for a in user.get("UserAttributes", []) if a["Name"] == "sub"),
        "",
    )
    if not sub:
        raise RuntimeError(f"Cognito user {username} has no sub attribute")
    _sub_cache[username] = sub
    return sub

