EventId: Entity[str] = EntityJson(
  type='EventId',
  path='$.event_id',
  coerce_type=True
)

Pubkey: Entity[str] = EntityJson(
  type='Pubkey',
  path='$.pubkey',
  coerce_type=True
)

Kind: int = Json(
  path='$.kind',
  coerce_type=True
)

CreatedAt: int = Json(
  path='$.created_at',
  coerce_type=True
)

Content: str = Json(
  path='$.content',
  coerce_type=True,
  optional=True
)

Tags: list = Json(
  path='$.tags',
  coerce_type=True,
  optional=True
)

ActionName=GetActionName()
