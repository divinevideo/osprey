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
    captured: dict = {}

    def _post(url, json=None, headers=None, timeout=None):
        captured['url'] = url
        captured['payload'] = json
        captured['headers'] = headers

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
    payload = captured['payload']
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
    payload = captured['payload']
    assert payload['userId'] == AUTHOR
    assert payload['content']['pubkey'] == AUTHOR
    assert payload['content']['author'] == {'id': AUTHOR, 'typeId': 'nostr-user-type'}


def test_the_wire_payload_has_the_expected_envelope(sink_module):
    captured = _drive(sink_module, {'Kind': 1984, 'EventId': WRAPPER_ID})
    payload = captured['payload']
    assert payload['contentId'] == CONTENT_ID
    assert payload['sync'] is False
    assert set(payload) == {'contentId', 'contentType', 'userId', 'content', 'sync'}
    assert captured['url'] == 'https://coop.test.invalid/api/v1/content'


def test_userId_and_content_pubkey_describe_the_same_event(sink_module):
    """They are derived separately and must not drift; a mismatch means Coop's
    creator and the item's stated author disagree."""
    captured = _drive(
        sink_module,
        {'Kind': 1984, 'ReportedPubkey': REPORTER, 'EventId': WRAPPER_ID},
    )
    payload = captured['payload']
    assert payload['userId'] == payload['content']['pubkey']


def test_the_payload_is_json_serialisable(sink_module):
    """It is handed to `requests.post(json=...)`, so a non-serialisable feature
    value would raise at submit time rather than in any unit test."""
    captured = _drive(sink_module, {'Kind': 1984, 'CreatedAt': 1, 'EventId': WRAPPER_ID})
    json.dumps(captured['payload'])
