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
from kafka import KafkaProducer
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


def test_three_native_members_start_paused_activate_and_commit_exact_drained_boundaries():
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
            'import sys; run_native_three_members(sys.argv[1])',
            bootstrap,
        ],
        capture_output=True,
        timeout=120,
        env={**os.environ, 'DD_TRACE_ENABLED': 'false', 'DD_INSTRUMENTATION_TELEMETRY_ENABLED': 'false'},
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-8192:].decode(errors='replace')


def run_native_three_members(bootstrap):
    assert re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', bootstrap), 'loopback broker required'
    topic = f'synthetic-handoff-{uuid.uuid4().hex}'
    admin = KafkaAdminClient(bootstrap_servers=[bootstrap], request_timeout_ms=10000)
    producer = None
    workers = []
    try:
        admin.create_topics([NewTopic(topic, 3, 1)])
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

        def consume(stream):
            for context in stream:
                with context:
                    context.set_disposition('completed')

        # Join sequentially to force real group-generation changes while paused.
        for index in range(3):
            consumer = PatchedKafkaConsumer(
                topic,
                bootstrap_servers=[bootstrap],
                group_id=f'{topic}-group',
                client_id=f'synthetic-{index}',
                partition_assignment_strategy=(RoundRobinPartitionAssignor,),
                auto_offset_reset='earliest',
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
        for consumer, handoff, stream, output, loop, _ in workers:
            assert consumer.assignment() <= consumer.paused()
            assert handoff.snapshot()['state'] == 'paused'
            assert handoff.snapshot()['accepted'] == 0
            assert not handoff.snapshot()['blocked_reasons']
            assert not loop.ready()
            assert output.stops == 0
            assert stream.request_handoff_activation()
        wait_for(lambda: all(worker[1].snapshot()['completed'] == 5 for worker in workers))
        for worker in workers:
            worker[1].request_drain()
        wait_for(lambda: all(worker[1].snapshot()['local_drained'] for worker in workers))
        for consumer, handoff, stream, output, loop, _ in workers:
            state = handoff.snapshot()
            assert state['accepted'] == state['completed'] == 5
            assert state['active'] == state['pending_bytes'] == state['pending_deliveries'] == 0
            assert not state['blocked_reasons']
            assert not loop.ready()
            assert output.stops == 1
            assert len(stream.handoff_offsets()) == 1
            assert stream.handoff_offsets()[0]['next_offset'] == 5
            for partition in consumer.assignment():
                assert consumer.committed(partition) == 5
    finally:
        for consumer, _, _, _, loop, watch in workers:
            watch.kill()
            loop.kill()
            consumer.close(autocommit=False)
        if producer is not None:
            producer.close(timeout=10)
        admin.delete_topics([topic])
        admin.close()
