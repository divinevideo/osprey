from types import SimpleNamespace

import gevent
from gevent.event import Event
from osprey.worker.sinks.handoff_supervisor import HandoffSupervisor
from osprey.worker.sinks.output_handoff import OutputHandoff, SourcePosition


def test_supervisor_joins_accepted_handlers_before_final_output_flush() -> None:
    handoff = OutputHandoff(enabled=True)
    position = SourcePosition('actions', 0, 42)
    assert handoff.begin_poll()
    handoff.accept_poll_result([position])
    handoff.request_drain()
    calls = []
    supervisor = HandoffSupervisor(
        handoff,
        SimpleNamespace(
            holding=True, request_handoff_commit=lambda: calls.append('commit'), handoff_offsets=lambda: []
        ),
        SimpleNamespace(stop=lambda: calls.append('flush')),
        emit=lambda status: None,
    )
    supervisor.tick()
    gevent.sleep(0)
    assert not calls
    handoff.finish(position, 'completed')
    supervisor.tick()
    gevent.sleep(0)
    supervisor.tick()
    assert calls == ['flush', 'commit']
    assert not handoff.snapshot()['local_drained']


def test_deadline_blocks_without_cancelling_ongoing_output() -> None:
    handoff = OutputHandoff(enabled=True)
    handoff.request_drain()
    clock = [0.0]
    release = Event()
    complete = Event()

    def stop() -> None:
        release.wait()
        complete.set()

    supervisor = HandoffSupervisor(
        handoff,
        SimpleNamespace(holding=True, request_handoff_commit=lambda: False, handoff_offsets=lambda: []),
        SimpleNamespace(stop=stop),
        deadline_seconds=2,
        clock=lambda: clock[0],
        emit=lambda status: None,
    )
    supervisor.tick()
    gevent.sleep(0)
    clock[0] = 3
    supervisor.tick()
    assert handoff.snapshot()['state'] == 'blocked'
    assert not complete.is_set()
    assert not supervisor._flush_task.dead
    release.set()
    gevent.sleep(0)
    assert complete.is_set()
    assert not handoff.snapshot()['local_drained']
