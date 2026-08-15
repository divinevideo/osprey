"""A UDF that throws must say which one, and why.

The executor catches an unexpected UDF exception, appends it to `error_infos`, sets the
node to Err(None), and NEVER logs it or reports it to Sentry. The count surfaces as
`__error_count` on the result; the exception itself goes nowhere.

On staging every action carries `__error_count: 1` -- one UDF throws on EVERY action -- and
its identity is undiscoverable from logs. That is the whole defect: not that a UDF fails,
but that a UDF failing 100% of the time is indistinguishable from one that never does.
"""

import sys
import types
from dataclasses import dataclass
from typing import Any


def _stub_osprey() -> None:
    """Stub the upstream imports so the sink can be exercised without the engine."""
    if 'osprey' in sys.modules:
        return
    for name in (
        'osprey',
        'osprey.worker',
        'osprey.worker.sinks',
        'osprey.worker.sinks.sink',
        'osprey.worker.sinks.sink.output_sink',
        'osprey.worker.lib',
        'osprey.worker.lib.osprey_shared',
        'osprey.worker.lib.osprey_shared.logging',
        'osprey.engine',
        'osprey.engine.executor',
        'osprey.engine.executor.execution_context',
    ):
        sys.modules.setdefault(name, types.ModuleType(name))

    class BaseOutputSink:
        pass

    sys.modules['osprey.worker.sinks.sink.output_sink'].BaseOutputSink = BaseOutputSink
    sys.modules['osprey.engine.executor.execution_context'].ExecutionResult = object

    captured: list[str] = []

    class _Logger:
        def error(self, msg: str, *a: Any, **k: Any) -> None:
            captured.append(msg % a if a else msg)

        def warning(self, msg: str, *a: Any, **k: Any) -> None:
            captured.append(msg % a if a else msg)

        def info(self, msg: str, *a: Any, **k: Any) -> None:
            pass

    _logger = _Logger()
    sys.modules['osprey.worker.lib.osprey_shared.logging'].get_logger = lambda *_a, **_k: _logger
    sys.modules['osprey.worker.lib.osprey_shared.logging'].CAPTURED = captured


_stub_osprey()
from osprey.worker.lib.osprey_shared.logging import CAPTURED  # noqa: E402
from services.udf_error_sink import UdfErrorSink  # noqa: E402


@dataclass
class _Node:
    """Stands in for the ASTNode the executor attaches to a failed call."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __str__(self) -> str:
        return self._name


@dataclass
class _ErrorInfo:
    error: BaseException
    node: Any


class _Result:
    def __init__(self, errors, features=None):
        self.error_infos = errors
        self.extracted_features = features or {'ActionName': 'nostr_kind_1984'}


def setup_function() -> None:
    CAPTURED.clear()


def test_a_thrown_udf_is_named_and_its_message_shown():
    """The point of the sink. Without it, `__error_count: 1` is all anyone sees."""
    sink = UdfErrorSink()
    result = _Result([_ErrorInfo(ValueError('funnelcake said no'), _Node('ResolveEventAuthor'))])
    assert sink.will_do_work(result) is True
    sink.push(result)
    joined = '\n'.join(CAPTURED)
    assert 'ResolveEventAuthor' in joined, f'must name the failing node; saw: {CAPTURED}'
    assert 'funnelcake said no' in joined, f'must carry the message; saw: {CAPTURED}'
    assert 'ValueError' in joined, f'must carry the exception type; saw: {CAPTURED}'


def test_the_action_is_named_so_the_failure_can_be_correlated():
    sink = UdfErrorSink()
    sink.push(_Result([_ErrorInfo(RuntimeError('boom'), _Node('CheckModerationResult'))]))
    assert 'nostr_kind_1984' in '\n'.join(CAPTURED), f'saw: {CAPTURED}'


def test_every_error_is_reported_not_just_the_first():
    """__error_count can exceed 1. Logging only the first would hide the rest."""
    sink = UdfErrorSink()
    sink.push(
        _Result(
            [
                _ErrorInfo(ValueError('first'), _Node('UdfOne')),
                _ErrorInfo(KeyError('second'), _Node('UdfTwo')),
            ]
        )
    )
    joined = '\n'.join(CAPTURED)
    assert 'UdfOne' in joined and 'UdfTwo' in joined, f'saw: {CAPTURED}'


def test_a_clean_result_logs_NOTHING():
    """The positive control. A sink that logs on every action would drown the signal it adds."""
    sink = UdfErrorSink()
    clean = _Result([])
    assert sink.will_do_work(clean) is False, 'must not do work when there are no errors'
    sink.push(clean)
    assert CAPTURED == [], f'a clean action must be silent; saw: {CAPTURED}'
