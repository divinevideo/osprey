"""Characterisation tests: pins what COOPSink sends to Coop TODAY.

These do not describe desired behaviour. They describe **current** behaviour, so
that the `/content` -> `/report` split has something to break against. If one of
these fails after a change, that is the point: decide whether the change was
intended, then update the test deliberately.

Why the payload builder is a separate module at all: `coop_sink.py` imports
gevent, requests, sentry_sdk and the osprey engine, and the plugin test step
installs pytest and websocket-client only. Pulling the pure part out follows the
pattern already used by `media_hash.py`, `reported_author.py`, and
`trusted_moderation.py` -- logic lifted out of a sink precisely so it can be
tested, with no new CI dependency.

That is a readability argument, NOT an impossibility one. An earlier version of
this docstring claimed the sink "cannot be imported here"; it can, by stubbing
those modules into `sys.modules`, and `test_coop_sink_payload_wire.py` now does
exactly that. The correction matters, because the false premise is what justified
asserting on the call site's *syntax* -- and syntax cannot see a variable rebound
on the line above the call. That is precisely how the reporter-as-creator
regression slipped through all 302 of these.

The I/O stays in the sink: the media lookup mutates the returned dict afterwards,
and the POST is unchanged. This module is only the part that turns features into
fields.

Every test below was mutation-checked -- the behaviour it pins was changed in the
source and the test confirmed to fail. Review then defeated the suite anyway, and
re-running the battery after the fix found two more survivors, because the fixture
values were already lowercase and unpadded, making `.lower()`, `.strip()` and
`str()` identity operations. The battery now stands at 12/12. Treat this as "these
specific behaviours are pinned", never as "the payload is fully covered": mutation
checking says nothing about the mutations nobody thought to try.
"""

import ast
from pathlib import Path

import pytest
from coop_payload import build_content_fields

AUTHOR = 'bc02e0a6c0f01ad9cb57a2b0f8ef8241bc5ff979ce4452ce9e243de457756725'
REPORTER = '19f9afb8f855f5e86b5bea160e78ec5871648b10dedb043f4806fca8ce50e4d3'
EVENT = '3f8a1c9e77b24d6e05a4c3b81f9e2d70a6c5b4938e7f1a2d0c9b8a7f6e5d4c3b'
WRAPPER = '9d1e4f7a3b8c2059e6f4a1d3c7b092e58a4f6d1c3e9b7a250f8c6d4b2a1e3f90'
# Deliberately NOT the production default. An earlier version used
# 'https://media.divine.video', the same value the sink falls back to, so
# hardcoding the prod host in the source passed every test.
MEDIA_BASE = 'https://media.test.invalid'


def build(features=None, **kw):
    """Call with today's defaults, overridable per test."""
    kw.setdefault('content_id', EVENT)
    kw.setdefault('wrapper_event_id', WRAPPER)
    kw.setdefault('author', AUTHOR)
    kw.setdefault('verdict', 'flag_for_review')
    kw.setdefault('action_name', 'nostr_kind_1984')
    kw.setdefault('media_base_url', MEDIA_BASE)
    return build_content_fields(features or {}, **kw)


# --- the always-present base -------------------------------------------------


def test_base_fields_are_always_present_even_with_no_features():
    got = build({})
    assert got['event_id'] == EVENT
    assert got['source_event_id'] == WRAPPER
    assert got['pubkey'] == AUTHOR
    assert got['verdict'] == 'flag_for_review'
    assert got['action_name'] == 'nostr_kind_1984'
    # Present as keys even when the feature is absent, carrying None.
    assert 'kind' in got and got['kind'] is None
    assert 'created_at' in got and got['created_at'] is None


def test_pubkey_is_the_resolved_author_not_the_reporter():
    """The single most consequential field. It becomes Coop's creator, which
    drives Unban-User / Unsuspend-User, so naming the reporter here would point
    reversals at the wrong person."""
    got = build({'ReportedPubkey': REPORTER}, author=AUTHOR)
    assert got['pubkey'] == AUTHOR
    assert got['reported_pubkey'] == REPORTER


def test_an_unresolved_author_is_carried_as_empty_string_not_dropped():
    """Author resolution fails closed. The empty string must still be SENT, so
    Coop records no creator and the adapter refuses loudly, rather than the key
    going missing and the value being inferred downstream."""
    got = build({}, author='')
    assert got['pubkey'] == ''


