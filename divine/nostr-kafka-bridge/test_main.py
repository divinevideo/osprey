"""Unit tests for the report-reason normalization in the Nostr->Kafka bridge.

This is the single point where every kind-1984 report's reason is mapped to the
canonical token COOP routes on. A wrong alias here silently routes a CSAM or
child-safety report to General Review, so the mapping is covered explicitly.

Pure functions only -- kafka/websockets are stubbed so this runs with no external
deps (the bridge ships its own requirements.txt separate from the osprey worker).
Run: `python divine/nostr-kafka-bridge/test_main.py` or `pytest` against this file.
"""

import asyncio
import importlib.util
import json
import re
import sys
import types
from pathlib import Path

# Stub the heavy imports main.py pulls at module load, so we can import the pure
# helpers without kafka/websockets installed.
for _name in ('kafka', 'websockets'):
    _mod = types.ModuleType(_name)
    if _name == 'kafka':
        _mod.KafkaProducer = object  # type: ignore[attr-defined]
    sys.modules.setdefault(_name, _mod)

_spec = importlib.util.spec_from_file_location('bridge_main', Path(__file__).with_name('main.py'))
assert _spec and _spec.loader
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)

# Tokens COOP gives a dedicated category queue. Everything else falls to General
# Review by the default rule. This is the one cross-repo constant (it mirrors a COOP
# fact, coop/divine/coop-setup-org.sh); test_routed_tokens_are_canonical guards it
# against bridge.CANONICAL_REASONS so it cannot list a token the bridge never emits.
ROUTED = {'csam', 'child_safety', 'underage_user', 'nudity', 'violence', 'harassment'}

# Live SML rules that actually act on report reasons. Parsed (not hand-listed) so the
# coupling tests break the moment a routed token loses its rule.
_RULES_DIR = Path(__file__).resolve().parent.parent / 'rules' / 'rules' / 'reports'


def _rule_reason_tokens():
    """Every report_reason token an SML rule in divine/rules matches on.

    Reads the live .sml files and pulls tokens from both `ReportReason == 'x'` and
    `ReportReason in ['x', 'y']` forms, so the check reflects the rules as they are
    rather than a second maintained list.
    """
    tokens = set()
    eq = re.compile(r"ReportReason\s*==\s*'([^']+)'")
    in_list = re.compile(r'ReportReason\s+in\s*\[([^\]]+)\]')
    for path in sorted(_RULES_DIR.glob('*.sml')):
        # Strip line comments first so a ReportReason literal inside a '#' comment
        # (e.g. an example in a docstring) cannot inject a phantom token.
        text = '\n'.join(line.split('#', 1)[0] for line in path.read_text().splitlines())
        tokens.update(eq.findall(text))
        for group in in_list.findall(text):
            tokens.update(re.findall(r"'([^']+)'", group))
    return tokens


def _reason(event):
    """report_reason that the bridge attaches to a wrapped event (or None)."""
    return bridge._wrap_nostr_event(event)['data']['data'].get('report_reason')


def _event(tags, content=''):
    return {
        'kind': 1984,
        'id': 'a' * 64,
        'pubkey': 'b' * 64,
        'created_at': 1,
        'content': content,
        'tags': tags,
    }


def _mobile(nip56_type, ns_label):
    # divine-mobile: NIP-56 type in the e/p 3rd element + the specific subtype in a
    # social.nos.ontology NIP-32 label (the label is what the bridge keys on).
    return _event(
        [
            ['e', 'e' * 64, nip56_type],
            ['p', 'p' * 64, nip56_type],
            ['L', 'social.nos.ontology'],
            ['l', ns_label, 'social.nos.ontology'],
        ]
    )


def _web(ns_label, raw_reason):
    # divine-web: raw reason in the e/p 3rd element + an NS- ontology label.
    return _event(
        [
            ['e', 'e' * 64, raw_reason],
            ['p', 'p' * 64, raw_reason],
            ['L', 'social.nos.ontology'],
            ['l', ns_label, 'social.nos.ontology'],
        ]
    )


