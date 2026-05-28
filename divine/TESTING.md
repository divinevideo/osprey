# Divine Osprey — Local Testing Guide

## Prerequisites

- Docker & Docker Compose v2+
- `curl` (for ClickHouse queries)

## Quick Start

```bash
cd divine
docker compose up -d --build
./scripts/init-clickhouse.sh
./scripts/test-local.sh
```

The stack is self-contained: Kafka, ClickHouse, Postgres, and all Osprey services run in Docker Compose. No Kind cluster required.

## Services

| Service | Local Port | Description |
|---------|-----------|-------------|
| Kafka | 9092 | Message broker (KRaft, no ZooKeeper) |
| ClickHouse | 8123 (HTTP), 9000 (native) | Event storage & analytics |
| Postgres | 5432 | Labels service metadata |
| etcd | 2379 | Coordination |
| Osprey Worker | 5011 | Rule execution engine |
| Osprey Coordinator | 5003 | Job coordination |
| Osprey UI | 5002 | Web dashboard |
| Nostr-Kafka Bridge | — | Streams relay events into Kafka |

## Sending Test Events

Events must be in the bridge envelope format:

```json
{
  "send_time": "2026-01-01T00:00:00+00:00",
  "data": {
    "action_id": 12345,
    "action_name": "nostr_kind_1984",
    "data": {
      "id": "event_hex_id",
      "pubkey": "hex_pubkey",
      "kind": 1984,
      "created_at": 1709000000,
      "content": "report content",
      "tags": [["report", "nudity"], ["e", "target_event_id"], ["p", "target_pubkey"]],
      "sig": "hex_sig",
      "reported_event_id": "target_event_id",
      "reported_pubkey": "target_pubkey",
      "report_reason": "nudity"
    }
  }
}
```

### Via Kafka directly

```bash
echo '{"send_time":"2026-01-01T00:00:00+00:00","data":{"action_id":1,"action_name":"nostr_kind_7","data":{"id":"test1","pubkey":"abc","kind":7,"created_at":1709000000,"content":"+","tags":[],"sig":"x"}}}' | \
  docker exec -i divine-kafka kafka-console-producer \
    --bootstrap-server kafka:29092 --topic osprey.actions_input
```

### Via the Nostr bridge

The bridge subscribes to the configured relay (`RELAY_URL`, default `ws://host.docker.internal:4444`) and converts raw Nostr events into the envelope format automatically.

## Checking Results in ClickHouse

```bash
# Count all events
curl 'http://localhost:8123/?user=default&password=clickhouse' --data 'SELECT count() FROM osprey.osprey_events'

# View recent events with verdicts
curl 'http://localhost:8123/?user=default&password=clickhouse' --data 'SELECT EventId, Kind, __verdicts, __rule_hits FROM osprey.osprey_events ORDER BY __time DESC LIMIT 10 FORMAT Pretty'

# Check rule hit stats
curl 'http://localhost:8123/?user=default&password=clickhouse' --data 'SELECT * FROM osprey.rule_hits_hourly ORDER BY hour DESC LIMIT 20 FORMAT Pretty'
```

## Seeding Entity Labels

Seed trusted reporters for auto-hide rules:

```bash
./scripts/seed-trusted-reporters.sh
```

Or manually via docker compose (format must match `EntityLabels.serialize()`):

```bash
docker compose exec postgres psql -U osprey -d osprey -c "
INSERT INTO entity_labels (entity_key, labels)
VALUES ('Pubkey/<hex_pubkey>',
  '{\"labels\": {\"trusted_reporter\": {\"status\": 1, \"reasons\": {\"seed\": {\"pending\": false, \"description\": \"\", \"features\": {}, \"created_at\": \"2026-01-01T00:00:00+00:00\", \"expires_at\": null}}, \"previous_states\": []}}}')
ON CONFLICT (entity_key) DO UPDATE SET labels = EXCLUDED.labels;
"
```

## Full Test Data

For comprehensive test data covering all rule types (requires `nak` CLI + local relay):

```bash
./scripts/seed-test-data.sh
```

## Teardown

```bash
cd divine
docker compose down -v --remove-orphans
```

## Troubleshooting

- **Worker not writing to ClickHouse?** Check logs: `docker logs divine-worker`
- **Kafka connection issues?** Ensure topics exist: `docker exec divine-kafka kafka-topics --bootstrap-server kafka:29092 --list`
- **ClickHouse schema errors?** Re-run: `./scripts/init-clickhouse.sh`
- **HasLabel errors?** Check entity_labels table: `docker compose exec postgres psql -U osprey -d osprey -c 'SELECT * FROM entity_labels'`
