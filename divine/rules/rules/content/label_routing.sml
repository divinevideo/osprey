# Label Routing -- Human-Verified Content Decisions
#
# Routes kind 1985 label events from moderation-service into Osprey
# verdicts. These labels represent human moderator decisions (confirmed
# or rejected) published via the swipe review UI.
#
# This is the primary path for getting moderation-service decisions into
# Osprey. The moderation-service classifies content (Hive AI), a human
# confirms or rejects via the admin dashboard, and a kind 1985 label
# event is published to the relay. The bridge extracts label fields
# and Osprey evaluates them here.
#
# The ai_classification.sml rules (via CheckModerationResult UDF) are
# a secondary path that checks the moderation API directly for video
# events that may not have label events yet.
#
# See also: reports/moderation_service.sml for kind 1984 automated
# reports (separate flow, different tag structure).
#
# Security: all rules gate on LabelSignerPubkey matching the trusted
# moderation identity. The LabelSource metadata field is user-controlled
# and must not be trusted alone for enforcement decisions.

Import(
  rules=[
    'models/base.sml',
    'models/nostr/kind1985_label.sml',
  ]
)

# Trusted moderation identity (NIP-05: moderation@divine.video).
# All enforcement rules require the kind 1985 event be signed by this
# pubkey, not just carry 'source: human-moderator' in metadata.
TRUSTED_MODERATION_PUBKEY = '8fd5eb6d8f362163bc00a5ab6b4a3167dbf32d00ec4efdbcf43b3c9514433b7e'

# --- Confirmed labels (human verified positive) ---

# Human confirmed: content contains nudity/sexual material.
# Map to age restriction (not ban) per North Star policy:
# consensual non-violent adult content OK if properly labeled/age-gated.
ConfirmedNudity = Rule(
  when_all=[
    Kind == 1985,
    LabelSignerPubkey == TRUSTED_MODERATION_PUBKEY,
    LabelNamespace == 'content-warning',
    LabelValue in ['nudity', 'sexual', 'explicit', 'pornography'],
    LabelSource == 'human-moderator',
    not LabelRejected,
  ],
  description='Human confirmed nudity/sexual content',
)

WhenRules(
  rules_any=[ConfirmedNudity],
  then=[
    LabelAdd(entity=LabelTargetEventEntity, label='age_restricted'),
    LabelAdd(entity=LabelTargetEventEntity, label='human_reviewed'),
    DeclareVerdict(verdict='restrict'),
  ],
)

# Human confirmed: content contains violence/gore.
ConfirmedViolence = Rule(
  when_all=[
    Kind == 1985,
    LabelSignerPubkey == TRUSTED_MODERATION_PUBKEY,
    LabelNamespace == 'content-warning',
    LabelValue in ['violence', 'gore', 'graphic-violence'],
    LabelSource == 'human-moderator',
    not LabelRejected,
  ],
  description='Human confirmed violence/gore content',
)

WhenRules(
  rules_any=[ConfirmedViolence],
  then=[
    LabelAdd(entity=LabelTargetEventEntity, label='age_restricted'),
    LabelAdd(entity=LabelTargetEventEntity, label='human_reviewed'),
    DeclareVerdict(verdict='restrict'),
  ],
)

# Human confirmed: CSAM or equivalent. Immediate ban, no nuance.
ConfirmedCSAM = Rule(
  when_all=[
    Kind == 1985,
    LabelSignerPubkey == TRUSTED_MODERATION_PUBKEY,
    LabelNamespace == 'content-warning',
    LabelValue in ['csam', 'sexual_minors'],
    LabelSource == 'human-moderator',
    not LabelRejected,
  ],
  description='Human confirmed CSAM',
)

WhenRules(
  rules_any=[ConfirmedCSAM],
  then=[
    # LabelTargetEvent = content event ID (from e tag), not the label event's own ID.
    # Pubkey here is the label publisher (moderation account). Content creator's pubkey
    # requires p-tag in the label event (label_target_pubkey); pass empty to avoid
    # accidentally banning the moderator. Sink skips pubkey ban when empty.
    BanNostrEvent(event_id=LabelTargetEvent, pubkey='', reason='Human confirmed CSAM'),
    LabelAdd(entity=LabelTargetEventEntity, label='human_reviewed'),
    DeclareVerdict(verdict='ban'),
  ],
)

# Human confirmed: content is AI-generated or deepfake.
# Flag for review rather than auto-ban. Policy on AI content is
# still evolving (per Mar 9 call: Divine's mission is authenticity
# verification, but response is TBD).
ConfirmedAIGenerated = Rule(
  when_all=[
    Kind == 1985,
    LabelSignerPubkey == TRUSTED_MODERATION_PUBKEY,
    LabelNamespace == 'content-warning',
    LabelValue in ['ai-generated', 'deepfake'],
    LabelSource == 'human-moderator',
    not LabelRejected,
  ],
  description='Human confirmed AI-generated or deepfake content',
)

WhenRules(
  rules_any=[ConfirmedAIGenerated],
  then=[
    LabelAdd(entity=LabelTargetEventEntity, label='human_reviewed'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

# --- Rejected labels (human verified false positive) ---

# Human rejected AI classification. Mark as reviewed, no enforcement.
# This protects against re-classification by automated rules.
RejectedLabel = Rule(
  when_all=[
    Kind == 1985,
    LabelSignerPubkey == TRUSTED_MODERATION_PUBKEY,
    LabelNamespace == 'content-warning',
    LabelSource == 'human-moderator',
    LabelRejected,
  ],
  description='Human rejected AI classification (false positive)',
)

WhenRules(
  rules_any=[RejectedLabel],
  then=[
    LabelAdd(entity=LabelTargetEventEntity, label='human_reviewed'),
    DeclareVerdict(verdict='approve'),
  ],
)
