"""Unit tests for the report-reason normalization in the Nostr->Kafka bridge.

This is the single point where every kind-1984 report's reason is mapped to the
canonical token COOP routes on. A wrong alias here silently routes a CSAM or
child-safety report to General Review, so the mapping is covered explicitly.

Pure functions only -- kafka/websockets are stubbed so this runs with no external
deps (the bridge ships its own requirements.txt separate from the osprey worker).
Run: `python divine/nostr-kafka-bridge/test_main.py` or `pytest` against this file.
"""

import importlib.util
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

# Canonical tokens COOP has a category-routing rule for. Everything else falls to
# General Review by the default rule. Keep in sync with coop/divine/coop-setup-org.sh.
ROUTED = {'csam', 'child_safety', 'underage_user', 'nudity', 'violence', 'harassment'}


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


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'\n{len(fns)} tests passed')
