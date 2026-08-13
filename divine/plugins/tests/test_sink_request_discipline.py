"""Guards that EVERY relay-manager call goes through one checked path.

The sink talks to relay-manager through an edge authenticator. Without a service
token that edge answers **200 with a sign-in page**, and `raise_for_status()`
cannot see a 200 -- so ban, pubkey ban, enforcement label and age-restrict all
reported success and did nothing.

Patching the four call sites that existed at the time would have fixed those four
and left the fifth, whenever someone adds it, exposed to the same silence. So the
requirement is structural: there is exactly ONE place in this module that issues
an HTTP request, and that place raises for status AND rejects a response that is
not the API.

**These guards were rewritten after review.** The first version was largely
decorative: of six mutations, five passed. A fifth handler calling
`requests.patch` passed; renaming a header to `CF-Access-Client-Sekret` passed;
reading the env vars and never setting the headers passed; wrapping the response
check in `try/except: pass` passed. Each of those reproduces the outage this file
claims to prevent. The specific weaknesses are named at each test below so the
next person can tell whether a change makes them decorative again.

SOURCE-LEVEL, like test_age_restrict_payload.py and for the same reason: the
plugin test step runs with pytest and websocket-client only, so the sink -- which
imports the osprey engine and requests -- cannot be imported here. The behaviour
of the check itself is unit tested in test_response_guard.py; this file asserts
the sink actually uses it.
"""

import ast
from pathlib import Path

_SINK = Path(__file__).resolve().parents[1] / 'src' / 'services' / 'relay_manager_sink.py'
_SRC = _SINK.read_text()
_TREE = ast.parse(_SRC)

# Methods that are infrastructure rather than effect handlers. Everything else on
# the class is treated as a handler, so a NEW handler is covered automatically --
# the previous version hardcoded four names and checked nothing beyond them.
_NON_HANDLERS = {'__init__', '_headers', '_request', 'will_do_work', 'push', 'stop'}


def _class() -> ast.ClassDef:
    for node in _TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == 'RelayManagerSink':
            return node
    raise AssertionError(f'RelayManagerSink not found in {_SINK.name}; this guard is checking nothing')


def _fn(name: str) -> ast.FunctionDef:
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f'{name} not found in {_SINK.name}. If it was renamed, this guard is now '
        f'checking nothing and must be pointed at the new name.'
    )


def _requests_aliases() -> set[str]:
    """Names bound to requests functions by `from requests import post` and friends.

    A previous version missed this entirely: `from requests import post` followed
    by a bare `post(...)` call was invisible to it.
    """
    aliases = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[0] in _HTTP_MODULES:
            for a in node.names:
                aliases.add(a.asname or a.name)
    return aliases


# Every module that can issue an HTTP request. Keyed on `requests` alone, this
# guard was blind to a handler reaching for `httpx.post` or -- more realistically,
# since it is stdlib and adds no dependency -- `urllib.request.urlopen`.
_HTTP_MODULES = {'requests', 'httpx', 'urllib', 'http', 'aiohttp', 'urllib3'}


def _http_call_nodes(scope: ast.AST) -> list[ast.Call]:
    """Every call that issues an HTTP request, however it is spelled.

    Catches `requests.<anything>(...)`, a bare name imported from an HTTP module,
    and `requests.Session().post(...)` -- where the receiver is itself a Call, so
    resolving only `ast.Name` receivers (as the first version did) missed it.
    """
    aliases = _requests_aliases()
    out = []
    for n in ast.walk(scope):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id in aliases:
            out.append(n)
        elif isinstance(f, ast.Attribute):
            # Walk down to the root of the receiver chain.
            root = f.value
            while isinstance(root, (ast.Attribute, ast.Call, ast.Subscript)):
                root = root.func if isinstance(root, ast.Call) else root.value
            if isinstance(root, ast.Name) and (root.id in _HTTP_MODULES or root.id in aliases):
                out.append(n)
    return out


def _calls(node: ast.AST) -> list[str]:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                out.append(f.attr)
            elif isinstance(f, ast.Name):
                out.append(f.id)
    return out


