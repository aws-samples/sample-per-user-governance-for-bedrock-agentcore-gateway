# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""HTTPS-only URL opener shared by this stack's Lambda functions.

`urllib.request.urlopen` goes through the default global opener, which carries
handlers for `file:`, `ftp:` and `data:` alongside http(s). Whatever scheme a
URL names is the scheme that gets opened, which is why static analysis flags
every call site (Bandit B310, ruff S310) no matter what the surrounding code
checks first.

This module takes the capability away instead of annotating the warning away.
The opener below starts from an empty `OpenerDirector` and adds only the HTTPS
handlers, so a non-https URL cannot be opened at all: there is no handler for
it and urllib raises `URLError("unknown url type")`. The host allowlist on top
of that keeps a malformed CloudFormation event or a mistyped endpoint from
turning a signed request into a call to somewhere else.

Redirects are checked per hop, not just on the first URL. The scheme is covered
by the handler set alone, but the host is not: without a check on each hop, an
https response redirecting to an https host outside the allowlist would be
followed, because the pre-flight check in `open_https` never sees that URL.
`_AllowlistedRedirectHandler` re-applies the same check to every hop, so a
redirect off the allowlist raises `ValueError` before the next host is dialed.

Behaviour is otherwise the same as `urlopen`: a 4xx or 5xx raises
`urllib.error.HTTPError` with a readable body, and the return value is a
context manager over the response.
"""
from __future__ import annotations

import urllib.request
from typing import Any
from urllib.parse import urlsplit

# AWS-owned endpoint suffixes. Every URL this stack opens is either a regional
# AWS service endpoint or a CloudFormation presigned S3 response URL, and both
# live under one of these. Matched as suffixes rather than exact hosts because
# the service endpoint names and the CloudFormation response bucket names both
# vary by region and by partition.
_ALLOWED_HOST_SUFFIXES = (
    ".amazonaws.com",  # commercial and GovCloud
    ".amazonaws.com.cn",  # China partitions
    ".api.aws",  # dual-stack service endpoints
)


def check_https_url(url: str) -> str:
    """Return `url` unchanged, or raise ValueError if it is not an AWS https URL."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"refusing non-https URL scheme: {parts.scheme!r}")
    host = (parts.hostname or "").lower()
    if not any(host.endswith(suffix) for suffix in _ALLOWED_HOST_SUFFIXES):
        raise ValueError(f"refusing host outside the AWS endpoint allowlist: {host!r}")
    return url


class _AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but only to URLs that pass the same check as the first.

    The stock handler follows a redirect wherever the Location header points.
    That is fine for the scheme, which this opener's handler set already fails
    closed on, and wrong for the host: an https response naming an https host
    outside the allowlist would be fetched, because `open_https` checked the
    original URL and nothing checks this one. Re-running the check here makes
    the allowlist a property of every hop.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        # urllib has already resolved newurl against the previous request, so
        # this is the absolute URL that would be opened next. Raising here
        # raises out of `open`, before any connection to the new host, and with
        # the same ValueError a caller gets from a rejected first URL.
        check_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Built by hand rather than with build_opener(): build_opener() seeds the
# default handler list, which is exactly the set that makes urlopen able to
# open file:, ftp: and data: URLs. Starting from an empty director and adding
# only these leaves https as the one scheme with a handler. UnknownHandler
# opens nothing; it is the piece that turns "no handler for this scheme" into a
# raised URLError instead of a silent None return.
_OPENER = urllib.request.OpenerDirector()
_OPENER.add_handler(urllib.request.HTTPSHandler())
_OPENER.add_handler(_AllowlistedRedirectHandler())
_OPENER.add_handler(urllib.request.HTTPErrorProcessor())
_OPENER.add_handler(urllib.request.HTTPDefaultErrorHandler())
_OPENER.add_handler(urllib.request.UnknownHandler())


def open_https(request: urllib.request.Request, timeout: float | None = None) -> Any:
    """Open a prepared Request over https only.

    The URL is checked before the call and the opener has no handler for any
    other scheme, so both the check and the mechanism would have to fail for a
    non-https URL to be fetched. If the response redirects, the new URL goes
    through the same check before it is opened.
    """
    check_https_url(request.full_url)
    if timeout is None:
        # Fall through to urllib's default timeout rather than passing None,
        # which means "block forever".
        return _OPENER.open(request)
    return _OPENER.open(request, timeout=timeout)
