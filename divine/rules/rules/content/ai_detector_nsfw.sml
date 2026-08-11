# Self-hosted NSFW evidence from divine-ai-detector.
#
# Positive findings enter human review. This rule deliberately has no ban,
# hide, restriction, or label mutation: the current model is evidence, not an
# enforcement decision.

Import(
  rules=[
    'models/base.sml',
    'models/ai_detector_nsfw.sml',
  ]
)

DetectorNsfwEvidence = Rule(
  when_all=[
    ActionName == 'ai_detector_nsfw',
    DetectorSignal == 'nsfw',
    DetectorDisposition == 'evidence',
    DetectorContentHash != '',
    DetectorClass in ['porn', 'sexy', 'hentai'],
    DetectorConfidence > 0,
  ],
  description='Self-hosted NSFW model produced evidence for human review',
)

WhenRules(
  rules_any=[DetectorNsfwEvidence],
  then=[
    DeclareVerdict(verdict='flag_for_review'),
  ],
)
