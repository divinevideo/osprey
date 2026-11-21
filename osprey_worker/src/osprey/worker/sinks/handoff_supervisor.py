"""Observe an opt-in local drain without terminating or replaying accepted work."""

import json
import time
from typing import Any, Callable

import gevent
from osprey.worker.sinks.output_handoff import OutputHandoff


class HandoffSupervisor:
    def __init__(
        self,
        handoff: OutputHandoff,
        input_stream: Any,
        output_sink: Any,
        *,
        deadline_seconds: float = 120,
        clock: Callable[[], float] = time.monotonic,
        emit: Callable[[dict[str, Any]], None] | None = None,
    ):
        if not handoff.enabled or not 0 < deadline_seconds <= 3600:
            raise ValueError('bounded enabled maintenance required')
        self._handoff = handoff
        self._input = input_stream
        self._output = output_sink
        self._deadline = deadline_seconds
        self._clock = clock
        self._emit = emit or (lambda status: print(json.dumps(status), flush=True))
        self._started: float | None = None
        self._flush_task: Any = None
        self._last_emitted_state: str | None = None
        self._last_emit = float('-inf')

    def _flush(self) -> None:
        try:
            self._output.stop()
        except BaseException:
            # Raw child exceptions may contain operational data. Keep every
            # worker alive and report only the controlled failure category.
            self._handoff.block('required_output_failed')

    def tick(self) -> None:
        now = self._clock()
        state = self._handoff.snapshot()
        if state['state'] not in {'running', 'paused'}:
            if self._started is None:
                self._started = now
            if not state['local_drained'] and now - self._started >= self._deadline:
                self._handoff.block('drain_deadline')
            if not state['active'] and self._input.holding:
                if self._flush_task is None:
                    self._flush_task = gevent.spawn(self._flush)
                elif self._flush_task.ready():
                    self._input.request_handoff_commit()
        status = self._handoff.snapshot()
        if status['state'] != self._last_emitted_state or now - self._last_emit >= 15:
            self._emit(
                {
                    'event': 'osprey_migration_handoff',
                    'local': status,
                    'offsets': self._input.handoff_offsets(),
                    'startup': self._input.handoff_startup_status(),
                    'acceptance': 'local_accounting_only',
                }
            )
            self._last_emitted_state = status['state']
            self._last_emit = now

    def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                self._handoff.block('required_output_failed')
            # The observation deadline never kills handlers, the flusher or
            # membership polling. Operators must verify the global barrier.
            gevent.sleep(0.1)
