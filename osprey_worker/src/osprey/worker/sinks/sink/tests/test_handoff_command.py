"""Exercise the actual pooled command and OS signal in an isolated process."""

import os
import selectors
import signal
import subprocess
import sys
import time

import pytest


@pytest.mark.parametrize('drain_signal', [signal.SIGUSR1, signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize('start_paused', [False, True])
def test_pooled_command_holds_and_flushes_after_real_signal(drain_signal: int, start_paused: bool) -> None:
    process = subprocess.Popen(
        [sys.executable, __file__, '--pooled-child', *(['--start-paused'] if start_paused else [])],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, 'DD_DOGSTATSD_DISABLE': 'true'},
    )
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 20
            ready = False
            activated = False
            while time.monotonic() < deadline and process.poll() is None:
                if selector.select(timeout=0.2):
                    chunk = os.read(process.stdout.fileno(), 65536)
                    output.extend(chunk)
                    assert len(output) <= 1048576, 'oversized synthetic command output'
                    if start_paused and not activated and b'HANDOFF_TEST_START_PAUSED' in output:
                        process.send_signal(signal.SIGUSR2)
                        activated = True
                    if b'HANDOFF_TEST_READY' in output:
                        ready = True
                        break
            assert ready, output[-4096:].decode(errors='replace')
        process.send_signal(drain_signal)
        tail, _ = process.communicate(timeout=20)
        output.extend(tail)
        assert process.returncode == 0, output[-4096:].decode(errors='replace')
        assert b'HANDOFF_TEST_DRAINED' in tail, output[-4096:].decode(errors='replace')
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def pooled_child() -> None:
    import json
    from types import SimpleNamespace
    from unittest.mock import patch

    import gevent
    from kafka import OffsetAndMetadata, TopicPartition
    from kafka.future import Future
    from osprey.worker.cli import sinks
    from osprey.worker.sinks.single_attempt_insert import InsertReceipt
    from osprey.worker.sinks.sink.clickhouse_output_sink import ClickHouseOutputSink
    from osprey.worker.sinks.sink.input_stream import KafkaInputStream
    from osprey.worker.sinks.sink.kafka_output_sink import KafkaOutputSink
    from osprey.worker.sinks.sink.output_sink import MultiOutputSink
    from osprey.worker.sinks.sink.tests.test_kafka_handoff import Consumer, record

    consumer = Consumer(record())
    consumer._subscription = SimpleNamespace(
        has_default_offset_reset_policy=lambda: False,
        all_consumed_offsets=lambda: {
            TopicPartition('actions', 0): OffsetAndMetadata(43, ''),
        },
    )
    commits = []

    def commit_async(offsets, callback):
        commits.append(offsets)
        callback(offsets, None)
        return Future().success(None)

    consumer.commit_async = commit_async
    stream = KafkaInputStream(consumer)
    inserts = []

    class Sender:
        def send(self, table, columns, rows, query_id):
            inserts.append((columns, rows))
            return InsertReceipt(query_id, 'confirmed')

    class Producer:
        def send(self, **kwargs):
            return Future().success(None)

        def flush(self, timeout):
            pass

    engine = SimpleNamespace(
        get_config_subkey=lambda cls: SimpleNamespace(get_action_config=lambda name: None),
        execution_graph=SimpleNamespace(
            validated_sources=SimpleNamespace(
                sources=SimpleNamespace(hash=lambda: 'synthetic-hash'),
            )
        ),
        execute=lambda helpers, action, **kwargs: SimpleNamespace(
            action=action,
            extracted_features_json=json.dumps({'SyntheticRule': True}),
            error_infos=[],
        ),
    )
    config = SimpleNamespace(
        get_str=lambda name, default: 'kafka' if name == 'OSPREY_INPUT_STREAM_SOURCE' else default,
        get_bool=lambda name, default: name == 'OSPREY_MIGRATION_HANDOFF'
        or (name == 'OSPREY_MIGRATION_START_PAUSED' and '--start-paused' in sys.argv),
        get_int=lambda name, default: default,
        expect_int=lambda name: 2,
    )
    outputs = MultiOutputSink(
        [
            ClickHouseOutputSink(SimpleNamespace(), batch_size=100),
            KafkaOutputSink('results', Producer()),
        ]
    )
    task = None

    def observe():
        if '--start-paused' in sys.argv:
            while stream._handoff is None or consumer.polls < 2:
                gevent.sleep(0.01)
            assert stream._handoff.snapshot()['state'] == 'paused'
            assert stream._handoff.snapshot()['accepted'] == 0
            assert not inserts and not commits
            print('HANDOFF_TEST_START_PAUSED', flush=True)
        while stream._handoff is None or stream._handoff.snapshot()['completed'] != 1:
            gevent.sleep(0.01)
        print('HANDOFF_TEST_READY', flush=True)
        while not stream._handoff.snapshot()['local_drained']:
            gevent.sleep(0.01)
        status = stream._handoff.snapshot()
        assert status['accepted'] == status['completed'] == 1
        assert status['active'] == status['pending_bytes'] == status['pending_deliveries'] == 0
        assert len(inserts) == 1 and len(inserts[0][1]) == 1
        assert len(commits) == 1
        assert not task.dead
        assert consumer.assigned <= consumer.paused
        print('HANDOFF_TEST_DRAINED', flush=True)
        os._exit(0)

    gevent.spawn_later(15, lambda: os._exit(9))
    with (
        patch.object(sinks, 'init_config', return_value=config),
        patch.object(sinks, 'bootstrap_engine_with_helpers', return_value=(engine, SimpleNamespace())),
        patch.object(sinks, 'get_rules_sink_input_stream', return_value=stream),
        patch.object(sinks, 'bootstrap_output_sinks', return_value=outputs),
        patch('osprey.worker.sinks.sink.clickhouse_output_sink.NativeInsertOnce', side_effect=lambda client: Sender()),
        patch(
            'osprey.worker.sinks.sink.rules_sink.get_envoy_check_server',
            return_value=SimpleNamespace(start=lambda: None, stop=lambda: None),
        ),
        patch.object(sinks, 'gevent_liveliness_watcher', side_effect=lambda: gevent.sleep(100)),
    ):
        task = gevent.spawn(sinks.run_rules_sink.callback, True, False, False)
        gevent.spawn(observe)
        gevent.wait()


if __name__ == '__main__' and '--pooled-child' in sys.argv:
    pooled_child()


def test_start_paused_without_maintenance_fails_before_engine_start() -> None:
    script = """
from types import SimpleNamespace
from unittest.mock import patch
from osprey.worker.cli import sinks
config = SimpleNamespace(get_bool=lambda name, default: name == 'OSPREY_MIGRATION_START_PAUSED')
with patch.object(sinks, 'init_config', return_value=config), patch.object(sinks, 'bootstrap_engine_with_helpers', side_effect=AssertionError('engine started before validation')):
    try:
        sinks.run_rules_sink.callback(False, False, False)
    except RuntimeError as error:
        assert str(error) == 'start-paused requires migration maintenance mode'
    else:
        raise AssertionError('invalid configuration consumed input')
"""
    result = subprocess.run(
        [sys.executable, '-c', script],
        capture_output=True,
        timeout=15,
        env={**os.environ, 'DD_DOGSTATSD_DISABLE': 'true'},
    )
    assert result.returncode == 0, result.stderr[-4096:].decode(errors='replace')
