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
    content_id_for_features,
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


def test_a_raised_failure_is_cached_briefly_then_retries():
    """Caps attacker-driven miss storms without suppressing recovery for long."""
    calls = []

    def flaky(event_id):
        calls.append(event_id)
        if len(calls) == 1:
            raise RuntimeError('transient')
        return _event()

    cache = {}
    clock = iter([0.0, 5.0, 100.0])
    now = lambda: next(clock)  # noqa: E731

    assert resolve_author(EVENT_ID, flaky, cache=cache, now=now) == ''
    # Within the negative TTL: served from cache, no second outbound call.
    assert resolve_author(EVENT_ID, flaky, cache=cache, now=now) == ''
    assert len(calls) == 1
    # Past it: retried and recovered.
    assert resolve_author(EVENT_ID, flaky, cache=cache, now=now) == AUTHOR
    assert len(calls) == 2


def test_an_unresolvable_response_is_cached_briefly_then_retries():
    """Distinct from the raising case: the fetch succeeds but is untrustworthy."""
    calls = []

    def eventually_ok(event_id):
        calls.append(event_id)
        if len(calls) == 1:
            return None
        return _event()

    cache = {}
    clock = iter([0.0, 5.0, 100.0])
    now = lambda: next(clock)  # noqa: E731

    assert resolve_author(EVENT_ID, eventually_ok, cache=cache, now=now) == ''
    assert resolve_author(EVENT_ID, eventually_ok, cache=cache, now=now) == ''
    assert len(calls) == 1
    assert resolve_author(EVENT_ID, eventually_ok, cache=cache, now=now) == AUTHOR
    assert len(calls) == 2


def test_negative_ttl_is_much_shorter_than_the_positive_one():
    """The whole point: a failure must not linger like a success."""
    from reported_author import CACHE_TTL_SECONDS, NEGATIVE_CACHE_TTL_SECONDS

    assert NEGATIVE_CACHE_TTL_SECONDS < CACHE_TTL_SECONDS


def test_repeated_misses_on_distinct_ids_still_each_cost_one_call():
    """Negative caching caps repeats of the SAME id; it cannot dedupe distinct
    ids, so this documents the residual cost rather than pretending otherwise."""
    calls = []
    cache = {}
    for i in range(50):
        resolve_author(f'{i:064x}', lambda e: calls.append(e), cache=cache)
    assert len(calls) == 50


def test_an_id_mismatched_response_is_cached_briefly_then_retries():
    calls = []

    def wrong_then_right(event_id):
        calls.append(event_id)
        if len(calls) == 1:
            return _event(event_id=OTHER_ID)
        return _event()

    cache = {}
    clock = iter([0.0, 100.0])
    now = lambda: next(clock)  # noqa: E731

    assert resolve_author(EVENT_ID, wrong_then_right, cache=cache, now=now) == ''
    assert resolve_author(EVENT_ID, wrong_then_right, cache=cache, now=now) == AUTHOR
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
    # Kind is always present on real features; a video event is not a wrapper.
    assert author_for_features({'Kind': 34236, 'Pubkey': AUTHOR}) == AUTHOR


def test_report_uses_the_resolved_author_not_the_reporter():
    features = {
        'Kind': 1984,
        'Pubkey': WRAPPER_SIGNER,  # the reporter signed the report
        'ReportedEventId': EVENT_ID,
        'ReportedAuthorPubkey': AUTHOR,
    }
    assert author_for_features(features) == AUTHOR


def test_report_with_unresolvable_author_yields_nothing_not_the_reporter():
    """The critical case. Falling back here would target whoever filed the report."""
    features = {
        'Kind': 1984,
        'Pubkey': WRAPPER_SIGNER,
        'ReportedEventId': EVENT_ID,
        'ReportedAuthorPubkey': '',
    }
    assert author_for_features(features) == ''


def test_report_with_no_resolved_feature_at_all_yields_nothing():
    features = {'Kind': 1984, 'Pubkey': WRAPPER_SIGNER, 'ReportedEventId': EVENT_ID}
    assert author_for_features(features) == ''


def test_label_uses_the_target_author_not_the_labeler():
    features = {
        'Kind': 1985,
        'Pubkey': WRAPPER_SIGNER,  # our own moderation identity signs labels
        'LabelTargetEvent': EVENT_ID,
        'LabelTargetAuthorPubkey': AUTHOR,
    }
    assert author_for_features(features) == AUTHOR


def test_label_with_unresolvable_author_yields_nothing_not_our_own_identity():
    features = {
        'Kind': 1985,
        'Pubkey': WRAPPER_SIGNER,
        'LabelTargetEvent': EVENT_ID,
        'LabelTargetAuthorPubkey': '',
    }
    assert author_for_features(features) == ''


