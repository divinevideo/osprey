"""Fetches one Nostr account's public profile from funnelcake.

Split from `coop_profile.py` on purpose: that module maps a response onto card
fields and is pure, this one does the I/O. Same split as `nostr_media.py` versus
the payload builder, and for the same reason -- the pure half is testable without
network or a plugin runtime.

Returns `(body, error)`, never raises. A caller enriching a moderation item must
not be able to drop it: an unreachable funnelcake means the moderator sees an
item marked `lookup_failed`, not no item at all.

**The error is returned rather than swallowed** because funnelcake answers HTTP
200 with a null profile for a pubkey it has never seen. That makes "no profile"
and "lookup broken" identical from the outside unless the failure is carried
explicitly.
"""

from typing import Any, Optional, Tuple

import requests

# Public, unauthenticated read. No API key: this is the same data any client sees.
DEFAULT_TIMEOUT = 3.0


def fetch_profile(
    base_url: str,
    pubkey: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Tuple[Optional[dict[str, Any]], Optional[str]]:
    """Fetch `GET {base_url}/api/users/{pubkey}`.

    Args:
        base_url: funnelcake's API base. No trailing slash required; one is
            stripped, because the caller-supplied env var has had one before and
            the resulting `//api/users` 404s into an empty profile that is
            indistinguishable from a user who has none.
        pubkey: 64-char hex. Not validated here -- the caller decides what is
            worth looking up.
        timeout: seconds, applied to the whole request.

    Returns:
        `(body, None)` on success, or `(None, reason)` on any failure. Never
        raises: enrichment must not be able to drop a review item.
    """
    if not base_url or not pubkey:
        return None, 'no funnelcake URL or pubkey'

    url = f'{base_url.rstrip("/")}/api/users/{pubkey}'
    try:
        response = requests.get(url, timeout=timeout, headers={'Accept': 'application/json'})
    except Exception as exc:  # noqa: BLE001 - deliberately broad; see module docstring
        return None, f'{type(exc).__name__}: {exc}'

    status = getattr(response, 'status_code', None)
    if status is not None and status >= 400:
        # Worth naming: funnelcake's rate limiter answers an in-cluster caller that
        # sends no X-Forwarded-For with `500 Unable To Extract Key!` (fixed by
        # divine-funnelcake#926). Surfacing the status makes that diagnosable from
        # the card instead of looking like an account with no profile.
        return None, f'HTTP {status}'

    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001
        return None, f'unparseable response: {type(exc).__name__}'

    if not isinstance(body, dict):
        return None, 'malformed response: expected an object'
    if body.get('pubkey') != pubkey:
        return None, 'malformed response: pubkey did not match request'
    if body.get('profile') is not None and not isinstance(body['profile'], dict):
        return None, 'malformed response: profile was not an object'
    if body.get('social') is not None and not isinstance(body['social'], dict):
        return None, 'malformed response: social was not an object'

    return body, None
