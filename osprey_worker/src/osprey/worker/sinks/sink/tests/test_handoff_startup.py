from types import SimpleNamespace

import gevent
from kafka import OffsetAndMetadata, TopicPartition
from osprey.worker.sinks.handoff_supervisor import HandoffSupervisor
from osprey.worker.sinks.output_handoff import OutputHandoff
from osprey.worker.sinks.sink.input_stream import KafkaInputStream
from osprey.worker.sinks.sink.tests.test_kafka_handoff import Consumer, record


def startup():
    handoff = OutputHandoff(enabled=True)
    consumer = Consumer(record())
    consumer._subscription = SimpleNamespace(
        all_consumed_offsets=lambda: {p: OffsetAndMetadata(42, '') for p in consumer.assigned}
    )
    stream = KafkaInputStream(consumer)
    stream.configure_handoff(handoff, start_paused=True)
    return handoff, consumer, stream


def test_pristine_startup_pause_survives_assignment_churn_without_finalizing_outputs():
    handoff, consumer, stream = startup()
    waiting = gevent.spawn(next, stream)
    calls = []
    supervisor = HandoffSupervisor(
        handoff, stream, SimpleNamespace(stop=lambda: calls.append('stop')), emit=lambda _: None
    )
    try:
        gevent.sleep(0.02)
        supervisor.tick()
        gevent.sleep(0)
        consumer.listener.on_partitions_revoked(consumer.assigned)
        consumer.assigned = {TopicPartition('actions', 1)}
        consumer.listener.on_partitions_assigned(consumer.assigned)
        supervisor.tick()
        gevent.sleep(0)
        assert handoff.snapshot()['state'] == 'paused'
        assert not handoff.snapshot()['blocked_reasons']
        assert handoff.snapshot()['accepted'] == 0
        assert not handoff.certify(committed=True)
        assert not stream.request_handoff_commit()
        assert not calls
        assert not waiting.ready()
    finally:
        waiting.kill()


def test_explicit_activation_opens_only_pristine_paused_worker_and_accepts_once():
    handoff, consumer, stream = startup()
    waiting = gevent.spawn(next, stream)
    try:
        gevent.sleep(0.02)
        assert stream.request_handoff_activation()
        context = waiting.get(timeout=1)
        with context:
            context.set_disposition('completed')
        assert handoff.snapshot()['state'] == 'running'
        assert handoff.snapshot()['accepted'] == 1
        assert not stream.request_handoff_activation()
    finally:
        waiting.kill()


def test_assignment_change_invalidates_pending_activation_request():
    handoff, consumer, stream = startup()
    waiting = gevent.spawn(next, stream)
    try:
        gevent.sleep(0.02)
        assert stream.request_handoff_activation()
        consumer.listener.on_partitions_revoked(consumer.assigned)
        consumer.listener.on_partitions_assigned(consumer.assigned)
        gevent.sleep(0.02)
        assert handoff.snapshot()['state'] == 'blocked'
        assert handoff.snapshot()['accepted'] == 0
        assert not stream.request_handoff_activation()
        assert not waiting.ready()
    finally:
        waiting.kill()


def test_drain_or_failure_permanently_forbids_startup_activation():
    for failure in (False, True):
        handoff, consumer, stream = startup()
        waiting = gevent.spawn(next, stream)
        try:
            gevent.sleep(0.02)
            if failure:
                handoff.block('required_output_failed')
            else:
                handoff.request_drain()
            assert not stream.request_handoff_activation()
            gevent.sleep(0.02)
            assert handoff.snapshot()['accepted'] == 0
            assert not waiting.ready()
        finally:
            waiting.kill()


def test_activation_waits_for_complete_position_metadata_without_network_calls():
    handoff, consumer, stream = startup()
    positions = {}
    consumer._subscription.all_consumed_offsets = lambda: positions
    waiting = gevent.spawn(next, stream)
    try:
        gevent.sleep(0.02)
        assert stream.request_handoff_activation()
        gevent.sleep(0.02)
        assert handoff.snapshot()['state'] == 'paused'
        assert handoff.snapshot()['accepted'] == 0
        positions.update({p: OffsetAndMetadata(42, '') for p in consumer.assigned})
        context = waiting.get(timeout=1)
        with context:
            context.set_disposition('completed')
        assert handoff.snapshot()['accepted'] == 1
    finally:
        waiting.kill()


def test_pending_output_forbids_activation_even_below_capacity():
    for delivery in (False, True):
        handoff, consumer, stream = startup()
        if delivery:
            handoff.set_pending_deliveries(1, bytes_pending=1)
        else:
            handoff.set_pending_output(bytes_pending=1, batches_pending=1)
        waiting = gevent.spawn(next, stream)
        try:
            gevent.sleep(0.02)
            assert stream.request_handoff_activation()
            gevent.sleep(0.02)
            assert handoff.snapshot()['state'] == 'paused'
            assert handoff.snapshot()['accepted'] == 0
            assert not waiting.ready()
        finally:
            waiting.kill()


def test_startup_pause_has_no_drain_deadline_and_never_finalizes_output():
    handoff, consumer, stream = startup()
    clock = [0.0]
    calls = []
    supervisor = HandoffSupervisor(
        handoff, stream, SimpleNamespace(stop=lambda: calls.append('stop')), clock=lambda: clock[0], emit=lambda _: None
    )
    waiting = gevent.spawn(next, stream)
    try:
        gevent.sleep(0.02)
        supervisor.tick()
        clock[0] = 3601
        supervisor.tick()
        gevent.sleep(0)
        assert handoff.snapshot()['state'] == 'paused'
        assert not handoff.snapshot()['blocked_reasons']
        assert not calls
    finally:
        waiting.kill()


def test_startup_receipt_reports_ready_positions_separately_from_commit_ack():
    handoff, consumer, stream = startup()
    waiting = gevent.spawn(next, stream)
    try:
        gevent.sleep(0.02)
        receipt = stream.handoff_startup_status()
        assert receipt['ready']
        assert receipt['positions'] == [{'topic': 'actions', 'partition': 0, 'next_offset': 42}]
        assert receipt['membership_epoch'] == stream._membership_epoch
        assert not stream.handoff_offsets()
    finally:
        waiting.kill()
