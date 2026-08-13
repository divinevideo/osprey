"""Proves that a response which is not the API gets rejected.

The failure this exists to stop: an edge authenticator in front of relay-manager
answers an unauthenticated call with **200** and a sign-in page. `raise_for_status`
cannot see that, so every enforcement action reports success and does nothing.

Deliberately a pure function over (status, content-type, body) rather than
something taking a `requests.Response`, so it can be unit tested here. The plugin
test step runs with pytest and websocket-client only -- no osprey engine, no
requests -- which is why the sink itself can only be checked structurally.
"""

import pytest
from response_guard import NotAnApiResponse, require_json_response


def test_accepts_a_real_json_api_response() -> None:
    require_json_response('u', 200, 'application/json', '{"success":true}')


def test_accepts_json_with_charset_and_odd_casing() -> None:
    require_json_response('u', 200, 'Application/JSON; charset=utf-8', '{"success":true}')


def test_rejects_a_200_that_is_html() -> None:
    with pytest.raises(NotAnApiResponse):
        require_json_response('u', 200, 'text/html; charset=utf-8', '<!DOCTYPE html><html><head>')


def test_rejects_a_200_whose_body_is_html_despite_the_content_type() -> None:
    # Content-Type is not trustworthy on its own; check the body shape too.
    with pytest.raises(NotAnApiResponse):
        require_json_response('u', 200, 'application/json', '<!DOCTYPE html><html>')


def test_rejects_an_empty_body() -> None:
    with pytest.raises(NotAnApiResponse):
        require_json_response('u', 200, 'application/json', '')


def test_message_does_not_echo_the_whole_page() -> None:
    # A sign-in page in the logs is noise, and a body can carry anything.
    with pytest.raises(NotAnApiResponse) as e:
        require_json_response('u', 200, 'text/html', '<!DOCTYPE html>' + 'x' * 5000)
    assert len(str(e.value)) < 300


def test_names_the_url_so_the_operator_knows_which_call_failed() -> None:
    with pytest.raises(NotAnApiResponse) as e:
        require_json_response('https://example.invalid/api/relay-rpc', 200, 'text/html', '<html>')
    assert 'example.invalid' in str(e.value)