def test_normalize_aliases():
    cases = {
        'sexual_minors': 'csam',
        'ns-csam': 'csam',
        'csam': 'csam',
        'child-safety': 'child_safety',
        'childsafety': 'child_safety',
        'underage-user': 'underage_user',
        'underageuser': 'underage_user',
        'sexual-content': 'nudity',
        'sexualcontent': 'nudity',
        'pornography': 'nudity',
        'ns-violence': 'violence',
        'vi': 'violence',
        'profanity': 'harassment',
        'aigenerated': 'ai_generated',
        'ns-other': 'other',
        'spam': 'spam',
        'illegal': 'illegal',
    }
    for raw, expected in cases.items():
        assert bridge._normalize_report_reason(raw) == expected, raw
        # Casing/whitespace must not matter.
        assert bridge._normalize_report_reason(f'  {raw.upper()} ') == expected, raw


def test_mobile_subtypes_route_correctly():
    cases = [
        ('illegal', 'NS-csam', 'csam'),
        ('other', 'NS-childSafety', 'child_safety'),
        ('other', 'NS-underageUser', 'underage_user'),
        ('nudity', 'NS-sexualContent', 'nudity'),
        ('illegal', 'NS-violence', 'violence'),
    ]
    for nip56_type, ns_label, expected in cases:
        assert _reason(_mobile(nip56_type, ns_label)) == expected, ns_label
        assert expected in ROUTED


def test_web_hyphenated_subtypes_route_correctly():
    cases = [
        ('NS-csam', 'csam', 'csam'),
        ('NS-child-safety', 'child-safety', 'child_safety'),
        ('NS-underage-user', 'underage-user', 'underage_user'),
        ('NS-sexual-content', 'sexual-content', 'nudity'),
    ]
    for ns_label, raw, expected in cases:
        assert _reason(_web(ns_label, raw)) == expected, ns_label
        assert expected in ROUTED


def test_generic_report_tag_and_mod_label():
    # Priority #1: explicit 'report' tag.
    assert _reason(_event([['e', 'e' * 64, ''], ['report', 'csam']])) == 'csam'
    # Priority #3: MOD-namespace label.
    assert _reason(_event([['e', 'e' * 64, ''], ['l', 'VI', 'MOD']])) == 'violence'


def test_bare_illegal_does_not_become_csam():
    # A NIP-56 report with no ontology label (e.g. a third-party client) carries only
    # the ambiguous 'illegal' type. It must NOT be promoted to csam -- it stays
    # 'illegal' and falls to General Review, not the NCMEC-bound CSAM queue.
    reason = _reason(_event([['e', 'e' * 64, 'illegal'], ['p', 'p' * 64, 'illegal']]))
    assert reason == 'illegal'
    assert reason not in ROUTED


def test_unmatched_reason_falls_through_to_general_review():
    # spam/other have no category rule -> General Review (default). They normalize but
    # are not in the routed set.
    assert _reason(_mobile('spam', 'NS-spam')) == 'spam'
    assert 'spam' not in ROUTED


def test_alias_targets_are_canonical():
    # Every spelling alias must resolve to a token catalogued in CANONICAL_REASONS,
    # so the canonical table is the complete vocabulary the bridge can emit.
    unknown = {v for v in bridge._REASON_ALIASES.values() if v not in bridge.CANONICAL_REASONS}
    assert not unknown, f'aliases resolve to non-canonical tokens: {unknown}'


def test_routed_tokens_are_canonical():
    # The COOP-routed set cannot name a token the bridge never emits.
    unknown = ROUTED - set(bridge.CANONICAL_REASONS)
    assert not unknown, f'ROUTED contains non-canonical tokens: {unknown}'


