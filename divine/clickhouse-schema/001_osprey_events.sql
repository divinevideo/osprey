-- Osprey events table for Divine's ClickHouse
-- Replaces Druid datasource for rule execution result storage + query UI

CREATE DATABASE IF NOT EXISTS osprey;

CREATE TABLE IF NOT EXISTS osprey.osprey_events
(
    `__time`       DateTime64(3, 'UTC'),
    `__action_id`  UInt64,
    `__verdicts`   String DEFAULT '',
    `__rule_hits`  String DEFAULT '',

    -- Dynamic features from rule execution are stored as JSON string columns.
    -- ClickHouse 23.1+ supports JSON type; for older versions, use String
    -- and extract with JSON functions at query time.
    --
    -- Common Nostr event fields (pre-defined for query performance):
    `EventType`    LowCardinality(String) DEFAULT '',
    `UserId`       String DEFAULT '',
    `Handle`       String DEFAULT '',
    `ActionName`   LowCardinality(String) DEFAULT '',

    -- Catch-all for additional extracted features
    `_extra`       String DEFAULT '{}',

    INDEX idx_user_id UserId TYPE bloom_filter GRANULARITY 4,
    INDEX idx_event_type EventType TYPE set(100) GRANULARITY 4,
    INDEX idx_action_name ActionName TYPE set(100) GRANULARITY 4,
    INDEX idx_verdicts __verdicts TYPE tokenbf_v1(256, 2, 0) GRANULARITY 4,
    INDEX idx_rule_hits __rule_hits TYPE tokenbf_v1(512, 2, 0) GRANULARITY 4
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(__time)
ORDER BY (__time, __action_id)
TTL __time + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;

-- Materialized view for per-rule hit counts (powers the UI dashboard)
CREATE MATERIALIZED VIEW IF NOT EXISTS osprey.rule_hits_hourly
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(hour)
ORDER BY (hour, rule_name)
AS
SELECT
    toStartOfHour(__time) AS hour,
    arrayJoin(JSONExtractKeysAndValues(__rule_hits, 'Bool')) AS kv,
    kv.1 AS rule_name,
    countIf(kv.2 = true) AS hit_count
FROM osprey.osprey_events
GROUP BY hour, rule_name;