def test_there_is_exactly_one_request_site() -> None:
    """A second site is how a check gets forgotten. Route new calls through _request."""
    sites = _http_call_nodes(_TREE)
    lines = sorted(n.lineno for n in sites)
    assert len(sites) == 1, (
        f'expected exactly one HTTP call site in {_SINK.name}, found {len(sites)} at lines {lines}. '
        f'Every relay-manager call must go through the single checked helper, so a new effect '
        f'handler cannot skip the response validation.'
    )
    # ...and it must be inside _request, not somewhere else that happens to be alone.
    helper = _fn('_request')
    assert helper.lineno <= sites[0].lineno <= (helper.end_lineno or helper.lineno), (
        f'the single HTTP call is at line {sites[0].lineno}, outside _request '
        f'(lines {helper.lineno}-{helper.end_lineno})'
    )


def test_every_handler_goes_through_the_request_helper() -> None:
    """Derived from the AST, so a NEW handler is covered without editing this test."""
    handlers = [
        n for n in _class().body
        if isinstance(n, ast.FunctionDef) and n.name not in _NON_HANDLERS
    ]
    assert handlers, 'no effect handlers found; the exclusion list has probably gone stale'
    for h in handlers:
        offenders = _http_call_nodes(h)
        assert not offenders, (
            f'{h.name} issues an HTTP request directly at line(s) '
            f'{sorted(n.lineno for n in offenders)}; route it through self._request'
        )


