# Osprey (Divine Fork)

Event stream rules engine for Nostr moderation. SML rules evaluate Kind 1984 reports and emit enforcement verdicts.

## Cross-Repo Coordination

This repo implements **Layer 3** (Osprey enforcement) in the auto-hide evolution plan. Read the coordination doc at session start:
`~/code/support-trust-safety/docs/moderation/auto-hide-evolution-plan.md`

When you make decisions or discover constraints that affect other layers, update that doc and flag it for the user.

## Local Dev

```bash
uv sync
uv run pre-commit install
cd divine && docker compose up -d --build
./scripts/init-clickhouse.sh
./scripts/test-local.sh
```

## Divine-specific code

- `divine/plugins/src/` — UDFs and output sinks
- `divine/rules/` — SML models, rules, and config (labels.yaml)
- `divine/clickhouse-schema/` — analytics tables

## Configuration gotchas & doc discipline

Before changing report routing, rules, queues, or the bridge, re-verify the live
ecosystem state (it drifts); the coupling gotchas are written up in
`support-trust-safety/docs/moderation/coop-osprey-configuration-gotchas.md`, which is
worth keeping current as config changes. Key couplings:
- The report_reason chain must align across THREE places (bridge `_REASON_ALIASES`
  → an osprey rule that emits an actionable verdict → coop-setup routing). A queue with
  no rule stays empty — COOPSink only posts actionable verdicts. The canonical token
  vocabulary + per-token ownership (osprey-rule / relay-manager / default-queue) is the
  single source of truth in `divine/nostr-kafka-bridge/main.py:CANONICAL_REASONS`; the
  coupling tests in `test_main.py` parse the live `.sml` rules and fail if an
  `osprey-rule` token has no rule, a rule references an uncatalogued token, or an alias
  resolves off-vocabulary. coop-setup's route tokens are validated against this set on
  the COOP side (a subset guard), so the three places cannot silently drift.
- Each Rule name becomes a ClickHouse column; adding a rule needs `ADD COLUMN` in
  `divine/clickhouse-schema/001_osprey_events.sql` or the sink fails the whole batch.

## Key conventions

- SML rules live in `divine/rules/rules/` grouped by domain (reports, content)
- Labels must be registered in `divine/rules/config/labels.yaml` with correct `valid_for` entity types or the worker crashes on startup
- `RelayManagerSink` handles enforcement (bans) and Kind 1985 label publishing
- CI builds images on push to `divine/*` branches
- Release tags use `divine-v<semver>` for stable releases only (e.g., `divine-v0.1.0`). The `divine-` prefix avoids collisions with upstream roostorg tags. Do NOT use plain `v*` tags. Do NOT use pre-release tags — use `divine/*` branch builds for pre-release testing instead. Pushing a `divine-v*` tag produces Docker image tags `<version>` and `<major>.<minor>`.
