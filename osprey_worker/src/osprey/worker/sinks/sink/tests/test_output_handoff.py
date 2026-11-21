from osprey.worker.sinks.output_handoff import OutputHandoff, SourcePosition


def test_disabled_handoff_cannot_change_normal_admission() -> None:
    handoff = OutputHandoff(enabled=False)
    assert not handoff.request_drain()
    handoff.block('insert_outcome_unknown')
    assert handoff.admission_open
    assert not handoff.snapshot()['local_drained']


def test_outstanding_poll_results_remain_accepted_after_drain_request() -> None:
    handoff = OutputHandoff(enabled=True)
    source = SourcePosition('actions', 0, 42)
    assert handoff.begin_poll()
    assert handoff.request_drain()
    handoff.accept_poll_result([source])
    assert handoff.snapshot()['accepted'] == 1
    assert handoff.snapshot()['active'] == 1
    assert not handoff.begin_poll()
    handoff.finish(source, 'completed')
    assert not handoff.snapshot()['local_drained']
    assert handoff.certify(committed=True)
    assert handoff.snapshot()['local_drained']


def test_unknown_insert_blocks_without_cancelling_accepted_action() -> None:
    handoff = OutputHandoff(enabled=True)
    source = SourcePosition('actions', 1, 7)
    assert handoff.begin_poll()
    handoff.accept_poll_result([source])
    handoff.block('insert_outcome_unknown')
    assert not handoff.admission_open
    assert handoff.snapshot()['active'] == 1
    handoff.finish(source, 'completed')
    assert handoff.snapshot()['active'] == 0
    assert not handoff.certify(committed=True)
    assert handoff.snapshot()['state'] == 'blocked'


def test_capacity_closes_intake_and_accounts_for_oversized_accepted_output() -> None:
    handoff = OutputHandoff(enabled=True, max_pending_bytes=10)
    handoff.set_pending_output(bytes_pending=12, batches_pending=1)
    assert not handoff.begin_poll()
    assert handoff.snapshot()['pending_bytes'] == 12
    assert handoff.snapshot()['pending_batches'] == 1
    assert not handoff.certify(committed=True)


def test_no_certificate_while_outputs_or_kafka_deliveries_are_pending() -> None:
    handoff = OutputHandoff(enabled=True)
    handoff.request_drain()
    handoff.set_pending_output(bytes_pending=1, batches_pending=1)
    assert not handoff.certify(committed=True)
    handoff.set_pending_output(bytes_pending=0, batches_pending=0)
    handoff.set_pending_deliveries(1)
    assert not handoff.certify(committed=True)
    handoff.set_pending_deliveries(0)
    assert not handoff.certify(committed=False)
    assert handoff.certify(committed=True)


def test_membership_change_invalidates_even_a_previously_drained_certificate() -> None:
    handoff = OutputHandoff(enabled=True)
    handoff.request_drain()
    assert handoff.certify(committed=True)
    handoff.membership_changed()
    assert not handoff.snapshot()['local_drained']
    assert not handoff.certify(committed=True)


def test_classification_failures_are_distinct_from_intentional_skips() -> None:
    handoff = OutputHandoff(enabled=True)
    positions = [SourcePosition('actions', 0, offset) for offset in (1, 2)]
    assert handoff.begin_poll()
    handoff.accept_poll_result(positions)
    handoff.finish(positions[0], 'skipped')
    handoff.finish(positions[1], 'failed')
    snapshot = handoff.snapshot()
    assert snapshot['accepted'] == 2
    assert snapshot['skipped'] == 1
    assert snapshot['failed'] == 1
    assert snapshot['completed'] == 0
    assert snapshot['active'] == 0
    assert not handoff.certify(committed=True)


def test_source_binding_uses_action_identity_not_reused_action_id() -> None:
    from types import SimpleNamespace

    handoff = OutputHandoff(enabled=True)
    positions = [SourcePosition('actions', 0, offset) for offset in (42, 43)]
    first = SimpleNamespace(action_id=7)
    second = SimpleNamespace(action_id=7)
    assert handoff.begin_poll()
    handoff.accept_poll_result(positions)
    handoff.bind_action(first, positions[0])
    handoff.bind_action(second, positions[1])
    assert handoff.source_for_action(first) == positions[0]
    assert handoff.source_for_action(second) == positions[1]
    handoff.finish(positions[0], 'completed')
    assert handoff.source_for_action(first) is None
    assert not handoff.admission_open  # A late/unaccounted output cannot certify a drain.
    assert handoff.source_for_action(second) == positions[1]


def test_unbound_output_is_retained_as_unresolved_not_certifiable() -> None:
    handoff = OutputHandoff(enabled=True)
    assert handoff.source_for_action(object()) is None
    handoff.request_drain()
    assert not handoff.certify(committed=True)
    assert 'invalid_source_record' in handoff.snapshot()['blocked_reasons']


def test_completed_source_records_do_not_accumulate_unbounded_action_state() -> None:
    handoff = OutputHandoff(enabled=True)
    for offset in range(1000):
        position = SourcePosition('actions', 0, offset)
        assert handoff.begin_poll()
        handoff.accept_poll_result([position])
        handoff.finish(position, 'completed')
    assert handoff.snapshot()['accepted'] == 1000
    assert handoff.snapshot()['completed'] == 1000
    assert not handoff._accepted
    assert handoff.begin_poll()
    try:
        handoff.accept_poll_result([SourcePosition('actions', 0, 999)])
    except ValueError:
        pass
    assert not handoff.admission_open
    assert 'duplicate_source_position' in handoff.snapshot()['blocked_reasons']
