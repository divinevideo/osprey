"""Drives the REAL COOPSink and asserts the profile fields reach the wire.

The pure builder is pinned in test_coop_profile.py. This asks the different
question: does the sink actually CALL it, for all three pubkeys, and does a
funnelcake failure leave the item submittable? Those are integration facts, and
the equivalent gap in the payload builder is how a regression that named the
reporter as creator passed 302 tests -- the mapping was right and the call site
was wrong.

Heavy imports are stubbed into sys.modules; stdlib only, no new CI dependency.
"""

import importlib
import logging
import sys
import types

import pytest

# The VERIFIED signer of the reported event. osprey resolves this into
# ReportedAuthorPubkey via funnelcake; it is NOT ReportedPubkey, which is the
# reporter's unverified claim about who is responsible. Getting these two
# confused is how enforcement lands on the wrong account.
AUTHOR = 'bc02e0a6c0f01ad9cb57a2b0f8ef8241bc5ff979ce4452ce9e243de457756725'
REPORTER = '19f9afb8f855f5e86b5bea160e78ec5871648b10dedb043f4806fca8ce50e4d3'
CONTENT_ID = '3f8a1c9e77b24d6e05a4c3b81f9e2d70a6c5b4938e7f1a2d0c9b8a7f6e5d4c3b'
WRAPPER_ID = '9d1e4f7a3b8c2059e6f4a1d3c7b092e58a4f6d1c3e9b7a250f8c6d4b2a1e3f90'


class _StubTimeout(Exception):
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _VerdictEffect:
    def __init__(self, verdict):
        self.verdict = verdict


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


@pytest.fixture
def sink(monkeypatch):
    """The real sink, with requests captured. `profiles` maps pubkey -> body, and
    `calls` records every GET so we can assert the lookup is deduplicated."""
    captured: dict = {'calls': [], 'payload': None}
    profiles: dict = {}

    class _Response:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    def _get(url, timeout=None, headers=None):
        captured['calls'].append(url)
        pubkey = url.rstrip('/').split('/')[-1]
        if pubkey in profiles and profiles[pubkey] is Exception:
            raise RuntimeError('funnelcake exploded')
        return _Response(profiles.get(pubkey, {'pubkey': pubkey, 'profile': None}))

    def _post(url, json=None, headers=None, timeout=None):
        captured['payload'] = json
        return _Response({})

    _stub('gevent', Timeout=_StubTimeout)
    _stub('requests', get=_get, post=_post)
    _stub('sentry_sdk', capture_exception=lambda *a, **k: None, capture_message=lambda *a, **k: None)
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
        _stub(pkg)
    _stub('osprey.engine.executor.execution_context', ExecutionResult=object)
    _stub('osprey.engine.language_types.verdicts', VerdictEffect=_VerdictEffect)
    _stub('osprey.worker.lib.osprey_shared.logging', get_logger=logging.getLogger)
    _stub('osprey.worker.sinks.sink.output_sink', BaseOutputSink=object)

    monkeypatch.setenv('DIVINE_COOP_URL', 'https://coop.test.invalid')
    monkeypatch.setenv('DIVINE_COOP_API_KEY', 'test-key')
    monkeypatch.setenv('DIVINE_MEDIA_BASE_URL', 'https://media.test.invalid')
    monkeypatch.setenv('DIVINE_RELAY_API_URL', 'https://funnelcake.test.invalid')
    monkeypatch.delenv('DIVINE_RELAY_WS_URL', raising=False)

    # funnelcake_profile imports `requests` at module level, so reloading only the
    # sink leaves it bound to a PREVIOUS test's stub -- whose closure holds that
    # test's `profiles` and `captured`. That leaked stale profile bodies between
    # tests and recorded calls into the wrong dict, which looked like a dedupe bug
    # in the sink and was not.
    fetcher = importlib.import_module('funnelcake_profile')
    importlib.reload(fetcher)
    module = importlib.import_module('services.coop_sink')
    importlib.reload(module)
    return module, captured, profiles


def _drive(sink, features, action_name='nostr_kind_1984'):
    module, captured, _ = sink

    class _Action:
        action_id = 7

    _Action.action_name = action_name

    class _Result:
        action = _Action()
        extracted_features = features

    module.COOPSink()._submit_content(CONTENT_ID, _VerdictEffect('flag_for_review'), _Result())
    return captured


