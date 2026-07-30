"""Tests for authoritative resolution of a reported event's author.

Covers the pure logic with an injected fetcher, so no network is involved.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from reported_author import (  # noqa: E402
    author_for_features,
    extract_author,
    normalize_event_id,
    resolve_author,
)

EVENT_ID = 'a' * 64
AUTHOR = 'b' * 64
OTHER_ID = 'c' * 64
WRAPPER_SIGNER = 'd' * 64  # the reporter, or our own moderation identity


def _event(event_id=EVENT_ID, pubkey=AUTHOR):
    return {'id': event_id, 'pubkey': pubkey}


# --- extract_author: the response-verification guard ---


def test_returns_author_for_a_matching_event():
    assert extract_author(_event(), EVENT_ID) == AUTHOR


def test_rejects_an_event_whose_id_does_not_match_what_we_asked_for():
    """Security: otherwise a relay could substitute a different event's author."""
    assert extract_author(_event(event_id=OTHER_ID), EVENT_ID) == ''


def test_rejects_an_event_with_no_id():
    assert extract_author({'pubkey': AUTHOR}, EVENT_ID) == ''


def test_rejects_a_malformed_author_pubkey():
    assert extract_author(_event(pubkey='not-a-pubkey'), EVENT_ID) == ''


def test_rejects_a_missing_author_pubkey():
    assert extract_author({'id': EVENT_ID}, EVENT_ID) == ''


@pytest.mark.parametrize('payload', [None, [], 'string', 42])
def test_rejects_non_dict_payloads(payload):
    assert extract_author(payload, EVENT_ID) == ''


def test_normalizes_case_on_both_sides():
    assert extract_author(_event(event_id=EVENT_ID.upper(), pubkey=AUTHOR.upper()), EVENT_ID) == AUTHOR


# --- resolve_author: validation, caching, failure handling ---


def test_refuses_a_malformed_event_id_without_calling_the_relay():
    calls = []

    def fetch(_):
        calls.append(_)
        return _event()

    assert resolve_author('not-hex', fetch, cache={}) == ''
    assert calls == []


def test_refuses_an_empty_event_id_without_calling_the_relay():
    calls = []
    assert resolve_author('', lambda e: calls.append(e), cache={}) == ''
    assert calls == []


def test_returns_the_resolved_author():
    assert resolve_author(EVENT_ID, lambda _: _event(), cache={}) == AUTHOR


def test_fails_closed_when_the_fetch_raises():
    """A resolution failure must yield no author, never a guessed one."""

    def boom(_):
        raise RuntimeError('relay unreachable')

    assert resolve_author(EVENT_ID, boom, cache={}) == ''


def test_fails_closed_when_the_event_is_not_found():
    assert resolve_author(EVENT_ID, lambda _: None, cache={}) == ''


def test_caches_a_successful_resolution():
    calls = []

    def fetch(event_id):
        calls.append(event_id)
        return _event()

    cache = {}
    assert resolve_author(EVENT_ID, fetch, cache=cache) == AUTHOR
    assert resolve_author(EVENT_ID, fetch, cache=cache) == AUTHOR
    assert len(calls) == 1


def test_does_not_cache_failures_so_transient_errors_retry():
    calls = []

    def flaky(event_id):
        calls.append(event_id)
        if len(calls) == 1:
            raise RuntimeError('transient')
        return _event()

    cache = {}
    assert resolve_author(EVENT_ID, flaky, cache=cache) == ''
    assert resolve_author(EVENT_ID, flaky, cache=cache) == AUTHOR
    assert len(calls) == 2


def test_does_not_cache_an_unresolvable_response():
    """Distinct from the raising case: the fetch succeeds but is untrustworthy.

    A not-found or id-mismatched answer must not pin an empty result for the
    whole TTL, or one bad reply blocks resolution until it expires.
    """
    calls = []

    def eventually_ok(event_id):
        calls.append(event_id)
        if len(calls) == 1:
            return None
        return _event()

    cache = {}
    assert resolve_author(EVENT_ID, eventually_ok, cache=cache) == ''
    assert resolve_author(EVENT_ID, eventually_ok, cache=cache) == AUTHOR
    assert len(calls) == 2


def test_does_not_cache_an_id_mismatched_response():
    calls = []

    def wrong_then_right(event_id):
        calls.append(event_id)
        if len(calls) == 1:
            return _event(event_id=OTHER_ID)
        return _event()

    cache = {}
    assert resolve_author(EVENT_ID, wrong_then_right, cache=cache) == ''
    assert resolve_author(EVENT_ID, wrong_then_right, cache=cache) == AUTHOR
    assert len(calls) == 2


def test_cache_entries_expire():
    calls = []

    def fetch(event_id):
        calls.append(event_id)
        return _event()

    cache = {}
    # One tick per call, which only holds because resolve_author reads the clock
    # exactly once.
    clock = iter([0.0, 10_000.0])
    now = lambda: next(clock)  # noqa: E731

    assert resolve_author(EVENT_ID, fetch, cache=cache, now=now) == AUTHOR
    assert resolve_author(EVENT_ID, fetch, cache=cache, now=now) == AUTHOR
    assert len(calls) == 2


def test_a_call_reads_the_clock_exactly_once():
    """Guards the invariant the expiry test above depends on."""
    ticks = []

    def now():
        ticks.append(len(ticks))
        return float(len(ticks))

    resolve_author(EVENT_ID, lambda _: _event(), cache={}, now=now)
    assert len(ticks) == 1


