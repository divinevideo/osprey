"""Unit tests for the report-reason normalization in the Nostr->Kafka bridge.

This is the single point where every kind-1984 report's reason is mapped to the
canonical token COOP routes on. A wrong alias here silently routes a CSAM or
child-safety report to General Review, so the mapping is covered explicitly.

Pure functions only -- kafka/websockets are stubbed so this runs with no external
deps (the bridge ships its own requirements.txt separate from the osprey worker).
Run: `python divine/nostr-kafka-bridge/test_main.py` or `pytest` against this file.
"""

import importlib.util
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


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'\n{len(fns)} tests passed')