def test_the_moderator_gets_a_name_not_just_hex(sink):
    _, _, profiles = sink
    profiles[AUTHOR] = {
        'pubkey': AUTHOR,
        'profile': {'display_name': 'Six Second Sam', 'nip05': 'sam@divine.video', 'nip05_verified': True},
    }
    captured = _drive(sink, {'Kind': 1984, 'ReportedAuthorPubkey': AUTHOR, 'EventId': WRAPPER_ID})
    content = captured['payload']['content']
    assert content['author_display_name'] == 'Six Second Sam'
    # Status folded in: the queue preview drops BOOLEAN fields, so a separate
    # verified flag would be invisible there. See coop_profile.profile_fields.
    assert content['author_nip05'] == 'sam@divine.video (verified)'
    assert content['author_profile_state'] == 'resolved'


def test_all_three_pubkeys_are_resolved(sink):
    _, _, profiles = sink
    profiles[AUTHOR] = {'pubkey': AUTHOR, 'profile': {'display_name': 'Author'}}
    profiles[REPORTER] = {'pubkey': REPORTER, 'profile': {'display_name': 'Reporter'}}
    captured = _drive(sink, {'Kind': 1984, 'ReportedAuthorPubkey': AUTHOR, 'Pubkey': REPORTER, 'EventId': WRAPPER_ID})
    content = captured['payload']['content']
    assert content['author_display_name'] == 'Author'
    assert content['reporter_display_name'] == 'Reporter'


def test_one_lookup_per_DISTINCT_pubkey(sink):
    """Author, reported and reporter are frequently the same person -- a reported
    post's author usually IS the reported user. Three calls where one would do is
    three times the load on funnelcake for every item osprey submits."""
    _, _, profiles = sink
    profiles[AUTHOR] = {'pubkey': AUTHOR, 'profile': {'display_name': 'Same Person'}}
    captured = _drive(sink, {'Kind': 1984, 'ReportedAuthorPubkey': AUTHOR, 'Pubkey': AUTHOR, 'EventId': WRAPPER_ID})
    assert len(captured['calls']) == 1, f'expected one lookup, made {len(captured["calls"])}: {captured["calls"]}'


def test_a_funnelcake_failure_still_submits_the_item_and_SAYS_SO(sink):
    """Fail-open, like the media lookup: enrichment must never drop a review item.
    But the card must show `lookup_failed`, not a blank that reads as 'no profile'."""
    _, _, profiles = sink
    profiles[AUTHOR] = Exception
    captured = _drive(sink, {'Kind': 1984, 'ReportedAuthorPubkey': AUTHOR, 'EventId': WRAPPER_ID})
    assert captured['payload'] is not None, 'the item was dropped; enrichment must never block submission'
    content = captured['payload']['content']
    assert content['author_profile_state'] == 'lookup_failed'
    # The REASON has to reach the card too. The state alone tells a moderator the
    # lookup failed; the reason is what makes it actionable -- 'HTTP 500 Unable To
    # Extract Key!' names funnelcake's rate limiter, 'timed out' names something
    # else entirely, and the fix differs.
    assert 'funnelcake exploded' in content['author_profile_error']


def test_a_TIMEOUT_is_reported_as_a_failed_lookup_not_a_missing_profile(sink):
    """The sink's own gevent deadline, distinct from the request timeout inside
    fetch_profile. A slow funnelcake must not silently look like an account with no
    profile, and must not eat the sink-level budget."""
    module, captured, profiles = sink

    def _slow(*a, **k):
        raise TimeoutError('too slow')

    # Patched on the SINK, not on funnelcake_profile: the sink does
    # `from funnelcake_profile import fetch_profile`, which binds the function
    # directly, so patching the source module would not reach it.
    module.fetch_profile = _slow
    captured_out = _drive(sink, {'Kind': 1984, 'ReportedAuthorPubkey': AUTHOR, 'EventId': WRAPPER_ID})
    content = captured_out['payload']['content']
    assert content['author_profile_state'] == 'lookup_failed'
    assert 'TimeoutError' in content['author_profile_error']


@pytest.mark.parametrize(
    ('body', 'reason'),
    [
        ([], 'expected an object'),
        ({'pubkey': REPORTER, 'profile': {'display_name': 'Wrong Person'}}, 'pubkey did not match request'),
        ({'pubkey': AUTHOR, 'profile': 'not an object'}, 'profile was not an object'),
    ],
)
def test_a_malformed_profile_response_still_submits_without_misidentifying_the_user(sink, body, reason):
    """A successful status is not enough to bind identity to the requested pubkey.

    Reject malformed or mismatched bodies before they can either label the card as
    another person or raise outside the sink's fail-open enrichment boundary.
    """
    _, _, profiles = sink
    profiles[AUTHOR] = body
    captured = _drive(sink, {'Kind': 1984, 'ReportedAuthorPubkey': AUTHOR, 'EventId': WRAPPER_ID})
    content = captured['payload']['content']
    assert captured['payload'] is not None
    assert content['author_profile_state'] == 'lookup_failed'
    assert reason in content['author_profile_error']
    assert 'author_display_name' not in content