def test_routed_tokens_have_a_consuming_rule_or_external_owner():
    # The coupling guard. Every routed token must either be matched by a live SML rule
    # or be explicitly owned outside Osprey (e.g. underage_user -> relay-manager age
    # review). A routed token with neither would normalize cleanly, pass the routing
    # tests, then fall into General Review with no error -- the exact gap this guards.
    rule_tokens = _rule_reason_tokens()
    for token in sorted(ROUTED):
        owner = bridge.CANONICAL_REASONS.get(token)
        if owner == 'osprey-rule':
            assert token in rule_tokens, (
                f"routed token '{token}' is owned by Osprey but no rule in {_RULES_DIR} "
                f"matches ReportReason == '{token}'"
            )
        else:
            # Routed outside Osprey: must be explicitly catalogued as such, not silent.
            assert owner in ('relay-manager', 'default-queue'), (
                f"routed token '{token}' has no consuming rule and no external owner in CANONICAL_REASONS"
            )


def test_osprey_rule_tokens_actually_have_a_rule():
    # Inverse drift: if the table claims Osprey acts on a token, a rule must exist.
    rule_tokens = _rule_reason_tokens()
    claimed = {t for t, owner in bridge.CANONICAL_REASONS.items() if owner == 'osprey-rule'}
    missing = claimed - rule_tokens
    assert not missing, f'CANONICAL_REASONS marks these osprey-rule but no rule matches: {missing}'


def test_rule_tokens_are_owned_by_osprey():
    """A new consuming rule must update the canonical ownership registry too."""
    misowned = {
        token: bridge.CANONICAL_REASONS.get(token)
        for token in _rule_reason_tokens()
        if bridge.CANONICAL_REASONS.get(token) != 'osprey-rule'
    }
    assert not misowned, f'SML report rules are not catalogued as osprey-rule: {misowned}'


def test_rules_only_reference_canonical_tokens():
    # A rule must not match a token the bridge can never emit / does not catalogue.
    unknown = _rule_reason_tokens() - set(bridge.CANONICAL_REASONS)
    assert not unknown, f'.sml rules reference non-canonical report reasons: {unknown}'


def test_subscription_filters_bounded_and_kind_scoped():
    # The bridge must never REQ with an unbounded {} filter, and never all-kinds:
    # both make the staging relay sort a set too large for ClickHouse's per-user
    # memory cap (Code: 241 MEMORY_LIMIT_EXCEEDED on relay_event_list), which
    # returns zero, drops the sub on the WS keepalive, and starves Osprey
    # ingestion. Each filter is kind-scoped + time/limit bounded; live events
    # still stream after EOSE.
    fs = bridge.build_subscription_filters(1_000_000)
    assert fs, 'must send at least one filter'
    for f in fs:
        assert f != {}, 'unbounded {} filter OOMs the relay and starves ingestion'
        assert f.get('kinds'), 'every filter must be kind-scoped (all-kinds also OOMs)'
        assert f['limit'] == bridge.SUBSCRIBE_LIMIT
        assert f['since'] == 1_000_000 - bridge.SUBSCRIBE_LOOKBACK_SECONDS
    all_kinds = {k for f in fs for k in f['kinds']}
    # The COOP path depends on reports (1984) and labels (1985) being fed.
    assert 1984 in all_kinds and 1985 in all_kinds


def test_subscription_since_disabled_when_lookback_zero():
    # SUBSCRIBE_LOOKBACK_SECONDS=0 falls back to a limit-only bound; filters must
    # still be kind-scoped and never the unbounded {}.
    orig = bridge.SUBSCRIBE_LOOKBACK_SECONDS
    try:
        bridge.SUBSCRIBE_LOOKBACK_SECONDS = 0
        fs = bridge.build_subscription_filters(1_000_000)
        assert fs
        for f in fs:
            assert 'since' not in f
            assert f['limit'] == bridge.SUBSCRIBE_LIMIT
            assert f.get('kinds')
    finally:
        bridge.SUBSCRIBE_LOOKBACK_SECONDS = orig


def test_cursor_resumes_from_high_water_on_reconnect():
    # On reconnect, resume from the newest created_at already published, not
    # now-lookback, so events still in the window aren't replayed with fresh
    # action IDs.
    now = 1_000_000
    hw = now - 60  # saw an event 60s ago; lookback default is 3600
    for f in bridge.build_subscription_filters(now, high_water=hw):
        assert f['since'] == hw


