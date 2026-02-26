Import(rules=['models/base.sml'])

VideoUrl: str = Json(
  path='$.video_url',
  coerce_type=True,
  optional=True
)

VideoHash: str = Json(
  path='$.video_hash',
  coerce_type=True,
  optional=True
)

VideoTitle: str = Json(
  path='$.video_title',
  coerce_type=True,
  optional=True
)
