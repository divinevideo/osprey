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

# Trusted moderation identity. All enforcement rules require the kind 1985
# event be signed by a trusted moderation pubkey, not merely carry
# 'source: human-moderator' in metadata, which is attacker-controlled.
#
# The identity differs per environment, so it is resolved at runtime from
# DIVINE_TRUSTED_MODERATION_PUBKEYS rather than hardcoded here. Unset, it
# defaults to production (NIP-05 moderation@divine.video), so production is
# unchanged. Hardcoding it previously made these rules unexercisable outside
# production: no label signed by the production key has ever reached the
# staging relay, so they could never fire there and a staging validation would
# silently do nothing. See divine/plugins/src/trusted_moderation.py.

# --- Confirmed labels (human verified positive) ---

# Age-restriction keys off the MEDIA HASH, not the event.
#
# /api/moderate-media takes {sha256, action, reason}; the event id is carried
# only for logging (services/relay_manager_sink.py::_age_restrict_media). The
# publishing side sets the imeta `x` tag unconditionally and the `e` tag only
# when it has one, so hash-present/target-absent is the COMMON shape, not an
# edge case. Rules therefore split on the hash, and the target only decides
# whether we can also label the event entity.
#
# These enforce rather than queueing a second opinion: the label already IS a
# human decision (LabelSource == 'human-moderator', signed by a trusted
# moderation key), and age-restriction is a reversible auth-gate rather than a
# removal, so a wrong call is correctable. Labels missing the hash cannot be
# enforced at all and route to review below.
#
# `reason` is USER-FACING. relay-manager forwards it to moderation-service,
# which renders it to the creator as "... was found to {reason}" (see
# nostr/dm-sender.mjs TEMPLATES.AGE_RESTRICTED). Phrase it to complete that
# sentence, not as a log line.

# Human confirmed: content contains nudity/sexual material.
# Map to age restriction (not ban) per North Star policy:
# consensual non-violent adult content OK if properly labeled/age-gated.
ConfirmedNudity = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelValue in ['nudity', 'sexual', 'explicit', 'pornography'],
    LabelSource == 'human-moderator',
    not LabelRejected,
    # Validity, not mere presence: a malformed hash would otherwise declare a
    # restriction here that the sink then declines to make (see IsValidMediaHash).
    IsValidMediaHash(sha256=LabelContentHash),
    LabelTargetEvent != None,
    LabelTargetEvent != '',
  ],
  description='Human confirmed nudity/sexual content (media hash and event target)',
)

WhenRules(
  rules_any=[ConfirmedNudity],
  then=[
    AgeRestrictNostrEvent(event_id=LabelTargetEvent, sha256=LabelContentHash, reason='contain nudity or sexual content'),
    LabelAdd(entity=LabelTargetEventEntity, label='age_restricted'),
    LabelAdd(entity=LabelTargetEventEntity, label='human_reviewed'),
    DeclareVerdict(verdict='restrict'),
  ],
)

# Same decision, no event target. Enforces identically -- the hash is all the
# media call needs. Cannot label the event entity, since there is no entity.
# Two rules because the engine distinguishes a null target from an empty one.
ConfirmedNudityHashOnlyNullTarget = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelValue in ['nudity', 'sexual', 'explicit', 'pornography'],
    LabelSource == 'human-moderator',
    not LabelRejected,
    IsValidMediaHash(sha256=LabelContentHash),
    LabelTargetEvent == None,
  ],
  description='Human confirmed nudity/sexual content (hash only, null event target)',
)

ConfirmedNudityHashOnlyEmptyTarget = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelValue in ['nudity', 'sexual', 'explicit', 'pornography'],
    LabelSource == 'human-moderator',
    not LabelRejected,
    IsValidMediaHash(sha256=LabelContentHash),
    LabelTargetEvent == '',
  ],
  description='Human confirmed nudity/sexual content (hash only, empty event target)',
)

WhenRules(
  rules_any=[ConfirmedNudityHashOnlyNullTarget, ConfirmedNudityHashOnlyEmptyTarget],
  then=[
    # Review, NOT enforcement. These branches used to drop silently; giving them
    # an outcome is the fix. Making that outcome an automatic age-restrict is a
    # separate change in enforcement posture and is deliberately not made here:
    # a hash with no event target is the COMMON shape, so it would convert most
    # confirmed labels from human review to automation in a bug-fix branch.
    # Tracked separately; see the enforcement-posture issue.
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

# Human confirmed: content contains violence/gore.
ConfirmedViolence = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelValue in ['violence', 'gore', 'graphic-violence'],
    LabelSource == 'human-moderator',
    not LabelRejected,
    # See ConfirmedNudity: validity, not presence.
    IsValidMediaHash(sha256=LabelContentHash),
    LabelTargetEvent != None,
    LabelTargetEvent != '',
  ],
  description='Human confirmed violence/gore content (media hash and event target)',
)

