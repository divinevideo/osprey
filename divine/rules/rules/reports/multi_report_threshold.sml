# Multi-Report Threshold Escalation
#
# Auto-hides content after 2+ reports flag it for serious categories.
# First report labels the target event and flags for review. Second
# report detects the label, auto-hides the event, and marks threshold met.
#
# Categories covered: nudity (sexual content), violence.
# CSAM is NOT here -- stays in ReportWatcher (threshold=1, single report).
#
# Bridge normalizes report reasons before they reach SML rules:
#   sexual-content, sexual, explicit, pornography, ns-nudity -> 'nudity'
#   violence, ns-violence -> 'violence'
#   See nostr-kafka-bridge/main.py _REASON_ALIASES for full mapping.
#
# Auto-hide uses event-level ban only (no pubkey ban). Reporter-supplied
# p-tags can name arbitrary pubkeys, so pubkey enforcement from user
# reports is unsafe. Event-level hide is safe because the ReportedEventId
# is the actual reported event.
#
# P2: distinct-reporter deduplication. Labels track target event +
# category but not reporter identity, so the same reporter submitting
# two distinct report events can satisfy the threshold. Mitigated by
# the action being event-hide (reversible), not pubkey ban.
# Proper fix: counter UDF keyed by (event, category, reporter_pubkey).

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
    ReportedEvent != '',
    not HasLabel(entity=ReportedEventId, label='sexual_reported'),
    not HasLabel(entity=ReportedEventId, label='human_reviewed'),
  ],
  description='First report of sexual content on this event',
)

FirstViolenceReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'violence',
    ReportedEvent != '',
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
    ReportedEvent != '',
    HasLabel(entity=ReportedEventId, label='sexual_reported'),
    not HasLabel(entity=ReportedEventId, label='human_reviewed'),
  ],
  description='Second+ report of sexual content, threshold met',
)

ThresholdViolenceReport = Rule(
  when_all=[
    Kind == 1984,
    ReportReason == 'violence',
    ReportedEvent != '',
    HasLabel(entity=ReportedEventId, label='violence_reported'),
    not HasLabel(entity=ReportedEventId, label='human_reviewed'),
  ],
  description='Second+ report of violence, threshold met',
)

WhenRules(
  rules_any=[ThresholdSexualReport],
  then=[
    BanNostrEvent(event_id=ReportedEvent, pubkey='', reason='Multi-report threshold: nudity'),
    LabelAdd(entity=ReportedEventId, label='threshold_met'),
    LabelAdd(entity=ReportedEventId, label='auto_hidden'),
    DeclareVerdict(verdict='auto_hide'),
  ],
)

WhenRules(
  rules_any=[ThresholdViolenceReport],
  then=[
    BanNostrEvent(event_id=ReportedEvent, pubkey='', reason='Multi-report threshold: violence'),
    LabelAdd(entity=ReportedEventId, label='threshold_met'),
    LabelAdd(entity=ReportedEventId, label='auto_hidden'),
    DeclareVerdict(verdict='auto_hide'),
  ],
)
