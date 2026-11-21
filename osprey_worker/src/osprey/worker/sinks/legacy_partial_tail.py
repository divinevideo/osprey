"""Offline legacy stop-flush preparation, without intake, classification or I/O.

This does not establish that a supplied tail was never submitted or is complete.
The caller must independently prove the worker's admission/flush boundaries and
source coverage. A prepared payload is not a drain or recovery certificate.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

LEGACY_BATCH_SIZE = 100
MAX_PREPARATION_BYTES = 16 * 1024 * 1024
# TODO(divinevideo/divine-iac-coreconfig#2615): retire after the verified legacy handoff.
_PASSTHROUGH = frozenset({'__verdicts', '__entity_label_mutations', '__ban_nostr_event', '__age_restrict_nostr_event'})


class LegacyTailPreparationError(ValueError):
    """Refuse preparation without including private source values in errors."""


def _dumps_feature(value: Any) -> str:
    try:
        return json.dumps(value)
    except RecursionError:
        raise LegacyTailPreparationError('invalid evaluation object') from None


@dataclass(frozen=True, repr=False)
class RecordedEvaluation:
    topic: str
    partition: int
    offset: int
    action_id: int
    timestamp: datetime
    extracted_features_json: str


@dataclass(frozen=True)
class PreparedLegacyTail:
    """serialized_size is the UTF-8 byte length of the JSON-encoded (columns, rows).

    It bounds preparation capacity in the same unit as the input check; it is not
    the size of any transport's insert payload.
    """

    columns: tuple[str, ...] = field(repr=False)
    rows: tuple[tuple[Any, ...], ...] = field(repr=False)
    sources: tuple[tuple[str, int, int], ...] = field(repr=False)
    serialized_size: int


def prepare_legacy_partial_tail(
    evaluations: Sequence[RecordedEvaluation],
    *,
    allowed_columns: frozenset[str],
    max_bytes: int = MAX_PREPARATION_BYTES,
) -> PreparedLegacyTail | None:
    """Seal exactly one worker's ordered, already evaluated partial buffer.

    Match 51a742d089aa39d1e10fbde1e46a7d26b685e563's push/stop transform:
    first-present samples choose missing union-member defaults (0 or ''),
    while entirely absent columns stay omitted for server defaults. Never
    combine workers, split by month, filter TTL-expired rows, or fill all schema
    columns. Offset gaps may represent separately proven skips; this function
    neither supplies that proof nor joins/commits a consumer group.

    The supplied column set must come from independently checked schema. Unknown
    columns and malformed evaluations fail the entire preparation, without
    dropping rows or columns. There is no sender, retry, or execution entrypoint.
    """
    if not 0 < max_bytes <= MAX_PREPARATION_BYTES:
        raise LegacyTailPreparationError('invalid preparation capacity')
    if not {'__time', '__action_id'} <= allowed_columns:
        raise LegacyTailPreparationError('schema must include base columns')
    if len(evaluations) >= LEGACY_BATCH_SIZE:
        raise LegacyTailPreparationError('partial tail requires fewer than 100 rows')
    if not evaluations:
        return None
    if not all(isinstance(item, RecordedEvaluation) and isinstance(item.timestamp, datetime) for item in evaluations):
        raise LegacyTailPreparationError('invalid evaluation object')

    source = (evaluations[0].topic, evaluations[0].partition)
    last_offset = -1
    input_bytes = 0
    buffered: list[dict[str, Any]] = []
    for item in evaluations:
        if type(item.topic) is not str or not item.topic or type(item.partition) is not int or item.partition < 0:
            raise LegacyTailPreparationError('invalid source position')
        if (item.topic, item.partition) != source:
            raise LegacyTailPreparationError('tail requires one source partition')
        if type(item.offset) is not int or item.offset <= last_offset:
            raise LegacyTailPreparationError('source offsets must increase')
        if type(item.action_id) is not int:
            raise LegacyTailPreparationError('invalid action identifier')
        if item.timestamp.tzinfo is None or item.timestamp.utcoffset() is None:
            raise LegacyTailPreparationError('timestamp requires explicit timezone')
        last_offset = item.offset
        try:
            input_bytes += len(item.extracted_features_json.encode())
        except (AttributeError, TypeError, UnicodeError):
            raise LegacyTailPreparationError('invalid evaluation object') from None
        if input_bytes > max_bytes:
            raise LegacyTailPreparationError('preparation capacity exceeded')
        try:
            features = json.loads(item.extracted_features_json)
        except (RecursionError, TypeError, UnicodeError, ValueError):
            raise LegacyTailPreparationError('invalid evaluation object') from None
        if not isinstance(features, dict):
            raise LegacyTailPreparationError('invalid evaluation object')

        row: dict[str, Any] = {'__time': item.timestamp.isoformat(), '__action_id': item.action_id}
        rule_hits: dict[str, bool] = {}
        for key, value in features.items():
            if key.startswith('__'):
                if key in _PASSTHROUGH and value is not None:
                    row[key] = _dumps_feature(value) if isinstance(value, (list, dict)) else value
                continue
            if value is None:
                continue
            if isinstance(value, (list, dict)):
                row[key] = _dumps_feature(value)
            elif isinstance(value, bool):
                row[key] = int(value)
                rule_hits[key] = value
            else:
                row[key] = value
        if rule_hits:
            row['__rule_hits'] = json.dumps(rule_hits)
        if not row.keys() <= allowed_columns:
            raise LegacyTailPreparationError('evaluation has unknown schema columns')
        buffered.append(row)

    # Preserve the original union's insertion order and first-present type.
    samples: dict[str, Any] = {}
    for row in buffered:
        for column, value in row.items():
            if column not in samples or samples[column] is None:
                samples[column] = value
    columns = tuple(samples)
    defaults = {column: 0 if isinstance(value, (int, float)) else '' for column, value in samples.items()}
    rows = tuple(tuple(row.get(column, defaults[column]) for column in columns) for row in buffered)
    size = len(json.dumps((columns, rows), ensure_ascii=False).encode())
    if size > max_bytes:
        raise LegacyTailPreparationError('preparation capacity exceeded')
    return PreparedLegacyTail(
        columns=columns,
        rows=rows,
        sources=tuple((item.topic, item.partition, item.offset) for item in evaluations),
        serialized_size=size,
    )
