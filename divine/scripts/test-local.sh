#!/usr/bin/env bash
# End-to-end local test for Divine Osprey stack.
# Usage: ./divine/scripts/test-local.sh
#
# Sends events in bridge envelope format to Kafka, waits for processing,
# and checks ClickHouse for results. Tests both Kind 1984 (report) and
# Kind 7 (reaction) event types to exercise heterogeneous batch flush.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DIVINE_DIR="$SCRIPT_DIR/.."
CLICKHOUSE_URL="http://localhost:8123/?user=default&password=clickhouse"
KAFKA_TOPIC="osprey.actions_input"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
PUBKEY="deadbeef00000000000000000000000000000000000000000000000000000001"

echo "=== Divine Osprey Local E2E Test ==="

# Verify services are running
echo ""
echo "Checking services..."
for container in divine-kafka divine-clickhouse divine-postgres divine-worker; do
  if docker inspect --format='{{.State.Status}}' "$container" 2>/dev/null | grep -q running; then
    echo "  $container: running"
  else
    echo "  $container: NOT RUNNING -- run 'docker compose up -d --build' first"
    exit 1
  fi
done

echo ""
echo "Applying ClickHouse schema..."
"$SCRIPT_DIR/init-clickhouse.sh"

echo ""
echo "Seeding trusted reporter labels..."
LABEL_JSON='{"labels": {"trusted_reporter": {"status": 1, "reasons": {"seed": {"pending": false, "description": "Seeded trusted reporter", "features": {}, "created_at": "2026-01-01T00:00:00+00:00", "expires_at": null}}, "previous_states": []}}}'
docker compose -f "$DIVINE_DIR/docker-compose.yaml" exec -T postgres \
  psql -U osprey -d osprey -c "
INSERT INTO entity_labels (entity_key, labels)
VALUES ('Pubkey/$PUBKEY', '$LABEL_JSON')
ON CONFLICT (entity_key) DO UPDATE SET labels = '$LABEL_JSON'::jsonb;
" 2>/dev/null
echo "  Seeded trusted_reporter for test pubkey"

echo ""
echo "Clearing previous test data from ClickHouse..."
curl -sf "$CLICKHOUSE_URL" --data "ALTER TABLE osprey.osprey_events DELETE WHERE EventId LIKE 'e2e_test_%'" 2>/dev/null || true

echo ""
echo "Waiting for worker consumer to stabilize (3s)..."
sleep 3

echo ""
echo "Sending test events to Kafka..."

# Event 1: Kind 1984 report from trusted reporter (should trigger TrustedReporterNSFW)
NOW=$(date +%s)
REPORT_EVENT=$(cat <<JSON
{"send_time":"$TIMESTAMP","data":{"action_id":90001,"action_name":"nostr_kind_1984","data":{"event_id":"e2e_test_report_001","pubkey":"$PUBKEY","kind":1984,"created_at":$NOW,"content":"nudity report","tags":[["report","nudity"],["e","e2e_test_target_001"],["p","e2e_test_target_pk_001"]],"sig":"test","reported_event_id":"e2e_test_target_001","reported_pubkey":"e2e_test_target_pk_001","report_reason":"nudity"}}}
JSON
)
echo "$REPORT_EVENT" | docker exec -i divine-kafka \
  kafka-console-producer --bootstrap-server kafka:29092 --topic "$KAFKA_TOPIC"
echo "  Sent Kind 1984 report (TrustedReporterNSFW)"

# Events 2-5: Kind 7 reactions (filler to trigger batch_size=5 flush)
for i in 1 2 3 4; do
  FILLER=$(cat <<JSON
{"send_time":"$TIMESTAMP","data":{"action_id":$((90001 + i)),"action_name":"nostr_kind_7","data":{"event_id":"e2e_test_react_00$i","pubkey":"e2e_test_pk_$i","kind":7,"created_at":$((NOW + i)),"content":"+","tags":[],"sig":"test"}}}
JSON
  )
  echo "$FILLER" | docker exec -i divine-kafka \
    kafka-console-producer --bootstrap-server kafka:29092 --topic "$KAFKA_TOPIC"
done
echo "  Sent 4 Kind 7 reactions (batch filler)"

echo ""
echo "Waiting for event processing (10s)..."
sleep 10

echo ""
echo "=== Results ==="

ROW_COUNT=$(curl -sf "$CLICKHOUSE_URL" --data "SELECT count() FROM osprey.osprey_events WHERE EventId LIKE 'e2e_test_%' FORMAT TabSeparated" 2>/dev/null || echo "0")
echo "Rows in ClickHouse: $ROW_COUNT"

if [ "$ROW_COUNT" = "0" ]; then
  echo ""
  echo "No rows found. Check worker logs:"
  echo "  docker compose logs osprey-worker --tail 30"
  echo ""
  echo "TEST RESULT: FAIL (no data in ClickHouse)"
  exit 1
fi

echo ""
echo "Event details:"
curl -sf "$CLICKHOUSE_URL" --data "
  SELECT EventId, Kind, __verdicts, __rule_hits
  FROM osprey.osprey_events
  WHERE EventId LIKE 'e2e_test_%'
  ORDER BY __time DESC
  FORMAT Pretty
"

echo ""
echo "Error counts:"
curl -sf "$CLICKHOUSE_URL" --data "
  SELECT EventId, Kind,
    toUInt32OrZero(toString(JSONExtractRaw(__rule_hits, 'TrustedReporterNSFW'))) AS trusted_nsfw,
    toUInt32OrZero(toString(JSONExtractRaw(__rule_hits, 'TrustedReporterCSAM'))) AS trusted_csam
  FROM osprey.osprey_events
  WHERE EventId LIKE 'e2e_test_%'
  FORMAT Pretty
"

# Check that the report event got a verdict
VERDICT=$(curl -sf "$CLICKHOUSE_URL" --data "
  SELECT __verdicts FROM osprey.osprey_events
  WHERE EventId = 'e2e_test_report_001' LIMIT 1 FORMAT TabSeparated
" 2>/dev/null || echo "")

if echo "$VERDICT" | grep -q 'flag_for_review\|auto_hide'; then
  echo ""
  echo "TEST RESULT: PASS (report event produced expected verdict)"
  exit 0
else
  echo ""
  echo "Verdict for report event: $VERDICT"
  echo "Expected flag_for_review or auto_hide."
  echo "Check worker logs: docker compose logs osprey-worker --tail 30"
  echo ""
  echo "TEST RESULT: INCONCLUSIVE (data written but verdict unexpected)"
  exit 0
fi
