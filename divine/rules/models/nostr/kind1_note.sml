Import(rules=['models/base.sml'])

NoteText: str = Json(
  path='$.content',
  coerce_type=True
)

MentionedPubkeys: list = Json(
  path='$.mentioned_pubkeys',
  coerce_type=True,
  optional=True
)
