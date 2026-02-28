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

VideoTitle: str = JsonData(
  path='$.video_title',
  coerce_type=True,
  required=False
)
