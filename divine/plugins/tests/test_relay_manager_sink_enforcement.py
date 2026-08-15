"""Runtime guard on the two enforcement boundaries that cannot be undone.

`test_enforcement_targets.py` polices which VALUES the SML rules may pass to
`BanNostrEvent`. It is source-level, and it states this sink's behaviour in its
docstring as a premise: that `pubkey=''` means the sink issues `banevent` and never
`banpubkey`. Nothing executed that premise. Changing the one line that enforces it
(`if effect.pubkey:` -> `if effect.pubkey is not None:`) left every test in the repo
green while every `pubkey=''` rule began issuing the irreversible purge.

That is the whole safety argument for letting CSAM act on a single report from any
reporter, so it needs a test that runs the code.

The sibling files say the sink "cannot be imported here" because the plugin CI step
installs only pytest and websocket-client. That premise is false: stdlib
`types.ModuleType` + `sys.modules` makes the real sink import and run with no new CI
dependency. Same harness as test_coop_sink_payload_wire.py.

Limit worth stating: stubbing proves the code path, not that the deployed image
imports cleanly. It replaces neither an integration test nor a working image build.
"""

import importlib
import logging
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

EVENT_ID = '3f8a1c9e77b24d6e05a4c3b81f9e2d70a6c5b4938e7f1a2d0c9b8a7f6e5d4c3b'
AUTHOR = 'bc02e0a6c0f01ad9cb57a2b0f8ef8241bc5ff979ce4452ce9e243de457756725'


@dataclass
class _BanEventEffect:
    """Mirrors udfs.ban_nostr_event.BanEventEffect's fields, which is all the sink reads."""

    event_id: str
    pubkey: str
    reason: str


@dataclass
class _AgeRestrictEffect:
    event_id: str = ''
    sha256: str = ''


class _AnyMeta(type):
    """Absorbs whatever the engine's bases are used for: subscripting (`Base[List[str]]`),
    enum-ish attribute access (`UdfCategories.ENGINE`), and plain subclassing."""

    def __getattr__(cls, name):
        return cls

    def __getitem__(cls, item):
        return cls


class _Subscriptable(metaclass=_AnyMeta):
    pass


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


@pytest.fixture
def sink_module(monkeypatch):
    """Import the real relay_manager_sink, capturing every POST it makes."""
    calls: list[dict] = []

    def _post(url, json=None, headers=None, timeout=None):
        calls.append({'url': url, 'payload': json})

        class _Response:
            status_code = 200
            headers = {'Content-Type': 'application/json'}
            text = '{"success": true, "result": true}'

            def raise_for_status(self):
                return None

        return _Response()

    _stub('requests', post=_post)
    _stub('sentry_sdk', capture_exception=lambda *a, **k: None, capture_message=lambda *a, **k: None)
    for pkg in (
        'osprey',
        'osprey.engine',
        'osprey.engine.executor',
        'osprey.engine.language_types',
        'osprey.engine.stdlib',
        'osprey.engine.stdlib.udfs',
        'osprey.engine.udf',
        'osprey.engine.utils',
        'osprey.worker',
        'osprey.worker.lib',
        'osprey.worker.lib.osprey_shared',
        'osprey.worker.sinks',
        'osprey.worker.sinks.sink',
    ):
        _stub(pkg)
    _stub('osprey.engine.executor.execution_context', ExecutionResult=_Subscriptable, ExecutionContext=_Subscriptable)
    _stub('osprey.engine.executor.custom_extracted_features', CustomExtractedFeature=_Subscriptable)
    # BanEventEffect is a @dataclass subclassing this; a bare object base is enough for
    # the dataclass machinery, and the sink only reads its fields.
    _stub(
        'osprey.engine.language_types.effects',
        EffectBase=_Subscriptable,
        EffectToCustomExtractedFeatureBase=_Subscriptable,
    )
    _stub('osprey.engine.stdlib.udfs.categories', UdfCategories=_Subscriptable)
    _stub('osprey.engine.udf.arguments', ArgumentsBase=_Subscriptable)
    _stub('osprey.engine.udf.base', UDFBase=_Subscriptable)
    # add_slots rewrites a class to use __slots__; identity keeps the dataclass usable.
    _stub('osprey.engine.utils.types', add_slots=lambda cls: cls)
    _stub('osprey.worker.lib.osprey_shared.logging', get_logger=logging.getLogger)
    _stub('osprey.worker.sinks.sink.output_sink', BaseOutputSink=_Subscriptable)
    # Stub the EFFECT modules rather than importing the real ones. They drag in the whole
    # UDF-registration machinery, which this file is not testing; the sink only needs the
    # class identity (to key result.effects) and the three fields. Using the same class
    # object here and in _drive is what makes the lookup work.
    _stub('udfs')
    _stub('udfs.ban_nostr_event', BanEventEffect=_BanEventEffect)
    _stub('udfs.age_restrict_nostr_event', AgeRestrictEffect=_AgeRestrictEffect)

    monkeypatch.setenv('DIVINE_RELAY_MANAGER_URL', 'https://relay-manager.test.invalid')
    monkeypatch.setenv('DIVINE_RELAY_MANAGER_API_KEY', 'test-key')
    # No signing key: _publish_label_event is a separate concern and is network I/O.
    monkeypatch.delenv('DIVINE_MODERATION_NSEC', raising=False)

    module = importlib.import_module('services.relay_manager_sink')
    importlib.reload(module)
    return module, calls


