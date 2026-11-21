import json
from types import SimpleNamespace
from typing import Any

import gevent
from kafka import OffsetAndMetadata, TopicPartition
from osprey.worker.sinks.output_handoff import OutputHandoff, SourcePosition
from osprey.worker.sinks.sink.input_stream import KafkaInputStream


class Consumer:
    def __init__(self, record: Any = None):
        self.record = record
        self.listener: Any = None
        self.paused: set[Any] = set()
        self.assigned = {TopicPartition('actions', 0)}
        self.polls = 0
        self.on_poll: Any = None
        self.config = {'group_id': 'synthetic-group'}
        self._coordinator = SimpleNamespace(config={'enable_auto_commit': True})
        self._subscription = SimpleNamespace(
            all_consumed_offsets=lambda: {p: OffsetAndMetadata(42, '') for p in self.assigned}
        )

    def subscription(self) -> set[str]:
        return {'actions'}

    def subscribe(self, topics: Any, listener: Any) -> None:
        self.listener = listener
        listener.on_partitions_assigned(self.assigned)

    def assignment(self) -> set[Any]:
        return self.assigned

    def pause(self, *partitions: Any) -> None:
        self.paused.update(partitions)

    def resume(self, *partitions: Any) -> None:
        self.paused.difference_update(partitions)

    def poll(self, **kwargs: Any) -> dict[Any, list[Any]]:
        assert kwargs == {'timeout_ms': 250, 'max_records': 1}
        self.polls += 1
        if self.on_poll:
            self.on_poll()
            self.on_poll = None
        gevent.sleep(0.001)
        if self.record is not None and not self.paused:
            record, self.record = self.record, None
            return {TopicPartition('actions', 0): [record]}
        return {}


def record(value: bytes | None = None) -> Any:
    return SimpleNamespace(
        topic='actions',
        partition=0,
        offset=42,
        value=value
        or json.dumps(
            {
                'send_time': '2026-01-01T00:00:00Z',
                'data': {'action_id': 1, 'action_name': 'synthetic', 'data': {}},
            }
        ).encode(),
    )


def test_outstanding_poll_returns_accepted_action_after_drain_request() -> None:
    handoff = OutputHandoff(enabled=True)
    consumer = Consumer(record())
    stream = KafkaInputStream(consumer)
    stream.configure_handoff(handoff)
    consumer.on_poll = handoff.request_drain
    context = next(stream)
    with context as action:
        assert handoff.source_for_action(action) == SourcePosition('actions', 0, 42)
        context.set_disposition('completed')
    assert handoff.snapshot()['completed'] == 1
    waiting = gevent.spawn(next, stream)
    gevent.sleep(0.02)
    assert not waiting.ready()
    assert consumer.paused == consumer.assigned
    assert consumer.polls > 1  # Membership remains serviced while held.
    waiting.kill()


def test_start_paused_never_delivers_prefetched_records() -> None:
    handoff = OutputHandoff(enabled=True)
    consumer = Consumer(record())
    stream = KafkaInputStream(consumer)
    stream.configure_handoff(handoff, start_paused=True)
    waiting = gevent.spawn(next, stream)
    gevent.sleep(0.02)
    assert not waiting.ready()
    assert consumer.polls > 1
    assert handoff.snapshot()['accepted'] == 0
    assert consumer.record is not None
    assert consumer.paused == consumer.assigned
    waiting.kill()


def test_malformed_accepted_record_blocks_and_keeps_worker_held() -> None:
    handoff = OutputHandoff(enabled=True)
    consumer = Consumer(record(b'invalid-json'))
    stream = KafkaInputStream(consumer)
    stream.configure_handoff(handoff)
    waiting = gevent.spawn(next, stream)
    gevent.sleep(0.02)
    assert not waiting.ready()
    assert handoff.snapshot()['accepted'] == 1
    assert handoff.snapshot()['failed'] == 1
    assert not handoff.admission_open
    assert consumer.paused == consumer.assigned
    waiting.kill()


def test_new_assignment_is_paused_and_invalidates_held_certificate() -> None:
    handoff = OutputHandoff(enabled=True)
    consumer = Consumer()
    stream = KafkaInputStream(consumer)
    stream.configure_handoff(handoff)
    handoff.request_drain()
    assert handoff.certify(committed=True)
    consumer.listener.on_partitions_revoked(consumer.assigned)
    consumer.assigned = {TopicPartition('actions', 1)}
    consumer.listener.on_partitions_assigned(consumer.assigned)
    assert consumer.assigned <= consumer.paused
    assert not handoff.snapshot()['local_drained']
    assert 'membership_changed' in handoff.snapshot()['blocked_reasons']


def commit_fixture() -> tuple[Any, Any, Any, Any]:
    from kafka import OffsetAndMetadata

    handoff = OutputHandoff(enabled=True)
    consumer = Consumer()
    consumer._subscription = SimpleNamespace(
        all_consumed_offsets=lambda: {
            TopicPartition('actions', 0): OffsetAndMetadata(42, ''),
        }
    )
    consumer.commit_callback = None
    consumer.commits = 0

    def commit_async(offsets: Any, callback: Any) -> Any:
        consumer.commits += 1
        consumer.commit_callback = lambda response: callback(offsets, response)
        # A coordinator lookup Future can complete before the actual commit.
        return SimpleNamespace(is_done=True, succeeded=lambda: True)

    consumer.commit_async = commit_async
    stream = KafkaInputStream(consumer)
    stream.configure_handoff(handoff)
    handoff.request_drain()
    waiting = gevent.spawn(next, stream)
    gevent.sleep(0.02)
    return handoff, consumer, stream, waiting


def test_final_commit_requires_explicit_offset_ack_not_lookup_future() -> None:
    handoff, consumer, stream, waiting = commit_fixture()
    try:
        with gevent.Timeout(2):
            assert stream.request_handoff_commit()
        gevent.sleep(0.01)
        assert not handoff.snapshot()['local_drained']
        assert consumer.commits == 1
        consumer.commit_callback(None)
        gevent.sleep(0.01)
        assert handoff.snapshot()['local_drained']
        assert stream.handoff_offsets() == [{'topic': 'actions', 'partition': 0, 'next_offset': 42}]
        assert stream.request_handoff_commit()
        assert consumer.commits == 1
    finally:
        waiting.kill()


def test_failed_final_offset_commit_blocks_without_retry_or_consumer_removal() -> None:
    handoff, consumer, stream, waiting = commit_fixture()
    try:
        assert stream.request_handoff_commit()
        consumer.commit_callback(RuntimeError('synthetic commit failure'))
        gevent.sleep(0.01)
        assert not handoff.snapshot()['local_drained']
        assert 'offset_commit_failed' in handoff.snapshot()['blocked_reasons']
        assert not stream.request_handoff_commit()
        assert consumer.commits == 1
        assert not waiting.ready()
    finally:
        waiting.kill()


def test_pending_outputs_prevent_final_offset_commit() -> None:
    handoff, consumer, stream, waiting = commit_fixture()
    try:
        handoff.set_pending_output(bytes_pending=1, batches_pending=1)
        assert not stream.request_handoff_commit()
        assert consumer.commits == 0
    finally:
        waiting.kill()
