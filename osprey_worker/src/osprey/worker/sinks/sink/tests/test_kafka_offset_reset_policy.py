from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from kafka import KafkaConsumer
from osprey.worker.sinks import input_stream_chooser as chooser
from osprey.worker.sinks.output_handoff import OutputHandoff
from osprey.worker.sinks.sink.input_stream import KafkaInputStream
from osprey.worker.sinks.sink.tests.test_kafka_handoff import Consumer


@pytest.mark.parametrize('policy', ['latest', 'earliest', None])
def test_maintenance_refuses_automatic_or_unspecified_offset_reset(policy: str | None) -> None:
    consumer = Consumer()
    consumer.config['auto_offset_reset'] = policy
    stream = KafkaInputStream(consumer)
    handoff = OutputHandoff(enabled=True)
    with pytest.raises(ValueError, match='automatic offset reset'):
        stream.configure_handoff(handoff, start_paused=True)
    assert consumer.listener is None
    assert handoff.snapshot()['state'] == 'running'
    assert consumer._coordinator.config['enable_auto_commit'] is True


@pytest.mark.parametrize('policy', ['latest', 'earliest'])
def test_maintenance_checks_effective_subscription_not_mutated_config(policy: str) -> None:
    from kafka.consumer.subscription_state import SubscriptionState

    consumer = Consumer()
    consumer._subscription = SubscriptionState(policy)
    consumer.config['auto_offset_reset'] = 'none'
    assert consumer._subscription.has_default_offset_reset_policy()
    with pytest.raises(ValueError, match='automatic offset reset'):
        KafkaInputStream(consumer).configure_handoff(OutputHandoff(enabled=True), start_paused=True)
    assert consumer.listener is None


def test_disabled_maintenance_keeps_existing_reset_policy() -> None:
    consumer = Consumer()
    consumer.config['auto_offset_reset'] = 'latest'
    KafkaInputStream(consumer).configure_handoff(OutputHandoff(enabled=False))
    assert consumer.config['auto_offset_reset'] == 'latest'
    assert consumer.listener is None


@pytest.mark.parametrize('maintenance,expected', [(False, 'latest'), (True, 'none')])
def test_command_input_constructor_uses_reviewed_reset_policy(maintenance: bool, expected: str) -> None:
    config = SimpleNamespace(
        get_str=lambda key, default: {'OSPREY_KAFKA_INPUT_STREAM_TOPIC': 'synthetic-actions'}.get(key, default),
        get_optional_str=lambda key: {'OSPREY_KAFKA_GROUP_ID': 'synthetic-group'}.get(key),
        get_str_list=lambda key, default: ['127.0.0.1:19092'],
        get_bool=lambda key, default: maintenance if key == 'OSPREY_MIGRATION_HANDOFF' else default,
    )
    captured: dict[str, Any] = {}

    def create(*topics: str, **kwargs: Any) -> Any:
        captured.update(kwargs)
        # Resolve the actual pinned library's default, rather than requiring an
        # explicit kwarg for the unchanged ordinary command path.
        return SimpleNamespace(config={**KafkaConsumer.DEFAULT_CONFIG, **kwargs})

    with (
        patch.object(chooser.CONFIG, 'instance', return_value=config),
        patch.object(chooser, 'PatchedKafkaConsumer', side_effect=create),
    ):
        stream = chooser.get_rules_sink_input_stream(chooser.InputStreamSource.KAFKA)
    assert stream._consumer.config['auto_offset_reset'] == expected
    assert captured['group_id'] == 'synthetic-group'
