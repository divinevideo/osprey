"""Opt-in native broker rehearsal; synthetic data and a disposable loopback broker only."""

import json
import os
import re
import subprocess
import sys
import time
import uuid

import gevent
import pytest
from kafka import KafkaProducer, OffsetAndMetadata, TopicPartition
from kafka.admin import KafkaAdminClient, NewTopic
from kafka.coordinator.assignors.roundrobin import RoundRobinPartitionAssignor
from osprey.worker.sinks.handoff_supervisor import HandoffSupervisor
from osprey.worker.sinks.output_handoff import OutputHandoff
from osprey.worker.sinks.sink.input_stream import KafkaInputStream
from osprey.worker.sinks.utils.kafka import PatchedKafkaConsumer


def wait_for(predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        gevent.sleep(0.05)
    assert predicate(), 'native handoff condition did not pass within its observation bound'


class CountingOutput:
    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1


@pytest.mark.parametrize('start_offset', [0, 2])
def test_three_native_members_start_paused_activate_and_commit_exact_drained_boundaries(start_offset):
    bootstrap = os.environ.get('OSPREY_HANDOFF_TEST_KAFKA')
    if not bootstrap:
        pytest.skip('requires an explicitly configured disposable native Kafka broker')
    assert re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', bootstrap), 'loopback broker required'
    result = subprocess.run(
        [
            sys.executable,
            '-c',
            'from osprey.worker.lib.patcher import patch_all; patch_all(patch_ddtrace=False); '
            'from osprey.worker.sinks.sink.tests.test_native_kafka_handoff import run_native_three_members; '
            'import sys; run_native_three_members(sys.argv[1], int(sys.argv[2]))',
            bootstrap,
            str(start_offset),
        ],
        capture_output=True,
        timeout=120,
        env={**os.environ, 'DD_TRACE_ENABLED': 'false', 'DD_INSTRUMENTATION_TELEMETRY_ENABLED': 'false'},
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-8192:].decode(errors='replace')


def run_native_three_members(bootstrap, start_offset=0):
    assert re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', bootstrap), 'loopback broker required'
    topic = f'synthetic-handoff-{uuid.uuid4().hex}'
    admin = KafkaAdminClient(bootstrap_servers=[bootstrap], request_timeout_ms=10000)
    producer = None
    workers = []
    try:
        admin.create_topics([NewTopic(topic, 3, 1)])
        partitions = [TopicPartition(topic, partition) for partition in range(3)]
        group = f'{topic}-group'
        producer = KafkaProducer(bootstrap_servers=[bootstrap], request_timeout_ms=10000)
        for partition in range(3):
            for offset in range(5):
                value = json.dumps(
                    {
                        'send_time': '2026-01-01T00:00:00Z',
                        'data': {'action_id': partition * 10 + offset, 'action_name': 'synthetic', 'data': {}},
                    }
                ).encode()
                producer.send(topic, partition=partition, value=value).get(timeout=10)
        producer.flush(timeout=10)
        seed = PatchedKafkaConsumer(bootstrap_servers=[bootstrap], group_id=group, enable_auto_commit=False)
        try:
            seed.assign(partitions)
            seed.commit({partition: OffsetAndMetadata(start_offset, '') for partition in partitions})
        finally:
            seed.close(autocommit=False)
        baseline = admin.list_consumer_group_offsets(group, partitions=partitions)
        assert set(baseline) == set(partitions)
        assert all(value.offset == start_offset for value in baseline.values())

        groupless = PatchedKafkaConsumer(topic, bootstrap_servers=[bootstrap])
        try:
            with pytest.raises(ValueError, match='named Kafka group'):
                KafkaInputStream(groupless).configure_handoff(OutputHandoff(enabled=True), start_paused=True)
        finally:
            groupless.close(autocommit=False)

        def consume(stream):
            for context in stream:
                with context:
                    context.set_disposition('completed')

        # Join sequentially to force real group-generation changes while paused.
        for index in range(3):
            consumer = PatchedKafkaConsumer(
                topic,
                bootstrap_servers=[bootstrap],
                group_id=group,
                client_id=f'synthetic-{index}',
                partition_assignment_strategy=(RoundRobinPartitionAssignor,),
                auto_offset_reset='none',
                request_timeout_ms=15000,
                session_timeout_ms=10000,
                max_poll_interval_ms=30000,
            )
            handoff = OutputHandoff(enabled=True)
            stream = KafkaInputStream(consumer)
            stream.configure_handoff(handoff, start_paused=True)
            output = CountingOutput()
            supervisor = HandoffSupervisor(handoff, stream, output, emit=lambda _: None)
            loop, watch = gevent.spawn(consume, stream), gevent.spawn(supervisor.run)
            workers.append((consumer, handoff, stream, output, loop, watch))
            wait_for(lambda: bool(consumer.assignment()))
        wait_for(lambda: all(len(worker[0].assignment()) == 1 for worker in workers))
        wait_for(lambda: all(worker[2].handoff_startup_status()['ready'] for worker in workers))
        # Cover both rejoin-time commits above and a complete default five-second
        # auto-commit interval. No explicit commit or activation has occurred.
        gevent.sleep(5.5)
        still_paused = admin.list_consumer_group_offsets(group, partitions=partitions)
        assert set(still_paused) == set(partitions)
        assert all(value.offset == start_offset for value in still_paused.values())
        for consumer, handoff, stream, output, loop, _ in workers:
            assert consumer.assignment() <= consumer.paused()
            assert handoff.snapshot()['state'] == 'paused'
            assert handoff.snapshot()['accepted'] == 0
            assert not handoff.snapshot()['blocked_reasons']
            assert not loop.ready()
            assert output.stops == 0
            assert stream.request_handoff_activation()
        accepted = 5 - start_offset
        wait_for(lambda: all(worker[1].snapshot()['completed'] == accepted for worker in workers))
        for worker in workers:
            worker[1].request_drain()
        wait_for(lambda: all(worker[1].snapshot()['local_drained'] for worker in workers))
        for consumer, handoff, stream, output, loop, _ in workers:
            state = handoff.snapshot()
            assert state['accepted'] == state['completed'] == accepted
            assert state['active'] == state['pending_bytes'] == state['pending_deliveries'] == 0
            assert not state['blocked_reasons']
            assert not loop.ready()
            assert output.stops == 1
            assert len(stream.handoff_offsets()) == 1
            assert stream.handoff_offsets()[0]['next_offset'] == 5
        broker_offsets = admin.list_consumer_group_offsets(group, partitions=partitions)
        assert set(broker_offsets) == set(partitions)
        assert all(value.offset == 5 for value in broker_offsets.values())
    finally:
        for consumer, _, _, _, loop, watch in workers:
            watch.kill()
            loop.kill()
            consumer.close(autocommit=False)
        if producer is not None:
            producer.close(timeout=10)
        admin.delete_topics([topic])
        admin.close()


def test_native_startup_cancellation_does_not_advance_existing_commit():
    bootstrap = os.environ.get('OSPREY_HANDOFF_TEST_KAFKA')
    if not bootstrap:
        pytest.skip('requires an explicitly configured disposable native Kafka broker')
    assert re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', bootstrap), 'loopback broker required'
    result = subprocess.run(
        [
            sys.executable,
            '-c',
            'from osprey.worker.lib.patcher import patch_all; patch_all(patch_ddtrace=False); '
            'from osprey.worker.sinks.sink.tests.test_native_kafka_handoff import run_native_cancelled_startup; '
            'import sys; run_native_cancelled_startup(sys.argv[1])',
            bootstrap,
        ],
        capture_output=True,
        timeout=60,
        env={**os.environ, 'DD_TRACE_ENABLED': 'false', 'DD_INSTRUMENTATION_TELEMETRY_ENABLED': 'false'},
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-8192:].decode(errors='replace')


def run_native_cancelled_startup(bootstrap):
    import signal

    assert re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', bootstrap), 'loopback broker required'
    topic = f'synthetic-cancel-{uuid.uuid4().hex}'
    group = f'{topic}-group'
    partition = TopicPartition(topic, 0)
    admin = KafkaAdminClient(bootstrap_servers=[bootstrap], request_timeout_ms=10000)
    producer = consumer = loop = watch = handler = None
    try:
        admin.create_topics([NewTopic(topic, 1, 1)])
        producer = KafkaProducer(bootstrap_servers=[bootstrap], request_timeout_ms=10000)
        for _ in range(4):
            producer.send(topic, value=b'synthetic-not-consumable', partition=0).get(timeout=10)
        producer.flush(timeout=10)
        seed = PatchedKafkaConsumer(bootstrap_servers=[bootstrap], group_id=group, enable_auto_commit=False)
        try:
            seed.assign([partition])
            seed.commit({partition: OffsetAndMetadata(2, '')})
        finally:
            seed.close(autocommit=False)
        assert admin.list_consumer_group_offsets(group, partitions=[partition])[partition].offset == 2
        consumer = PatchedKafkaConsumer(topic, bootstrap_servers=[bootstrap], group_id=group, auto_offset_reset='none')
        handoff = OutputHandoff(enabled=True)
        stream = KafkaInputStream(consumer)
        stream.configure_handoff(handoff, start_paused=True)
        supervisor = HandoffSupervisor(handoff, stream, CountingOutput(), emit=lambda _: None)
        handler = gevent.signal_handler(signal.SIGTERM, handoff.request_drain)
        loop, watch = gevent.spawn(next, stream), gevent.spawn(supervisor.run)
        wait_for(lambda: stream.handoff_startup_status()['ready'])
        assert stream.handoff_startup_status()['positions'][0]['next_offset'] == 2
        os.kill(os.getpid(), signal.SIGTERM)
        wait_for(lambda: handoff.snapshot()['state'] == 'blocked')
        gevent.sleep(0.5)
        assert handoff.snapshot()['blocked_reasons'] == ['startup_cancelled']
        assert handoff.snapshot()['accepted'] == 0
        assert not handoff.snapshot()['local_drained']
        assert not stream.request_handoff_commit()
        assert not stream.request_handoff_activation()
        assert not loop.ready()
        assert admin.list_consumer_group_offsets(group, partitions=[partition])[partition].offset == 2
    finally:
        if handler is not None:
            handler.cancel()
        for task in (watch, loop):
            if task is not None:
                task.kill()
        if consumer is not None:
            consumer.close(autocommit=False)
        if producer is not None:
            producer.close(timeout=10)
        admin.delete_topics([topic])
        admin.close()


@pytest.mark.parametrize('committed_offset', [None, 99])
def test_native_command_never_resets_missing_or_out_of_range_offsets(committed_offset):
    bootstrap = os.environ.get('OSPREY_HANDOFF_TEST_KAFKA')
    if not bootstrap:
        pytest.skip('requires an explicitly configured disposable native Kafka broker')
    assert re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', bootstrap), 'loopback broker required'
    result = subprocess.run(
        [
            sys.executable,
            '-c',
            'from osprey.worker.lib.patcher import patch_all; patch_all(patch_ddtrace=False); '
            'from osprey.worker.sinks.sink.tests.test_native_kafka_handoff import run_native_no_reset; '
            'import sys; run_native_no_reset(sys.argv[1], int(sys.argv[2]))',
            bootstrap,
            str(-1 if committed_offset is None else committed_offset),
        ],
        capture_output=True,
        timeout=90,
        env={**os.environ, 'DD_TRACE_ENABLED': 'false', 'DD_INSTRUMENTATION_TELEMETRY_ENABLED': 'false'},
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-8192:].decode(errors='replace')


def run_native_no_reset(bootstrap, committed_offset):
    from types import SimpleNamespace
    from unittest.mock import patch

    from osprey.worker.sinks import input_stream_chooser as chooser

    assert re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', bootstrap), 'loopback broker required'
    topic = f'synthetic-no-reset-{uuid.uuid4().hex}'
    group = f'{topic}-group'
    partition = TopicPartition(topic, 0)
    admin = KafkaAdminClient(bootstrap_servers=[bootstrap], request_timeout_ms=10000)
    producer = consumer = loop = watch = None
    try:
        admin.create_topics([NewTopic(topic, 1, 1)])
        producer = KafkaProducer(bootstrap_servers=[bootstrap], request_timeout_ms=10000)
        for offset in range(4):
            value = json.dumps(
                {
                    'send_time': '2026-01-01T00:00:00Z',
                    'data': {'action_id': offset, 'action_name': 'synthetic', 'data': {}},
                }
            ).encode()
            producer.send(topic, value=value, partition=0).get(timeout=10)
        producer.flush(timeout=10)
        if committed_offset != -1:
            seed = PatchedKafkaConsumer(bootstrap_servers=[bootstrap], group_id=group, enable_auto_commit=False)
            try:
                seed.assign([partition])
                seed.commit({partition: OffsetAndMetadata(committed_offset, '')})
            finally:
                seed.close(autocommit=False)
        config = SimpleNamespace(
            get_str=lambda key, default: {'OSPREY_KAFKA_INPUT_STREAM_TOPIC': topic}.get(key, default),
            get_optional_str=lambda key: {'OSPREY_KAFKA_GROUP_ID': group}.get(key),
            get_str_list=lambda key, default: [bootstrap],
            get_bool=lambda key, default: key == 'OSPREY_MIGRATION_HANDOFF' or default,
        )
        with patch.object(chooser.CONFIG, 'instance', return_value=config):
            stream = chooser.get_rules_sink_input_stream(chooser.InputStreamSource.KAFKA)
        consumer = stream._consumer
        assert consumer.config['auto_offset_reset'] == 'none'
        handoff = OutputHandoff(enabled=True)
        stream.configure_handoff(handoff, start_paused=True)
        output = CountingOutput()
        supervisor = HandoffSupervisor(handoff, stream, output, emit=lambda _: None)
        loop, watch = gevent.spawn(next, stream), gevent.spawn(supervisor.run)
        if committed_offset != -1:
            # A cached position is not proof that its offset is still retained.
            # Exercise the error path after a deliberately invalid activation.
            wait_for(lambda: stream.handoff_startup_status()['ready'])
            assert stream.handoff_startup_status()['positions'][0]['next_offset'] == committed_offset
            assert stream.request_handoff_activation()
        wait_for(lambda: handoff.snapshot()['state'] == 'blocked')
        assert handoff.snapshot()['accepted'] == 0
        assert not handoff.snapshot()['local_drained']
        assert not stream.handoff_startup_status()['ready']
        assert not stream.request_handoff_activation()
        assert not loop.ready()
        assert not output.stops
        broker = admin.list_consumer_group_offsets(group, partitions=[partition])[partition].offset
        assert broker == committed_offset
        assert consumer._subscription.all_consumed_offsets().get(partition, OffsetAndMetadata(-1, '')).offset == (
            committed_offset
        )
    finally:
        for task in (watch, loop):
            if task is not None:
                task.kill()
        if consumer is not None:
            consumer.close(autocommit=False)
        if producer is not None:
            producer.close(timeout=10)
        admin.delete_topics([topic])
        admin.close()
