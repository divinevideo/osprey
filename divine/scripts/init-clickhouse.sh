#!/usr/bin/env bash
# Apply ClickHouse schema to local dev instance.
# Usage: ./divine/scripts/init-clickhouse.sh
#
# Environment variables:
#   CLICKHOUSE_HOST     (default: localhost)
#   CLICKHOUSE_PORT     (default: 8123)
#   CLICKHOUSE_USER     (default: empty)
#   CLICKHOUSE_PASSWORD (default: empty)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCHEMA_DIR="$SCRIPT_DIR/../clickhouse-schema"
CH_HOST="${CLICKHOUSE_HOST:-localhost}"
CH_PORT="${CLICKHOUSE_PORT:-8123}"
CH_USER="${CLICKHOUSE_USER:-default}"
CH_PASS="${CLICKHOUSE_PASSWORD:-clickhouse}"

echo "Waiting for ClickHouse at $CH_HOST:$CH_PORT ..."
for i in $(seq 1 30); do
  if curl -sf "http://$CH_HOST:$CH_PORT/ping" >/dev/null 2>&1; then
    echo "ClickHouse is ready"
    break
  fi
  [ "$i" -eq 30 ] && { echo "ClickHouse not ready after 30s"; exit 1; }
  sleep 1
done

python3 - "$SCHEMA_DIR" "$CH_HOST" "$CH_PORT" "$CH_USER" "$CH_PASS" <<'PYEOF'
import sys, os, re, urllib.request, glob

schema_dir, host, port, user, password = sys.argv[1:6]
base_url = f"http://{host}:{port}/"
params = ""
if user:
    params = f"?user={user}"
    if password:
        params += f"&password={password}"

for sql_file in sorted(glob.glob(os.path.join(schema_dir, "*.sql"))):
    print(f"Applying {os.path.basename(sql_file)} ...")
    sql = open(sql_file).read()
    sql = re.sub(r"--[^\n]*", "", sql)
    # Split on top-level semicolons (those followed by newline or EOF)
    stmts = [s.strip() for s in re.split(r";\s*\n", sql) if s.strip()]
    for i, stmt in enumerate(stmts, 1):
        stmt = stmt.rstrip(";").strip()
        if not stmt:
            continue
        try:
            req = urllib.request.Request(base_url + params, data=stmt.encode())
            urllib.request.urlopen(req)
            print(f"  Statement {i}: OK")
        except Exception as e:
            body = e.read().decode()[:200] if hasattr(e, "read") else str(e)
            print(f"  Statement {i}: {body}")
            print(f"    SQL: {stmt[:100]}...")

print("ClickHouse schema applied.")
PYEOF
