Import(rules=['models/base.sml'])

VideoUrl: str = JsonData(
  path='$.video_url',
  coerce_type=True,
  optional=True
)

VideoHash: str = JsonData(
  path='$.video_hash',
  coerce_type=True,
  optional=True
)

VideoTitle: str = JsonData(
  path='$.video_title',
  coerce_type=True,
  optional=True
)