def test_kind_decides_not_feature_presence():
    """An event has exactly one Kind, so Kind alone picks the resolved field.

    Constructed dict: a label that also carries report features. Only Kind
    should matter, so the report side must be ignored entirely.
    """
    features = {
        'Kind': 1985,
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
    features = {'Kind': 1984, 'Pubkey': WRAPPER_SIGNER, 'ReportedPubkey': AUTHOR, 'ReportedAuthorPubkey': ''}
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


# --- author_for_features keys on Kind, not on feature presence ---
#
# Inferring "is this a wrapper?" from which optional derived features happen to
# be populated is unsound: a hash-only CSAM label has no LabelTargetEvent, and a
# tag-less report has neither ReportedEventId nor ReportedPubkey. Both then fell
# through to Pubkey, which on a wrapper is the reporter or our own moderation
# identity. Since COOP's `creator` is the reversal target, that put the wrong
# account in front of Unban-User, and will put it in front of the purging
# banpubkey once s-t-s#190 moves forward actions onto `creator`.

MODERATION_IDENTITY = '8fd5eb6d8f362163bc00a5ab6b4a3167dbf32d00ec4efdbcf43b3c9514433b7e'


def test_hash_only_csam_label_never_returns_our_own_identity():
    """ConfirmedCSAMHashOnlyNullTarget is live and emits an actionable verdict.

    LabelTargetEvent is None by construction, so a presence check misses it.
    """
    features = {
        'Kind': 1985,
        'Pubkey': MODERATION_IDENTITY,
        'LabelTargetEvent': None,
        'LabelContentHash': 'a' * 64,
    }
    assert author_for_features(features) == ''


def test_hash_only_csam_label_with_empty_target_never_returns_our_own_identity():
    features = {
        'Kind': 1985,
        'Pubkey': MODERATION_IDENTITY,
        'LabelTargetEvent': '',
        'LabelContentHash': 'a' * 64,
    }
    assert author_for_features(features) == ''


def test_report_with_neither_e_nor_p_tag_never_returns_the_reporter():
    features = {'Kind': 1984, 'Pubkey': WRAPPER_SIGNER}
    assert author_for_features(features) == ''


def test_report_with_empty_e_tag_never_returns_the_reporter():
    features = {'Kind': 1984, 'Pubkey': WRAPPER_SIGNER, 'ReportedEvent': ''}
    assert author_for_features(features) == ''


def test_label_with_only_a_p_tag_never_returns_our_own_identity():
    features = {'Kind': 1985, 'Pubkey': MODERATION_IDENTITY, 'LabelTargetPubkey': AUTHOR}
    assert author_for_features(features) == ''


def test_video_event_uses_its_own_author():
    assert author_for_features({'Kind': 34236, 'Pubkey': AUTHOR}) == AUTHOR


@pytest.mark.parametrize('kind', [1984, '1984'])
def test_kind_is_coerced_so_a_string_still_counts_as_a_wrapper(kind):
    features = {'Kind': kind, 'Pubkey': WRAPPER_SIGNER, 'ReportedEventId': EVENT_ID}
    assert author_for_features(features) == ''


def test_unknown_kind_with_wrapper_indicators_still_refuses():
    """Defence in depth: if Kind is missing or unexpected but the item looks
    wrapped, refuse rather than fall back to the wrapper's signer."""
    features = {'Pubkey': WRAPPER_SIGNER, 'ReportedEventId': EVENT_ID}
    assert author_for_features(features) == ''


def test_label_kind_branch_is_load_bearing_not_masked_by_the_backstop():
    """A malformed label with no target features at all.

    The wrapper-marker backstop cannot catch this one, so only keying on Kind
    prevents falling through to Pubkey, which on a label is our own identity.
    """
    features = {'Kind': 1985, 'Pubkey': MODERATION_IDENTITY}
    assert author_for_features(features) == ''


def test_report_kind_branch_is_load_bearing_not_masked_by_the_backstop():
    features = {'Kind': 1984, 'Pubkey': WRAPPER_SIGNER}
    assert author_for_features(features) == ''


def test_string_kind_resolves_positively_not_just_defensively():
    """Pins the coercion itself.

    Without it, Kind is unrecognised and the backstop returns '' — which looks
    correct but is the wrong reason, and would silently stop resolving authors
    if Kind ever arrived as a string.
    """
    features = {
        'Kind': '1985',
        'Pubkey': WRAPPER_SIGNER,
        'LabelTargetEvent': EVENT_ID,
        'LabelTargetAuthorPubkey': AUTHOR,
    }
    assert author_for_features(features) == AUTHOR


def test_string_kind_resolves_positively_for_reports_too():
    features = {
        'Kind': '1984',
        'Pubkey': WRAPPER_SIGNER,
        'ReportedEventId': EVENT_ID,
        'ReportedAuthorPubkey': AUTHOR,
    }
    assert author_for_features(features) == AUTHOR


def test_bool_is_not_treated_as_a_kind():
    """True == 1 in Python; it must not be coerced into a kind."""
    assert author_for_features({'Kind': True, 'Pubkey': AUTHOR}) == AUTHOR


# --- S1: the marker backstop must cover label bodies too ---


def test_backstop_catches_a_label_body_with_only_label_tags():
    """Kind unparseable, only L/l features present. Without label markers this
    fell through to Pubkey, which on a label is our own moderation identity."""
    features = {'Pubkey': MODERATION_IDENTITY, 'LabelNamespace': 'content-warning', 'LabelValue': 'csam'}
    assert author_for_features(features) == ''


def test_backstop_catches_a_report_body_with_only_a_reason():
    features = {'Pubkey': WRAPPER_SIGNER, 'ReportReason': 'nudity'}
    assert author_for_features(features) == ''


def test_backstop_does_not_treat_ordinary_content_as_wrapped():
    """LabelSignerPubkey is deliberately NOT a marker: it reads $.pubkey and is
    populated on every event, so treating it as one would make every note look
    like a wrapper and stop resolving authors for real content."""
    features = {'Kind': 1, 'Pubkey': AUTHOR, 'LabelSignerPubkey': AUTHOR}
    assert author_for_features(features) == AUTHOR


# --- S2: Kind coercion must not raise on inputs isdigit() accepts ---


@pytest.mark.parametrize('exotic', ['²', '³', '¹', '⑥'])
def test_kind_coercion_does_not_raise_on_superscripts_and_circled_digits(exotic):
    """str.isdigit() is True for these but int() rejects them. The raise escaped
    author_for_features and left COOPSink's own try block."""
    assert author_for_features({'Kind': exotic, 'Pubkey': AUTHOR, 'ReportedEventId': EVENT_ID}) == ''


def test_kind_coercion_rejects_non_ascii_decimals():
    """Arabic-Indic digits convert cleanly but are not a kind we should honour."""
    assert author_for_features({'Kind': '١٩٨٤', 'Pubkey': AUTHOR}) == AUTHOR


# --- S3: a miss storm must not evict legitimate resolutions ---


def test_negative_entries_cannot_evict_positive_ones():
    """The negative TTL introduced this: negatives shared one bounded cache, so
    a storm of distinct-id misses flushed real answers and made honest traffic
    re-fetch, increasing load rather than reducing it."""
    cache = {}
    good = [f'{i:064x}' for i in range(50)]
    for gid in good:
        resolve_author(gid, lambda _, x=gid: {'id': x, 'pubkey': AUTHOR}, cache=cache, now=lambda: 0.0)

    for i in range(3000):
        resolve_author(f'{i + 9000:064x}', lambda _: None, cache=cache, now=lambda: 1.0)

    survivors = [g for g in good if resolve_author(g, lambda _: pytest.fail('refetched'), cache=cache, now=lambda: 2.0)]
    assert len(survivors) == 50


# --- S4: contentId and userId must describe the same event ---


def test_content_id_and_author_describe_the_same_event_for_a_report():
    features = {
        'Kind': 1984,
        'EventId': OTHER_ID,
        'ReportedEventId': EVENT_ID,
        'ReportedAuthorPubkey': AUTHOR,
        'LabelTargetEvent': 'b' * 64,
    }
    assert content_id_for_features(features) == EVENT_ID
    assert author_for_features(features) == AUTHOR


def test_content_id_and_author_describe_the_same_event_for_a_label():
    features = {
        'Kind': 1985,
        'EventId': OTHER_ID,
        'LabelTargetEvent': EVENT_ID,
        'LabelTargetAuthorPubkey': AUTHOR,
        'ReportedEventId': 'b' * 64,
    }
    assert content_id_for_features(features) == EVENT_ID
    assert author_for_features(features) == AUTHOR


def test_content_id_falls_back_to_the_event_itself_for_direct_content():
    assert content_id_for_features({'Kind': 34236, 'EventId': EVENT_ID}) == EVENT_ID


def test_content_id_is_none_when_nothing_identifies_the_content():
    assert content_id_for_features({'Kind': 1984}) is None
