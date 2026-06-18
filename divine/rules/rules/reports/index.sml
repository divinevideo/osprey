# Report handling rules for NIP-56 kind 1984 reports

Import(
  rules=[
    'models/base.sml',
    'models/nostr/kind1984_report.sml',
  ]
)

Require(rule='rules/reports/auto_hide.sml')
Require(rule='rules/reports/multi_report_threshold.sml')
Require(rule='rules/reports/moderation_service.sml')
Require(rule='rules/reports/first_report_review.sml')
