# Trusted Reporter — Category Review (Child Safety, Harassment)
#
# Flags additional report categories from trusted reporters for HUMAN REVIEW.
# Unlike auto_hide.sml (CSAM/NSFW, which auto-hides), these only emit a
# flag_for_review verdict so the item surfaces in its dedicated Coop queue
# (Child Safety, Harassment) for a moderator to triage -- no automatic
# enforcement. Mirrors the existing TrustedReporterNSFW pattern in auto_hide.sml.
#
# Report reasons are normalized by the bridge (nostr-kafka-bridge/main.py):
#   divine-web 'sexual-content' / divine-mobile 'sexualContent' -> 'nudity', etc.
#   childSafety -> 'child_safety', harassment -> 'harassment'.
#
# WHY THIS EXISTS: before this rule, child_safety and harassment reports produced
# NO verdict, so COOPSink never posted them and the Child Safety / Harassment
# queues were never fed by the Osprey path.
#
# NOTE (gating): like the existing trusted-reporter rules, this only acts on
# reports from labelled trusted_reporter pubkeys. Reports of these categories from
# ordinary users do not yet surface via Osprey -- broadening to a first-report
# pattern (cf. multi_report_threshold.sml) is a policy decision.
#
# NOTE (underage_user / Age Review): underage-user reports are intentionally NOT
# handled here. They feed the relay-manager age-review case system (15-day clock,
# tiers) via ReportWatcher, and the full Coop/Osprey integration is a tracked lift
# (support-trust-safety/docs/moderation/coop-osprey-future-lifts-roadmap.md).
#
# NOTE (ClickHouse): each Rule name below becomes a boolean column in
# osprey.osprey_events (clickhouse-schema/001_osprey_events.sql). Adding a rule
# requires the matching ADD COLUMN -- see that file.

Import(
  rules=[
    'models/base.sml',
    'models/nostr/kind1984_report.sml',
  ]
)

TrustedReporterChildSafety = Rule(
  when_all=[
    Kind == 1984,
    HasLabel(entity=Pubkey, label='trusted_reporter'),
    ReportReason == 'child_safety',
  ],
  description='Trusted reporter flagged a child-safety concern',
)

TrustedReporterHarassment = Rule(
  when_all=[
    Kind == 1984,
    HasLabel(entity=Pubkey, label='trusted_reporter'),
    ReportReason == 'harassment',
  ],
  description='Trusted reporter flagged harassment',
)

WhenRules(
  rules_any=[TrustedReporterChildSafety, TrustedReporterHarassment],
  then=[
    DeclareVerdict(verdict='flag_for_review'),
  ],
)
