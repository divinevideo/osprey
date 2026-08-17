"""Drives the REAL COOPSink and asserts on the payload it would POST.

Every other test here pins either the pure builder or the *syntax* of the call
site. Review showed why that is not enough: rebinding `author` on the line ABOVE
the call, and leaving `author=author` untouched, passed all 302 of them. The wire
payload then carried `"userId": "REPORTER_PUBKEY"` -- Coop's `creator` naming the
reporter, so Unban-User and Unsuspend-User would reverse against the person who
FILED the report. That is the regression already recorded in task #21, and an
AST test cannot see it, because it is semantics rather than syntax.

So this executes the sink. `coop_sink` imports gevent, requests, sentry_sdk and
the osprey engine, none of which the plugin test step installs -- so they are
stubbed into `sys.modules` before import. Stdlib only, no new CI dependencies.

This is the only test in the tree that runs the real `_submit_content`, which
means it is also the only one that exercises `author_for_features` and the
assembly of the outer payload. It asserts on the wire rather than on source, so
it survives the /content -> /report split rather than being rewritten by it.

Stubbing is a real limitation and worth stating: it proves the code path, not
that the deployed image imports cleanly. It replaces neither an integration test
nor a working image build.
"""

import importlib
import json
import logging
import sys
import types

import pytest

AUTHOR = 'bc02e0a6c0f01ad9cb57a2b0f8ef8241bc5ff979ce4452ce9e243de457756725'
REPORTER = '19f9afb8f855f5e86b5bea160e78ec5871648b10dedb043f4806fca8ce50e4d3'
CONTENT_ID = '3f8a1c9e77b24d6e05a4c3b81f9e2d70a6c5b4938e7f1a2d0c9b8a7f6e5d4c3b'
WRAPPER_ID = '9d1e4f7a3b8c2059e6f4a1d3c7b092e58a4f6d1c3e9b7a250f8c6d4b2a1e3f90'


