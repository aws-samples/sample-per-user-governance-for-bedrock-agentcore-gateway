# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Model & Provider Allowed Set (B3) — pure, unit-testable enforcement logic.

Feature F2 (B3 Allowed Set + B1 entitlement). This module is the in-memory
representation of the org-level catalog that ``lambda_function.py`` consults at
CP-3 pipeline **Stage 4** (Entitlement ∩ Allowed Set), *before* any upstream
Bedrock call is opened. It contains no I/O and no boto3 — the interceptor (or a
loader) hands it plain data, so every decision here is deterministic and
testable in isolation.

The catalog is held as ``ALLOWED#<model>`` and ``ORG#`` items in the ledger
table.

Requirement mapping (see the per-function docstrings for the exact clause):

* REQ-ADMIN-004   — org catalog of enabled providers/models, independent of
                    per-user entitlement.
* REQ-ADMIN-004A  — provider-level enable/disable (``ORG#.providers[provider]``).
* REQ-ADMIN-004B  — model-level enable/disable (``ALLOWED#<alias>.enabled``).
* REQ-ADMIN-004C  — a caller's entitlement is *constrained to* the Allowed Set:
                    a grant naming a model absent/disabled in the set is not
                    routable (re-checked here at request time to close the race
                    where a model is removed/disabled after the grant).
* REQ-API-016     — stable client-facing ALIAS resolves to the pinned model_id.
* REQ-SEC-034     — every entry records its concrete provider model_id and
                    whether it is ``pinned`` or ``floating``.

The **effective allow decision** (B3 §2.1) is the AND of three conditions,
cheapest first:

    routable  iff  ORG#.providers[provider] == enabled          (004A)
              AND  ALLOWED#<alias>.enabled == True               (004B)
              AND  alias ∈ caller's effective entitlement (B1)   (004C / SEC-009)

Anything short of all three → not routable → the interceptor returns 403
(``model_not_allowed`` / entitlement) with no upstream connection opened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping


# Stable, machine-readable reasons the interceptor can map to an audit code.
# Kept as plain strings (not an enum) so the value is trivially assertable in
# tests and loggable without translation.
REASON_OK = "ok"
REASON_MODEL_NOT_IN_SET = "model_not_in_allowed_set"
REASON_MODEL_DISABLED = "model_disabled"
REASON_PROVIDER_DISABLED = "provider_disabled"
REASON_NOT_ENTITLED = "not_entitled"


@dataclass(frozen=True)
class ModelEntry:
    """One ``ALLOWED#<alias>`` / ``META`` catalog entry (ARCHITECTURE §3).

    ``alias`` is the stable client-facing name (REQ-API-016); ``model_id`` is
    the concrete upstream id it routes to; ``pinned`` records pinned vs floating
    (REQ-SEC-034). ``enabled`` is the fast data-write disable flag
    (REQ-ADMIN-004B). Safety/routing metadata (``region``, ``zdr_confirmed``,
    ``no_training_terms``) is carried through for the buckets that own it (B9/
    B10) but is *not* part of the Stage-4 routable decision here.
    """

    alias: str
    model_id: str
    provider: str
    enabled: bool = True
    pinned: bool = True
    region: str = ""
    zdr_confirmed: bool = False
    no_training_terms: bool = False


@dataclass(frozen=True)
class AllowedSet:
    """Org-level catalog: provider enable rollup + entries keyed by alias.

    ``providers`` maps a provider name to its ``ORG#.providers[provider]`` enable
    flag (REQ-ADMIN-004A). ``entries`` maps a client-facing alias to its
    ``ModelEntry`` (REQ-ADMIN-004 / 004B). A concrete ``model_id`` is also
    indexed so a caller may name either the alias or the resolved id.
    """

    providers: Mapping[str, bool] = field(default_factory=dict)
    entries: Mapping[str, ModelEntry] = field(default_factory=dict)

    def provider_enabled(self, provider: str) -> bool:
        """REQ-ADMIN-004A: a provider absent from the map is treated as
        disabled (fail-closed — an unknown provider is not routable)."""
        return bool(self.providers.get(provider, False))

    def entry_for(self, model_or_alias: str) -> ModelEntry | None:
        """Look an entry up by its stable alias or by its concrete model_id.

        Alias match wins (the catalog *is* the alias table, B3 §2.8); falling
        back to model_id lets a caller that already holds a resolved id still be
        matched to its governing entry. Returns ``None`` when the name is not in
        the set at all (REQ-ADMIN-004 / REQ-ADMIN-004C removal case).
        """
        entry = self.entries.get(model_or_alias)
        if entry is not None:
            return entry
        for candidate in self.entries.values():
            if candidate.model_id == model_or_alias:
                return candidate
        return None


def build_allowed_set(config: Mapping) -> AllowedSet:
    """Build an :class:`AllowedSet` from the ``allowed_set`` block of
    ``config/config.yaml`` (or an equivalently-shaped dict projected from the
    ``ALLOWED#`` / ``ORG#`` DynamoDB items — ARCHITECTURE §3).

    For the demo the catalog is seeded from config; in the real system CP-5
    projects the reviewed artifact into DynamoDB and this same shape is read on
    the hot path. Pure: no I/O, no boto3 — the caller supplies already-parsed
    data.

    A provider's rollup flag (REQ-ADMIN-004A) comes from its ``enabled`` field;
    each model's ``enabled`` field is the model-level flag (REQ-ADMIN-004B).
    """
    providers: dict[str, bool] = {}
    entries: dict[str, ModelEntry] = {}
    for provider in config.get("providers", []) or []:
        name = str(provider.get("name", "")).strip()
        if not name:
            continue
        providers[name] = bool(provider.get("enabled", False))
        for model in provider.get("models", []) or []:
            alias = str(model.get("alias", "")).strip()
            model_id = str(model.get("model_id", "")).strip()
            if not alias or not model_id:
                # A malformed catalog row is skipped rather than silently
                # creating an unroutable half-entry; the projection/review path
                # (REQ-SEC-033) is responsible for well-formed rows.
                continue
            entries[alias] = ModelEntry(
                alias=alias,
                model_id=model_id,
                provider=name,
                enabled=bool(model.get("enabled", False)),
                pinned=bool(model.get("pinned", True)),
                region=str(model.get("region", "")).strip(),
                zdr_confirmed=bool(model.get("zdr_confirmed", False)),
                no_training_terms=bool(model.get("no_training_terms", False)),
            )
    return AllowedSet(providers=providers, entries=entries)


def resolve_alias(allowed_set: AllowedSet, model_or_alias: str) -> str | None:
    """Resolve a stable client-facing ALIAS to its pinned upstream model_id.

    REQ-API-016 (stable alias) + REQ-SEC-034 (pinned id): a request naming the
    alias ``nova-micro`` resolves to the concrete ``amazon.nova-micro-v1:0``
    recorded in the catalog. If the caller already passed a concrete model_id
    that matches an entry, that same id is returned (idempotent). Returns
    ``None`` when the name is not in the Allowed Set at all — the caller (Stage
    4) turns that into a 403 without opening an upstream connection.

    Note this resolves *membership → id*; it deliberately does **not** consider
    ``enabled`` (that is a separate condition in :func:`is_routable`), so the
    interceptor can distinguish "unknown alias" from "known but disabled" when
    rendering the error/audit reason.
    """
    entry = allowed_set.entry_for(model_or_alias)
    if entry is None:
        return None
    return entry.model_id


def is_routable(
    allowed_set: AllowedSet,
    model_or_alias: str,
    user_entitlements: Iterable[str] | None,
) -> tuple[bool, str, str | None]:
    """The Stage-4 Entitlement ∩ Allowed Set decision (B3 §2.1, ARCHITECTURE §4).

    Returns ``(ok, reason, resolved_model_id)``:

    * ``ok`` — True only when all three conditions hold.
    * ``reason`` — one of the ``REASON_*`` constants; ``REASON_OK`` on success.
      The interceptor maps a non-ok reason to a 403 (``model_not_allowed`` /
      entitlement) audit code.
    * ``resolved_model_id`` — the pinned upstream id (REQ-API-016/SEC-034) when
      the model is *in the set* (returned even on an entitlement/enable
      rejection so the audit record can name the concrete id); ``None`` only
      when the model is not in the set at all.

    Conditions, evaluated cheapest-decisive first:

    1. **In the set** — ``model_or_alias`` resolves to a catalog entry
       (REQ-ADMIN-004). Not present → ``REASON_MODEL_NOT_IN_SET``. This also
       covers REQ-ADMIN-004C's removal race: a model removed from the catalog is
       no longer routable even if a stale entitlement still names it.
    2. **Provider enabled** — ``ORG#.providers[provider]`` (REQ-ADMIN-004A).
       Disabling a provider masks every model under it. → ``REASON_PROVIDER_DISABLED``.
    3. **Model enabled** — ``ALLOWED#<alias>.enabled`` (REQ-ADMIN-004B).
       → ``REASON_MODEL_DISABLED``.
    4. **Caller entitled** — the alias (or its resolved id) is in the caller's
       effective entitlement set from B1 (REQ-ADMIN-004C / REQ-SEC-009). A
       ``None`` entitlement set means "no per-user restriction" and is treated
       as entitled to anything already in the set (the sample's semantics for
       ``allowed_models is None``); an *empty* set means entitled to nothing.
       Not entitled → ``REASON_NOT_ENTITLED``.

    Provider/enable are checked before entitlement so an org-wide disable is
    reported as a provider/model disable rather than masquerading as a
    per-user entitlement problem, which keeps the audit reason accurate.
    """
    entry = allowed_set.entry_for(model_or_alias)
    if entry is None:
        # Not in the set (never catalogued, or removed) — REQ-ADMIN-004 /
        # REQ-ADMIN-004C removal race. No id to report.
        return (False, REASON_MODEL_NOT_IN_SET, None)

    resolved = entry.model_id

    # REQ-ADMIN-004A: provider rollup disable masks all its models.
    if not allowed_set.provider_enabled(entry.provider):
        return (False, REASON_PROVIDER_DISABLED, resolved)

    # REQ-ADMIN-004B: per-model fast disable.
    if not entry.enabled:
        return (False, REASON_MODEL_DISABLED, resolved)

    # REQ-ADMIN-004C / REQ-SEC-009: entitlement constrained to the Allowed Set.
    # None => no per-user restriction (matches Policy.allowed_models is None).
    if user_entitlements is not None:
        entitled = set(user_entitlements)
        if entry.alias not in entitled and entry.model_id not in entitled:
            return (False, REASON_NOT_ENTITLED, resolved)

    return (True, REASON_OK, resolved)


def routable_ids(allowed_set: AllowedSet) -> frozenset[str]:
    """Every name the org catalog currently permits, alias and concrete id.

    Used by the gateway's model-discovery route so a client's model picker
    lists only what would actually be routable at invocation. An entry is
    included only when the entry itself is enabled AND its provider is enabled
    -- the same two flags :func:`is_routable` tests in conditions 2 and 3 --
    so an org-wide provider disable or a per-model disable removes it from
    discovery as well as from invocation.

    Both the alias and the resolved ``model_id`` are returned because
    :meth:`AllowedSet.entry_for` accepts either, so a caller holding either
    name must be able to intersect its own entitlement against this set
    without first resolving aliases.

    Entitlement is deliberately NOT applied here: that is per-user state the
    catalog does not hold. The caller intersects this with the user's
    ``allowed_models``.
    """
    names: set[str] = set()
    for entry in allowed_set.entries.values():
        if not entry.enabled:
            continue
        if not allowed_set.provider_enabled(entry.provider):
            continue
        names.add(entry.alias)
        names.add(entry.model_id)
    return frozenset(names)
