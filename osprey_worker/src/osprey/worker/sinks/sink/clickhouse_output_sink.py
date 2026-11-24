"""ClickHouse output sink — replaces KafkaOutputSink for Divine's stack.

Writes rule execution results directly to ClickHouse instead of
routing through Kafka → Druid. The table schema mirrors what Druid
would ingest so the query UI works unchanged.
"""

import json
import math
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import gevent
import sentry_sdk
from gevent.lock import Semaphore
from osprey.engine.executor.execution_context import ExecutionResult
from osprey.worker.lib.osprey_shared.logging import get_logger
from osprey.worker.sinks.output_handoff import OutputHandoff, SourcePosition
from osprey.worker.sinks.single_attempt_insert import NativeInsertOnce
from osprey.worker.sinks.sink.output_sink import BaseOutputSink

logger = get_logger()

# Default batch size before flushing to ClickHouse
DEFAULT_BATCH_SIZE = 500
DEFAULT_FLUSH_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class BufferedRow:
    values: dict[str, Any]
    size: int
    source: SourcePosition | None


@dataclass(frozen=True)
class OwnedBatch:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    query_id: str
    sources: tuple[SourcePosition | None, ...]
    size: int


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
        flush_interval_seconds: float | None = DEFAULT_FLUSH_INTERVAL_SECONDS,
        handoff: OutputHandoff | None = None,
    ):
        self._client = clickhouse_client
        self._table = table
        self._database = database
        self._batch_size = batch_size
        if flush_interval_seconds is None:
            self._flush_interval_seconds = 0.0
        elif isinstance(flush_interval_seconds, bool):
            raise ValueError(
                f'flush_interval_seconds must be a finite non-negative number, got {flush_interval_seconds!r}'
            )
        else:
            interval = float(flush_interval_seconds)
            if not math.isfinite(interval) or interval < 0:
                raise ValueError(
                    f'flush_interval_seconds must be a finite non-negative number, got {flush_interval_seconds!r}'
                )
            self._flush_interval_seconds = interval
        self._buffer: list[BufferedRow] = []
        self._accounting_owner = object()
        self._state_lock = Semaphore()
        self._flush_lock = Semaphore()
        self._flush_timer: gevent.Greenlet | None = None
        self._flush_in_progress = False
        self._stopped = False
        self._owned_batch: OwnedBatch | None = None
        self._owned_outcome: str | None = None
        self._handoff: OutputHandoff | None = None
        self._sender: NativeInsertOnce | None = None
        if handoff is not None:
            self.configure_handoff(handoff)
        logger.info(
            f'ClickHouseOutputSink initialized: {database}.{table} '
            f'(batch_size={batch_size}, flush_interval_seconds={self._flush_interval_seconds})'
        )

    def configure_handoff(self, handoff: OutputHandoff) -> None:
        if not handoff.enabled:
            return
        with self._state_lock:
            if (
                self._buffer
                or self._owned_batch is not None
                or self._handoff is not None
                or self._stopped
                or self._flush_timer is not None
                or self._flush_in_progress
            ):
                handoff.block('required_output_failed')
                raise ValueError('maintenance must be configured before accepting output')
            try:
                self._sender = NativeInsertOnce(self._client)
            except Exception:
                handoff.block('transport_guard_unavailable')
                raise
            self._handoff = handoff
            self.max_retries = 0
            self.manages_insert_transport = True

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

            source = self._handoff.source_for_action(result.action) if self._handoff else None
            item = BufferedRow(row, len(json.dumps(row).encode()), source)
            with self._state_lock:
                self._buffer.append(item)
                self._update_pending()
                full = len(self._buffer) >= self._batch_size or (
                    self._handoff is not None and not self._handoff.admission_open
                )
                if full:
                    self._cancel_flush_timer_locked()
                else:
                    self._schedule_flush_timer_locked()
            if full:
                self._flush()

        except Exception as e:
            if self._handoff:
                self._handoff.block('required_output_failed')
            logger.error(f'ClickHouse sink error: {e}')
            sentry_sdk.capture_exception(error=e)

    def _update_pending(self) -> None:
        if self._handoff:
            self._handoff.set_pending_output(
                bytes_pending=sum(row.size for row in self._buffer)
                + (self._owned_batch.size if self._owned_batch else 0),
                batches_pending=int(bool(self._buffer)) + int(self._owned_batch is not None),
                owner=self._accounting_owner,
            )

    def _seal(self) -> OwnedBatch | None:
        if not self._buffer:
            return None
        buffered = self._buffer
        # Build the same union and type defaults as the existing writer. Seal
        # everything before yielding to transport; new appends own another list.
        all_columns: dict[str, Any] = {}
        for item in buffered:
            for col, value in item.values.items():
                if col not in all_columns or all_columns[col] is None:
                    all_columns[col] = value
        columns = tuple(all_columns)
        defaults = {col: 0 if isinstance(value, (int, float)) else '' for col, value in all_columns.items()}
        rows = tuple(tuple(item.values.get(col, defaults[col]) for col in columns) for item in buffered)
        batch = OwnedBatch(
            columns=columns,
            rows=rows,
            query_id='osprey-handoff-' + uuid4().hex,
            sources=tuple(item.source for item in buffered),
            size=len(json.dumps((columns, rows)).encode()),
        )
        self._buffer = []
        return batch

    def _schedule_flush_timer_locked(self) -> None:
        if (
            self._stopped
            or self._flush_interval_seconds <= 0
            or self._flush_in_progress
            or self._owned_batch is not None
            or self._flush_timer is not None
            or not self._buffer
        ):
            return
        self._flush_timer = gevent.spawn_later(self._flush_interval_seconds, self._flush_from_timer)

    def _cancel_flush_timer_locked(self) -> None:
        timer = self._flush_timer
        self._flush_timer = None
        if timer is not None and timer is not gevent.getcurrent():
            timer.kill(block=False)

    def _flush_from_timer(self) -> None:
        try:
            with self._state_lock:
                if self._flush_timer is gevent.getcurrent():
                    self._flush_timer = None
                if self._stopped or not self._buffer:
                    return
            self._flush(apply_timeout=True)
        except Exception as error:
            if self._handoff:
                self._handoff.block('required_output_failed')
            logger.error(f'ClickHouse timer flush error: {error}')
            sentry_sdk.capture_exception(error=error)

    def _flush(self, *, apply_timeout: bool = False) -> None:
        # A second flusher may append while insertion yields, then waits here.
        # It never submits the first owner's rows or clears its successor list.
        with self._flush_lock:
            with self._state_lock:
                if self._owned_batch is not None:
                    return  # Unconfirmed batches require independent settlement.
                self._cancel_flush_timer_locked()
                self._owned_batch = self._seal()
                batch = self._owned_batch
                if batch is not None:
                    self._owned_outcome = None
                self._update_pending()
                if batch is None:
                    return
                self._flush_in_progress = True
            try:
                if self._sender is not None:
                    try:
                        receipt = self._sender.send(
                            f'{self._database}.{self._table}', batch.columns, batch.rows, batch.query_id
                        )
                    except Exception as error:
                        with self._state_lock:
                            assert self._handoff is not None
                            self._handoff.block('required_output_failed')
                            self._update_pending()
                        logger.error(f'ClickHouse flush error ({len(batch.rows)} rows): {error}')
                        sentry_sdk.capture_exception(error=error)
                        return
                    with self._state_lock:
                        self._owned_outcome = receipt.outcome
                        if receipt.outcome == 'confirmed':
                            self._owned_batch = None
                        else:
                            assert self._handoff is not None
                            self._handoff.block(
                                'insert_outcome_unknown' if receipt.outcome == 'unknown' else 'insert_not_sent'
                            )
                        self._update_pending()
                    return
                timer_timeout = gevent.Timeout(self.timeout) if apply_timeout else None
                try:
                    with timer_timeout if timer_timeout is not None else nullcontext():
                        self._client.insert(
                            f'{self._database}.{self._table}',
                            data=batch.rows,
                            column_names=batch.columns,
                            column_oriented=False,
                        )
                    logger.info(f'Flushed {len(batch.rows)} rows to ClickHouse')
                except gevent.Timeout as timeout_error:
                    if timeout_error is not timer_timeout:
                        raise
                    logger.error(f'ClickHouse flush timed out ({len(batch.rows)} rows) after {self.timeout}s')
                    sentry_sdk.capture_exception(error=timeout_error)
                except Exception as error:
                    logger.error(f'ClickHouse flush error ({len(batch.rows)} rows): {error}')
                    sentry_sdk.capture_exception(error=error)
                finally:
                    with self._state_lock:
                        self._owned_batch = None
            finally:
                with self._state_lock:
                    self._flush_in_progress = False
                    self._schedule_flush_timer_locked()

    def maintenance_status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                'pending_rows': len(self._buffer) + (len(self._owned_batch.rows) if self._owned_batch else 0),
                'unknown_batches': int(self._owned_batch is not None and self._owned_outcome == 'unknown'),
            }

    def stop(self) -> None:
        with self._state_lock:
            self._stopped = True
            self._cancel_flush_timer_locked()
        self._flush()
