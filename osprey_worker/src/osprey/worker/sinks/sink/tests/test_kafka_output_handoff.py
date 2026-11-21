from types import SimpleNamespace
from typing import Any

from kafka.future import Future
from osprey.worker.sinks.output_handoff import OutputHandoff, SourcePosition
from osprey.worker.sinks.sink.kafka_output_sink import KafkaOutputSink


class Producer:
    def __init__(self):
        self.future = Future()
        self.sends = 0
        self.flushes = 0

    def send(self, **kwargs: Any) -> Any:
        self.sends += 1
        return self.future

    def flush(self, timeout: int) -> None:
        self.flushes += 1


def accepted(handoff: OutputHandoff) -> Any:
    action = object()
    position = SourcePosition('actions', 0, 42)
    assert handoff.begin_poll()
    handoff.accept_poll_result([position])
    handoff.bind_action(action, position)
    return SimpleNamespace(action=action, extracted_features_json='{"Synthetic":true}')


def test_kafka_delivery_requires_future_ack_not_flush_return() -> None:
    handoff = OutputHandoff(enabled=True)
    producer = Producer()
    sink = KafkaOutputSink('results', producer)
    sink.configure_handoff(handoff)
    sink.push(accepted(handoff))
    handoff.finish(SourcePosition('actions', 0, 42), 'completed')
    handoff.request_drain()
    sink.stop()
    assert producer.flushes == 1
    assert handoff.snapshot()['pending_deliveries'] == 1
    assert not handoff.certify(committed=True)
    producer.future.success(None)
    assert handoff.snapshot()['pending_deliveries'] == 0
    assert handoff.certify(committed=True)


def test_kafka_delivery_failure_retains_exact_payload_and_blocks_without_replay() -> None:
    handoff = OutputHandoff(enabled=True)
    producer = Producer()
    sink = KafkaOutputSink('results', producer)
    sink.configure_handoff(handoff)
    sink.push(accepted(handoff))
    producer.future.failure(RuntimeError('synthetic failure'))
    sink.stop()
    sink.stop()
    assert producer.sends == 1
    assert not handoff.admission_open
    assert sink.maintenance_status()['failed_deliveries'] == 1
    assert next(iter(sink._deliveries.values())).payload == b'{"Synthetic":true}'
    assert 'kafka_delivery_failed' in handoff.snapshot()['blocked_reasons']


def test_kafka_pending_payloads_share_the_admission_memory_budget() -> None:
    handoff = OutputHandoff(enabled=True, max_pending_bytes=8)
    producer = Producer()
    sink = KafkaOutputSink('results', producer)
    sink.configure_handoff(handoff)
    sink.push(accepted(handoff))
    assert handoff.snapshot()['pending_bytes'] == len(b'{"Synthetic":true}')
    assert not handoff.admission_open
    assert handoff.snapshot()['state'] == 'blocked'
    assert sink.maintenance_status()['pending_deliveries'] == 1
