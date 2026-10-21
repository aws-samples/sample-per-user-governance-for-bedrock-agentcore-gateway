# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""B7 request-path content detectors (feature F4) — pure, dependency-free.

This module is the deterministic core of the CP-3 **Stage 5** content
inspection stage. It is intentionally free of boto3, network
calls, and any AWS SDK so that:

* Stage 5 blocking is deterministic and reproducible in unit tests (no extra
  AWS calls on the hot path), and it keeps running even when the logging /
  alerting path is down (REQ-SEC-015) and cannot be disabled per-key/route
  (REQ-SEC-028 — the wiring in ``lambda_function.py`` makes it always-on).
* An OPTIONAL Bedrock Guardrails ``ApplyGuardrail`` hook (already wired as
  ``_apply_guardrail_input`` in ``lambda_function.py``) can layer the
  *probabilistic* PII / prompt-attack categories on top for deployments that
  configure a guardrail; the demo DEFAULTS to these deterministic detectors so
  it works with no guardrail configured.

Detector families implemented here (request path only — response-path
detectors REQ-SEC-026/-044 are OUT OF SCOPE / deferred, see module note below):

1. PII Class 4/5 — SSN, payment card / PAN (Luhn-validated), bank account,
   government ID (REQ-SEC-016).
2. Credentials / secrets — AWS access keys (AKIA/ASIA), PEM private-key
   material, JWTs, and known LLM-provider key prefixes (REQ-SEC-027). The JWT
   shape mirrors the token grammar of ``lambda_function.JWT_PATTERN``
   (three base64url segments separated by dots), applied to arbitrary content
   rather than only the ``Bearer`` authorization header.
3. Prompt-injection / jailbreak indicators, matched across ALL request content
   including user messages, system prompts, and ``tool_result`` blocks
   (REQ-SEC-043).
4. URL-host allowlist — a content-walk enumerates every URL reference and
   rejects any whose host is not on the configured allowlist (REQ-SEC-019).

CRITICAL invariant (REQ-SEC-014): every returned ``reason`` names only the
*detector class* — it NEVER echoes the matched secret / PII value. Callers can
safely log or surface the reason. The block maps to a 403 in the interceptor.

The result contract for every check is a ``DetectorResult``:
``(blocked: bool, detector_class: str, reason: str)``. When ``blocked`` is
False the class/reason are empty strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Sequence

# Detector class identifiers. These are the *only* strings that appear in a
# block reason (plus a fixed "detected" suffix), so no matched content leaks.
CLASS_PII = "pii"
CLASS_CREDENTIALS = "credentials"
CLASS_INJECTION = "prompt_injection"
CLASS_URL_HOST = "url_host"


@dataclass(frozen=True)
class DetectorResult:
    """Outcome of a detector or the aggregate Stage-5 run.

    ``detector_class`` and ``reason`` are empty when ``blocked`` is False.
    ``reason`` names only the detector class (REQ-SEC-014): it MUST NOT contain
    any fragment of the matched secret / PII / URL.
    """

    blocked: bool
    detector_class: str = ""
    reason: str = ""


_NOT_BLOCKED = DetectorResult(False, "", "")


def _blocked(detector_class: str) -> DetectorResult:
    """Build a block result whose reason names the class only (REQ-SEC-014)."""
    return DetectorResult(True, detector_class, f"{detector_class} detected")


# ── Segment / URL reference model (the content-walk emits these) ────────────


@dataclass(frozen=True)
class TextSegment:
    """A text-bearing node discovered by the content-walk.

    ``path`` is a human-readable node path (e.g. ``messages[0].content[1]``)
    used only for internal reasoning; it is NEVER surfaced in a block reason.
    """

    path: str
    text: str


@dataclass(frozen=True)
class UrlRef:
    """A URL-bearing node discovered by the content-walk."""

    path: str
    url: str


# ── 1. PII Class 4/5 (REQ-SEC-016) ──────────────────────────────────────────

# US SSN: 3-2-4 grouped by hyphen or space. We deliberately require the
# separator so a bare 9-digit run (e.g. an order id) does not false-positive.
_SSN_RE = re.compile(r"\b\d{3}[- ]\d{2}[- ]\d{4}\b")

