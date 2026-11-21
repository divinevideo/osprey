from types import SimpleNamespace
from typing import Any

from osprey.worker.sinks.sink.rules_sink import RulesRunner, SampleDecision


def runner_and_result(*, drop: bool = False, fail: bool = False) -> tuple[Any, Any, list[Any]]:
    output: list[Any] = []
    result = SimpleNamespace(action=SimpleNamespace(action_id=1))

    def execute(*args: Any, **kwargs: Any) -> Any:
        if fail:
            raise ValueError('synthetic classification failure')
        return result

    engine = SimpleNamespace(
        execute=execute,
        execution_graph=SimpleNamespace(
            validated_sources=SimpleNamespace(
                sources=SimpleNamespace(hash=lambda: 'synthetic-hash'),
            )
        ),
    )
    runner = RulesRunner(engine, SimpleNamespace(push=output.append), SimpleNamespace())
    runner._sampler = SimpleNamespace(sample=lambda action: SampleDecision(sample_rate=100, drop=drop))
    return runner, result, output


def test_successful_classification_reports_completion_after_output() -> None:
    runner, result, output = runner_and_result()
    dispositions = []
    actual = runner.classify_one(
        SimpleNamespace(action_id=1, action_name='synthetic'),
        'synthetic',
        report_disposition=dispositions.append,
    )
    assert actual is result
    assert output == [result]
    assert dispositions == ['completed']


def test_intentional_sampling_is_distinct_from_failed_classification() -> None:
    for drop, fail, expected in ((True, False, 'skipped'), (False, True, 'failed')):
        runner, result, output = runner_and_result(drop=drop, fail=fail)
        dispositions = []
        assert (
            runner.classify_one(
                SimpleNamespace(action_id=1, action_name='synthetic'),
                'synthetic',
                report_disposition=dispositions.append,
            )
            is None
        )
        assert not output
        assert dispositions == [expected]
