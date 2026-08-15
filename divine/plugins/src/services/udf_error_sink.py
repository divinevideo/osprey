"""Say which UDF threw, and why.

The executor catches an unexpected UDF exception, appends it to `error_infos`, marks the
node `Err(None)` and moves on. It does not log it and does not report it to Sentry
(`osprey/engine/executor/executor.py`, `_wrapped_execution`). The only trace is
`__error_count` on the result.

That is how staging reached a state where EVERY action carries `__error_count: 1` -- one UDF
throwing on every single action -- with nothing in four thousand lines of logs naming it. A
UDF that fails 100% of the time and one that never fails look identical from outside.

Deliberately a divine-side sink rather than a patch to `_wrapped_execution`: `error_infos`
is already on the ExecutionResult every sink receives, so this needs no change to upstream
and does not widen the fork (see the standing question about returning to stock images).

An unexpected exception is NOT an ExpectedUdfException. The engine raises those on purpose
for ordinary control flow, and the executor filters them out before counting. Everything
reaching here is a genuine fault.
"""

from typing import Any

from osprey.engine.executor.execution_context import ExecutionResult
from osprey.worker.lib.osprey_shared.logging import get_logger
from osprey.worker.sinks.sink.output_sink import BaseOutputSink

logger = get_logger(__name__)

# One line per error would drown a real incident and cost more than it tells. The identity
# of the failing UDF is the diagnostic; the hundredth instance of the same one is not.
_MAX_REPORTED_PER_ACTION = 10


class UdfErrorSink(BaseOutputSink):
    """Reports UDF exceptions the executor swallows."""

    def will_do_work(self, result: ExecutionResult) -> bool:
        # Silent on a clean action, or this becomes noise that hides the thing it adds.
        return bool(getattr(result, 'error_infos', None))

    def push(self, result: ExecutionResult) -> None:
        errors = getattr(result, 'error_infos', None) or []
        if not errors:
            return

        features: dict[str, Any] = getattr(result, 'extracted_features', None) or {}
        # Name the action so a failure can be correlated with the events that trigger it.
        # A UDF that throws only on kind 1985 is a different problem from one that always does.
        action = features.get('ActionName') or 'unknown_action'
        event_id = features.get('EventId') or ''

        for info in errors[:_MAX_REPORTED_PER_ACTION]:
            error = getattr(info, 'error', None)
            node = getattr(info, 'node', None)
            logger.error(
                'UDF error on %s%s: %s raised %s: %s',
                action,
                f' (event {event_id})' if event_id else '',
                node,
                type(error).__name__ if error is not None else 'UnknownError',
                error,
            )

        if len(errors) > _MAX_REPORTED_PER_ACTION:
            logger.error(
                'UDF error on %s: %d further errors not shown',
                action,
                len(errors) - _MAX_REPORTED_PER_ACTION,
            )