def test_kind_and_created_at_pass_through_unchanged():
    got = build({'Kind': 34236, 'CreatedAt': 1786637235})
    assert got['kind'] == 34236
    assert got['created_at'] == 1786637235


# --- conditional fields: falsy is OMITTED, not sent empty ---------------------


@pytest.mark.parametrize(
    'feature,field',
    [
        ('ReportReason', 'report_reason'),
        ('ReportedPubkey', 'reported_pubkey'),
        ('LabelValue', 'label_value'),
        ('LabelNamespace', 'label_namespace'),
    ],
)
def test_optional_fields_are_included_when_truthy(feature, field):
    got = build({feature: 'x'})
    assert got[field] == 'x'


@pytest.mark.parametrize(
    'feature,field',
    [
        ('ReportReason', 'report_reason'),
        ('ReportedPubkey', 'reported_pubkey'),
        ('ReportedEventId', 'reported_event_id'),
        ('LabelValue', 'label_value'),
        ('LabelNamespace', 'label_namespace'),
        ('NoteText', 'text'),
    ],
)
def test_optional_fields_are_omitted_when_absent(feature, field):
    assert field not in build({})


@pytest.mark.parametrize(
    'feature,field',
    [
        ('ReportReason', 'report_reason'),
        ('ReportedPubkey', 'reported_pubkey'),
        ('LabelValue', 'label_value'),
        ('NoteText', 'text'),
    ],
)
def test_optional_fields_are_omitted_when_empty_string(feature, field):
    """Guarded on truthiness, not presence. An empty string is dropped entirely
    rather than sent as ''. Coop then shows no row instead of a blank one."""
    assert field not in build({feature: ''})


def test_pass_through_fields_are_carried_verbatim():
    """Not coerced, not truncated, not normalised. ReportedEventId is the ONLY
    field deliberately transformed.

    **The fixture values are the test.** An earlier version used 'nudity' and
    'nsfw', which are already lowercase, unpadded strings, so str(), .lower() and
    .strip() were all identity on them and six separate coercion mutations passed
    against the one test whose whole purpose is pass-through. Every value below is
    chosen so that a transformation CHANGES it: mixed case, surrounding whitespace,
    and one non-string.

    label_namespace matters most. Lowercasing it would collide the three disjoint
    kind-1985 namespaces.
    """
    long_text = '  Six Second Loop  ' + 'x' * 900
    got = build(
        {
            'NoteText': long_text,
            'ReportReason': '  Nudity  ',
            'LabelValue': '  NSFW  ',
            'LabelNamespace': '  Moderation/Resolution  ',
            'ReportedPubkey': 12345,
        }
    )
    assert got['text'] == long_text, 'text must not be truncated, stripped or coerced'
    assert got['report_reason'] == '  Nudity  '
    assert got['label_value'] == '  NSFW  '
    assert got['label_namespace'] == '  Moderation/Resolution  ', (
        'case must be preserved: lowercasing collides the three disjoint 1985 namespaces'
    )
    assert got['reported_pubkey'] == 12345, 'carried as given; only ReportedEventId is str()-coerced'
    assert not isinstance(got['reported_pubkey'], str)


def test_non_string_pass_through_values_are_not_stringified():
    """Separate from the test above because str() is INVISIBLE against a string.

    Mixed case and whitespace catch .lower() and .strip(), but `str('  Nudity  ')`
    is identity, so a str() coercion survived every assertion until this existed.
    Only a non-string value can see it. Osprey features are typed by the SML rule
    that produced them, so a numeric value reaching one of these is possible;
    ReportedEventId is the one field where we accept the coercion on purpose.
    """
    got = build({'ReportReason': 42, 'LabelValue': 7, 'LabelNamespace': 3})
    assert got['report_reason'] == 42
    assert got['label_value'] == 7
    assert got['label_namespace'] == 3
    assert not any(isinstance(got[k], str) for k in ('report_reason', 'label_value', 'label_namespace'))


def test_detector_confidence_is_not_coerced():
    got = build({'DetectorContentHash': EVENT, 'DetectorConfidence': 1}, action_name=DETECTOR)
    assert got['confidence'] == 1
    assert isinstance(got['confidence'], int)


def test_note_text_becomes_the_text_field():
    got = build({'NoteText': 'six second loop'})
    assert got['text'] == 'six second loop'


