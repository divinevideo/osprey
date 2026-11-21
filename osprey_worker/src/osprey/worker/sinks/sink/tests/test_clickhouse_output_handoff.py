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
    sink = ClickHouseOutputSink(SimpleNamespace(), batch_size=100, handoff=handoff)
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
