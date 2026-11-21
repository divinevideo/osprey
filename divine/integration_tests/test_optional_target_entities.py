"""Compile the real rules: unrelated event targets are expected omissions."""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import pytest
from osprey.engine.ast.sources import Sources
from osprey.engine.ast_validator import validate_sources
from osprey.engine.ast_validator.validator_registry import ValidatorRegistry
from osprey.engine.executor.execution_context import Action
from osprey.engine.executor.execution_graph import compile_execution_graph
from osprey.engine.executor.executor import execute
from osprey.engine.executor.udf_execution_helpers import UDFHelpers
from osprey.engine.stdlib.udfs.labels import HasLabel
from osprey.engine.udf.registry import UDFRegistry
from osprey.worker._stdlibplugin.udf_register import register_udfs
from osprey.worker._stdlibplugin.validator_regsiter import register_ast_validators
from osprey.worker.lib.osprey_shared.labels import EntityLabels
from osprey.worker.lib.singletons import CONFIG
from osprey.worker.lib.sources_config import get_config_registry
from osprey.worker.lib.storage.labels import LabelsProvider, LabelsServiceBase
from result import Ok

RULES = Path(__file__).resolve().parents[1] / 'rules'
TARGETS = {
    'models/nostr/kind1984_report.sml': ('ReportedEventId', 'ReportedPubkey'),
    'models/nostr/kind1985_label.sml': ('LabelTargetEventEntity',),
}


def legacy_sources(sources):
    """Restore only the three historical required-entity declarations."""
    legacy = dict(sources)
    for path, features in TARGETS.items():
        for feature in features:
            pattern = rf'{feature}: Entity\[str\] = EntityJson\([^)]*\)'
            block = re.search(pattern, legacy[path])
            assert block is not None and block[0].count('required=False') == 1
            legacy[path] = legacy[path].replace(block[0], block[0].replace('required=False', 'required=True'))
    return legacy


@pytest.fixture
def run_rules(monkeypatch):
    # Read-only fake label lookup; no output sink, broker, database or HTTP is used.
    from types import SimpleNamespace

    from udfs.age_restrict_nostr_event import AgeRestrictNostrEvent
    from udfs.ban_nostr_event import BanNostrEvent
    from udfs.check_moderation_result import CheckModerationResult
    from udfs.is_trusted_moderation_signer import IsTrustedModerationSigner
    from udfs.is_valid_media_hash import IsValidMediaHash
    from udfs.normalize_media_hash import NormalizeMediaHash
    from udfs.nostr_account_age import NostrAccountAge
    from udfs.resolve_event_author import ResolveEventAuthor

    def no_http(*args, **kwargs):
        raise AssertionError('Synthetic rules must not contact external services')

    monkeypatch.delenv('DIVINE_RELAY_API_URL', raising=False)
    monkeypatch.delenv('DIVINE_MODERATION_API_URL', raising=False)
    monkeypatch.setenv('DIVINE_TRUSTED_MODERATION_PUBKEYS', 'b' * 64)
    monkeypatch.setattr('requests.sessions.Session.request', no_http)
    CONFIG.instance().configure({})
    registry = UDFRegistry.with_udfs(
        *register_udfs(),
        AgeRestrictNostrEvent,
        BanNostrEvent,
        CheckModerationResult,
        IsTrustedModerationSigner,
        IsValidMediaHash,
        NormalizeMediaHash,
        NostrAccountAge,
        ResolveEventAuthor,
    )
    validators = ValidatorRegistry.from_validator_classes(
        set(register_ast_validators()) | {get_config_registry().get_validator()}
    )
    helpers = UDFHelpers()
    provider = LabelsProvider(
        cast(
            LabelsServiceBase,
            SimpleNamespace(
                read_labels=lambda entity: EntityLabels(),
                batch_read_labels=lambda entities: [Ok(EntityLabels()) for _ in entities],
            ),
        )
    )
    helpers.set_udf_helper(HasLabel, provider)
    sources = Sources.from_path(RULES).to_dict()
    graphs = {}

    def run(data, *, legacy=False, async_pool=None):
        if legacy not in graphs:
            graph_sources = legacy_sources(sources) if legacy else sources
            graphs[legacy] = compile_execution_graph(
                validate_sources(Sources.from_dict(graph_sources), udf_registry=registry, validator_registry=validators)
            )
        return execute(
            graphs[legacy],
            helpers,
            Action(
                action_id=1, action_name='synthetic', data=data, timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc)
            ),
            sample_rate=100,
            async_pool=async_pool,
        )

    try:
        yield run
    finally:
        CONFIG.instance().unconfigure_for_tests()