def test_reported_event_id_is_coerced_to_string():
    """Alone among the optional fields, this one is str()-wrapped: it arrives
    from a report's e-tag and is not guaranteed to be a string."""
    got = build({'ReportedEventId': 12345})
    assert got['reported_event_id'] == '12345'
    assert isinstance(got['reported_event_id'], str)


def test_reported_event_id_zero_is_omitted_not_sent_as_string_zero():
    """Consequence of the truthiness guard, pinned because it is surprising."""
    assert 'reported_event_id' not in build({'ReportedEventId': 0})


# --- the AI-detector branch --------------------------------------------------

DETECTOR = 'ai_detector_nsfw'


def test_detector_builds_media_url_from_the_trusted_base_and_content_id():
    """Never the caller-supplied URL carried in the Action for diagnostics."""
    got = build(
        {'DetectorContentHash': EVENT, 'DetectorVideoUrl': 'https://evil.example/x.mp4'},
        action_name=DETECTOR,
    )
    assert got['media_url'] == f'{MEDIA_BASE}/{EVENT}'


def test_detector_overrides_label_namespace_and_value():
    got = build(
        {
            'DetectorContentHash': EVENT,
            'DetectorClass': 'nudity',
            'LabelNamespace': 'from-the-label',
            'LabelValue': 'from-the-label',
        },
        action_name=DETECTOR,
    )
    assert got['label_namespace'] == 'content-warning'
    assert got['label_value'] == 'nudity'


def test_detector_defaults_class_confidence_and_model():
    got = build({'DetectorContentHash': EVENT}, action_name=DETECTOR)
    assert got['label_value'] == 'nsfw'
    assert got['confidence'] == 0
    assert got['model'] == ''


def test_detector_branch_requires_the_hash_to_MATCH_the_content_id():
    """Both conditions, not either. A detector action whose hash names different
    bytes than the item must not get a playable URL built for the item."""
    got = build({'DetectorContentHash': 'a' * 64}, action_name=DETECTOR)
    assert 'media_url' not in got
    assert 'confidence' not in got


def test_detector_branch_does_not_fire_when_the_hash_is_ABSENT():
    """Distinct from the mismatch case, and the more dangerous one.

    Guarding with `features.get('DetectorContentHash', content_id) == content_id`
    survives the mismatch test, but fabricates a media_url, a content-warning
    namespace and an 'nsfw' label for bytes that were never classified.
    """
    got = build(
        {'LabelNamespace': 'moderation/resolution', 'LabelValue': 'dismissed'},
        action_name=DETECTOR,
    )
    assert 'media_url' not in got
    # Supplied rather than absent, so this proves the branch did not OVERRIDE them,
    # instead of passing for two reasons at once.
    assert got['label_namespace'] == 'moderation/resolution'
    assert got['label_value'] == 'dismissed'


def test_detector_branch_adds_exactly_five_fields_and_no_others():
    """Pins the branch's OUTPUT, not just that it fired. This is where an
    accidental passthrough of the caller-controlled DetectorVideoUrl would land."""
    base = set(build({}))
    got = build(
        {'DetectorContentHash': EVENT, 'DetectorVideoUrl': 'https://evil.example/x.mp4'},
        action_name=DETECTOR,
    )
    assert set(got) - base == {'media_url', 'label_namespace', 'label_value', 'confidence', 'model'}


def test_detector_branch_does_not_fire_for_other_actions():
    got = build({'DetectorContentHash': EVENT}, action_name='nostr_kind_1984')
    assert 'media_url' not in got
    assert 'model' not in got


def test_non_detector_actions_never_get_a_media_url_here():
    """Media for reports and labels is resolved later, from the relay, by the
    sink. This function must not invent one."""
    got = build({'Kind': 34236, 'NoteText': 'a video'})
    assert 'media_url' not in got
    assert 'media_thumbnail' not in got


# --- a full production-shaped report -----------------------------------------