WhenRules(
  rules_any=[ConfirmedViolence],
  then=[
    AgeRestrictNostrEvent(event_id=LabelTargetEvent, sha256=LabelContentHash, reason='contain violent or graphic content'),
    LabelAdd(entity=LabelTargetEventEntity, label='age_restricted'),
    LabelAdd(entity=LabelTargetEventEntity, label='human_reviewed'),
    DeclareVerdict(verdict='restrict'),
  ],
)

# See ConfirmedNudityHashOnly*: same decision, no event target, reviewed the same.
ConfirmedViolenceHashOnlyNullTarget = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelValue in ['violence', 'gore', 'graphic-violence'],
    LabelSource == 'human-moderator',
    not LabelRejected,
    IsValidMediaHash(sha256=LabelContentHash),
    LabelTargetEvent == None,
  ],
  description='Human confirmed violence/gore content (hash only, null event target)',
)

ConfirmedViolenceHashOnlyEmptyTarget = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelValue in ['violence', 'gore', 'graphic-violence'],
    LabelSource == 'human-moderator',
    not LabelRejected,
    IsValidMediaHash(sha256=LabelContentHash),
    LabelTargetEvent == '',
  ],
  description='Human confirmed violence/gore content (hash only, empty event target)',
)

WhenRules(
  rules_any=[ConfirmedViolenceHashOnlyNullTarget, ConfirmedViolenceHashOnlyEmptyTarget],
  then=[
    # See the nudity block above: review, not enforcement, on purpose.
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

# Human confirmed nudity/violence, but the label carries no ACTIONABLE media
# hash -- absent, empty, or malformed. Enforcement is addressed by hash, so
# there is nothing to act on; route to a human who can locate the media.
#
# One condition covers all three cases: IsValidMediaHash treats absent and
# malformed alike, because neither can be enforced. That matters -- gating
# enforcement on validity without a matching review path here would leave a
# malformed hash matching no rule at all, silently dropped, which is exactly
# the gap this section exists to close.

ConfirmedAgeRestrictNoValidHash = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelValue in ['nudity', 'sexual', 'explicit', 'pornography', 'violence', 'gore', 'graphic-violence'],
    LabelSource == 'human-moderator',
    not LabelRejected,
    not IsValidMediaHash(sha256=LabelContentHash),
    LabelTargetEvent != None,
    LabelTargetEvent != '',
  ],
  description='Human confirmed nudity/violence, no actionable media hash -- needs a human',
)

WhenRules(
  rules_any=[ConfirmedAgeRestrictNoValidHash],
  then=[
    LabelAdd(entity=LabelTargetEventEntity, label='human_reviewed'),
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

# Neither an actionable hash nor an event target. Nothing to enforce and no
# entity to label, but it still gets a verdict rather than vanishing.
# Two rules because the engine distinguishes a null target from an empty one.
ConfirmedAgeRestrictNoValidHashNullTarget = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelValue in ['nudity', 'sexual', 'explicit', 'pornography', 'violence', 'gore', 'graphic-violence'],
    LabelSource == 'human-moderator',
    not LabelRejected,
    not IsValidMediaHash(sha256=LabelContentHash),
    LabelTargetEvent == None,
  ],
  description='Human confirmed nudity/violence, no actionable hash, null event target',
)

ConfirmedAgeRestrictNoValidHashEmptyTarget = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelValue in ['nudity', 'sexual', 'explicit', 'pornography', 'violence', 'gore', 'graphic-violence'],
    LabelSource == 'human-moderator',
    not LabelRejected,
    not IsValidMediaHash(sha256=LabelContentHash),
    LabelTargetEvent == '',
  ],
  description='Human confirmed nudity/violence, no actionable hash, empty event target',
)

