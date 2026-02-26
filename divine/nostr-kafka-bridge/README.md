# Nostr-Kafka Bridge

Lightweight service that subscribes to a Nostr relay via WebSocket and publishes incoming events to a Kafka topic.

## Configuration

| Env var | Default | Description |
|---|---|---|
| `RELAY_URL` | `wss://relay.divine.video` | Nostr relay WebSocket URL |
| `KAFKA_BOOTSTRAP_SERVERS` | `kafka:9092` | Comma-separated Kafka brokers |
| `KAFKA_TOPIC` | `nostr-events` | Kafka topic for events |
| `HEALTH_PORT` | `8080` | HTTP health check port |

## Run

```bash
docker build -t nostr-kafka-bridge .
docker run -e KAFKA_BOOTSTRAP_SERVERS=kafka:9092 nostr-kafka-bridge
```

## Health Check

`GET :8080/` → `200 ok` when connected, `503 disconnected` otherwise.

## How It Works

1. Connects to the Nostr relay and sends a `REQ` subscription for all events
2. For each `EVENT` message, publishes the event JSON to the configured Kafka topic
3. On disconnect, reconnects with exponential backoff (1s → 60s max)
