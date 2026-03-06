# Content moderation rules for Nostr events
#
# ai_classification: Routes Hive AI results to verdicts (restrict, review, ban)
#
# Future:
# - Text content filtering (hate speech, harassment patterns)
# - Spam link detection
# - Content provenance / C2PA signal rules

Import(rules=['models/base.sml'])

Require(rule='rules/content/ai_classification.sml')
