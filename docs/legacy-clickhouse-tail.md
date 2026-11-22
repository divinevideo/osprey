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
- Supply rows in the order the worker pushed them to the sink, which must also
  be strictly increasing offset order. A worker running more than one rules-sink
  greenlet (`OSPREY_RULES_SINK_NUM_WORKERS` above 1) can push out of offset
  order, and its buffer is refused. Do not sort such a buffer to pass the check:
  row order decides the union's column order and which sample picks each
  missing-member default.
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
  Size is counted in UTF-8 bytes, for both the recorded input and the
  JSON-encoded prepared columns and rows (`serialized_size`). Prepared values
  must be encodable as UTF-8.

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

## One-time operation ownership

`osprey.worker.sinks.legacy_tail_attempt` adds an operations-only manifest codec
and submission guard. It is unwired from workers and has no command-line entrypoint.
The caller must first establish and retain the real admission fence, prove the
complete never-submitted per-worker tail and required privacy outcomes, verify
the current schema, and supply the verified authoritative client.

The encoder produces reviewable bytes containing the exact prepared payload,
legacy container identity and schema hash. The prepared payload uses the same
UTF-8 capacity check when preparing, encoding, and reading it back. The manifest
file itself has a separate 32 MiB limit and escapes non-ASCII characters, which
can take up to three times their UTF-8 size. A prepared tail made mostly of
non-ASCII text can therefore pass preparation and still be refused by the
encoder with `manifest capacity exceeded`, before any reservation. The encoder
preserves scalar types and float bits, including NULL, signed zero and
non-finite values, without executable deserialization. Store the manifest in a
private, owned POSIX directory on durable storage, with private regular files
and reliable exclusive creation, atomic replacement and file/directory `fsync`.
Retain its reviewed SHA256.
The schema hash binds that review; the submission guard does not query or
certify the live schema, authority or source boundaries.

Before constructing the Native sender, submission exclusively reserves one
`attempt-<legacy-container-id>.json` receipt and syncs both the receipt and its
directory. The receipt retains the entire original manifest and query ID.
Every existing receipt blocks another submission for that container, including
confirmed, unknown, `NOT_SENT`, incomplete or corrupt records. Changing the
manifest does not create a new attempt for the same container.

Confirmation releases the in-memory batch only after the outcome receipt is
durably replaced. An unknown insert, pre-transport failure or outcome-write
failure retains the batch and blocks another attempt. A process crash leaves
the reservation for independent settlement. There is no automatic replay,
re-arm, receipt deletion or reconciliation path.

Keep the same operations directory and truthful container identity across
restarts. A fresh directory or invented identity cannot prove that an insert
was never attempted. These are one-time handoff receipts, separate from service
ingestion. They neither deduplicate overlapping tails from different workers
nor establish completeness. A `None` result from partial-tail preparation needs
no submission; all other handoff gates still apply.

Tests use synthetic manifests, filesystem failures, competing owners and process
loss. Real Native request and aggregate behavior needs isolated integration
validation before operational use. No production submission is activated by
adding this module.