def test_no_funnelcake_url_means_no_lookup_and_no_profile_fields(sink):
    """Same posture as DIVINE_RELAY_WS_URL: unset disables the enrichment rather
    than failing the submission."""
    module, captured, profiles = sink
    import os

    os.environ.pop('DIVINE_RELAY_API_URL', None)
    importlib.reload(module)
    captured['calls'].clear()
    _drive(sink, {'Kind': 1984, 'ReportedAuthorPubkey': AUTHOR, 'EventId': WRAPPER_ID})
    assert captured['calls'] == []
    assert not any(k.endswith('_profile_state') for k in captured['payload']['content'])


def test_the_REPORTED_account_is_the_reporters_claim_not_the_verified_author(sink):
    """The gap a review found: no test drove the `reported` branch at all, so three
    mutations survived -- including binding `reported` to the verified author.

    These are different facts and a moderator acts on the difference. `author` is who
    actually signed the content; `reported` is who the reporter SAYS is responsible.
    Showing the verified identity under the label of an unverified claim is the same
    class of error as the reporter-as-creator regression, one field over.
    """
    _, _, profiles = sink
    CLAIMED = 'c0ffee11deadbeef2222333344445555666677778888999900001111222233ff'
    profiles[AUTHOR] = {'pubkey': AUTHOR, 'profile': {'display_name': 'Actual Author'}}
    profiles[CLAIMED] = {'pubkey': CLAIMED, 'profile': {'display_name': 'Accused Party'}}
    captured = _drive(
        sink,
        {
            'Kind': 1984,
            'ReportedAuthorPubkey': AUTHOR,
            'ReportedPubkey': CLAIMED,
            'Pubkey': REPORTER,
            'EventId': WRAPPER_ID,
        },
    )
    content = captured['payload']['content']
    assert content['author_display_name'] == 'Actual Author'
    assert content['reported_display_name'] == 'Accused Party'
    assert content['author_display_name'] != content['reported_display_name']


def test_a_reporter_controlled_pubkey_cannot_steer_the_lookup_URL(sink):
    """`ReportedPubkey` is the first p-tag of a report -- reporter-controlled -- and is
    interpolated into a URL path. requests normalises dot segments, so an unvalidated
    value redirects an in-cluster GET to an arbitrary funnelcake path and puts the URL
    in a moderator-visible error field. Must never reach the fetcher."""
    _, captured, _ = sink
    _drive(
        sink,
        {
            'Kind': 1984,
            'ReportedAuthorPubkey': AUTHOR,
            'ReportedPubkey': '../../api/admin/purge',
            'EventId': WRAPPER_ID,
        },
    )
    assert not any('admin' in c or '..' in c for c in captured['calls']), (
        f'a crafted p-tag reached the fetcher: {captured["calls"]}'
    )
    content = captured['payload']['content']
    assert content['reported_profile_state'] == 'lookup_failed'


def test_the_push_budget_covers_every_hop_it_now_has(sink):
    """A review found that adding profile lookups without raising this budget made a
    slow-but-working funnelcake DROP review items: MultiOutputSink wraps push() in
    gevent.Timeout(sink.timeout) with max_retries 0, so overrunning is a drop, not a
    degraded submission. The fix has to stay fixed, hence arithmetic rather than
    prose -- reverting the formula previously passed the entire suite.

    Profile lookups share ONE deadline, so the worst case is media + profiles + POST
    regardless of how many distinct pubkeys an item carries.
    """
    module, _, _ = sink
    s = module.COOPSink()
    worst_case = s.media_timeout + s.profile_timeout + s.coop_timeout
    assert s.timeout > worst_case, (
        f'push budget {s.timeout}s cannot cover media {s.media_timeout} + profiles '
        f'{s.profile_timeout} + POST {s.coop_timeout} = {worst_case}s; a slow '
        f'dependency would drop the item rather than submit it unenriched'
    )


def test_a_malformed_pubkey_says_WHY_rather_than_looking_like_a_dead_lookup(sink):
    """Distinct from a funnelcake failure. 'not a 64-char hex pubkey' tells a moderator
    the report itself carried a bad p-tag; 'lookup did not complete' would blame our
    infrastructure for the reporter's malformed input."""
    _, _, _ = sink
    captured = _drive(
        sink,
        {'Kind': 1984, 'ReportedAuthorPubkey': AUTHOR, 'ReportedPubkey': 'not-a-pubkey', 'EventId': WRAPPER_ID},
    )
    content = captured['payload']['content']
    assert content['reported_profile_state'] == 'lookup_failed'
    assert 'hex' in content['reported_profile_error']
