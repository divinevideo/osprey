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

    def will_do_work(self, result: ExecutionResult) -> bool:
        return True

    def push(self, result: ExecutionResult) -> None:
        try:
            features = json.loads(result.extracted_features_json)

            row = {
                '__time': result.action.timestamp.isoformat(),
                '__action_id': result.action.action_id,
                **features,
            }

            # Add verdict info if present
            if result.verdicts:
                row['__verdicts'] = json.dumps([v.value if hasattr(v, 'value') else str(v) for v in result.verdicts])

            # Add rule hit info
            if result.rule_results:
                row['__rule_hits'] = json.dumps(
                    {name: bool(val) for name, val in result.rule_results.items() if val is not None}
                )

            self._buffer.append(row)

            if len(self._buffer) >= self._batch_size:
                self._flush()

        except Exception as e:
            logger.error(f'ClickHouse sink error: {e}')
            sentry_sdk.capture_exception(error=e)

    def _flush(self) -> None:
        if not self._buffer:
            return

        try:
            self._client.insert(
                f'{self._database}.{self._table}',
                data=self._buffer,
                column_oriented=False,
            )
            logger.debug(f'Flushed {len(self._buffer)} rows to ClickHouse')
        except Exception as e:
            logger.error(f'ClickHouse flush error ({len(self._buffer)} rows): {e}')
            sentry_sdk.capture_exception(error=e)
        finally:
            self._buffer.clear()

    def stop(self) -> None:
        self._flush()
