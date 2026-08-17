# UNREACHABLE from main.sml as of 2026-08-16. Its only importer was
# content/ai_classification.sml, which was retired because moderation-service
# already enforces its own classifications. Kept rather than deleted: it is the
# model a corrected video rule would need, and the `MediaHash` entity type it
# declares is deliberately shared with models/nostr/kind1985_label.sml so the
# label path and any future video path agree on one entity.
#
# Unlike kind1_note.sml this file has no iac coupling, so it could be deleted on
# its own. It stays for symmetry and because the type-sharing note above is worth
# preserving.

Import(rules=['models/base.sml'])

VideoUrl: str = JsonData(
  path='$.video_url',
  coerce_type=True,
  required=False
)

VideoHash: str = JsonData(
  path='$.video_hash',
  coerce_type=True,
  required=False
)

# Entity form of the video's media hash.
#
# Same 'MediaHash' type as kind1985_label.sml's LabelContentHashEntity, and that
# sharing is the point: a moderator's decision on a label that named only the media
# is recorded against this entity, and the automated classification rules read it
# back here to avoid re-deciding content a human already cleared. The bridge fills
# both from the event's `x` tag, so they describe the same blob.
#
# Normalised on both sides, or an uppercase hash on either would make a second
# entity and the guard would find nothing.
VideoHashEntity: Entity[str] = Entity(
  type='MediaHash',
  id=NormalizeMediaHash(sha256=VideoHash),
)

VideoTitle: str = JsonData(
  path='$.video_title',
  coerce_type=True,
  required=False
)