class _StubTimeout(Exception):
    """Stands in for gevent.Timeout, which is used as a context manager."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _VerdictEffect:
    def __init__(self, verdict):
        self.verdict = verdict


def _stub(monkeypatch, name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


@pytest.fixture
def sink_module(monkeypatch):
    """Import the real coop_sink with its heavy dependencies stubbed.

    Captures whatever `requests.post` is called with, so the assertion is on the
    payload that would go over the wire.
    """
    # Every POST, in order. The sink makes more than one call now (the content
    # submission, then the nostr_user item), so a single overwritten slot would
    # silently re-point existing assertions at whichever call happened to be last.
    # The helpers below select a call by ENDPOINT rather than by position.
    captured: dict = {'calls': []}

    def _post(url, json=None, headers=None, timeout=None):
        captured['calls'].append({'url': url, 'payload': json, 'headers': headers})

        class _Response:
            status_code = 200

            def raise_for_status(self):
                return None

        return _Response()

    _stub(monkeypatch, 'gevent', Timeout=_StubTimeout)
    _stub(monkeypatch, 'requests', post=_post)
    _stub(
        monkeypatch,
        'sentry_sdk',
        capture_exception=lambda *a, **k: None,
        capture_message=lambda *a, **k: None,
    )
    for pkg in (
        'osprey',
        'osprey.engine',
        'osprey.engine.executor',
        'osprey.engine.language_types',
        'osprey.worker',
        'osprey.worker.lib',
        'osprey.worker.lib.osprey_shared',
        'osprey.worker.sinks',
        'osprey.worker.sinks.sink',
    ):
        _stub(monkeypatch, pkg)
    _stub(monkeypatch, 'osprey.engine.executor.execution_context', ExecutionResult=object)
    _stub(monkeypatch, 'osprey.engine.language_types.verdicts', VerdictEffect=_VerdictEffect)
    _stub(monkeypatch, 'osprey.worker.lib.osprey_shared.logging', get_logger=logging.getLogger)
    _stub(monkeypatch, 'osprey.worker.sinks.sink.output_sink', BaseOutputSink=object)

    monkeypatch.setenv('DIVINE_COOP_URL', 'https://coop.test.invalid')
    monkeypatch.setenv('DIVINE_COOP_API_KEY', 'test-key')
    monkeypatch.setenv('DIVINE_COOP_USER_TYPE_ID', 'nostr-user-type')
    monkeypatch.setenv('DIVINE_MEDIA_BASE_URL', 'https://media.test.invalid')
    # Unset so the sink skips the relay media lookup; that path is network I/O and
    # is not what this file is measuring.
    monkeypatch.delenv('DIVINE_RELAY_WS_URL', raising=False)

    missing = object()
    services_package = importlib.import_module('services')
    previous_module = sys.modules.pop('services.coop_sink', missing)
    previous_attribute = getattr(services_package, 'coop_sink', missing)
    services_package.__dict__.pop('coop_sink', None)

    module = importlib.import_module('services.coop_sink')
    importlib.reload(module)
    try:
        yield module, captured
    finally:
        sys.modules.pop('services.coop_sink', None)
        services_package.__dict__.pop('coop_sink', None)
        if previous_module is not missing:
            sys.modules['services.coop_sink'] = previous_module
        if previous_attribute is not missing:
            services_package.coop_sink = previous_attribute


def _drive(sink_module, features, action_name='nostr_kind_1984', content_id=CONTENT_ID):
    module, captured = sink_module

    class _Action:
        action_id = 7

    _Action.action_name = action_name

    class _Result:
        action = _Action()
        extracted_features = features

    sink = module.COOPSink()
    sink._submit_content(content_id, _VerdictEffect('flag_for_review'), _Result())
    return captured


def _call_to(captured, suffix):
    """The single POST whose URL ends with `suffix`, or None.

    Selecting by endpoint rather than by index is what keeps these assertions
    honest as calls are added: a test asking for the content submission can never
    silently start reading the user-item submission instead.
    """
    matches = [c for c in captured['calls'] if c['url'].endswith(suffix)]
    assert len(matches) <= 1, f'expected at most one POST to {suffix}, got {len(matches)}'
    return matches[0] if matches else None


def _content_call(captured):
    call = _call_to(captured, '/api/v1/content')
    assert call is not None, 'the content submission is the primary path and must always be attempted'
    return call


def _user_item_call(captured):
    return _call_to(captured, '/api/v1/items/async/')


def test_the_wire_payload_names_the_AUTHOR_as_creator(sink_module):
    """The assertion this file exists for.

    `userId` becomes Coop's `creator`, which is the subject of Unban-User and
    Unsuspend-User. If it ever carries the reporter, a reversal acts against the
    person who reported the content.
    """
    captured = _drive(
        sink_module,
        {
            'Kind': 1984,
            'CreatedAt': 1786637235,
            'Pubkey': AUTHOR,
            'ReportedPubkey': REPORTER,
            'ReportedAuthorPubkey': AUTHOR,
            'ReportedEventId': CONTENT_ID,
            'EventId': WRAPPER_ID,
        },
    )
    payload = _content_call(captured)['payload']
    assert payload['userId'] == AUTHOR, 'userId must carry the resolved author'
    assert payload['content']['pubkey'] == AUTHOR, 'pubkey must carry the resolved author'
    # And the reporter's own claim is still carried, clearly labelled as a claim.
    assert payload['content']['reported_pubkey'] == REPORTER


def test_wire_account_identifiers_use_one_canonical_spelling(sink_module):
    captured = _drive(
        sink_module,
        {
            'Kind': 1984,
            'ReportedAuthorPubkey': AUTHOR.upper(),
            'ReportedEventId': CONTENT_ID,
            'EventId': WRAPPER_ID,
        },
    )
    payload = _content_call(captured)['payload']
    assert payload['userId'] == AUTHOR
    assert payload['content']['pubkey'] == AUTHOR
    assert payload['content']['author'] == {'id': AUTHOR, 'typeId': 'nostr-user-type'}


def test_the_wire_payload_has_the_expected_envelope(sink_module):
    captured = _drive(sink_module, {'Kind': 1984, 'EventId': WRAPPER_ID})
    payload = _content_call(captured)['payload']
    assert payload['contentId'] == CONTENT_ID
    assert payload['sync'] is False
    assert set(payload) == {'contentId', 'contentType', 'userId', 'content', 'sync'}
    assert _content_call(captured)['url'] == 'https://coop.test.invalid/api/v1/content'


def test_userId_and_content_pubkey_describe_the_same_event(sink_module):
    """They are derived separately and must not drift; a mismatch means Coop's
    creator and the item's stated author disagree."""
    captured = _drive(
        sink_module,
        {'Kind': 1984, 'ReportedPubkey': REPORTER, 'EventId': WRAPPER_ID},
    )
    payload = _content_call(captured)['payload']
    assert payload['userId'] == payload['content']['pubkey']


