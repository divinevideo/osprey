import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import gevent
from gevent.event import Event
from osprey.worker.sinks.output_handoff import OutputHandoff, SourcePosition
from osprey.worker.sinks.single_attempt_insert import InsertReceipt
from osprey.worker.sinks.sink.clickhouse_output_sink import ClickHouseOutputSink


def result(action_id: int) -> Any:
    return SimpleNamespace(
        action=SimpleNamespace(action_id=action_id, timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        extracted_features_json=json.dumps({'SyntheticRule': True, 'SyntheticValue': f'value-{action_id}'}),
    )


def test_default_mode_never_resubmits_or_erases_concurrent_append() -> None:
    started = Event()
    release = Event()
    inserted: list[int] = []

    class Client:
        def insert(self, table: str, *, data: Any, column_names: Any, **kwargs: Any) -> None:
            if not started.is_set():
                started.set()
                release.wait()
            action_column = list(column_names).index('__action_id')
            inserted.extend(row[action_column] for row in data)

    sink = ClickHouseOutputSink(Client(), batch_size=1)
    first = gevent.spawn(sink.push, result(1))
    assert started.wait(timeout=2)
    second = gevent.spawn(sink.push, result(2))
    gevent.sleep(0)
    release.set()
    gevent.joinall([first, second], timeout=2, raise_error=True)
    assert first.ready() and second.ready()
    sink.stop()
    assert sorted(inserted) == [1, 2]


def test_maintenance_unknown_retains_exact_batch_and_blocks_later_insert(monkeypatch: Any) -> None:
    calls: list[tuple[Any, ...]] = []

    class Sender:
        def send(self, table: str, columns: Any, rows: Any, query_id: str) -> InsertReceipt:
            calls.append((table, columns, rows, query_id))
            return InsertReceipt(query_id, 'unknown')

    monkeypatch.setattr(
        'osprey.worker.sinks.sink.clickhouse_output_sink.NativeInsertOnce', lambda client: Sender(), raising=False
    )
    handoff = OutputHandoff(enabled=True)
    sink = ClickHouseOutputSink(SimpleNamespace(), batch_size=1, handoff=handoff)
    first, second = result(1), result(2)
    positions = [SourcePosition('actions', 0, offset) for offset in (1, 2)]
    assert handoff.begin_poll()
    handoff.accept_poll_result(positions)
    handoff.bind_action(first.action, positions[0])
    handoff.bind_action(second.action, positions[1])
    sink.push(first)
    handoff.finish(positions[0], 'completed')
    assert not handoff.admission_open
    sink.push(second)  # Work accepted before the gate closed must still be retained.
    handoff.finish(positions[1], 'completed')
    sink.stop()
    assert len(calls) == 1
    assert sink.maintenance_status()['pending_rows'] == 2
    assert sink.maintenance_status()['unknown_batches'] == 1
    assert sink._owned_batch is not None
    assert sink._owned_batch.columns == calls[0][1]
    assert sink._owned_batch.rows == calls[0][2]
    assert sink._owned_batch.query_id == calls[0][3]
    assert not handoff.certify(committed=True)


def test_confirmed_partial_batch_releases_only_after_drain_flush(monkeypatch: Any) -> None:
    calls: list[Any] = []

    class Sender:
        def send(self, table: str, columns: Any, rows: Any, query_id: str) -> InsertReceipt:
            calls.append(rows)
            return InsertReceipt(query_id, 'confirmed')

    monkeypatch.setattr(
        'osprey.worker.sinks.sink.clickhouse_output_sink.NativeInsertOnce', lambda client: Sender(), raising=False
    )
    handoff = OutputHandoff(enabled=True)
    sink = ClickHouseOutputSink(SimpleNamespace(), batch_size=100, flush_interval_seconds=0, handoff=handoff)
    accepted = result(1)
    source = SourcePosition('actions', 0, 1)
    assert handoff.begin_poll()
    handoff.accept_poll_result([source])
    handoff.bind_action(accepted.action, source)
    sink.push(accepted)
    handoff.finish(source, 'completed')
    assert not calls
    assert handoff.snapshot()['pending_bytes'] > 0
    handoff.request_drain()
    sink.stop()
    assert len(calls) == 1
    assert sink.maintenance_status()['pending_rows'] == 0
    assert handoff.certify(committed=True)


def test_maintenance_wrapper_does_not_interrupt_or_retry_owned_insert(monkeypatch: Any) -> None:
    from osprey.worker.sinks.sink.output_sink import BaseOutputSink, MultiOutputSink

    calls: list[str] = []

    class Sender:
        def send(self, table: str, columns: Any, rows: Any, query_id: str) -> InsertReceipt:
            calls.append('insert')
            gevent.sleep(0.03)
            return InsertReceipt(query_id, 'confirmed')

    class Sibling(BaseOutputSink):
        def will_do_work(self, result: Any) -> bool:
            return True

        def push(self, result: Any) -> None:
            calls.append('sibling')

        def stop(self) -> None:
            pass

    monkeypatch.setattr('osprey.worker.sinks.sink.clickhouse_output_sink.NativeInsertOnce', lambda client: Sender())
    handoff = OutputHandoff(enabled=True)
    sink = ClickHouseOutputSink(SimpleNamespace(), batch_size=1)
    sink.timeout = 0.005
    outputs = MultiOutputSink([sink, Sibling()])
    outputs.configure_handoff(handoff)
    accepted = result(1)
    source = SourcePosition('actions', 0, 1)
    assert handoff.begin_poll()
    handoff.accept_poll_result([source])
    handoff.bind_action(accepted.action, source)
    outputs.push(accepted)
    handoff.finish(source, 'completed')
    handoff.request_drain()
    outputs.stop()
    assert calls == ['insert', 'sibling']
    assert handoff.certify(committed=True)


def test_wrapper_failure_blocks_but_finishes_sibling_output_and_stop() -> None:
    from osprey.worker.sinks.sink.output_sink import BaseOutputSink, MultiOutputSink

    calls: list[str] = []

    class Sink(BaseOutputSink):
        def __init__(self, failing: bool):
            self.failing = failing

        def will_do_work(self, result: Any) -> bool:
            return True

        def push(self, result: Any) -> None:
            calls.append('failing-push' if self.failing else 'sibling-push')
            if self.failing:
                raise ValueError('synthetic failure')

        def stop(self) -> None:
            calls.append('failing-stop' if self.failing else 'sibling-stop')
            if self.failing:
                raise ValueError('synthetic failure')

    handoff = OutputHandoff(enabled=True)
    outputs = MultiOutputSink([Sink(True), Sink(False)])
    outputs.configure_handoff(handoff)
    outputs.push(result(1))
    outputs.stop()
    assert calls == ['failing-push', 'sibling-push', 'failing-stop', 'sibling-stop']
    assert not handoff.admission_open
    assert 'required_output_failed' in handoff.snapshot()['blocked_reasons']


def test_partial_batch_flushes_after_flush_interval() -> None:
    flushed_event = Event()
    batches: list[list[int]] = []

    class Client:
        def insert(self, table: str, *, data: Any, column_names: Any, **kwargs: Any) -> None:
            action_column = list(column_names).index('__action_id')
            batches.append([row[action_column] for row in data])
            flushed_event.set()

    sink = ClickHouseOutputSink(Client(), batch_size=100, flush_interval_seconds=0.05)
    try:
        sink.push(result(1))
        sink.push(result(2))
        assert not batches
        assert flushed_event.wait(timeout=2)
        assert batches == [[1, 2]]
    finally:
        sink.stop()


def test_concurrent_append_during_timer_flush_schedules_followup_flush() -> None:
    first_started = Event()
    release_first = Event()
    second_flushed = Event()
    batches: list[list[int]] = []

    class Client:
        def insert(self, table: str, *, data: Any, column_names: Any, **kwargs: Any) -> None:
            action_column = list(column_names).index('__action_id')
            ids = [row[action_column] for row in data]
            if not first_started.is_set():
                first_started.set()
                release_first.wait(timeout=2)
            batches.append(ids)
            if len(batches) >= 2:
                second_flushed.set()

    sink = ClickHouseOutputSink(Client(), batch_size=100, flush_interval_seconds=0.03)
    try:
        sink.push(result(1))
        assert first_started.wait(timeout=2)
        # Arrives while the first timer-triggered flush is in flight.
        sink.push(result(2))
        release_first.set()
        assert second_flushed.wait(timeout=2)
        assert batches == [[1], [2]]
    finally:
        sink.stop()


def test_flush_interval_disabled_when_zero() -> None:
    calls: list[Any] = []

    class Client:
        def insert(self, table: str, *, data: Any, column_names: Any, **kwargs: Any) -> None:
            calls.append(data)

    sink = ClickHouseOutputSink(Client(), batch_size=100, flush_interval_seconds=0)
    sink.push(result(1))
    gevent.sleep(0.05)
    assert not calls
    sink.stop()
    assert len(calls) == 1


def test_handoff_timer_flush_confirms_partial_batch_and_clears_pending(monkeypatch: Any) -> None:
    calls: list[Any] = []

    class Sender:
        def send(self, table: str, columns: Any, rows: Any, query_id: str) -> InsertReceipt:
            calls.append(rows)
            return InsertReceipt(query_id, 'confirmed')

    monkeypatch.setattr(
        'osprey.worker.sinks.sink.clickhouse_output_sink.NativeInsertOnce', lambda client: Sender(), raising=False
    )
    handoff = OutputHandoff(enabled=True)
    sink = ClickHouseOutputSink(SimpleNamespace(), batch_size=100, flush_interval_seconds=0.03, handoff=handoff)
    try:
        accepted = result(1)
        source = SourcePosition('actions', 0, 1)
        assert handoff.begin_poll()
        handoff.accept_poll_result([source])
        handoff.bind_action(accepted.action, source)
        sink.push(accepted)
        handoff.finish(source, 'completed')
        with gevent.Timeout(2):
            while sink.maintenance_status()['pending_rows'] > 0:
                gevent.sleep(0.01)
        assert len(calls) == 1
        assert sink.maintenance_status()['pending_rows'] == 0
        assert handoff.snapshot()['pending_bytes'] == 0
        handoff.request_drain()
        sink.stop()
        assert len(calls) == 1
        assert handoff.certify(committed=True)
    finally:
        sink.stop()


def test_handoff_timer_flush_unknown_blocks_and_never_replays(monkeypatch: Any) -> None:
    calls: list[Any] = []

    class Sender:
        def send(self, table: str, columns: Any, rows: Any, query_id: str) -> InsertReceipt:
            calls.append((columns, rows, query_id))
            return InsertReceipt(query_id, 'unknown')

    monkeypatch.setattr(
        'osprey.worker.sinks.sink.clickhouse_output_sink.NativeInsertOnce', lambda client: Sender(), raising=False
    )
    handoff = OutputHandoff(enabled=True)
    sink = ClickHouseOutputSink(SimpleNamespace(), batch_size=100, flush_interval_seconds=0.02, handoff=handoff)
    try:
        accepted = result(1)
        source = SourcePosition('actions', 0, 1)
        assert handoff.begin_poll()
        handoff.accept_poll_result([source])
        handoff.bind_action(accepted.action, source)
        sink.push(accepted)
        handoff.finish(source, 'completed')
        with gevent.Timeout(2):
            while handoff.admission_open:
                gevent.sleep(0.01)
        gevent.sleep(0.05)
        sink.stop()
        assert len(calls) == 1
        assert sink.maintenance_status()['pending_rows'] == 1
        assert sink.maintenance_status()['unknown_batches'] == 1
        assert 'insert_outcome_unknown' in handoff.snapshot()['blocked_reasons']
        assert not handoff.certify(committed=True)
    finally:
        sink.stop()


def test_handoff_timer_flush_sender_exception_blocks_handoff(monkeypatch: Any) -> None:
    class Sender:
        def send(self, table: str, columns: Any, rows: Any, query_id: str) -> InsertReceipt:
            raise RuntimeError('synthetic sender crash')

    monkeypatch.setattr(
        'osprey.worker.sinks.sink.clickhouse_output_sink.NativeInsertOnce', lambda client: Sender(), raising=False
    )
    handoff = OutputHandoff(enabled=True)
    sink = ClickHouseOutputSink(SimpleNamespace(), batch_size=100, flush_interval_seconds=0.02, handoff=handoff)
    try:
        accepted = result(1)
        source = SourcePosition('actions', 0, 1)
        assert handoff.begin_poll()
        handoff.accept_poll_result([source])
        handoff.bind_action(accepted.action, source)
        sink.push(accepted)
        handoff.finish(source, 'completed')
        with gevent.Timeout(2):
            while handoff.admission_open:
                gevent.sleep(0.01)
        assert 'required_output_failed' in handoff.snapshot()['blocked_reasons']
        assert sink.maintenance_status()['pending_rows'] == 1
        assert not handoff.certify(committed=True)
    finally:
        sink.stop()


def test_invalid_flush_interval_rejected() -> None:
    import pytest

    for bad in (-1, -0.01, float('inf'), float('-inf'), float('nan')):
        with pytest.raises(ValueError):
            ClickHouseOutputSink(SimpleNamespace(), flush_interval_seconds=bad)

    sink = ClickHouseOutputSink(SimpleNamespace(), flush_interval_seconds=None)
    assert sink._flush_interval_seconds == 0.0
    sink.stop()


def test_batch_full_and_stop_cancel_pending_timer() -> None:
    batches: list[list[int]] = []

    class Client:
        def insert(self, table: str, *, data: Any, column_names: Any, **kwargs: Any) -> None:
            action_column = list(column_names).index('__action_id')
            batches.append([row[action_column] for row in data])

    sink = ClickHouseOutputSink(Client(), batch_size=2, flush_interval_seconds=0.05)
    sink.push(result(1))
    assert sink._flush_timer is not None
    sink.push(result(2))
    assert sink._flush_timer is None
    assert batches == [[1, 2]]
    gevent.sleep(0.08)
    assert batches == [[1, 2]]

    sink.push(result(3))
    assert sink._flush_timer is not None
    sink.stop()
    assert sink._flush_timer is None
    assert batches == [[1, 2], [3]]

    # Post-stop pushes do not arm a new timer.
    sink.push(result(4))
    assert sink._flush_timer is None


def test_stalled_inflight_flush_does_not_pile_up_timers() -> None:
    first_started = Event()
    release_first = Event()
    second_flushed = Event()
    batches: list[list[int]] = []

    class Client:
        def insert(self, table: str, *, data: Any, column_names: Any, **kwargs: Any) -> None:
            action_column = list(column_names).index('__action_id')
            ids = [row[action_column] for row in data]
            if not first_started.is_set():
                first_started.set()
                release_first.wait(timeout=2)
            batches.append(ids)
            if len(batches) >= 2:
                second_flushed.set()

    sink = ClickHouseOutputSink(Client(), batch_size=100, flush_interval_seconds=0.02)
    try:
        sink.push(result(1))
        assert first_started.wait(timeout=2)
        # Multiple pushes while flush is stalled longer than flush_interval_seconds
        # should not spawn timers until the in-flight flush finishes.
        sink.push(result(2))
        gevent.sleep(0.05)
        sink.push(result(3))
        assert sink._flush_timer is None
        release_first.set()
        assert second_flushed.wait(timeout=2)
        assert batches == [[1], [2, 3]]
    finally:
        sink.stop()


def test_default_mode_timer_flush_enforces_timeout() -> None:
    started = Event()
    timed_out = Event()

    class HangingClient:
        def insert(self, table: str, *, data: Any, column_names: Any, **kwargs: Any) -> None:
            started.set()
            try:
                gevent.sleep(5)
            except gevent.Timeout:
                timed_out.set()
                raise

    sink = ClickHouseOutputSink(HangingClient(), batch_size=100, flush_interval_seconds=0.02)
    sink.timeout = 0.05
    try:
        sink.push(result(1))
        assert started.wait(timeout=2)
        assert timed_out.wait(timeout=2)
        with gevent.Timeout(2):
            while sink._flush_in_progress:
                gevent.sleep(0.01)
        assert sink.maintenance_status()['pending_rows'] == 0
    finally:
        sink.stop()
