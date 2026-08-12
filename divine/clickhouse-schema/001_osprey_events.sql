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
    `ReportedAuthorPubkey` String DEFAULT '',
    `ReportReason`         String DEFAULT '',
    -- Distinct-reporter dedup. These are plain extracted features rather than
    -- rule hits, and they need a column for the same reason: an unrecognised
    -- name makes the sink reject the whole batch. They were in the upgrade DDL
    -- below but not here, so a database created fresh from this file would have
    -- failed every insert while an upgraded one worked.
    `ReporterPubkeyStr`    String DEFAULT '',
    `EventReporterId`      String DEFAULT '',

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
    -- Hash-keyed entity shared by the label path and the video path, so a human's
    -- decision on media with no event target can be recorded and read back.
    `LabelContentHashEntity` String DEFAULT '',
    `VideoHashEntity`        String DEFAULT '',
    `LabelTargetAuthorPubkey` String DEFAULT '',

    -- Video event fields
    `VideoHash`            String DEFAULT '',
    `VideoUrl`             String DEFAULT '',
    `VideoTitle`           String DEFAULT '',

    -- Self-hosted detector evidence
    `DetectorContentHash`   String DEFAULT '',
    `DetectorVideoUrl`      String DEFAULT '',
    `DetectorSignal`        LowCardinality(String) DEFAULT '',
    `DetectorClass`         LowCardinality(String) DEFAULT '',
    `DetectorConfidence`    Float32 DEFAULT 0,
    `DetectorFramesFlagged` Int32 DEFAULT 0,
    `DetectorTotalFrames`   Int32 DEFAULT 0,
    `DetectorModel`         String DEFAULT '',
    `DetectorDisposition`   LowCardinality(String) DEFAULT '',

    -- Rule results (boolean features)
    `NewAccountSpam`       UInt8 DEFAULT 0,
    `RapidPosting`         UInt8 DEFAULT 0,
    `PreviouslyWarned`     UInt8 DEFAULT 0,
    `PreviouslySuspended`  UInt8 DEFAULT 0,
    `PermanentBan`         UInt8 DEFAULT 0,
    `TrustedReporterCSAM`  UInt8 DEFAULT 0,
    `TrustedReporterNSFW`  UInt8 DEFAULT 0,
    `FirstChildSafetyReport` UInt8 DEFAULT 0,
    `FirstHarassmentReport`  UInt8 DEFAULT 0,
    -- Label routing. Each family has three target shapes (target present, null
    -- target, empty target) and EVERY rule name needs a column here.
    --
    -- A rule that does not match still emits `false` rather than nothing, so it
    -- needs a column whether or not it ever fires. Whether it emits `false` or
    -- `null` depends on the syntactic form of its conditions, and `null` is
    -- skipped by the sink, so not every rule breaks every insert. But the sink
    -- unions columns across the whole buffer into ONE insert, so a single action
    -- carrying an unrecognised name discards every unrelated row batched with it.
    -- Provision a column for every rule and the distinction never has to be made.
    `ConfirmedNudity`      UInt8 DEFAULT 0,
    `ConfirmedNudityHashOnlyNullTarget`  UInt8 DEFAULT 0,
    `ConfirmedNudityHashOnlyEmptyTarget` UInt8 DEFAULT 0,
    `ConfirmedViolence`    UInt8 DEFAULT 0,
    `ConfirmedViolenceHashOnlyNullTarget`  UInt8 DEFAULT 0,
    `ConfirmedViolenceHashOnlyEmptyTarget` UInt8 DEFAULT 0,
    `ConfirmedAgeRestrictNoValidHash`            UInt8 DEFAULT 0,
    `ConfirmedAgeRestrictNoValidHashNullTarget`  UInt8 DEFAULT 0,
    `ConfirmedAgeRestrictNoValidHashEmptyTarget` UInt8 DEFAULT 0,
    `ConfirmedCSAM`        UInt8 DEFAULT 0,
    `ConfirmedCSAMHashOnlyNullTarget`  UInt8 DEFAULT 0,
    `ConfirmedCSAMHashOnlyEmptyTarget` UInt8 DEFAULT 0,
    `ConfirmedAIGenerated` UInt8 DEFAULT 0,
    `ConfirmedAIGeneratedNullTarget`  UInt8 DEFAULT 0,
    `ConfirmedAIGeneratedEmptyTarget` UInt8 DEFAULT 0,
    `AgeRestricted`        UInt8 DEFAULT 0,
    `NeedsReview`          UInt8 DEFAULT 0,
    `ModerationServiceBan` UInt8 DEFAULT 0,
    `RejectedLabel`        UInt8 DEFAULT 0,
    `RejectedLabelNullTarget`  UInt8 DEFAULT 0,
    `RejectedLabelEmptyTarget` UInt8 DEFAULT 0,
    `FirstSexualReport`    UInt8 DEFAULT 0,
    `FirstViolenceReport`  UInt8 DEFAULT 0,
    `ThresholdSexualReport` UInt8 DEFAULT 0,
    `ThresholdViolenceReport` UInt8 DEFAULT 0,
    `DetectorNsfwEvidence` UInt8 DEFAULT 0,
    `__entity_label_mutations` String DEFAULT '',
    `__ban_nostr_event`    String DEFAULT '',
    -- Mirrors __ban_nostr_event for the age-restrict effect, so the enforcement
    -- that actually fired is queryable rather than falling into _extra.
    `__age_restrict_nostr_event` String DEFAULT '',

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
-- Authoritative authors resolved from the reported/labelled event. Every
-- extracted feature needs a column here or the sink drops the whole batch.
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ReportedAuthorPubkey` String DEFAULT '';
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `LabelTargetAuthorPubkey` String DEFAULT '';
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `FirstSexualReport` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `FirstViolenceReport` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ThresholdSexualReport` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ThresholdViolenceReport` UInt8 DEFAULT 0;
-- Hash-only and no-valid-hash label branches. Rule hits are emitted on EVERY
-- action, not only when the rule matches, so a rule without a column fails
-- every insert rather than an occasional batch.
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedNudityHashOnlyNullTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedNudityHashOnlyEmptyTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedViolenceHashOnlyNullTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedViolenceHashOnlyEmptyTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedCSAMHashOnlyNullTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedCSAMHashOnlyEmptyTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedAgeRestrictNoValidHash` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedAgeRestrictNoValidHashNullTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedAgeRestrictNoValidHashEmptyTarget` UInt8 DEFAULT 0;
-- New rule columns must be added here AND to the CREATE TABLE list above, or the
-- ClickHouse output sink fails the whole batch insert (it writes each rule as a
-- column). Coupled to divine/rules/rules/reports/first_report_review.sml.
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `FirstChildSafetyReport` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `FirstHarassmentReport` UInt8 DEFAULT 0;
-- Coupled to models/ai_detector_nsfw.sml and its review-only rule. Every
-- extracted feature needs a column or ClickHouse rejects the whole batch.
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ReporterPubkeyStr` String DEFAULT '';
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `EventReporterId` String DEFAULT '';
-- Hash-keyed entity for human decisions on media with no event target.
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `LabelContentHashEntity` String DEFAULT '';
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `VideoHashEntity` String DEFAULT '';
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `DetectorContentHash` String DEFAULT '';
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `DetectorVideoUrl` String DEFAULT '';
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `DetectorSignal` LowCardinality(String) DEFAULT '';
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `DetectorClass` LowCardinality(String) DEFAULT '';
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `DetectorConfidence` Float32 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `DetectorFramesFlagged` Int32 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `DetectorTotalFrames` Int32 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `DetectorModel` String DEFAULT '';
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `DetectorDisposition` LowCardinality(String) DEFAULT '';
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `DetectorNsfwEvidence` UInt8 DEFAULT 0;
-- Coupled to divine/rules/rules/content/label_routing.sml. Each label family has
-- three target shapes and every one is a rule, so every one is a column. These
-- were missing: the hash-only variants shipped without columns. A rule that does
-- not match still emits `false`, so it needs a column whether or not it ever
-- fires, and one unrecognised name discards every unrelated row in the same
-- batch, because the sink unions columns across the buffer into one insert.
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedNudityHashOnlyNullTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedNudityHashOnlyEmptyTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedViolenceHashOnlyNullTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedViolenceHashOnlyEmptyTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedAgeRestrictNoValidHash` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedAgeRestrictNoValidHashNullTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedAgeRestrictNoValidHashEmptyTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedCSAMHashOnlyNullTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedCSAMHashOnlyEmptyTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedAIGeneratedNullTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedAIGeneratedEmptyTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `RejectedLabelNullTarget` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `RejectedLabelEmptyTarget` UInt8 DEFAULT 0;
-- Effect feature columns. CREATE TABLE IF NOT EXISTS is a no-op on every already-
-- deployed table, so a column that only appears in CREATE never lands on staging
-- or production. The sink then rejects the whole batch on the first effect
-- insert (unrecognised column) and empties telemetry — the failure class both
-- schema halves exist to prevent. Coupled to each effect's feature_name and
-- ClickHouseOutputSink._PASSTHROUGH_INTERNAL_KEYS.
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `__ban_nostr_event` String DEFAULT '';
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `__age_restrict_nostr_event` String DEFAULT '';

-- Every rule name belongs in BOTH halves, and the coupling test enforces it.
-- Which half a column needs used to depend on when it was introduced, and that
-- judgement is what produced a deployed table 19 columns behind the workers on
-- 2026-08-12. ADD COLUMN IF NOT EXISTS is a no-op where the column already
-- exists, so stating every rule here costs nothing and removes the question.
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `AgeRestricted` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedAIGenerated` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedCSAM` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedNudity` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ConfirmedViolence` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `ModerationServiceBan` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `NeedsReview` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `NewAccountSpam` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `PermanentBan` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `PreviouslySuspended` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `PreviouslyWarned` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `RapidPosting` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `RejectedLabel` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `TrustedReporterCSAM` UInt8 DEFAULT 0;
ALTER TABLE osprey.osprey_events ADD COLUMN IF NOT EXISTS `TrustedReporterNSFW` UInt8 DEFAULT 0;

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