def test_cursor_floors_at_cold_start_after_long_outage():
    # After an outage longer than the lookback, don't try to backfill the whole
    # gap (that reintroduces the unbounded scan that OOMs the relay); floor the
    # resume point at now - lookback.
    now = 1_000_000
    floor = now - bridge.SUBSCRIBE_LOOKBACK_SECONDS
    hw = floor - 10_000  # last published event well before the floor
    for f in bridge.build_subscription_filters(now, high_water=hw):
        assert f['since'] == floor


def test_cursor_clamped_to_now_not_future():
    # created_at is attacker-controlled; a future-dated event must not poison the
    # cursor and stall ingestion (since past now would match nothing until real
    # time catches up).
    now = 1_000_000
    for f in bridge.build_subscription_filters(now, high_water=now + 999_999):
        assert f['since'] == now


def test_parse_kinds_rejects_set_but_empty_override():
    # A set-but-malformed override (',' / whitespace / garbage) must not silently
    # yield [] -- kinds: [] matches nothing and stops ingestion. Unset is fine
    # (falls back to default); set-but-empty is a hard config error.
    for bad in (',', ' , ', ',,,'):
        try:
            bridge._parse_kinds(bad, [1984, 1985])
            raise AssertionError(f'expected ValueError for override {bad!r}')
        except ValueError:
            pass
    # unset -> default; valid -> parsed
    assert bridge._parse_kinds('', [1984, 1985]) == [1984, 1985]
    assert bridge._parse_kinds('   ', [1984, 1985]) == [1984, 1985]
    assert bridge._parse_kinds('1984, 1985, 34235', []) == [1984, 1985, 34235]


def test_progress_ts_rejects_future_and_non_int():
    now = 1000
    assert bridge._progress_ts(900, now) == 900  # past: ok
    assert bridge._progress_ts(now, now) == now  # exactly now: ok
    assert bridge._progress_ts(now + 10, now) == now + 10  # within skew: ok
    assert bridge._progress_ts(now + bridge.MAX_CLOCK_SKEW_SECONDS + 1, now) is None  # too far future
    assert bridge._progress_ts('123', now) is None  # non-int
    assert bridge._progress_ts(None, now) is None
    assert bridge._progress_ts(True, now) is None  # bool is not a valid timestamp


# --- consume_subscription fakes ---


class _FakeFuture:
    def __init__(self, exc=None):
        self._exc = exc

    def get(self, timeout=None):
        if self._exc:
            raise self._exc
        return True


class _FakeProducer:
    """Records sends; optionally fails the Nth send's delivery."""

    def __init__(self, fail_on_index=None):
        self.sent = []
        self._fail_on = fail_on_index

    def send(self, topic, value=None):
        idx = len(self.sent)
        self.sent.append(value)
        if self._fail_on is not None and idx == self._fail_on:
            return _FakeFuture(exc=RuntimeError('broker rejected'))
        return _FakeFuture()


class _FakeWS:
    """Async-iterable of raw relay frames (JSON strings)."""

    def __init__(self, frames):
        self._frames = list(frames)

    def __aiter__(self):
        self._it = iter(self._frames)
        return self

    async def __anext__(self):
        try:
            frame = next(self._it)
        except StopIteration:
            raise StopAsyncIteration
        if isinstance(frame, BaseException):
            raise frame  # simulate a mid-stream WS drop
        return frame


def _event_frame(created_at, kind=1984, eid='deadbeef'):
    return json.dumps(
        [
            'EVENT',
            'sub',
            {
                'kind': kind,
                'id': eid,
                'pubkey': 'p',
                'created_at': created_at,
                'tags': [],
                'content': '',
            },
        ]
    )


def _eose_frame():
    return json.dumps(['EOSE', 'sub'])