def _is_swallowed(fn: ast.FunctionDef, call_name: str) -> bool:
    """True if every occurrence of call_name sits inside a try whose handler swallows."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        in_body = any(c == call_name for c in _calls(ast.Module(body=node.body, type_ignores=[])))
        if not in_body:
            continue
        for handler in node.handlers:
            # A handler that neither re-raises nor raises anything swallows.
            raises = [n for n in ast.walk(handler) if isinstance(n, ast.Raise)]
            if not raises:
                return True
    return False


def test_the_request_helper_raises_for_status_and_rejects_non_json() -> None:
    helper = _fn('_request')
    calls = _calls(helper)
    assert 'raise_for_status' in calls, f'_request must still catch non-2xx responses; calls were {calls}'
    assert 'require_json_response' in calls, (
        '_request must reject a 2xx whose body is not the API. raise_for_status cannot see a '
        '200 sign-in page, which is exactly how enforcement silently no-opped.'
    )
    # Present but swallowed is the same as absent. A previous version checked only
    # for presence, so wrapping the call in try/except: pass passed.
    for name in ('raise_for_status', 'require_json_response'):
        assert not _is_swallowed(helper, name), (
            f'{name} is called inside a try whose handler swallows the exception, '
            f'which defeats it entirely'
        )


def test_the_helper_passes_the_whole_body_not_a_prefix() -> None:
    """The guard parses the body; a truncated prefix can never parse."""
    helper = ast.unparse(_fn('_request'))
    assert 'resp.text' in helper, '_request must pass the response body to the guard'
    assert 'resp.text[' not in helper, (
        '_request must pass the COMPLETE body to require_json_response. A sliced prefix '
        'cannot be parsed as JSON, so every response would be rejected as malformed.'
    )


def test_headers_use_the_exact_names_the_edge_matches_on() -> None:
    """The header NAME is what the edge authenticator matches; a typo ships green.

    A previous version asserted only that the env-var names appeared somewhere in
    the function, so renaming the header to CF-Access-Client-Sekret passed, and so
    did reading the env vars and never assigning either header.
    """
    src = ast.unparse(_fn('_headers'))
    for header in ("'CF-Access-Client-Id'", "'CF-Access-Client-Secret'"):
        assert header in src, f'_headers must set the {header} header exactly'
    # Assigned into the returned dict, not merely mentioned.
    assigns = [
        n for n in ast.walk(_fn('_headers'))
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Subscript) for t in n.targets)
    ]
    assigned = {ast.unparse(t.slice) for n in assigns for t in n.targets if isinstance(t, ast.Subscript)}
    for header in ("'CF-Access-Client-Id'", "'CF-Access-Client-Secret'"):
        assert header in assigned, f'{header} is referenced but never assigned into the headers dict'


def test_both_halves_of_the_token_are_required_together() -> None:
    """Sending one half is not a partial credential, it is an unauthenticated request.

    Asserts on the AST node rather than on the substring ' and ', which was the
    previous check and would have been silently retired by any second `and`
    appearing anywhere in the method.
    """
    guards = [
        n for n in ast.walk(_fn('_headers'))
        if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.And) and len(n.values) == 2
    ]
    assert guards, '_headers must guard the header pair on BOTH values being present'
    src = ast.unparse(_fn('_headers'))
    assert 'CF_ACCESS_CLIENT_ID' in src and 'CF_ACCESS_CLIENT_SECRET' in src, (
        '_headers must read both environment variables'
    )


def test_every_handler_re_raises_rather_than_swallowing() -> None:
    """The enforcement-correctness invariant, and nothing protected it before.

    `push()` publishes the kind-1985 `auto_hidden` label only after the bans
    succeed, and relies on `_ban_event` RAISING to skip it. Drop that `raise` and
    a failed ban logs, returns normally, and the label is signed with Divine's
    moderation key and broadcast for a ban that never happened. The audit trail
    then asserts something false, durably and attributably.

    Found by mutation: removing the trailing `raise` from `_ban_event` passed all
    286 tests, because `_is_swallowed` was only ever applied to `_request`.
    """
    handlers = [
        n for n in _class().body
        if isinstance(n, ast.FunctionDef) and n.name not in _NON_HANDLERS
    ]
    assert handlers, 'no effect handlers found; the exclusion list has probably gone stale'
    for h in handlers:
        for node in ast.walk(h):
            if not isinstance(node, ast.Try):
                continue
            for handler in node.handlers:
                raises = [n for n in ast.walk(handler) if isinstance(n, ast.Raise)]
                assert raises, (
                    f'{h.name} catches an exception at line {handler.lineno} without re-raising. '
                    f'A handler that swallows lets push() proceed to publish an enforcement '
                    f'label for an action that did not happen.'
                )


def _guard_call() -> ast.Call:
    for node in ast.walk(_fn('_request')):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == 'require_json_response':
            return node
    raise AssertionError('require_json_response call not found in _request')


def test_the_helper_passes_the_response_body_itself_not_a_derived_value() -> None:
    """Asserts on the ARGUMENT NODE, not on a substring of the source.

    The previous version checked that `resp.text[` did not appear, which is
    dodged by `body = resp.text` then passing `body[:200]`. Re-adding truncation
    breaks every call rather than silently passing one, so it is loud -- but this
    test claims to prevent it, so it should actually prevent it.
    """
    body_arg = _guard_call().args[3]
    assert isinstance(body_arg, ast.Attribute) and body_arg.attr == 'text', (
        f'the body argument to require_json_response is {type(body_arg).__name__}, not a bare '
        f'`resp.text`. The guard parses the body, so a slice or a derived value cannot parse '
        f'and every response would be rejected as malformed.'
    )


def test_the_helper_passes_the_real_content_type() -> None:
    """A hardcoded '' reduces the guard to body-only without failing anything."""
    ctype_arg = _guard_call().args[2]
    assert not isinstance(ctype_arg, ast.Constant), (
        'the content-type argument is a constant; pass the real response header so the '
        'content-type branch of the guard is not dead'
    )


def test_the_guard_call_is_not_conditional() -> None:
    """An `if not os.environ.get('SKIP_...')` wrapper is a swallow by another name.

    `_is_swallowed` looks for try/except and would not see this. A "temporary"
    debug toggle that survives is a realistic way for the check to stop running.
    """
    call_line = _guard_call().lineno
    for node in ast.walk(_fn('_request')):
        if isinstance(node, ast.If):
            body_lines = {
                n.lineno for stmt in node.body for n in ast.walk(stmt) if hasattr(n, 'lineno')
            }
            assert call_line not in body_lines, (
                f'require_json_response at line {call_line} is inside a conditional at line '
                f'{node.lineno}; it must run on every request'
            )


def test_each_access_header_carries_its_matching_value() -> None:
    """Swapped values pass every other check. Copy-paste is the realistic error."""
    pairs = {}
    for node in ast.walk(_fn('_headers')):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Subscript):
            key = ast.unparse(node.targets[0].slice)
            pairs[key] = ast.unparse(node.value)
    assert pairs.get("'CF-Access-Client-Id'") == 'client_id', (
        f"CF-Access-Client-Id is assigned {pairs.get(chr(39) + 'CF-Access-Client-Id' + chr(39))!r}, "
        f'expected the client id'
    )
    assert pairs.get("'CF-Access-Client-Secret'") == 'client_secret', (
        f"CF-Access-Client-Secret is assigned "
        f"{pairs.get(chr(39) + 'CF-Access-Client-Secret' + chr(39))!r}, expected the client secret"
    )
