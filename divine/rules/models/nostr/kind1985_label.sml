# NIP-32 Label Event (kind 1985)
#
# Published by moderation-service for human-verified content decisions.
# Uses 'content-warning' namespace with category labels (nudity, violence,
# ai-generated, etc.) and metadata JSON containing confidence scores.
#
# Tag structure:
#   ['L', 'content-warning']              -- namespace declaration
#   ['l', '<label>', 'content-warning', '<metadata_json>']  -- label + metadata
#   ['e', '<nostr_event_id>']             -- referenced event (if known)
#   ['x', '<sha256>']                     -- content hash
#   ['r', '<cdn_url>']                    -- reference URL
#
# Metadata JSON: { confidence, verified, source, sha256, [rejected] }
#
# Rejected labels use 'not-<label>' format (e.g., 'not-nudity').

Import(rules=['models/base.sml'])

LabelNamespace: str = JsonData(
  path='$.label_namespace',
  coerce_type=True,
  required=False
)

LabelValue: str = JsonData(
  path='$.label_value',
  coerce_type=True,
  required=False
)

LabelMetadata: str = JsonData(
  path='$.label_metadata',
  coerce_type=True,
  required=False
)

LabelTargetEvent: str = JsonData(
  path='$.label_target_event',
  coerce_type=True,
  required=False
)

# Entity version for use with LabelAdd/HasLabel (needs EntityT, not plain str).
# Type 'ReportedEventId' matches kind1984_report.sml so labels are shared
# across report rules and label routing rules (e.g., human_reviewed guard).
LabelTargetEventEntity: Entity[str] = EntityJson(
  type='ReportedEventId',
  path='$.label_target_event',
  coerce_type=True
)

# AUTHORITATIVE author of the labelled event, resolved from that event rather
# than from this label's signer. On a label the signer is our own moderation
# identity, so anything enforcing on it would target us. '' when it cannot be
# resolved; consumers must decline to enforce rather than fall back.
LabelTargetAuthorPubkey: str = ResolveEventAuthor(event_id=LabelTargetEvent)

# CLAIMED target. Written into the label by its signer and not verified against
# the labelled event. Do not enforce on this; see LabelTargetAuthorPubkey above.
LabelTargetPubkey: str = JsonData(
  path='$.label_target_pubkey',
  coerce_type=True,
  required=False
)

LabelContentHash: str = JsonData(
  path='$.label_content_hash',
  coerce_type=True,
  required=False
)

# Entity form of the media hash, for labelling a decision that has no event to
# attach to.
#
# A label carries the `e` tag only when the publisher has an event id, so the
# ordinary shape names the media and nothing else. Without a hash-keyed entity a
# moderator's decision on that shape can be recorded nowhere: LabelTargetEventEntity
# is empty, so `human_reviewed` goes to no entity at all and the automated rules
# re-decide content a human already settled.
#
# Type 'MediaHash' is shared with video_event.sml so the two paths agree: the label
# writes here and the removed ai_classification.sml read the same entity off the video
# own hash. The id is normalised for exactly that reason, since either side may
# arrive uppercased and two spellings would be two entities.
LabelContentHashEntity: Entity[str] = Entity(
  type='MediaHash',
  id=NormalizeMediaHash(sha256=LabelContentHash),
)

LabelConfidence: float = JsonData(
  path='$.label_confidence',
  coerce_type=True,
  required=False
)

LabelSource: str = JsonData(
  path='$.label_source',
  coerce_type=True,
  required=False
)

LabelRejected: bool = JsonData(
  path='$.label_rejected',
  coerce_type=True,
  required=False
)

# Plain string version of the event signer pubkey for rule conditions.
# The Entity version (Pubkey in base.sml) can't be compared against
# string literals in SML conditions; this accessor enables trusted
# signer checks in label_routing.sml.
LabelSignerPubkey: str = JsonData(
  path='$.pubkey',
  coerce_type=True,
  required=False
)
