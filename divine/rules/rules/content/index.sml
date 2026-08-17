# Content moderation rules for Nostr events
#
# label_routing: Routes kind 1985 human-verified labels to verdicts
#                (the path moderation-service decisions actually arrive on)
# ai_detector_nsfw: Self-hosted NSFW model evidence, review only, no enforcement
#
# ai_classification was removed on 2026-08-16. It duplicated enforcement
# moderation-service already performs on its own Hive results, and could never
# fire on staging: DIVINE_MODERATION_API_URL is unset there and there is no
# staging moderation-api to point it at.
#
# Future:
# - Text content filtering (hate speech, harassment patterns)
# - Spam link detection
# - Content provenance / C2PA signal rules

Import(rules=['models/base.sml'])

Require(rule='rules/content/label_routing.sml')
Require(rule='rules/content/ai_detector_nsfw.sml')
