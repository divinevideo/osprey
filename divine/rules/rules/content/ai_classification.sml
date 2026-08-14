# AI Classification Routing
#
# Routes Hive AI classification results from moderation-service into
# Osprey verdicts. The moderation-service classifies uploads into four tiers:
#   SAFE / REVIEW / AGE_RESTRICTED / PERMANENT_BAN
#
# NOTE: the claim that CheckModerationResult is "currently a stub returning
# 'unknown'" was stale and is removed. The UDF makes a real HTTP call, and
# moderation-api's /check-result endpoint answers it: probed 2026-08-12, it
# returns 200 with a JSON body carrying status/blocked/age_restricted. So these
# rules can fire, which is why the human_reviewed guards below are load-bearing
# rather than theoretical. What a hash has no result for returns 'unknown' and
# matches nothing, which is a per-item condition, not a disabled rule set.
#
# NOTE: the claim that "the bridge or an adapter needs to normalize" the
# moderation-service's kind 1985 labels was ALSO stale, and is removed. The bridge
# subscribes to kinds [1984, 1985] and extracts label_namespace / label_value
# (nostr-kafka-bridge/main.py:249-256), and label_routing.sml consumes them. Verified
# 2026-08-14. Two stale claims in one header is a pattern: verify this file's notes
# against the code before relying on them.
#
# WHAT IS still true and is a real gap: every Coop routing rule matches on
# `report_reason`, and a label-driven item carries label_value/label_namespace with no
# report_reason at all. So a classification falls through every specialist rule into
# General Review -- a Hive permanent_ban-tier item never reaches the CSAM queue. The
# fix is Coop-side routing rules keyed on label_value; no change here.

Import(
  rules=[
    'models/base.sml',
    'models/nostr/video_event.sml',
  ]
)

# Video classified as requiring age restriction (nudity, suggestive content
# that doesn't meet the ban threshold).
AgeRestricted = Rule(
  when_all=[
    Kind in [34235, 34236],
    CheckModerationResult(video_hash=VideoHash) == 'age_restricted',
    not HasLabel(entity=EventId, label='human_reviewed'),
    # Also honour a decision recorded against the MEDIA rather than this event.
    # A moderator clearing a false positive publishes a label that names the hash
    # and usually no event, so the clearance lands on VideoHashEntity and the
    # event-keyed guard above cannot see it. Without this, a human's decision was
    # silently re-litigated by the classifier on the very next evaluation.
    not HasLabel(entity=VideoHashEntity, label='human_reviewed'),
  ],
  description='AI classified video as age-restricted',
)

WhenRules(
  rules_any=[AgeRestricted],
  then=[
    LabelAdd(entity=EventId, label='age_restricted'),
    LabelAdd(entity=EventId, label='ai_classified'),
    DeclareVerdict(verdict='restrict'),
  ],
)

# Video classified as requiring human review (borderline content,
# classification confidence below threshold).
NeedsReview = Rule(
  when_all=[
    Kind in [34235, 34236],
    CheckModerationResult(video_hash=VideoHash) == 'review',
    not HasLabel(entity=EventId, label='human_reviewed'),
    # Same reason as AgeRestricted above: re-queueing a human's own clearance for
    # review is the most literal form of ignoring it.
    not HasLabel(entity=VideoHashEntity, label='human_reviewed'),
  ],
  description='AI classified video as needing human review',
)

WhenRules(
  rules_any=[NeedsReview],
  then=[
    LabelAdd(entity=EventId, label='ai_classified'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

# Video classified as permanent ban (CSAM, extreme violence, etc.).
# Auto-enforce on first sight, but never after a human has already decided.
# Without the human_reviewed guards, a moderator clearing a false-positive
# permanent_ban classification (usually a targetless rejected label writing
# human_reviewed on MediaHash) is silently re-banned on the next video
# evaluation — the clearance story this branch exists for would stop at
# AgeRestricted/NeedsReview and leave the worst tier unguarded.
PermanentBan = Rule(
  when_all=[
    Kind in [34235, 34236],
    CheckModerationResult(video_hash=VideoHash) == 'permanent_ban',
    not HasLabel(entity=EventId, label='human_reviewed'),
    not HasLabel(entity=VideoHashEntity, label='human_reviewed'),
  ],
  description='AI classified video for permanent ban',
)

WhenRules(
  rules_any=[PermanentBan],
  then=[
    BanNostrEvent(event_id=EventId, pubkey=Pubkey, reason='AI classification: permanent ban'),
    LabelAdd(entity=EventId, label='ai_classified'),
    LabelAdd(entity=Pubkey, label='warned'),
    DeclareVerdict(verdict='ban'),
  ],
)
