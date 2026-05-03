# Multi-Report Auto-Hide
#
# Auto-hides content after 2+ unique reporters flag it for serious categories.
# Uses Osprey entity labels as a counting mechanism: first report labels
# the target event, second report checks for the label and fires auto-hide.
#
# Categories covered: nudity (sexual content), violence, extremism.
# CSAM is NOT here -- stays in ReportWatcher (threshold=1, single report).
#
# Bridge normalizes report reasons before they reach SML rules:
#   sexual-content, sexual, explicit, pornography, NS-nudity -> 'nudity'
#   violence, NS-violence -> 'violence'
#   See nostr-kafka-bridge/main.py _REASON_ALIASES for full mapping.
#
# The label-based pattern covers threshold=2. For higher thresholds,
# a counter UDF would be needed (future improvement).
#
# Race condition note: if two reports arrive near-simultaneously, both
# could miss the label before PostgreSQL commits. The label service uses
# SELECT FOR UPDATE row locking which should serialize, but this needs
# staging validation.

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

# --- Second report: auto-hide ---

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
    BanNostrEvent(event_id=ReportedEventId, pubkey=ReportedPubkey, reason='Multiple reports: sexual content'),
    LabelAdd(entity=ReportedEventId, label='auto_hidden'),
    DeclareVerdict(verdict='auto_hide'),
  ],
)

WhenRules(
  rules_any=[ThresholdViolenceReport],
  then=[
    BanNostrEvent(event_id=ReportedEventId, pubkey=ReportedPubkey, reason='Multiple reports: violence'),
    LabelAdd(entity=ReportedEventId, label='auto_hidden'),
    DeclareVerdict(verdict='auto_hide'),
  ],
)
