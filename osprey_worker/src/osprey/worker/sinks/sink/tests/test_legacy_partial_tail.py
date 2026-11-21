import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from osprey.worker.sinks.legacy_partial_tail import (
    LegacyTailPreparationError,
    RecordedEvaluation,
    prepare_legacy_partial_tail,
)

TIME = datetime(2026, 1, 1, 0, 0, 0, 123000, tzinfo=timezone.utc)
COLUMNS = frozenset({'__time', '__action_id', 'Name', 'Number', 'Flag', 'List', 'Object', '__rule_hits', '__verdicts'})


def evaluation(offset: int, features: object, **kwargs: object) -> RecordedEvaluation:
    return replace(
        RecordedEvaluation('synthetic-input', 0, offset, offset, TIME, json.dumps(features)),
        **kwargs,
    )


@pytest.mark.parametrize('count', [0, 1, 28, 99])
def test_exact_partial_batch_retains_order_and_multiplicity(count: int) -> None:
    inputs = [evaluation(n, {'Name': 'synthetic'}, action_id=7) for n in range(count)]
    batch = prepare_legacy_partial_tail(inputs, allowed_columns=COLUMNS)
    if count == 0:
        assert batch is None
        return
    assert batch is not None
    assert batch.columns == ('__time', '__action_id', 'Name')
    assert batch.rows == ((TIME.isoformat(), 7, 'synthetic'),) * count
    assert batch.sources == tuple(('synthetic-input', 0, n) for n in range(count))


def test_legacy_union_defaults_use_first_present_sample_not_schema_defaults() -> None:
    inputs = [evaluation(3, {'Name': 'first', 'Number': 2}), evaluation(5, {'Flag': False})]
    batch = prepare_legacy_partial_tail(inputs, allowed_columns=COLUMNS)
    assert batch is not None
    assert batch.columns == ('__time', '__action_id', 'Name', 'Number', 'Flag', '__rule_hits')
    assert batch.rows == (
        (TIME.isoformat(), 3, 'first', 2, 0, ''),
        (TIME.isoformat(), 5, '', 0, 0, '{"Flag": false}'),
    )
    # A column absent from the whole partial tail must stay omitted. Its server
    # default may be NULL, '{}', or '[]', rather than the union member's ''.
    assert not {'List', 'Object', '__verdicts'} & set(batch.columns)


def test_legacy_transforms_internal_features_collections_booleans_and_none() -> None:
    batch = prepare_legacy_partial_tail(
        [
            evaluation(
                1,
                {
                    'Flag': True,
                    'List': [1, 'x'],
                    'Object': {'x': 2},
                    '__verdicts': ['x'],
                    '__private_ignored': 'ignored',
                    '__rule_hits': 'ignored',
                    'Name': None,
                },
            )
        ],
        allowed_columns=COLUMNS,
    )
    assert batch is not None
    assert dict(zip(batch.columns, batch.rows[0])) == {
        '__time': TIME.isoformat(),
        '__action_id': 1,
        'Flag': 1,
        'List': '[1, "x"]',
        'Object': '{"x": 2}',
        '__verdicts': '["x"]',
        '__rule_hits': '{"Flag": true}',
    }


def test_do_not_combine_workers_or_split_tail_into_individual_batches() -> None:
    first = evaluation(0, {'Number': 1})
    second = evaluation(1, {'Name': 'second'})
    together = prepare_legacy_partial_tail([first, second], allowed_columns=COLUMNS)
    alone = prepare_legacy_partial_tail([second], allowed_columns=COLUMNS)
    assert together is not None and alone is not None
    assert together.rows[1][together.columns.index('Number')] == 0
    assert 'Number' not in alone.columns
    with pytest.raises(LegacyTailPreparationError, match='one source partition'):
        prepare_legacy_partial_tail([first, replace(second, partition=1)], allowed_columns=COLUMNS)


@pytest.mark.parametrize('offsets', [(1, 1), (2, 1), (-1, 0)])
def test_duplicate_reversed_or_negative_source_positions_refuse(offsets: tuple[int, int]) -> None:
    with pytest.raises(LegacyTailPreparationError, match='source offsets must increase'):
        prepare_legacy_partial_tail([evaluation(n, {}) for n in offsets], allowed_columns=COLUMNS)


def test_100th_row_cannot_be_claimed_as_unsubmitted_partial_tail() -> None:
    with pytest.raises(LegacyTailPreparationError, match='fewer than 100'):
        prepare_legacy_partial_tail([evaluation(n, {}) for n in range(100)], allowed_columns=COLUMNS)