def test_a_fully_populated_report_produces_the_expected_field_set():
    """Locks the whole shape, so an added or removed field is visible as a diff
    rather than being noticed only in Coop.

    Order is pinned as characterisation, NOT because Coop depends on it. Coop's
    getPrimaryContentFields iterates the REGISTERED SCHEMA and looks up
    content[field.name], so what a moderator sees is ordered by the schema, not by
    our key order. A reorder here is therefore harmless to moderators; do not
    refuse one on this test's account, just update it deliberately.
    """
    got = build(
        {
            'Kind': 34236,
            'CreatedAt': 1786637235,
            'ReportReason': 'nudity',
            'ReportedPubkey': AUTHOR,
            'ReportedEventId': EVENT,
            'NoteText': 'six second loop, kitchen dance',
        }
    )
    assert list(got) == [
        'event_id',
        'source_event_id',
        'pubkey',
        'kind',
        'created_at',
        'verdict',
        'action_name',
        'report_reason',
        'reported_pubkey',
        'reported_event_id',
        'text',
    ], 'field ORDER as well as membership, pinned as pure characterisation'


# --- the call site ------------------------------------------------------------
#
# Everything above proves the FUNCTION is right. None of it proves the SINK calls
# it correctly. The wire test covers runtime values, while these smaller static
# checks give a direct failure when the builder signature and call site drift.

_SINK = Path(__file__).resolve().parents[1] / 'src' / 'services' / 'coop_sink.py'
_MODULE = Path(__file__).resolve().parents[1] / 'src' / 'coop_payload.py'


def _signature():
    tree = ast.parse(_MODULE.read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'build_content_fields')
    return fn


def _call_site():
    tree = ast.parse(_SINK.read_text())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'build_content_fields'
    ]
    assert len(calls) == 1, f'expected exactly one call site, found {len(calls)}'
    return calls[0]


def test_the_sink_passes_every_required_keyword():
    fn = _signature()
    required = {a.arg for a in fn.args.kwonlyargs}
    passed = {k.arg for k in _call_site().keywords if k.arg is not None}
    missing = required - passed
    assert not missing, (
        f'coop_sink.py calls build_content_fields without {sorted(missing)}. '
        f'This raises TypeError on the first real event and no other test here can see it.'
    )


def test_the_sink_passes_no_unknown_keyword():
    fn = _signature()
    known = {a.arg for a in fn.args.kwonlyargs} | {a.arg for a in fn.args.args}
    passed = {k.arg for k in _call_site().keywords if k.arg is not None}
    unknown = passed - known
    assert not unknown, (
        f'coop_sink.py passes {sorted(unknown)}, which build_content_fields does not accept. '
        f'Raises TypeError at runtime.'
    )


def test_the_sink_passes_the_RIGHT_EXPRESSION_for_every_keyword():
    """Names alone are not enough, and this gap already shipped once.

    An earlier version of these tests compared only keyword NAMES against the
    signature. Every one of these passed the full suite:

        author=features.get('ReportedPubkey', '')   <- reporter as creator
        verdict=result.action.action_name           <- swapped with action_name
        media_base_url=self._url                    <- Coop's API URL as media host
        content_id=wrapper_event_id                 <- swapped with wrapper

    The first is the exact regression recorded in task #21: Coop's `creator`
    naming the reporter, so Unban-User and Unsuspend-User reverse against the
    person who FILED the report. `test_pubkey_is_the_resolved_author_not_the_reporter`
    does not catch it -- that pins the function, and the sink is what runs.

    There is no other guard: mypy excludes `divine/` (.pre-commit-config.yaml), so
    a wrong expression of the right type is caught by nothing else in CI.
    """
    expected = {
        'content_id': 'content_id',
        'wrapper_event_id': 'wrapper_event_id',
        'author': 'author',
        'verdict': 'verdict.verdict',
        'action_name': 'result.action.action_name',
        'media_base_url': 'self._media_base_url',
        'user_type_id': 'self._user_type_id',
    }
    actual = {kw.arg: ast.unparse(kw.value) for kw in _call_site().keywords if kw.arg is not None}
    assert actual == expected, (
        f'coop_sink.py passes the wrong expression for one or more keywords.\n'
        f'  expected: {expected}\n  actual:   {actual}'
    )


def test_the_sink_passes_the_features_dict_positionally():
    """The one positional parameter, and it must be `features` itself."""
    call = _call_site()
    assert len(call.args) == 1, f'expected features passed positionally, found {len(call.args)} positional args'
    assert ast.unparse(call.args[0]) == 'features'


