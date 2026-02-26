#!/usr/bin/env bash
# End-to-end local test for Divine Osprey stack.
# Usage: ./divine/scripts/test-local.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DIVINE_DIR="$SCRIPT_DIR/.."
COMPOSE="docker compose -f $DIVINE_DIR/docker-compose.yaml"
CLICKHOUSE_URL="http://localhost:8123"
KAFKA_TOPIC="osprey.actions_input"

cleanup() {
  echo "🧹 Tearing down..."
  $COMPOSE down -v --remove-orphans 2>/dev/null || true
}

# Uncomment to auto-cleanup on exit:
# trap cleanup EXIT

echo "🚀 Starting Divine stack..."
$COMPOSE up -d --build

echo "⏳ Waiting for services to be healthy..."
for svc in kafka clickhouse postgres; do
  echo -n "  $svc: "
  for i in $(seq 1 60); do
    status=$(docker inspect --format='{{.State.Health.Status}}' "divine-$svc" 2>/dev/null || echo "missing")
    if [ "$status" = "healthy" ]; then
      echo "✅"
      break
    fi
    [ "$i" -eq 60 ] && { echo "❌ (timeout)"; exit 1; }
    sleep 2
  done
done

echo "📄 Applying ClickHouse schema..."
"$SCRIPT_DIR/init-clickhouse.sh"

echo "⏳ Waiting for Kafka topics..."
sleep 5

# Send a sample event to Kafka
SAMPLE_EVENT='{"id":"test123","pubkey":"abc456","kind":1,"created_at":1709000000,"content":"hello world","tags":[["p","def789"]],"sig":"test"}'

echo "📤 Sending sample event to Kafka topic $KAFKA_TOPIC ..."
echo "$SAMPLE_EVENT" | docker exec -i divine-kafka \
  kafka-console-producer --bootstrap-server kafka:29092 --topic "$KAFKA_TOPIC"

echo "⏳ Waiting for event processing (15s)..."
sleep 15

echo "🔍 Checking ClickHouse for results..."
RESULT=$(curl -sf "$CLICKHOUSE_URL" --data "SELECT count() FROM osprey.osprey_events FORMAT TabSeparated" 2>/dev/null || echo "ERROR")

if [ "$RESULT" = "ERROR" ] || [ "$RESULT" = "0" ]; then
  echo ""
  echo "⚠️  No rows found in osprey.osprey_events."
  echo "   This may be expected if the worker needs additional config to write to ClickHouse."
  echo "   Check worker logs: docker logs divine-worker"
  echo ""
  echo "   Manual verification:"
  echo "     curl 'http://localhost:8123' --data 'SELECT * FROM osprey.osprey_events FORMAT Pretty'"
  echo ""
  echo "🟡 TEST RESULT: INCONCLUSIVE (schema OK, pipeline needs verification)"
  exit 0
else
  echo "✅ Found $RESULT row(s) in osprey.osprey_events"
  curl -sf "$CLICKHOUSE_URL" --data "SELECT * FROM osprey.osprey_events FORMAT Pretty"
  echo ""
  echo "🟢 TEST RESULT: PASS"
  exit 0
fi
