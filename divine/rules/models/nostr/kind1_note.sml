Import(rules=['models/base.sml'])

NoteText: str = JsonData(
  path='$.content',
  coerce_type=True
)

MentionedPubkeys: list = JsonData(
  path='$.mentioned_pubkeys',
  coerce_type=True,
  required=False
)