# Candidate payment-card / PAN run: 13–19 digits, optionally grouped in 4s by
# space or hyphen. This is only a *candidate*; a Luhn check confirms it so
# arbitrary long digit strings (order ids, nonces) do not trip the detector.
_PAN_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

# US bank routing/account hint: an ABA-style "routing:"/"account:" label
# followed by a digit run, OR a 9-digit routing + longer account pair. We key
# off an explicit label to keep false positives low on the deterministic path.
_BANK_RE = re.compile(
    r"\b(?:routing|aba|account|acct|iban)\b[^\n]{0,20}?\b\d{6,17}\b",
    re.IGNORECASE,
)

# Government ID: US passport (letter + 8 digits or 9 digits labeled), or an
# explicit "passport"/"driver" label followed by an alphanumeric id.
_GOV_ID_RE = re.compile(
    r"\b(?:passport|driver'?s?\s*licen[cs]e|dl\s*no)\b[^\n]{0,15}?\b[A-Z0-9]{6,12}\b",
    re.IGNORECASE,
)


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum over a digit-only string."""
    if len(digits) < 13:
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if d < 0 or d > 9:
            return False
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def detect_pii(text: str) -> DetectorResult:
    """Deterministic PII Class 4/5 detector (REQ-SEC-016).

    Returns a block naming only ``CLASS_PII`` — never the matched value.
    """
    if not isinstance(text, str) or not text:
        return _NOT_BLOCKED
    if _SSN_RE.search(text):
        return _blocked(CLASS_PII)
    for candidate in _PAN_CANDIDATE_RE.finditer(text):
        digits = re.sub(r"[ -]", "", candidate.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return _blocked(CLASS_PII)
    if _BANK_RE.search(text):
        return _blocked(CLASS_PII)
    if _GOV_ID_RE.search(text):
        return _blocked(CLASS_PII)
    return _NOT_BLOCKED


# ── 2. Credentials / secrets (REQ-SEC-027) ──────────────────────────────────

# AWS access key id: AKIA (long-term) / ASIA (STS) + 16 base32 chars.
_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")

# PEM private-key header, any key type (RSA/EC/OPENSSH/generic).
_PEM_PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")

# JWT: three base64url segments separated by dots. Mirrors the token grammar of
# lambda_function.JWT_PATTERN (which anchors on "Bearer <jwt>" for the auth
# header); here we match the token shape anywhere in content. The leading
# "eyJ" (base64url of '{"') keeps this specific to real JWT headers and avoids
# matching arbitrary dotted tokens.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")

# Known LLM-provider API-key prefixes: OpenAI ("sk-", incl. "sk-proj-"),
# Anthropic ("sk-ant-"), Google AI ("AIza..."). The trailing character class
# requires a plausible key body so a bare "sk-" word does not trip it.
_LLM_KEY_RE = re.compile(
    r"\b(?:sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{16,})\b"
)


def detect_credentials(text: str) -> DetectorResult:
    """Deterministic credential / secret detector (REQ-SEC-027).

    Covers AWS access keys, PEM private-key material, JWTs, and known LLM
    provider key prefixes. Returns a block naming only ``CLASS_CREDENTIALS``.
    """
    if not isinstance(text, str) or not text:
        return _NOT_BLOCKED
    if (
        _AWS_KEY_RE.search(text)
        or _PEM_PRIVATE_KEY_RE.search(text)
        or _JWT_RE.search(text)
        or _LLM_KEY_RE.search(text)
    ):
        return _blocked(CLASS_CREDENTIALS)
    return _NOT_BLOCKED


# ── 3. Prompt-injection / jailbreak (REQ-SEC-043) ───────────────────────────

# A conservative, deterministic phrase set for the demo. Probabilistic coverage
# is delegated to the OPTIONAL Guardrails prompt-attack filter; this set exists
# so the demo blocks the canonical injection strings without a guardrail.
# Matching is case-insensitive and whitespace-tolerant.
_INJECTION_PHRASES = (
    r"ignore (?:all|any)?\s*(?:the\s+)?previous instructions",
    r"ignore (?:all|any)?\s*(?:the\s+)?above instructions",
    r"disregard (?:all|any)?\s*(?:the\s+)?(?:previous|prior|above) instructions",
    r"forget (?:all|any)?\s*(?:the\s+)?(?:previous|prior|above) instructions",
    r"ignore your (?:previous |prior )?instructions",
    r"you are now (?:in )?(?:developer|dan|jailbreak) mode",
    r"enable developer mode",
    r"do anything now",
    r"bypass (?:your )?(?:safety|content|guardrail)",
    r"reveal (?:your )?(?:system prompt|initial instructions)",
    r"print (?:your )?system prompt",
    r"ignore (?:your )?(?:safety|content) (?:rules|policy|policies|guidelines)",
)
_INJECTION_RE = re.compile(
    "|".join(f"(?:{p})" for p in _INJECTION_PHRASES),
    re.IGNORECASE,
)


def detect_injection(text: str) -> DetectorResult:
    """Deterministic prompt-injection / jailbreak detector (REQ-SEC-043).

    Runs over any text segment — including tool_result and system content once
    the content-walk feeds them in. Returns a block naming ``CLASS_INJECTION``.
    """
    if not isinstance(text, str) or not text:
        return _NOT_BLOCKED
    # Collapse runs of whitespace so "ignore   all\nprevious instructions" is
    # matched the same as the single-spaced phrase, without echoing content.
    normalized = re.sub(r"\s+", " ", text)
    if _INJECTION_RE.search(normalized):
        return _blocked(CLASS_INJECTION)
    return _NOT_BLOCKED


# ── 4. URL-host allowlist (REQ-SEC-019) ─────────────────────────────────────

# Extract http/https URLs embedded in free text (for the content-walk's text
# segments). Explicit URL fields (image_url.url, etc.) are handled directly by
# the walk without this regex.
_URL_IN_TEXT_RE = re.compile(r"\bhttps?://[^\s\"'<>)\]}]+", re.IGNORECASE)


def extract_host(url: str) -> str | None:
    """Return the lowercase host of an http/https URL, or None if hostless.

    Canonicalization for the allowlist compare (see B7-critique F-B7-6):
    lowercase, strip any ``userinfo@`` prefix (so
    ``https://allowed.com@evil.com`` resolves to the real host ``evil.com``),
    drop port, and strip a single trailing dot. Non-http(s) or hostless URIs
    (e.g. ``data:``) return None; the caller decides policy for those.
    """
    if not isinstance(url, str) or not url:
        return None
    m = re.match(r"\s*(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*):", url)
    if not m:
        return None
    scheme = m.group("scheme").lower()
    if scheme not in ("http", "https"):
        return None
    rest = url[m.end():]
    if not rest.startswith("//"):
        return None
    authority = rest[2:]
    # Authority ends at the first '/', '?' or '#'.
    for sep in ("/", "?", "#"):
        idx = authority.find(sep)
        if idx != -1:
            authority = authority[:idx]
    # Strip userinfo (everything up to and including the last '@').
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    # Strip port. IPv6 literals ([::1]) keep the bracketed host.
    if authority.startswith("["):
        end = authority.find("]")
        host = authority[: end + 1] if end != -1 else authority
    else:
        host = authority.split(":", 1)[0]
    host = host.strip().rstrip(".").lower()
    return host or None


def _host_allowed(host: str, allowlist: Sequence[str]) -> bool:
    """True if host equals an allowlist entry or is a subdomain of one."""
    for entry in allowlist:
        e = str(entry).strip().rstrip(".").lower()
        if not e:
            continue
        if host == e or host.endswith("." + e):
            return True
    return False


def detect_url_host(
    url: str, allowlist: Sequence[str], *, reject_hostless: bool = True
) -> DetectorResult:
    """URL-host allowlist check for a single URL (REQ-SEC-019).

    A URL whose host is not on the allowlist is blocked. Hostless / opaque
    URIs (e.g. ``data:``) are blocked by default (``reject_hostless``) since
    they cannot be allowlist-verified (see B7-critique F-B7-6). Returns a
    block naming only ``CLASS_URL_HOST`` — never the URL itself.
    """
    host = extract_host(url)
    if host is None:
        return _blocked(CLASS_URL_HOST) if reject_hostless else _NOT_BLOCKED
    if _host_allowed(host, allowlist):
        return _NOT_BLOCKED
    return _blocked(CLASS_URL_HOST)


# ── The content-walk (completeness-oriented enumeration) ────────────────────


def _walk_node(node: Any, path: str) -> Iterator[Any]:
    """Recursively yield TextSegment / UrlRef for a body subtree.

    Completeness contract (B7 design §2.1, critique F-B7-4): this enumerates
    every text-bearing and URL-bearing node across BOTH the Anthropic Messages
    shape (messages[].content[] incl. tool_use.input / tool_result.content,
    system) and the OpenAI chat shape (messages[].content, string or parts).
    URL-typed fields (url / image_url.url / source.url) emit a UrlRef; URLs
    also embedded inside free text are extracted by the caller.
    """
    if isinstance(node, str):
        if node:
            yield TextSegment(path, node)
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            # Explicit URL-bearing fields.
            if key in ("url", "image_url", "source") and isinstance(value, str):
                yield UrlRef(child_path, value)
                continue
            if key == "text" and isinstance(value, str):
                if value:
                    yield TextSegment(child_path, value)
                continue
            yield from _walk_node(value, child_path)
        return
    if isinstance(node, (list, tuple)):
        for i, item in enumerate(node):
            yield from _walk_node(item, f"{path}[{i}]")
        return
    # ints / floats / bools / None carry no inspectable text or URL.


def walk_content(body: Any) -> Iterator[Any]:
    """Yield every TextSegment and UrlRef from a parsed request body.

    Surface-independent: recurses the whole body so it covers the Anthropic
    Messages shape (``messages[].content[]`` including ``tool_use.input`` and
    nested ``tool_result.content``, plus top-level ``system``) and the OpenAI
    chat shape (``messages[].content`` as a string or a list of parts). This
    is deliberately a superset enumeration — a completeness-oriented walk that
    errs toward inspecting more nodes rather than fewer (REQ-SEC-043/-026
    inspection surface; REQ-SEC-019 URL enumeration).

    Boundary note (B7-critique F-B7-4): a secret split so it spans two adjacent
    segments (e.g. two separate content blocks) is NOT reassembled here, so a
    deliberately fragmented secret can evade the per-segment regexes. This is a
    documented limitation of the deterministic request-path walk; the OPTIONAL
    Guardrails hook and a future normalization pass are the mitigations.
    """
    if not isinstance(body, (Mapping, list, tuple)):
        return
    yield from _walk_node(body, "")


# ── Stage-5 aggregate over a full request body ──────────────────────────────

# Detectors that consume a text segment, in run order. Deterministic detectors
# are high-confidence by construction (design §2.6): any match blocks.
_TEXT_DETECTORS = (detect_pii, detect_credentials, detect_injection)


def run_request_detectors(
    body: Any, url_host_allowlist: Sequence[str] | None
) -> DetectorResult:
    """Run all request-path detectors over a full parsed body (Stage 5).

    Walks the body once and applies every text detector to each TextSegment and
    the URL-host allowlist to each UrlRef (and to URLs embedded in text). On
    the first high-confidence match it returns a block naming only the detector
    class (REQ-SEC-014). When ``url_host_allowlist`` is None the URL-host
    detector is not run (no allowlist configured); the other three always run
    (non-optional — REQ-SEC-028, enforced by the caller).

    This function performs no I/O and raises nothing for ordinary content, so
    it is safe to call inline on the hot path and works with the logging path
    down (REQ-SEC-015).
    """
    for item in walk_content(body):
        if isinstance(item, TextSegment):
            for detector in _TEXT_DETECTORS:
                result = detector(item.text)
                if result.blocked:
                    return result
            # URLs embedded in free text are also host-checked.
            if url_host_allowlist is not None:
                for match in _URL_IN_TEXT_RE.finditer(item.text):
                    r = detect_url_host(match.group(0), url_host_allowlist)
                    if r.blocked:
                        return r
        elif isinstance(item, UrlRef):
            if url_host_allowlist is not None:
                r = detect_url_host(item.url, url_host_allowlist)
                if r.blocked:
                    return r
    return _NOT_BLOCKED