def test_the_payload_is_json_serialisable(sink_module):
    """It is handed to `requests.post(json=...)`, so a non-serialisable feature
    value would raise at submit time rather than in any unit test."""
    captured = _drive(sink_module, {'Kind': 1984, 'CreatedAt': 1, 'EventId': WRAPPER_ID})
    json.dumps(_content_call(captured)['payload'])


MEDIA_SHA = 'c' * 64


def test_wire_payload_carries_relay_manager_url_media_link(sink_module, monkeypatch):
    """When the moderated event's media resolves to a sha, the card links to the
    restricted-media viewer at {DIVINE_RELAY_MANAGER_URL}/media/{sha}, and carries the
    raw sha in media_sha256. This is the click-through a moderator uses to see auto-hidden
    content before acting.
    """
    module, _ = sink_module
    monkeypatch.setenv('DIVINE_RELAY_WS_URL', 'ws://relay.test.invalid')
    monkeypatch.setenv('DIVINE_RELAY_MANAGER_URL', 'https://api-relay-staging.divine.video')
    monkeypatch.setattr(
        module,
        'fetch_event_media_with_hash',
        lambda *a, **k: ('https://media.test.invalid/v.mp4', 'https://media.test.invalid/t.jpg', MEDIA_SHA),
    )

    captured = _drive(
        sink_module,
        {
            'Kind': 1984,
            'CreatedAt': 1786637235,
            'Pubkey': AUTHOR,
            'ReportedAuthorPubkey': AUTHOR,
            'ReportedEventId': CONTENT_ID,
            'EventId': WRAPPER_ID,
        },
    )
    content = _content_call(captured)['payload']['content']
    assert content['media_sha256'] == MEDIA_SHA
    assert content['relay_manager_url'] == f'https://api-relay-staging.divine.video/media/{MEDIA_SHA}'


def test_no_relay_manager_url_when_no_sha(sink_module, monkeypatch):
    """Fail-open: media resolves but carries no sha -> no link, item still submitted."""
    module, _ = sink_module
    monkeypatch.setenv('DIVINE_RELAY_WS_URL', 'ws://relay.test.invalid')
    monkeypatch.setenv('DIVINE_RELAY_MANAGER_URL', 'https://api-relay-staging.divine.video')
    monkeypatch.setattr(
        module,
        'fetch_event_media_with_hash',
        lambda *a, **k: ('https://media.test.invalid/v.mp4', None, None),
    )
    captured = _drive(
        sink_module,
        {
            'Kind': 1984,
            'CreatedAt': 1786637235,
            'Pubkey': AUTHOR,
            'ReportedAuthorPubkey': AUTHOR,
            'ReportedEventId': CONTENT_ID,
            'EventId': WRAPPER_ID,
        },
    )
    content = _content_call(captured)['payload']['content']
    assert 'relay_manager_url' not in content
    assert 'media_sha256' not in content


DETECTOR_SHA = 'd' * 64


