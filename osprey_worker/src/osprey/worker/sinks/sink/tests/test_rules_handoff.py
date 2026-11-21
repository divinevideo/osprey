from types import SimpleNamespace
from typing import Any

from osprey.worker.sinks.sink.rules_sink import RulesRunner, SampleDecision


def runner_and_result(*, drop: bool = False, fail: bool = False) -> tuple[Any, Any, list[Any]]:
    output: list[Any] = []
    result = SimpleNamespace(action=SimpleNamespace(action_id=1), error_infos=[])

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


def test_real_partial_execution_error_preserves_output_but_blocks_handoff() -> None:
    exercise_real_execution_error(expected=False, receipt=True)


def test_expected_udf_outcome_is_not_an_unexpected_classification_failure() -> None:
    exercise_real_execution_error(expected=True, receipt=True)


def test_default_partial_execution_output_is_preserved_without_handoff() -> None:
    exercise_real_execution_error(expected=False, receipt=False)


def exercise_real_execution_error(*, expected: bool, receipt: bool) -> None:
    from datetime import datetime, timezone

    from osprey.engine.ast.sources import Sources
    from osprey.engine.executor.execution_context import Action, ExecutionContext, ExpectedUdfException
    from osprey.engine.executor.udf_execution_helpers import UDFHelpers
    from osprey.engine.udf.arguments import ArgumentsBase
    from osprey.engine.udf.base import UDFBase
    from osprey.engine.udf.registry import UDFRegistry
    from osprey.worker.adaptor.plugin_manager import bootstrap_ast_validators
    from osprey.worker.lib.osprey_engine import OspreyEngine
    from osprey.worker.lib.sources_provider import StaticSourcesProvider
    from osprey.worker.sinks.output_handoff import OutputHandoff, SourcePosition
    from osprey.worker.sinks.sink.input_stream import HandoffActionContext

    class SyntheticFailure(UDFBase[ArgumentsBase, int]):
        def execute(self, execution_context: ExecutionContext, arguments: ArgumentsBase) -> int:
            if expected:
                raise ExpectedUdfException('synthetic expected outcome')
            raise ValueError('synthetic unexpected execution error')

    bootstrap_ast_validators()
    engine = OspreyEngine(
        StaticSourcesProvider(Sources.from_dict({'main.sml': 'A = SyntheticFailure()\nB = 2 + 3'})),
        UDFRegistry.with_udfs(SyntheticFailure),
    )
    output = []
    runner = RulesRunner(engine, SimpleNamespace(push=output.append), UDFHelpers())
    action = Action(action_id=1, action_name='synthetic', data={}, timestamp=datetime.now(timezone.utc))
    if receipt:
        handoff = OutputHandoff(enabled=True)
        source = SourcePosition('synthetic-actions', 0, 0)
        assert handoff.begin_poll()
        handoff.accept_poll_result([source])
        handoff.bind_action(action, source)
        context = HandoffActionContext(action, handoff, source)
        with context as accepted:
            result = runner.classify_one(accepted, 'synthetic', report_disposition=context.set_disposition)
        handoff.request_drain()
        state = handoff.snapshot()
        assert state['accepted'] == 1
        assert state['failed'] == (0 if expected else 1)
        assert state['completed'] == (1 if expected else 0)
        assert handoff.certify(committed=True) is expected
    else:
        result = runner.classify_one(action, 'synthetic')
    assert result is not None
    assert output == [result]
    assert result.extracted_features['A'] is None
    assert result.extracted_features['B'] == 5
    assert len(result.error_infos) == (0 if expected else 1)
