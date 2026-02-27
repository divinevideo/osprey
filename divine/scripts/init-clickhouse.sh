#!/usr/bin/env bash
# Apply ClickHouse schema to local dev instance.
# Usage: ./divine/scripts/init-clickhouse.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCHEMA_DIR="$SCRIPT_DIR/../clickhouse-schema"
CLICKHOUSE_HOST="${CLICKHOUSE_HOST:-localhost}"
CLICKHOUSE_PORT="${CLICKHOUSE_PORT:-8123}"

echo "⏳ Waiting for ClickHouse at $CLICKHOUSE_HOST:$CLICKHOUSE_PORT ..."
for i in $(seq 1 30); do
  if curl -sf "http://$CLICKHOUSE_HOST:$CLICKHOUSE_PORT/ping" >/dev/null 2>&1; then
    echo "✅ ClickHouse is ready"
    break
  fi
  [ "$i" -eq 30 ] && { echo "❌ ClickHouse not ready after 30s"; exit 1; }
  sleep 1
done

for sql_file in "$SCHEMA_DIR"/*.sql; do
  echo "📄 Applying $(basename "$sql_file") ..."
  curl -sf "http://$CLICKHOUSE_HOST:$CLICKHOUSE_PORT/" --data-binary @"$sql_file"
done

echo "✅ ClickHouse schema applied."
