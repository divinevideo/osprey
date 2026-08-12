"""ClickHouse output sink — replaces KafkaOutputSink for Divine's stack.

Writes rule execution results directly to ClickHouse instead of
routing through Kafka → Druid. The table schema mirrors what Druid
would ingest so the query UI works unchanged.
"""

import json
from typing import Any

import sentry_sdk
from osprey.engine.executor.execution_context import ExecutionResult
from osprey.worker.lib.osprey_shared.logging import get_logger
from osprey.worker.sinks.sink.output_sink import BaseOutputSink

logger = get_logger()

# Default batch size before flushing to ClickHouse
DEFAULT_BATCH_SIZE = 500
DEFAULT_FLUSH_INTERVAL_SECONDS = 5


class ClickHouseOutputSink(BaseOutputSink):
    """An output sink that writes extracted features to a ClickHouse table.

    Uses clickhouse-connect for efficient batch inserts with configurable
    flush interval and batch size.
    """

    timeout: float = 10.0
    max_retries: int = 2

    def __init__(
        self,
        clickhouse_client: Any,  # clickhouse_connect.driver.Client
        table: str = 'osprey_events',
        database: str = 'osprey',
        batch_size: int = DEFAULT_BATCH_SIZE,
    ):
        self._client = clickhouse_client
        self._table = table
        self._database = database
        self._batch_size = batch_size
        self._buffer: list[dict[str, Any]] = []
        logger.info(f'ClickHouseOutputSink initialized: {database}.{table} (batch_size={batch_size})')

    def will_do_work(self, result: ExecutionResult) -> bool:
        return True

    _PASSTHROUGH_INTERNAL_KEYS = frozenset(
        {
            '__verdicts',
            '__entity_label_mutations',
            '__ban_nostr_event',
            '__age_restrict_nostr_event',
        }
    )

    def push(self, result: ExecutionResult) -> None:
        try:
            features = json.loads(result.extracted_features_json)

            row: dict[str, Any] = {
                '__time': result.action.timestamp.isoformat(),
                '__action_id': result.action.action_id,
            }

            rule_hits: dict[str, bool] = {}
            for key, val in features.items():
                if key.startswith('__'):
                    if key in self._PASSTHROUGH_INTERNAL_KEYS and val is not None:
                        row[key] = json.dumps(val) if isinstance(val, (list, dict)) else val
                    continue
                if val is None:
                    continue
                if isinstance(val, (list, dict)):
                    row[key] = json.dumps(val)
                elif isinstance(val, bool):
                    row[key] = int(val)
                    rule_hits[key] = val
                else:
                    row[key] = val

            if rule_hits:
                row['__rule_hits'] = json.dumps(rule_hits)

            self._buffer.append(row)

            if len(self._buffer) >= self._batch_size:
                self._flush()

        except Exception as e:
            logger.error(f'ClickHouse sink error: {e}')
            sentry_sdk.capture_exception(error=e)

    def _flush(self) -> None:
        if not self._buffer:
            return

        rows_to_flush = len(self._buffer)
        try:
            # Build union of all column names across the batch. Different
            # event types produce different feature keys, so row dicts
            # vary within a batch.
            all_columns: dict[str, Any] = {}
            for row in self._buffer:
                for col, val in row.items():
                    if col not in all_columns or all_columns[col] is None:
                        all_columns[col] = val
            column_names = list(all_columns.keys())

            # Infer a type-appropriate default for missing values.
            # ClickHouse columns are not Nullable, so None is not valid.
            col_defaults: dict[str, Any] = {}
            for col in column_names:
                sample = all_columns[col]
                if isinstance(sample, (int, float)):
                    col_defaults[col] = 0
                else:
                    col_defaults[col] = ''

            data = [[row.get(col, col_defaults[col]) for col in column_names] for row in self._buffer]
            self._client.insert(
                f'{self._database}.{self._table}',
                data=data,
                column_names=column_names,
                column_oriented=False,
            )
            logger.info(f'Flushed {rows_to_flush} rows to ClickHouse')
        except Exception as e:
            logger.error(f'ClickHouse flush error ({rows_to_flush} rows): {e}')
            sentry_sdk.capture_exception(error=e)
        finally:
            self._buffer.clear()

    def stop(self) -> None:
        self._flush()