def test_cached_entry_is_returned_without_reading_the_clock_twice():
    cache = {}
    resolve_author(EVENT_ID, lambda _: _event(), cache=cache, now=lambda: 0.0)
    ticks = []

    def now():
        ticks.append(len(ticks))
        return 1.0

    assert resolve_author(EVENT_ID, lambda _: pytest.fail('should not refetch'), cache=cache, now=now) == AUTHOR
    assert len(ticks) == 1


def test_cache_is_bounded():
    cache = {}
    for i in range(1200):
        event_id = f'{i:064x}'
        resolve_author(event_id, lambda _, e=event_id: _event(event_id=e, pubkey=AUTHOR), cache=cache, max_size=1000)
    assert len(cache) <= 1000


# --- author_for_features: which pubkey COOP is told to act on ---


def test_direct_content_uses_the_events_own_author():
    assert author_for_features({'Pubkey': AUTHOR}) == AUTHOR


def test_report_uses_the_resolved_author_not_the_reporter():
    features = {
        'Pubkey': WRAPPER_SIGNER,  # the reporter signed the report
        'ReportedEventId': EVENT_ID,
        'ReportedAuthorPubkey': AUTHOR,
    }
    assert author_for_features(features) == AUTHOR


def test_report_with_unresolvable_author_yields_nothing_not_the_reporter():
    """The critical case. Falling back here would target whoever filed the report."""
    features = {
        'Pubkey': WRAPPER_SIGNER,
        'ReportedEventId': EVENT_ID,
        'ReportedAuthorPubkey': '',
    }
    assert author_for_features(features) == ''


def test_report_with_no_resolved_feature_at_all_yields_nothing():
    features = {'Pubkey': WRAPPER_SIGNER, 'ReportedEventId': EVENT_ID}
    assert author_for_features(features) == ''


def test_label_uses_the_target_author_not_the_labeler():
    features = {
        'Pubkey': WRAPPER_SIGNER,  # our own moderation identity signs labels
        'LabelTargetEvent': EVENT_ID,
        'LabelTargetAuthorPubkey': AUTHOR,
    }
    assert author_for_features(features) == AUTHOR


def test_label_with_unresolvable_author_yields_nothing_not_our_own_identity():
    features = {
        'Pubkey': WRAPPER_SIGNER,
        'LabelTargetEvent': EVENT_ID,
        'LabelTargetAuthorPubkey': '',
    }
    assert author_for_features(features) == ''


def test_label_takes_precedence_over_report_mirroring_content_id_resolution():
    """userId must describe the same event that becomes contentId."""
    features = {
        'LabelTargetEvent': EVENT_ID,
        'LabelTargetAuthorPubkey': AUTHOR,
        'ReportedEventId': OTHER_ID,
        'ReportedAuthorPubkey': WRAPPER_SIGNER,
    }
    assert author_for_features(features) == AUTHOR


@pytest.mark.parametrize('features', [None, [], 'string', 42])
def test_non_dict_features_yield_nothing(features):
    assert author_for_features(features) == ''


def test_missing_pubkey_on_direct_content_yields_nothing():
    assert author_for_features({}) == ''


def test_p_only_report_yields_nothing_not_the_reporter():
    """NIP-56 allows a report with only a p-tag, e.g. a profile report.

    The bridge sets reported_event_id only when an e-tag exists, so there is no
    event to resolve an author from. Falling through to Pubkey here would name
    the reporter, which is the bug this module exists to prevent.
    """
    features = {
        'Pubkey': WRAPPER_SIGNER,  # the reporter
        'ReportedPubkey': AUTHOR,  # their unverified claim
    }
    assert author_for_features(features) == ''


def test_p_only_report_does_not_trust_the_claimed_pubkey_either():
    features = {'Pubkey': WRAPPER_SIGNER, 'ReportedPubkey': AUTHOR, 'ReportedAuthorPubkey': ''}
    assert author_for_features(features) == ''


# --- normalize_event_id: keeps attacker-controlled ids out of logs and URLs ---


def test_normalize_returns_the_validated_id():
    assert normalize_event_id(EVENT_ID.upper()) == EVENT_ID


HOSTILE_IDS = [
    'not-hex',
    f'{EVENT_ID}\r\nINJECTED log line',
    '../../etc/passwd',
    f'{EVENT_ID}/../../admin',
    'a' * 5000,
    '',
    None,
    42,
    ['a' * 64],
]


@pytest.mark.parametrize('hostile', HOSTILE_IDS)
def test_normalize_rejects_hostile_input(hostile):
    """Anything that is not exactly 64 hex is refused, so it reaches neither a
    URL path nor a log line."""
    assert normalize_event_id(hostile) == ''


@pytest.mark.parametrize('value', HOSTILE_IDS + [EVENT_ID, f'  {EVENT_ID}  ', f'{EVENT_ID}\n', EVENT_ID.upper()])
def test_normalize_output_is_always_clean(value):
    """The security property, stated directly: whatever goes in, what comes out
    is either empty or exactly 64 lowercase hex. Nothing else can reach a log
    line or a URL path, so there is no room for injected content.

    Surrounding whitespace is tolerated and stripped, so a sloppy e-tag still
    resolves while the newline never survives into the output.
    """
    out = normalize_event_id(value)
    assert out == '' or re.fullmatch(r'[0-9a-f]{64}', out)
