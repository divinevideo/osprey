"""Local accounting for explicitly enabled maintenance; never a global cutover gate."""

from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from gevent.lock import Semaphore

Disposition = Literal['completed', 'skipped', 'failed']


@dataclass(frozen=True)
class SourcePosition:
    topic: str
    partition: int
    offset: int


class OutputHandoff:
    """Keep accepted work accounted for while admission is closed.

    Uncertainty is sticky. Completing an accepted handler does not resolve its
    output, and a deadline or membership change cannot cancel accepted work.
    Rows and immutable insert payloads remain owned by the output sink.
    """

    def __init__(self, *, enabled: bool = False, max_pending_bytes: int = 16 * 1024 * 1024):
        if max_pending_bytes <= 0:
            raise ValueError('positive output capacity required')
        self.enabled = enabled
        self.max_pending_bytes = max_pending_bytes
        self._lock = Semaphore()
        self._state = 'running'
        self._poll_active = False
        self._membership_poll = False
        self._accepted: dict[SourcePosition, None] = {}
        self._accepted_total = 0
        self._finished: Counter[Disposition] = Counter()
        self._last_offset: dict[tuple[str, int], int] = {}
        self._actions: dict[int, tuple[Any, SourcePosition]] = {}
        self._pending_bytes = 0
        self._pending_batches = 0
        self._pending_deliveries = 0
        self._delivery_bytes = 0
        self._reasons: set[str] = set()

    def _admission_open(self) -> bool:
        return not self.enabled or (
            self._state == 'running' and self._pending_bytes + self._delivery_bytes < self.max_pending_bytes
        )

    @property
    def admission_open(self) -> bool:
        with self._lock:
            return self._admission_open()

    def begin_poll(self) -> bool:
        with self._lock:
            if not self._admission_open() or self._poll_active:
                return False
            self._poll_active = True
            self._membership_poll = False
            return True

    def begin_membership_poll(self) -> bool:
        with self._lock:
            if self._poll_active:
                return False
            self._poll_active = True
            self._membership_poll = True
            return True

    def accept_poll_result(self, positions: list[SourcePosition]) -> None:
        with self._lock:
            if not self._poll_active:
                raise ValueError('no outstanding poll')
            self._poll_active = False
            if self._membership_poll and positions:
                self._block('invalid_source_record')
            self._membership_poll = False
            # A poll advances offsets before returning. Account for its entire
            # result even if drain was requested while that poll was running.
            for position in positions:
                key = (position.topic, position.partition)
                if position.offset <= self._last_offset.get(key, -1):
                    self._block('duplicate_source_position')
                    raise ValueError('source position already accepted')
                self._accepted[position] = None
                self._accepted_total += 1
                self._last_offset[key] = position.offset

    def bind_action(self, action: Any, position: SourcePosition) -> None:
        with self._lock:
            if position not in self._accepted or self._accepted[position] is not None or id(action) in self._actions:
                self._block('invalid_source_record')
                raise ValueError('action requires an outstanding accepted source')
            # Retaining the object prevents identity reuse until handler completion.
            self._actions[id(action)] = (action, position)

    def source_for_action(self, action: Any) -> SourcePosition | None:
        with self._lock:
            binding = self._actions.get(id(action))
            if binding is None or binding[0] is not action:
                self._block('invalid_source_record')
                return None
            return binding[1]

    def request_drain(self) -> bool:
        with self._lock:
            if not self.enabled or self._state == 'blocked':
                return False
            if self._state == 'running':
                self._state = 'draining'
            return True

    def _block(self, reason: str) -> None:
        if self.enabled:
            self._state = 'blocked'
            self._reasons.add(reason)

    def block(self, reason: str) -> None:
        allowed = {
            'insert_outcome_unknown',
            'insert_not_sent',
            'output_capacity_exceeded',
            'classification_failed',
            'required_output_failed',
            'kafka_delivery_failed',
            'membership_changed',
            'offset_commit_failed',
            'drain_deadline',
            'invalid_source_record',
            'transport_guard_unavailable',
            'duplicate_source_position',
        }
        if reason not in allowed:
            raise ValueError('unknown handoff failure category')
        with self._lock:
            self._block(reason)

    def finish(self, position: SourcePosition, disposition: Disposition) -> None:
        if disposition not in {'completed', 'skipped', 'failed'}:
            raise ValueError('unknown classification disposition')
        with self._lock:
            if position not in self._accepted or self._accepted[position] is not None:
                raise ValueError('action is not outstanding')
            del self._accepted[position]
            self._finished[disposition] += 1
            self._actions = {key: binding for key, binding in self._actions.items() if binding[1] != position}
            if disposition == 'failed':
                self._block('classification_failed')

    def set_pending_output(self, *, bytes_pending: int, batches_pending: int) -> None:
        if bytes_pending < 0 or batches_pending < 0:
            raise ValueError('negative pending output')
        with self._lock:
            self._pending_bytes = bytes_pending
            self._pending_batches = batches_pending
            if bytes_pending + self._delivery_bytes > self.max_pending_bytes:
                self._block('output_capacity_exceeded')
            elif self._state == 'drained' and (bytes_pending or batches_pending):
                self._block('required_output_failed')

    def set_pending_deliveries(self, count: int, *, bytes_pending: int = 0) -> None:
        if count < 0 or bytes_pending < 0 or (count == 0 and bytes_pending != 0):
            raise ValueError('negative pending deliveries')
        with self._lock:
            self._pending_deliveries = count
            self._delivery_bytes = bytes_pending
            if self._pending_bytes + bytes_pending > self.max_pending_bytes:
                self._block('output_capacity_exceeded')
            if self._state == 'drained' and count:
                self._block('required_output_failed')

    def membership_changed(self) -> None:
        self.block('membership_changed')

    def certify(self, *, committed: bool) -> bool:
        with self._lock:
            if (
                not self.enabled
                or self._state not in {'draining', 'drained'}
                or not committed
                or self._poll_active
                or any(value is None for value in self._accepted.values())
                or self._pending_bytes
                or self._pending_batches
                or self._pending_deliveries
                or self._reasons
            ):
                return False
            self._state = 'drained'
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            values = list(self._accepted.values())
            return {
                'enabled': self.enabled,
                'state': self._state,
                'local_drained': self.enabled and self._state == 'drained',
                'poll_active': self._poll_active,
                'accepted': self._accepted_total,
                'active': len(values),
                'completed': self._finished['completed'],
                'skipped': self._finished['skipped'],
                'failed': self._finished['failed'],
                'pending_bytes': self._pending_bytes + self._delivery_bytes,
                'pending_batches': self._pending_batches,
                'pending_deliveries': self._pending_deliveries,
                'blocked_reasons': sorted(self._reasons),
            }
