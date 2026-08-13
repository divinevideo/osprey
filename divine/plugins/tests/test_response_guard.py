"""Proves that a response which is not the API gets rejected.

The failure this exists to stop: an edge authenticator in front of relay-manager
answers an unauthenticated call with **200** and a sign-in page. `raise_for_status`
cannot see that, so every enforcement action reports success and does nothing.

The first version of this checked only that the body did not *look* like markup,
which closed the one instance we had hit and not the class it claimed. Review
found two realistic responses that walked straight through: a JSON-shaped denial
from a gateway or IdP, and a plain-text body with no Content-Type at all. Both are
covered here now.

The contract is deliberately strict -- a JSON object carrying a truthy `success`
-- and that is safe rather than optimistic. Verified against relay-manager: all
three endpoints this sink calls return `{"success": true, ...}` with 200, and
every failure path is non-2xx. There is no 200-with-`success:false` case.
  /api/relay-rpc      worker/src/index.ts:1178
  /api/publish        worker/src/index.ts:861
  /api/moderate-media worker/src/index.ts:1806
A new endpoint that does not follow that shape will fail loudly here, which is
the correct outcome: it should be a deliberate decision, not a silent pass.

Deliberately a pure function over (url, status, content-type, body) rather than
something taking a `requests.Response`, so it can be unit tested here. The plugin
test step runs with pytest and websocket-client only -- no osprey engine, no
requests -- which is why the sink itself can only be checked structurally.
"""

import pytest
from response_guard import NotAnApiResponse, require_json_response

_OK = '{"success": true, "result": []}'


def test_accepts_a_real_json_api_response() -> None:
    require_json_response('u', 200, 'application/json', _OK)


def test_accepts_json_with_charset_and_odd_casing() -> None:
    require_json_response('u', 200, 'Application/JSON; charset=utf-8', _OK)


def test_accepts_a_json_suffix_content_type() -> None:
    require_json_response('u', 200, 'application/vnd.api+json', _OK)


def test_rejects_a_200_that_is_html() -> None:
    with pytest.raises(NotAnApiResponse):
        require_json_response('u', 200, 'text/html; charset=utf-8', '<!DOCTYPE html><html><head>')


def test_rejects_a_200_whose_body_is_html_despite_the_content_type() -> None:
    # Content-Type is not trustworthy on its own; check the body too.
    with pytest.raises(NotAnApiResponse):
        require_json_response('u', 200, 'application/json', '<!DOCTYPE html><html>')


def test_rejects_plain_text_with_no_content_type_at_all() -> None:
    # The gap that made the first version instance-specific: no angle bracket,
    # no declared type, so both earlier branches were skipped.
    with pytest.raises(NotAnApiResponse):
        require_json_response('u', 200, '', 'Access denied')


def test_rejects_a_content_type_that_is_not_json() -> None:
    """Exercises the content-type branch ALONE.

    The body must be valid, success-carrying JSON, otherwise a later check
    rejects it anyway and this branch could be deleted with the suite still
    green. That is exactly what happened to the first version of this test.
    """
    with pytest.raises(NotAnApiResponse) as e:
        require_json_response('u', 200, 'text/plain', _OK)
    assert 'text/plain' in str(e.value), 'must fail ON the content-type, not incidentally'


def test_rejects_a_json_shaped_denial_from_a_gateway() -> None:
    # Valid JSON, correct Content-Type, and still not the API answering.
    with pytest.raises(NotAnApiResponse):
        require_json_response('u', 200, 'application/json', '{"error": "access denied"}')


def test_rejects_json_that_is_not_an_object() -> None:
    with pytest.raises(NotAnApiResponse):
        require_json_response('u', 200, 'application/json', '[1, 2, 3]')


def test_rejects_a_success_false_envelope() -> None:
    with pytest.raises(NotAnApiResponse):
        require_json_response('u', 200, 'application/json', '{"success": false, "error": "nope"}')


def test_rejects_an_empty_body() -> None:
    with pytest.raises(NotAnApiResponse):
        require_json_response('u', 200, 'application/json', '')


def test_rejects_a_whitespace_only_body() -> None:
    with pytest.raises(NotAnApiResponse):
        require_json_response('u', 200, 'application/json', '   \n\t  ')


def test_rejects_truncated_json_rather_than_accepting_it() -> None:
    # The sink used to hand this function a 200-character prefix, which can never
    # be parsed. Truncation belongs in the message, not in the input.
    with pytest.raises(NotAnApiResponse):
        require_json_response('u', 200, 'application/json', '{"success": true, "result": [1, 2')


def test_message_does_not_echo_the_whole_page() -> None:
    with pytest.raises(NotAnApiResponse) as e:
        require_json_response('u', 200, 'text/html', '<!DOCTYPE html>' + 'x' * 5000)
    assert len(str(e.value)) < 400


def test_names_the_url_so_the_operator_knows_which_call_failed() -> None:
    with pytest.raises(NotAnApiResponse) as e:
        require_json_response('https://example.invalid/api/relay-rpc', 200, 'text/html', '<html>')
    assert 'example.invalid' in str(e.value)
