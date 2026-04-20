# Divine Moderation Service Signal (kind 1984 reports)
#
# Handles kind 1984 events published by moderation-service for automated
# AI classifications. Uses NOSTR_PRIVATE_KEY with MOD namespace labels NS/VI/AI.
# The bridge normalizes these to 'nudity', 'violence', 'ai_generated'.
#
# This rule is a SIGNAL only -- it flags content for human review but does
# not enforce bans directly. Two reasons:
#
# 1. moderation-service kind 1984 events use ['p', sha256] (video hash, not
#    a real pubkey) and have no 'e' tag, so ReportedEventId is empty and
#    ReportedPubkey is a sha256. BanNostrEvent with those identifiers would
#    fail or produce incorrect bans.
#
# 2. Enforcement with real Nostr identifiers is handled by ai_classification.sml
#    (which operates on actual video events and calls the moderation API directly)
#    and label_routing.sml (which fires on kind 1985 human-verified decisions).
#
# This path is one of two for moderation-service output into Osprey:
#   - Kind 1984 (this file): automated AI signal, routes to human review
#   - Kind 1985 (content/label_routing.sml): human-verified decisions, enforces
#
# Naming note: the rule is still called `ModerationServiceBan` to match the
# existing `ModerationServiceBan` column in the `osprey.osprey_events`
# ClickHouse schema. Rule names become ClickHouse columns, so renaming the
# rule without a coordinated ALTER TABLE breaks every output sink flush.
# The semantics have changed (signal-only, no ban) but the name stays until
# we land a paired iac-coreconfig column rename.

Import(
  rules=[
    'models/base.sml',
    'models/nostr/kind1984_report.sml',
  ]
)

ModerationServiceBan = Rule(
  when_all=[
    Kind == 1984,
    HasLabel(entity=Pubkey, label='moderation_service'),
    ReportReason in ['nudity', 'violence', 'ai_generated'],
  ],
  description='Divine moderation service flagged content for human review (signal only, name retained for ClickHouse schema compatibility)',
)

WhenRules(
  rules_any=[ModerationServiceBan],
  then=[
    LabelAdd(entity=EventId, label='ai_classified'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)
