# Offline legacy ClickHouse tail preparation

`osprey.worker.sinks.legacy_partial_tail.prepare_legacy_partial_tail` prepares the
payload that a legacy worker's final `stop()` would have flushed. It performs no
network or filesystem operations, starts no worker or engine, and has no insert
entrypoint. It is preparation for the initial transition to held-drain workers,
tracked in [the migration issue](https://github.com/divinevideo/divine-iac-coreconfig/issues/2615).

The transform is pinned to worker source
`51a742d089aa39d1e10fbde1e46a7d26b685e563`,
`osprey_worker/src/osprey/worker/sinks/sink/clickhouse_output_sink.py`:

- Supply ordered, recorded evaluations from exactly one worker and partition.
  Source coordinates remain attached; repeated action IDs are preserved.
- Fewer than 100 rows may form a partial buffer. A 100th row would have triggered
  the legacy automatic flush, so this helper refuses it.
- Preserve the original union of columns and the first present value's type.
  Missing members of included columns receive `0` or `''`. Columns absent from
  the entire buffer stay omitted and retain their server defaults, which may
  include `NULL`, `'{}'` and `'[]'`.
- Preserve original timestamps, internal effect fields and JSON serialization.
  Do not combine workers, split the buffer by month, remove expired rows, or
  split it into individual inserts: these operations can change stored values.
- Supply an independently verified schema column set. Unknown columns, malformed
  evaluations, mixed partitions, duplicate/reversed source offsets and excessive
  size refuse the whole preparation. There is no truncated success result.

Preparation does **not** prove that those evaluations form the complete buffer,
that it was never submitted, or that required moderation effects succeeded.
Offset gaps require separate evidence of their disposition. The caller still
needs a real admission fence, a positive last-flush boundary, continuous ownership,
complete input/evaluated multiplicity, exact raw values and aggregate accounting,
and authoritative privacy outcomes. Matching counts, sampled offsets and absent
rows are insufficient. An already attempted insert with an unknown outcome is
a separate reconciliation case.

No worker deployment or first submission is enabled by this helper. Future
execution must retain one immutable per-worker payload and its attempt ownership,
use the reviewed `NativeInsertOnce` transport, and retain/block on `UNKNOWN` or
`NOT_SENT`. Do not reconstruct labels from their lossy analytics strings or
replay classification and sibling outputs. No retry or re-arm is supplied here.

Unit tests cover the transform and refusal conditions. Comparing synthetic
payloads with the actual pinned legacy image can verify push/stop equivalence;
it does not establish live source coverage, schema compatibility, materialized
view effects or a safe runtime fence.
