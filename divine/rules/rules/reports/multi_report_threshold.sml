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
# Distinct-reporter dedup: RESOLVED for threshold=2. Every rule below is
# guarded on `reporter_counted` against EventReporterId, the
# (event, category, reporter) composite entity defined in
# models/nostr/kind1984_report.sml, and every acting branch sets it. So the same
# reporter submitting two report events cannot satisfy the threshold on their
# own.
#
# The category has to be in that key, because the single `reporter_counted`
# label guards all four rules below at once. Keyed on (event, reporter) alone, a
# reporter's first report blocks their own later report of the same event under
# a different category: it matches neither the first-report rule nor the
# threshold rule, so it is silently dropped. Case D of
# local-stubs/validate-distinct-reporter.sh is the regression test.
#
# Still NOT a count. The threshold is fixed at 2 by the shape of these rules;
# going higher needs a chain of labels that does not scale. A real count needs
# the funnelcake accessor in the PRD's C2, where `uniq(reporter_pubkey)` already
# exists but is not reachable from a rule.

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
    not HasLabel(entity=EventReporterId, label='reporter_counted'),
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
    not HasLabel(entity=EventReporterId, label='reporter_counted'),
  ],
  description='First report of violence on this event',
)

WhenRules(
  rules_any=[FirstSexualReport],
  then=[
    LabelAdd(entity=EventReporterId, label='reporter_counted'),
    LabelAdd(entity=ReportedEventId, label='sexual_reported'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

WhenRules(
  rules_any=[FirstViolenceReport],
  then=[
    LabelAdd(entity=EventReporterId, label='reporter_counted'),
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
    not HasLabel(entity=EventReporterId, label='reporter_counted'),
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
    not HasLabel(entity=EventReporterId, label='reporter_counted'),
  ],
  description='Second+ report of violence, threshold met',
)

WhenRules(
  rules_any=[ThresholdSexualReport],
  then=[
    BanNostrEvent(event_id=ReportedEvent, pubkey='', reason='Multi-report threshold: nudity'),
    LabelAdd(entity=EventReporterId, label='reporter_counted'),
    LabelAdd(entity=ReportedEventId, label='threshold_met'),
    LabelAdd(entity=ReportedEventId, label='auto_hidden'),
    DeclareVerdict(verdict='auto_hide'),
  ],
)

WhenRules(
  rules_any=[ThresholdViolenceReport],
  then=[
    BanNostrEvent(event_id=ReportedEvent, pubkey='', reason='Multi-report threshold: violence'),
    LabelAdd(entity=EventReporterId, label='reporter_counted'),
    LabelAdd(entity=ReportedEventId, label='threshold_met'),
    LabelAdd(entity=ReportedEventId, label='auto_hidden'),
    DeclareVerdict(verdict='auto_hide'),
  ],
)
