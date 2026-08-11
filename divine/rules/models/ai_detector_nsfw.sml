# Evidence emitted by divine-ai-detector after it classifies fetched video
# bytes with the self-hosted NSFW model. These names are deliberately separate
# from NIP-32 Label* fields: this is machine evidence, not a signed label event.

Import(rules=['models/base.sml'])

DetectorContentHash: str = JsonData(
  path='$.sha256',
  coerce_type=True,
  required=False
)

DetectorVideoUrl: str = JsonData(
  path='$.video_url',
  coerce_type=True,
  required=False
)

DetectorSignal: str = JsonData(
  path='$.signal',
  coerce_type=True,
  required=False
)

DetectorClass: str = JsonData(
  path='$.class',
  coerce_type=True,
  required=False
)

DetectorConfidence: float = JsonData(
  path='$.confidence',
  coerce_type=True,
  required=False
)

DetectorFramesFlagged: int = JsonData(
  path='$.frames_flagged',
  coerce_type=True,
  required=False
)

DetectorTotalFrames: int = JsonData(
  path='$.total_frames',
  coerce_type=True,
  required=False
)

DetectorModel: str = JsonData(
  path='$.model',
  coerce_type=True,
  required=False
)

DetectorDisposition: str = JsonData(
  path='$.disposition',
  coerce_type=True,
  required=False
)
