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

## Key conventions

- SML rules live in `divine/rules/rules/` grouped by domain (reports, behavioral, content)
- Labels must be registered in `divine/rules/config/labels.yaml` with correct `valid_for` entity types or the worker crashes on startup
- `RelayManagerSink` handles enforcement (bans) and Kind 1985 label publishing
- CI builds images on push to `divine/*` branches
- Release tags use `divine-v<semver>` (e.g., `divine-v0.1.0`). The `divine-` prefix avoids collisions with upstream roostorg tags. Do NOT use plain `v*` tags. Pushing a `divine-v*` tag produces Docker image tags `<version>` and `<major>.<minor>`.