def _consume(frames, producer, cursor_value, now=5000):
    """Run consume_subscription over `frames` and return the resulting cursor
    value. `now` may be an int or a callable. Propagates exceptions (so
    delivery/WS-drop tests can assert on them); use a _Cursor directly to inspect
    committed progress after an exception."""
    c = bridge._Cursor(cursor_value)
    now_fn = now if callable(now) else (lambda: now)
    asyncio.run(bridge.consume_subscription(_FakeWS(frames), producer, c, now=now_fn))
    return c.value


def test_consume_commits_cursor_at_eose():
    # Replayed events advance a tentative mark that is committed once EOSE proves
    # the stored window was fully delivered.
    cursor = _consume([_event_frame(1000), _event_frame(1010), _eose_frame()], _FakeProducer(), None)
    assert cursor == 1010


def test_consume_does_not_commit_before_eose():
    # A drop before EOSE means the replay was incomplete; the cursor must stay at
    # the prior safe point so the next connection re-fetches the whole window.
    cursor = _consume([_event_frame(1000), _event_frame(1010)], _FakeProducer(), 500)
    assert cursor == 500


def test_consume_commits_each_live_event_after_eose():
    cursor = _consume([_eose_frame(), _event_frame(2000), _event_frame(2001)], _FakeProducer(), 500)
    assert cursor == 2001


def test_consume_does_not_advance_past_undelivered_event():
    # A delivery failure must propagate (caller keeps the old cursor and the
    # event is re-fetched), never silently advance the cursor past it.
    try:
        _consume([_eose_frame(), _event_frame(2000), _event_frame(2001)], _FakeProducer(fail_on_index=1), 500)
        raise AssertionError('expected delivery failure to propagate')
    except RuntimeError:
        pass


def test_consume_rejects_future_created_at_for_cursor():
    # A future-dated event is still published but must not advance the cursor
    # (else the next reconnect skips outage-gap events).
    cursor = _consume([_eose_frame(), _event_frame(9_999_999), _event_frame(3000)], _FakeProducer(), 500, now=4000)
    assert cursor == 3000


def test_consume_validates_created_at_at_receipt_not_after_delivery():
    # created_at is future at RECEIPT but the wall clock crosses the skew
    # boundary while the (slow) delivery is in flight. Validation must use the
    # receipt-time clock, so the event is rejected and the cursor is not advanced.
    skew = bridge.MAX_CLOCK_SKEW_SECONDS
    clock = {'t': 1000}
    created = 1000 + skew + 50  # future vs receipt (1000), valid vs the later reading

    class _SlowProducer(_FakeProducer):
        def send(self, topic, value=None):
            fut = super().send(topic, value)
            clock['t'] = 1000 + skew + 100  # time passes during delivery
            return fut

    cursor = _consume([_eose_frame(), _event_frame(created)], _SlowProducer(), 500, now=lambda: clock['t'])
    assert cursor == 500  # validated at receipt -> rejected -> cursor unchanged


def test_consume_preserves_committed_progress_on_delivery_failure():
    # A live event is acked+committed, then a later delivery fails and the
    # coroutine raises. The committed progress must survive (else the report is
    # re-published on reconnect and trips the second-report auto-hide).
    c = bridge._Cursor(500)
    frames = [_eose_frame(), _event_frame(2000), _event_frame(2001)]
    try:
        asyncio.run(bridge.consume_subscription(_FakeWS(frames), _FakeProducer(fail_on_index=1), c, now=lambda: 9000))
        raise AssertionError('expected delivery failure to propagate')
    except RuntimeError:
        pass
    assert c.value == 2000  # 2000 acked+committed before 2001 failed


def test_consume_preserves_committed_progress_on_ws_drop():
    # Same invariant when the WS itself drops mid-stream after a live ack.
    c = bridge._Cursor(500)
    frames = [_eose_frame(), _event_frame(2000), ConnectionError('ws drop')]
    try:
        asyncio.run(bridge.consume_subscription(_FakeWS(frames), _FakeProducer(), c, now=lambda: 9000))
        raise AssertionError('expected WS drop to propagate')
    except ConnectionError:
        pass
    assert c.value == 2000


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'\n{len(fns)} tests passed')