@pytest.mark.parametrize('features', [[], None, 'not an object', 42])
def test_non_object_evaluations_refuse(features: object) -> None:
    with pytest.raises(LegacyTailPreparationError, match='evaluation object'):
        prepare_legacy_partial_tail([evaluation(0, features)], allowed_columns=COLUMNS)


def test_invalid_json_and_unknown_columns_refuse_without_exposing_content() -> None:
    for item in [evaluation(0, {}, extracted_features_json='private-content'), evaluation(0, {'PrivateUnknown': 1})]:
        with pytest.raises(LegacyTailPreparationError) as caught:
            prepare_legacy_partial_tail([item], allowed_columns=COLUMNS)
        assert 'private-content' not in str(caught.value)
        assert 'PrivateUnknown' not in str(caught.value)


def test_timestamp_and_column_shape_must_be_explicit() -> None:
    with pytest.raises(LegacyTailPreparationError, match='timezone'):
        prepare_legacy_partial_tail([evaluation(0, {}, timestamp=TIME.replace(tzinfo=None))], allowed_columns=COLUMNS)
    with pytest.raises(LegacyTailPreparationError, match='base columns'):
        prepare_legacy_partial_tail([evaluation(0, {})], allowed_columns=frozenset({'Name'}))


def test_no_timestamp_filter_or_month_split() -> None:
    older = TIME.replace(year=2020)
    batch = prepare_legacy_partial_tail(
        [evaluation(0, {}, timestamp=older), evaluation(1, {}, timestamp=TIME.replace(month=2))],
        allowed_columns=COLUMNS,
    )
    assert batch is not None
    assert [row[0] for row in batch.rows] == [older.isoformat(), TIME.replace(month=2).isoformat()]


def test_capacity_failure_never_returns_a_truncated_batch() -> None:
    with pytest.raises(LegacyTailPreparationError, match='capacity'):
        prepare_legacy_partial_tail([evaluation(0, {'Name': 'x' * 1024})], allowed_columns=COLUMNS, max_bytes=128)


def test_input_list_changes_cannot_mutate_prepared_payload() -> None:
    inputs = [evaluation(0, {'Name': 'private-synthetic-value'})]
    batch = prepare_legacy_partial_tail(inputs, allowed_columns=COLUMNS)
    assert batch is not None
    inputs.clear()
    assert len(batch.rows) == 1
    assert 'private-synthetic-value' not in repr(batch)
    assert 'private-synthetic-value' not in repr(evaluation(0, {'Name': 'private-synthetic-value'}))


@pytest.mark.parametrize('limit', [0, -1, 16 * 1024 * 1024 + 1])
def test_cannot_disable_preparation_capacity(limit: int) -> None:
    with pytest.raises(LegacyTailPreparationError, match='capacity'):
        prepare_legacy_partial_tail([], allowed_columns=COLUMNS, max_bytes=limit)


@pytest.mark.parametrize(
    'field,value,message',
    [
        ('partition', True, 'invalid source position'),
        ('offset', True, 'source offsets must increase'),
        ('action_id', True, 'invalid action identifier'),
        ('topic', '', 'invalid source position'),
        ('topic', 1, 'invalid source position'),
        ('topic', b'synthetic', 'invalid source position'),
        ('topic', ['synthetic'], 'invalid source position'),
    ],
)
def test_invalid_source_and_identity_types_refuse(field: str, value: object, message: str) -> None:
    with pytest.raises(LegacyTailPreparationError, match=message):
        prepare_legacy_partial_tail([replace(evaluation(0, {}), **{field: value})], allowed_columns=COLUMNS)


def test_all_effect_fields_retain_legacy_passthrough_without_rehydrating_effects() -> None:
    features = {
        '__verdicts': {'synthetic': True},
        '__entity_label_mutations': ['Synthetic:Label:ADDED'],
        '__ban_nostr_event': True,
        '__age_restrict_nostr_event': False,
    }
    batch = prepare_legacy_partial_tail([evaluation(0, features)], allowed_columns=COLUMNS | frozenset(features))
    assert batch is not None
    values = dict(zip(batch.columns, batch.rows[0]))
    assert values['__entity_label_mutations'] == '["Synthetic:Label:ADDED"]'
    assert values['__verdicts'] == '{"synthetic": true}'
    assert values['__ban_nostr_event'] is True
    assert values['__age_restrict_nostr_event'] is False
