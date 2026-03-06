# Divine Moderation Service Auto-Ban
# Automatically bans content flagged by the AI moderation service.
#
# NOTE (matt, 2026-03-06): This rule checks Kind == 1984, but moderation-service
# publishes kind 1985 labels (NIP-32), not kind 1984 reports (NIP-56). For this
# rule to fire on actual moderation-service output, either:
#   1. The Nostr-Kafka bridge normalizes kind 1985 into the report model, or
#   2. This rule is rewritten to match kind 1985 with a new SML model, or
#   3. Moderation-service is changed to publish kind 1984 for its flagged content.
#
# Also: the ReportReason values here ('ai_generated', 'deepfake', 'self_harm',
# 'offensive') don't match moderation-service's actual Hive AI categories
# (sexual, violence, gore, hate, drugs, weapons, self_harm, bullying, spam).
# These should be aligned with the classifier output.

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
    ReportReason in ['ai_generated', 'deepfake', 'self_harm', 'offensive'],
  ],
  description='Divine moderation service flagged content for permanent ban',
)

WhenRules(
  rules_any=[ModerationServiceBan],
  then=[
    BanNostrEvent(event_id=ReportedEventId, pubkey=ReportedPubkey, reason='Content flagged by moderation service'),
    DeclareVerdict(verdict='auto_ban'),
  ],
)
