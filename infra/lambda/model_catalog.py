# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Phase 7 — model-catalog & group-entitlement resolution (pure, testable).

UI requirements #1/#2/#5 and the PHASE7_PLAN "enable ALL models" decision.

The gateway's Allowed Set (B3) is per-user: a user's ``POLICY#<sub>.allowed_models``
string-set is their entitlement. Phase 7 adds Cognito *groups* whose membership
determines that set from a live catalog:

    live ACTIVE models  (bedrock:ListFoundationModels, status ACTIVE)
      -> filter to text-generation models (drop embeddings/image/rerank/audio)
      -> resolve each group's family globs against that catalog
      -> that group's entitlement = the matched model ids

This module is the deterministic core of that pipeline. It has **no I/O and no
boto3**: the caller (deploy script / app server) fetches the ACTIVE model ids
and hands them in as a plain list, so every mapping decision is unit-testable in
isolation exactly like ``allowed_set.py`` and ``usd_quota.py``.

A "family" is a shell-style glob (``fnmatch``) matched case-insensitively
against a model id. ``"*"`` matches everything. Group ``families`` are a list of
such globs; a model is in the group's entitlement iff it matches ANY glob.

Hosted-GPT id note (verified 2026-09-16): ``openai.gpt-5.6-*`` and
``openai.gpt-6-astra`` have NO on-demand throughput and are only invocable via a
``us.`` cross-region inference profile. So ``gpt_group`` families name the
``us.openai.gpt-5.6-*`` / ``us.openai.gpt-6-*`` PROFILE ids, not the bare model
ids. :func:`resolve_group_models` therefore also expands, for any ``us.<id>``
glob, the matching bare account model into its ``us.`` profile form so the glob
can match even though ListFoundationModels returns the bare id.
"""

from __future__ import annotations

import fnmatch
from typing import Iterable, Mapping, Sequence


# Default substrings that mark a model id as NON-text (not a chat model). The
# caller may override via config ``allowed_set.text_exclude_substrings``. Kept
# lowercase; matching is case-insensitive.
DEFAULT_TEXT_EXCLUDE = (
    "embed",
    "rerank",
    "stability.",
    "titan-image",
    "nova-2-multimodal-embeddings",
    "nova-2-sonic",
    "marengo",
    "pegasus",
    "voxtral",
    "palmyra-vision",
    "gpt-oss-safeguard",
)


def is_text_model(model_id: str, exclude_substrings: Iterable[str] | None = None) -> bool:
    """True when ``model_id`` looks like a text-generation model.

    A model is considered text unless its id contains any excluded substring
    (embeddings, image, rerank, audio-only, moderation-only variants). This is a
    deny-list, not an allow-list, so a newly launched text family is included by
    default rather than silently dropped.
    """
    mid = model_id.lower()
    subs = tuple(exclude_substrings) if exclude_substrings is not None else DEFAULT_TEXT_EXCLUDE
    return not any(sub.lower() in mid for sub in subs)


def text_models(
    active_model_ids: Iterable[str],
    exclude_substrings: Iterable[str] | None = None,
) -> list[str]:
    """Filter ACTIVE model ids to the text-generation subset, sorted & unique."""
    keep = {m for m in active_model_ids if m and is_text_model(m, exclude_substrings)}
    return sorted(keep)


def us_profile_id(model_id: str) -> str:
    """The ``us.`` cross-region inference-profile id for a bare model id.

    ``openai.gpt-5.6-sol`` -> ``us.openai.gpt-5.6-sol``. Idempotent: an id that
    already starts with ``us.``/``global.`` is returned unchanged.
    """
    if model_id.startswith(("us.", "global.")):
        return model_id
    return f"us.{model_id}"


def _candidate_ids(active_text_ids: Sequence[str]) -> list[str]:
    """The id universe a glob may match: every ACTIVE text id PLUS its ``us.``
    profile form, so a ``us.openai.gpt-5.6-*`` glob matches even though
    ListFoundationModels only returns the bare ``openai.gpt-5.6-sol``.

    De-duplicated, order-stable (bare id first, then its profile form).
    """
    seen: set[str] = set()
    out: list[str] = []
    for mid in active_text_ids:
        for candidate in (mid, us_profile_id(mid)):
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


def resolve_families(
    families: Iterable[str],
    active_text_ids: Sequence[str],
) -> list[str]:
    """Resolve a list of family globs against the ACTIVE text catalog.

    Returns the sorted, de-duplicated list of model ids (in the id form the glob
    named — bare or ``us.`` profile) that match ANY glob. ``"*"`` matches every
    bare ACTIVE text id (NOT the synthetic ``us.`` duplicates, so ``*`` yields a
    clean one-per-model list rather than doubling every id).
    """
    fam = [f for f in families if f]
    if not fam:
        return []
    # "*" (or any glob ending in a bare star with no dot) should enumerate the
    # real catalog once, not the bare+profile doubling. Detect the pure "*".
    result: set[str] = set()
    has_star = any(f.strip() == "*" for f in fam)
    if has_star:
        result.update(active_text_ids)
    candidates = _candidate_ids(active_text_ids)
    for glob in fam:
        if glob.strip() == "*":
            continue  # handled above against the un-doubled catalog
        gl = glob.lower()
        for cand in candidates:
            if fnmatch.fnmatch(cand.lower(), gl):
                result.add(cand)
    return sorted(result)


def resolve_group_models(
    groups: Sequence[Mapping],
    active_model_ids: Iterable[str],
    exclude_substrings: Iterable[str] | None = None,
) -> dict[str, list[str]]:
    """Map each group name -> its entitlement (list of concrete model ids).

    ``groups`` is ``config.yaml identity.groups`` (each ``{name, families}``).
    ``active_model_ids`` is the raw ACTIVE list from ListFoundationModels. The
    result is the per-group ``allowed_models`` set the deploy script seeds onto
    every member's ``POLICY#<sub>`` and the app filters the model dropdown by.
    """
    txt = text_models(active_model_ids, exclude_substrings)
    out: dict[str, list[str]] = {}
    for g in groups:
        name = str(g.get("name", "")).strip()
        if not name:
            continue
        out[name] = resolve_families(g.get("families", []) or [], txt)
    return out


def group_for_username(username: str) -> str | None:
    """Recover the group from a Phase-7 username of the form ``<group>_userNN``.

    ``claude_user01`` -> ``claude_group``; ``admin_user03`` -> ``admin``. The
    encoding (PHASE7_PLAN decision #5) is ``<prefix>_userNN`` where prefix is the
    group name with a trailing ``_group`` dropped for the family groups and left
    bare for ``admin``. Returns ``None`` when the username does not match.
    """
    if "_user" not in username:
        return None
    prefix = username.split("_user", 1)[0]
    if not prefix:
        return None
    if prefix == "admin":
        return "admin"
    return f"{prefix}_group"


def username_for(group: str, index: int) -> str:
    """The seeded username for member ``index`` (1-based) of ``group``.

    ``claude_group`` -> ``claude_user01``; ``admin`` -> ``admin_user01``.
    Inverse of :func:`group_for_username`.
    """
    prefix = "admin" if group == "admin" else group[: -len("_group")] if group.endswith("_group") else group
    return f"{prefix}_user{index:02d}"


import re as _re

# Strip a trailing Bedrock date/version suffix: -20251001-v1:0, -v1:0, -1:0, :0.
_VERSION_SUFFIX = _re.compile(r"(-\d{8})?(-v\d+)?(:\d+)?(-\d+:\d+)?$")


def invocable_ids(
    model_id: str, profiles: Iterable[str] | None = None
) -> list[str]:
    """The id form(s) a client can actually send for a given catalog model id.

    The gateway's inference doors accept a *bare* provider/alias form, not
    always the ListFoundationModels id. This returns every form the entitlement
    set should carry so a request matches ``allowed_models`` however the UI
    names the model. The catalog id itself is always included first.

    ``profiles`` is the set of cross-region inference-profile ids that actually
    exist in the target account and region (``bedrock:ListInferenceProfiles``).
    When supplied, the ``us.``/``global.`` profile form of this model is added
    whenever it is really present. That is what entitles a user on the
    ``/bedrock-runtime`` door, and it matters for cost attribution rather than
    convenience: Bedrock writes model invocation logs *only* for the
    bedrock-runtime endpoint, so that door is the only one where per-request
    usage can be joined back to a user. The mantle door's finest grain is
    per-project-per-day, which is what previously forced one project per user.
    Passing nothing keeps the original mantle-only behaviour.

    Staying I/O-free is deliberate (see the module docstring): the caller
    fetches the profile list and hands it in, so this stays unit-testable.

    Rules (verified against the live gateway 2026-09-16):
      * Anthropic: the mantle Anthropic-Messages door serves the BARE alias with
        any ``-YYYYMMDD-vN:0`` suffix stripped
        (``anthropic.claude-haiku-4-5-20251001-v1:0`` -> ``anthropic.claude-haiku-4-5``).
      * OpenAI gpt-oss: the OpenAI Chat-Completions door serves the bare
        ``gpt-oss-20b`` (drop the ``openai.`` prefix and the ``-N:0`` suffix).
      * Hosted OpenAI (``openai.gpt-5.6-*``/``gpt-6-*``): served by the gateway
        inference target on the OpenAI **Responses** door in its BARE catalog
        form (``openai.gpt-5.6-sol``) — verified live 2026-09-16. The catalog id
        is therefore already the invocable form; the ``us.`` cross-region
        profile form is ALSO included so an entitlement still matches a client
        that reaches the model over the ``/bedrock-runtime`` Converse door
        (which takes the profile id).
      * Everything else: keep the catalog id (open-weight models are served by
        the mantle/passthrough doors under their catalog id).
    """
    forms = [model_id]
    mid = model_id
    if mid.startswith("anthropic."):
        bare = _re.sub(r"(-\d{8})?-v\d+:\d+$", "", mid)
        bare = _re.sub(r"-v\d+$", "", bare)
        if bare != mid:
            forms.append(bare)
    elif mid.startswith("openai.gpt-oss"):
        bare = mid[len("openai.") :]
        bare = _re.sub(r"-\d+:\d+$", "", bare)  # gpt-oss-20b-1:0 -> gpt-oss-20b
        if bare not in forms:
            forms.append(bare)
    elif mid.startswith(("openai.gpt-5", "openai.gpt-6")):
        prof = us_profile_id(mid)
        if prof not in forms:
            forms.append(prof)
    elif mid.startswith(("us.openai.gpt-5", "us.openai.gpt-6")):
        # Hosted GPT non-OSS entitlement seeded in the ``us.openai.*`` profile
        # form. The runtime serves this family on the OpenAI **Responses** door,
        # which takes the BARE catalog id, so the entitlement set MUST also carry
        # the bare form or the interceptor 403s ``model_not_allowed`` on the id
        # the runtime actually sends. Keep the ``us.`` form too (Converse door).
        bare = mid[len("us."):]
        if bare not in forms:
            forms.append(bare)
    elif mid.startswith(("us.openai.", "us.anthropic.")):
        pass  # already an invocable profile id
    else:
        # open-weight: the mantle chat door strips a trailing -vN:N version tail
        # (qwen.qwen3-32b-v1:0 -> qwen.qwen3-32b). Include that form so the
        # normalized invoke id matches the entitlement set. Mirrors
        # gateway_client.normalize_invoke_id.
        stripped = _re.sub(r"-v\d+:\d+$", "", mid)
        if stripped != mid:
            forms.append(stripped)
    # The cross-region inference-profile form, but only when a profile by that
    # exact id exists in the account and region. Checking rather than assuming
    # keeps a non-existent id out of the entitlement set, where it would be
    # dead weight at best and, if a model later appeared under that id, an
    # entitlement nobody granted. Skipped when the catalog id is itself already
    # a profile id (handled by the branches above).
    if profiles and not mid.startswith(("us.", "global.")):
        available = profiles if isinstance(profiles, (set, frozenset)) else set(profiles)
        for prefix in ("us.", "global."):
            candidate = f"{prefix}{mid}"
            if candidate in available and candidate not in forms:
                forms.append(candidate)
    # de-dup, order-stable
    seen: set[str] = set()
    out: list[str] = []
    for f in forms:
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def entitlement_ids(
    model_ids: Iterable[str], profiles: Iterable[str] | None = None
) -> list[str]:
    """Expand a list of catalog model ids into the full entitlement set the
    ``POLICY#.allowed_models`` string-set must carry (catalog id + every
    invocable form), sorted and de-duplicated.

    ``profiles`` is forwarded to :func:`invocable_ids`; supply the account's
    real inference-profile ids so each user is also entitled on the
    ``/bedrock-runtime`` door, which is the only door whose usage Bedrock
    records per request.
    """
    available = (
        None
        if profiles is None
        else (profiles if isinstance(profiles, (set, frozenset)) else set(profiles))
    )
    out: set[str] = set()
    for mid in model_ids:
        out.update(invocable_ids(mid, available))
    return sorted(out)