@pytest.mark.parametrize('kind', [1, 34236])
@pytest.mark.parametrize('asynchronous', [False, True])
def test_unrelated_events_do_not_fail_for_missing_report_and_label_targets(run_rules, kind, asynchronous):
    from gevent.pool import Pool

    result = run_rules(
        {'event_id': 'a' * 64, 'pubkey': 'b' * 64, 'kind': kind, 'created_at': 1767225600, 'content': '', 'tags': []},
        async_pool=Pool(12) if asynchronous else None,
    )
    assert [(type(error.error).__name__, str(error.error)) for error in result.error_infos] == []
    assert result.extracted_features['__error_count'] == 0
    for feature in ('ReportedEventId', 'ReportedPubkey', 'LabelTargetEventEntity'):
        assert result.extracted_features[feature] is None
    assert not result.effects


@pytest.mark.parametrize(
    'extra',
    [
        {},
        {'kind': 34236},
        {'kind': 1984, 'reported_event_id': 'c' * 64, 'reported_pubkey': '', 'report_reason': 'spam'},
        {'kind': 1984, 'reported_event_id': '', 'reported_pubkey': 'c' * 64, 'report_reason': 'spam'},
        {
            'kind': 1985,
            'label_namespace': 'content-warning',
            'label_value': 'nudity',
            'label_target_event': 'c' * 64,
            'label_content_hash': 'd' * 64,
        },
        {'kind': 1985, 'label_namespace': 'content-warning', 'label_value': 'nudity', 'label_content_hash': 'd' * 64},
        {'reported_event_id': None, 'reported_pubkey': None, 'label_target_event': None},
        {'reported_event_id': '', 'reported_pubkey': '', 'label_target_event': ''},
    ],
)
def test_target_omissions_preserve_existing_rule_values_and_moderation_effects(run_rules, extra):
    data = {
        'event_id': 'a' * 64,
        'pubkey': 'b' * 64,
        'kind': 1,
        'created_at': 1767225600,
        'content': '',
        'tags': [],
        **extra,
    }
    current = run_rules(data)
    historical = run_rules(data, legacy=True)
    assert current.extracted_features['__error_count'] == 0
    assert current.effects == historical.effects
    assert {k: v for k, v in current.extracted_features.items() if k != '__error_count'} == {
        k: v for k, v in historical.extracted_features.items() if k != '__error_count'
    }


@pytest.mark.parametrize('kind', [None, 'not-an-integer'])
def test_missing_or_malformed_required_core_data_still_fails(run_rules, kind):
    data = {'event_id': 'a' * 64, 'pubkey': 'b' * 64, 'created_at': 1767225600, 'content': '', 'tags': []}
    if kind is not None:
        data['kind'] = kind
    result = run_rules(data)
    assert result.extracted_features['__error_count'] > 0
    assert any(type(e.error).__name__ in {'MissingJsonPath', 'InvalidJsonType'} for e in result.error_infos)


@pytest.mark.parametrize('target', [None, '', 'c' * 64])
def test_confirmed_label_enforcement_is_preserved_for_each_target_shape(run_rules, target):
    data = {
        'event_id': 'a' * 64,
        'pubkey': 'b' * 64,
        'kind': 1985,
        'created_at': 1767225600,
        'content': '',
        'tags': [],
        'label_namespace': 'content-warning',
        'label_value': 'nudity',
        'label_content_hash': 'd' * 64,
        'label_source': 'human-moderator',
        'label_rejected': False,
        'label_target_event': target,
    }
    current = run_rules(data)
    historical = run_rules(data, legacy=True)
    assert current.extracted_features['__error_count'] == 0
    # Hash-only labels deliberately stay in review; this fix must not turn
    # that existing posture into automatic enforcement.
    assert [v.verdict for v in current.verdicts] == (['restrict'] if target else ['flag_for_review'])
    assert current.effects == historical.effects
    assert {k: v for k, v in current.extracted_features.items() if k != '__error_count'} == {
        k: v for k, v in historical.extracted_features.items() if k != '__error_count'
    }
