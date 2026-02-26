# Divine Osprey — Local Testing Guide

## Prerequisites

- Docker & Docker Compose v2+
- `gh` CLI (for cloning) or git
- `curl` (for ClickHouse queries)

## Quick Start

```bash
# Start the full stack
cd divine
docker compose up -d --build

# Apply ClickHouse schema
./scripts/init-clickhouse.sh

# Run the automated test
./scripts/test-local.sh
```

## Services

| Service | Local Port | Description |
|---------|-----------|-------------|
| Kafka | 9092 | Message broker (KRaft, no ZooKeeper) |
| ClickHouse | 8123 (HTTP), 9000 (native) | Event storage & analytics |
| Postgres | 5432 | Labels service metadata |
| etcd | 2379 | Coordination |
| Osprey Worker | 5001 | Rule execution engine |
| Osprey Coordinator | 5003 | Job coordination |
| Osprey UI | 5002 | Web dashboard |
| Nostr-Kafka Bridge | — | Streams relay events into Kafka |

## Sending Test Events

### Via Kafka directly

```bash
# Single event
echo '{"id":"test1","pubkey":"abc","kind":1,"created_at":1709000000,"content":"hello","tags":[],"sig":"x"}' | \
  docker exec -i divine-kafka kafka-console-producer \
    --bootstrap-server kafka:29092 --topic osprey.actions_input

# Batch from sample file
cat example_data/sample_events.jsonl | \
  docker exec -i divine-kafka kafka-console-producer \
    --bootstrap-server kafka:29092 --topic osprey.actions_input
```

### Via the Nostr bridge

The bridge automatically subscribes to the configured relay (`RELAY_URL`, default `wss://relay.divine.video`) and forwards all events to the `nostr-events` Kafka topic. To test with a different relay:

```bash
RELAY_URL=wss://relay.damus.io docker compose up -d nostr-kafka-bridge
```

## Checking Results in ClickHouse

```bash
# Count all events
curl 'http://localhost:8123' --data 'SELECT count() FROM osprey.osprey_events'

# View recent events
curl 'http://localhost:8123' --data 'SELECT * FROM osprey.osprey_events ORDER BY __time DESC LIMIT 10 FORMAT Pretty'

# Check rule hit stats
curl 'http://localhost:8123' --data 'SELECT * FROM osprey.rule_hits_hourly ORDER BY hour DESC LIMIT 20 FORMAT Pretty'

# Query by event type
curl 'http://localhost:8123' --data "SELECT * FROM osprey.osprey_events WHERE EventType = 'nostr_kind_1' FORMAT Pretty"
```

## Writing & Testing SML Rules

1. Add or edit rule files in `divine/rules/` (SML format)
2. Restart the worker to pick up changes:
   ```bash
   docker compose restart osprey-worker
   ```
3. Send a test event and check ClickHouse for the `__rule_hits` column to verify your rule fired

Rules are mounted at `/app/divine_rules` inside the worker container. The plugin path is `/app/divine_plugins/src`.

## Deploying to PoC Environment

The PoC environment runs on the Divine infrastructure. To deploy:

1. **Build & push images:**
   ```bash
   docker build -t divine-worker -f divine/Dockerfile.worker .
   docker build -t nostr-kafka-bridge -f divine/nostr-kafka-bridge/Dockerfile .
   # Tag and push to your registry
   ```

2. **Apply ClickHouse schema** to the PoC ClickHouse instance:
   ```bash
   CLICKHOUSE_HOST=<poc-clickhouse-host> ./divine/scripts/init-clickhouse.sh
   ```

3. **Configure environment variables** on the PoC worker to point to the PoC Kafka and ClickHouse instances.

## Teardown

```bash
cd divine
docker compose down -v --remove-orphans
```

## Troubleshooting

- **Worker not writing to ClickHouse?** Check logs: `docker logs divine-worker`
- **Kafka connection issues?** Ensure topics exist: `docker exec divine-kafka kafka-topics --bootstrap-server kafka:29092 --list`
- **ClickHouse schema errors?** Re-run: `./scripts/init-clickhouse.sh`
