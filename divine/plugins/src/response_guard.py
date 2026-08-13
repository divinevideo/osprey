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

Deliberately a pure function over (url, status, content-type, body) rather than
one taking a `requests.Response`: the plugin test step installs pytest and
websocket-client only, so anything importing `requests` cannot be unit tested
there. See test_response_guard.py.
"""

_MAX_DETAIL = 120


class NotAnApiResponse(RuntimeError):
    """A 2xx that did not come from the API -- typically an auth page."""


def require_json_response(url: str, status_code: int, content_type: str, body: str) -> None:
    """Raise unless the response looks like a JSON body from the API.

    Args:
        url: The URL that was called. Named in the error so an operator knows
            which call failed without correlating logs.
        status_code: HTTP status. Passed for context in the message only --
            callers still call raise_for_status() for the non-2xx case.
        content_type: The response Content-Type header, or ''.
        body: The response body, or a prefix of it.

    Raises:
        NotAnApiResponse: if the body is empty, is not declared as JSON, or
            begins like a markup document.
    """
    stripped = (body or '').lstrip()
    ctype = (content_type or '').split(';', 1)[0].strip().lower()

    if not stripped:
        raise NotAnApiResponse(f'{url} returned {status_code} with an empty body, not a JSON API response')

    # Check the body shape as well as the declared type. A gateway is free to
    # label an interstitial as application/json, and the body is the ground truth.
    if stripped[0] in '<':
        raise NotAnApiResponse(
            f'{url} returned {status_code} with a markup body, not JSON. This is usually an '
            f'authentication interstitial, which means the request never reached the API. '
            f'Body began: {stripped[:_MAX_DETAIL]!r}'
        )

    if ctype and ctype != 'application/json' and not ctype.endswith('+json'):
        raise NotAnApiResponse(
            f'{url} returned {status_code} with Content-Type {ctype!r}, not JSON. '
            f'Body began: {stripped[:_MAX_DETAIL]!r}'
        )