def test_detector_item_gets_the_link_from_its_hash_without_a_relay_lookup(sink_module, monkeypatch):
    """A direct detector Action already has media_url set from its content hash, so the
    relay-lookup block is skipped. The card must still carry the restricted-media viewer
    link, built from that same hash. This is the highest-value case: detector content is
    exactly what auto-hide pulls from public view, and its media_url stops serving once
    blocked, so without this link the moderator has nothing to click through to.
    """
    module, _ = sink_module
    monkeypatch.setenv('DIVINE_RELAY_MANAGER_URL', 'https://api-relay-staging.divine.video')
    # Relay is configured too, but the detector path must NOT fetch: it already has the sha.
    monkeypatch.setenv('DIVINE_RELAY_WS_URL', 'ws://relay.test.invalid')

    def _must_not_fetch(*a, **k):
        raise AssertionError('detector path must not call the relay lookup')

    monkeypatch.setattr(module, 'fetch_event_media_with_hash', _must_not_fetch)

    captured = _drive(
        sink_module,
        {'DetectorContentHash': DETECTOR_SHA, 'DetectorClass': 'nsfw'},
        action_name='ai_detector_nsfw',
        content_id=DETECTOR_SHA,
    )
    content = _content_call(captured)['payload']['content']
    assert content['media_url'] == f'https://media.test.invalid/{DETECTOR_SHA}'
    assert content['media_sha256'] == DETECTOR_SHA
    assert content['relay_manager_url'] == f'https://api-relay-staging.divine.video/media/{DETECTOR_SHA}'


def test_no_relay_manager_url_when_base_unset(sink_module, monkeypatch):
    """If DIVINE_RELAY_MANAGER_URL is not configured, media_sha256 is still recorded but no
    link is fabricated from a missing base."""
    module, _ = sink_module
    monkeypatch.setenv('DIVINE_RELAY_WS_URL', 'ws://relay.test.invalid')
    monkeypatch.delenv('DIVINE_RELAY_MANAGER_URL', raising=False)
    monkeypatch.setattr(
        module,
        'fetch_event_media_with_hash',
        lambda *a, **k: ('https://media.test.invalid/v.mp4', None, MEDIA_SHA),
    )
    captured = _drive(
        sink_module,
        {
            'Kind': 1984,
            'CreatedAt': 1786637235,
            'Pubkey': AUTHOR,
            'ReportedAuthorPubkey': AUTHOR,
            'ReportedEventId': CONTENT_ID,
            'EventId': WRAPPER_ID,
        },
    )
    content = _content_call(captured)['payload']['content']
    assert content['media_sha256'] == MEDIA_SHA
    assert 'relay_manager_url' not in content


# --- the nostr_user item submission ---------------------------------------------
#
# Coop resolves the Associated User panel by looking the account up as an ITEM
# (`latestItemSubmissions`), not by reading the content item's `author` reference,
# which carries only {id, typeId}. So the profile osprey already fetches reaches a
# moderator only if the account is submitted as an item in its own right.

PROFILE_BODY = {
    'profile': {'display_name': 'Alice', 'nip05': 'alice@divine.video', 'nip05_verified': True},
    'social': {'follower_count': 42},
}


def _with_profile(sink_module, monkeypatch, body=PROFILE_BODY, error=None):
    """Enable enrichment and answer every funnelcake lookup with `body`."""
    module, _ = sink_module
    monkeypatch.setenv('DIVINE_RELAY_API_URL', 'https://funnelcake.test.invalid')
    monkeypatch.setattr(module, 'fetch_profile', lambda *a, **k: (body, error))


def _report_features():
    return {
        'Kind': 1984,
        'Pubkey': REPORTER,
        'ReportedAuthorPubkey': AUTHOR,
        'ReportedEventId': CONTENT_ID,
        'EventId': WRAPPER_ID,
    }


def test_the_user_item_submission_carries_the_profile_fields(sink_module, monkeypatch):
    """The point of the whole change: display_name and nip05 on the account item.

    Without this the panel renders 'No user information found' against a 64-char
    hex string, and a moderator has to leave Coop to size up the account they are
    about to ban.
    """
    _with_profile(sink_module, monkeypatch)
    captured = _drive(sink_module, _report_features())

    call = _user_item_call(captured)
    assert call is not None, 'the account must be submitted as its own item'
    data = call['payload']['items'][0]['data']
    assert data['display_name'] == 'Alice'
    assert data['nip05'] == 'alice@divine.video (verified)'
    assert data['nip05_verified'] is True
    assert data['follower_count'] == 42
    assert data['profile_state'] == 'resolved'