def _drive(sink_module, event_id, pubkey, reason='test'):
    module, calls = sink_module
    effect = _BanEventEffect(event_id=event_id, pubkey=pubkey, reason=reason)

    class _Result:
        effects = {_BanEventEffect: [effect]}

    sink = module.RelayManagerSink()
    sink.push(_Result())
    return calls


def _methods(calls):
    return [c['payload'].get('method') for c in calls if isinstance(c.get('payload'), dict)]


class TestIrreversibleBoundary:
    def test_empty_pubkey_bans_the_event_and_never_the_account(self, sink_module):
        """The invariant every report-driven rule relies on, finally executed.

        banpubkey PURGES all of an account's content irreversibly. A reporter-supplied
        pubkey is an unverified claim, so report rules pass pubkey='' and the account
        decision goes to a human. If this ever issues banpubkey, a single report can
        erase someone.
        """
        calls = _drive(sink_module, EVENT_ID, '')
        methods = _methods(calls)
        assert 'banevent' in methods, f'the event ban must still happen; saw {methods}'
        assert 'banpubkey' not in methods, f'IRREVERSIBLE: an empty pubkey reached banpubkey; saw {methods}'

    def test_a_real_pubkey_still_bans_the_account(self, sink_module):
        """A positive control. Without it, a sink that never bans anyone passes above."""
        calls = _drive(sink_module, EVENT_ID, AUTHOR)
        assert 'banpubkey' in _methods(calls), 'the account ban path must still work'

    def test_an_uppercase_pubkey_is_sent_and_signed_canonicalised(self, sink_module):
        calls = _drive(sink_module, EVENT_ID, AUTHOR.upper())
        banpubkey = [c for c in calls if c['payload'].get('method') == 'banpubkey']
        labels = [c for c in calls if c['url'].endswith('/api/publish')]
        assert banpubkey[0]['payload']['params'][0] == AUTHOR
        assert ['p', AUTHOR] in labels[0]['payload']['tags']


class TestMalformedInputIsRefused:
    """A report's `e` tag is attacker-chosen, and CSAM now acts on one report from any
    reporter. Unvalidated, that string reaches the relay AND gets signed into a kind-1985
    under Divine's key. Refusing is the safe failure.
    """

    @pytest.mark.parametrize(
        'bad_id',
        ['not-hex', 'a' * 63, 'a' * 65, 'zz' + 'a' * 62, '../../etc/passwd', 'id\nwith-newline'],
    )
    def test_a_malformed_event_id_enforces_nothing(self, sink_module, bad_id):
        calls = _drive(sink_module, bad_id, '')
        assert _methods(calls) == [], f'{bad_id!r} must not reach the relay; saw {_methods(calls)}'

    def test_an_uppercase_event_id_is_sent_canonicalised(self, sink_module):
        """Valid but non-canonical. /api/relay-rpc does not lowercase, so sending it raw
        would ban a different key than everything else refers to.
        """
        calls = _drive(sink_module, EVENT_ID.upper(), '')
        banevent = [c for c in calls if c['payload'].get('method') == 'banevent']
        assert banevent, 'an uppercase id is well-formed and must still be enforced'
        assert banevent[0]['payload']['params'][0] == EVENT_ID

    def test_a_malformed_pubkey_keeps_the_event_ban_and_drops_the_account_ban(self, sink_module):
        """Partial enforcement, loudly: the event ban is legitimate and reversible, so
        dropping it too would be worse than refusing only the irreversible half.
        """
        calls = _drive(sink_module, EVENT_ID, 'not-a-pubkey')
        methods = _methods(calls)
        assert 'banevent' in methods
        assert 'banpubkey' not in methods, f'IRREVERSIBLE on malformed input; saw {methods}'
        labels = [c for c in calls if c['url'].endswith('/api/publish')]
        assert not any(tag[0] == 'p' for tag in labels[0]['payload']['tags'])
