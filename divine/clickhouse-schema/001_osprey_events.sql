-- Osprey events table for Divine's ClickHouse
-- Replaces Druid datasource for rule execution result storage + query UI

CREATE DATABASE IF NOT EXISTS osprey;

CREATE TABLE IF NOT EXISTS osprey.osprey_events
(
    `__time`       DateTime64(3, 'UTC'),
    `__action_id`  UInt64,
    `__verdicts`   String DEFAULT '',
    `__rule_hits`  String DEFAULT '',

    -- Common fields
    `EventType`    LowCardinality(String) DEFAULT '',
    `UserId`       String DEFAULT '',
    `Handle`       String DEFAULT '',
    `ActionName`   LowCardinality(String) DEFAULT '',

    -- Nostr event fields
    `EventId`              String DEFAULT '',
    `Pubkey`               String DEFAULT '',
    `Kind`                 Int32 DEFAULT 0,
    `CreatedAt`            Int64 DEFAULT 0,
    `Content`              String DEFAULT '',
    `Tags`                 String DEFAULT '[]',
    `NoteText`             String DEFAULT '',
    `MentionedPubkeys`     String DEFAULT '[]',
    `ReportedEventId`      String DEFAULT '',
    `ReportedEvent`        String DEFAULT '',
    `ReportedPubkey`       String DEFAULT '',
    `ReportReason`         String DEFAULT '',

    -- Kind 1985 label event fields
    `LabelNamespace`       LowCardinality(String) DEFAULT '',
    `LabelValue`           LowCardinality(String) DEFAULT '',
    `LabelSource`          LowCardinality(String) DEFAULT '',
    `LabelRejected`        UInt8 DEFAULT 0,
    `LabelMetadata`        String DEFAULT '',
    `LabelTargetEvent`     String DEFAULT '',
    `LabelContentHash`     String DEFAULT '',
    `LabelConfidence`      Float32 DEFAULT 0,
    `LabelSignerPubkey`    String DEFAULT '',
    `LabelTargetPubkey`    String DEFAULT '',
    `LabelTargetEventEntity` String DEFAULT '',

    -- Video event fields
    `VideoHash`            String DEFAULT '',
    `VideoUrl`             String DEFAULT '',
    `VideoTitle`           String DEFAULT '',

    -- Rule results (boolean features)
    `NewAccountSpam`       UInt8 DEFAULT 0,
    `RapidPosting`         UInt8 DEFAULT 0,
    `PreviouslyWarned`     UInt8 DEFAULT 0,
    `PreviouslySuspended`  UInt8 DEFAULT 0,
    `PermanentBan`         UInt8 DEFAULT 0,
    `TrustedReporterCSAM`  UInt8 DEFAULT 0,
    `TrustedReporterNSFW`  UInt8 DEFAULT 0,
    `TrustedReporterChildSafety` UInt8 DEFAULT 0,
    `TrustedReporterHarassment`  UInt8 DEFAULT 0,
    `ConfirmedNudity`      UInt8 DEFAULT 0,
    `ConfirmedViolence`    UInt8 DEFAULT 0,
    `ConfirmedCSAM`        UInt8 DEFAULT 0,
    `ConfirmedAIGenerated` UInt8 DEFAULT 0,
    `AgeRestricted`        UInt8 DEFAULT 0,
    `NeedsReview`          UInt8 DEFAULT 0,
    `ModerationServiceBan` UInt8 DEFAULT 0,
    `RejectedLabel`        UInt8 DEFAULT 0,
    `FirstSexualReport`    UInt8 DEFAULT 0,
    `FirstViolenceReport`  UInt8 DEFAULT 0,
    `ThresholdSexualReport` UInt8 DEFAULT 0,
    `ThresholdViolenceReport` UInt8 DEFAULT 0,
    `__entity_label_mutations` String DEFAULT '',
    `__ban_nostr_event`    String DEFAULT '',

    -- Catch-all for additional extracted features
    `_extra`       String DEFAULT '{}',

    INDEX idx_user_id UserId TYPE bloom_filter GRANULARITY 4,
    INDEX idx_event_type EventType TYPE set(100) GRANULARITY 4,
    INDEX idx_action_name ActionName TYPE set(100) GRANULARITY 4,
    INDEX idx_pubkey Pubkey TYPE bloom_filter GRANULARITY 4,
    INDEX idx_event_id EventId TYPE bloom_filter GRANULARITY 4,
    INDEX idx_kind Kind TYPE set(100) GRANULARITY 4,
    INDEX idx_verdicts __verdicts TYPE tokenbf_v1(256, 2, 0) GRANULARITY 4,
    INDEX idx_rule_hits __rule_hits TYPE tokenbf_v1(512, 2, 0) GRANULARITY 4
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(__time)
ORDER BY (__time, __action_id)
TTL toDateTime(__time) + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;

-- Upgrade DDL: add columns that may be missing on tables created from older schemas.
-- ALTER TABLE ... ADD COLUMN IF NOT EXISTS is idempotent on fresh and existing tables.
-- AFTER clauses omitted so these work regardless of which columns already exist.
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ReportedEvent` String DEFAULT '';
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `LabelSignerPubkey` String DEFAULT '';
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `LabelTargetPubkey` String DEFAULT '';
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `LabelTargetEventEntity` String DEFAULT '';
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `FirstSexualReport` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `FirstViolenceReport` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ThresholdSexualReport` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ThresholdViolenceReport` UInt8 DEFAULT 0;
-- New rule columns must be added here AND to the CREATE TABLE list above, or the
-- ClickHouse output sink fails the whole batch insert (it writes each rule as a
-- column). Coupled to divine/rules/rules/reports/trusted_reporter_categories.sml.
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `TrustedReporterChildSafety` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `TrustedReporterHarassment` UInt8 DEFAULT 0;

-- Materialized view for per-rule hit counts (powers the UI dashboard)
CREATE MATERIALIZED VIEW IF NOT EXISTS osprey.rule_hits_hourly
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(hour)
ORDER BY (hour, rule_name)
AS
SELECT
    hour,
    rule_name,
    count() AS hit_count
FROM (
    SELECT
        toStartOfHour(__time) AS hour,
        tupleElement(kv, 1) AS rule_name,
        tupleElement(kv, 2) AS hit
    FROM osprey.osprey_events
    ARRAY JOIN JSONExtractKeysAndValues(__rule_hits, 'Bool') AS kv
    WHERE hit = true
)
GROUP BY hour, rule_name;
