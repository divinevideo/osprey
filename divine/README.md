# Divine Osprey Integration

Divine-specific adaptations to [Osprey](https://github.com/roostorg/osprey) for the Divine T&S stack.

## What's Changed

### ClickHouse replaces Druid

Divine runs ClickHouse on GKE (shared with Funnelcake). These adapters replace Osprey's Druid dependency:

| Original (Druid) | Divine (ClickHouse) |
|---|---|
| `KafkaOutputSink` → Kafka → Druid | `ClickHouseOutputSink` → ClickHouse directly |
| `ast_druid_translator.py` (JSON filters) | `ast_clickhouse_translator.py` (SQL WHERE) |
| `druid.py` (pydruid query builder) | `clickhouse.py` (SQL queries) |

### Files

```
divine/
  README.md                     ← this file
  clickhouse-schema/
    001_osprey_events.sql       ← ClickHouse table + materialized views

osprey_worker/src/osprey/
  worker/sinks/sink/
    clickhouse_output_sink.py   ← output sink (batch inserts)
  engine/query_language/
    ast_clickhouse_translator.py ← AST → SQL translator
  worker/ui_api/osprey/lib/
    clickhouse.py               ← query backend (timeseries, topN, scan, groupBy)
  worker/_stdlibplugin/
    sink_register_clickhouse.py ← sink registration with CH support
```

### Configuration

```env
# Enable ClickHouse output sink
OSPREY_CLICKHOUSE_OUTPUT_SINK=true
OSPREY_CLICKHOUSE_HOST=clickhouse.funnelcake.svc.cluster.local
OSPREY_CLICKHOUSE_PORT=8123
OSPREY_CLICKHOUSE_USER=default
OSPREY_CLICKHOUSE_PASSWORD=<from-secret>
OSPREY_CLICKHOUSE_DATABASE=osprey
OSPREY_CLICKHOUSE_TABLE=osprey_events
OSPREY_CLICKHOUSE_BATCH_SIZE=500
```

### Schema

Apply `divine/clickhouse-schema/001_osprey_events.sql` to your ClickHouse instance.
Follows the same pattern as `funnelcake-clickhouse-schema` in divine-iac-coreconfig.

## TODO

- [ ] Wire `clickhouse.py` into UI API views (replace `druid.py` imports)
- [ ] Divine-specific UDFs (NIP-86 enforcement, Sightengine, Nostr event parsing)
- [ ] Nostr event SML models (kind 0, 1, 7, 1984, video events)
- [ ] K8s manifests for divine-iac-coreconfig
- [ ] Funnelcake → Kafka bridge (publish Nostr events to Kafka topic)
- [ ] Integration tests with Divine's staging ClickHouse
