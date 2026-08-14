"""Rejects a response that is not actually the API answering.

`raise_for_status()` only catches a non-2xx. An edge authenticator in front of
relay-manager answers an unauthenticated request with **200 and a sign-in page**,
which that check cannot see: the sink treats the page as success, and the
enforcement action silently does nothing.

Note this is client-library dependent, which is what makes it easy to miss. A
client whose user-agent the edge WAF rejects gets a 4xx and fails loudly. The one
this worker uses gets the 200. So the loud version of the bug and the silent
version are the same misconfiguration seen through different libraries, and only
the silent one reaches here.

Credentials alone do not close this. They fix today's instance; this closes the
class, so the next misconfiguration -- an expired token, a policy change, a new
gateway -- fails loudly instead of quietly enforcing nothing.

**The check is a JSON object carrying a truthy `success`, not merely "does not
look like markup."** The weaker shape-only version was written first and review
found it instance-specific: a JSON-shaped denial from a gateway or IdP passed, and
so did a plain-text body sent with no Content-Type. Strictness here is safe rather
than optimistic, verified against relay-manager rather than assumed -- all three
endpoints this sink calls return ``{"success": true, ...}`` with 200, and every
failure path is non-2xx, so there is no 200-with-``success:false`` case:

    /api/relay-rpc       worker/src/index.ts:1177
    /api/publish         worker/src/index.ts:861
    /api/moderate-media  worker/src/index.ts:1806

An endpoint added later that does not follow that shape fails loudly here. That
is the intended outcome: adopting a different response contract should be a
decision someone makes, not something that slips through.

Deliberately a pure function over (url, status, content-type, body) rather than
one taking a `requests.Response`: the plugin test step installs pytest and
websocket-client only, so anything importing `requests` cannot be unit tested
there. See test_response_guard.py.
"""

import json

_MAX_DETAIL = 120


class NotAnApiResponse(RuntimeError):
    """A 2xx that did not come from the API -- typically an auth page."""


def _detail(body: str) -> str:
    """A bounded, escaped excerpt safe to put in an exception message.

    Bounded because a body can be an entire HTML document, and this string ends
    up in logs and in Sentry. repr escapes control characters, so a hostile body
    cannot forge log lines.
    """
    return repr(body[:_MAX_DETAIL])


def require_json_response(url: str, status_code: int, content_type: str, body: str) -> None:
    """Raise unless the response is a JSON object from the API carrying success.

    Args:
        url: The URL that was called. Named in the error so an operator knows
            which call failed without correlating logs.
        status_code: HTTP status, for context in the message. Callers still call
            raise_for_status() for the non-2xx case.
        content_type: The response Content-Type header, or ''.
        body: The response body, **complete**. Do not pass a truncated prefix:
            it cannot be parsed, and every response would be rejected as
            malformed. Truncation happens here, for the message only.

    Raises:
        NotAnApiResponse: on an empty body, a non-JSON Content-Type, a body that
            does not parse as JSON, a JSON value that is not an object, or an
            object without a truthy ``success``.
    """
    stripped = (body or '').strip()

    if not stripped:
        raise NotAnApiResponse(f'{url} returned {status_code} with an empty body, not a JSON API response')

    # Checked before parsing so that a mislabelled body is reported as the
    # wrong-content-type problem it is, rather than as a parse failure.
    ctype = (content_type or '').split(';', 1)[0].strip().lower()
    if ctype and ctype != 'application/json' and not ctype.endswith('+json'):
        raise NotAnApiResponse(
            f'{url} returned {status_code} with Content-Type {ctype!r}, not JSON. '
            f'This usually means the request never reached the API. Body began: {_detail(stripped)}'
        )

    try:
        parsed = json.loads(stripped)
    except ValueError:
        raise NotAnApiResponse(
            f'{url} returned {status_code} with a body that is not JSON. This is usually an '
            f'authentication interstitial, which means the request never reached the API. '
            f'Body began: {_detail(stripped)}'
        ) from None

    if not isinstance(parsed, dict):
        raise NotAnApiResponse(
            f'{url} returned {status_code} with a JSON {type(parsed).__name__}, not an object. '
            f'Body began: {_detail(stripped)}'
        )

    # A gateway or IdP can answer with valid JSON that is not this API. Requiring
    # the envelope the API actually sends is what separates the two.
    if not parsed.get('success'):
        raise NotAnApiResponse(
            f'{url} returned {status_code} with a JSON object carrying no truthy "success". '
            f'Either the call did not reach the API, or the API reported failure in a 2xx, '
            f'which it is not expected to do. Body began: {_detail(stripped)}'
        )