WhenRules(
  rules_any=[ConfirmedAgeRestrictNoValidHashNullTarget, ConfirmedAgeRestrictNoValidHashEmptyTarget],
  then=[
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

# Human confirmed: CSAM or equivalent. Immediate ban, no nuance.
ConfirmedCSAM = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelValue in ['csam', 'sexual_minors'],
    LabelSource == 'human-moderator',
    not LabelRejected,
    LabelTargetEvent != None,
    LabelTargetEvent != '',
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

# Human confirmed CSAM but label has no event target (hash-only).
# Can't ban an event we don't have an ID for, but the content hash
# is actionable. Route to manual review so a human can locate and
# remove the content by hash.
ConfirmedCSAMHashOnlyNullTarget = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelValue in ['csam', 'sexual_minors'],
    LabelSource == 'human-moderator',
    not LabelRejected,
    LabelContentHash != '',
    LabelTargetEvent == None,
  ],
  description='Human confirmed CSAM (hash only, null event target)',
)

ConfirmedCSAMHashOnlyEmptyTarget = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelValue in ['csam', 'sexual_minors'],
    LabelSource == 'human-moderator',
    not LabelRejected,
    LabelContentHash != '',
    LabelTargetEvent == '',
  ],
  description='Human confirmed CSAM (hash only, empty event target)',
)

WhenRules(
  rules_any=[ConfirmedCSAMHashOnlyNullTarget, ConfirmedCSAMHashOnlyEmptyTarget],
  then=[
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

# Human confirmed: content is AI-generated or deepfake.
# Flag for review rather than auto-ban. Policy on AI content is
# still evolving (per Mar 9 call: Divine's mission is authenticity
# verification, but response is TBD).
ConfirmedAIGenerated = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelValue in ['ai-generated', 'deepfake'],
    LabelSource == 'human-moderator',
    not LabelRejected,
    LabelTargetEvent != None,
    LabelTargetEvent != '',
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

# Same decision, no event target. The publisher sets the `e` tag only when it has
# an event id, so this is the ordinary shape of a label rather than an edge case,
# and without these two rules it matched nothing at all: no verdict, no COOP item,
# no telemetry row. The verdict matches the target-present case because the
# moderator's decision is the same; only our ability to label the event differs.
#
# `!= None` does not exclude '' and `== None` does not catch it, so both spellings
# are needed or one shape still falls through.
ConfirmedAIGeneratedNullTarget = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelValue in ['ai-generated', 'deepfake'],
    LabelSource == 'human-moderator',
    not LabelRejected,
    LabelTargetEvent == None,
  ],
  description='Human confirmed AI-generated or deepfake content (null event target)',
)

ConfirmedAIGeneratedEmptyTarget = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelValue in ['ai-generated', 'deepfake'],
    LabelSource == 'human-moderator',
    not LabelRejected,
    LabelTargetEvent == '',
  ],
  description='Human confirmed AI-generated or deepfake content (empty event target)',
)

WhenRules(
  rules_any=[ConfirmedAIGeneratedNullTarget, ConfirmedAIGeneratedEmptyTarget],
  then=[
    DeclareVerdict(verdict='flag_for_review'),
  ],
)

# --- Rejected labels (human verified false positive) ---

# Human rejected AI classification. Mark as reviewed, no enforcement.
# This protects against re-classification by automated rules.
RejectedLabel = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelSource == 'human-moderator',
    LabelRejected,
    LabelTargetEvent != None,
    LabelTargetEvent != '',
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

# A rejection with no event target. This was the costliest of the missing shapes:
# a moderator clearing a false positive through the admin category-verification
# path publishes exactly this label, and it matched nothing, so the clearance left
# no trace anywhere in Osprey.
#
# The verdict alone does not yet stop automated re-classification. That guard is
# keyed on `human_reviewed` against the event entity, which a targetless label
# cannot write, and closing it is a separate change to ai_classification.sml
# rather than something to fold in here silently.
RejectedLabelNullTarget = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelSource == 'human-moderator',
    LabelRejected,
    LabelTargetEvent == None,
  ],
  description='Human rejected AI classification (false positive, null event target)',
)

RejectedLabelEmptyTarget = Rule(
  when_all=[
    Kind == 1985,
    IsTrustedModerationSigner(pubkey=LabelSignerPubkey),
    LabelNamespace == 'content-warning',
    LabelSource == 'human-moderator',
    LabelRejected,
    LabelTargetEvent == '',
  ],
  description='Human rejected AI classification (false positive, empty event target)',
)

WhenRules(
  rules_any=[RejectedLabelNullTarget, RejectedLabelEmptyTarget],
  then=[
    DeclareVerdict(verdict='approve'),
  ],
)