def test_the_user_item_fields_are_unprefixed(sink_module, monkeypatch):
    """The content item namespaces three different people as author_/reported_/
    reporter_. The user item IS one person, so the same namespacing there would
    declare fields no producer fills and read as a second, unrelated account."""
    _with_profile(sink_module, monkeypatch)
    captured = _drive(sink_module, _report_features())

    data = _user_item_call(captured)['payload']['items'][0]['data']
    assert not [k for k in data if k.startswith(('author_', 'reported_', 'reporter_'))], data


def test_the_user_item_is_keyed_by_pubkey_and_typed_as_nostr_user(sink_module, monkeypatch):
    """`id` is what Coop resolves the panel through, so it must be the same
    canonical pubkey the content item's `author` reference carries."""
    _with_profile(sink_module, monkeypatch)
    captured = _drive(sink_module, _report_features())

    item = _user_item_call(captured)['payload']['items'][0]
    assert item['id'] == AUTHOR
    assert item['typeId'] == 'nostr-user-type'
    # `pubkey` is declared required on nostr_user, so omitting it 400s the whole
    # submission.
    assert item['data']['pubkey'] == AUTHOR
    assert item['id'] == _content_call(captured)['payload']['content']['author']['id']


def test_the_user_item_is_submitted_with_one_canonical_spelling(sink_module, monkeypatch):
    """An uppercase pubkey left raw would be a SECOND, distinct account item in
    Coop for the very same person -- and the panel would resolve neither."""
    _with_profile(sink_module, monkeypatch)
    captured = _drive(
        sink_module,
        {'Kind': 1984, 'ReportedAuthorPubkey': AUTHOR.upper(), 'ReportedEventId': CONTENT_ID, 'EventId': WRAPPER_ID},
    )
    assert _user_item_call(captured)['payload']['items'][0]['id'] == AUTHOR


def test_a_failing_user_item_submission_never_blocks_the_content_submission(sink_module, monkeypatch):
    """Fail-open. Enrichment must never cost us the review item itself."""
    module, captured = sink_module
    _with_profile(sink_module, monkeypatch)

    real_post = sys.modules['requests'].post

    def _post(url, json=None, headers=None, timeout=None):
        response = real_post(url, json=json, headers=headers, timeout=timeout)
        if url.endswith('/api/v1/items/async/'):
            raise RuntimeError('coop is down')
        return response

    monkeypatch.setattr(sys.modules['requests'], 'post', _post)

    _drive(sink_module, _report_features())

    # The content submission still happened, and _submit_content did not raise.
    assert _content_call(captured)['payload']['contentId'] == CONTENT_ID


def test_the_content_submission_is_attempted_before_the_user_item(sink_module, monkeypatch):
    """Order is the fail-open guarantee: the primary path must not be able to lose
    its share of the push budget to the enrichment POST."""
    _with_profile(sink_module, monkeypatch)
    captured = _drive(sink_module, _report_features())

    urls = [c['url'] for c in captured['calls']]
    assert urls.index('https://coop.test.invalid/api/v1/content') < urls.index(
        'https://coop.test.invalid/api/v1/items/async/'
    )


def test_no_user_item_is_submitted_when_enrichment_is_off(sink_module, monkeypatch):
    """Without DIVINE_RELAY_API_URL there is no profile to carry, so a second POST
    per item would buy nothing. Production and poc are in this state today."""
    monkeypatch.delenv('DIVINE_RELAY_API_URL', raising=False)
    captured = _drive(sink_module, _report_features())

    assert _user_item_call(captured) is None
    assert len(captured['calls']) == 1


def test_no_user_item_is_submitted_without_a_resolvable_author(sink_module, monkeypatch):
    """A junk id becomes an actionable account in Coop, exactly as it would in the
    `author` reference. Emit or omit; never a placeholder."""
    _with_profile(sink_module, monkeypatch)
    captured = _drive(sink_module, {'Kind': 1984, 'EventId': WRAPPER_ID})

    assert _user_item_call(captured) is None


def test_the_user_item_payload_is_json_serialisable(sink_module, monkeypatch):
    _with_profile(sink_module, monkeypatch)
    captured = _drive(sink_module, _report_features())
    json.dumps(_user_item_call(captured)['payload'])
