# Hold worker output for a planned database handoff

`run-rules-sink` has an opt-in maintenance mode. It closes Kafka admission,
finishes accepted classifications and required output, and remains alive while
reporting local accounting. This does not change rules, make remote effects
transactional, change steady-state Kafka commits, or authorize a database switch.

The default is off. Set `OSPREY_MIGRATION_HANDOFF=true` only in a reviewed,
measured maintenance configuration using Kafka input. The implementation requires
the pinned Kafka and ClickHouse client versions. It fails before intake when the
reviewed insert transport cannot be installed. Defaults are a 16 MiB pending
serialized-output budget and a 120-second drain observation deadline; configure
these with `OSPREY_MIGRATION_MAX_PENDING_BYTES` and
`OSPREY_MIGRATION_DRAIN_DEADLINE_SECONDS`. The deadline is an observation bound,
not a measured maintenance or rollback budget.

Send `SIGUSR1` to request a planned drain. In maintenance mode `SIGTERM` and
`SIGINT` request the same cooperative work. The signal callback only sets the
admission flag. Do not terminate or scale workers down to request a drain:
Kubernetes can still kill them after its grace period. Leave every worker alive
until the global handoff barrier has passed.

Records from an outstanding poll remain accepted even if admission closes before
the poll returns. The command accounts for their topic, partition and offset,
joins their handlers, distinguishes intentional skips from failures, and flushes
a partial ClickHouse batch. ClickHouse owns an immutable submitted batch; later
rows use a separate buffer. Maintenance inserts have a synchronous response and
an independent bounded transport, with both application and physical transport
replay disabled. Any unconfirmed insert retains its payload and blocks the
handoff. It is never automatically retried, dropped, or repaired by replaying
classification or sibling moderation effects.

Kafka output retains delivery payloads until their individual futures acknowledge
success. A producer flush alone cannot establish delivery. Its pending payloads
share the admission memory budget. Already accepted oversized output remains
owned and blocks further admission; arbitrary allocations inside classification
are outside this bound. Completed source records become counters and per-partition
boundaries rather than an ever-growing collection of action objects.

While held, assigned partitions are paused and polling continues to service group
membership. Assignment changes invalidate local drain accounting, including a
previously drained result. Final offset commit uses explicit accepted boundaries
and waits for its actual acknowledgement callback; a coordinator-lookup future is
insufficient. Commit failure blocks without automatic retry. Workers remain alive
after both success and failure. Use `OSPREY_MIGRATION_START_PAUSED=true` for
replacement workers that must not consume before the global gates authorize them.
There is no automatic resume from a blocked or drained process; use the reviewed
replacement sequence after verifying the barrier.

Private worker logs emit `osprey_migration_handoff` on state changes and at bounded
intervals. Inspect the image and command independently and collect every worker's
receipt. Require accepted = completed + intentional skips, zero failures, active
handlers, pending bytes/batches/deliveries, no blocked reasons, stable membership
and acknowledged offsets. A heartbeat, zero lag, elapsed time, a returned handler,
or a process exit cannot establish this. `local_drained` describes this process's
accounting only; independently reconcile raw-row multiplicity, aggregate effects,
privacy state and all other writers before fencing a database.

For an unknown insert, prove the request is terminal and no attempt remains
active, then reconcile the retained exact rows and dependent aggregate effects.
Query identifiers correlate evidence; they do not guarantee idempotency. Source
row presence alone does not establish materialized-view correctness. Missing
query-log or delivery evidence remains a blocker. This mechanism does not provide
recovery from arbitrary process loss or a durable journal.

Installing the patch does not retroactively certify old workers' buffered tails.
Preserve and independently reconcile the initial rollout while the existing
database remains authoritative. Before production use, run the isolated native
ClickHouse/Kafka and failure rehearsals, prove the initial transition's coverage,
and measure the complete forward and abort paths. Keep deployment endpoint,
credential, image and authority changes in their separately reviewed operational
configuration. This source feature does not perform them.