class TestAuthorRelatedItem:
    """Coop's `author` field is RELATED_ITEM, and `creatorId` points at it. Without it the
    Associated User panel does not render and none of Ban/Suspend/Unban/Unsuspend-User can be
    exercised — half of moderation, silently absent.

    EMIT OR OMIT, never a placeholder: Coop rejects the WHOLE submission with a 400 if a
    RELATED_ITEM carries an empty id, so a missing author must drop the key rather than send
    {'id': ''}. Losing one item's account panel is recoverable; losing the item is not.
    """

    def test_emits_the_author_as_a_related_item(self):
        # Features carry BOTH a claimed p-tag and the wrapper's own signer, so this
        # distinguishes "the resolved author" from "a value that happened to be nearby".
        # Without them, `{'id': features.get('ReportedPubkey') or author}` passes — and that
        # mutant hands a reporter the power to get someone else's account actioned.
        content = build_content_fields(
            {'Kind': 1984, 'Pubkey': 'd' * 64, 'ReportedPubkey': 'e' * 64},
            content_id='a' * 64,
            wrapper_event_id='b' * 64,
            author='c' * 64,
            verdict='flag_for_review',
            action_name='nostr_kind_1984',
            media_base_url='https://media.test',
            user_type_id='9f8b0bb15fd',
        )
        assert content['author'] == {'id': 'c' * 64, 'typeId': '9f8b0bb15fd'}
        # The RELATED_ITEM id and the pubkey string must describe the SAME account.
        assert content['author']['id'] == content['pubkey'] == 'c' * 64
        assert content['author']['id'] != 'e' * 64, "the reporter's claim must never be the subject"

    def test_still_carries_the_pubkey_string_separately(self):
        """`pubkey` is a STRING field and cannot hold the role; both must be present."""
        content = build_content_fields(
            {'Kind': 1984},
            content_id='a' * 64,
            wrapper_event_id='b' * 64,
            author='c' * 64,
            verdict='flag_for_review',
            action_name='nostr_kind_1984',
            media_base_url='https://media.test',
            user_type_id='9f8b0bb15fd',
        )
        assert content['pubkey'] == 'c' * 64

    def test_normalizes_the_author_id_so_casing_cannot_fork_one_account_into_two(self):
        """Coop keys related items by id string. An uppercase pubkey emitted raw would be a
        SECOND, distinct user item for the same account -- moderating one would leave the
        other untouched."""
        content = build_content_fields(
            {'Kind': 1984},
            content_id='a' * 64,
            wrapper_event_id='b' * 64,
            author='C' * 64,
            verdict='flag_for_review',
            action_name='nostr_kind_1984',
            media_base_url='https://media.test',
            user_type_id='9f8b0bb15fd',
        )
        assert content['author']['id'] == 'c' * 64

    def test_omits_author_when_the_id_is_not_a_64_char_hex_pubkey(self):
        """Emit-or-omit on SHAPE, not truthiness. Coop accepts any id with length > 0, so a
        junk value is not a 400 -- it silently creates a related user item a moderator can
        then Ban. Non-wrapper actions (detector, repeat_offender) reach here with a pubkey
        that was never signature-verified."""
        for junk in ['   ', 'not-hex', 'c' * 63, 'zz' + 'c' * 62]:
            content = build_content_fields(
                {'Kind': 1984},
                content_id='a' * 64,
                wrapper_event_id='b' * 64,
                author=junk,
                verdict='flag_for_review',
                action_name='nostr_kind_1984',
                media_base_url='https://media.test',
                user_type_id='9f8b0bb15fd',
            )
            assert 'author' not in content, f'{junk!r} must not become a related user item'

    def test_omits_author_entirely_when_it_could_not_be_resolved(self):
        content = build_content_fields(
            {'Kind': 1984},
            content_id='a' * 64,
            wrapper_event_id='b' * 64,
            author='',
            verdict='flag_for_review',
            action_name='nostr_kind_1984',
            media_base_url='https://media.test',
            user_type_id='9f8b0bb15fd',
        )
        assert 'author' not in content, 'an empty RELATED_ITEM id 400s the whole submission'

    def test_omits_author_when_the_user_type_is_not_configured(self):
        """Until iac plumbs DIVINE_COOP_USER_TYPE_ID the field cannot be built, and a
        guessed typeId would be rejected for every item."""
        content = build_content_fields(
            {'Kind': 1984},
            content_id='a' * 64,
            wrapper_event_id='b' * 64,
            author='c' * 64,
            verdict='flag_for_review',
            action_name='nostr_kind_1984',
            media_base_url='https://media.test',
            user_type_id='',
        )
        assert 'author' not in content
