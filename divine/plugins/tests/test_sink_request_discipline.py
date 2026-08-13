"""Guards that EVERY relay-manager call goes through one checked path.

Two failures this prevents, both seen on staging 2026-08-13.

The sink talks to relay-manager through an edge authenticator. Without a service
token that edge answers **200 with a sign-in page**, and `raise_for_status()`
cannot see a 200 -- so ban, label-publish and age-restrict all reported success
and did nothing.

Patching the four call sites that existed at the time would have fixed those four
and left the fifth, whenever someone adds it, exposed to the same silence. So the
requirement is structural: there is exactly ONE place in this module that issues
an HTTP request, and that place both raises for status and rejects a non-JSON
body. A new effect handler then cannot express the wrong thing.

SOURCE-LEVEL, like test_age_restrict_payload.py and for the same reason: the
plugin test step runs with pytest and websocket-client only, so the sink -- which
imports the osprey engine and requests -- cannot be imported here. The behaviour
of the check itself is unit tested in test_response_guard.py; this file asserts
the sink actually uses it.
"""

import ast
from pathlib import Path

_SINK = Path(__file__).resolve().parents[1] / 'src' / 'services' / 'relay_manager_sink.py'
_TREE = ast.parse(_SINK.read_text())


def _fn(name: str) -> ast.FunctionDef:
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f'{name} not found in {_SINK.name}. If it was renamed, this guard is now '
        f'checking nothing and must be pointed at the new name.'
    )


def _calls(node: ast.AST) -> list[str]:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                base = f.value.id if isinstance(f.value, ast.Name) else ''
                out.append(f'{base}.{f.attr}' if base else f.attr)
            elif isinstance(f, ast.Name):
                out.append(f.id)
    return out


def test_there_is_exactly_one_request_site() -> None:
    """A second site is how a check gets forgotten. Route new calls through the helper."""
    sites = [c for c in _calls(_TREE) if c in ('requests.post', 'requests.get', 'requests.put', 'requests.request')]
    assert len(sites) == 1, (
        f'expected exactly one HTTP call site in {_SINK.name}, found {len(sites)}: {sites}. '
        f'Every relay-manager call must go through the single checked helper, so that a new '
        f'effect handler cannot skip the response validation.'
    )


def test_the_request_helper_raises_for_status_and_rejects_non_json() -> None:
    helper = _fn('_request')
    calls = _calls(helper)
    # Matched on the attribute rather than the exact expression: the receiver is
    # an implementation detail (resp.raise_for_status, response.raise_for_status).
    assert any(c.split('.')[-1] == 'raise_for_status' for c in calls), (
        f'_request must still catch non-2xx responses; calls were {calls}'
    )
    assert 'require_json_response' in calls, (
        '_request must reject a 2xx whose body is not JSON. raise_for_status cannot see a '
        '200 sign-in page, which is exactly how enforcement silently no-opped.'
    )


def test_effect_handlers_do_not_call_requests_directly() -> None:
    for name in ('_ban_event', '_ban_pubkey', '_publish_label_event', '_age_restrict_media'):
        calls = _calls(_fn(name))
        offenders = [c for c in calls if c.startswith('requests.')]
        assert not offenders, f'{name} calls {offenders} directly; route it through _request'


def test_headers_include_the_edge_service_token_when_configured() -> None:
    src = ast.unparse(_fn('_headers'))
    for var in ('CF_ACCESS_CLIENT_ID', 'CF_ACCESS_CLIENT_SECRET'):
        assert var in src, (
            f'_headers must send {var} when it is configured. Without the pair the edge '
            f'returns an auth page instead of the API.'
        )


def test_both_halves_of_the_token_are_required_together() -> None:
    """Sending one half is not a partial credential, it is an unauthenticated request."""
    src = ast.unparse(_fn('_headers'))
    assert ' and ' in src, (
        '_headers must require BOTH CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET before '
        'sending either, so a half-configured deployment is not mistaken for an authenticated one'
    )
