# Multi-Report Threshold Rules
#
# Escalates content after 2+ reports flag it for serious categories.
# First report labels the target and flags for review. Second report
# from a different event detects the label and escalates priority.
#
# Categories covered: nudity (sexual content), violence.
# CSAM is NOT here -- stays in ReportWatcher (threshold=1, single report).
#
# Bridge normalizes report reasons before they reach SML rules:
#   sexual-content, sexual, explicit, pornography, ns-nudity -> 'nudity'
#   violence, ns-violence -> 'violence'
#   See nostr-kafka-bridge/main.py _REASON_ALIASES for full mapping.
#
# No auto-enforcement from user reports. Reporter-supplied p-tags can
# name arbitrary pubkeys, so pubkey bans from this path would be unsafe.
# All threshold hits route to human review (COOP/Zendesk).
#
# Known limitation: labels track target event + category but not reporter
# identity, so the same reporter submitting two distinct report events
# can satisfy the threshold. A counter UDF keyed by (event, category,
# reporter) is the proper fix.

Import(
  rules=[
    'models/base.sml',
    'models/nostr/kind1984_report.sml',
  ]
)

# --- First report: label the target ---

FirstSexualReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'nudity',
    not HasLabel(entity=ReportedEventId, label='sexual_reported'),
    not HasLabel(entity=ReportedEventId, label='human_reviewed'),
  ],
  description='First report of sexual content on this event',
)

FirstViolenceReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'violence',
    not HasLabel(entity=ReportedEventId, label='violence_reported'),
    not HasLabel(entity=ReportedEventId, label='human_reviewed'),
  ],
  description='First report of violence on this event',
)

WhenRules(
  rules_any=[FirstSexualReport],
  then=[
    LabelAdd(entity=ReportedEventId, label='sexual_reported'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

WhenRules(
  rules_any=[FirstViolenceReport],
  then=[
    LabelAdd(entity=ReportedEventId, label='violence_reported'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

# --- Second report: escalate for review ---

ThresholdSexualReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'nudity',
    HasLabel(entity=ReportedEventId, label='sexual_reported'),
    not HasLabel(entity=ReportedEventId, label='human_reviewed'),
  ],
  description='Second+ report of sexual content, threshold met',
)

ThresholdViolenceReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'violence',
    HasLabel(entity=ReportedEventId, label='violence_reported'),
    not HasLabel(entity=ReportedEventId, label='human_reviewed'),
  ],
  description='Second+ report of violence, threshold met',
)

WhenRules(
  rules_any=[ThresholdSexualReport],
  then=[
    LabelAdd(entity=ReportedEventId, label='threshold_met'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

WhenRules(
  rules_any=[ThresholdViolenceReport],
  then=[
    LabelAdd(entity=ReportedEventId, label='threshold_met'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)
